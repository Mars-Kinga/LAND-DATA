"""Platform-neutral deterministic social extraction; no network or LLM calls."""
from typing import Dict, List, Protocol

from .models import POIMention, SourceContent
from .processing import extract
from .storage import Storage
from .util import normalized_name, stable_id


EVIDENCE_STRENGTH = {
    "AUTHOR_GEO_TAG": 0.95,
    "TITLE_EXTRACTED": 0.82,
    "TEXT_EXTRACTED": 0.72,
    "HASHTAG": 0.48,
    "AREA_GEO_TAG": 0.22,
}


class LocationExtractor(Protocol):
    def extract(self, content: SourceContent, payload: Dict) -> List[dict]: ...


class RecommendationExtractor(Protocol):
    def extract(self, context: str) -> List[dict]: ...


class GeoTagExtractor:
    def extract(self, content: SourceContent, payload: Dict) -> List[dict]:
        tag = payload.get("geo_tag")
        if not isinstance(tag, dict) or tag.get("level") != "POI" or not tag.get("name"):
            return []
        return [{"name": tag["name"], "city": tag["city"], "country": tag["country"],
                 "category": tag["category"], "latitude": tag.get("latitude"),
                 "longitude": tag.get("longitude"), "context": payload.get("text", content.text),
                 "evidence": [{"type": "AUTHOR_GEO_TAG", "raw_value": tag["name"],
                               "confidence": EVIDENCE_STRENGTH["AUTHOR_GEO_TAG"]}]}]


class FixtureTextLocationExtractor:
    """Fixture annotations identify names and per-place text spans, not decisions."""
    def extract(self, content: SourceContent, payload: Dict) -> List[dict]:
        result = []
        for hint in payload.get("text_mentions", []):
            name = hint["name"]
            origin = "TITLE_EXTRACTED" if hint.get("origin") == "title" else "TEXT_EXTRACTED"
            result.append({"name": name, "city": hint["city"], "country": hint["country"],
                           "category": hint["category"], "context": hint.get("context", ""),
                           "evidence": [{"type": origin, "raw_value": name,
                                         "confidence": EVIDENCE_STRENGTH[origin]}]})
        return result


class RuleBasedRecommendationExtractor:
    RULES = (("sunset", "feature", "sunset"), ("date", "good_for", "date"),
             ("抹茶", "food", "matcha"), ("学习", "good_for", "study"),
             ("cocktail", "drink", "cocktail"))

    def extract(self, context: str) -> List[dict]:
        lower = context.casefold()
        return [{"type": kind, "value": value, "confidence": 0.85,
                 "sentiment": "positive", "evidence_reference": "text:" + term}
                for term, kind, value in self.RULES if term in lower]


def process_social(content: SourceContent, storage: Storage, payload: Dict,
                   transient: Dict = None) -> None:
    if content.content_status == "UNAVAILABLE":
        return
    material = dict(payload)
    if transient:
        material.update(transient)
    extractors: List[LocationExtractor] = [GeoTagExtractor(), FixtureTextLocationExtractor()]
    locations = []
    for extractor in extractors:
        for location in extractor.extract(content, material):
            key = (normalized_name(location["name"]), location["category"])
            prior = next((value for value in locations
                          if (normalized_name(value["name"]), value["category"]) == key), None)
            if prior:
                prior["evidence"].extend(location["evidence"])
                if location.get("context"):
                    prior["context"] = location["context"]
            else:
                locations.append(location)
    area = material.get("geo_tag")
    if isinstance(area, dict) and area.get("level") == "AREA":
        for location in locations:
            location["evidence"].append({"type": "AREA_GEO_TAG",
                                         "raw_value": area.get("name", ""),
                                         "confidence": EVIDENCE_STRENGTH["AREA_GEO_TAG"]})
    recommendations: RecommendationExtractor = RuleBasedRecommendationExtractor()
    for location in locations:
        context = location.pop("context", "")
        if not context and len(locations) == 1:
            context = content.text
        signals = recommendations.extract(context)
        if signals:
            location["relation_type"] = "RECOMMENDATION"
            location["recommendation_summary"] = ", ".join(value["value"] for value in signals)
            location["recommendation_signals"] = signals
    events = []
    for event in material.get("events", []):
        item = {key: event[key] for key in ("name", "starts_at", "ends_at") if key in event}
        venue_name = normalized_name(event.get("venue_name", ""))
        for index, location in enumerate(locations):
            if normalized_name(location["name"]) == venue_name:
                item["venue_index"] = index
                break
        events.append(item)
    extract(content, storage, {"source_kind": "content", "locations": locations,
                               "events": events}, False)
    active_mentions = {stable_id("mention", content.id, index)
                       for index, location in enumerate(locations) if location.get("relation_type")}
    for mention in storage.all(POIMention):
        if mention.source_content_id == content.id and mention.id not in active_mentions and mention.active:
            mention.active = False
            mention.review_status = "NEEDS_REVIEW"
            mention.review_reasons = ["SOURCE_CONTENT_UPDATED"]
            storage.put(mention)
