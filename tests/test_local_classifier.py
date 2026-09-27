import tempfile
import unittest
from pathlib import Path

from land_data.local_classifier import (
    CLASSIFIER_VERSION,
    LocalClassifier,
    classify_content,
    current_classifications,
    persist_classification,
)
from land_data.models import ContentClassification, POI, SourceContent
from land_data.sqlite_storage import SQLiteStorage


class LocalClassifierTests(unittest.TestCase):
    def content(self, title="", text="", *, platform="xiaohongshu-browser", geo_tag=None,
                hashtags=None, content_type="POST"):
        return SourceContent(
            id="content-1", raw_record_id="raw-1", platform=platform,
            external_id="post-1", source_url="https://example.test/post-1",
            content_type=content_type, title=title, text=text,
            collected_at="2026-09-27T00:00:00Z", geo_tag=geo_tag,
            hashtags=list(hashtags or []),
        )

    def test_empty_text_is_stable_and_irrelevant(self):
        content = self.content()
        first = classify_content(content, {})
        second = LocalClassifier().classify(content, {})
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.version, CLASSIFIER_VERSION)
        self.assertEqual(first.primary_label, "irrelevant")
        self.assertEqual(first.route, "irrelevant")
        self.assertFalse(first.needs_review)
        self.assertFalse(first.model_recommended)
        self.assertIn("text.empty", first.signals)
        self.assertIn("empty_visible_text", first.reasons)

    def test_short_unrelated_text_is_irrelevant_without_model(self):
        result = LocalClassifier().classify(self.content(title="今天下雨了", text="还不错"), {})
        self.assertEqual(result.primary_label, "irrelevant")
        self.assertFalse(result.model_recommended)
        self.assertIn("text.short", result.signals)

    def test_search_group_is_audit_signal_not_content_evidence(self):
        result = LocalClassifier().classify(
            self.content(title="让纽约感受一下中国功夫", text="今天来拍照，记录日常瞬间"),
            {"source_metadata": {"query_group": "culture"}},
        )
        self.assertNotIn("culture", result.scenario_labels)
        self.assertEqual(result.primary_label, "irrelevant")
        self.assertIn("query_group.culture", result.signals)

    def test_generic_venue_noun_does_not_turn_knowledge_into_place(self):
        result = LocalClassifier().classify(
            self.content(title="美国社交礼仪", text="这些餐厅通常需要付小费，也要注意餐桌礼仪。"), {})
        self.assertEqual(result.primary_label, "knowledge")
        self.assertFalse(result.model_recommended)

    def test_generic_activity_word_is_not_an_event(self):
        result = LocalClassifier().classify(
            self.content(title="美国社交礼仪", text="参加社交活动时注意礼仪，small talk 也要尊重边界。"), {})
        self.assertEqual(result.primary_label, "knowledge")
        self.assertNotIn("event", result.labels)

    def test_culture_and_etiquette_are_knowledge_without_a_place(self):
        result = LocalClassifier().classify(
            self.content(title="河内文化礼仪", text="进入寺庙要注意着装和拍照禁忌"),
            {"source_metadata": {"query_group": "culture"}},
        )
        self.assertEqual(result.primary_label, "knowledge")
        self.assertEqual(result.route, "knowledge")
        self.assertEqual(result.scenario_labels, ["culture"])
        self.assertTrue(result.needs_review)
        self.assertFalse(result.model_recommended)
        self.assertIn("scenario.culture", result.signals)
        self.assertIn("non_poi_travel_knowledge", result.reasons)

    def test_poi_geotag_is_structured_poi(self):
        result = LocalClassifier().classify(
            self.content(title="咖啡推荐", text="环境很安静", geo_tag={
                "id": "poi-123", "name": "Example Cafe", "level": "POI",
                "city": "San Francisco", "latitude": 37.7, "longitude": -122.4,
            }),
            {"geo_tag": {"id": "poi-123", "name": "Example Cafe", "level": "POI"}},
        )
        self.assertEqual(result.primary_label, "structured_poi")
        self.assertEqual(result.route, "structured_poi")
        self.assertEqual(result.labels, ["structured_poi"])
        self.assertFalse(result.model_recommended)
        self.assertFalse(result.needs_review)
        self.assertIn("place.geo_tag.poi", result.signals)
        self.assertAlmostEqual(result.confidence, 0.99)

    def test_known_poi_uses_exact_name_without_llm(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStorage(str(Path(temp) / "classifier.db"))
            self.addCleanup(storage.close)
            storage.put(POI(
                id="poi-1", names={"en": "Golden Gate Park"}, latitude=37.7694,
                longitude=-122.4862, city="san-francisco", country="US",
                category="park", created_at="2026-09-27T00:00:00Z",
                updated_at="2026-09-27T00:00:00Z", aliases=["GGP"],
            ))
            result = LocalClassifier(storage).classify(
                self.content(title="Golden Gate Park", text="很适合散步"),
                {"match_known_pois": True},
            )
            self.assertEqual(result.primary_label, "known_poi")
            self.assertEqual(result.route, "known_poi")
            self.assertFalse(result.model_recommended)
            self.assertFalse(result.needs_review)
            self.assertIn("place.known_poi_exact_name", result.signals)
            self.assertIn("unique_exact_poi_name", result.reasons)

    def test_explicit_event_is_event_and_keeps_structured_event_signal(self):
        result = LocalClassifier().classify(
            self.content(title="河内摄影展", text="本周末开放，欢迎报名", content_type="EVENT"),
            {"content_type": "event", "events": [{"name": "河内摄影展"}]},
        )
        self.assertEqual(result.primary_label, "event")
        self.assertEqual(result.route, "event")
        self.assertIn("event", result.labels)
        self.assertTrue(result.needs_review)
        self.assertFalse(result.model_recommended)
        self.assertIn("event.structured_input", result.signals)

    def test_poi_and_event_are_mixed(self):
        result = LocalClassifier().classify(
            self.content(title="博物馆特展", text="周六开幕，值得去", geo_tag={
                "name": "City Museum", "level": "POI", "city": "hanoi",
            }),
            {"events": [{"name": "特展"}]},
        )
        self.assertEqual(result.primary_label, "mixed")
        self.assertEqual(result.route, "mixed")
        self.assertEqual(result.labels[0], "mixed")
        self.assertIn("structured_poi", result.labels)
        self.assertIn("event", result.labels)
        self.assertTrue(result.needs_review)

    def test_unknown_place_recommends_model_but_does_not_call_one(self):
        result = LocalClassifier().classify(
            self.content(title="Luna Cafe", text="这家咖啡店适合工作，位置在市中心"),
            {},
        )
        self.assertEqual(result.primary_label, "unresolved_place")
        self.assertEqual(result.route, "unresolved_place")
        self.assertTrue(result.needs_review)
        self.assertTrue(result.model_recommended)
        self.assertIn("place.text_cue", result.signals)
        self.assertIn("place_without_structured_identity", result.reasons)

    def test_mapping_access_and_scores_are_json_friendly(self):
        result = LocalClassifier().classify(
            self.content(title="Hanoi cafe", text="culture and etiquette tips"), {})
        payload = result.to_dict()
        self.assertEqual(payload["version"], result["version"])
        self.assertEqual(payload["primary_label"], result.get("primary_label"))
        self.assertIsInstance(payload["scores"], dict)
        self.assertEqual(list(payload["scores"]), sorted(payload["scores"]))

    def test_persistence_is_idempotent_and_keeps_new_content_version(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStorage(str(Path(temp) / "classifier.db"))
            self.addCleanup(storage.close)
            first_content = self.content(title="河内文化礼仪", text="进入寺庙注意着装")
            storage.put(first_content)
            first_result = LocalClassifier(storage).classify(first_content, {})
            first = persist_classification(storage, first_content, first_result, {})
            repeated = persist_classification(storage, first_content, first_result, {})
            self.assertEqual(first.id, repeated.id)
            self.assertEqual(len(storage.all(ContentClassification)), 1)

            changed = self.content(title="河内文化礼仪", text="进入寺庙注意着装和拍照禁忌")
            storage.put(changed)
            changed_result = LocalClassifier(storage).classify(changed, {})
            second = persist_classification(storage, changed, changed_result, {})
            self.assertNotEqual(first.id, second.id)
            self.assertEqual(len(storage.all(ContentClassification)), 2)
            self.assertEqual([item.id for item in current_classifications(storage)], [second.id])


if __name__ == "__main__":
    unittest.main()
