import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from land_data.location_verification_prepare import prepare
from land_data.location_verification_replay import load_replay, run


class CandidatePreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dossier = self.root / "dossiers.jsonl"
        self.item = {"case_id": "candidate-1", "as_of": "2026-10-28T23:59:59Z",
                     "candidate_claims": {"latitude": 40.0, "longitude": -73.0},
                     "source_records": [
                         {"source_group": "DOHMH_FROZEN", "row_locator": "source-1",
                          "row_sha256": "hash-a", "lineage_key": "DOHMH",
                          "timestamp": "2026-10-18T00:00:00Z", "raw_record": {}},
                         {"source_group": "DOT_CURRENT", "row_locator": 1,
                          "row_sha256": "hash-b", "lineage_key": "DOT",
                          "timestamp": "2026-10-20T00:00:00Z",
                          "raw_record": {"latitude": "40.01", "longitude": "-73.0"}}]}
        for record in self.item["source_records"]:
            record["row_sha256"] = hashlib.sha256(json.dumps(
                record["raw_record"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.dossier.write_text(json.dumps(self.item) + "\n")

    def checksum(self):
        return hashlib.sha256(self.dossier.read_bytes()).hexdigest()

    def test_prepared_real_source_case_is_hash_locked_and_handoffs(self):
        output = self.root / "prepared"
        result = prepare(self.dossier, self.checksum(), "candidate-1", output)
        case, tools, _ = load_replay(Path(result["case_path"]), Path(result["tool_manifest_path"]))
        self.assertEqual(result["tool_count"], 2)
        self.assertEqual(len(case.claims), 2)
        self.assertEqual(len(tools), 2)
        self.assertEqual(prepare(self.dossier, self.checksum(), "candidate-1", output), result)
        self.assertEqual(Path(result["case_path"]).stat().st_mode & 0o777, 0o600)
        outcome = run(Path(result["case_path"]), Path(result["tool_manifest_path"]),
                      self.root / "journal.db", "fixed", self.root)
        self.assertEqual((outcome["status"], outcome["reason"]),
                         ("HANDOFF_HUMAN", "CLAIM_CONFLICT"))

    def test_hash_answer_leak_and_zero_coordinate_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            prepare(self.dossier, "bad", "candidate-1", self.root / "out")
        self.item["gold_label"] = "KEEP"
        self.dossier.write_text(json.dumps(self.item) + "\n")
        with self.assertRaisesRegex(ValueError, "answer-bearing"):
            prepare(self.dossier, self.checksum(), "candidate-1", self.root / "out")
        del self.item["gold_label"]
        self.item["candidate_claims"]["latitude"] = 0
        self.item["candidate_claims"]["longitude"] = 0
        self.dossier.write_text(json.dumps(self.item) + "\n")
        with self.assertRaisesRegex(ValueError, "valid coordinates"):
            prepare(self.dossier, self.checksum(), "candidate-1", self.root / "out")


if __name__ == "__main__":
    unittest.main()
