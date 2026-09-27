"""SQLite implementation; SQL stays behind the small Storage interface."""
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Sequence, Type, TypeVar

from . import models
from .util import utc_now

T = TypeVar("T")

TABLES = {
    models.RawSourceRecord: "raw_source_records",
    models.SourceContent: "source_contents",
    models.ContentClassification: "content_classifications",
    models.POI: "pois",
    models.POICandidate: "poi_candidates",
    models.POIMention: "poi_mentions",
    models.RecommendationSignal: "recommendation_signals",
    models.LocationEvidence: "location_evidence",
    models.EventCandidate: "event_candidates",
    models.Event: "events",
    models.FieldProvenance: "field_provenance",
    models.SourceRunRecord: "source_runs",
    models.SourceCollectionCheckpoint: "source_collection_checkpoints",
    models.SourceDiscoveryCandidate: "source_discovery_candidates",
    models.SourceDiscoveryRunRecord: "source_discovery_runs",
    models.SourceDiscoveryCheckpoint: "source_discovery_checkpoints",
    models.OfficialGuidanceSnapshot: "official_guidance_snapshots",
    models.OfficialGuidancePageState: "official_guidance_page_states",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_source_records (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL, city TEXT NOT NULL,
 expires_at TEXT, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_contents (
 id TEXT PRIMARY KEY, raw_record_id TEXT NOT NULL,
 platform TEXT NOT NULL, external_id TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(platform, external_id)
);
CREATE TABLE IF NOT EXISTS content_classifications (
 id TEXT PRIMARY KEY,
 source_content_id TEXT NOT NULL REFERENCES source_contents(id),
 classifier_version TEXT NOT NULL, content_fingerprint TEXT NOT NULL,
 primary_label TEXT NOT NULL, route TEXT NOT NULL,
 confidence REAL NOT NULL,
 needs_review INTEGER NOT NULL CHECK(needs_review IN (0,1)),
 model_recommended INTEGER NOT NULL CHECK(model_recommended IN (0,1)),
 created_at TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(source_content_id, classifier_version, content_fingerprint)
);
CREATE TABLE IF NOT EXISTS pois (
 id TEXT PRIMARY KEY, city TEXT NOT NULL, country TEXT NOT NULL,
 map_visibility INTEGER NOT NULL CHECK(map_visibility IN (0,1)),
 parent_poi_id TEXT REFERENCES pois(id), data TEXT NOT NULL,
 CHECK(parent_poi_id IS NULL OR parent_poi_id != id)
);
CREATE TABLE IF NOT EXISTS poi_candidates (
 id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL REFERENCES source_contents(id),
 resolved_poi_id TEXT REFERENCES pois(id), city TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS poi_mentions (
 id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL REFERENCES source_contents(id),
 poi_id TEXT REFERENCES pois(id), poi_candidate_id TEXT REFERENCES poi_candidates(id),
 review_status TEXT NOT NULL, data TEXT NOT NULL,
 CHECK(poi_id IS NOT NULL OR poi_candidate_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS recommendation_signals (
 id TEXT PRIMARY KEY, poi_mention_id TEXT NOT NULL REFERENCES poi_mentions(id),
 type TEXT NOT NULL, value TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS location_evidence (
 id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL REFERENCES source_contents(id),
 poi_candidate_id TEXT NOT NULL REFERENCES poi_candidates(id), data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_candidates (
 id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL REFERENCES source_contents(id), data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL REFERENCES source_contents(id),
 venue_poi_id TEXT REFERENCES pois(id), data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS field_provenance (
 id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 field_name TEXT NOT NULL, source_content_id TEXT REFERENCES source_contents(id), data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_decisions (
 id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
 previous_status TEXT NOT NULL, new_status TEXT NOT NULL, action TEXT NOT NULL,
 created_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_extraction_cache (
 key TEXT PRIMARY KEY, expires_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_runs (
 id TEXT PRIMARY KEY, multi_run_id TEXT NOT NULL, source TEXT NOT NULL,
 collection_key TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
 new_count INTEGER NOT NULL, updated_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL,
 status TEXT NOT NULL, failure_reason TEXT, next_run_status TEXT NOT NULL,
 http_requests INTEGER, retry_count INTEGER, endpoint TEXT, runtime_seconds REAL,
 data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_collection_checkpoints (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, collection_key TEXT NOT NULL,
 cursor_updated_at TEXT, cursor_row_id TEXT, cycle_complete INTEGER NOT NULL,
 updated_at TEXT NOT NULL, strategy TEXT NOT NULL DEFAULT '', data TEXT NOT NULL,
 UNIQUE(source, collection_key)
);
CREATE TABLE IF NOT EXISTS source_discovery_candidates (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset_id TEXT NOT NULL,
 domain TEXT NOT NULL, status TEXT NOT NULL, discovered_query TEXT NOT NULL,
 last_checked_at TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(provider, dataset_id)
);
CREATE TABLE IF NOT EXISTS source_discovery_runs (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, started_at TEXT NOT NULL,
 ended_at TEXT, status TEXT NOT NULL, next_run_status TEXT NOT NULL,
 new_count INTEGER NOT NULL, updated_count INTEGER NOT NULL,
 duplicate_count INTEGER NOT NULL, filtered_count INTEGER NOT NULL,
 http_requests INTEGER NOT NULL, retry_count INTEGER NOT NULL,
 query_count INTEGER NOT NULL, failure_reason TEXT, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_discovery_checkpoints (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, query TEXT NOT NULL,
 cursor INTEGER NOT NULL, cycle_complete INTEGER NOT NULL,
 updated_at TEXT NOT NULL, strategy TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(provider, query)
);
CREATE TABLE IF NOT EXISTS official_guidance_snapshots (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, page_key TEXT NOT NULL,
 source_url TEXT NOT NULL, content_version TEXT NOT NULL, collected_at TEXT NOT NULL,
 review_status TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(source, page_key, content_version)
);
CREATE TABLE IF NOT EXISTS official_guidance_page_states (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, page_key TEXT NOT NULL,
 source_url TEXT NOT NULL, current_snapshot_id TEXT,
 last_checked_at TEXT NOT NULL, availability TEXT NOT NULL, failure_reason TEXT,
 parse_status TEXT NOT NULL DEFAULT 'OK', parse_failure_reason TEXT,
 data TEXT NOT NULL, UNIQUE(source, page_key)
);
CREATE TRIGGER IF NOT EXISTS review_decisions_no_update
 BEFORE UPDATE ON review_decisions BEGIN SELECT RAISE(ABORT, 'review decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS review_decisions_no_delete
 BEFORE DELETE ON review_decisions BEGIN SELECT RAISE(ABORT, 'review decisions are append-only'); END;
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_source_records(source);
CREATE INDEX IF NOT EXISTS idx_candidates_city ON poi_candidates(city);
CREATE INDEX IF NOT EXISTS idx_pois_city ON pois(city, map_visibility);
CREATE INDEX IF NOT EXISTS idx_mentions_poi ON poi_mentions(poi_id, review_status);
CREATE INDEX IF NOT EXISTS idx_mentions_candidate ON poi_mentions(poi_candidate_id);
CREATE INDEX IF NOT EXISTS idx_signals_mention ON recommendation_signals(poi_mention_id);
CREATE INDEX IF NOT EXISTS idx_review_target ON review_decisions(target_type, target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_cache_expiry ON llm_extraction_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_discovery_candidate_status ON source_discovery_candidates(provider, status);
CREATE INDEX IF NOT EXISTS idx_discovery_run_provider ON source_discovery_runs(provider, started_at);
CREATE INDEX IF NOT EXISTS idx_content_classification_content
 ON content_classifications(source_content_id, created_at);
CREATE INDEX IF NOT EXISTS idx_content_classification_route
 ON content_classifications(primary_label, model_recommended, created_at);
"""


class SQLiteStorage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        if "parent_poi_id" not in {row[1] for row in self.connection.execute("PRAGMA table_info(pois)")}:
            self.connection.execute("ALTER TABLE pois ADD COLUMN parent_poi_id TEXT REFERENCES pois(id)")
        if "updated_count" not in {row[1] for row in self.connection.execute("PRAGMA table_info(source_runs)")}:
            self.connection.execute("ALTER TABLE source_runs ADD COLUMN updated_count INTEGER NOT NULL DEFAULT 0")
        source_run_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(source_runs)")}
        for column, definition in (("http_requests", "INTEGER"), ("retry_count", "INTEGER"),
                                   ("endpoint", "TEXT"), ("runtime_seconds", "REAL")):
            if column not in source_run_columns:
                self.connection.execute("ALTER TABLE source_runs ADD COLUMN {} {}".format(column, definition))
        if "strategy" not in {row[1] for row in self.connection.execute("PRAGMA table_info(source_collection_checkpoints)")}:
            self.connection.execute("ALTER TABLE source_collection_checkpoints ADD COLUMN strategy TEXT NOT NULL DEFAULT ''")
        if "parse_status" not in {row[1] for row in self.connection.execute("PRAGMA table_info(official_guidance_page_states)")}:
            self.connection.execute("ALTER TABLE official_guidance_page_states ADD COLUMN parse_status TEXT NOT NULL DEFAULT 'OK'")
            self.connection.execute("ALTER TABLE official_guidance_page_states ADD COLUMN parse_failure_reason TEXT")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _put(self, item: object) -> None:
        model = type(item)
        table = TABLES[model]
        # A human rejection is durable across later collection, parsing and
        # validation passes.  Those passes may rebuild a model object with
        # fresher source fields, but must never silently reopen a rejected
        # review target.  ReviewService records the decision append-only; this
        # guard enforces the same rule for all ordinary ``put`` callers.
        review_field = "status" if model is models.SourceDiscoveryCandidate else "review_status"
        if model in (models.EventCandidate, models.Event, models.OfficialGuidanceSnapshot,
                     models.SourceDiscoveryCandidate, models.POICandidate,
                     models.POIMention, models.POI):
            existing = self.connection.execute(
                "SELECT data FROM {} WHERE id=?".format(table), (item.id,)).fetchone()
            if existing:
                existing_payload = json.loads(existing["data"])
                if existing_payload.get(review_field) == "REJECTED":
                    setattr(item, review_field, "REJECTED")
                    if "review_reasons" in existing_payload:
                        item.review_reasons = list(existing_payload["review_reasons"])
                    elif "reasons" in existing_payload:
                        item.reasons = list(existing_payload["reasons"])
        payload = json.dumps(asdict(item), ensure_ascii=False, sort_keys=True)
        values = {"id": item.id, "data": payload}
        if model is models.RawSourceRecord:
            values.update(source=item.source, external_id=item.external_id, city=item.city, expires_at=item.expires_at)
        elif model is models.SourceContent:
            values.update(raw_record_id=item.raw_record_id, platform=item.platform, external_id=item.external_id)
        elif model is models.ContentClassification:
            values.update(source_content_id=item.source_content_id,
                          classifier_version=item.classifier_version,
                          content_fingerprint=item.content_fingerprint,
                          primary_label=item.primary_label, route=item.route,
                          confidence=item.confidence,
                          needs_review=int(item.needs_review),
                          model_recommended=int(item.model_recommended),
                          created_at=item.created_at)
        elif model is models.POI:
            values.update(city=item.city, country=item.country, map_visibility=int(item.map_visibility),
                          parent_poi_id=item.parent_poi_id)
        elif model is models.POICandidate:
            values.update(source_content_id=item.source_content_id, resolved_poi_id=item.resolved_poi_id, city=item.city)
        elif model is models.POIMention:
            values.update(source_content_id=item.source_content_id, poi_id=item.poi_id,
                          poi_candidate_id=item.poi_candidate_id, review_status=item.review_status)
        elif model is models.RecommendationSignal:
            values.update(poi_mention_id=item.poi_mention_id, type=item.type, value=item.value)
        elif model is models.LocationEvidence:
            values.update(source_content_id=item.source_content_id, poi_candidate_id=item.poi_candidate_id)
        elif model in (models.EventCandidate, models.Event):
            values.update(source_content_id=item.source_content_id)
            if model is models.Event:
                values["venue_poi_id"] = item.venue_poi_id
        elif model is models.FieldProvenance:
            values.update(entity_type=item.entity_type, entity_id=item.entity_id,
                          field_name=item.field_name, source_content_id=item.source_content_id)
        elif model is models.SourceRunRecord:
            values.update(multi_run_id=item.multi_run_id, source=item.source,
                          collection_key=item.collection_key, started_at=item.started_at,
                          ended_at=item.ended_at, new_count=item.new_count, updated_count=item.updated_count,
                          duplicate_count=item.duplicate_count, status=item.status,
                          failure_reason=item.failure_reason, next_run_status=item.next_run_status,
                          http_requests=item.http_requests, retry_count=item.retry_count,
                          endpoint=item.endpoint, runtime_seconds=item.runtime_seconds)
        elif model is models.SourceCollectionCheckpoint:
            values.update(source=item.source, collection_key=item.collection_key,
                          cursor_updated_at=item.cursor_updated_at,
                          cursor_row_id=item.cursor_row_id,
                          cycle_complete=int(item.cycle_complete), updated_at=item.updated_at,
                          strategy=item.strategy)
        elif model is models.SourceDiscoveryCandidate:
            values.update(provider=item.provider, dataset_id=item.dataset_id,
                          domain=item.domain, status=item.status,
                          discovered_query=item.discovered_query,
                          last_checked_at=item.last_checked_at)
        elif model is models.SourceDiscoveryRunRecord:
            values.update(provider=item.provider, started_at=item.started_at,
                          ended_at=item.ended_at, status=item.status,
                          next_run_status=item.next_run_status,
                          new_count=item.new_count, updated_count=item.updated_count,
                          duplicate_count=item.duplicate_count, filtered_count=item.filtered_count,
                          http_requests=item.http_requests, retry_count=item.retry_count,
                          query_count=item.query_count, failure_reason=item.failure_reason)
        elif model is models.SourceDiscoveryCheckpoint:
            values.update(provider=item.provider, query=item.query, cursor=item.cursor,
                          cycle_complete=int(item.cycle_complete), updated_at=item.updated_at,
                          strategy=item.strategy)
        elif model is models.OfficialGuidanceSnapshot:
            values.update(source=item.source, page_key=item.page_key, source_url=item.source_url,
                          content_version=item.content_version, collected_at=item.collected_at,
                          review_status=item.review_status)
        elif model is models.OfficialGuidancePageState:
            values.update(source=item.source, page_key=item.page_key, source_url=item.source_url,
                          current_snapshot_id=item.current_snapshot_id, last_checked_at=item.last_checked_at,
                          availability=item.availability, failure_reason=item.failure_reason,
                          parse_status=item.parse_status, parse_failure_reason=item.parse_failure_reason)
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join("{}=excluded.{}".format(key, key) for key in columns if key != "id")
        statement = "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT(id) DO UPDATE SET {}".format(
            table, ",".join(columns), placeholders, updates)
        self.connection.execute(statement, tuple(values.values()))

    def put(self, item: object) -> None:
        with self.connection:
            self._put(item)

    def apply_review(self, items: List[object], decision: models.ReviewDecision,
                     related_decisions: Sequence[models.ReviewDecision] = ()) -> None:
        target_models = {model.__name__: model for model in
                         (models.POICandidate, models.POIMention, models.POI,
                          models.EventCandidate, models.Event,
                          models.OfficialGuidanceSnapshot,
                          models.SourceDiscoveryCandidate)}
        decisions = [decision] + list(related_decisions)
        with self.connection:
            for current_decision in decisions:
                model = target_models.get(current_decision.target_type)
                if model is None or not any(type(item) is model and item.id == current_decision.target_id for item in items):
                    raise ValueError("review target must be among changed items")
                row = self.connection.execute(
                    "SELECT data FROM {} WHERE id=?".format(TABLES[model]), (current_decision.target_id,)).fetchone()
                if row is None:
                    raise ValueError("review target not found")
                current_payload = json.loads(row["data"])
                review_field = "status" if model is models.SourceDiscoveryCandidate else "review_status"
                if current_payload[review_field] != current_decision.previous_status:
                    raise ValueError("review target changed; reload before deciding")
                latest = self.connection.execute(
                    "SELECT action, new_status FROM review_decisions WHERE target_type=? AND target_id=? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (current_decision.target_type, current_decision.target_id)).fetchone()
                if latest and latest["action"] == current_decision.action and latest["new_status"] == current_decision.new_status:
                    raise ValueError("duplicate review operation")
            for item in items:
                self._put(item)
            for current_decision in decisions:
                self.connection.execute(
                    "INSERT INTO review_decisions(id,target_type,target_id,previous_status,new_status,action,created_at,data) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (current_decision.id, current_decision.target_type, current_decision.target_id,
                     current_decision.previous_status, current_decision.new_status,
                     current_decision.action, current_decision.created_at,
                     json.dumps(asdict(current_decision), ensure_ascii=False, sort_keys=True)))

    def review_history(self, target_type: str, target_id: str) -> List[models.ReviewDecision]:
        rows = self.connection.execute(
            "SELECT data FROM review_decisions WHERE target_type=? AND target_id=? ORDER BY created_at, id",
            (target_type, target_id)).fetchall()
        return [models.ReviewDecision(**json.loads(row["data"])) for row in rows]

    def get_extraction_cache(self, key: str, now: str) -> Optional[dict]:
        row = self.connection.execute(
            "SELECT data FROM llm_extraction_cache WHERE key=? AND expires_at>?", (key, now)).fetchone()
        return json.loads(row["data"]) if row else None

    def put_extraction_cache(self, key: str, result: dict, expires_at: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM llm_extraction_cache WHERE expires_at<=?", (utc_now(),))
            self.connection.execute(
                "INSERT INTO llm_extraction_cache(key,expires_at,data) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET expires_at=excluded.expires_at,data=excluded.data",
                (key, expires_at, json.dumps(result, ensure_ascii=False, sort_keys=True)))

    def source_runs(self) -> List[models.SourceRunRecord]:
        return self.all(models.SourceRunRecord)

    def get_checkpoint(self, source: str, collection_key: str) -> Optional[models.SourceCollectionCheckpoint]:
        row = self.connection.execute(
            "SELECT data FROM source_collection_checkpoints WHERE source=? AND collection_key=?",
            (source, collection_key)).fetchone()
        return models.SourceCollectionCheckpoint(**json.loads(row["data"])) if row else None

    def get_discovery_checkpoint(self, provider: str, query: str) -> Optional[models.SourceDiscoveryCheckpoint]:
        row = self.connection.execute(
            "SELECT data FROM source_discovery_checkpoints WHERE provider=? AND query=?",
            (provider, query)).fetchone()
        return models.SourceDiscoveryCheckpoint(**json.loads(row["data"])) if row else None

    def get(self, model: Type[T], item_id: str) -> Optional[T]:
        row = self.connection.execute("SELECT data FROM {} WHERE id=?".format(TABLES[model]), (item_id,)).fetchone()
        return model(**json.loads(row["data"])) if row else None

    def all(self, model: Type[T]) -> List[T]:
        rows = self.connection.execute("SELECT data FROM {} ORDER BY id".format(TABLES[model])).fetchall()
        return [model(**json.loads(row["data"])) for row in rows]

    def pois_in_city(self, city: str) -> List[models.POI]:
        rows = self.connection.execute("SELECT data FROM pois WHERE city=? ORDER BY id", (city,)).fetchall()
        return [models.POI(**json.loads(row["data"])) for row in rows]

    def recommendation_counts(self, poi_id: str) -> List[dict]:
        rows = self.connection.execute("""
            SELECT s.type, s.value, COUNT(DISTINCT m.source_content_id) AS mentions
            FROM recommendation_signals s
            JOIN poi_mentions m ON m.id=s.poi_mention_id
            JOIN source_contents c ON c.id=m.source_content_id
            WHERE m.poi_id=? AND m.review_status='AUTO_APPROVED' AND json_extract(m.data, '$.active') IS NOT 0
              AND json_extract(c.data, '$.content_status') != 'UNAVAILABLE'
            GROUP BY s.type, s.value
            ORDER BY mentions DESC, s.type, s.value
        """, (poi_id,)).fetchall()
        return [dict(row) for row in rows]

    def prune_signals(self, mention_id: str, keep_ids: List[str]) -> None:
        with self.connection:
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                self.connection.execute(
                    "DELETE FROM recommendation_signals WHERE poi_mention_id=? AND id NOT IN ({})".format(placeholders),
                    (mention_id, *keep_ids))
            else:
                self.connection.execute("DELETE FROM recommendation_signals WHERE poi_mention_id=?", (mention_id,))

    def purge_expired_raw(self, now: str) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM raw_source_records WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
        return cursor.rowcount
