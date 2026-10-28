import hashlib
from pathlib import Path
import tempfile
import unittest

from land_data.location_verification_db import attach_business_review_targets, prepare_db
from land_data.location_verification_replay import load_replay, run
from land_data.models import FieldProvenance, POI, POICandidate, RawSourceRecord, SourceContent
from land_data.sqlite_storage import SQLiteStorage


NOW = "2026-10-28T00:00:00Z"


class BusinessCandidatePreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "business.db"
        with SQLiteStorage(str(self.db_path)) as storage:
            storage.put(RawSourceRecord("raw-1", "osm", "node-1", "https://example.test/1",
                                        "new-york", {"name": "Cafe"}, NOW))
            storage.put(SourceContent("content-1", "raw-1", "osm", "node-1",
                                      "https://example.test/1", "place", "Cafe", "", NOW))
            storage.put(POI("poi-1", {"en": "Cafe"}, 40.0, -73.0, "new-york", "US",
                            "cafe", NOW, NOW, address="1 Main St", review_status="AUTO_APPROVED"))
            storage.put(POICandidate("candidate-1", "content-1", "Cafe", "new-york",
                                      "US", "cafe", NOW, address="1 Main St",
                                      latitude=40.0, longitude=-73.0,
                                      resolved_poi_id="poi-1", review_status="AUTO_APPROVED"))
            storage.put(FieldProvenance("prov-1", "POI", "poi-1", "latitude", "osm",
                                        NOW, source_content_id="content-1"))

    def test_live_candidate_is_frozen_without_promoting_old_approval_to_truth(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        output = self.root / "prepared"
        result = prepare_db(self.db_path, "candidate-1", output, NOW)
        case, tools, _ = load_replay(Path(result["case_path"]),
                                     Path(result["tool_manifest_path"]))
        self.assertEqual(result["tool_count"], 3)
        self.assertEqual(len(case.claims), 3)
        self.assertEqual({"read_candidate", "read_source_metadata", "read_linked_poi"}, set(tools))
        all_evidence = [item for tool in tools.values() for item in tool.handler(case, {})]
        self.assertFalse(any(item.claim_field == "identity" for item in all_evidence))
        self.assertNotIn("AUTO_APPROVED", Path(result["tool_manifest_path"]).read_text())
        self.assertEqual(result, prepare_db(self.db_path, "candidate-1", output, NOW))
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)
        outcome = run(Path(result["case_path"]), Path(result["tool_manifest_path"]),
                      self.root / "journal.db", "fixed", self.root)
        self.assertEqual(outcome["status"], "HANDOFF_HUMAN")
        self.assertEqual(outcome["reason"], "BUDGET_EXHAUSTED")
        linked = attach_business_review_targets(self.db_path, [
            {"case_id": "candidate-1", "reason": "BUDGET_EXHAUSTED"},
            {"case_id": "other", "reason": "CLAIM_CONFLICT"}])
        self.assertEqual(linked[0]["business_review_target"], {
            "type": "POICandidate", "id": "candidate-1", "review_status": "AUTO_APPROVED"})
        self.assertIsNone(linked[1]["business_review_target"])

    def test_missing_candidate_and_changed_snapshot_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "candidate not found"):
            prepare_db(self.db_path, "missing", self.root / "prepared", NOW)
        first = prepare_db(self.db_path, "candidate-1", self.root / "prepared", NOW)
        with SQLiteStorage(str(self.db_path)) as storage:
            candidate = storage.get(POICandidate, "candidate-1")
            candidate.latitude = 40.01
            storage.put(candidate)
        with self.assertRaisesRegex(ValueError, "existing prepared file differs"):
            prepare_db(self.db_path, "candidate-1", self.root / "prepared", NOW)
        self.assertTrue(Path(first["case_path"]).is_file())

    def test_future_business_data_is_not_exposed_to_earlier_replay(self):
        with self.assertRaisesRegex(ValueError, "collected after as-of"):
            prepare_db(self.db_path, "candidate-1", self.root / "earlier",
                       "2026-10-27T00:00:00Z")

    def test_post_uses_publication_time_for_claim_freshness(self):
        with SQLiteStorage(str(self.db_path)) as storage:
            content = storage.get(SourceContent, "content-1")
            content.published_at = "2024-01-01T00:00:00Z"
            storage.put(content)
        result = prepare_db(self.db_path, "candidate-1", self.root / "dated", NOW)
        case, tools, _ = load_replay(Path(result["case_path"]),
                                     Path(result["tool_manifest_path"]))
        evidence = tools["read_candidate"].handler(case, {})
        self.assertTrue(all(item.observed_at == "2024-01-01T00:00:00Z" for item in evidence))


if __name__ == "__main__":
    unittest.main()
