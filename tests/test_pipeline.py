import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from land_data.auth import MockSessionStore
from land_data.config import load_config
from land_data.export import dataset
from land_data.models import (Event, FieldProvenance, LocationEvidence, POI, POICandidate,
                              POIMention, RawSourceRecord, RecommendationSignal, SourceContent)
from land_data.pipeline import Pipeline
from land_data.processing import distance_meters, resolve, valid_coordinates, validate_score_review
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = load_config(str(ROOT))
        self.storage = SQLiteStorage(str(Path(self.temp.name) / "test.db"))
        self.addCleanup(self.storage.close)
        self.sessions = MockSessionStore(str(Path(self.temp.name) / "mock-session.json"))
        self.pipeline = Pipeline(self.storage, self.config)

    def adapters(self):
        return [self.config.adapter(name, self.sessions) for name in self.config.sources]

    def run_full(self):
        self.sessions.authenticate("mock-social")
        return self.pipeline.run("mock-tokyo", self.adapters())

    def test_full_pipeline_counts_and_relationships(self):
        counts = self.run_full()
        self.assertEqual(counts["RawSourceRecord"], 4)
        self.assertEqual(counts["SourceContent"], 4)
        self.assertEqual(counts["POICandidate"], 4)
        self.assertEqual(counts["POIMention"], 3)
        self.assertEqual(counts["RecommendationSignal"], 4)
        self.assertEqual(counts["LocationEvidence"], 5)
        self.assertEqual(counts["Event"], 1)
        self.assertEqual(len(self.storage.all(FieldProvenance)), 5)

    def test_raw_normalized_and_derived_are_separate(self):
        self.run_full()
        raw = self.storage.all(RawSourceRecord)
        contents = self.storage.all(SourceContent)
        self.assertTrue(all(item.raw_record_id for item in contents))
        self.assertTrue(any(item.payload.get("locations") for item in raw))
        self.assertTrue(all("session_token" not in json.dumps(item.payload) for item in raw))
        self.assertTrue(all("session_token" not in json.dumps(item.raw_metadata) for item in contents))
        self.assertTrue(all("recommendation_signals" not in json.dumps(item.raw_metadata) for item in contents))

    def test_unresolved_mention_and_signal_survive(self):
        self.run_full()
        unresolved = [item for item in self.storage.all(POIMention) if item.poi_id is None]
        self.assertEqual(len(unresolved), 1)
        self.assertIsNotNone(unresolved[0].poi_candidate_id)
        self.assertEqual(unresolved[0].review_status, "NEEDS_REVIEW")
        self.assertEqual(len([s for s in self.storage.all(RecommendationSignal)
                              if s.poi_mention_id == unresolved[0].id]), 1)

    def test_resolved_mentions_bind_to_poi_and_aggregate(self):
        self.run_full()
        poi = self.storage.all(POI)[0]
        linked = [m for m in self.storage.all(POIMention) if m.poi_id == poi.id]
        self.assertEqual(len(linked), 2)
        self.assertTrue(all(m.poi_candidate_id for m in linked))
        self.assertEqual(self.storage.recommendation_counts(poi.id), [
            {"type": "feature", "value": "sunset", "mentions": 2},
            {"type": "good_for", "value": "date", "mentions": 1}])

    def test_export_contains_only_visible_and_approved_data(self):
        self.run_full()
        result = dataset(self.storage, self.config, "mock-tokyo")
        self.assertEqual((len(result["pois"]), len(result["mentions"])), (1, 2))
        serialized = json.dumps(result)
        self.assertNotIn("SYNTHETIC_TEST_ONLY", serialized)
        self.assertNotIn("Unknown Rooftop", serialized)
        self.assertIn("https://social.example.invalid/posts/001", serialized)
        self.assertNotIn("utm_source", serialized)

    def test_replay_is_idempotent(self):
        first = self.run_full()
        second = self.pipeline.run("mock-tokyo", self.adapters())
        self.assertEqual(first, second)

    def test_review_queue_has_unresolved_and_event(self):
        self.run_full()
        queue = self.pipeline.review_queue()
        self.assertEqual(len(queue["POICandidate"]), 1)
        self.assertEqual(len(queue["POIMention"]), 1)
        self.assertEqual(len(queue["Event"]), 1)
        self.assertIn("INSUFFICIENT_LOCATION_EVIDENCE", queue["POICandidate"][0]["reasons"])

    def test_map_visibility_requires_all_gates(self):
        self.run_full()
        poi = self.storage.all(POI)[0]
        self.assertTrue(poi.map_visibility)
        poi.active = False
        self.storage.put(poi)
        validate_score_review(self.storage, 0.8)
        self.assertFalse(self.storage.get(POI, poi.id).map_visibility)
        poi.active = True
        poi.coordinates_verified = False
        self.storage.put(poi)
        validate_score_review(self.storage, 0.8)
        self.assertFalse(self.storage.get(POI, poi.id).map_visibility)

    def test_exact_name_without_coordinates_resolves_uniquely(self):
        self.run_full()
        candidates = self.storage.all(POICandidate)
        no_coords = [item for item in candidates if item.name == "Example Rooftop" and item.latitude is None]
        self.assertEqual(len(no_coords), 1)
        self.assertIsNotNone(no_coords[0].resolved_poi_id)

    def test_invalid_coordinates_do_not_create_poi(self):
        self.run_full()
        content = self.storage.all(SourceContent)[0]
        candidate = POICandidate("invalid", content.id, "Bad", "mock-tokyo", "JP", "food", "t",
                                 latitude=120.0, longitude=139.0, source_kind="structured",
                                 coordinate_verified=True, confidence_score=0.95)
        self.storage.put(candidate)
        resolve(self.storage)
        self.assertIsNone(self.storage.get(POICandidate, "invalid").resolved_poi_id)
        self.assertIn("INVALID_COORDINATES", self.storage.get(POICandidate, "invalid").review_reasons)

    def test_conflicting_coordinates_stay_for_review(self):
        self.run_full()
        content = self.storage.all(SourceContent)[0]
        candidate = POICandidate("conflict", content.id, "Example Rooftop", "mock-tokyo", "JP", "food", "t",
                                 latitude=35.7, longitude=139.8, confidence_score=0.9)
        self.storage.put(candidate)
        resolve(self.storage)
        self.assertIn("CONFLICTING_COORDINATES", self.storage.get(POICandidate, "conflict").review_reasons)

    def test_ambiguous_match_stays_for_review(self):
        self.run_full()
        original = self.storage.all(POI)[0]
        other = POI("another", {"en": "Example Rooftop"}, 35.68125, 139.7671,
                    "mock-tokyo", "JP", "food", "t", "t")
        self.storage.put(other)
        content = self.storage.all(SourceContent)[0]
        candidate = POICandidate("ambiguous", content.id, "Example Rooftop", "mock-tokyo", "JP", "food", "t",
                                 latitude=original.latitude, longitude=original.longitude)
        self.storage.put(candidate)
        resolve(self.storage)
        self.assertIn("AMBIGUOUS_POI_MATCH", self.storage.get(POICandidate, "ambiguous").review_reasons)

    def test_distance_and_coordinate_validation(self):
        self.assertLess(distance_meters(35.0, 139.0, 35.0001, 139.0), 20)
        self.assertFalse(valid_coordinates(float("nan"), 139.0))
        self.assertFalse(valid_coordinates(91.0, 139.0))

    def test_sqlite_mention_constraint(self):
        self.run_full()
        content = self.storage.all(SourceContent)[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.connection.execute(
                "INSERT INTO poi_mentions(id,source_content_id,review_status,data) VALUES (?,?,?,?)",
                ("broken", content.id, "NEEDS_REVIEW", "{}"))

    def test_raw_retention_purge(self):
        self.run_full()
        deleted = self.storage.purge_expired_raw("9999-01-01T00:00:00Z")
        self.assertEqual(deleted, 4)
        self.assertEqual(len(self.storage.all(RawSourceRecord)), 0)
        self.assertEqual(len(self.storage.all(SourceContent)), 4)

    def test_rejected_review_decision_survives_validation(self):
        self.run_full()
        poi = self.storage.all(POI)[0]
        poi.review_status = "REJECTED"
        poi.review_reasons = ["manual_rejection"]
        self.storage.put(poi)
        mention = next(item for item in self.storage.all(POIMention) if item.poi_id == poi.id)
        mention.review_status = "REJECTED"
        mention.review_reasons = ["manual_rejection"]
        self.storage.put(mention)
        self.pipeline.validate()
        self.assertEqual(self.storage.get(POI, poi.id).review_status, "REJECTED")
        self.assertFalse(self.storage.get(POI, poi.id).map_visibility)
        self.assertEqual(self.storage.get(POIMention, mention.id).review_status, "REJECTED")

    def test_export_policy_can_hide_recommendation_details(self):
        self.run_full()
        from dataclasses import replace
        from land_data.config import SourceConfig
        source = self.config.sources["mock-social"]
        restricted = replace(source, policy=replace(source.policy, export_fields=()))
        config = replace(self.config, sources={**self.config.sources, "mock-social": restricted})
        output = dataset(self.storage, config, "mock-tokyo")
        self.assertNotIn("source_url", output["mentions"][0]["source"])
        self.assertNotIn("recommendation_summary", output["mentions"][0])
        self.assertNotIn("signals", output["mentions"][0])

    def test_source_policy_can_omit_url_from_raw_and_normalized(self):
        from dataclasses import replace
        source = self.config.sources["mock-places"]
        restricted = replace(source, policy=replace(source.policy, retain_source_url=False))
        config = replace(self.config, sources={**self.config.sources, "mock-places": restricted})
        pipeline = Pipeline(self.storage, config)
        pipeline.collect("mock-tokyo", [config.adapter("mock-places", self.sessions)])
        pipeline.parse("mock-tokyo")
        self.assertIsNone(self.storage.all(RawSourceRecord)[0].source_url)
        self.assertIsNone(self.storage.all(SourceContent)[0].source_url)
