import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from land_data.adapters import MockSocialAdapter, SourceItem
from land_data.auth import MockSessionStore
from land_data.config import load_config
from land_data.export import dataset
from land_data.geoscope import GeoScope, load_scope
from land_data.models import (Event, EventCandidate, FieldProvenance, LocationEvidence, POI,
                              POICandidate, POIMention, RawSourceRecord, RecommendationSignal, SourceContent)
from land_data.osm import OSMSourceAdapter
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, _limit):
        return self.body


class OneItemAdapter:
    name = "mock-social-2c"

    def __init__(self, item):
        self.item = item

    def collect(self, _city):
        return [self.item]


class SocialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = SQLiteStorage(str(Path(temporary.name) / "social.db"))
        self.addCleanup(self.storage.close)
        self.config = load_config(str(ROOT))
        self.scope = load_scope(str(ROOT / "config/social-acceptance-scope.json"))
        self.sessions = MockSessionStore(str(Path(temporary.name) / "session.json"))
        self.pipeline = Pipeline(self.storage, self.config)
        osm_body = (ROOT / "fixtures/osm_social_2c.json").read_bytes()
        osm = OSMSourceAdapter(self.scope, opener=lambda _request, timeout: Response(osm_body))
        self.pipeline.run(self.scope.name, [osm], self.scope)
        self.social = self.config.adapter("mock-social-2c", self.sessions)

    def run_social(self):
        return self.pipeline.run(self.scope.name, [self.social], self.scope)

    def content(self, external_id):
        return next(item for item in self.storage.all(SourceContent) if item.external_id == external_id)

    def candidates(self, external_id):
        return [item for item in self.storage.all(POICandidate)
                if item.source_content_id == self.content(external_id).id]

    def mentions(self, external_id):
        return [item for item in self.storage.all(POIMention)
                if item.source_content_id == self.content(external_id).id]

    def signals(self, mention):
        return [item for item in self.storage.all(RecommendationSignal) if item.poi_mention_id == mention.id]

    def poi(self, name):
        return next(item for item in self.storage.all(POI) if name in item.names.values())

    def test_social_adapter_contract_and_source_content_schema(self):
        self.assertIsInstance(self.social, MockSocialAdapter)
        capability = self.social.capability
        self.assertTrue(all((capability.supports_text, capability.supports_title,
                             capability.supports_geo_tag, capability.supports_hashtags,
                             capability.supports_media_metadata, capability.supports_author)))
        self.assertFalse(capability.requires_authentication)
        raw_item = next(item for item in self.social.collect(self.scope.name)
                        if item.external_id == "case-a")
        self.assertIn("geo_tag", raw_item.payload)
        self.assertNotIn("locations", raw_item.payload)
        self.run_social()
        content = self.content("case-a")
        self.assertEqual(content.platform, "mock-social-2c")
        self.assertEqual(content.canonical_url, "https://social.example.invalid/2c/case-a")
        self.assertEqual(content.author_identifier, "mock-author-a")
        self.assertEqual(content.hashtags, ["sunset"])
        self.assertEqual(content.media_metadata, [{"type": "image", "count": 1}])
        self.assertEqual(content.language, "zh")
        self.assertEqual(content.published_at, "2026-09-20T12:00:00Z")
        self.assertEqual(content.collection_scope, self.scope.name)
        self.assertEqual(content.content_status, "ACTIVE")

    def test_geo_tag_and_text_extraction(self):
        self.run_social()
        rooftop = self.candidates("case-a")[0]
        self.assertEqual(rooftop.resolved_poi_id, self.poi("Example Rooftop").id)
        evidence = [item for item in self.storage.all(LocationEvidence) if item.poi_candidate_id == rooftop.id]
        self.assertEqual([item.type for item in evidence], ["AUTHOR_GEO_TAG"])
        self.assertEqual({signal.value for signal in self.signals(self.mentions("case-a")[0])},
                         {"sunset", "date"})
        matcha = self.candidates("case-b")[0]
        self.assertEqual(matcha.resolved_poi_id, self.poi("Example Matcha Cafe").id)
        self.assertIn("TEXT_EXTRACTED", [item.type for item in self.storage.all(LocationEvidence)
                                         if item.poi_candidate_id == matcha.id])
        signal = self.signals(self.mentions("case-b")[0])[0]
        self.assertEqual((signal.type, signal.value, signal.sentiment), ("food", "matcha", "positive"))

    def test_multiple_pois_keep_recommendations_separate(self):
        self.run_social()
        self.assertEqual(len(self.candidates("case-c")), 2)
        mentions = self.mentions("case-c")
        self.assertEqual(len(mentions), 2)
        by_poi = {mention.poi_id: {(signal.type, signal.value) for signal in self.signals(mention)}
                  for mention in mentions}
        self.assertEqual(by_poi[self.poi("Cafe A").id], {("good_for", "study")})
        self.assertEqual(by_poi[self.poi("Bar B").id], {("drink", "cocktail")})
        self.assertNotEqual(mentions[0].id, mentions[1].id)

    def test_area_tag_and_no_location_content(self):
        self.run_social()
        area = self.candidates("case-d")[0]
        self.assertEqual(area.name, "Unidentified cafe")
        self.assertEqual(area.review_status, "NEEDS_REVIEW")
        self.assertIsNone(area.resolved_poi_id)
        self.assertNotIn("Shibuya", [name for poi in self.storage.all(POI) for name in poi.names.values()])
        self.assertIn("AREA_GEO_TAG", [item.type for item in self.storage.all(LocationEvidence)
                                       if item.poi_candidate_id == area.id])
        self.assertEqual((self.candidates("case-e"), self.mentions("case-e")), ([], []))
        self.assertEqual(self.content("case-e").text, "东京今天的天气太舒服了。")

    def test_event_links_gallery_without_creating_event_poi(self):
        self.run_social()
        events = [item for item in self.storage.all(EventCandidate)
                  if item.source_content_id == self.content("case-f").id]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].venue_candidate_id, self.candidates("case-f")[0].id)
        event = next(item for item in self.storage.all(Event) if item.name == "摄影展")
        self.assertEqual(event.venue_poi_id, self.poi("Example Gallery").id)
        self.assertNotIn("摄影展", [name for poi in self.storage.all(POI) for name in poi.names.values()])

    def test_social_resolves_to_existing_osm_pois_and_exports(self):
        counts = self.run_social()
        self.assertEqual((counts["RawSourceRecord"], counts["SourceContent"],
                          counts["POICandidate"], counts["POI"], counts["POIMention"],
                          counts["RecommendationSignal"]), (11, 11, 11, 5, 4, 5))
        self.assertTrue(all(mention.poi_id for mention in self.storage.all(POIMention)))
        self.assertTrue(all(mention.review_status == "AUTO_APPROVED"
                            for mention in self.storage.all(POIMention)))
        self.assertTrue(all(any(provenance.source == "osm" and provenance.entity_id == poi.id
                                for provenance in self.storage.all(FieldProvenance))
                            for poi in self.storage.all(POI)))
        output = dataset(self.storage, self.config, self.scope.city, self.scope.name)
        self.assertEqual((len(output["pois"]), len(output["mentions"])), (5, 4))
        self.assertNotIn("东京今天的天气", json.dumps(output, ensure_ascii=False))
        self.assertNotIn("media_metadata", json.dumps(output))

    def test_social_duplicate_idempotency(self):
        first = self.run_social()
        ids_before = {model: {item.id for item in self.storage.all(model)}
                      for model in (SourceContent, POIMention, RecommendationSignal)}
        second = self.run_social()
        self.assertEqual(first, second)
        self.assertEqual(ids_before, {model: {item.id for item in self.storage.all(model)}
                                      for model in ids_before})

    def test_updated_content_same_identity_and_signal_refresh(self):
        self.run_social()
        before = self.content("case-b")
        mention = self.mentions("case-b")[0]
        item = next(value for value in self.social.collect(self.scope.name)
                    if value.external_id == "case-b")
        changed = dict(item.payload)
        changed["text"] = "Example Matcha Cafe 很适合 date。"
        changed["text_mentions"] = [{**changed["text_mentions"][0],
                                     "context": "Example Matcha Cafe 很适合 date。"}]
        self.pipeline.run(self.scope.name, [OneItemAdapter(SourceItem(item.source, item.external_id,
                                                                      item.source_url, changed))], self.scope)
        after = self.content("case-b")
        self.assertEqual(after.id, before.id)
        self.assertEqual(after.content_status, "UPDATED")
        self.assertEqual(len(self.mentions("case-b")), 1)
        self.assertEqual(self.mentions("case-b")[0].id, mention.id)
        self.assertEqual([(item.type, item.value) for item in self.signals(mention)],
                         [("good_for", "date")])

    def test_unavailable_content_stays_identifiable_but_not_exported(self):
        self.run_social()
        before = self.content("case-b")
        item = next(value for value in self.social.collect(self.scope.name)
                    if value.external_id == "case-b")
        unavailable = {"content_type": "SOCIAL_POST", "content_status": "UNAVAILABLE"}
        self.pipeline.run(self.scope.name, [OneItemAdapter(SourceItem(item.source, item.external_id,
                                                                      item.source_url, unavailable))], self.scope)
        after = self.content("case-b")
        self.assertEqual(after.id, before.id)
        self.assertEqual(after.content_status, "UNAVAILABLE")
        self.assertEqual(self.mentions("case-b")[0].review_status, "NEEDS_REVIEW")
        self.assertEqual(self.mentions("case-b")[0].review_reasons, ["SOURCE_UNAVAILABLE"])
        output = dataset(self.storage, self.config, self.scope.city, self.scope.name)
        self.assertEqual(len(output["mentions"]), 3)
        self.pipeline.run(self.scope.name, [OneItemAdapter(item)], self.scope)
        self.assertEqual(self.content("case-b").id, before.id)
        self.assertEqual(self.content("case-b").content_status, "UPDATED")
        self.assertEqual(self.mentions("case-b")[0].review_status, "AUTO_APPROVED")
        self.assertEqual(len(dataset(self.storage, self.config, self.scope.city, self.scope.name)["mentions"]), 4)

    def test_geo_tag_update_rebinds_existing_mention(self):
        self.run_social()
        before_mention = self.mentions("case-a")[0]
        item = next(value for value in self.social.collect(self.scope.name)
                    if value.external_id == "case-a")
        changed = dict(item.payload)
        changed["geo_tag"] = {"level": "POI", "name": "Example Matcha Cafe",
                              "city": "Mock City", "country": "ZZ", "category": "cafe",
                              "latitude": 0.0002, "longitude": 0.0002}
        self.pipeline.run(self.scope.name, [OneItemAdapter(SourceItem(item.source, item.external_id,
                                                                      item.source_url, changed))], self.scope)
        after_mention = self.mentions("case-a")[0]
        self.assertEqual(after_mention.id, before_mention.id)
        self.assertEqual(after_mention.poi_id, self.poi("Example Matcha Cafe").id)
        self.assertEqual(self.content("case-a").content_status, "UPDATED")
        self.assertEqual({value.value for value in self.signals(after_mention)}, {"sunset", "date"})

    def test_removed_location_deactivates_old_mention(self):
        self.run_social()
        item = next(value for value in self.social.collect(self.scope.name)
                    if value.external_id == "case-b")
        changed = dict(item.payload)
        changed["text"] = "今天的天气很好。"
        changed["text_mentions"] = []
        self.pipeline.run(self.scope.name, [OneItemAdapter(SourceItem(item.source, item.external_id,
                                                                      item.source_url, changed))], self.scope)
        mention = self.mentions("case-b")[0]
        self.assertFalse(mention.active)
        self.assertEqual(mention.review_reasons, ["SOURCE_CONTENT_UPDATED"])
        self.assertEqual(len(dataset(self.storage, self.config, self.scope.city, self.scope.name)["mentions"]), 3)

    def test_source_policy_omits_restricted_raw_and_export_fields(self):
        original = self.config.sources["mock-social-2c"]
        raw_fields = tuple(key for key in original.policy.raw_fields
                           if key not in ("text", "media_metadata", "source_metadata"))
        normalized_fields = tuple(key for key in original.policy.normalized_fields if key in raw_fields)
        policy = replace(original.policy, raw_fields=raw_fields, normalized_fields=normalized_fields,
                         temporary_fields=("text",), export_fields=())
        config = replace(self.config, sources={**self.config.sources,
                                               "mock-social-2c": replace(original, policy=policy)})
        pipeline = Pipeline(self.storage, config)
        pipeline.run(self.scope.name, [self.social], self.scope)
        raw = next(item for item in self.storage.all(RawSourceRecord)
                   if item.source == "mock-social-2c" and item.external_id == "case-a")
        content = self.content("case-a")
        self.assertNotIn("text", raw.payload)
        self.assertNotIn("media_metadata", raw.payload)
        self.assertEqual(content.text, "")
        self.assertEqual(content.media_metadata, [])
        self.assertEqual({value.value for value in self.signals(self.mentions("case-a")[0])},
                         {"sunset", "date"})
        output = dataset(self.storage, config, self.scope.city, self.scope.name)
        serialized = json.dumps(output)
        self.assertNotIn("recommendation_summary", serialized)
        self.assertNotIn('"signals"', serialized)
        self.assertNotIn("social.example.invalid", serialized)

    def test_media_metadata_keeps_structure_only(self):
        item = next(value for value in self.social.collect(self.scope.name)
                    if value.external_id == "case-a")
        changed = dict(item.payload)
        changed["media_metadata"] = [{"type": "image", "width": 640,
                                      "url": "https://private.invalid/image.jpg",
                                      "bytes": "fake-image-data"}]
        self.pipeline.run(self.scope.name, [OneItemAdapter(SourceItem(item.source, item.external_id,
                                                                      item.source_url, changed))], self.scope)
        raw = next(value for value in self.storage.all(RawSourceRecord)
                   if value.source == "mock-social-2c")
        self.assertEqual(raw.payload["media_metadata"], [{"type": "image", "width": 640}])
        self.assertEqual(self.content("case-a").media_metadata, [{"type": "image", "width": 640}])

    def test_scope_export_isolation_survives_raw_purge(self):
        self.run_social()
        other = GeoScope("other-scope", "ZZ", self.scope.bbox, "CUSTOM", "Mock City")
        element = {"type": "node", "id": 92001, "lat": 0.0007, "lon": 0.0007,
                   "tags": {"name": "Other Place", "amenity": "cafe"}}
        body = json.dumps({"elements": [element]}).encode()
        adapter = OSMSourceAdapter(other, opener=lambda _request, timeout: Response(body))
        self.pipeline.run(other.name, [adapter], other)
        full = dataset(self.storage, self.config, "Mock City")
        one = dataset(self.storage, self.config, "Mock City", self.scope.name)
        two = dataset(self.storage, self.config, "Mock City", other.name)
        self.assertEqual((len(full["pois"]), len(one["pois"]), len(two["pois"])), (6, 5, 1))
        self.storage.purge_expired_raw("9999-01-01T00:00:00Z")
        self.assertEqual(len(dataset(self.storage, self.config, "Mock City", self.scope.name)["pois"]), 5)
        self.assertEqual(len(dataset(self.storage, self.config, "Mock City", other.name)["pois"]), 1)


if __name__ == "__main__":
    unittest.main()
