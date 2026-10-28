import json
import tempfile
import unittest
from pathlib import Path

from temporal_official_check import (
    DEFAULT_OFFICIAL_DIR,
    DEFAULT_QUERIES_DIR,
    DEFAULT_QUEUE,
    load_official_batches,
    load_queue,
)


def _record(camis: str) -> dict[str, str]:
    return {
        "camis": camis,
        "dba": "TEST PLACE",
        "boro": "1",
        "building": "1",
        "street": "TEST STREET",
        "zipcode": "10001",
        "latitude": "40.750000",
        "longitude": "-73.990000",
        "record_date": "2026-10-26",
    }


def _write_batch(root: Path, name: str, query_ids: list[str], response_ids: list[str]) -> None:
    official_dir = root / "official"
    queries_dir = root / "queries"
    official_dir.mkdir(parents=True, exist_ok=True)
    queries_dir.mkdir(parents=True, exist_ok=True)
    (official_dir / f"{name}.json").write_text(
        json.dumps([_record(camis) for camis in response_ids]),
        encoding="utf-8",
    )
    quoted_ids = ",".join(f'"{camis}"' for camis in query_ids)
    (queries_dir / f"{name}.where").write_text(
        f"camis in ({quoted_ids})",
        encoding="utf-8",
    )


class TemporalOfficialIntegrityTest(unittest.TestCase):
    def test_frozen_real_capture_passes_query_and_batch_checks(self):
        queue_rows, _ = load_queue(DEFAULT_QUEUE)
        expected_camis = {row["camis"] for row in queue_rows}
        by_camis, batches, stats = load_official_batches(
            DEFAULT_OFFICIAL_DIR,
            DEFAULT_QUERIES_DIR,
            expected_camis=expected_camis,
        )

        self.assertEqual(len(expected_camis), 790)
        self.assertEqual(sum(item["query_camis"] for item in batches), 790)
        self.assertEqual(stats["total_records"], 791)
        self.assertEqual(stats["unique_camis"], 777)
        self.assertEqual(len(by_camis), 777)
        self.assertTrue(stats["query_union_matches_expected"])
        self.assertTrue(stats["response_camis_within_batch_queries"])

    def test_missing_query_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            official_dir = root / "official"
            queries_dir = root / "queries"
            official_dir.mkdir()
            queries_dir.mkdir()
            (official_dir / "batch-01.json").write_text(
                json.dumps([_record("1")]), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "missing query files"):
                load_official_batches(official_dir, queries_dir, expected_camis={"1"})

    def test_duplicate_and_union_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_batch(root, "batch-01", ["1", "1"], ["1"])
            with self.assertRaisesRegex(ValueError, "duplicate CAMIS"):
                load_official_batches(
                    root / "official", root / "queries", expected_camis={"1"}
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_batch(root, "batch-01", ["2"], ["2"])
            with self.assertRaisesRegex(ValueError, "union does not equal frozen queue"):
                load_official_batches(
                    root / "official", root / "queries", expected_camis={"1"}
                )

    def test_response_from_another_batch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_batch(root, "batch-01", ["1"], ["2"])
            _write_batch(root, "batch-02", ["2"], ["2"])

            with self.assertRaisesRegex(ValueError, "outside its batch query"):
                load_official_batches(
                    root / "official",
                    root / "queries",
                    expected_camis={"1", "2"},
                )


if __name__ == "__main__":
    unittest.main()
