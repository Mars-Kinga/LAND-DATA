"""SQLite implementation; SQL stays behind the small Storage interface."""
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Sequence, Type, TypeVar

from . import models

T = TypeVar("T")

TABLES = {
    models.RawSourceRecord: "raw_source_records",
    models.SourceContent: "source_contents",
    models.POI: "pois",
    models.POICandidate: "poi_candidates",
    models.POIMention: "poi_mentions",
    models.RecommendationSignal: "recommendation_signals",
    models.LocationEvidence: "location_evidence",
    models.EventCandidate: "event_candidates",
    models.Event: "events",
    models.FieldProvenance: "field_provenance",
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
CREATE TABLE IF NOT EXISTS pois (
 id TEXT PRIMARY KEY, city TEXT NOT NULL, country TEXT NOT NULL,
 map_visibility INTEGER NOT NULL CHECK(map_visibility IN (0,1)), data TEXT NOT NULL
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
"""


class SQLiteStorage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _put(self, item: object) -> None:
        model = type(item)
        table = TABLES[model]
        payload = json.dumps(asdict(item), ensure_ascii=False, sort_keys=True)
        values = {"id": item.id, "data": payload}
        if model is models.RawSourceRecord:
            values.update(source=item.source, external_id=item.external_id, city=item.city, expires_at=item.expires_at)
        elif model is models.SourceContent:
            values.update(raw_record_id=item.raw_record_id, platform=item.platform, external_id=item.external_id)
        elif model is models.POI:
            values.update(city=item.city, country=item.country, map_visibility=int(item.map_visibility))
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
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        if model is models.RawSourceRecord:
            statement = "INSERT OR IGNORE INTO {} ({}) VALUES ({})".format(table, ",".join(columns), placeholders)
        else:
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
                         (models.POICandidate, models.POIMention, models.POI)}
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
                if json.loads(row["data"])["review_status"] != current_decision.previous_status:
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
            WHERE m.poi_id=? AND m.review_status='AUTO_APPROVED'
            GROUP BY s.type, s.value
            ORDER BY mentions DESC, s.type, s.value
        """, (poi_id,)).fetchall()
        return [dict(row) for row in rows]

    def purge_expired_raw(self, now: str) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM raw_source_records WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
        return cursor.rowcount
