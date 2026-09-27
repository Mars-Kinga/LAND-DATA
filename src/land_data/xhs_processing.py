"""Selective, auditable processing for Xiaohongshu browser captures."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import time

from .llm_extraction import LLMExtractionService, qwen_from_env
from .local_classifier import LocalClassifier, persist_classification
from .models import RawSourceRecord, SourceContent
from .pipeline import Pipeline
from .processing import normalize
from .util import stable_id, utc_now


ROUTER_VERSION = "xhs-browser-v4-local-classifier-v4"
SOURCE = "xiaohongshu-browser"
KNOWLEDGE_GROUPS = {
    "entry", "arrival", "packing", "money", "food", "transport", "stay",
    "culture", "safety", "connectivity", "sightseeing", "shopping", "health",
    "departure", "special-needs",
}
SCENARIO_PATTERNS = (
    ("culture", re.compile(r"文化|礼仪|禁忌|习俗|餐桌|社交|拍照规定|dress\s*code|etiquette", re.I)),
    ("entry", re.compile(r"签证|入境|海关|护照检查|visa|immigration|customs", re.I)),
    ("arrival", re.compile(r"落地|机场到|机场.{0,8}市区|arrival", re.I)),
    ("packing", re.compile(r"行前|打包|行李清单|插头|电压|穿衣|packing", re.I)),
    ("money", re.compile(r"支付|信用卡|现金|换汇|ATM|小费|apple\s*pay|payment", re.I)),
    ("transport", re.compile(r"地铁|公交|打车|租车|自驾|停车|交通|transit", re.I)),
    ("stay", re.compile(r"入住|退房|行李寄存|住宿|check.?in|checkout", re.I)),
    ("safety", re.compile(r"安全|防骗|诈骗|紧急|报警|护照丢失|领事|scam|emergency", re.I)),
    ("connectivity", re.compile(r"eSIM|SIM|Wi-?Fi|上网|漫游|通信|connectivity", re.I)),
    ("shopping", re.compile(r"购物|退税|免税|退换货|伴手礼|shopping", re.I)),
    ("health", re.compile(r"看病|医院|药房|买药|医疗|保险理赔|health|pharmacy", re.I)),
    ("departure", re.compile(r"返程|离境|值机|安检|转机|departure", re.I)),
    ("special-needs", re.compile(r"亲子|儿童|婴儿|长者|老人|无障碍|轮椅|宠物|accessib", re.I)),
    ("food", re.compile(r"点餐|过敏|素食|餐饮|food|dining", re.I)),
    ("sightseeing", re.compile(r"游览|景点|活动|展览|夜生活|sightseeing", re.I)),
)


def _ensure_schema(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS xhs_processing_state (
      raw_record_id TEXT PRIMARY KEY,
      external_id TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      router_version TEXT NOT NULL,
      route TEXT NOT NULL,
      reason TEXT NOT NULL,
      status TEXT NOT NULL,
      calls INTEGER NOT NULL,
      cache_hits INTEGER NOT NULL,
      usage_json TEXT,
      error TEXT,
      processed_at TEXT NOT NULL,
      model_latency_ms INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_xhs_processing_status
      ON xhs_processing_state(status, processed_at);
    CREATE TABLE IF NOT EXISTS xhs_processing_attempts (
      id TEXT PRIMARY KEY,
      raw_record_id TEXT NOT NULL,
      external_id TEXT NOT NULL,
      router_version TEXT NOT NULL,
      llm_mode TEXT NOT NULL,
      route TEXT NOT NULL,
      reason TEXT NOT NULL,
      previous_status TEXT,
      new_status TEXT NOT NULL,
      calls INTEGER NOT NULL,
      cache_hits INTEGER NOT NULL,
      latency_ms INTEGER NOT NULL,
      model_latency_ms INTEGER,
      model TEXT,
      usage_json TEXT,
      error TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS xhs_processing_attempts_no_update
      BEFORE UPDATE ON xhs_processing_attempts
      BEGIN SELECT RAISE(ABORT, 'xhs processing attempts are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS xhs_processing_attempts_no_delete
      BEFORE DELETE ON xhs_processing_attempts
      BEGIN SELECT RAISE(ABORT, 'xhs processing attempts are append-only'); END;
    CREATE TABLE IF NOT EXISTS xhs_knowledge_candidates (
      id TEXT PRIMARY KEY,
      source_content_id TEXT NOT NULL,
      city TEXT NOT NULL,
      scenario TEXT NOT NULL,
      query TEXT NOT NULL,
      review_status TEXT NOT NULL,
      reason TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      data TEXT NOT NULL,
      UNIQUE(source_content_id, scenario)
    );
    CREATE INDEX IF NOT EXISTS idx_xhs_knowledge_review
      ON xhs_knowledge_candidates(city, review_status, scenario);
    """)
    connection.commit()


