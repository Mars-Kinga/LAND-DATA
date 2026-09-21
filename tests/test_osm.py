import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from land_data.auth import MockSessionStore
from land_data.config import load_config
from land_data.cli import main
from land_data.export import dataset
from land_data.geoscope import GeoScope, load_scope
from land_data.models import FieldProvenance, POI, POICandidate, RawSourceRecord, SourceContent
from land_data.osm import OSMSourceAdapter, category_for, parse_element
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]
SCOPE = GeoScope("test-scope", "JP", (35.68, 139.76, 35.681, 139.761),
                 "CUSTOM", "Tokyo")
OBJECTS = [
    {"type": "node", "id": 101, "lat": 35.6802, "lon": 139.7602,
     "tags": {"name": "Test Cafe", "amenity": "cafe", "website": "https://example.invalid/"}},
    {"type": "way", "id": 102, "center": {"lat": 35.6805, "lon": 139.7605},
     "tags": {"name": "Test Museum", "tourism": "museum"}},
]


class Response:
    def __init__(self, elements):
        self.payload = json.dumps({"elements": elements}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, _size):
        return self.payload


class OSMTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = SQLiteStorage(str(Path(temporary.name) / "osm.db"))
        self.addCleanup(self.storage.close)
        self.config = load_config(str(ROOT))
        self.pipeline = Pipeline(self.storage, self.config)

    def adapter(self, elements=OBJECTS, opener=None):
        return OSMSourceAdapter(SCOPE, ("cafe", "museum"), 20, 5,
                                opener or (lambda _request, timeout: Response(elements)))

    def test_response_parsing_query_is_bounded_and_identity_stable(self):
        adapter = self.adapter()
        self.assertIn("out center 20;", adapter.query())
        self.assertIn("(35.68,139.76,35.681,139.761)", adapter.query())
        records = list(adapter.collect(SCOPE.name))
        self.assertEqual([item.external_id for item in records], ["node/101", "way/102"])
        self.assertEqual(records[0].source_url, "https://www.openstreetmap.org/node/101")
        self.assertEqual(records[1].payload["locations"][0]["latitude"], 35.6805)
        self.assertEqual(records[0].payload["osm_object_id"], 101)
        self.assertEqual(records[0].external_id, list(adapter.collect(SCOPE.name))[0].external_id)

    def test_tag_mapping_does_not_guess_unknown(self):
        self.assertEqual(category_for({"amenity": "restaurant"}), "restaurant")
        self.assertEqual(category_for({"amenity": "cafe"}), "cafe")
        self.assertEqual(category_for({"tourism": "museum"}), "museum")
        self.assertIsNone(category_for({"amenity": "bank"}))
        unknown = parse_element({"type": "node", "id": 999, "lat": 35.68, "lon": 139.76,
                                 "tags": {"name": "Bank", "amenity": "bank"}}, SCOPE)
        self.assertEqual(unknown.payload["locations"][0]["category"], "unknown")
        self.assertEqual(unknown.payload["locations"][0]["osm_tags"],
                         {"name": "Bank", "amenity": "bank"})

    def test_malformed_object_and_response(self):
        for value in ({"type": "node", "id": "1"}, {"type": "area", "id": 1},
                      {"type": "node", "id": -1}, {"type": "node", "id": 1, "tags": []}):
            with self.assertRaises(ValueError):
                parse_element(value, SCOPE)
        with self.assertRaisesRegex(ValueError, "missing elements"):
            list(self.adapter(opener=lambda _request, timeout: Response(None)).collect(SCOPE.name))

    def test_missing_name_coordinates_unknown_category_need_review(self):
        elements = [
            {"type": "node", "id": 201, "lat": 35.68, "lon": 139.76,
             "tags": {"amenity": "cafe"}},
            {"type": "way", "id": 202, "tags": {"name": "No coordinate", "amenity": "cafe"}},
            {"type": "node", "id": 203, "lat": 35.68, "lon": 139.76,
             "tags": {"name": "Unsupported", "shop": "gift"}},
        ]
        self.pipeline.run(SCOPE.name, [self.adapter(elements)], SCOPE)
        self.assertEqual(len(self.storage.all(POI)), 0)
        reasons = {item.name: item.review_reasons for item in self.storage.all(POICandidate)}
        self.assertEqual(reasons[""], ["MISSING_REQUIRED_FIELD"])
        self.assertEqual(reasons["No coordinate"], ["MISSING_REQUIRED_FIELD"])
        self.assertEqual(reasons["Unsupported"], ["UNKNOWN_CATEGORY"])
        self.assertEqual(len(self.storage.all(RawSourceRecord)), 3)
        self.assertEqual(len(self.storage.all(SourceContent)), 3)

    def test_duplicate_collection_all_layers_and_provenance(self):
        first = self.pipeline.run(SCOPE.name, [self.adapter()], SCOPE)
        first_ids = {model: {item.id for item in self.storage.all(model)}
                     for model in (RawSourceRecord, SourceContent, POICandidate, POI)}
        second = self.pipeline.run(SCOPE.name, [self.adapter()], SCOPE)
        self.assertEqual(first, second)
        self.assertEqual(first_ids, {model: {item.id for item in self.storage.all(model)}
                                     for model in first_ids})
        self.assertEqual((first["RawSourceRecord"], first["SourceContent"],
                          first["POICandidate"], first["POI"]), (2, 2, 2, 2))
        self.assertTrue(all(item.map_visibility for item in self.storage.all(POI)))
        cafe = next(item for item in self.storage.all(POI) if item.category == "cafe")
        self.assertEqual(cafe.website, "https://example.invalid/")
        fields = {item.field_name: item for item in self.storage.all(FieldProvenance)
                  if item.entity_type == "POI" and item.entity_id == cafe.id}
        self.assertEqual(fields["names"].source_field, "tags.name")
        self.assertEqual(fields["category"].source_field, "tags.amenity")
        self.assertEqual(fields["website"].source_field, "tags.website")
        self.assertEqual(fields["latitude"].source_field, "lat")
        self.assertEqual(fields["longitude"].source_field, "lon")
        self.assertTrue(all(item.source == "osm" and item.source_content_id for item in fields.values()))
        self.assertEqual(dataset(self.storage, self.config, "Tokyo")["source_attributions"][0]["license"],
                         "ODbL 1.0")

    def test_cli_parse_validate_export_with_geo_scope(self):
        self.pipeline.collect(SCOPE.name, [self.adapter()], SCOPE)
        path = Path(self.storage.connection.execute("PRAGMA database_list").fetchone()[2])
        scope_path = path.parent / "scope.json"
        scope_path.write_text(json.dumps({"name": SCOPE.name, "country": SCOPE.country,
                                          "city": SCOPE.city, "bbox": list(SCOPE.bbox)}))
        target = path.parent / "export.json"

        def command(*args):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(path), *args]), 0)
            return json.loads(output.getvalue())

        self.assertEqual(command("parse", "--scope", str(scope_path))["parsed"], 2)
        self.assertEqual(command("validate")["POI"], 2)
        self.assertEqual(command("export", "--scope", str(scope_path), "--output", str(target))["pois"], 2)
        self.assertEqual(json.loads(target.read_text())["city"], "Tokyo")
        self.assertEqual(command("review", "list", "--city", "Tokyo")["count"], 0)

    def test_response_limit_enforced_even_if_server_over_returns(self):
        elements = [dict(OBJECTS[0], id=index + 1) for index in range(21)]
        with self.assertRaisesRegex(ValueError, "exceeds result limit"):
            list(self.adapter(elements).collect(SCOPE.name))

    def test_request_has_user_agent_and_timeout(self):
        captured = []

        def open_request(request, timeout):
            captured.append((request.get_header("User-agent"), timeout,
                             request.data.decode("utf-8")))
            return Response([])

        self.assertEqual(list(self.adapter(opener=open_request).collect(SCOPE.name)), [])
        agent, timeout, form = captured[0]
        self.assertIn("LAND-Data/2B", agent)
        self.assertEqual(timeout, 8)
        self.assertIn("data=", form)

    def test_changed_raw_snapshot_keeps_one_source_identity(self):
        self.pipeline.run(SCOPE.name, [self.adapter([OBJECTS[0]])], SCOPE)
        changed = dict(OBJECTS[0], tags={**OBJECTS[0]["tags"], "website": "https://new.invalid/"})
        self.pipeline.run(SCOPE.name, [self.adapter([changed])], SCOPE)
        self.assertEqual(len(self.storage.all(RawSourceRecord)), 2)
        self.assertEqual(len(self.storage.all(SourceContent)), 1)
        self.assertEqual(len(self.storage.all(POICandidate)), 1)
        self.assertEqual(len(self.storage.all(POI)), 1)
        content = self.storage.all(SourceContent)[0]
        raw = self.storage.get(RawSourceRecord, content.raw_record_id)
        self.assertEqual(raw.payload["locations"][0]["website"], "https://new.invalid/")

    def test_geo_scope_validation_and_independent_city(self):
        self.assertEqual(load_scope(str(ROOT / "config/osm-acceptance-scope.json")).city, "Tokyo")
        for bbox in ((35.7, 139.0, 35.6, 139.1), (0, 0, 91, 1), (0, 0, 0, 1)):
            with self.assertRaises(ValueError):
                GeoScope("bad", "JP", bbox)
        with self.assertRaises(ValueError):
            GeoScope("bad", "JP", SCOPE.bbox, center=(35.68, 139.76))
        with self.assertRaises(ValueError):
            OSMSourceAdapter(GeoScope("big", "JP", (35.6, 139.7, 35.7, 139.8)))
        with self.assertRaises(ValueError):
            OSMSourceAdapter(SCOPE, limit=31)
        self.pipeline.run(SCOPE.name, [self.adapter()], SCOPE)
        self.assertEqual(self.storage.all(RawSourceRecord)[0].city, SCOPE.name)
        self.assertEqual(self.storage.all(POI)[0].city, "Tokyo")

    @patch("land_data.osm.time.sleep")
    def test_network_error_retries_once_with_clear_error(self, sleep):
        attempts = []

        def fail(_request, timeout):
            attempts.append(timeout)
            raise URLError("offline")

        with self.assertRaisesRegex(ValueError, "Overpass network/timeout error"):
            list(self.adapter(opener=fail).collect(SCOPE.name))
        self.assertEqual(attempts, [8, 8])
        sleep.assert_called_once_with(1)

    @patch("land_data.osm.time.sleep")
    def test_timeout_behavior_and_http_error(self, sleep):
        calls = []

        def timeout(_request, **kwargs):
            calls.append(kwargs["timeout"])
            raise TimeoutError("timed out")

        with self.assertRaisesRegex(ValueError, "timeout"):
            list(self.adapter(opener=timeout).collect(SCOPE.name))
        self.assertEqual(calls, [8, 8])
        self.assertEqual(sleep.call_count, 1)
        def bad_request(_request, **_kwargs):
            raise HTTPError("https://overpass-api.de/api/interpreter", 400, "bad", {}, None)
        with self.assertRaisesRegex(ValueError, "Overpass HTTP 400"):
            list(self.adapter(opener=bad_request).collect(SCOPE.name))
        self.assertEqual(sleep.call_count, 1)

    @patch("land_data.osm.time.sleep")
    def test_read_timeout_oserror_retries_once(self, sleep):
        attempts = []

        def fail_read(_request, **_kwargs):
            attempts.append(1)
            raise OSError("The read operation timed out")

        with self.assertRaisesRegex(ValueError, "Overpass network/timeout error"):
            list(self.adapter(opener=fail_read).collect(SCOPE.name))
        self.assertEqual(len(attempts), 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
