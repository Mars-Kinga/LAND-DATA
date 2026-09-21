import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from land_data.auth import MockSessionStore
from land_data.config import load_config
from land_data.cli import main
from land_data.export import dataset
from land_data.models import (FieldProvenance, LocationEvidence, POI, POICandidate,
                              POIMention, RecommendationSignal, ReviewDecision, SourceContent)
from land_data.pipeline import Pipeline
from land_data.review import ReviewService
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = load_config(str(ROOT))
        self.storage = SQLiteStorage(str(Path(temporary.name) / "review.db"))
        self.addCleanup(self.storage.close)
        sessions = MockSessionStore(str(Path(temporary.name) / "session.json"))
        sessions.authenticate("mock-social")
        adapters = [self.config.adapter(name, sessions) for name in self.config.sources if name != "osm"]
        Pipeline(self.storage, self.config).run("mock-review-city", adapters)
        self.review = ReviewService(self.storage, self.config.visibility_threshold)
        self.candidate = next(item for item in self.storage.all(POICandidate)
                              if item.name == "Evening Rooftop")
        self.mention = next(item for item in self.storage.all(POIMention)
                            if item.poi_candidate_id == self.candidate.id)
        self.east = next(item for item in self.storage.all(POI)
                         if "East" in next(iter(item.names.values())))
        self.west = next(item for item in self.storage.all(POI)
                         if "West" in next(iter(item.names.values())))

    def test_review_list_filters_and_machine_reasons(self):
        all_items = self.review.list(city="mock-review-city")
        self.assertEqual(all_items["count"], 2)
        candidates = self.review.list("candidate", "mock-review-city")
        mentions = self.review.list("mention", "mock-review-city")
        self.assertEqual((candidates["count"], mentions["count"]), (1, 1))
        self.assertEqual(self.review.list("poi", "mock-review-city")["count"], 0)
        self.assertEqual(self.review.list(city="mock-tokyo")["count"], 0)
        self.assertEqual(candidates["items"][0]["reasons"][0]["code"], "AMBIGUOUS_POI_MATCH")
        self.assertTrue(candidates["items"][0]["reasons"][0]["explanation"])

    def test_show_candidate_has_matches_evidence_and_source_context(self):
        shown = self.review.show(self.candidate.id)
        self.assertEqual({item["poi_id"] for item in shown["possible_poi_matches"]},
                         {self.east.id, self.west.id})
        self.assertEqual(len(shown["location_evidence"]), 2)
        self.assertEqual(shown["source"]["source_content"]["external_id"], "review-post-123")
        self.assertIn("sunset", shown["source"]["source_content"]["text"])
        self.assertTrue(shown["source"]["raw_source_record"]["retained"])
        self.assertEqual(shown["mention_ids"], [self.mention.id])

    def test_show_mention_has_signals_and_source(self):
        shown = self.review.show(self.mention.id)
        self.assertEqual({value["value"] for value in shown["recommendation_signals"]},
                         {"sunset", "date"})
        self.assertEqual(shown["candidate"]["id"], self.candidate.id)
        self.assertEqual(shown["source"]["source_content"]["external_id"], "review-post-123")
        self.assertEqual(shown["detail"]["relation_type"], "RECOMMENDATION")

    def test_show_poi_has_provenance_and_source_context(self):
        self.review.resolve(self.candidate.id, self.east.id)
        shown = self.review.show(self.east.id)
        self.assertEqual(shown["type"], "poi")
        self.assertIn(self.candidate.id, shown["candidate_ids"])
        self.assertIn(self.mention.id, shown["mention_ids"])
        self.assertTrue(shown["provenance"])
        self.assertIn("review-post-123", [item["source_content"]["external_id"]
                                           for item in shown["sources"]])

    def test_review_decision_creation_and_history(self):
        decision = self.review.resolve(self.candidate.id, self.east.id, "Matched east entrance")
        self.assertIsInstance(decision, ReviewDecision)
        self.assertEqual((decision.previous_status, decision.new_status, decision.action),
                         ("NEEDS_REVIEW", "AUTO_APPROVED", "RESOLVE"))
        self.assertEqual((decision.reviewer, decision.reason), ("local-user", "Matched east entrance"))
        self.assertEqual(decision.metadata["previous_review_reasons"], ["AMBIGUOUS_POI_MATCH"])
        self.assertEqual(self.review.history(self.candidate.id)[0]["id"], decision.id)
        self.assertEqual(self.review.history(self.mention.id)[0]["action"], "BIND")
        later = self.review.approve(self.candidate.id, "Confirmed again")
        self.assertEqual([item["action"] for item in self.review.history(self.candidate.id)],
                         ["RESOLVE", "APPROVE"])
        self.assertEqual(later.previous_status, "AUTO_APPROVED")

    def test_review_history_is_append_only_at_database_level(self):
        decision = self.review.resolve(self.candidate.id, self.east.id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.connection.execute("UPDATE review_decisions SET action='X' WHERE id=?", (decision.id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.connection.execute("DELETE FROM review_decisions WHERE id=?", (decision.id,))
        self.assertEqual(self.review.history(self.candidate.id)[0]["action"], "RESOLVE")

    def test_approve_poi_and_bound_mention(self):
        poi_decision = self.review.approve(self.east.id)
        self.assertEqual(poi_decision.action, "APPROVE")
        self.assertTrue(self.storage.get(POI, self.east.id).map_visibility)
        with self.assertRaises(ValueError):
            self.review.approve(self.mention.id)
        self.review.resolve(self.candidate.id, self.east.id)
        mention_decision = self.review.approve(self.mention.id)
        self.assertEqual(mention_decision.target_type, "POIMention")
        self.assertEqual([item["action"] for item in self.review.history(self.mention.id)],
                         ["BIND", "APPROVE"])

    def test_manual_approval_of_low_confidence_mention_survives_validation(self):
        self.mention.confidence_score = 0.5
        self.storage.put(self.mention)
        self.review.resolve(self.candidate.id, self.east.id)
        self.assertEqual(self.storage.get(POIMention, self.mention.id).review_status, "NEEDS_REVIEW")
        self.assertEqual(self.review.list("mention", "mock-review-city")["count"], 1)
        self.review.approve(self.mention.id, "Human verified recommendation")
        self.assertEqual(self.storage.get(POIMention, self.mention.id).review_status, "AUTO_APPROVED")
        self.assertEqual(self.storage.recommendation_counts(self.east.id)[0]["mentions"], 1)

    def test_reject_requires_reason_and_preserves_rejection(self):
        with self.assertRaises(ValueError):
            self.review.reject(self.candidate.id, "  ")
        self.assertEqual(self.review.history(self.candidate.id), [])
        decision = self.review.reject(self.candidate.id, "Not a real place")
        self.assertEqual(decision.action, "REJECT")
        self.assertEqual(self.storage.get(POICandidate, self.candidate.id).review_status, "REJECTED")
        self.assertEqual(self.storage.get(POIMention, self.mention.id).review_status, "NEEDS_REVIEW")
        self.assertIn("REJECTED_CANDIDATE", self.storage.get(POIMention, self.mention.id).review_reasons)
        with self.assertRaises(ValueError):
            self.review.reject(self.candidate.id, "Again")
        self.assertEqual(len(self.review.history(self.candidate.id)), 1)

    def test_reject_mention_keeps_signals(self):
        signal_ids = {item.id for item in self.storage.all(RecommendationSignal)}
        self.review.reject(self.mention.id, "Not a recommendation")
        self.assertEqual(self.storage.get(POIMention, self.mention.id).review_status, "REJECTED")
        self.assertEqual({item.id for item in self.storage.all(RecommendationSignal)}, signal_ids)

    def test_resolve_preserves_derived_data_and_changes_export_aggregation(self):
        before = dataset(self.storage, self.config, "mock-review-city")
        self.assertEqual((len(before["pois"]), len(before["mentions"])), (2, 0))
        mention_ids = {item.id for item in self.storage.all(POIMention)}
        signal_ids = {item.id for item in self.storage.all(RecommendationSignal)}
        evidence_ids = {item.id for item in self.storage.all(LocationEvidence)}
        provenance_ids = {item.id for item in self.storage.all(FieldProvenance)}
        self.review.resolve(self.candidate.id, self.east.id)
        self.assertEqual(self.storage.get(POICandidate, self.candidate.id).resolved_poi_id, self.east.id)
        self.assertEqual(self.storage.get(POIMention, self.mention.id).poi_id, self.east.id)
        self.assertEqual({item.id for item in self.storage.all(POIMention)}, mention_ids)
        self.assertEqual({item.id for item in self.storage.all(RecommendationSignal)}, signal_ids)
        self.assertEqual({item.id for item in self.storage.all(LocationEvidence)}, evidence_ids)
        self.assertTrue(provenance_ids <= {item.id for item in self.storage.all(FieldProvenance)})
        self.assertTrue(all(item.matched_poi_id == self.east.id for item in self.storage.all(LocationEvidence)
                            if item.poi_candidate_id == self.candidate.id))
        self.assertEqual(self.storage.recommendation_counts(self.east.id), [
            {"type": "feature", "value": "sunset", "mentions": 1},
            {"type": "good_for", "value": "date", "mentions": 1}])
        self.assertEqual(self.storage.recommendation_counts(self.west.id), [])
        after = dataset(self.storage, self.config, "mock-review-city")
        self.assertEqual((len(after["pois"]), len(after["mentions"])), (2, 1))
        self.assertEqual(after["mentions"][0]["poi_id"], self.east.id)

    def test_promote_creates_poi_without_guessing_fields_and_preserves_links(self):
        content = self.storage.get(SourceContent, self.candidate.source_content_id)
        candidate = POICandidate("new-place", content.id, "Brand New Rooftop",
                                 "mock-review-city", "ZZ", "food", "2026-01-01T00:00:00Z",
                                 latitude=0.02, longitude=0.03, confidence_score=0.88)
        self.storage.put(candidate)
        mention = POIMention("new-mention", content.id, "RECOMMENDS", "2026-01-01T00:00:00Z",
                             poi_candidate_id=candidate.id, confidence_score=0.88)
        self.storage.put(mention)
        signal = RecommendationSignal("new-signal", mention.id, "food", "matcha", 0.9,
                                      "text", "2026-01-01T00:00:00Z")
        self.storage.put(signal)
        evidence = LocationEvidence("new-evidence", content.id, candidate.id, "GEO_TAG", "new rooftop",
                                    0.88, "2026-01-01T00:00:00Z")
        self.storage.put(evidence)
        decision = self.review.promote(candidate.id, "Verified new place")
        poi_id = decision.metadata["poi_id"]
        self.assertEqual(self.storage.get(POI, poi_id).names, {"und": "Brand New Rooftop"})
        self.assertTrue(self.storage.get(POI, poi_id).map_visibility)
        self.assertEqual(self.storage.get(POICandidate, candidate.id).resolved_poi_id, poi_id)
        self.assertEqual(self.storage.get(POIMention, mention.id).poi_id, poi_id)
        self.assertEqual(self.storage.get(RecommendationSignal, signal.id).poi_mention_id, mention.id)
        self.assertEqual(self.storage.get(LocationEvidence, evidence.id).matched_poi_id, poi_id)
        self.assertEqual(len([item for item in self.storage.all(FieldProvenance)
                              if item.entity_type == "POI" and item.entity_id == poi_id]), 5)
        self.assertEqual(self.review.history(mention.id)[0]["action"], "BIND")
        self.assertEqual(self.storage.recommendation_counts(poi_id)[0]["value"], "matcha")

    def test_promote_requires_known_coordinates_and_fields(self):
        self.candidate.latitude = None
        self.storage.put(self.candidate)
        with self.assertRaisesRegex(ValueError, "INVALID_COORDINATES"):
            self.review.promote(self.candidate.id)
        self.assertEqual(self.review.history(self.candidate.id), [])

    def test_invalid_target_and_poi(self):
        for action in (lambda: self.review.show("missing"),
                       lambda: self.review.approve("missing"),
                       lambda: self.review.reject("missing", "reason"),
                       lambda: self.review.promote("missing"),
                       lambda: self.review.resolve("missing", self.east.id),
                       lambda: self.review.resolve(self.candidate.id, "missing")):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(self.review.history(self.candidate.id), [])

    def test_duplicate_resolve_and_approve(self):
        self.review.resolve(self.candidate.id, self.east.id)
        with self.assertRaisesRegex(ValueError, "already resolved"):
            self.review.resolve(self.candidate.id, self.east.id)
        self.review.approve(self.mention.id)
        with self.assertRaisesRegex(ValueError, "duplicate review operation"):
            self.review.approve(self.mention.id)

    def test_rejected_candidate_cannot_be_resolved_or_promoted(self):
        self.review.reject(self.candidate.id, "False location")
        with self.assertRaisesRegex(ValueError, "rejected candidate"):
            self.review.resolve(self.candidate.id, self.east.id)
        with self.assertRaisesRegex(ValueError, "rejected candidate"):
            self.review.promote(self.candidate.id)

    def test_rejected_poi_cannot_receive_resolution(self):
        self.review.reject(self.east.id, "Invalid place")
        with self.assertRaisesRegex(ValueError, "rejected POI"):
            self.review.resolve(self.candidate.id, self.east.id)
        self.assertIsNone(self.storage.get(POICandidate, self.candidate.id).resolved_poi_id)
        self.assertEqual(self.review.history(self.candidate.id), [])

    def test_cli_list_show_reject_and_history(self):
        db_path = self.storage.connection.execute("PRAGMA database_list").fetchone()[2]

        def command(*arguments):
            output = StringIO()
            with redirect_stdout(output):
                code = main(["--db", db_path, "review", *arguments])
            self.assertEqual(code, 0)
            return json.loads(output.getvalue())

        listing = command("list", "--type", "candidate", "--city", "mock-review-city")
        self.assertEqual(listing["items"][0]["id"], self.candidate.id)
        self.assertEqual(command("show", self.candidate.id)["source"]["source_content"]["external_id"],
                         "review-post-123")
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                main(["--db", db_path, "review", "reject", self.candidate.id])
        decision = command("reject", self.candidate.id, "--reason", "No longer valid")
        self.assertEqual(decision["action"], "REJECT")
        self.assertEqual(command("history", self.candidate.id)["history"][0]["reason"],
                         "No longer valid")

    def test_source_context_after_raw_expiry_and_secret_redaction(self):
        self.storage.purge_expired_raw("9999-01-01T00:00:00Z")
        content = self.storage.get(SourceContent, self.candidate.source_content_id)
        content.text += " token=private-value Bearer another-secret"
        self.storage.put(content)
        shown = json.dumps(self.review.show(self.candidate.id))
        self.assertNotIn("private-value", shown)
        self.assertNotIn("another-secret", shown)
        self.assertIn('"retained": false', shown)
        self.assertIn("review-post-123", shown)


if __name__ == "__main__":
    unittest.main()
