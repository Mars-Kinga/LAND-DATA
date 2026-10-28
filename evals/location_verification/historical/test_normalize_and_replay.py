import unittest
from pathlib import Path

from normalize_and_replay import build_report


ROOT = Path(__file__).resolve().parents[3]


class HistoricalReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_report(ROOT)

    def test_record_counts_and_deduplication(self):
        self.assertEqual(
            self.report["record_counts"],
            {
                "source_records": 51,
                "final_rawResults": 51,
                "final_uniqueEntities": 47,
                "dedupe_removed": 4,
                "duplicate_groups": 3,
            },
        )
        self.assertEqual(
            [group["canonical_name"] for group in self.report["duplicate_groups"]],
            ["Cà Phê Giảng", "MAY KITCHENWARE", "The Haflington"],
        )

    def test_status_distribution(self):
        self.assertEqual(
            self.report["status_distribution"],
            {"CONFIRMED": 38, "CONFLICT": 9, "NOT_FOUND": 4},
        )

    def test_missing_fields_and_sources(self):
        missing = self.report["missing_fields"]["all_records"]
        self.assertEqual(missing["latitude"], 11)
        self.assertEqual(missing["longitude"], 11)
        self.assertEqual(missing["address"], 4)
        self.assertEqual(self.report["source_coverage"]["primary_missing"], 0)
        self.assertEqual(self.report["source_coverage"]["secondary_missing"], 0)
        self.assertEqual(self.report["source_coverage"]["both_present"], 51)

    def test_replay_consistency(self):
        checks = self.report["replay_checks"]
        self.assertTrue(checks["all_passed"])
        self.assertTrue(checks["source_indices_contiguous_1_to_51"])
        self.assertTrue(checks["source_records_match_final_rawResults"])
        self.assertTrue(checks["source_dedupe_matches_final_uniqueEntities"])
        self.assertTrue(checks["final_summary_matches_source"])

    def test_hashes_are_stable_shape(self):
        self.assertRegex(self.report["normalized_records_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(self.report["record_sha256"]), 51)
        self.assertTrue(
            all(len(value) == 64 for value in self.report["input_files"].values())
        )


if __name__ == "__main__":
    unittest.main()