def ensure_xhs_schema(connection):
    """Create the additive XHS processing tables for read-only downstream tools."""
    _ensure_schema(connection)


def route_xhs(content, payload, storage):
    """Compatibility projection from the shared local classifier."""
    decision = LocalClassifier(storage).classify(content, payload)
    persist_classification(storage, content, decision, payload)
    return _classifier_route(decision)


def _classifier_route(decision):
    reason = decision.reasons[0] if decision.reasons else "classifier_decision"
    if decision.primary_label == "irrelevant":
        return "skip", reason
    if decision.primary_label == "ambiguous":
        return "review", reason
    if decision.primary_label == "knowledge":
        scenario = decision.scenario_labels[0] if decision.scenario_labels else "travel"
        return "knowledge", "non_poi_{}_knowledge".format(scenario)
    if decision.primary_label in ("structured_poi", "known_poi"):
        return decision.primary_label, reason
    if decision.primary_label == "event":
        return "deterministic_event", reason
    if decision.primary_label == "mixed" and not decision.model_recommended:
        return "deterministic_mixed", reason
    return "model", reason


def knowledge_scenarios(content, payload):
    """Return all stable knowledge scenarios supported by this post."""
    return LocalClassifier().classify(content, payload).scenario_labels


def knowledge_scenario(content, payload):
    """Return the first scenario for routing while retaining all candidates."""
    scenarios = knowledge_scenarios(content, payload)
    return scenarios[0] if scenarios else None


def _put_knowledge_candidate(storage, content, record, scenario, reason):
    metadata = record.payload.get("source_metadata", {})
    query = metadata.get("query", "") if isinstance(metadata, dict) else ""
    ident = stable_id("xhs-knowledge", content.id, scenario)
    content_hash = _content_hash(record)
    now = utc_now()
    existing = storage.connection.execute(
        "SELECT review_status,data FROM xhs_knowledge_candidates WHERE id=?", (ident,)
    ).fetchone()
    review_status = "NEEDS_REVIEW"
    if existing is not None:
        try:
            previous_data = json.loads(existing["data"])
        except (TypeError, ValueError):
            previous_data = {}
        if previous_data.get("content_hash") == content_hash:
            review_status = existing["review_status"]
    data = json.dumps({"source": SOURCE, "content_hash": content_hash}, sort_keys=True)
    storage.connection.execute("""
      INSERT INTO xhs_knowledge_candidates
      (id,source_content_id,city,scenario,query,review_status,reason,
       created_at,updated_at,data)
      VALUES (?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET
       query=excluded.query,review_status=excluded.review_status,reason=excluded.reason,
       updated_at=excluded.updated_at,data=excluded.data
    """, (ident, content.id, record.city, scenario, query, review_status, reason,
          now, now, data))
    storage.connection.commit()


def _content_hash(record):
    return stable_id("xhs-content", record.external_id, record.payload)


def _put_content_with_update_status(storage, content):
    previous = storage.get(SourceContent, content.id)
    fields = ("title", "text", "geo_tag", "hashtags", "media_metadata",
              "language", "author_identifier", "published_at")
    if (previous and content.content_status == "ACTIVE" and
            (previous.content_status == "UNAVAILABLE" or
             any(getattr(previous, field) != getattr(content, field) for field in fields) or
             previous.raw_metadata != content.raw_metadata)):
        content.content_status = "UPDATED"
    elif previous and previous.content_status == "UPDATED" and content.content_status == "ACTIVE":
        content.content_status = "UPDATED"
    storage.put(content)


def process_xhs(storage, config, city, *, llm_mode="never", limit=None,
                retry_errors=False):
    """Process latest browser captures; model use is explicit and pending-only."""
    if llm_mode not in ("never", "auto"):
        raise ValueError("xhs llm mode must be never or auto")
    _ensure_schema(storage.connection)
    with _processing_lock(config.root):
        return _process_xhs_locked(storage, config, city, llm_mode=llm_mode,
                                   limit=limit, retry_errors=retry_errors)


