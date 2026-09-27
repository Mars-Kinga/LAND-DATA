import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from land_data.adapters import SourceItem
from land_data.config import load_config
from land_data.llm_extraction import (LLMExtractionService, QwenExtractionClient,
                                      load_local_env, to_pipeline_payload, validate_output,
                                      visible_input)
from land_data.models import (EventCandidate, LocationEvidence, POICandidate,
                              POIMention, RecommendationSignal, SourceContent)
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class OneItemAdapter:
    name = "mock-social-2c"

    def __init__(self, item):
        self.item = item

    def collect(self, _city):
        return [self.item]


def evidence(field, quote, confidence=0.9):
    return {"field": field, "quote": quote, "confidence": confidence}


def location(name, quote, *, kind="POI", role="TARGET", category="cafe", parent="", branch="",
             signals=(), field="text"):
    return {"name": name, "kind": kind, "target_role": role,
            "category": category, "city": "Mock City",
            "country": "ZZ", "parent_name": parent, "branch_hint": branch,
            "area_context": "", "confidence": 0.9,
            "evidence": [evidence(field, quote)],
            "relation_type": "RECOMMENDATION" if signals else "MENTION",
            "signals": [{"type": kind, "value": value, "sentiment": "positive",
                         "confidence": 0.85, "evidence": evidence("text", phrase)}
                        for kind, value, phrase in signals]}


class FakeClient:
    model = "test-model"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.inputs = []

    def complete(self, fields):
        self.inputs.append(fields)
        if self.error:
            raise self.error
        return self.result


class LLMExtractionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = SQLiteStorage(str(Path(temporary.name) / "test.db"))
        self.addCleanup(self.storage.close)
        self.config = load_config(str(ROOT))

    def run_item(self, client, external_id="post-1", text=None, title="Two cafes"):
        service = LLMExtractionService(client)
        pipeline = Pipeline(self.storage, self.config, service)
        payload = {"content_type": "SOCIAL_POST", "title": title,
                   "text": text or "Cafe A has sunset drinks. Cafe B at Mall on second floor has matcha. Photo exhibit at Cafe B.",
                   "hashtags": ["travel"], "cookie": "do-not-send", "session": "do-not-send",
                   "html": "<html>do-not-send</html>"}
        item = SourceItem("mock-social-2c", external_id, "https://social.example.invalid/" + external_id,
                          payload)
        pipeline.run("mock-review-city", [OneItemAdapter(item)])
        return service, pipeline, item

    def test_multiple_places_signals_context_event_and_cache(self):
        result = {"locations": [
            location("Cafe A", "Cafe A", signals=(("feature", "sunset", "sunset"),)),
            location("Cafe B", "Cafe B", parent="Mall", branch="second floor",
                     signals=(("food", "matcha", "matcha"),))],
            "events": [{"name": "Photo exhibit", "starts_at": "", "ends_at": "",
                        "venue_index": 1, "confidence": 0.75,
                        "evidence": evidence("text", "Photo exhibit")}]}
        client = FakeClient(result)
        service, pipeline, item = self.run_item(client)
        self.assertEqual(service.calls, 1)
        self.assertEqual((len(self.storage.all(POICandidate)), len(self.storage.all(POIMention)),
                          len(self.storage.all(RecommendationSignal)), len(self.storage.all(EventCandidate))),
                         (2, 2, 2, 1))
        candidates = self.storage.all(POICandidate)
        self.assertEqual({value.parent_name for value in candidates}, {"", "Mall"})
        self.assertEqual(next(value for value in candidates if value.name == "Cafe B")
                         .extraction_context["branch_hint"], "second floor")
        evidence_rows = self.storage.all(LocationEvidence)
        self.assertEqual({value.type for value in evidence_rows}, {"TEXT_EXTRACTED"})
        self.assertTrue(all(value.confidence == 0.9 for value in evidence_rows))
        event = self.storage.all(EventCandidate)[0]
        self.assertEqual(event.extraction_confidence, 0.75)
        self.assertEqual(event.extraction_evidence, "text:Photo exhibit")
        linked = {candidate.id: candidate.name for candidate in candidates}
        by_name = {linked[mention.poi_candidate_id]: [signal.value for signal in
                   self.storage.all(RecommendationSignal) if signal.poi_mention_id == mention.id]
                   for mention in self.storage.all(POIMention)}
        self.assertEqual(by_name, {"Cafe A": ["sunset"], "Cafe B": ["matcha"]})
        self.assertEqual(event.venue_candidate_id,
                         next(candidate.id for candidate in candidates if candidate.name == "Cafe B"))
        self.assertNotIn("do-not-send", json.dumps(client.inputs))
        self.assertNotIn("html", client.inputs[0])
        pipeline.run("mock-review-city", [OneItemAdapter(item)])
        self.assertEqual((service.calls, service.cache_hits), (1, 1))
        another = LLMExtractionService(FakeClient(error=AssertionError("cache miss")))
        Pipeline(self.storage, self.config, another).parse("mock-review-city")
        self.assertEqual((another.calls, another.cache_hits), (0, 1))

    def test_content_update_changes_hash_and_calls_again(self):
        result = {"locations": [location("Cafe A", "Cafe A")], "events": []}
        client = FakeClient(result)
        service, pipeline, item = self.run_item(client)
        changed = dict(item.payload)
        changed["text"] += " Another sentence."
        pipeline.run("mock-review-city", [OneItemAdapter(SourceItem(item.source, item.external_id,
                                                               item.source_url, changed))])
        self.assertEqual((service.calls, service.cache_hits), (2, 0))
        self.assertEqual(len(self.storage.all(POICandidate)), 1)

    def test_invalid_evidence_falls_back_to_existing_deterministic_extractor(self):
        result = {"locations": [location("Invented Cafe", "not in the post")], "events": []}
        client = FakeClient(result)
        service, _, _ = self.run_item(client, text="Cafe A has sunset drinks.")
        self.assertEqual((service.calls, service.fallbacks), (1, 1))
        self.assertEqual(self.storage.all(POICandidate), [])
        self.assertEqual(self.storage.all(POIMention), [])

    def test_network_failure_uses_fixture_fallback(self):
        client = FakeClient(error=TimeoutError())
        service = LLMExtractionService(client)
        pipeline = Pipeline(self.storage, self.config, service)
        item = SourceItem("mock-social-2c", "fallback", "https://social.example.invalid/fallback",
                          {"title": "Cafe A", "text": "Cafe A 很适合学习。",
                           "text_mentions": [{"name": "Cafe A", "city": "Mock City", "country": "ZZ",
                                              "category": "cafe", "context": "Cafe A 很适合学习。"}]})
        pipeline.run("mock-review-city", [OneItemAdapter(item)])
        self.assertEqual(service.fallbacks, 1)
        self.assertEqual(self.storage.all(POICandidate)[0].name, "Cafe A")
        self.assertEqual(self.storage.all(RecommendationSignal)[0].value, "study")
        pipeline.run("mock-review-city", [OneItemAdapter(item)])
        self.assertEqual((service.calls, service.cache_hits, service.fallbacks), (2, 1, 2))

    def test_only_visible_fields_are_sent_to_qwen(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, _limit):
                return json.dumps({"choices": [{"message": {"content": '{"locations": [], "events": []}'}}]}).encode()

        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return Response()

        client = QwenExtractionClient("test-key", "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                                      "qwen3.7-plus", opener=opener)
        content = SourceContent("id", "raw", "mock-social-2c", "post", None, "POST", "Visible title",
                                "Visible body", "2026-01-01T00:00:00Z", geo_tag={"name": "Area", "level": "AREA"})
        result = client.complete(visible_input(content, {"cookie": "private", "html": "private"}))
        self.assertEqual(result, {"locations": [], "events": []})
        request, timeout = requests[0]
        body = json.loads(request.data)
        self.assertEqual(timeout, 18)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertFalse(body["enable_thinking"])
        sent = body["messages"][1]["content"]
        self.assertIn("Visible body", sent)
        self.assertNotIn("private", sent)
        self.assertNotIn("test-key", sent)

    def test_unquoted_branch_and_parent_are_not_persisted(self):
        item = location("Cafe A", "Cafe A", kind="BRAND", parent="Invented Mall",
                        branch="Official East Branch")
        output = to_pipeline_payload({"locations": [item], "events": []}, None,
                                     {"title": "", "text": "Cafe A is nearby.",
                                      "hashtags": [], "visible_location": {}})
        candidate = output["locations"][0]
        self.assertEqual(candidate["parent_name"], "")
        self.assertEqual(candidate["extraction_context"]["branch_hint"], "")
        self.assertEqual(candidate["category"], "unknown")

    def test_local_env_takes_precedence_without_logging_key(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text("DASHSCOPE_API_KEY=local-test-value\n", encoding="utf-8")
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "inherited-test-value"}):
                load_local_env(Path(directory))
                self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "local-test-value")

    def test_context_and_station_reference_do_not_create_mentions(self):
        station = location("Mock Station", "Mock Station", category="other")
        context = location("Mall", "Mall", kind="PARENT", role="CONTEXT", category="other")
        cafe = location("Cafe A", "Cafe A", parent="Mall")
        output = to_pipeline_payload({"locations": [station, context, cafe], "events": []}, None,
                                     {"title": "", "text": "Meet at Mock Station, then visit Cafe A in Mall.",
                                      "hashtags": [], "visible_location": {}})
        self.assertEqual([item["name"] for item in output["locations"]], ["Cafe A"])
        self.assertEqual(output["locations"][0]["parent_name"], "Mall")

    def test_named_container_of_another_venue_is_not_a_target(self):
        container = location("Bookstore", "Bookstore", signals=(
            ("feature", "seating", "seating"),))
        cafe = location("Cafe A", "Cafe A")
        fields = {"title": "Cafe A with a view",
                  "text": "Bookstore里找了个Cafe A seating was available.",
                  "hashtags": [], "visible_location": {}}
        output = to_pipeline_payload({"locations": [container, cafe], "events": []}, None, fields)
        self.assertEqual([item["name"] for item in output["locations"]], ["Cafe A"])

    def test_adjacent_parent_is_context_when_title_targets_another_venue(self):
        parent = location("Mall", "Mall", kind="PARENT", category="other")
        cafe = location("Cafe A", "Cafe A")
        fields = {"title": "Cafe A", "text": "Cafe A was good. 旁边Mall有露台花园。",
                  "hashtags": [], "visible_location": {}}
        output = to_pipeline_payload({"locations": [cafe, parent], "events": []}, None, fields)
        self.assertEqual([item["name"] for item in output["locations"]], ["Cafe A"])

    def test_explicit_area_target_needs_real_signal(self):
        area = location("Old Quarter", "Old Quarter", kind="AREA", category="area")
        area["signals"] = [{"type": "feature", "value": "positive", "sentiment": "positive",
                            "confidence": 0.8, "evidence": evidence("text", "great view")}]
        area["relation_type"] = "RECOMMENDATION"
        fields = {"title": "", "text": "Old Quarter has a great view.",
                  "hashtags": [], "visible_location": {}}
        self.assertEqual(to_pipeline_payload({"locations": [area], "events": []}, None, fields)["locations"], [])
        area["signals"][0]["value"] = "city views"
        self.assertEqual(len(to_pipeline_payload({"locations": [area], "events": []}, None, fields)["locations"]), 1)

    def test_signal_values_drop_sentiment_and_sentences(self):
        venue = location("Cafe A", "Cafe A")
        venue["signals"] = [
            {"type": "other", "value": value, "sentiment": "positive", "confidence": 0.8,
             "evidence": evidence("text", "matcha")}
            for value in ("positive", "This entire sentence describes every detail of the venue and its service",
                          "matcha")]
        output = to_pipeline_payload({"locations": [venue], "events": []})
        self.assertEqual([value["value"] for value in output["locations"][0]["recommendation_signals"]],
                         ["matcha"])

    def test_signal_values_remove_generic_evaluation_without_inventing_topics(self):
        venue = location("Cafe A", "Cafe A")
        venue["signals"] = [
            {"type": "food", "value": value, "sentiment": "positive", "confidence": 0.8,
             "evidence": evidence("text", "Cafe A")}
            for value in ("将军咖啡很好喝", "waffle好吃", "chill", "很chill",
                          "sweet and great", "甜品精致复古", "手冲很丝滑",
                          "macchiato一般", "酒店里的餐饮体验", "找座位",
                          "alternative to crowded cafes", "coffee和cake", "可以看海")]
        output = to_pipeline_payload({"locations": [venue], "events": []})
        self.assertEqual([value["value"] for value in output["locations"][0]["recommendation_signals"]],
                         ["将军咖啡", "waffle", "甜品", "手冲", "macchiato"])

    def test_event_title_survives_model_timeout_without_station_mention(self):
        client = FakeClient(error=TimeoutError())
        service, pipeline, item = self.run_item(client, text="", title="Mock Station 的免费摄影展！")
        self.assertEqual(service.calls, 2)
        self.assertEqual(self.storage.all(POIMention), [])
        events = self.storage.all(EventCandidate)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].extraction_confidence, 0.45)
        self.assertIsNone(events[0].venue_candidate_id)
        pipeline.run("mock-review-city", [OneItemAdapter(item)])
        self.assertEqual((service.calls, service.cache_hits), (2, 1))

    def test_one_network_retry_can_recover_and_then_cache(self):
        class RecoveringClient(FakeClient):
            def complete(self, fields):
                self.inputs.append(fields)
                if len(self.inputs) == 1:
                    raise ValueError("Qwen extraction network error")
                return self.result

        client = RecoveringClient({"locations": [location("Cafe A", "Cafe A")], "events": []})
        service, pipeline, item = self.run_item(client)
        self.assertEqual((service.calls, service.fallbacks), (2, 0))
        self.assertEqual(len(self.storage.all(POIMention)), 1)
        pipeline.run("mock-review-city", [OneItemAdapter(item)])
        self.assertEqual((service.calls, service.cache_hits), (2, 1))

    def test_invalid_evidence_is_removed_without_losing_other_venues_and_event(self):
        bad = location("Invented", "absent")
        good = location("Cafe A", "Cafe A", signals=(
            ("feature", "sunset", "sunset"), ("food", "matcha", "absent")))
        result = {"locations": [bad, good], "events": [
            {"name": "Photo exhibit", "starts_at": "", "ends_at": "",
             "venue_index": 1, "confidence": 0.8, "evidence": evidence("text", "Photo exhibit")}]}
        fields = {"title": "", "text": "Cafe A has sunset drinks. Photo exhibit.",
                  "hashtags": [], "visible_location": {}}
        cleaned = validate_output(result, fields)
        self.assertEqual([item["name"] for item in cleaned["locations"]], ["Cafe A"])
        self.assertEqual([item["value"] for item in cleaned["locations"][0]["signals"]], ["sunset"])
        self.assertEqual(cleaned["events"][0]["venue_index"], 0)


if __name__ == "__main__":
    unittest.main()
