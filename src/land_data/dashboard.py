"""Local review dashboard for LAND-DATA.

The dashboard is intentionally small and dependency-free.  It is a local
operator surface, not a public web application: every request opens its own
SQLite connection, GET requests never mutate editorial state, and all POST
operations require both a session CSRF nonce and an explicit reviewer/reason.
"""

import collections
import hashlib
import html
import ipaddress
import json
import secrets
import socket
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

from .knowledge_bridge import prepare_release, release_diff, verify_release
from .knowledge_processing import (ensure_knowledge_schema, knowledge_review_history,
                                   knowledge_drafts, review_knowledge_draft)
from .local_classifier import current_classifications
from .dashboard_workflows import DashboardWorkflowService
from .dashboard_ui import render_dashboard_html
from .models import (ContentClassification, Event, EventCandidate, FieldProvenance, OfficialGuidancePageState,
                     OfficialGuidanceSnapshot,
                     POI, POICandidate, POIMention, RawSourceRecord, SourceContent,
                     SourceDiscoveryCandidate, SourceDiscoveryRunRecord, SourceRunRecord)
from .release_install import install_release, rollback_release
from .place_release_bridge import (diff_place_release, install_place_release,
                                    prepare_place_release, preflight_place_release,
                                    resolve_city_id, rollback_place_release,
                                    verify_place_release)
from .review import REVIEW_TYPES as SERVICE_REVIEW_TYPES
from .review import ReviewService, safe_display, safe_text
from .sqlite_storage import SQLiteStorage
from .util import utc_now
from .xhs_processing import ensure_xhs_schema


SESSION_COOKIE = "land_dashboard_session"
ACCESS_COOKIE = "land_dashboard_access"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_ITEMS = 1000

REVIEW_MODELS = {
    "candidate": POICandidate,
    "mention": POIMention,
    "poi": POI,
    "event_candidate": EventCandidate,
    "event": Event,
    # ReviewService uses these canonical names; the UI keeps explicit names
    # so the table does not confuse a page snapshot with a POI.
    "official_guidance_snapshot": OfficialGuidanceSnapshot,
    "source_discovery_candidate": SourceDiscoveryCandidate,
}
SERVICE_TO_DASHBOARD_KIND = {
    "candidate": "candidate", "mention": "mention", "poi": "poi",
    "event_candidate": "event_candidate", "event": "event",
    "official_guidance": "official_guidance_snapshot",
    "source_discovery": "source_discovery_candidate",
}
DASHBOARD_TO_SERVICE_KIND = {value: key for key, value in SERVICE_TO_DASHBOARD_KIND.items()}
READ_ONLY_KINDS = {
    "event_candidate", "event", "official_guidance_snapshot",
    "source_discovery_candidate", "source_discovery_run", "source_content",
    "raw_source_record", "source_run", "processing_state", "content_classification",
}


def _now_epoch() -> float:
    return time.time()


def _bucket(status: str) -> str:
    status = str(status or "").upper()
    if status in {"NEEDS_REVIEW", "DRAFT_NEEDS_REVIEW", "REVIEW_REQUIRED", "PARSE_ERROR",
                  "PENDING_LLM", "KNOWLEDGE_REVIEW", "DISCOVERED", "REVIEW",
                  "CLASSIFIER_REVIEW", "MODEL_OPTIONAL"} or "REQUIRES_REVIEW" in status:
        return "REVIEW_REQUIRED"
    if status in {"EDITORIAL_ACCEPTED_FOR_BRIDGE", "VERIFIED"}:
        return "REVIEWED_ACCEPTED"
    if status in {"AUTO_APPROVED", "SUPPORTED", "DONE", "SKIPPED", "NOT_NEEDED", "SUCCEEDED",
                  "COMPLETE", "CONTINUE_REQUIRED", "AVAILABLE", "CLASSIFIED"}:
        return "AUTO_ACCEPTED"
    if status in {"REJECTED", "EDITORIAL_REJECTED"}:
        return "REVIEWED_REJECTED"
    if status in {"PREPARED"}:
        return "PUBLISH_READY"
    if status in {"INSTALLED"}:
        return "PUBLISHED"
    if status in {"FAILED", "ERROR", "UNAVAILABLE", "RETRY_REQUIRED", "BLOCKED",
                  "PARTIAL_FAILURE", "PARSE_ERROR"} or "FAILED" in status:
        return "FAILED_OR_BLOCKED"
    return "INFORMATIONAL"


def _status_label(status: str) -> str:
    return {
        "NEEDS_REVIEW": "需要审核", "DRAFT_NEEDS_REVIEW": "草稿待审核",
        "AUTO_APPROVED": "自动通过", "EDITORIAL_ACCEPTED_FOR_BRIDGE": "人工通过（待发布）",
        "EDITORIAL_REJECTED": "人工拒绝", "REJECTED": "已拒绝", "DONE": "已处理",
        "SKIPPED": "规则跳过", "PENDING_LLM": "等待模型", "ERROR": "处理失败",
        "PREPARED": "已准备发布", "VERIFIED": "已验证发布包", "INSTALLED": "已安装",
        "SUCCEEDED": "成功", "NOT_NEEDED": "无需更新", "RETRY_REQUIRED": "等待重试",
        "CONTINUE_REQUIRED": "需要继续", "UNAVAILABLE": "暂不可用", "PARSE_ERROR": "解析待审",
        "DISCOVERED": "已发现待判断", "SUPPORTED": "来源已接受",
        "CLASSIFIED": "本地分类完成", "CLASSIFIER_REVIEW": "分类后需审核",
        "MODEL_OPTIONAL": "可选模型抽取",
    }.get(status, status or "未标记")


def _safe_label(value, limit=180):
    return safe_text(str(value or ""), limit) or ""


def _iso_or_empty(value):
    return value if isinstance(value, str) else ""