@contextmanager
def _processing_lock(root):
    path = Path(root) / ".local" / "xiaohongshu" / "processing.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another xhs processing run is already active") from exc
        yield
    finally:
        stream.close()


def _process_xhs_locked(storage, config, city, *, llm_mode, limit, retry_errors):
    service = (LLMExtractionService(qwen_from_env(config.root), max_attempts=1)
               if llm_mode == "auto" else None)
    pipeline = Pipeline(storage, config)
    policy = config.sources[SOURCE].policy
    latest = {}
    for record in storage.all(RawSourceRecord):
        if record.source == SOURCE and record.city == city:
            prior = latest.get(record.external_id)
            if prior is None or record.collected_at > prior.collected_at:
                latest[record.external_id] = record
    totals = {"eligible": len(latest), "processed": 0, "resumed": 0,
              "skipped": 0, "deterministic": 0, "pending_llm": 0,
              "knowledge_review": 0, "llm_calls": 0, "cache_hits": 0, "needs_review": 0,
              "errors": 0, "usage": {}, "classifier_counts": {},
              "model_recommended": 0}
    for record in sorted(latest.values(), key=lambda item: (item.collected_at, item.external_id)):
        if limit is not None and totals["processed"] >= limit:
            break
        previous = storage.connection.execute(
            "SELECT * FROM xhs_processing_state WHERE raw_record_id=?", (record.id,)).fetchone()
        content_hash = _content_hash(record)
        if (previous and previous["router_version"] == ROUTER_VERSION and
                previous["content_hash"] == content_hash):
            may_resume = ((previous["status"] == "PENDING_LLM" and llm_mode == "auto") or
                          (previous["status"] == "ERROR" and retry_errors))
            if not may_resume:
                totals["resumed"] += 1
                continue
        started = time.monotonic()
        calls_before = service.calls if service else 0
        hits_before = service.cache_hits if service else 0
        route = "error"
        reason = "processing_error"
        status = "ERROR"
        error = None
        usage = None
        model_latency_ms = None
        content = normalize(record, policy)
        try:
            _put_content_with_update_status(storage, content)
            decision = LocalClassifier(storage).classify(content, record.payload)
            persist_classification(storage, content, decision, record.payload)
            totals["classifier_counts"][decision.primary_label] = (
                totals["classifier_counts"].get(decision.primary_label, 0) + 1)
            if decision.model_recommended:
                totals["model_recommended"] += 1
            scenarios = decision.scenario_labels
            for scenario in scenarios:
                _put_knowledge_candidate(
                    storage, content, record, scenario,
                    "{}_knowledge_requires_review".format(scenario))
                totals["knowledge_review"] += 1
            route, reason = _classifier_route(decision)
            if route == "skip":
                status = "SKIPPED"
                totals["skipped"] += 1
            elif route == "knowledge":
                status = "KNOWLEDGE_REVIEW"
            elif route == "review":
                status = "NEEDS_REVIEW"
                totals["needs_review"] += 1
            elif route == "model" and llm_mode == "never":
                status = "PENDING_LLM"
                totals["pending_llm"] += 1
            else:
                pipeline.llm_service = service if route == "model" else None
                pipeline._parse_one(record, policy)
                if route == "model":
                    usage = service.usage_by_content.get(record.external_id)
                    latency = service.latencies_by_content.get(record.external_id)
                    model_latency_ms = round(latency * 1000) if latency is not None else None
                    if record.external_id in service.errors:
                        status = "ERROR"
                        error = service.errors[record.external_id]
                        totals["errors"] += 1
                    else:
                        status = "NEEDS_REVIEW"
                        totals["needs_review"] += 1
                elif route in ("deterministic_event", "deterministic_mixed"):
                    status = "NEEDS_REVIEW"
                    totals["needs_review"] += 1
                    totals["deterministic"] += 1
                else:
                    status = "DONE"
                    totals["deterministic"] += 1
            pipeline.validate()
        except Exception as exc:
            status = "ERROR"
            error = type(exc).__name__ + ": " + str(exc)[:240]
            totals["errors"] += 1
        calls = (service.calls - calls_before) if service else 0
        hits = (service.cache_hits - hits_before) if service else 0
        totals["llm_calls"] += calls
        totals["cache_hits"] += hits
        if usage:
            for key, value in usage.items():
                if isinstance(value, int):
                    totals["usage"][key] = totals["usage"].get(key, 0) + value
        elapsed_ms = round((time.monotonic() - started) * 1000)
        created_at = utc_now()
        usage_json = json.dumps(usage, ensure_ascii=False, sort_keys=True) if usage else None
        values = (record.id, record.external_id, content_hash, ROUTER_VERSION,
                  route, reason, status, calls, hits, usage_json, error, created_at,
                  model_latency_ms)
        storage.connection.execute("""
          INSERT INTO xhs_processing_state
          (raw_record_id,external_id,content_hash,router_version,route,reason,status,
           calls,cache_hits,usage_json,error,processed_at,model_latency_ms)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(raw_record_id) DO UPDATE SET
           content_hash=excluded.content_hash,router_version=excluded.router_version,
           route=excluded.route,reason=excluded.reason,
           status=excluded.status,calls=excluded.calls,cache_hits=excluded.cache_hits,
           usage_json=excluded.usage_json,error=excluded.error,
           processed_at=excluded.processed_at,model_latency_ms=excluded.model_latency_ms
        """, values)
        attempt_id = stable_id("xhs-processing-attempt", record.id, created_at,
                               previous["status"] if previous else "", status)
        storage.connection.execute("""
          INSERT INTO xhs_processing_attempts
          (id,raw_record_id,external_id,router_version,llm_mode,route,reason,
           previous_status,new_status,calls,cache_hits,latency_ms,model_latency_ms,
           model,usage_json,error,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (attempt_id, record.id, record.external_id, ROUTER_VERSION, llm_mode,
               route, reason, previous["status"] if previous else None, status,
               calls, hits, elapsed_ms, model_latency_ms,
               getattr(service.client, "model", None) if service else None,
               usage_json, error, created_at))
        storage.connection.commit()
        totals["processed"] += 1
    return totals


def processing_status(storage, limit=50):
    _ensure_schema(storage.connection)
    counts = {row["status"]: row["count"] for row in storage.connection.execute(
        "SELECT status, COUNT(*) AS count FROM xhs_processing_state GROUP BY status")}
    rows = storage.connection.execute(
        "SELECT external_id,route,reason,status,calls,cache_hits,usage_json,error,processed_at "
        "FROM xhs_processing_state ORDER BY processed_at DESC LIMIT ?", (limit,)).fetchall()
    knowledge_counts = {row["scenario"]: row["count"] for row in storage.connection.execute(
        "SELECT scenario,COUNT(*) AS count FROM xhs_knowledge_candidates "
        "WHERE review_status='NEEDS_REVIEW' GROUP BY scenario")}
    return {"counts": counts, "knowledge_counts": knowledge_counts,
            "items": [dict(row) for row in rows]}


def knowledge_candidates(storage, *, city=None, scenario=None, limit=50):
    """Return a bounded, local review view for non-POI travel knowledge."""
    if limit < 1 or limit > 500:
        raise ValueError("xhs knowledge limit must be between 1 and 500")
    _ensure_schema(storage.connection)
    clauses = ["k.review_status='NEEDS_REVIEW'"]
    values = []
    if city:
        clauses.append("k.city=?")
        values.append(city)
    if scenario:
        clauses.append("k.scenario=?")
        values.append(scenario)
    values.append(limit)
    rows = storage.connection.execute("""
      SELECT k.id,k.source_content_id,k.city,k.scenario,k.query,k.review_status,
             k.reason,k.updated_at,s.data AS content_data
      FROM xhs_knowledge_candidates k
      JOIN source_contents s ON s.id=k.source_content_id
      WHERE {} ORDER BY k.updated_at DESC LIMIT ?
    """.format(" AND ".join(clauses)), values).fetchall()
    items = []
    for row in rows:
        content = json.loads(row["content_data"])
        items.append({
            "id": row["id"], "source_content_id": row["source_content_id"],
            "city": row["city"], "scenario": row["scenario"],
            "query": row["query"], "review_status": row["review_status"],
            "reason": row["reason"], "updated_at": row["updated_at"],
            "title": content.get("title", ""),
            "excerpt": content.get("text", "")[:240],
            "source_url": content.get("source_url"),
        })
    return items
