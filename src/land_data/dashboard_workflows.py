"""Backend orchestration for the local LAND-DATA dashboard.

This module is deliberately independent from the HTTP dashboard.  It gives a
local dashboard one small, durable control surface for city setup, collection,
processing, review preparation, and release preparation.  Jobs run in daemon
threads so the UI can return immediately, while the job row remains the source
of truth for progress after a page refresh.

The workflow boundary is intentionally conservative:

* collection and deterministic processing use zero LLM calls by default;
* Qwen is available only for an explicitly selected list of knowledge
  candidate IDs, with an explicit model and hard limit;
* source planning reports missing city configuration and OSM scopes instead of
  inferring that a source is available;
* job logs contain counters and error classes, never source body text, prompts,
  bearer tokens, or API keys.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .auth import AuthRequired, MockSessionStore
from .city_setup import CITY_ID, ensure_city
from .city_homepage import (ACTIVE_TASK_STATUSES, CODEX_MAX_CALLS, CODEX_MODEL,
                             CODEX_REASONING_EFFORT, inspect_city_homepage,
                             launch_codex_task, list_homepage_tasks,
                             request_homepage_task)
from .config import load_config
from .knowledge_bridge import prepare_release
from .knowledge_processing import KnowledgeProcessingService, qwen_knowledge_from_env
from .land_raw import process_land_raw
from .multi_source import MultiSourceRunner, load_specs
from .pipeline import Pipeline
from .sqlite_storage import SQLiteStorage
from .threads import ThreadsAdapter
from .util import utc_now
from .xhs_browser import ChromeRemoteBrowser, XHSCaptureAdapter, collect_xhs
from .xhs_processing import process_xhs


JOB_TERMINAL = frozenset({
    "SUCCEEDED", "SUCCEEDED_WITH_REVIEW", "FAILED", "CANCELLED", "INTERRUPTED",
    "AUTH_REQUIRED",
})
JOB_ACTIVE = frozenset({"QUEUED", "RUNNING", "CANCEL_REQUESTED"})

# These are public names because the HTTP dashboard can use them to render a
# stable set of controls.  Aliases are kept for the existing terminal command
# vocabulary.
ALLOWED_STEPS = frozenset({
    "xhs_collect", "collect_xhs", "xhs_process", "process_xhs",
    "threads_collect", "collect_threads",
    "multi_collect", "structured_collect", "multi_pipeline",
    "land_raw_process", "process_land_raw", "knowledge_extract",
    "publish_prepare", "prepare_release",
})

STEP_ALIASES = {
    "collect_xhs": "xhs_collect",
    "process_xhs": "xhs_process",
    "collect_threads": "threads_collect",
    "structured_collect": "multi_collect",
    "multi_pipeline": "multi_collect",
    "process_land_raw": "land_raw_process",
    "prepare_release": "publish_prepare",
}

STEP_PARAMS = {
    "xhs_collect": {
        "query_file", "queries", "groups", "posts", "scrolls", "pause_seconds",
        "refresh", "open_login", "chrome_port", "chrome_path",
    },
    "xhs_process": {"limit", "retry_errors", "llm_mode"},
    "threads_collect": {"query", "search_type", "limit", "pages", "timeout"},
    "multi_collect": {"source_config", "only_sources"},
    "land_raw_process": {"raw_dir", "limit", "retry_errors", "allow_historical"},
    "knowledge_extract": {"candidate_ids", "retry_errors"},
    "publish_prepare": {"base_index_path", "releases_root"},
}

MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
SECRET_PATTERN = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|access[_-]?token|password|secret|authorization)\s*[:=]\s*)[^\s,;]+"
)


def ensure_dashboard_job_schema(connection) -> None:
    """Create or migrate the durable dashboard job table.

    This is additive and intentionally lives here rather than expanding the
    collector's core schema.  A dashboard can therefore be added to an older
    LAND-DATA database without rewriting existing records.
    """

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS dashboard_jobs (
          id TEXT PRIMARY KEY,
          city TEXT NOT NULL,
          step TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          started_at TEXT,
          ended_at TEXT,
          progress REAL NOT NULL DEFAULT 0,
          progress_json TEXT NOT NULL DEFAULT '{}',
          counts_json TEXT NOT NULL DEFAULT '{}',
          error TEXT,
          model TEXT,
          llm_calls INTEGER NOT NULL DEFAULT 0,
          llm_limit INTEGER,
          output_summary_json TEXT NOT NULL DEFAULT '{}',
          log_path TEXT,
          params_json TEXT NOT NULL DEFAULT '{}',
          cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_dashboard_jobs_city
          ON dashboard_jobs(city, created_at);
        CREATE INDEX IF NOT EXISTS idx_dashboard_jobs_status
          ON dashboard_jobs(status, created_at);
        """
    )
    # A few early development databases may contain only the primary key.  Do
    # not fail startup because the local operator has an older database.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(dashboard_jobs)")}
    additions = {
        "progress": "REAL NOT NULL DEFAULT 0",
        "progress_json": "TEXT NOT NULL DEFAULT '{}'",
        "counts_json": "TEXT NOT NULL DEFAULT '{}'",
        "error": "TEXT",
        "model": "TEXT",
        "llm_calls": "INTEGER NOT NULL DEFAULT 0",
        "llm_limit": "INTEGER",
        "output_summary_json": "TEXT NOT NULL DEFAULT '{}'",
        "log_path": "TEXT",
        "params_json": "TEXT NOT NULL DEFAULT '{}'",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute("ALTER TABLE dashboard_jobs ADD COLUMN {} {}".format(name, definition))
    connection.commit()


def _json(value: Any, fallback: Any):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return result


def _redact(value: Any, *, limit: int = 500) -> str:
    """Make exception text safe for a local log line."""

    text = SECRET_PATTERN.sub(r"\1[REDACTED]", str(value or ""))
    # Newlines make JSONL logs difficult to inspect and can accidentally turn
    # source content into another log record.
    text = " ".join(text.split())
    return text[:limit]


def _safe_path(root: Path, value: Optional[str], *, default: Optional[Path] = None,
               must_be_inside: bool = True) -> Path:
    candidate = Path(value) if value else (default or root)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if must_be_inside:
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("dashboard path must stay inside LAND-DATA root") from exc
    return resolved


def _safe_summary(value: Any, *, depth: int = 0) -> Any:
    """Keep machine-readable counters while dropping source payloads."""

    if depth > 4:
        return "[omitted]"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if any(part in lowered for part in ("body", "text", "prompt", "token", "secret", "password",
                                                "content", "evidence", "source_url", "url")):
                # URLs can contain private query parameters and source content
                # is not useful for the job list; locations are reported via
                # storage_summary instead.
                continue
            result[name] = _safe_summary(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_summary(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else _redact(value, limit=180)
    return _redact(value, limit=180)


def _summary_result(value: Any) -> Dict[str, Any]:
    """Return an allowlisted job result summary, never a full pipeline result."""

    if not isinstance(value, dict):
        return {"result": _redact(value, limit=180)}
    allowed = {
        "status", "next_run_status", "new_count", "updated_count", "duplicate_count",
        "processed", "resumed", "skipped", "deterministic", "pending_llm", "knowledge_review",
        "needs_review", "errors", "model_calls", "cache_hits", "planned_model_ratio",
        "actual_api_call_ratio", "llm_calls", "cached", "deferred", "failed", "selected",
        "multi_run_id", "sources", "collection", "browser", "next_action", "draft_ids",
        "release_id", "release_path", "verification", "city", "query_count", "files",
        "duplicate", "structured_poi", "known_poi", "model", "review_required",
        "classifier_counts", "model_recommended",
    }
    return {key: _safe_summary(value[key]) for key in allowed if key in value}


class DashboardWorkflowService:
    """Durable, local workflow service used by a LAN dashboard.

    ``start_job`` is asynchronous and returns the row immediately.  Tests and
    integrations that need deterministic execution can use ``wait_job`` or
    call the private worker through a small fake runner; all state transitions
    still go through the same SQLite table.
    """

    def __init__(self, db_path: str, root: str, *, land_root: Optional[str] = None):
        self.db_path = str(Path(db_path).resolve())
        self.root = Path(root).resolve()
        self.land_root = Path(land_root or (self.root / ".." / "LAND")).resolve()
        self._threads: Dict[str, threading.Thread] = {}
        self._threads_lock = threading.RLock()
        self._ensure_schema()
        self.recover_interrupted_jobs()

    def _storage(self):
        storage = SQLiteStorage(self.db_path)
        ensure_dashboard_job_schema(storage.connection)
        return storage

    def _ensure_schema(self):
        with self._storage() as storage:
            ensure_dashboard_job_schema(storage.connection)

    def recover_interrupted_jobs(self) -> Dict[str, Any]:
        """Mark jobs left active by a previous process as interrupted."""

        now = utc_now()
        with self._storage() as storage:
            rows = storage.connection.execute(
                "SELECT id,status FROM dashboard_jobs WHERE status IN ('RUNNING','CANCEL_REQUESTED')"
            ).fetchall()
            for row in rows:
                storage.connection.execute(
                    """UPDATE dashboard_jobs SET status='INTERRUPTED',ended_at=?,error=?,
                       progress_json=?,cancel_requested=0 WHERE id=?""",
                    (now, "dashboard process restarted", json.dumps({"stage": "interrupted"}), row["id"]),
                )
            storage.connection.commit()
        return {"interrupted": len(rows), "job_ids": [row["id"] for row in rows]}

    # ------------------------------------------------------------------
    # City and planning surfaces
    # ------------------------------------------------------------------
    def create_city(self, city_id: str, city_name: Optional[str] = None,
                    country: Optional[str] = None) -> Dict[str, Any]:
        if not CITY_ID.fullmatch(city_id or ""):
            raise ValueError("city must use lowercase letters, numbers and hyphens")
        result = ensure_city(self.root, city_id, city_name, country)
        result["homepage"] = self.homepage_status(city_id)
        result["storage"] = self.storage_summary(city_id)
        return result

    def homepage_status(self, city: str) -> Dict[str, Any]:
        """Return published homepage status and durable Codex handoffs."""

        city = (city or "").strip()
        profile = None
        for item in self.list_cities():
            if item.get("city") == city:
                profile = item
                break
        homepage = inspect_city_homepage(
            city, city_name=(profile or {}).get("display_name"), land_root=self.land_root)
        tasks = list_homepage_tasks(self.root, city)
        homepage["tasks"] = [
            {key: value for key, value in task.items()
             if key in {"task_id", "status", "created_at", "updated_at", "requested_by",
                        "request_reason", "task_path", "status_note", "codex", "execution"}}
            for task in tasks
        ]
        active = next((task for task in tasks if task.get("status") in ACTIVE_TASK_STATUSES), None)
        homepage["active_task"] = active
        homepage["generation_status"] = active.get("status") if active else (
            tasks[0].get("status") if tasks else None)
        homepage["latest_task"] = tasks[0] if tasks else None
        if homepage["published"]:
            homepage["next_action"] = "继续采集和发布内容"
        elif homepage["status"] == "PREVIEW_NEEDS_ACCEPTANCE":
            homepage["next_action"] = "先在 LAND 中验收城市主页预览，确认 reviewState=ready"
        elif active:
            homepage["next_action"] = "等待 Codex 执行任务规范；完成后重新检查 LAND 主页"
        else:
            homepage["next_action"] = "确认后创建 Codex 城市主页生成任务"
        homepage["codex_policy"] = {
            "model": CODEX_MODEL,
            "reasoning_effort": CODEX_REASONING_EFFORT,
            "max_calls": CODEX_MAX_CALLS,
            "qwen_calls": 0,
            "sandbox": "workspace-write",
            "approval_policy": "approve-for-me",
        }
        return homepage

    def request_homepage_generation(self, city: str, *, reviewer: str,
                                    reason: str) -> Dict[str, Any]:
        """Create the confirmed Codex task without running Codex or Qwen."""

        city = (city or "").strip()
        profile = None
        for item in self.list_cities():
            if item.get("city") == city:
                profile = item
                break
        if profile is None:
            raise ValueError("unknown city: {}; create the city first".format(city))
        result = request_homepage_task(
            self.root, city, city_profile=profile, land_root=self.land_root,
            reviewer=reviewer, reason=reason)
        task = result.get("task") or {}
        if result.get("status") == "TASK_READY" and task.get("task_id"):
            result["task"] = launch_codex_task(
                self.root, self.land_root, task["task_id"])
        result["homepage"] = self.homepage_status(city)
        return result

    def list_cities(self) -> List[Dict[str, Any]]:
        path = self.root / "config" / "cities.json"
        if not path.is_file():
            return []
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("cities configuration is unreadable: {}".format(type(exc).__name__)) from exc
        if not isinstance(values, dict):
            raise ValueError("cities configuration must be an object")
        result = []
        for city_id, value in sorted(values.items()):
            item = dict(value) if isinstance(value, dict) else {}
            query_path = self._query_path(city_id)
            item.update({"city": city_id, "query_file": str(query_path),
                         "query_configured": self._query_configured(city_id, query_path),
                         "homepage": inspect_city_homepage(
                             city_id, city_name=item.get("display_name"),
                             land_root=self.land_root)})
            result.append(item)
        return result

    def _query_path(self, city: str) -> Path:
        city_path = self.root / "config" / ("xiaohongshu-queries-{}.json".format(city))
        if city_path.is_file():
            return city_path.resolve()
        return (self.root / "config" / "xiaohongshu-queries.json").resolve()

    def _query_configured(self, city: str, path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(value, dict) and value.get("city") == city

    def _community_source_definitions(self) -> Dict[str, Dict[str, Any]]:
        """Read production community source definitions without exposing secrets.

        ``sources.json`` also contains fixtures and the internal LAND Raw
        importer.  Those are useful for tests and migration, but they are not
        platforms a beginner should be offered as live collectors.
        """

        path = self.root / "config" / "sources.json"
        if not path.is_file():
            return {}
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = values.get("sources") if isinstance(values, dict) else None
        if not isinstance(entries, dict):
            return {}
        result = {}
        for name, value in entries.items():
            if not isinstance(value, dict) or value.get("processing_kind") != "social":
                continue
            # Fixture adapters and the internal Raw bridge are not live
            # platforms.  Showing them as selectable sources would imply that
            # the dashboard can search them.
            if value.get("fixture") or name in {"xiaohongshu-land-raw"}:
                continue
            result[str(name)] = value
        return result

    def _community_plan(self, city: str, cities: Dict[str, Dict[str, Any]],
                        query_path: Path) -> List[Dict[str, Any]]:
        """Describe community platforms and their actual dashboard entrypoint."""

        city_exists = city in cities
        xhs_configured = city_exists and self._query_configured(city, query_path)
        xhs_storage = self.root / ".local" / "xiaohongshu" / "captures"
        plan: List[Dict[str, Any]] = [{
            "source": "xiaohongshu-browser", "platform": "xiaohongshu",
            "display_name": "小红书", "kind": "community_experience",
            "source_type": "community", "configured": xhs_configured,
            "status": "READY_WITH_LOGIN" if xhs_configured else "CITY_OR_QUERY_MISSING",
            "requires_login": True, "requires_oauth": False,
            "auth_status": "browser_login_required",
            "supports_search": True, "supports_auto_search": True,
            "dashboard_selectable": True, "workflow_step": "xhs_collect",
            "collection_mode": "authenticated_browser",
            "entrypoint": "Dashboard → 从小红书采集新内容",
            "llm_default": "never", "requires_llm": False,
            "query_file": str(query_path),
            "storage": {"database": self.db_path, "capture_dir": str(xhs_storage)},
            "authority_status": "community_source_not_official",
            "notes": "使用本机已登录浏览器；登录失效时会打开登录页面。",
        }]

        definitions = self._community_source_definitions()
        for name, value in sorted(definitions.items()):
            if name == "xiaohongshu-browser":
                continue
            capability = value.get("capability") if isinstance(value.get("capability"), dict) else {}
            supports_search = capability.get("supports_search") is True
            if name == "threads":
                token_present = bool(os.environ.get("LAND_THREADS_ACCESS_TOKEN", "").strip())
                plan.append({
                    "source": name, "platform": name,
                    "display_name": "Threads", "kind": "community_experience",
                    "source_type": "community", "configured": city_exists,
                    "source_configured": True, "auth_configured": token_present,
                    "status": (("READY" if token_present else "AUTH_REQUIRED")
                               if city_exists else "CITY_OR_QUERY_MISSING"),
                    "requires_login": True, "requires_oauth": True,
                    "auth_status": "oauth_token_configured" if token_present else "oauth_token_missing",
                    "supports_search": supports_search,
                    "supports_auto_search": True,
                    "dashboard_selectable": True,
                    "workflow_step": "threads_collect",
                    "collection_mode": "official_api",
                    "entrypoint": "Dashboard → 从 Threads 搜集公开帖子",
                    "llm_default": "never", "requires_llm": False,
                    "storage": {"database": self.db_path},
                    "authority_status": "community_source_not_official",
                    "notes": "需要 Meta 官方 OAuth token（LAND_THREADS_ACCESS_TOKEN）；关键词不等于城市过滤，结果仍需核对地点。",
                })
                continue

            # A future non-fixture social definition is still useful to show,
            # but is deliberately marked unavailable until its collector is
            # wired into an explicit Dashboard workflow step.
            plan.append({
                "source": name, "platform": name,
                "display_name": name, "kind": "community_experience",
                "source_type": "community", "configured": city_exists,
                "status": "CONFIGURED_BUT_UNAVAILABLE" if city_exists else "CITY_OR_QUERY_MISSING",
                "requires_login": capability.get("access_mode") == "AUTHENTICATED",
                "requires_oauth": capability.get("access_mode") == "AUTHENTICATED",
                "auth_status": "authentication_required" if capability.get("access_mode") == "AUTHENTICATED" else "public",
                "supports_search": supports_search, "supports_auto_search": False,
                "dashboard_selectable": False, "collection_mode": "adapter_not_connected",
                "entrypoint": None, "llm_default": "never", "requires_llm": False,
                "storage": {"database": self.db_path},
                "authority_status": "community_source_not_official",
                "notes": "配置文件里有来源定义，但尚未接入 Dashboard 自动采集。",
            })
        return plan

    def collection_plan(self, city: str) -> Dict[str, Any]:
        """Return an honest source plan without probing or inventing providers."""

        city = (city or "").strip()
        cities = {item["city"]: item for item in self.list_cities()}
        query_path = self._query_path(city)
        community_plan = self._community_plan(city, cities, query_path)
        plan: List[Dict[str, Any]] = list(community_plan)

        multi_path = self.root / "config" / "multi-source.json"
        entries = []
        if multi_path.is_file():
            try:
                values = json.loads(multi_path.read_text(encoding="utf-8"))
                entries = values.get("sources", []) if isinstance(values, dict) else []
            except (OSError, ValueError):
                entries = []
        if not isinstance(entries, list):
            entries = []
        for value in entries:
            if not isinstance(value, dict) or value.get("enabled", True) is not True:
                continue
            name = str(value.get("name", ""))
            configured_city = value.get("city") or value.get("collection_key")
            city_match = configured_city == city
            scope_value = value.get("scope")
            scope_path = (self.root / scope_value).resolve() if isinstance(scope_value, str) else None
            scope_ok = scope_path is None or scope_path.is_file()
            if not city_match:
                status = "UNCONFIGURED_FOR_CITY"
            elif not scope_ok:
                status = "SCOPE_MISSING"
            else:
                status = "READY"
            kind = "osm" if name == "osm" else "structured"
            plan.append({
                "source": name, "kind": kind, "configured": city_match and scope_ok,
                "status": status, "requires_login": False, "requires_llm": False,
                "llm_default": "never", "collection_key": value.get("collection_key"),
                "scope": str(scope_path) if scope_path else None,
                "scope_exists": scope_ok,
                # The config does not carry a legal authority assertion.  Keep
                # the result explicit so a UI cannot label it as official.
                "authority_status": "configured_source_authority_not_asserted",
            })
        return {
            "city": city, "city_exists": city in cities,
            "homepage": self.homepage_status(city),
            "community_platforms": community_plan,
            "sources": plan,
            "llm_policy": {"default": "never", "knowledge_extract": "explicit_candidates_only"},
            "storage": self.storage_summary(city),
        }

    def storage_summary(self, city: Optional[str] = None) -> Dict[str, Any]:
        """Return locations and counts without reading source bodies."""

        query_path = self._query_path(city) if city else None
        locations = {
            "database": self.db_path,
            "project_root": str(self.root),
            "city_config": str(self.root / "config" / "cities.json"),
            "source_config": str(self.root / "config" / "sources.json"),
            "multi_source_config": str(self.root / "config" / "multi-source.json"),
            "xiaohongshu_capture_dir": str(self.root / ".local" / "xiaohongshu" / "captures"),
            "land_raw_dir": str((self.root / ".." / "LAND" / "Raw").resolve()),
            "release_dir": str(self.root / ".local" / "releases"),
            "dashboard_job_log_dir": str(self.root / ".local" / "dashboard" / "jobs"),
        }
        if query_path:
            locations["xiaohongshu_query_config"] = str(query_path)
        existing = {name: Path(path).is_file() or Path(path).is_dir()
                    for name, path in locations.items()}
        counts = {}
        with self._storage() as storage:
            for table in ("dashboard_jobs", "raw_source_records", "source_contents",
                          "content_classifications", "pois",
                          "poi_candidates", "poi_mentions", "events", "source_runs"):
                try:
                    counts[table] = storage.connection.execute(
                        "SELECT COUNT(*) FROM {}".format(table)
                    ).fetchone()[0]
                except Exception:
                    counts[table] = None
        return {"locations": locations, "exists": existing, "counts": counts}

    # ------------------------------------------------------------------
    # Job CRUD and state transitions
    # ------------------------------------------------------------------
    def start_job(self, city: str, step: str, *, params: Optional[Dict[str, Any]] = None,
                  model: Optional[str] = None, llm_limit: Optional[int] = None,
                  llm_opt_in: bool = False, candidate_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        city = (city or "").strip()
        if not CITY_ID.fullmatch(city):
            raise ValueError("city must use lowercase letters, numbers and hyphens")
        if step not in ALLOWED_STEPS:
            raise ValueError("unsupported dashboard workflow step")
        canonical = STEP_ALIASES.get(step, step)
        values = dict(params or {})
        if not isinstance(values, dict):
            raise ValueError("job params must be an object")
        unknown = sorted(set(values) - STEP_PARAMS[canonical])
        if unknown:
            raise ValueError("unsupported parameters for {}: {}".format(canonical, ", ".join(unknown)))

        selected = candidate_ids if candidate_ids is not None else values.pop("candidate_ids", None)
        # candidate_ids may only be supplied to the LLM extraction operation.
        if selected is not None and canonical != "knowledge_extract":
            raise ValueError("candidate_ids are only valid for knowledge_extract")
        if canonical == "knowledge_extract":
            if not llm_opt_in:
                raise ValueError("knowledge_extract requires explicit llm_opt_in=true")
            if not isinstance(selected, (list, tuple)) or not selected:
                raise ValueError("knowledge_extract requires a non-empty candidate_ids list")
            selected = list(dict.fromkeys(selected))
            if any(not isinstance(item, str) or not item.strip() for item in selected):
                raise ValueError("candidate_ids must be non-empty strings")
            if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model):
                raise ValueError("knowledge_extract requires an explicit model name")
            if not isinstance(llm_limit, int) or isinstance(llm_limit, bool) or llm_limit < 1 or llm_limit > 100:
                raise ValueError("knowledge_extract requires a hard llm_limit between 1 and 100")
            if len(selected) > llm_limit:
                raise ValueError("candidate_ids exceed the hard llm_limit")
            values["candidate_ids"] = selected
        else:
            if llm_opt_in or model is not None or llm_limit is not None:
                raise ValueError("LLM options are only valid for explicit knowledge_extract jobs")
            if canonical == "xhs_process" and values.get("llm_mode", "never") != "never":
                raise ValueError("llm_mode=auto is only valid for explicit knowledge_extract jobs")
            if canonical != "xhs_process" and "llm_mode" in values:
                raise ValueError("llm_mode is only valid as 'never' for xhs_process")
            values.pop("candidate_ids", None)
        if canonical == "knowledge_extract":
            # Keep the caller's explicit model in the job record and pass the
            # same name into the worker.  Do not put any API key in params.
            values["model"] = model
            values["llm_limit"] = llm_limit
            values["llm_opt_in"] = True
        else:
            values["llm_opt_in"] = False

        now = utc_now()
        job_id = "job-" + secrets.token_hex(12)
        log_dir = self.root / ".local" / "dashboard" / "jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / (job_id + ".jsonl")
        with self._storage() as storage:
            storage.connection.execute(
                """INSERT INTO dashboard_jobs
                (id,city,step,status,created_at,progress,progress_json,counts_json,model,llm_calls,llm_limit,
                 output_summary_json,log_path,params_json,cancel_requested)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, city, canonical, "QUEUED", now, 0.0, "{}", "{}", model or None, 0,
                 llm_limit if canonical == "knowledge_extract" else None, "{}", str(log_path),
                 json.dumps(values, ensure_ascii=False, sort_keys=True), 0),
            )
            storage.connection.commit()
        self._write_log(job_id, "QUEUED", step=canonical, city=city)
        thread = threading.Thread(target=self._worker, args=(job_id,),
                                   name="land-dashboard-" + job_id, daemon=True)
        with self._threads_lock:
            self._threads[job_id] = thread
        thread.start()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self._storage() as storage:
            row = storage.connection.execute("SELECT * FROM dashboard_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("unknown dashboard job: {}".format(job_id))
        result = dict(row)
        result["progress_detail"] = _json(result.pop("progress_json"), {})
        result["counts"] = _json(result.pop("counts_json"), {})
        result["output_summary"] = _json(result.pop("output_summary_json"), {})
        result["params"] = _safe_summary(_json(result.pop("params_json"), {}))
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        return result

    def list_jobs(self, *, city: Optional[str] = None, status: Optional[str] = None,
                  limit: int = 100) -> List[Dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise ValueError("job limit must be between 1 and 1000")
        clauses, values = [], []
        if city:
            clauses.append("city=?")
            values.append(city)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._storage() as storage:
            rows = storage.connection.execute(
                "SELECT id FROM dashboard_jobs{} ORDER BY created_at DESC,id DESC LIMIT ?".format(where),
                tuple(values) + (limit,),
            ).fetchall()
        return [self.get_job(row["id"]) for row in rows]

    def cancel_job(self, job_id: str, reason: str = "cancelled by dashboard operator") -> Dict[str, Any]:
        now = utc_now()
        with self._storage() as storage:
            row = storage.connection.execute("SELECT status FROM dashboard_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("unknown dashboard job: {}".format(job_id))
            status = row["status"]
            if status in JOB_TERMINAL:
                return self.get_job(job_id)
            if status == "QUEUED":
                storage.connection.execute(
                    "UPDATE dashboard_jobs SET status='CANCELLED',ended_at=?,error=?,cancel_requested=1 WHERE id=?",
                    (now, _redact(reason), job_id),
                )
            else:
                storage.connection.execute(
                    "UPDATE dashboard_jobs SET status='CANCEL_REQUESTED',cancel_requested=1,error=? WHERE id=?",
                    (_redact(reason), job_id),
                )
            storage.connection.commit()
        self._write_log(job_id, "CANCEL_REQUESTED", reason=reason)
        return self.get_job(job_id)

    def wait_job(self, job_id: str, timeout: float = 30.0, poll_seconds: float = 0.05) -> Dict[str, Any]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            result = self.get_job(job_id)
            if result["status"] in JOB_TERMINAL:
                return result
            if time.monotonic() >= deadline:
                return result
            time.sleep(max(0.01, min(1.0, poll_seconds)))

    # ------------------------------------------------------------------
    # Worker implementation
    # ------------------------------------------------------------------
    def _write_log(self, job_id: str, event: str, **fields):
        try:
            with self._storage() as storage:
                row = storage.connection.execute("SELECT log_path FROM dashboard_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or not row["log_path"]:
                return
            payload = {"at": utc_now(), "job_id": job_id, "event": event}
            for key, value in fields.items():
                if key in {"body", "text", "prompt", "token", "api_key", "source_url", "content"}:
                    continue
                if key == "error":
                    # Error messages are useful for the operator, but a
                    # third-party exception can echo a response body.  Keep
                    # only its stable class/prefix in JSONL logs.
                    error_prefix = str(value).split(":", 1)[0].strip().split(None, 1)[0]
                    payload[key] = _redact(error_prefix, limit=120)
                else:
                    payload[key] = _safe_summary(value)
            with Path(row["log_path"]).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except (OSError, KeyError):
            # Logging must never turn a source job into a second failure.
            return

    def _set_progress(self, job_id: str, progress: float, detail: Optional[Dict[str, Any]] = None,
                      counts: Optional[Dict[str, Any]] = None):
        progress = max(0.0, min(1.0, float(progress)))
        with self._storage() as storage:
            updates = ["progress=?", "progress_json=?"]
            values: List[Any] = [progress, json.dumps(_safe_summary(detail or {}), ensure_ascii=False, sort_keys=True)]
            if counts is not None:
                updates.append("counts_json=?")
                values.append(json.dumps(_safe_summary(counts), ensure_ascii=False, sort_keys=True))
            values.append(job_id)
            storage.connection.execute("UPDATE dashboard_jobs SET {} WHERE id=?".format(",".join(updates)), values)
            storage.connection.commit()

    def _cancel_requested(self, job_id: str) -> bool:
        with self._storage() as storage:
            row = storage.connection.execute("SELECT cancel_requested FROM dashboard_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def _worker(self, job_id: str):
        started = utc_now()
        with self._storage() as storage:
            row = storage.connection.execute("SELECT status FROM dashboard_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] == "CANCELLED":
                return
            storage.connection.execute("UPDATE dashboard_jobs SET status='RUNNING',started_at=? WHERE id=?",
                                       (started, job_id))
            storage.connection.commit()
        self._write_log(job_id, "RUNNING")
        try:
            if self._cancel_requested(job_id):
                raise _Cancelled()
            result = self._execute_job(job_id)
            summary = _summary_result(result)
            final_status = str(result.get("status", "SUCCEEDED")) if isinstance(result, dict) else "SUCCEEDED"
            if final_status in {"PARTIAL_FAILURE", "FAILED", "ERROR"}:
                final_status = "FAILED"
            elif final_status in {"SUCCEEDED_WITH_REVIEW", "REVIEW_REQUIRED"}:
                final_status = "SUCCEEDED_WITH_REVIEW"
            elif final_status == "AUTH_REQUIRED":
                final_status = "AUTH_REQUIRED"
            else:
                final_status = "SUCCEEDED"
            if self._cancel_requested(job_id):
                final_status = "CANCELLED"
            counts = summary.get("collection") if isinstance(summary.get("collection"), dict) else summary
            llm_calls = int(summary.get("llm_calls", summary.get("model_calls", 0)) or 0)
            with self._storage() as storage:
                storage.connection.execute(
                    """UPDATE dashboard_jobs SET status=?,ended_at=?,progress=?,progress_json=?,counts_json=?,
                       output_summary_json=?,error=NULL,llm_calls=? WHERE id=?""",
                    (final_status, utc_now(), 1.0 if final_status != "CANCELLED" else 0.0,
                     json.dumps({"stage": "complete"}, ensure_ascii=False),
                     json.dumps(_safe_summary(counts or {}), ensure_ascii=False, sort_keys=True),
                     json.dumps(summary, ensure_ascii=False, sort_keys=True), llm_calls, job_id),
                )
                storage.connection.commit()
            self._write_log(job_id, final_status, counts=counts, llm_calls=llm_calls)
        except _Cancelled:
            with self._storage() as storage:
                storage.connection.execute(
                    "UPDATE dashboard_jobs SET status='CANCELLED',ended_at=?,progress_json=?,cancel_requested=1 WHERE id=?",
                    (utc_now(), json.dumps({"stage": "cancelled"}), job_id),
                )
                storage.connection.commit()
            self._write_log(job_id, "CANCELLED")
        except Exception as exc:
            error = _redact(type(exc).__name__ + ": " + str(exc))
            with self._storage() as storage:
                storage.connection.execute(
                    "UPDATE dashboard_jobs SET status='FAILED',ended_at=?,error=?,progress_json=? WHERE id=?",
                    (utc_now(), error, json.dumps({"stage": "failed"}), job_id),
                )
                storage.connection.commit()
            self._write_log(job_id, "FAILED", error=error)

    def _execute_job(self, job_id: str) -> Dict[str, Any]:
        job = self.get_job(job_id)
        city = job["city"]
        step = job["step"]
        params = dict(job.get("params") or {})
        self._set_progress(job_id, 0.05, {"stage": "loading", "step": step})
        if step == "xhs_collect":
            return self._run_xhs_collect(job_id, city, params)
        if step == "xhs_process":
            return self._run_xhs_process(job_id, city, params)
        if step == "threads_collect":
            return self._run_threads_collect(job_id, city, params)
        if step == "multi_collect":
            return self._run_multi_collect(job_id, city, params)
        if step == "land_raw_process":
            return self._run_land_raw(job_id, city, params)
        if step == "knowledge_extract":
            return self._run_knowledge_extract(job_id, city, params)
        if step == "publish_prepare":
            return self._run_publish_prepare(job_id, city, params)
        raise ValueError("unsupported dashboard workflow step")

    def _run_xhs_collect(self, job_id: str, city: str, params: Dict[str, Any]) -> Dict[str, Any]:
        config = load_config(str(self.root))
        if city not in config.cities:
            raise ValueError("unknown city: {}".format(city))
        query_path = _safe_path(self.root, params.get("query_file"), default=self._query_path(city))
        if not query_path.is_file():
            raise ValueError("Xiaohongshu query configuration is missing")
        value = json.loads(query_path.read_text(encoding="utf-8"))
        if value.get("city") != city:
            raise ValueError("Xiaohongshu query configuration belongs to a different city")
        groups = value.get("query_groups", {}) if isinstance(value, dict) else {}
        selected_groups = params.get("groups")
        query_groups = {}
        queries = params.get("queries")
        if queries is None:
            queries = []
            wanted = list(groups) if selected_groups is None else list(selected_groups)
            if not isinstance(groups, dict) or any(group not in groups for group in wanted):
                raise ValueError("Xiaohongshu query group is not configured")
            for group in wanted:
                values_for_group = groups[group]
                if not isinstance(values_for_group, list):
                    raise ValueError("Xiaohongshu query group must be a list")
                for query in values_for_group:
                    if query not in query_groups:
                        queries.append(query)
                        query_groups[query] = group
        if not isinstance(queries, list) or not queries:
            raise ValueError("at least one XHS query is required")
        limits = value.get("limits", {}) if isinstance(value, dict) else {}
        posts = params.get("posts", limits.get("posts_per_query", 20))
        scrolls = params.get("scrolls", limits.get("scrolls_per_query", 3))
        pause = params.get("pause_seconds", limits.get("pause_seconds", 1.5))
        browser_kwargs = {"port": int(params.get("chrome_port", 9222))}
        if params.get("chrome_path"):
            browser_kwargs["chrome_path"] = params["chrome_path"]
        browser = ChromeRemoteBrowser(self.root, **browser_kwargs)
        state = browser.status()
        if params.get("open_login", True) and (state.get("browser") != "RUNNING" or state.get("auth_required")):
            self._set_progress(job_id, 0.1, {"stage": "login_required", "browser": state.get("browser")})
            opened = browser.launch_login()
            self._write_log(job_id, "LOGIN_WINDOW_OPENED", port=opened.get("port"))

        with SQLiteStorage(self.db_path) as storage:
            pipeline = Pipeline(storage, config)

            def persist(item):
                pipeline.collect(city, [XHSCaptureAdapter(item)])

            self._set_progress(job_id, 0.2, {"stage": "collecting", "queries": len(queries)})
            result = collect_xhs(storage, self.root, city, queries,
                                  posts=int(posts), scrolls=int(scrolls), pause_seconds=float(pause),
                                  refresh=bool(params.get("refresh", False)), browser=browser,
                                  sink=persist, query_groups=query_groups)
        return result

    def _run_xhs_process(self, job_id: str, city: str, params: Dict[str, Any]) -> Dict[str, Any]:
        config = load_config(str(self.root))
        self._set_progress(job_id, 0.2, {"stage": "deterministic_processing"})
        with SQLiteStorage(self.db_path) as storage:
            result = process_xhs(storage, config, city, llm_mode="never",
                                 limit=params.get("limit"), retry_errors=bool(params.get("retry_errors", False)))
        return result

    def _run_threads_collect(self, job_id: str, city: str,
                             params: Dict[str, Any]) -> Dict[str, Any]:
        """Run one bounded official Threads keyword search with zero LLM calls."""

        config = load_config(str(self.root))
        if city not in config.cities:
            raise ValueError("unknown city: {}".format(city))
        query = params.get("query")
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > 120:
            raise ValueError("Threads query must be 1-120 characters")
        search_type = params.get("search_type", "RECENT")
        if search_type not in ("TOP", "RECENT"):
            raise ValueError("Threads search type must be TOP or RECENT")
        limit = params.get("limit", 5)
        pages = params.get("pages", 1)
        timeout = params.get("timeout", 15)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("Threads result limit must be 1-5")
        if type(pages) is not int or not 1 <= pages <= 2:
            raise ValueError("Threads pages must be 1-2")
        if type(timeout) is not int or not 2 <= timeout <= 30:
            raise ValueError("Threads timeout must be 2-30 seconds")
        token = os.environ.get("LAND_THREADS_ACCESS_TOKEN", "").strip()
        if not token:
            return {
                "status": "AUTH_REQUIRED", "llm_calls": 0,
                "next_action": "Configure official Threads OAuth as LAND_THREADS_ACCESS_TOKEN and retry.",
                "sources": ["threads"],
            }
        self._set_progress(job_id, 0.2, {"stage": "threads_keyword_search", "limit": limit})
        try:
            adapter = ThreadsAdapter(token, query=query.strip(), search_type=search_type,
                                     limit=limit, pages=pages, timeout=timeout)
            with SQLiteStorage(self.db_path) as storage:
                counts = Pipeline(storage, config).run(city, [adapter])
        except AuthRequired:
            return {
                "status": "AUTH_REQUIRED", "llm_calls": 0,
                "next_action": "Refresh the official Threads OAuth token and retry.",
                "sources": ["threads"],
            }
        return {"status": "SUCCEEDED", "collection": counts, "llm_calls": 0,
                "sources": ["threads"], "query_count": 1}

    def _run_multi_collect(self, job_id: str, city: str, params: Dict[str, Any]) -> Dict[str, Any]:
        config = load_config(str(self.root))
        source_path = _safe_path(self.root, params.get("source_config"),
                                 default=self.root / "config" / "multi-source.json")
        only = params.get("only_sources")
        if only is not None and (not isinstance(only, list) or any(not isinstance(item, str) for item in only)):
            raise ValueError("only_sources must be a list of source names")
        specs = load_specs(str(source_path), self.root, only)
        matching = [spec for spec in specs if spec.city == city or spec.collection_key == city]
        if only:
            requested = set(only)
            unmatched = requested - {spec.name for spec in matching}
            if unmatched:
                raise ValueError("sources are not configured for city {}: {}".format(city, ", ".join(sorted(unmatched))))
        if not matching:
            raise ValueError("no configured structured source for city {}".format(city))
        sessions = MockSessionStore(str(self.root / ".local" / "mock-sessions.json"))
        with SQLiteStorage(self.db_path) as storage:
            def make_adapter(spec):
                categories = list(spec.categories) if spec.categories else None
                if spec.borough:
                    categories = [spec.borough]
                return config.adapter(spec.name, sessions, spec.scope, categories, spec.limit, spec.timeout,
                                      storage=storage, collection_key=spec.collection_key, max_pages=spec.max_pages,
                                      initial_updated_at=spec.initial_updated_at,
                                      overlap_seconds=spec.overlap_seconds,
                                      safety_rescan_days=spec.safety_rescan_days,
                                      endpoints=spec.endpoints, max_retries=spec.max_retries)
            self._set_progress(job_id, 0.2, {"stage": "structured_collection", "sources": len(matching)})
            return MultiSourceRunner(Pipeline(storage, config), storage, matching, make_adapter).run()

    def _run_land_raw(self, job_id: str, city: str, params: Dict[str, Any]) -> Dict[str, Any]:
        config = load_config(str(self.root))
        raw_dir = params.get("raw_dir")
        if not raw_dir:
            raw_dir = str((self.root / ".." / "LAND" / "Raw").resolve())
        self._set_progress(job_id, 0.2, {"stage": "land_raw_processing", "city": city})
        with SQLiteStorage(self.db_path) as storage:
            # This dashboard operation is deliberately deterministic.  The
            # selected-candidate Qwen path is knowledge_extract below.
            return process_land_raw(storage, config, raw_dir, use_llm=False,
                                    limit=params.get("limit"), retry_errors=bool(params.get("retry_errors", False)),
                                    allow_historical=bool(params.get("allow_historical", False)))

    def _run_knowledge_extract(self, job_id: str, city: str, params: Dict[str, Any]) -> Dict[str, Any]:
        candidate_ids = params.get("candidate_ids")
        model = params.get("model")
        llm_limit = params.get("llm_limit")
        if not candidate_ids or not model or not isinstance(llm_limit, int):
            raise ValueError("knowledge_extract lost its explicit candidate/model/limit envelope")
        self._set_progress(job_id, 0.2, {"stage": "qwen_selected_candidates", "selected": len(candidate_ids)},
                           {"selected": len(candidate_ids), "llm_calls": 0})
        config = load_config(str(self.root))
        client = qwen_knowledge_from_env(config.root)
        if client.model != model:
            raise ValueError("configured Qwen model does not match the selected job model")
        with SQLiteStorage(self.db_path) as storage:
            result = KnowledgeProcessingService(storage, client).process(
                mode="auto", candidate_ids=candidate_ids, limit=llm_limit,
                retry_errors=bool(params.get("retry_errors", False)))
        return result

    def _run_publish_prepare(self, job_id: str, city: str, params: Dict[str, Any]) -> Dict[str, Any]:
        config = load_config(str(self.root))
        land_root = (self.root / ".." / "LAND").resolve()
        base_index = _safe_path(land_root, params.get("base_index_path"),
                                default=land_root / "LAND" / "Resources" / "question-content-index-v1.json",
                                must_be_inside=False)
        releases_root = _safe_path(self.root, params.get("releases_root"),
                                   default=self.root / ".local" / "releases")
        if not base_index.is_file():
            raise ValueError("LAND base question index is missing")
        self._set_progress(job_id, 0.3, {"stage": "preparing_release"})
        with SQLiteStorage(self.db_path) as storage:
            return prepare_release(storage, city=city, base_index_path=base_index,
                                   releases_root=releases_root, city_countries=config.cities,
                                   source_policies=config.sources)


class _Cancelled(Exception):
    pass


# A short alias makes the class convenient for a future dashboard adapter and
# avoids coupling that adapter to the implementation name.
DashboardWorkflow = DashboardWorkflowService


__all__ = [
    "ALLOWED_STEPS", "DashboardWorkflow", "DashboardWorkflowService",
    "ensure_dashboard_job_schema",
]
