import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from land_data.location_verification import Claim, Evidence, VerificationCase
from land_data.location_verification_db import prepare_db
from land_data.location_verification_replay import run
from land_data.location_verification_review import hold_conflict
from land_data.models import POI, POICandidate, RawSourceRecord, SourceContent
from land_data.processing import validate_score_review
from land_data.review import ReviewService
from land_data.sqlite_storage import SQLiteStorage


NOW = "2026-10-28T00:00:00Z"


class BusinessReviewHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "business.db"
        with SQLiteStorage(str(self.db)) as storage:
            storage.put(RawSourceRecord("raw-1", "osm", "1", "https://example.test/1",
                                        "new-york", {}, NOW))
            storage.put(SourceContent("content-1", "raw-1", "osm", "1",
                                      "https://example.test/1", "place", "Cafe", "", NOW))
            storage.put(POI("poi-1", {"en": "Cafe"}, 40.0, -73.0, "new-york", "US",
                            "cafe", NOW, NOW, address="1 Main St", review_status="AUTO_APPROVED",
                            coordinates_verified=True, validation_passed=True,
                            confidence_score=1.0, map_visibility=True))
            storage.put(POICandidate("candidate-1", "content-1", "Cafe", "new-york",
                                      "US", "cafe", NOW, address="1 Main St",
                                      latitude=40.0, longitude=-73.0,
                                      resolved_poi_id="poi-1", review_status="AUTO_APPROVED"))

    def conflict_run(self):
        case = VerificationCase("candidate-1", "candidate-1", NOW,
                                (Claim("coordinates", "40.0,-73.0", tolerance_meters=10),
                                 Claim("identity", "candidate-1"),
                                 Claim("address", "1 Main St")),
                                ("read_candidate", "read_conflict"), max_calls=2)
        evidence = [Evidence("coordinates", value, name, group, NOW,
                             "candidate-1", name, name).__dict__
                    for name, group, value in (
                        ("candidate", "OSM", "40.0,-73.0"),
                        ("dot", "DOT", "40.01,-73.0"))]
        manifest = {"schema_version": "lv-replay-tools-v1", "case_id": case.case_id,
                    "tools": {"read_candidate": {"source_groups": ["OSM"],
                                                 "evidence": [evidence[0]]},
                              "read_conflict": {"source_groups": ["DOT"],
                                                "evidence": [evidence[1]]}}}
        manifest_path = self.root / "tools.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        case_path = self.root / "case.json"
        case_path.write_text(json.dumps({"schema_version": "lv-replay-input-v1",
                                         "tool_manifest_sha256": hashlib.sha256(
                                             manifest_path.read_bytes()).hexdigest(),
                                         "case": {"case_id": case.case_id,
                                                  "identity_key": case.identity_key,
                                                  "as_of": case.as_of,
                                                  "claims": [item.__dict__ for item in case.claims],
                                                  "allowed_tools": list(case.allowed_tools),
                                                  "max_calls": case.max_calls,
                                                  "max_decisions": case.max_decisions,
                                                  "max_evidence_age_days": case.max_evidence_age_days}}))
        journal = self.root / "journal.db"
        outcome = run(case_path, manifest_path, journal, "fixed", self.root)
        self.assertEqual(outcome["reason"], "CLAIM_CONFLICT")
        return case_path, manifest_path, journal

    def test_preview_then_apply_holds_candidate_and_poi_across_validation(self):
        case_path, manifest_path, journal = self.conflict_run()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        preview = hold_conflict(self.db, case_path, manifest_path, journal)
        self.assertFalse(preview["applied"])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        result = hold_conflict(self.db, case_path, manifest_path, journal, apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual(len(result["review_decisions"]), 2)
        with SQLiteStorage(str(self.db)) as storage:
            candidate = storage.get(POICandidate, "candidate-1")
            poi = storage.get(POI, "poi-1")
            self.assertEqual(candidate.review_status, "NEEDS_REVIEW")
            self.assertEqual(poi.review_status, "NEEDS_REVIEW")
            self.assertFalse(poi.map_visibility)
            self.assertEqual(storage.review_history("POICandidate", candidate.id)[-1].action,
                             "VERIFY_HOLD")
            candidate.review_status = "AUTO_APPROVED"
            storage.put(candidate)
            self.assertEqual(storage.get(POICandidate, candidate.id).review_status,
                             "NEEDS_REVIEW")
            validate_score_review(storage, 0.5)
            self.assertEqual(storage.get(POI, poi.id).review_status, "NEEDS_REVIEW")
            self.assertFalse(storage.get(POI, poi.id).map_visibility)
            review = ReviewService(storage, 0.5)
            review.approve(candidate.id, reviewer="human")
            review.approve(poi.id, reviewer="human")
            self.assertEqual(storage.get(POI, poi.id).review_status, "AUTO_APPROVED")
        self.assertFalse(hold_conflict(self.db, case_path, manifest_path, journal,
                                       apply=True)["applied"])

    def test_budget_exhaustion_cannot_apply_conflict_hold(self):
        prepared = prepare_db(self.db, "candidate-1", self.root / "prepared", NOW)
        journal = self.root / "budget.db"
        outcome = run(Path(prepared["case_path"]), Path(prepared["tool_manifest_path"]),
                      journal, "fixed", self.root)
        self.assertEqual(outcome["reason"], "BUDGET_EXHAUSTED")
        with self.assertRaisesRegex(ValueError, "only an evidence conflict"):
            hold_conflict(self.db, Path(prepared["case_path"]),
                          Path(prepared["tool_manifest_path"]), journal, apply=True)


if __name__ == "__main__":
    unittest.main()
