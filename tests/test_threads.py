"""Offline contract tests for the official Threads transport and pipeline."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from land_data.config import load_config
from land_data.export import dataset
from land_data.models import (FieldProvenance, LocationEvidence, POI, POICandidate,
                              POIMention, RawSourceRecord, RecommendationSignal, SourceContent)
from land_data.pipeline import Pipeline
from land_data.review import ReviewService
from land_data.sqlite_storage import SQLiteStorage
from land_data.threads import ThreadsAPIError, ThreadsAdapter, location_level, parse_post
from land_data.util import utc_now
from land_data.auth import AuthRequired


ROOT = Path(__file__).resolve().parents[1]
POST = {"id": "12345", "permalink": "https://www.threads.net/@example/post/abc",
        "text": "Example Rooftop sunset date", "username": "example",
        "timestamp": "2026-09-20T12:00:00+0000", "media_type": "TEXT_POST",
        "topic_tag": "travel", "location_id": "98765"}
LOCATION = {"id": "98765", "name": "Example Rooftop", "address": "1 Sample St",
            "city": "Mock City", "country": "ZZ", "latitude": 0.0, "longitude": 0.0}


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, _limit):
        return json.dumps(self.data).encode("utf-8")


class FakeAPI:
    def __init__(self, post=None, location=None):
        self.post = POST.copy() if post is None else post
        self.location = LOCATION.copy() if location is None else location
        self.calls = []
        self.error = None

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, timeout, request.get_header("Authorization")))
        if self.error:
            raise self.error
        path = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        if path == "keyword_search":
            return Response({"data": [self.post], "paging": {"cursors": {}}})
        if path == "98765":
            return Response(self.location)
        if path == "12345":
            return Response(self.post)
        if path == "me":
            return Response({"id": "111", "username": "owner"})
        raise AssertionError(path)


class ThreadsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = SQLiteStorage(str(Path(temporary.name) / "threads.db"))
        self.addCleanup(self.storage.close)
        self.config = load_config(str(ROOT))
        self.pipeline = Pipeline(self.storage, self.config)
        self.api = FakeAPI()

    def adapter(self, **kwargs):
        return ThreadsAdapter("secret-token", query="rooftop", opener=self.api,
                              sleeper=lambda _seconds: None, **kwargs)

    def seed_poi(self):
        now = utc_now()
        self.storage.put(POI(id="existing-rooftop", names={"en": "Example Rooftop"},
                             latitude=0.0, longitude=0.0, city="Mock City", country="ZZ",
                             category="restaurant", created_at=now, updated_at=now,
                             confidence_score=0.95, coordinates_verified=True))

    def test_post_parsing_and_identity(self):
        item = parse_post(POST, LOCATION, "RETRIEVED")
        self.assertEqual((item.external_id, item.source_url),
                         ("12345", POST["permalink"]))
        self.assertEqual(item.payload["published_at"], POST["timestamp"])
        self.assertEqual(item.payload["hashtags"], ["travel"])
        self.assertEqual(item.payload["media_metadata"], [{"type": "TEXT_POST"}])
        self.assertEqual(item.payload["geo_tag"]["id"], "98765")

    def test_location_shape_is_conservative(self):
        self.assertEqual(location_level(LOCATION), "POI")
        self.assertEqual(location_level({**LOCATION, "name": "Mock City"}), "AREA")
        self.assertEqual(location_level({"id": "98765", "name": "Rooftop"}), "UNKNOWN")

    def test_search_and_location_request_are_bounded_and_authenticated(self):
        adapter = self.adapter(limit=1, timeout=15)
        items = list(adapter.collect("Mock City"))
        self.assertEqual(len(items), 1)
        self.assertEqual(adapter.stats, {"posts": 1, "with_location_id": 1, "location_details": 1})
        self.assertEqual(len(self.api.calls), 3)
        self.assertEqual(parse_qs(urlsplit(self.api.calls[0][0]).query)["limit"], ["1"])
        self.assertTrue(all(call[1] == 15 and call[2] == "Bearer secret-token" for call in self.api.calls))
        self.assertNotIn("secret-token", str(items))

    def test_missing_location_is_allowed(self):
        self.api.post = {key: value for key, value in POST.items() if key != "location_id"}
        adapter = self.adapter()
        item = list(adapter.collect("Mock City"))[0]
        self.assertEqual(adapter.stats["with_location_id"], 0)
        self.assertNotIn("geo_tag", item.payload)

    def test_text_only_post_matches_unique_known_poi(self):
        self.seed_poi()
        self.api.post.pop("location_id")
        self.pipeline.run("Mock City", [self.adapter()])
        evidence = self.storage.all(LocationEvidence)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].type, "TEXT_EXTRACTED")
        self.assertEqual(self.storage.all(POIMention)[0].poi_id, "existing-rooftop")

    def test_area_tag_does_not_resolve_or_create_poi(self):
        self.api.location["name"] = "Mock City"
        self.pipeline.run("Mock City", [self.adapter()])
        candidate = self.storage.all(POICandidate)[0]
        self.assertEqual(candidate.category, "area")
        self.assertEqual(candidate.review_status, "NEEDS_REVIEW")
        self.assertEqual(self.storage.all(POI), [])

    def test_unknown_location_type_stays_in_review_even_with_name_match(self):
        self.seed_poi()
        self.api.location.pop("address")
        self.pipeline.run("Mock City", [self.adapter()])
        candidate = self.storage.all(POICandidate)[0]
        self.assertEqual(candidate.review_status, "NEEDS_REVIEW")
        self.assertEqual(candidate.review_reasons, ["UNKNOWN_LOCATION_TYPE"])
        self.assertIsNone(candidate.resolved_poi_id)

    def test_manual_review_resolves_unknown_category_to_existing_poi(self):
        self.pipeline.run("Mock City", [self.adapter()])
        candidate = self.storage.all(POICandidate)[0]
        self.assertEqual(candidate.review_status, "NEEDS_REVIEW")
        self.seed_poi()
        decision = ReviewService(self.storage, self.config.visibility_threshold).resolve(
            candidate.id, "existing-rooftop", "Verified matching place")
        self.assertEqual(decision.action, "RESOLVE")
        self.assertEqual(self.storage.all(POIMention)[0].poi_id, "existing-rooftop")
        self.assertEqual(len(self.storage.all(RecommendationSignal)), 2)
        self.assertEqual(len(self.storage.all(LocationEvidence)), 1)

    def test_location_retrieval_404_preserves_id_without_guessing(self):
        def opener(request, timeout):
            if urlsplit(request.full_url).path.endswith("98765"):
                raise HTTPError(request.full_url, 404, "missing", {}, None)
            return self.api(request, timeout)
        adapter = ThreadsAdapter("token", query="rooftop", opener=opener)
        item = list(adapter.collect("Mock City"))[0]
        self.assertEqual(item.payload["source_metadata"]["location_status"], "UNAVAILABLE")
        self.assertEqual(item.payload["geo_tag"]["id"], "98765")
        self.assertEqual(item.payload["geo_tag"]["level"], "UNKNOWN")

    def test_malformed_responses_and_post(self):
        with self.assertRaises(ThreadsAPIError):
            parse_post({"id": "1", "permalink": "https://other.example/post/x"})
        with self.assertRaises(ThreadsAPIError):
            parse_post({"id": "1"})
        self.api.post = {"bad": "record"}
        with self.assertRaises(ThreadsAPIError):
            list(self.adapter().collect("Mock City"))

    def test_auth_and_rate_limit_errors_are_safe(self):
        for status, expected in ((401, AuthRequired), (403, AuthRequired),
                                 (429, ThreadsAPIError)):
            self.api.error = HTTPError("https://graph.threads.net/", status,
                                       "secret-token should never print", {}, None)
            with self.assertRaises(expected) as caught:
                list(self.adapter().collect("Mock City"))
            self.assertNotIn("secret-token", str(caught.exception))

    def test_timeout_retries_once(self):
        self.api.error = URLError("offline secret-token")
        with self.assertRaisesRegex(ThreadsAPIError, "network or timeout") as caught:
            list(self.adapter().collect("Mock City"))
        self.assertEqual(len(self.api.calls), 2)
        self.assertNotIn("secret-token", str(caught.exception))

    def test_limits_and_token_validation(self):
        with self.assertRaises(ValueError):
            self.adapter(limit=6)
        with self.assertRaises(ValueError):
            self.adapter(pages=3)
        with self.assertRaises(AuthRequired):
            ThreadsAdapter("", query="x")
        self.assertEqual(self.adapter().validate_token()["user_id"], "111")

    def test_search_uses_bounded_pagination(self):
        first = {**POST, "id": "100", "location_id": None}
        second = {**POST, "id": "101", "location_id": None}
        calls = []
        def opener(request, timeout):
            path = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
            calls.append((path, parse_qs(urlsplit(request.full_url).query)))
            if path == "keyword_search":
                if "after" in calls[-1][1]:
                    return Response({"data": [second]})
                return Response({"data": [first], "paging": {"cursors": {"after": "next"}}})
            return Response(first if path == "100" else second)
        items = list(ThreadsAdapter("token", query="rooftop", limit=2, pages=2,
                                    opener=opener).collect("Mock City"))
        self.assertEqual([item.external_id for item in items], ["100", "101"])
        self.assertEqual(len([path for path, _ in calls if path == "keyword_search"]), 2)

    def test_pipeline_resolves_existing_poi_and_provenance(self):
        self.seed_poi()
        first = self.pipeline.run("Mock City", [self.adapter()])
        self.assertEqual((first["RawSourceRecord"], first["SourceContent"],
                          first["POICandidate"], first["POI"], first["POIMention"],
                          first["RecommendationSignal"], first["LocationEvidence"]),
                         (1, 1, 1, 1, 1, 2, 1))
        candidate = self.storage.all(POICandidate)[0]
        mention = self.storage.all(POIMention)[0]
        self.assertEqual((candidate.resolved_poi_id, mention.poi_id),
                         ("existing-rooftop", "existing-rooftop"))
        self.assertEqual(self.storage.all(LocationEvidence)[0].type, "AUTHOR_GEO_TAG")
        self.assertTrue(any(item.field_name == "location_id" and item.source_field == "geo_tag.id"
                            for item in self.storage.all(FieldProvenance)))
        self.assertEqual(self.storage.all(SourceContent)[0].text, "")
        self.assertEqual(self.storage.all(RawSourceRecord)[0].payload["text"], POST["text"])
        self.assertEqual(len(dataset(self.storage, self.config, "Mock City")["mentions"]), 0)
        self.assertEqual(self.pipeline.run("Mock City", [self.adapter()]), first)

    def test_update_and_unavailable_preserve_ids_and_exclude_aggregation(self):
        self.seed_poi()
        self.pipeline.run("Mock City", [self.adapter()])
        ids = tuple(self.storage.all(model)[0].id for model in
                    (SourceContent, POICandidate, POIMention, RecommendationSignal))
        self.api.post["text"] = "Example Rooftop sunset"
        self.pipeline.run("Mock City", [self.adapter()])
        self.assertEqual(self.storage.all(SourceContent)[0].content_status, "UPDATED")
        self.assertEqual(len(self.storage.all(RecommendationSignal)), 1)
        self.assertEqual(ids[:3], tuple(self.storage.all(model)[0].id for model in
                                         (SourceContent, POICandidate, POIMention)))
        missing = ThreadsAdapter("token", post_id="12345", opener=lambda request, timeout:
                                 (_ for _ in ()).throw(HTTPError(request.full_url, 404, "missing", {}, None)))
        self.pipeline.run("Mock City", [missing])
        self.assertEqual(self.storage.all(SourceContent)[0].content_status, "UNAVAILABLE")
        self.assertEqual(len(self.storage.all(POIMention)), 1)
        self.assertEqual(self.storage.recommendation_counts("existing-rooftop"), [])
        self.assertEqual(dataset(self.storage, self.config, "Mock City")["mentions"], [])

    def test_unknown_missing_post_does_not_create_unavailable_content(self):
        missing = ThreadsAdapter("token", post_id="12345", opener=lambda request, timeout:
                                 (_ for _ in ()).throw(HTTPError(request.full_url, 404, "missing", {}, None)))
        self.pipeline.run("Mock City", [missing])
        self.assertEqual(self.storage.all(RawSourceRecord), [])
        self.assertEqual(self.storage.all(SourceContent), [])

    def test_policy_excludes_sensitive_fields_and_export_details(self):
        self.seed_poi()
        self.pipeline.run("Mock City", [self.adapter()])
        raw = self.storage.all(RawSourceRecord)[0]
        content = self.storage.all(SourceContent)[0]
        self.assertNotIn("thumbnail_url", str(raw.payload))
        self.assertNotIn("secret-token", str(raw))
        self.assertEqual(content.author_identifier, None)
        exported = dataset(self.storage, self.config, "Mock City")
        self.assertEqual(exported["mentions"], [])
        self.assertEqual(exported["pois"][0]["recommendation_counts"], [])


if __name__ == "__main__":
    unittest.main()
