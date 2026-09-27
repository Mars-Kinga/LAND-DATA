import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from land_data.config import load_config
from land_data.models import POI, POICandidate, RawSourceRecord, SourceContent
from land_data.multi_source import MultiSourceRunner, MultiSourceSpec
from land_data.nyc_open_data import NYCOpenDataRestaurantAdapter
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, _): return json.dumps(self.value).encode()


def snapshot(day):
    return [{"latest_record_date": day + "T06:00:17.000"}]


def row(camis, day, *, cuisine="Italian"):
    return {"camis": camis, "dba": "Restaurant " + camis, "boro": "Manhattan",
            "building": "10", "street": "Broadway", "zipcode": "10001",
            "cuisine_description": cuisine, "inspection_date": day + "T00:00:00.000",
            "snapshot_record_date": day + "T06:00:12.000", "latitude": "40.7", "longitude": "-74.0"}


class NYCOpenDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = SQLiteStorage(str(Path(self.temp.name) / "test.db"))
        self.addCleanup(self.storage.close)
        self.pipeline = Pipeline(self.storage, load_config(str(ROOT)))

    def adapter(self, opener, **kwargs):
        return NYCOpenDataRestaurantAdapter(limit=kwargs.pop("limit", 2),
                                            max_pages=kwargs.pop("max_pages", 3), storage=self.storage,
                                            initial_updated_at="2026-09-01T00:00:00.000",
                                            opener=opener, **kwargs)

    def scripted(self, responses):
        """Route the three bounded grouped queries for one logical page."""
        last_rows = []
        def opener(request, timeout):
            nonlocal last_rows
            query = parse_qs(urlsplit(request.full_url).query)
            select = query.get("$select", [""])[0]
            if select.startswith("max(record_date)"):
                value = responses.pop(0)
                return Response(value)
            if "count(distinct" in select:
                values = []
                for current in last_rows:
                    values.append({"camis": current["camis"], **{
                        alias: str(2 if field in current.get("conflict_fields", []) else 1)
                        for field, alias in (("dba", "dba_values"), ("boro", "boro_values"),
                            ("building", "building_values"), ("street", "street_values"),
                            ("zipcode", "zipcode_values"), ("cuisine_description", "cuisine_values"),
                            ("latitude", "latitude_values"), ("longitude", "longitude_values"))}})
                return Response(values)
            if select == "camis,max(latitude) as latitude,max(longitude) as longitude":
                return Response([{"camis": current["camis"], "latitude": current.get("latitude", "40.7"),
                                  "longitude": current.get("longitude", "-74.0")} for current in last_rows])
            last_rows = responses.pop(0)
            return Response(last_rows)
        return opener

    def test_cross_page_duplicate_camis_is_one_grouped_restaurant_snapshot(self):
        responses = [snapshot("2026-09-21"),
                     [row("1", "2026-09-21", cuisine="B"), row("2", "2026-09-21", cuisine="C")],
                     [row("2", "2026-09-21", cuisine="C"), row("3", "2026-09-21", cuisine="A")], []]
        requests = []
        scripted = self.scripted(responses)

        def opener(request, timeout):
            requests.append(request.full_url)
            return scripted(request, timeout)

        self.pipeline.run("new-york", [self.adapter(opener)])
        self.assertEqual({item.external_id for item in self.storage.all(SourceContent)}, {"1", "2", "3"})
        self.assertEqual(len(self.storage.all(RawSourceRecord)), 3)
        first_page = parse_qs(urlsplit(requests[1]).query)
        self.assertEqual(first_page["$group"], ["camis"])
        self.assertNotIn("grade=", first_page["$where"][0])
        self.assertIn("camis > '2'", parse_qs(urlsplit(requests[11]).query)["$where"][0])

    def test_batch_reports_continue_required_and_request_breakdown(self):
        responses = [snapshot("2026-09-21"), [row("1", "2026-09-21")]]
        runner = MultiSourceRunner(
            self.pipeline, self.storage,
            [MultiSourceSpec("nyc-open-data-restaurants", "new-york", limit=1, max_pages=1)],
            lambda _spec: self.adapter(self.scripted(responses), limit=1, max_pages=1))
        outcome = runner.run()["sources"][0]
        self.assertEqual(outcome["status"], "CONTINUE_REQUIRED")
        self.assertEqual(outcome["next_run_status"], "CONTINUE_REQUIRED")
        self.assertEqual(outcome["collection"]["request_breakdown"], {
            "snapshot_anchor": 1, "camis_page": 1, "coordinate": 1, "conflict_check": 8})

    def test_new_snapshot_waits_until_current_snapshot_cycle_finishes(self):
        first_urls = []
        first_responses = [snapshot("2026-09-21"), [row("1", "2026-09-21")],
                           [row("2", "2026-09-21")], []]
        first_scripted = self.scripted(first_responses)

        def first_opener(request, timeout):
            first_urls.append(request.full_url)
            return first_scripted(request, timeout)

        self.pipeline.run("new-york", [self.adapter(first_opener, limit=1)])
        first_queries = [parse_qs(urlsplit(url).query) for url in first_urls]
        self.assertEqual(sum(query["$select"][0].startswith("max(record_date)") for query in first_queries), 1)
        self.assertTrue(all("2026-09-21T00:00:00.000" in query.get("$where", [""])[0]
                            for query in first_queries[1:]))

        next_responses = [snapshot("2026-09-22"), [row("1", "2026-09-22", cuisine="Korean")]]
        next_scripted = self.scripted(next_responses)
        def next_opener(request, timeout):
            return next_scripted(request, timeout)

        runner = MultiSourceRunner(self.pipeline, self.storage,
                                   [MultiSourceSpec("nyc-open-data-restaurants", "new-york", limit=2)],
                                   lambda _spec: self.adapter(next_opener, limit=2))
        outcome = runner.run()["sources"][0]
        self.assertEqual((outcome["new_count"], outcome["updated_count"]), (0, 1))
        self.assertEqual(len(self.storage.all(RawSourceRecord)), 3)

    def test_completed_cycle_rescans_same_snapshot_and_counts_unchanged(self):
        first = [snapshot("2026-09-21"), [row("1", "2026-09-21")]]
        second = [snapshot("2026-09-21"), [row("1", "2026-09-21")]]
        calls = [first, second]

        def factory(_spec):
            responses = calls.pop(0)
            return self.adapter(self.scripted(responses), limit=2)

        runner = MultiSourceRunner(self.pipeline, self.storage,
                                   [MultiSourceSpec("nyc-open-data-restaurants", "new-york", limit=2)], factory)
        first = runner.run()["sources"][0]
        self.assertEqual(first["new_count"], 1)
        self.assertEqual(first["status"], "COMPLETE")
        replay = runner.run()["sources"][0]
        self.assertEqual((replay["new_count"], replay["updated_count"], replay["duplicate_count"]), (0, 0, 0))
        self.assertEqual(replay["status"], "NOT_NEEDED")

    def test_interrupted_page_resumes_from_persisted_camis(self):
        responses = [snapshot("2026-09-21"), [row("1", "2026-09-21"), row("2", "2026-09-21")],
                     OSError("temporary outage")]
        last_rows = []
        def failing(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            if query.get("$select", [""])[0].startswith("max(record_date)"):
                value = responses.pop(0)
            elif "count(distinct" in query.get("$select", [""])[0]:
                return Response([{"camis": current["camis"], "dba_values": "1", "boro_values": "1",
                                  "building_values": "1", "street_values": "1", "zipcode_values": "1",
                                  "cuisine_values": "1", "latitude_values": "1", "longitude_values": "1"}
                                 for current in last_rows])
            elif query.get("$select", [""])[0] == "camis,max(latitude) as latitude,max(longitude) as longitude":
                return Response([{"camis": current["camis"], "latitude": current["latitude"],
                                  "longitude": current["longitude"]} for current in last_rows])
            else:
                value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            if not query.get("$select", [""])[0].startswith("max(record_date)"):
                last_rows[:] = value
            return Response(value)

        with self.assertRaisesRegex(ValueError, "network error"):
            self.pipeline.run("new-york", [self.adapter(failing)])
        resumed_urls = []
        resumed_scripted = self.scripted([snapshot("2026-09-21"), [row("3", "2026-09-21")]])
        def resumed(request, timeout):
            resumed_urls.append(request.full_url)
            return resumed_scripted(request, timeout)

        self.pipeline.run("new-york", [self.adapter(resumed)])
        self.assertEqual({item.external_id for item in self.storage.all(SourceContent)}, {"1", "2", "3"})
        self.assertIn("camis > '2'", parse_qs(urlsplit(resumed_urls[1]).query)["$where"][0])

    def test_independent_max_fields_within_camis_are_held_for_review(self):
        conflicting = row("1", "2026-09-21")
        conflicting["min_dba"] = "Another Restaurant"
        conflicting["min_boro"] = "Manhattan"
        conflicting["min_building"] = "10"
        conflicting["min_street"] = "Broadway"
        conflicting["min_zipcode"] = "10001"
        conflicting["min_cuisine_description"] = "Italian"
        conflicting["min_latitude"] = "40.7"
        conflicting["min_longitude"] = "-74.0"
        conflicting["conflict_fields"] = ["dba"]
        responses = [snapshot("2026-09-21"), [conflicting]]
        self.pipeline.run("new-york", [self.adapter(self.scripted(responses))])
        candidate = self.storage.all(POICandidate)[0]
        self.assertEqual(candidate.review_status, "NEEDS_REVIEW")
        self.assertEqual(candidate.review_reasons, ["NYC_AGGREGATE_FIELD_CONFLICT"])
        self.assertEqual(self.storage.all(POI), [])


if __name__ == "__main__":
    unittest.main()
