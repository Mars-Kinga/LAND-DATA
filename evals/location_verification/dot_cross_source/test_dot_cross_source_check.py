import json
import tempfile
import unittest
from pathlib import Path

from dot_cross_source_check import (
    build_current_index,
    current_summary,
    build_historical_index,
    compare_current_case,
    compare_historical_case,
    load_capture_manifest,
    normalize_address,
    source_address,
    source_name,
)


class DotCrossSourceCheckTests(unittest.TestCase):
    def test_address_normalization_is_conservative_and_deterministic(self):
        self.assertEqual(normalize_address("9 DOYERS STREET"), normalize_address("9 Doyers St."))
        self.assertNotEqual(normalize_address("9 DOYERS STREET"), normalize_address("19 DOYERS STREET"))

    def test_current_snapshot_aliases_and_ambiguous_key_are_preserved(self):
        records = [
            {
                "assumed_name_s": "Example Cafe",
                "street": "9 DOYERS STREET",
                "latitude": "40.7144",
                "longitude": "-73.9981",
                "license_status": "Issued",
            },
            {
                "assumed_name_s": "Example Cafe",
                "street": "9 DOYERS STREET",
                "latitude": "40.7145",
                "longitude": "-73.9982",
                "license_status": "Issued",
            },
        ]
        self.assertEqual(source_name(records[0]), "Example Cafe")
        self.assertEqual(source_address(records[0]), "9 DOYERS STREET")
        index, stats = build_current_index(records)
        self.assertEqual(stats["ambiguous_name_address_keys"], 1)
        case = compare_current_case(
            {
                "screening_id": "screen-1",
                "candidate_id": "candidate-1",
                "name": "Example Cafe",
                "address": "9 DOYERS ST",
                "city": "New York",
                "latitude": 40.7144,
                "longitude": -73.9981,
                "platform": "nyc-open-data-restaurants",
                "external_id": "123",
            },
            index,
        )
        self.assertEqual(case["match_type"], "ambiguous_name_address_match")
        self.assertIsNone(case["dot_row"])

    def test_historical_duplicate_conflict_is_not_silently_resolved(self):
        records = [
            {
                "restaurant_name": "Example Cafe",
                "business_address": "9 DOYERS STREET, Manhattan, NY",
                "food_service_establishment": "123",
                "latitude": "40.7144",
                "longitude": "-73.9981",
            },
            {
                "restaurant_name": "Example Cafe",
                "business_address": "10 DOYERS STREET, Manhattan, NY",
                "food_service_establishment": "123",
                "latitude": "40.7144",
                "longitude": "-73.9981",
            },
        ]
        index, stats = build_historical_index(records)
        self.assertEqual(stats["duplicate_camis_groups"], 1)
        case = compare_historical_case(
            {
                "screening_id": "screen-1",
                "candidate_id": "candidate-1",
                "name": "Example Cafe",
                "address": "9 DOYERS STREET",
                "city": "New York",
                "latitude": 40.7144,
                "longitude": -73.9981,
                "platform": "nyc-open-data-restaurants",
                "external_id": "123",
            },
            index,
        )
        self.assertEqual(case["match_type"], "duplicate_camis_rows")
        self.assertEqual(case["duplicate_consistency"], "field_missing_or_conflict")
        self.assertFalse(case["address_comparable"])
        self.assertTrue(case["coordinate_comparable"])

    def test_current_coordinate_denominator_separates_missing_zero_sentinel(self):
        records = [
            {
                "assumed_name_s": "Example Cafe",
                "street": "9 DOYERS STREET",
                "latitude": "40.7144",
                "longitude": "-73.9981",
                "license_status": "Issued",
            },
        ]
        index, index_stats = build_current_index(records)
        summary, _ = current_summary(
            [
                {
                    "screening_id": "screen-1",
                    "candidate_id": "candidate-1",
                    "name": "Example Cafe",
                    "address": "9 DOYERS ST",
                    "city": "New York",
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "platform": "nyc-open-data-restaurants",
                    "external_id": "123",
                }
            ],
            records,
            index,
            index_stats,
        )
        metrics = summary["metrics_after_unique_match"]
        self.assertEqual(summary["any_name_address_match"], {"count": 1, "denominator": 1, "rate": 1.0})
        self.assertEqual(metrics["coordinate_comparable_coverage"], {"count": 0, "denominator": 1, "rate": 0.0})
        self.assertEqual(metrics["coordinate_within_10m"]["denominator"], 0)

    def test_capture_manifest_checks_raw_input_hashes_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current.json"
            historical = root / "historical.json"
            current.write_text(json.dumps([{"id": 1}]), encoding="utf-8")
            historical.write_text(json.dumps([{"id": 2}, {"id": 3}]), encoding="utf-8")
            manifest = root / "capture-manifest.json"

            from dot_cross_source_check import sha256_file

            payload = {
                "schema_version": "test",
                "generated_at": "2026-10-28T00:00:00Z",
                "datasets": {
                    "current": {
                        "dataset_id": "current-id",
                        "rows": 1,
                        "files": {"current.json": {"sha256": sha256_file(current)}},
                    },
                    "historical": {
                        "dataset_id": "historical-id",
                        "rows": 2,
                        "files": {"historical.json": {"sha256": sha256_file(historical)}},
                    },
                },
            }
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            current_info = {"sha256": sha256_file(current), "records": 1}
            historical_info = {"sha256": sha256_file(historical), "records": 2}
            result = load_capture_manifest(manifest, current_info, historical_info)
            self.assertEqual(result["datasets"]["current"]["dataset_id"], "current-id")
            self.assertEqual(result["datasets"]["historical"]["rows"], 2)


if __name__ == "__main__":
    unittest.main()
