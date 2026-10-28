import unittest
from unittest.mock import patch

from build_ai_provisional_annotations import daily_annotation, hard_annotation, validate


def record(group, raw):
    return {"source_group": group, "snapshot_sha256": "snapshot", "row_locator": group,
            "row_sha256": "row-hash-" + group, "raw_record": raw}


class ProvisionalAnnotationTests(unittest.TestCase):
    @patch("build_ai_provisional_annotations.sha", return_value="locked-input")
    def test_cross_borough_conflict_is_not_promoted_to_gold(self, _sha):
        packet = {"case_id": "a", "candidate_claims": {"latitude": 40.0, "longitude": -73.0},
                  "source_records": [record("DOHMH_FROZEN", {"raw_metadata": {"source_metadata": {"borough": "Manhattan"}}}),
                                     record("DOT_CURRENT", {"borough": "Brooklyn", "latitude": 40.01,
                                                            "longitude": -73.0})]}
        label = hard_annotation(packet, "address_coordinate_conflict")
        self.assertEqual(label["claim_reviews"]["identity"]["status"], "CONFLICTING")
        self.assertEqual(label["claim_reviews"]["coordinates"]["status"], "CROSS_SOURCE_CONFLICT")
        self.assertEqual(label["next_action"], "HUMAN_DISAMBIGUATION")
        self.assertIsNone(label["gold_label"])
        self.assertFalse(label["eligible_for_accuracy"])

    @patch("build_ai_provisional_annotations.sha", return_value="locked-input")
    def test_zero_zero_and_one_source_remain_unknown(self, _sha):
        packet = {"case_id": "a", "candidate_claims": {"latitude": 0.0, "longitude": 0.0},
                  "source_records": [record("DOHMH_FROZEN", {"raw_metadata": {}}),
                                     record("DOT_CURRENT", {"latitude": 40.0, "longitude": -73.0})]}
        self.assertEqual(hard_annotation(packet, "address_coordinate_conflict")["claim_reviews"]["coordinates"]["status"],
                         "UNKNOWN")
        daily = {"screening_id": "daily-a", "city": "Hanoi",
                 "candidate": {"name": "", "address": "", "latitude": 0.0, "longitude": 0.0,
                               "source_content_id": "s"},
                 "source": {"platform": "osm", "source_url": "https://example.invalid"}, "evidence": []}
        label = daily_annotation(daily)
        self.assertEqual(label["missing_candidate_fields"], ["name", "address", "coordinates"])
        self.assertTrue(all(item["status"] == "UNKNOWN" for item in label["claim_reviews"].values()))
        with self.assertRaisesRegex(ValueError, "Gold or split"):
            validate([daily], [], [{**label, "gold_label": "KEEP"}])


if __name__ == "__main__":
    unittest.main()
