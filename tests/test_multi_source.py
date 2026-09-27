import tempfile
import unittest
from pathlib import Path

from land_data.adapters import AdapterCapability, SourceItem
from land_data.config import load_config
from land_data.models import RawSourceRecord, SourceRunRecord
from land_data.multi_source import MultiSourceRunner, MultiSourceSpec
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class FakeAdapter:
    capability = AdapterCapability("PUBLIC")

    def __init__(self, name, external_ids, fail=False):
        self.name = name
        self.external_ids = external_ids
        self.fail = fail

    def collect(self, city):
        if self.fail:
            raise ValueError("simulated source failure")
        for external_id in self.external_ids:
            yield SourceItem(self.name, external_id, "https://example.invalid/" + external_id,
                             {"content_type": "PLACE", "title": external_id, "text": "",
                              "source_kind": "structured", "locations": [{
                                  "name": external_id, "city": "Tokyo", "country": "JP",
                                  "category": "cafe", "latitude": 35.68, "longitude": 139.76,
                                  "coordinate_verified": True
                              }]})


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = SQLiteStorage(str(Path(self.temp.name) / "test.db"))
        self.addCleanup(self.storage.close)
        self.config = load_config(str(ROOT))

    def test_sources_are_isolated_and_replay_is_duplicate_only(self):
        failing = {"value": True}

        def factory(spec):
            if spec.name == "osm" and failing["value"]:
                return FakeAdapter("osm", [], fail=True)
            return FakeAdapter(spec.name, ["one", "two"])

        specs = [MultiSourceSpec("mock-places", "mock-tokyo"),
                 MultiSourceSpec("osm", "mock-tokyo")]
        runner = MultiSourceRunner(Pipeline(self.storage, self.config), self.storage, specs, factory)
        first = runner.run()
        self.assertEqual(first["status"], "PARTIAL_FAILURE")
        self.assertEqual(first["sources"][0]["status"], "SUCCEEDED")
        self.assertEqual(first["sources"][1]["next_run_status"], "RETRY_REQUIRED")
        self.assertEqual(len(self.storage.all(RawSourceRecord)), 2)

        failing["value"] = False
        second = runner.run()
        self.assertEqual(second["status"], "SUCCEEDED")
        osm = second["sources"][1]
        self.assertEqual((osm["new_count"], osm["duplicate_count"]), (2, 0))

        third = runner.run()
        for outcome in third["sources"]:
            self.assertEqual((outcome["new_count"], outcome["duplicate_count"]), (0, 2))
        self.assertEqual(len(self.storage.all(RawSourceRecord)), 4)
        self.assertEqual(len(self.storage.all(SourceRunRecord)), 6)


if __name__ == "__main__":
    unittest.main()
