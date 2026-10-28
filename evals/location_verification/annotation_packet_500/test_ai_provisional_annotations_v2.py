import unittest
from unittest.mock import patch

from build_ai_provisional_annotations_v2 import enrich_daily


def record(group, raw):
    return {"source_group": group, "snapshot_sha256": "snapshot", "row_locator": 1,
            "row_sha256": "row", "raw_record": raw}


class ProvisionalV2Tests(unittest.TestCase):
    @patch("build_ai_provisional_annotations_v2.sha", return_value="dossier-hash")
    def test_current_dot_agreement_is_provisional_not_gold(self, _sha):
        label = {"claim_reviews": {field: {"status": "UNKNOWN"} for field in
                                   ("identity", "address", "coordinates", "operational_status")},
                 "gold_label": None, "eligible_for_accuracy": False,
                 "next_action": "COLLECT_CURRENT_INDEPENDENT_EVIDENCE"}
        dossier = {"candidate_claims": {"latitude": 40.0, "longitude": -73.0},
                   "source_records": [record("DOHMH_INTAKE", {}),
                                      record("DOT_CURRENT", {"latitude": 40.0, "longitude": -73.0})]}
        result = enrich_daily(label, dossier)
        self.assertEqual(result["claim_reviews"]["coordinates"]["status"], "CROSS_SOURCE_AGREEMENT")
        self.assertEqual(result["claim_reviews"]["identity"]["status"], "PLAUSIBLE_MATCH_NOT_CONFIRMED")
        self.assertEqual(result["claim_reviews"]["operational_status"]["status"], "UNKNOWN")
        self.assertIsNone(result["gold_label"])
        self.assertFalse(result["eligible_for_accuracy"])

    @patch("build_ai_provisional_annotations_v2.sha", return_value="dossier-hash")
    def test_zero_zero_stays_unknown(self, _sha):
        label = {"claim_reviews": {field: {"status": "UNKNOWN"} for field in
                                   ("identity", "address", "coordinates", "operational_status")},
                 "next_action": "REPAIR_INPUT_AND_COLLECT_EVIDENCE"}
        dossier = {"candidate_claims": {"latitude": 0.0, "longitude": 0.0},
                   "source_records": [record("DOHMH_INTAKE", {}),
                                      record("DOT_CURRENT", {"latitude": 40.0, "longitude": -73.0})]}
        result = enrich_daily(label, dossier)
        self.assertEqual(result["claim_reviews"]["coordinates"]["status"], "UNKNOWN")
        self.assertEqual(result["next_action"], "REPAIR_INPUT_AND_COLLECT_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