def _host_without_port(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _host_allowed(host_header: str) -> bool:
    """Only accept loopback/private/link-local hosts for LAN operation."""
    host = _host_without_port(host_header)
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local")
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def _lan_address() -> str:
    """Best-effort local address for the printed URL; no packet is sent."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            value = sock.getsockname()[0]
        finally:
            sock.close()
        if value and value != "0.0.0.0":
            return value
    except OSError:
        pass
    try:
        value = socket.gethostbyname(socket.gethostname())
        if value:
            return value
    except OSError:
        pass
    return "127.0.0.1"


def _cookie_header(name: str, value: str, *, http_only=True, max_age=None) -> str:
    parts = ["{}={}".format(name, value), "Path=/", "SameSite=Lax"]
    if http_only:
        parts.append("HttpOnly")
    if max_age is not None:
        parts.append("Max-Age={}".format(max_age))
    return "; ".join(parts)


def _parse_cookies(header: str) -> Dict[str, str]:
    cookie = SimpleCookie()
    try:
        cookie.load(header or "")
    except (TypeError, ValueError):
        return {}
    return {key: morsel.value for key, morsel in cookie.items()}


def _json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length < 0 or length > 256_000:
        raise ValueError("request body is too large")
    raw = handler.rfile.read(length)
    content_type = (handler.headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        value = json.loads(raw.decode("utf-8")) if raw else {}
    else:
        value = {key: values[-1] for key, values in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


class DashboardApp:
    """Request-independent application object used by the HTTP handler and tests."""

    def __init__(self, db_path: str, root: str, *, bind_host="127.0.0.1", allow_install=False,
                 land_root=None, releases_root=None, access_token=None):
        self.db_path = str(Path(db_path).resolve())
        self.root = Path(root).resolve()
        self.config = None
        # Loading config is deliberately lazy.  This keeps unit tests able to
        # use a minimal temporary root while production uses the normal config.
        from .config import load_config
        self.config = load_config(str(self.root))
        self.bind_host = bind_host
        self.allow_install = bool(allow_install)
        self.land_root = Path(land_root or (self.root / ".." / "LAND")).resolve()
        self.releases_root = Path(releases_root or (self.root / ".local" / "releases")).resolve()
        self.external_access = bind_host not in {"127.0.0.1", "localhost", "::1"}
        self.access_token = access_token or (secrets.token_urlsafe(32) if self.external_access else None)
        self.workflows = DashboardWorkflowService(
            self.db_path, str(self.root), land_root=str(self.land_root))
        self._sessions = {}
        self._session_lock = threading.RLock()

    def _storage(self):
        """A fresh connection per request, required for launchd/CLI concurrency."""
        storage = SQLiteStorage(self.db_path)
        ensure_xhs_schema(storage.connection)
        ensure_knowledge_schema(storage.connection)
        return storage

    def _ensure_action_schema(self, storage):
        storage.connection.executescript("""
        CREATE TABLE IF NOT EXISTS dashboard_action_log (
          id TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
          action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
          created_at TEXT NOT NULL, result_json TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS dashboard_action_log_no_update
          BEFORE UPDATE ON dashboard_action_log
          BEGIN SELECT RAISE(ABORT, 'dashboard action log is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS dashboard_action_log_no_delete
          BEFORE DELETE ON dashboard_action_log
          BEGIN SELECT RAISE(ABORT, 'dashboard action log is append-only'); END;
        """)
        storage.connection.commit()

    def _session(self, sid=None):
        now = _now_epoch()
        with self._session_lock:
            expired = [key for key, value in self._sessions.items()
                       if now - value["created"] > SESSION_TTL_SECONDS]
            for key in expired:
                self._sessions.pop(key, None)
            if sid and sid in self._sessions:
                self._sessions[sid]["last_seen"] = now
                return sid, self._sessions[sid]["csrf"], False
            new_sid = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self._sessions[new_sid] = {"csrf": csrf, "created": now, "last_seen": now}
            return new_sid, csrf, True

    def authenticate(self, headers, cookies, query, *, mutation=False):
        if not _host_allowed(headers.get("Host", "")):
            raise PermissionError("dashboard host is not local or private LAN")
        set_cookies = []
        redirect = False
        if self.external_access:
            supplied = cookies.get(ACCESS_COOKIE)
            query_token = query.get("access_token", [""])[0]
            if not supplied or not secrets.compare_digest(supplied, self.access_token):
                if query_token and secrets.compare_digest(query_token, self.access_token):
                    set_cookies.append(_cookie_header(ACCESS_COOKIE, self.access_token,
                                                       max_age=SESSION_TTL_SECONDS))
                    redirect = True
                else:
                    raise PermissionError("dashboard access token required")
        sid, csrf, is_new = self._session(cookies.get(SESSION_COOKIE))
        if is_new:
            set_cookies.append(_cookie_header(SESSION_COOKIE, sid, max_age=SESSION_TTL_SECONDS))
        if mutation:
            supplied_csrf = headers.get(CSRF_HEADER, "")
            if not supplied_csrf or not secrets.compare_digest(supplied_csrf, csrf):
                raise PermissionError("invalid dashboard CSRF nonce")
        if redirect:
            return {"csrf": csrf, "set_cookies": set_cookies, "redirect": True}
        return {"csrf": csrf, "set_cookies": set_cookies, "redirect": False}

    def _audit(self, storage, *, kind, target_id, action, reviewer, reason, result):
        self._ensure_action_schema(storage)
        action_id = secrets.token_hex(16)
        safe_result = {key: value for key, value in (result or {}).items()
                       if key not in {"source", "detail", "draft", "content", "body"}}
        storage.connection.execute(
            "INSERT INTO dashboard_action_log(id,target_kind,target_id,action,reviewer,reason,created_at,result_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (action_id, kind, target_id or "", action, reviewer, reason, utc_now(),
             json.dumps(safe_result, ensure_ascii=False, sort_keys=True)[:20000]))
        storage.connection.commit()
        return action_id

    def _base_item(self, *, kind, item_id, status, label, city=None, summary="", updated_at=None,
                   read_only=False, payload=None):
        item_payload = dict(payload or {})
        # The browser uses this explicit capability bit when building the
        # optional, user-triggered Qwen review queue.  Keeping it in every
        # row makes unsupported types explainable instead of silently
        # disappearing from the selection UI.
        item_payload.setdefault("llm_eligible", False)
        item_payload.setdefault("llm_disabled_reason", "此类记录不支持知识候选模型整理")
        return {"id": str(item_id), "kind": kind, "status": status or "", "status_label": _status_label(status),
                "bucket": _bucket(status), "needs_review": _bucket(status) == "REVIEW_REQUIRED",
                "city": city or "", "label": _safe_label(label), "summary": _safe_label(summary, 360),
                "updated_at": _iso_or_empty(updated_at), "read_only": bool(read_only),
                "allowed_actions": list(item_payload.get("allowed_actions", [])),
                "payload": item_payload}

    def _review_items(self, storage) -> List[dict]:
        service = ReviewService(storage, self.config.visibility_threshold)
        rows = []
        # ReviewService owns the status semantics for POIs, events, official
        # snapshots and source candidates.  The dashboard only renames the
        # two long kinds for readability and adds presentation metadata.
        for service_row in service.list_all()["items"]:
            service_kind = service_row["type"]
            kind = SERVICE_TO_DASHBOARD_KIND.get(service_kind, service_kind)
            model = SERVICE_REVIEW_TYPES[service_kind]
            item = storage.get(model, service_row["id"])
            updated = getattr(item, "updated_at", None) or getattr(item, "last_checked_at", None)
            updated = updated or getattr(item, "collected_at", None) or getattr(item, "created_at", None)
            allowed_actions = []
            if service_row["status"] in {"NEEDS_REVIEW", "DISCOVERED"}:
                allowed_actions = ["approve", "reject"]
                if kind == "candidate":
                    allowed_actions = ["resolve", "promote", "reject"]
            rows.append(self._base_item(
                kind=kind, item_id=service_row["id"], status=service_row["status"],
                label=service_row["label"], city=service_row.get("city"),
                summary=", ".join(reason["code"] for reason in service_row.get("reasons", [])),
                updated_at=updated, read_only=False,
                payload={"confidence": service_row.get("confidence"),
                         "reasons": [reason["code"] for reason in service_row.get("reasons", [])],
                         "allowed_actions": allowed_actions}))
        return rows

    def _knowledge_items(self, storage) -> List[dict]:
        rows = []
        candidates = storage.connection.execute("""
          SELECT k.id,k.source_content_id,k.city,k.scenario,k.query,k.review_status,k.reason,k.updated_at,
                 s.data AS content_data
          FROM xhs_knowledge_candidates k LEFT JOIN source_contents s ON s.id=k.source_content_id
          ORDER BY k.updated_at DESC,k.id DESC
        """).fetchall()
        for row in candidates:
            content = {}
            try:
                content = json.loads(row["content_data"] or "{}")
            except (TypeError, ValueError):
                pass
            llm_eligible = row["review_status"] == "NEEDS_REVIEW"
            rows.append(self._base_item(
                kind="knowledge_candidate", item_id=row["id"], status=row["review_status"],
                label=content.get("title") or row["scenario"], city=row["city"],
                summary=content.get("text", "")[:360], updated_at=row["updated_at"],
                read_only=True, payload={"scenario": row["scenario"], "query": row["query"],
                                         "reason": row["reason"], "allowed_actions": [],
                                         "llm_eligible": llm_eligible,
                                         "llm_disabled_reason": ("只有状态为 NEEDS_REVIEW 的知识候选可以交给模型"
                                                                  if not llm_eligible else "")}))
        for item in knowledge_drafts(storage, limit=min(MAX_ITEMS, 500)):
            draft = item.get("draft", {})
            candidate = draft.get("candidate", {}) if isinstance(draft, dict) else {}
            qau = draft.get("qau_draft", {}) if isinstance(draft, dict) else {}
            rows.append(self._base_item(
                kind="knowledge_draft", item_id=item["id"], status=item["status"],
                label=qau.get("question") or candidate.get("scenario") or item["id"],
                city=candidate.get("city"), summary=(qau.get("direct_answer") or {}).get("text", "")
                if isinstance(qau.get("direct_answer"), dict) else "",
                updated_at=item["updated_at"], read_only=False,
                payload={"candidate_id": item["candidate_id"], "model": item["model"],
                         "verification_required": item["verification_required"],
                         "allowed_actions": (["approve", "reject"]
                                             if item["status"] == "DRAFT_NEEDS_REVIEW" else [])}))
        return rows

    def _other_items(self, storage) -> List[dict]:
        rows = []
        for item in current_classifications(storage):
            content = storage.get(SourceContent, item.source_content_id)
            status = ("MODEL_OPTIONAL" if item.model_recommended else
                      "CLASSIFIER_REVIEW" if item.needs_review else "CLASSIFIED")
            rows.append(self._base_item(
                kind="content_classification", item_id=item.id, status=status,
                label=(content.title if content else "") or item.primary_label,
                city=(content.collection_scope if content else ""),
                summary="{} · {:.0%} · {}".format(
                    item.primary_label, item.confidence, ", ".join(item.reasons)),
                updated_at=item.created_at, read_only=True,
                payload={"primary_label": item.primary_label, "route": item.route,
                         "confidence": item.confidence,
                         "model_recommended": item.model_recommended,
                         "scenario_labels": item.scenario_labels,
                         "reasons": item.reasons}))
        for kind, model, label_fn, summary_fn, city_fn, status_fn, update_fn in (
            ("official_guidance_page_state", OfficialGuidancePageState, lambda x: x.page_key,
             lambda x: x.failure_reason or x.parse_status, lambda x: "", lambda x: x.availability,
             lambda x: x.last_checked_at),
            ("source_discovery_run", SourceDiscoveryRunRecord, lambda x: x.provider,
             lambda x: x.failure_reason or x.next_run_status, lambda x: "", lambda x: x.status,
             lambda x: x.ended_at or x.started_at),
            ("source_run", SourceRunRecord, lambda x: x.source,
             lambda x: x.failure_reason or x.collection_key, lambda x: "", lambda x: x.status,
             lambda x: x.ended_at or x.started_at),
        ):
            for item in storage.all(model):
                status = status_fn(item)
                rows.append(self._base_item(kind=kind, item_id=item.id, status=status,
                                            label=label_fn(item), city=city_fn(item),
                                            summary=summary_fn(item), updated_at=update_fn(item),
                                            read_only=True, payload={"next_run_status": getattr(item, "next_run_status", None),
                                                                     "failure_reason": getattr(item, "failure_reason", None)}))
        # These are operational/input records.  Their full payload is never
        # included in the list; detail views whitelist visible fields below.
        for item in storage.all(SourceContent):
            rows.append(self._base_item(kind="source_content", item_id=item.id, status=item.content_status,
                                        label=item.title or item.external_id, city="", summary=item.text,
                                        updated_at=item.collected_at, read_only=True,
                                        payload={"platform": item.platform, "external_id": item.external_id}))
        for item in storage.all(RawSourceRecord):
            rows.append(self._base_item(kind="raw_source_record", item_id=item.id, status="INFORMATIONAL",
                                        label=item.external_id, city=item.city, summary=item.source,
                                        updated_at=item.collected_at, read_only=True,
                                        payload={"source": item.source, "external_id": item.external_id,
                                                 "payload_keys": sorted(item.payload)}))
        for row in storage.connection.execute("""
          SELECT p.raw_record_id,p.external_id,p.route,p.reason,p.status,p.calls,p.cache_hits,
                 p.error,p.processed_at,r.city
          FROM xhs_processing_state p LEFT JOIN raw_source_records r ON r.id=p.raw_record_id
          ORDER BY p.processed_at DESC
        """).fetchall():
            rows.append(self._base_item(kind="processing_state", item_id=row["raw_record_id"],
                                        status=row["status"], label=row["external_id"], city=row["city"],
                                        summary=row["reason"], updated_at=row["processed_at"], read_only=True,
                                        payload={"route": row["route"], "calls": row["calls"],
                                                 "cache_hits": row["cache_hits"], "error": row["error"]}))
        return rows

    def list_items(self, params: Optional[dict] = None) -> dict:
        params = params or {}
        kind_filter = (params.get("kind", [""])[0] if isinstance(params.get("kind"), list) else params.get("kind", ""))
        status_filter = (params.get("status", [""])[0] if isinstance(params.get("status"), list) else params.get("status", ""))
        bucket_filter = (params.get("bucket", [""])[0] if isinstance(params.get("bucket"), list) else params.get("bucket", ""))
        city_filter = (params.get("city", [""])[0] if isinstance(params.get("city"), list) else params.get("city", ""))
        query = (params.get("q", [""])[0] if isinstance(params.get("q"), list) else params.get("q", ""))
        try:
            limit = min(MAX_ITEMS, max(1, int((params.get("limit", [500])[0] if isinstance(params.get("limit"), list) else params.get("limit", 500)))))
        except (TypeError, ValueError):
            limit = 500
        with self._storage() as storage:
            items = self._review_items(storage) + self._knowledge_items(storage) + self._other_items(storage)
        if kind_filter:
            items = [item for item in items if item["kind"] == kind_filter]
        if status_filter:
            items = [item for item in items if item["status"] == status_filter]
        if bucket_filter:
            items = [item for item in items if item["bucket"] == bucket_filter]
        if city_filter:
            items = [item for item in items if item["city"] == city_filter]
        if query:
            needle = query.casefold()
            items = [item for item in items if needle in json.dumps(
                {key: item.get(key) for key in ("id", "kind", "label", "summary", "city", "status")},
                ensure_ascii=False).casefold()]
        items.sort(key=lambda item: (item.get("updated_at") or "", item["kind"], item["id"]), reverse=True)
        visible = items[:limit]
        by_bucket = collections.Counter(item["bucket"] for item in items)
        return {"items": visible, "total": len(items), "shown": len(visible),
                "counts": dict(by_bucket), "truncated": len(items) > limit}

    def _visible_source(self, storage, content_id):
        content = storage.get(SourceContent, content_id)
        if content is None:
            return None
        return {"id": content.id, "platform": content.platform, "external_id": safe_text(content.external_id, 160),
                "source_url": safe_text(content.source_url, 800), "content_type": content.content_type,
                "title": safe_text(content.title), "text": safe_text(content.text, 10_000),
                "hashtags": [safe_text(tag, 160) for tag in content.hashtags],
                "published_at": content.published_at, "collected_at": content.collected_at,
                "content_status": content.content_status}

    def get_item(self, kind: str, item_id: str) -> dict:
        with self._storage() as storage:
            if kind in REVIEW_MODELS:
                service = ReviewService(storage, self.config.visibility_threshold)
                result = service.show(item_id)
                service_kind = DASHBOARD_TO_SERVICE_KIND.get(kind, kind)
                status = result.get("status", "")
                if status in {"NEEDS_REVIEW", "DISCOVERED"}:
                    actions = ["approve", "reject"]
                    if kind == "candidate":
                        actions = ["resolve", "promote", "reject"]
                else:
                    actions = []
                result.update({"dashboard_kind": kind, "read_only": False,
                               "allowed_actions": actions, "service_kind": service_kind})
                return result
            if kind == "knowledge_candidate":
                row = storage.connection.execute("""
                  SELECT k.id,k.source_content_id,k.city,k.scenario,k.query,k.review_status,k.reason,
                         k.created_at,k.updated_at,k.data
                  FROM xhs_knowledge_candidates k WHERE k.id=?
                """, (item_id,)).fetchone()
                if row is None:
                    raise ValueError("dashboard item not found")
                return {"kind": kind, **dict(row), "data": json.loads(row["data"]),
                        "source": self._visible_source(storage, row["source_content_id"]),
                        "read_only": True, "allowed_actions": []}
            if kind == "knowledge_draft":
                values = knowledge_drafts(storage, limit=min(MAX_ITEMS, 500))
                item = next((value for value in values if value["id"] == item_id), None)
                if item is None:
                    raise ValueError("dashboard item not found")
                item["kind"] = kind
                item["history"] = knowledge_review_history(storage, item_id)
                candidate = item["draft"].get("candidate", {})
                item["source"] = self._visible_source(storage, item.get("source_content_id"))
                item["read_only"] = False
                item["allowed_actions"] = (["approve", "reject"]
                                           if item.get("status") == "DRAFT_NEEDS_REVIEW" else [])
                return safe_display(item)
            model_by_kind = {
                "content_classification": ContentClassification,
                "event_candidate": EventCandidate, "event": Event,
                "official_guidance_snapshot": OfficialGuidanceSnapshot,
                "official_guidance_page_state": OfficialGuidancePageState,
                "source_discovery_candidate": SourceDiscoveryCandidate,
                "source_discovery_run": SourceDiscoveryRunRecord, "source_run": SourceRunRecord,
                "source_content": SourceContent, "raw_source_record": RawSourceRecord,
            }
            if kind in model_by_kind:
                item = storage.get(model_by_kind[kind], item_id)
                if item is None:
                    raise ValueError("dashboard item not found")
                if kind == "source_content":
                    return {"kind": kind, "read_only": True,
                            "source": self._visible_source(storage, item.id),
                            "allowed_actions": []}
                if kind == "raw_source_record":
                    value = asdict(item)
                    value.pop("payload", None)
                    value["payload_keys"] = sorted(item.payload)
                    return {"kind": kind, "read_only": True, "allowed_actions": [], **safe_display(value)}
                return {"kind": kind, "read_only": True, "allowed_actions": [], **safe_display(asdict(item))}
            if kind == "processing_state":
                row = storage.connection.execute(
                    "SELECT raw_record_id,external_id,router_version,route,reason,status,calls,cache_hits,usage_json,error,processed_at,model_latency_ms FROM xhs_processing_state WHERE raw_record_id=?",
                    (item_id,)).fetchone()
                if row is None:
                    raise ValueError("dashboard item not found")
                return {"kind": kind, "read_only": True, "allowed_actions": [], **safe_display(dict(row))}
        raise ValueError("dashboard item kind is unsupported")

    def perform_action(self, payload: dict) -> dict:
        kind = str(payload.get("kind", "")).strip()
        item_id = str(payload.get("id", "")).strip()
        action = str(payload.get("action", "")).strip().lower()
        reviewer = str(payload.get("reviewer", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        if not item_id or kind not in REVIEW_MODELS and kind != "knowledge_draft":
            raise ValueError("dashboard action target is invalid")
        if not reviewer or len(reviewer) > 120:
            raise ValueError("reviewer is required")
        if not reason or len(reason) > 2000:
            raise ValueError("reason is required")
        if action not in {"approve", "reject", "resolve", "promote"}:
            raise ValueError("unsupported dashboard action")
        with self._storage() as storage:
            if kind == "knowledge_draft":
                if action not in {"approve", "reject"}:
                    raise ValueError("knowledge drafts support approve or reject only")
                result = review_knowledge_draft(
                    storage, item_id, action=action, reviewer=reviewer, reason=reason,
                    reference_url=payload.get("reference_url") or None,
                    reference_kind=payload.get("reference_kind") or None,
                    reference_title=payload.get("reference_title") or None,
                    verified_at=payload.get("verified_at") or None,
                    valid_until=payload.get("valid_until") or None)
            else:
                service = ReviewService(storage, self.config.visibility_threshold)
                if action == "approve":
                    result = asdict(service.approve(item_id, reason, reviewer=reviewer))
                elif action == "reject":
                    result = asdict(service.reject(item_id, reason, reviewer=reviewer))
                elif action == "resolve":
                    result = asdict(service.resolve(item_id, str(payload.get("poi_id", "")).strip(),
                                                     reason, reviewer=reviewer))
                else:
                    result = asdict(service.promote(item_id, reason, reviewer=reviewer))
            result["dashboard_reviewer"] = reviewer
            result["dashboard_action"] = action
            result["audit_id"] = self._audit(storage, kind=kind, target_id=item_id, action=action,
                                              reviewer=reviewer, reason=reason, result=result)
            return result

    def _release_path(self, release):
        value = str(release or "").strip()
        if not value or len(value) > 200:
            raise ValueError("release is required")
        candidate = Path(value)
        releases_root = self.releases_root.resolve()
        path = candidate if candidate.is_absolute() else releases_root / candidate
        path = path.resolve()
        try:
            path.relative_to(releases_root)
        except ValueError:
            raise ValueError("release must be inside releases root") from None
        if not path.is_dir():
            raise ValueError("release directory not found")
        return path

    def _release_id(self, release):
        """Validate a release id before it is used for rollback lookup."""
        value = str(release or "").strip()
        if not value or len(value) > 200 or not value[0].isalnum():
            raise ValueError("release id is required")
        if not all(char.isalnum() or char in "_-" for char in value):
            raise ValueError("release id contains unsupported characters")
        return value

    def _place_overlay(self, payload, *, allow_missing=False):
        """Read an optional inline or project-local publication overlay.

        The bridge itself is responsible for checking editorial fields and
        POI safety.  Official structured POIs can use the bridge's audited
        source-language fallback, so an overlay is optional.  The Dashboard
        only limits file access to LAND-DATA.
        """
        inline = payload.get("publication_overlay")
        if inline is not None:
            if not isinstance(inline, dict):
                raise ValueError("publication_overlay must be a JSON object")
            return inline
        requested = str(payload.get("overlay_path", "")).strip()
        if not requested:
            if allow_missing:
                return None
            raise ValueError("Place overlay path or publication_overlay is required")
        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise ValueError("overlay_path must be inside the LAND-DATA project") from None
        if not candidate.is_file():
            raise ValueError("overlay file not found")
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("overlay file is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("overlay file must contain a JSON object")
        return value

    @staticmethod
    def _place_poi_ids(payload):
        value = payload.get("poi_ids", payload.get("poi_id", []))
        if isinstance(value, str):
            return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        if value in (None, ""):
            return []
        raise ValueError("poi_ids must be a list or comma-separated string")

    def publish_status(self):
        releases = []
        if self.releases_root.is_dir():
            for path in sorted(self.releases_root.iterdir(), reverse=True):
                manifest_path = path / "manifest.json"
                if not path.is_dir() or not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                releases.append({"release_id": manifest.get("release_id", path.name),
                                 "kind": manifest.get("kind", "land-knowledge"),
                                 "status": manifest.get("status", ""),
                                 "city": manifest.get("city", ""),
                                 "blockers": manifest.get("blockers", []),
                                 "questions_added": manifest.get("counts", {}).get("questions_added", 0),
                                 "path": str(path)})
        return {"allow_install": self.allow_install, "releases": releases[:100],
                "ready": [item for item in releases if item["status"] == "VERIFIED" and not item["blockers"]]}

    def locations(self):
        """Return safe city/source choices for the future collection controls."""
        cities = []
        for city, descriptor in sorted(self.config.cities.items()):
            if isinstance(descriptor, dict):
                cities.append({"id": city, "name": descriptor.get("display_name", city),
                               "country": descriptor.get("country", ""),
                               "query_file": descriptor.get("query_file")})
            else:
                cities.append({"id": city, "name": city, "country": str(descriptor)})
        sources = []
        for name, source in sorted(self.config.sources.items()):
            capability = source.capability
            sources.append({"id": name, "access_mode": capability.access_mode,
                            "processing_kind": source.processing_kind,
                            "export_enabled": source.policy.export_enabled,
                            "requires_login": capability.access_mode == "AUTHENTICATED"})
        return {"cities": cities, "sources": sources,
                "llm_default": {"enabled": False, "requires_explicit_ids": True,
                                "requires_explicit_limit": True}}

    def jobs(self):
        """Expose collection/processing progress without exposing raw payloads."""
        with self._storage() as storage:
            source_runs = [safe_display(asdict(value)) for value in storage.source_runs()]
            discovery_runs = [safe_display(asdict(value)) for value in storage.all(SourceDiscoveryRunRecord)]
            processing = storage.connection.execute(
                "SELECT status,COUNT(*) AS count FROM xhs_processing_state GROUP BY status").fetchall()
            tables = {row["name"] for row in storage.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            workflow_jobs = []
            if "dashboard_jobs" in tables:
                workflow_jobs = [safe_display(dict(row)) for row in storage.connection.execute(
                    "SELECT id,kind,status,city,source,started_at,ended_at,error,updated_at "
                    "FROM dashboard_jobs ORDER BY updated_at DESC LIMIT 200").fetchall()]
        return {"source_runs": source_runs, "source_discovery_runs": discovery_runs,
                "processing_counts": {row["status"]: row["count"] for row in processing},
                "workflow_jobs": workflow_jobs}

    def storage_locations(self):
        return {"database": {"path": self.db_path, "exists": Path(self.db_path).is_file()},
                "releases": {"path": str(self.releases_root), "exists": self.releases_root.is_dir()},
                "land": {"path": str(self.land_root), "exists": self.land_root.is_dir()},
                "raw_default": {"path": str(self.root / ".local"),
                                "exists": (self.root / ".local").is_dir()}}

    def workflow_cities(self):
        return {"cities": self.workflows.list_cities()}

    def workflow_plan(self, city: str):
        return self.workflows.collection_plan(city)

    def workflow_homepage(self, city: str):
        return self.workflows.homepage_status(city)

    def workflow_jobs(self, city=None, status=None):
        return {"jobs": self.workflows.list_jobs(city=city or None, status=status or None)}

    def workflow_job(self, job_id: str):
        return self.workflows.get_job(job_id)

    def workflow_storage(self, city=None):
        return self.workflows.storage_summary(city or None)

    def _validate_llm_selection(self, city: str, candidate_ids) -> list:
        """Validate the browser's explicit LLM queue before creating a job.

        The knowledge extractor already re-checks each candidate while it
        runs.  This earlier gate gives the operator an immediate, readable
        error and prevents a POI/event/source id from being accepted as a
        knowledge candidate by a hand-written request.
        """
        if not isinstance(candidate_ids, (list, tuple)):
            raise ValueError("请先选择至少一条知识候选")
        selected = list(dict.fromkeys(candidate_ids))
        if any(not isinstance(item, str) or not item.strip() for item in selected):
            raise ValueError("模型审核选择中包含无效候选 ID")
        if not selected:
            raise ValueError("请先选择至少一条知识候选")
        if len(selected) > 100:
            raise ValueError("一次模型审核最多选择 100 条知识候选")
        with self._storage() as storage:
            rows = storage.connection.execute(
                "SELECT id FROM xhs_knowledge_candidates "
                "WHERE city=? AND review_status='NEEDS_REVIEW' AND id IN ({})".format(
                    ",".join("?" for _ in selected)),
                (city, *selected),
            ).fetchall()
        eligible = {row["id"] for row in rows}
        invalid = [item for item in selected if item not in eligible]
        if invalid:
            preview = ", ".join(invalid[:5])
            if len(invalid) > 5:
                preview += " …"
            raise ValueError(
                "只能选择当前城市中状态为 NEEDS_REVIEW 的知识候选；以下 ID 不支持模型审核：{}".format(preview))
        return selected

    def workflow_action(self, payload: dict) -> dict:
        operation = str(payload.get("operation", "")).strip().lower()
        reviewer = str(payload.get("reviewer", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        confirm = str(payload.get("confirm", "")).strip().upper()
        if not reviewer or len(reviewer) > 120:
            raise ValueError("workflow reviewer is required")
        if not reason or len(reason) > 2000:
            raise ValueError("workflow reason is required")
        if operation == "create_city":
            if confirm != "CREATE_CITY":
                raise ValueError("explicit CREATE_CITY confirmation is required")
            result = self.workflows.create_city(
                str(payload.get("city", "")).strip(),
                str(payload.get("city_name", "")).strip() or None,
                str(payload.get("country", "")).strip() or None)
            target_id = result["city"]
        elif operation == "request_homepage":
            if confirm != "REQUEST_HOMEPAGE":
                raise ValueError("explicit REQUEST_HOMEPAGE confirmation is required")
            city = str(payload.get("city", "")).strip()
            result = self.workflows.request_homepage_generation(
                city, reviewer=reviewer, reason=reason)
            target_id = ((result.get("task") or {}).get("task_id") or
                         "homepage:" + city)
        elif operation == "start_job":
            if confirm != "START_JOB":
                raise ValueError("explicit START_JOB confirmation is required")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("workflow params must be an object")
            candidate_ids = payload.get("candidate_ids")
            if isinstance(candidate_ids, str):
                candidate_ids = [value.strip() for value in candidate_ids.replace("\n", ",").split(",")
                                 if value.strip()]
            limit = payload.get("llm_limit")
            if isinstance(limit, str) and limit.strip():
                try:
                    limit = int(limit)
                except ValueError as exc:
                    raise ValueError("llm_limit must be an integer") from exc
            elif limit in ("", None):
                limit = None
            step = str(payload.get("step", "")).strip()
            city = str(payload.get("city", "")).strip()
            if step == "knowledge_extract" and payload.get("llm_opt_in") is True:
                candidate_ids = self._validate_llm_selection(city, candidate_ids)
            result = self.workflows.start_job(
                city, step, params=params,
                model=str(payload.get("model", "")).strip() or None,
                llm_limit=limit,
                llm_opt_in=payload.get("llm_opt_in") is True,
                candidate_ids=candidate_ids)
            if step == "knowledge_extract":
                result["llm_plan"] = {
                    "selected": len(candidate_ids or []),
                    "max_calls": min(len(candidate_ids or []), limit or 0),
                    "model": str(payload.get("model", "")).strip(),
                    "result_status": "DRAFT_NEEDS_REVIEW",
                    "publish_automatically": False,
                }
            target_id = result["id"]
        elif operation == "cancel_job":
            if confirm != "CANCEL_JOB":
                raise ValueError("explicit CANCEL_JOB confirmation is required")
            target_id = str(payload.get("job_id", "")).strip()
            result = self.workflows.cancel_job(target_id, reason)
        else:
            raise ValueError("unsupported workflow operation")
        with self._storage() as storage:
            result = safe_display(result)
            result["audit_id"] = self._audit(
                storage, kind="workflow", target_id=target_id, action=operation,
                reviewer=reviewer, reason=reason, result=result)
        return result

    def publish_action(self, payload: dict) -> dict:
        operation = str(payload.get("operation", "")).strip().lower()
        content_type = str(payload.get("content_type", payload.get("kind", "knowledge"))).strip().lower()
        reviewer = str(payload.get("reviewer", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        confirm = str(payload.get("confirm", "")).strip().upper()
        if content_type in {"places", "place"}:
            content_type = "place"
        elif content_type in {"knowledge", "qau", ""}:
            content_type = "knowledge"
        else:
            raise ValueError("content_type must be knowledge or place")
        if operation not in {"preflight", "prepare", "diff", "verify", "install", "rollback"}:
            raise ValueError("unsupported publishing operation")
        if operation == "preflight" and content_type != "place":
            raise ValueError("只读预检仅支持 Place")
        if operation != "preflight" and (not reviewer or not reason):
            if content_type == "place" and operation in {"prepare", "diff", "verify"}:
                # Preparing and inspecting a deterministic official release is
                # a mechanical operation, not a human editorial decision.
                # Human-reviewed verification still rejects these defaults in
                # the bridge and asks for a named reviewer and reason.
                reviewer = reviewer or "dashboard-policy"
                reason = reason or "Dashboard mechanical {}".format(operation)
            else:
                raise ValueError("publishing reviewer and reason are required")
        if confirm != operation.upper():
            raise ValueError("explicit operation confirmation is required")
        with self._storage() as storage:
            if content_type == "place":
                if operation in {"preflight", "prepare"}:
                    requested_city = str(payload.get("city", "")).strip()
                    city = requested_city
                    if not city:
                        raise ValueError("Place preflight/prepare requires city")
                    city, city_profile = resolve_city_id(requested_city, self.config.cities)
                    if operation == "preflight":
                        result = preflight_place_release(
                            storage, city=city, poi_ids=self._place_poi_ids(payload),
                            publication_overlay=self._place_overlay(payload, allow_missing=True),
                            releases_root=str(self.releases_root), city_profile=city_profile)
                        target_id = "preflight:" + city
                    else:
                        result = prepare_place_release(
                            storage, city=city, poi_ids=self._place_poi_ids(payload),
                            publication_overlay=self._place_overlay(payload, allow_missing=True),
                            releases_root=str(self.releases_root), city_profile=city_profile)
                        target_id = result.get("release_id", "")
                elif operation == "diff":
                    path = self._release_path(payload.get("release"))
                    result = diff_place_release(str(path), storage=storage)
                    target_id = path.name
                elif operation == "verify":
                    path = self._release_path(payload.get("release"))
                    result = verify_place_release(str(path), storage=storage,
                                                  reviewer=reviewer, reason=reason)
                    target_id = path.name
                elif operation == "install":
                    if not self.allow_install:
                        raise PermissionError("installation is disabled; restart dashboard with --allow-install")
                    path = self._release_path(payload.get("release"))
                    result = install_place_release(str(path), str(self.land_root))
                    target_id = path.name
                else:
                    release_id = self._release_id(payload.get("release"))
                    if not self.allow_install:
                        raise PermissionError("installation is disabled; restart dashboard with --allow-install")
                    result = rollback_place_release(release_id, str(self.land_root), reason=reason)
                    target_id = release_id
            else:
                if operation == "prepare":
                    city = str(payload.get("city", "")).strip()
                    if not city:
                        raise ValueError("prepare requires city")
                    base_index = self.land_root / "LAND" / "Resources" / "question-content-index-v1.json"
                    result = prepare_release(storage, city=city, base_index_path=base_index,
                                             releases_root=self.releases_root,
                                             city_countries=self.config.cities,
                                             source_policies=self.config.sources)
                    target_id = result.get("release_id", "")
                elif operation == "diff":
                    path = self._release_path(payload.get("release"))
                    result = release_diff(path)
                    target_id = path.name
                elif operation == "verify":
                    path = self._release_path(payload.get("release"))
                    result = verify_release(path, storage=storage, reviewer=reviewer, reason=reason)
                    target_id = path.name
                elif operation == "install":
                    if not self.allow_install:
                        raise PermissionError("installation is disabled; restart dashboard with --allow-install")
                    path = self._release_path(payload.get("release"))
                    result = install_release(path, self.land_root)
                    target_id = path.name
                else:
                    if not self.allow_install:
                        raise PermissionError("installation is disabled; restart dashboard with --allow-install")
                    release_id = self._release_id(payload.get("release"))
                    result = rollback_release(release_id, self.land_root, reason=reason)
                    target_id = release_id
            result = safe_display(result)
            result["content_type"] = content_type
            if operation == "verify":
                # The browser must not infer permission from a successful
                # verification alone.  It also needs the explicit startup
                # flag, which is intentionally returned as a capability bit.
                result["allow_install"] = self.allow_install
            if operation == "preflight":
                # Preflight is deliberately read-only: do not write an audit
                # record or create a release path as a side effect.
                result["read_only"] = True
                result["release_dir_created"] = False
                return result
            result["audit_id"] = self._audit(storage, kind="publish", target_id=target_id,
                                              action=operation, reviewer=reviewer, reason=reason, result=result)
            return result

    def html(self, csrf: str) -> bytes:
        return render_dashboard_html(csrf)
        # Keep the UI self-contained so the dashboard works on a disconnected LAN.
        token = html.escape(csrf, quote=True)
        page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LAND-DATA 采集与审核 Dashboard</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0;background:#f5f6f8;color:#1f2937}
header{background:#172033;color:white;padding:18px 24px} header h1{margin:0 0 5px;font-size:21px} header p{margin:0;color:#cbd5e1;font-size:13px}
main{display:grid;grid-template-columns:minmax(480px,1fr) minmax(360px,520px);gap:16px;padding:16px;max-width:1500px;margin:auto}main>section:nth-of-type(1){order:2}main>section:nth-of-type(2){order:3}main>section:nth-of-type(3){order:1}
.panel{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:14px;box-shadow:0 2px 8px #0f172a0d}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
input,select,textarea,button{font:inherit;padding:8px;border:1px solid #cbd5e1;border-radius:6px}input,select{background:white}textarea{width:100%;min-height:72px;box-sizing:border-box}button{background:#1d4ed8;color:white;border:0;cursor:pointer}button.secondary{background:#64748b}button.danger{background:#b91c1c}button:disabled{opacity:.45;cursor:not-allowed}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid #e5e7eb;padding:8px 5px;text-align:left;vertical-align:top}th{color:#475569;font-weight:600}.item{cursor:pointer}.item:hover{background:#eff6ff}.pill{display:inline-block;padding:2px 6px;border-radius:10px;background:#e2e8f0;font-size:11px}.review{background:#fef3c7}.accepted{background:#dcfce7}.rejected{background:#fee2e2}.failed{background:#fee2e2}.muted{color:#64748b;font-size:12px}.pre{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:10px;border-radius:6px;max-height:300px;overflow:auto}.actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.section{border-top:1px solid #e5e7eb;padding-top:11px;margin-top:11px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.grid label{font-size:12px;color:#475569}.grid input{width:100%;box-sizing:border-box}.notice{padding:8px;background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;font-size:12px}.count{font-size:12px;color:#475569;margin:5px 0 10px}@media(max-width:900px){main{grid-template-columns:1fr}}
</style></head><body>
<header><h1>LAND-DATA 采集与审核 Dashboard</h1><p>创建城市 → 采集与处理 → 查看进度和数据位置 → 人工审核 → 准备并验证 LAND 发布包</p></header>
<main><section class="panel"><h2>3. 数据与人工审核</h2><div class="toolbar"><input id="q" placeholder="搜索标题、城市、ID"/><select id="bucket"><option value="">全部状态</option><option value="REVIEW_REQUIRED">需要审核</option><option value="AUTO_ACCEPTED">自动通过/信息</option><option value="REVIEWED_ACCEPTED">人工通过</option><option value="REVIEWED_REJECTED">人工拒绝</option><option value="FAILED_OR_BLOCKED">失败/阻塞</option><option value="PUBLISH_READY">可发布</option></select><select id="kind"><option value="">全部类型</option><option value="candidate">POI候选</option><option value="mention">POI提及</option><option value="poi">POI</option><option value="knowledge_candidate">知识候选</option><option value="knowledge_draft">知识草稿</option><option value="event_candidate">活动候选</option><option value="event">活动</option><option value="official_guidance_snapshot">官方快照</option><option value="official_guidance_page_state">官方页面状态</option><option value="source_discovery_candidate">来源发现</option><option value="source_discovery_run">发现运行</option><option value="source_content">来源内容</option><option value="raw_source_record">Raw记录</option><option value="source_run">来源运行</option><option value="processing_state">处理状态</option></select><button id="refresh">刷新</button></div><div id="count" class="count"></div><div style="overflow:auto"><table><thead><tr><th>类型</th><th>内容</th><th>状态</th><th>城市</th><th>更新时间</th></tr></thead><tbody id="items"></tbody></table></div></section>
<section class="panel"><div id="detail"><p class="muted">选择左侧内容查看详情。</p></div><div class="section"><h2>4. 发布到 LAND</h2><div class="notice">准备、查看差异、验证、安装是四个独立操作。默认不允许安装；只有启动时显式加入 <code>--allow-install</code> 才会显示安装按钮。</div><div class="grid"><label>城市<input id="pub-city" placeholder="new-york"></label><label>发布目录<input id="pub-release" placeholder="knowledge-city-..."></label><label>审核人<input id="pub-reviewer" placeholder="姓名或账号"></label><label>原因<input id="pub-reason" placeholder="本次操作原因"></label></div><div class="actions"><button onclick="publish('prepare')">准备发布包</button><button class="secondary" onclick="publish('diff')">查看差异</button><button class="secondary" onclick="publish('verify')">验证发布包</button><button id="install" class="danger" onclick="publish('install')" disabled>安装到 LAND</button></div><pre id="pub-result" class="pre"></pre></div></section>
<section class="panel" style="grid-column:1/-1"><h2>1–2. 城市、采集与处理</h2><div class="notice">先创建城市并查看来源覆盖，再分别启动采集和处理作业。城市档案会生成完整 Query Bank；默认全部为零 LLM。</div><div class="grid"><label>城市 ID<input id="wf-city" placeholder="san-francisco"></label><label>搜索名称<input id="wf-city-name" placeholder="旧金山"></label><label>国家代码<input id="wf-country" placeholder="US"></label><label>操作者<input id="wf-reviewer" placeholder="姓名或账号"></label></div><label>操作原因<input id="wf-reason" placeholder="创建城市或启动本轮采集的原因"></label><div class="actions"><button onclick="workflow('create_city')">创建/补全城市</button><button class="secondary" onclick="loadPlan()">查看采集计划</button></div><pre id="wf-plan" class="pre"></pre>
<div class="section"><h3>启动作业</h3><div class="grid"><label>作业步骤<select id="wf-step"><option value="xhs_collect">小红书采集（需要登录）</option><option value="xhs_process">零模型规则处理</option><option value="multi_collect">已配置结构化来源</option><option value="land_raw_process">LAND Raw 处理</option><option value="publish_prepare">准备 QAU 发布包</option><option value="knowledge_extract">仅选中候选的千问抽取</option></select></label><label>结构化来源（逗号分隔）<input id="wf-sources" placeholder="osm,nyc-parks-events"></label><label>千问模型<input id="wf-model" value="qwen3.7-flash"></label><label>千问硬上限<input id="wf-limit" type="number" min="1" max="100" value="1"></label></div><label>候选 ID（仅千问；逗号或换行分隔）<textarea id="wf-candidates"></textarea></label><div class="actions"><button onclick="workflow('start_job')">启动所选作业</button><button class="secondary" onclick="loadWorkflow()">刷新进度和存储位置</button></div><pre id="wf-result" class="pre"></pre></div>
<div class="section"><h3>作业、进度与数据位置</h3><div id="wf-jobs"></div><pre id="runtime" class="pre"></pre></div></section></main>
<script>const csrf=""" + json.dumps(csrf) + """;let selected=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const p=new URLSearchParams();if(q.value)p.set('q',q.value);if(bucket.value)p.set('bucket',bucket.value);if(kind.value)p.set('kind',kind.value);const r=await fetch('/api/items?'+p);const d=await r.json();count.textContent=`共 ${d.total} 条，当前显示 ${d.shown} 条；需要审核 ${d.counts.REVIEW_REQUIRED||0} 条`;items.innerHTML=d.items.map(x=>`<tr class="item" onclick="showItem('${esc(x.kind)}','${esc(x.id)}')"><td>${esc(x.kind)}<br><span class="pill">${esc(x.bucket)}</span></td><td><b>${esc(x.label)}</b><br><span class="muted">${esc(x.summary).slice(0,120)}</span></td><td>${esc(x.status_label)}</td><td>${esc(x.city)}</td><td>${esc(x.updated_at)}</td></tr>`).join('')||'<tr><td colspan="5">没有匹配内容</td></tr>';}
async function showItem(k,id){selected={kind:k,id:id};const r=await fetch('/api/item?'+new URLSearchParams({kind:k,id:id}));const d=await r.json();if(!r.ok){detail.innerHTML='<p class="notice">'+esc(d.error)+'</p>';return}detail.innerHTML='<h2>'+esc(k)+' · '+esc(d.status_label||d.status||'详情')+'</h2><pre class="pre">'+esc(JSON.stringify(d,null,2))+'</pre>'+actionForm(d);}
function actionForm(d){if(d.read_only)return '<p class="muted">只读：当前类型没有安全的审核决策接口。</p>';let kind=selected.kind;if(kind==='knowledge_draft'&&d.status==='DRAFT_NEEDS_REVIEW')return `<div class="section"><h3>编辑审核</h3><div class="grid"><label>审核人<input id="reviewer"></label><label>原因<input id="reason"></label><label>官方/运营 URL<input id="reference_url"></label><label>类型<select id="reference_kind"><option value="">个人经验</option><option value="official">官方</option><option value="operator">运营方</option></select></label><label>来源标题<input id="reference_title"></label><label>验证时间<input id="verified_at" placeholder="2026-09-26T00:00:00Z"></label><label>有效期<input id="valid_until" placeholder="2026-10-26T00:00:00Z"></label></div><div class="actions"><button onclick="act('approve')">通过</button><button class="danger" onclick="act('reject')">拒绝</button></div></div>`;if(['NEEDS_REVIEW','DISCOVERED'].includes(d.status))return `<div class="section"><h3>人工审核</h3><label>审核人<input id="reviewer"></label><label>原因<textarea id="reason"></textarea></label>${kind==='candidate'?'<label>已有 POI ID（可选，用于绑定）<input id="poi_id"></label>':''}<div class="actions"><button onclick="act('approve')">通过</button>${kind==='candidate'?'<button class="secondary" onclick="act(&quot;resolve&quot;)">绑定已有 POI</button><button class="secondary" onclick="act(&quot;promote&quot;)">创建新 POI</button>':''}<button class="danger" onclick="act('reject')">拒绝</button></div></div>`;return '<p class="muted">当前状态没有可执行的审核动作。</p>';}
async function act(action){const p={kind:selected.kind,id:selected.id,action,reviewer:document.getElementById('reviewer').value,reason:document.getElementById('reason').value};for(const id of ['reference_url','reference_kind','reference_title','verified_at','valid_until','poi_id']){const e=document.getElementById(id);if(e)p[id]=e.value}const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(p)});const d=await r.json();detail.innerHTML='<pre class="pre">'+esc(JSON.stringify(d,null,2))+'</pre>';load();}
async function publish(operation){const reviewer=document.getElementById('pub-reviewer').value,reason=document.getElementById('pub-reason').value;const p={operation,reviewer,reason,confirm:operation.toUpperCase(),city:document.getElementById('pub-city').value,release:document.getElementById('pub-release').value};const r=await fetch('/api/publish',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(p)});const d=await r.json();document.getElementById('pub-result').textContent=JSON.stringify(d,null,2);if(d.release_id)document.getElementById('pub-release').value=d.release_id;if(operation==='verify'&&d.ok)document.getElementById('install').disabled=false;}
refresh.onclick=load;q.onchange=load;bucket.onchange=load;kind.onchange=load;load();
async function workflow(operation){const city=document.getElementById('wf-city').value,reviewer=document.getElementById('wf-reviewer').value,reason=document.getElementById('wf-reason').value;let p={operation,city,reviewer,reason,confirm:operation==='create_city'?'CREATE_CITY':'START_JOB'};if(operation==='create_city'){p.city_name=document.getElementById('wf-city-name').value;p.country=document.getElementById('wf-country').value}else{p.step=document.getElementById('wf-step').value;p.params={};const sources=document.getElementById('wf-sources').value.split(',').map(x=>x.trim()).filter(Boolean);if(p.step==='multi_collect'&&sources.length)p.params.only_sources=sources;if(p.step==='knowledge_extract'){p.candidate_ids=document.getElementById('wf-candidates').value;p.model=document.getElementById('wf-model').value;p.llm_limit=document.getElementById('wf-limit').value;p.llm_opt_in=true}}const r=await fetch('/api/workflow',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(p)});const d=await r.json();document.getElementById('wf-result').textContent=JSON.stringify(d,null,2);await loadPlan();await loadWorkflow();}
async function cancelJob(id){const p={operation:'cancel_job',job_id:id,reviewer:document.getElementById('wf-reviewer').value,reason:document.getElementById('wf-reason').value,confirm:'CANCEL_JOB'};const r=await fetch('/api/workflow',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(p)});document.getElementById('wf-result').textContent=JSON.stringify(await r.json(),null,2);loadWorkflow();}
async function loadPlan(){const city=document.getElementById('wf-city').value;if(!city)return;const r=await fetch('/api/workflow/plan?'+new URLSearchParams({city}));document.getElementById('wf-plan').textContent=JSON.stringify(await r.json(),null,2);}
async function loadWorkflow(){const city=document.getElementById('wf-city').value;const [a,b,c]=await Promise.all([fetch('/api/workflow/cities'),fetch('/api/workflow/jobs?'+new URLSearchParams(city?{city}:{})),fetch('/api/workflow/storage?'+new URLSearchParams(city?{city}:{}))]);const d={cities:await a.json(),jobs:await b.json(),storage:await c.json()};runtime.textContent=JSON.stringify({cities:d.cities,storage:d.storage},null,2);const jobs=d.jobs.jobs||[];document.getElementById('wf-jobs').innerHTML=jobs.map(j=>`<div class="notice"><b>${esc(j.step)} · ${esc(j.status)}</b> ${Math.round((j.progress||0)*100)}%<br><span class="muted">${esc(j.id)} · LLM ${esc(j.llm_calls||0)}/${esc(j.llm_limit||0)} · ${esc(j.error||'')}</span>${['QUEUED','RUNNING','CANCEL_REQUESTED'].includes(j.status)?`<button class="danger" onclick="cancelJob('${esc(j.id)}')">取消</button>`:''}</div>`).join('')||'<p class="muted">尚无作业。</p>';}
loadWorkflow();setInterval(loadWorkflow,3000);
</script></body></html>"""
        return page.encode("utf-8")


class _DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _write(self, status, body, *, content_type="application/json", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _context(self, *, mutation=False):
        parsed = urlsplit(self.path)
        return self.server.dashboard_app.authenticate(
            self.headers, _parse_cookies(self.headers.get("Cookie", "")),
            parse_qs(parsed.query), mutation=mutation)

    def _error(self, status, message, cookies=()):
        self._write(status, json.dumps({"ok": False, "error": message}, ensure_ascii=False),
                    headers=[("Set-Cookie", value) for value in cookies])

    def do_GET(self):  # noqa: N802
        try:
            context = self._context()
            if context["redirect"]:
                parsed = urlsplit(self.path)
                headers = [("Location", parsed.path or "/")]
                headers.extend(("Set-Cookie", value) for value in context["set_cookies"])
                self._write(HTTPStatus.SEE_OTHER, b"", content_type="text/plain", headers=headers)
                return
            parsed = urlsplit(self.path)
            if parsed.path in ("", "/"):
                headers = [("Set-Cookie", value) for value in context["set_cookies"]]
                self._write(HTTPStatus.OK, self.server.dashboard_app.html(context["csrf"]),
                            content_type="text/html", headers=headers)
                return
            if parsed.path == "/api/items":
                result = self.server.dashboard_app.list_items(parse_qs(parsed.query))
            elif parsed.path == "/api/item":
                query = parse_qs(parsed.query)
                result = self.server.dashboard_app.get_item(query.get("kind", [""])[0], query.get("id", [""])[0])
            elif parsed.path == "/api/publish/status":
                result = self.server.dashboard_app.publish_status()
            elif parsed.path == "/api/locations":
                result = self.server.dashboard_app.locations()
            elif parsed.path == "/api/jobs":
                result = self.server.dashboard_app.jobs()
            elif parsed.path == "/api/storage":
                result = self.server.dashboard_app.storage_locations()
            elif parsed.path == "/api/workflow/cities":
                result = self.server.dashboard_app.workflow_cities()
            elif parsed.path == "/api/workflow/plan":
                query = parse_qs(parsed.query)
                result = self.server.dashboard_app.workflow_plan(query.get("city", [""])[0])
            elif parsed.path == "/api/workflow/homepage":
                query = parse_qs(parsed.query)
                result = self.server.dashboard_app.workflow_homepage(query.get("city", [""])[0])
            elif parsed.path == "/api/workflow/jobs":
                query = parse_qs(parsed.query)
                result = self.server.dashboard_app.workflow_jobs(
                    query.get("city", [""])[0], query.get("status", [""])[0])
            elif parsed.path == "/api/workflow/job":
                query = parse_qs(parsed.query)
                result = self.server.dashboard_app.workflow_job(query.get("id", [""])[0])
            elif parsed.path == "/api/workflow/storage":
                query = parse_qs(parsed.query)
                result = self.server.dashboard_app.workflow_storage(query.get("city", [""])[0])
            elif parsed.path == "/api/session":
                result = {"csrf": context["csrf"], "allow_install": self.server.dashboard_app.allow_install}
            else:
                self._error(HTTPStatus.NOT_FOUND, "未找到页面", context["set_cookies"])
                return
            self._write(HTTPStatus.OK, json.dumps(result, ensure_ascii=False),
                        headers=[("Set-Cookie", value) for value in context["set_cookies"]])
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Dashboard 请求失败，请查看终端状态")

    def do_POST(self):  # noqa: N802
        try:
            context = self._context(mutation=True)
            parsed = urlsplit(self.path)
            payload = _json_body(self)
            if parsed.path == "/api/action":
                result = self.server.dashboard_app.perform_action(payload)
            elif parsed.path == "/api/publish":
                result = self.server.dashboard_app.publish_action(payload)
            elif parsed.path == "/api/workflow":
                result = self.server.dashboard_app.workflow_action(payload)
            else:
                self._error(HTTPStatus.NOT_FOUND, "未找到变更入口")
                return
            self._write(HTTPStatus.OK, json.dumps({"ok": True, **result}, ensure_ascii=False),
                        headers=[("Set-Cookie", value) for value in context["set_cookies"]])
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except (ValueError, KeyError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Dashboard 变更失败，请查看详情后重试")

    def log_message(self, *_args):
        # Access tokens, source URLs and post bodies must never enter request logs.
        return


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: DashboardApp):
        self.dashboard_app = app
        super().__init__(address, _DashboardHandler)


def start_dashboard(db_path: str, root: str, *, host="127.0.0.1", port=8765,
                    allow_install=False, land_root=None, releases_root=None) -> Tuple[DashboardHTTPServer, dict]:
    """Create a server and return it with the URL printed by the CLI."""
    app = DashboardApp(db_path, root, bind_host=host, allow_install=allow_install,
                       land_root=land_root, releases_root=releases_root)
    server = DashboardHTTPServer((host, port), app)
    server.dashboard_app = app
    actual_host, actual_port = server.server_address[:2]
    display_host = _lan_address() if host in {"0.0.0.0", "::", ""} else ("localhost" if host == "127.0.0.1" else actual_host)
    query = {"access_token": app.access_token} if app.external_access else {}
    url = "http://{}:{}{}".format(display_host, actual_port, "?" + urlencode(query) if query else "")
    return server, {"status": "RUNNING", "url": url, "host": host, "port": actual_port,
                    "allow_install": bool(allow_install),
                    "access_token_required": bool(app.external_access)}


def serve_dashboard(db_path: str, root: str, *, host="127.0.0.1", port=8765,
                    allow_install=False, land_root=None, releases_root=None, output=None):
    server, info = start_dashboard(db_path, root, host=host, port=port,
                                   allow_install=allow_install, land_root=land_root,
                                   releases_root=releases_root)
    if output is not None:
        print(json.dumps(info, ensure_ascii=False, sort_keys=True), file=output, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return info
