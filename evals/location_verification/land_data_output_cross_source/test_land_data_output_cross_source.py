import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from land_data_output_cross_source import (
    FREEZE_SCHEMA_VERSION,
    build_current_index,
    build_historical_index,
    current_diagnostics,
    historical_diagnostics,
    load_and_validate_freeze_manifest,
    open_immutable_database,
)


def _output(
    candidate_id: str,
    status: str,
    external_id: str = "123",
    latitude: float = 40.0,
    longitude: float = -73.0,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "source_content_id": f"source-{candidate_id}",
        "resolved_poi_id": f"poi-{candidate_id}" if status == "AUTO_APPROVED" else None,
        "candidate_city": "New York",
        "platform": "nyc-open-data-restaurants",
        "external_id": external_id,
        "name": "Example Cafe",
        "address": "9 DOYERS STREET",
        "latitude": latitude,
        "longitude": longitude,
        "candidate_review_status": status,
        "candidate_confidence": 1.0,
        "source_collected_at": "2026-10-18T00:00:00Z",
        "source_record_date": "2026-10-16",
        "poi_id": f"poi-{candidate_id}" if status == "AUTO_APPROVED" else None,
        "poi_review_status": status if status == "AUTO_APPROVED" else "MISSING",
        "poi_active": True if status == "AUTO_APPROVED" else None,
    }


class LandDataOutputCrossSourceTests(unittest.TestCase):
    def test_current_conflicts_are_grouped_by_legacy_status(self):
        outputs = [
            _output("auto", "AUTO_APPROVED"),
            _output("review", "NEEDS_REVIEW"),
        ]
        # The DOT row is roughly 111 metres north of the candidate; both
        # candidates share the normalized name/address key.
        current_records = [
            {
                "assumed_name_s": "Example Cafe",
                "street": "9 DOYERS STREET",
                "latitude": "40.001",
                "longitude": "-73.0",
                "license_status": "Issued",
            }
        ]
        current_index, index_stats = build_current_index(current_records)
        summary, _ = current_diagnostics(outputs, current_records, current_index, index_stats)
        self.assertEqual(summary["unique_name_address_match"], {"count": 2, "denominator": 2, "rate": 1.0})
        self.assertEqual(summary["metrics_after_unique_match"]["coordinate_over_10m"]["count"], 2)
        self.assertEqual(summary["metrics_after_unique_match"]["coordinate_over_50m"]["count"], 2)
        self.assertEqual(
            summary["legacy_status_breakdown"]["AUTO_APPROVED"]["coordinate_over_10m"],
            {"count": 1, "denominator": 1, "rate": 1.0},
        )
        self.assertEqual(
            summary["legacy_status_breakdown"]["NEEDS_REVIEW"]["coordinate_over_50m"],
            {"count": 1, "denominator": 1, "rate": 1.0},
        )

    def test_historical_empty_external_id_is_not_cam_is_eligible(self):
        outputs = [_output("with-camis", "AUTO_APPROVED", external_id="123"), _output("without", "NEEDS_REVIEW", external_id="")]
        historical_records = [
            {
                "restaurant_name": "Example Cafe",
                "business_address": "9 DOYERS STREET",
                "food_service_establishment": "123",
                "latitude": "40.0",
                "longitude": "-73.0",
            }
        ]
        historical_index, index_stats = build_historical_index(historical_records)
        summary, cases = historical_diagnostics(outputs, historical_records, historical_index, index_stats)
        self.assertEqual(summary["camis_match"], {"count": 1, "denominator": 1, "rate": 1.0})
        self.assertTrue(cases[0]["camis_join_eligible"])
        self.assertFalse(cases[1]["camis_join_eligible"])

    def test_immutable_database_and_freeze_hash_are_checked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "snapshot.db"
            connection = sqlite3.connect(db_path)
            connection.execute("create table marker(value text)")
            connection.execute("insert into marker values ('ok')")
            connection.commit()
            connection.close()
            db_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
            manifest = root / "manifest.json"
            payload = {
                "schema_version": FREEZE_SCHEMA_VERSION,
                "git_head": "test",
                "databases": [
                    {
                        "snapshot": db_path.name,
                        "bytes": db_path.stat().st_size,
                        "sha256": db_sha,
                        "quick_check": "ok",
                    }
                ],
            }
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            validated = load_and_validate_freeze_manifest(manifest, db_path)
            self.assertEqual(validated["database"]["sha256"], db_sha)
            immutable = open_immutable_database(db_path)
            self.assertEqual(immutable.execute("select value from marker").fetchone()[0], "ok")
            with self.assertRaises(sqlite3.OperationalError):
                immutable.execute("insert into marker values ('write')")
            immutable.close()


if __name__ == "__main__":
    unittest.main()
