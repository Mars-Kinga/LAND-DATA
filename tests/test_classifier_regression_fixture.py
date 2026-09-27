import json
import unittest
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "classifier_regression.jsonl"
ALLOWED_LABELS = {
    "structured_poi",
    "known_poi",
    "knowledge",
    "event",
    "mixed",
    "unresolved_place",
    "ambiguous",
    "irrelevant",
}
REQUIRED_FIELDS = {
    "id",
    "title",
    "text",
    "city",
    "source",
    "gold_primary_label",
    "gold_route",
    "expected_model_recommended",
    "notes",
}


class ClassifierRegressionFixtureContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = []
        with FIXTURE.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AssertionError(f"invalid JSON on line {line_number}: {exc}")
                row["_line_number"] = line_number
                cls.rows.append(row)

    def test_fixture_is_large_enough_for_regression_coverage(self):
        self.assertGreaterEqual(len(self.rows), 30)

    def test_each_row_has_stable_schema_and_unique_id(self):
        ids = []
        for row in self.rows:
            line = row["_line_number"]
            self.assertTrue(REQUIRED_FIELDS.issubset(row), f"missing field on line {line}")
            self.assertIsInstance(row["id"], str)
            self.assertTrue(row["id"].strip(), f"empty id on line {line}")
            ids.append(row["id"])
            self.assertIsInstance(row["title"], str)
            self.assertIsInstance(row["text"], str)
            self.assertIsInstance(row["city"], str)
            self.assertTrue(row["city"].strip(), f"empty city on line {line}")
            self.assertIsInstance(row["source"], str)
            self.assertTrue(row["source"].strip(), f"empty source on line {line}")
            self.assertIsInstance(row["notes"], str)
            self.assertTrue(row["notes"].strip(), f"empty notes on line {line}")
            self.assertIn(row["gold_primary_label"], ALLOWED_LABELS, line)
            self.assertIn(row["gold_route"], ALLOWED_LABELS, line)
            self.assertIs(row["expected_model_recommended"],
                          bool(row["expected_model_recommended"]), line)
        self.assertEqual(len(ids), len(set(ids)))

    def test_optional_classifier_inputs_have_expected_types(self):
        for row in self.rows:
            if "content_type" in row:
                self.assertIsInstance(row["content_type"], str, row["id"])
            if "geo_tag" in row:
                self.assertIsInstance(row["geo_tag"], dict, row["id"])
            if "payload" in row:
                self.assertIsInstance(row["payload"], dict, row["id"])

    def test_fixture_declares_core_content_diversity(self):
        labels = {row["gold_primary_label"] for row in self.rows}
        sources = {row["source"] for row in self.rows}
        cities = {row["city"] for row in self.rows}
        self.assertTrue({"knowledge", "event", "mixed", "unresolved_place"} <= labels)
        self.assertTrue({"irrelevant", "ambiguous", "structured_poi"} <= labels)
        self.assertGreaterEqual(len(sources), 3)
        self.assertGreaterEqual(len(cities), 4)


if __name__ == "__main__":
    unittest.main()
