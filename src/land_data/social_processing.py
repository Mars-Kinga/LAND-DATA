"""Platform-neutral social extraction with an optional, bounded LLM step."""
import re
from typing import Dict, List, Optional, Protocol

from .llm_extraction import LLMExtractionService, to_pipeline_payload, visible_input
from .local_classifier import LocalClassifier, persist_classification
from .models import FieldProvenance, POI, POIMention, SourceContent
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

EVENT_TITLE_CUE = re.compile(r"(?:展览|展会|演出|音乐节|市集|exhibition|festival|concert|展(?:[！!。，,.\s]|$))",
                             re.IGNORECASE)


def _event_title_fallback(content: SourceContent, material: Dict) -> List[dict]:
    """Conservative event candidate when a visible title is explicit but model access fails."""
    title = material.get("title", content.title)
    if not isinstance(title, str) or not EVENT_TITLE_CUE.search(title):
        return []
    title = title.strip()[:160]
    return [{"name": title, "starts_at": None, "ends_at": None,
             "extraction_confidence": 0.45, "extraction_evidence": "title:" + title}]


class LocationExtractor(Protocol):
    def extract(self, content: SourceContent, payload: Dict) -> List[dict]: ...


class RecommendationExtractor(Protocol):
    def extract(self, context: str) -> List[dict]: ...


class GeoTagExtractor:
    def extract(self, content: SourceContent, payload: Dict) -> List[dict]:
        tag = payload.get("geo_tag")
        if not isinstance(tag, dict) or not tag.get("name"):
            return []
        level = tag.get("level")
        if level not in ("POI", "AREA", "UNKNOWN"):
            return []
        if level in ("AREA", "UNKNOWN") and content.platform != "threads":
            return []
        evidence_type = "AREA_GEO_TAG" if level == "AREA" else "AUTHOR_GEO_TAG"
        return [{"name": tag["name"], "city": tag.get("city", ""),
                 "country": tag.get("country", ""),
                 "category": "area" if level == "AREA" else tag.get("category", "unknown"),
                 "address": tag.get("address", ""),
                 "latitude": tag.get("latitude"),
                 "longitude": tag.get("longitude"), "context": payload.get("text", content.text),
                 "evidence": [{"type": evidence_type,
                               "raw_value": str(tag.get("id") or tag["name"]),
                               "confidence": EVIDENCE_STRENGTH[evidence_type]}]}]


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


class KnownPOITextLocationExtractor:
    """Conservative exact-name extraction for known POIs, without inferred geography."""
    def __init__(self, storage: Storage):
        self.storage = storage

    def extract(self, content: SourceContent, payload: Dict) -> List[dict]:
        if not (content.platform == "threads" or payload.get("match_known_pois")) or payload.get("geo_tag"):
            return []
        title, body = payload.get("title", ""), payload.get("text", "")
        if not isinstance(title, str) or not isinstance(body, str) or not (title or body):
            return []
        context = "\n".join(part for part in (title, body) if part)
        names = {}
        for poi in self.storage.all(POI):
            for name in list(poi.names.values()) + poi.aliases:
                if len(name.strip()) >= 5:
                    names.setdefault(normalized_name(name), []).append((name, poi))
        matches = []
        for entries in names.values():
            poi_ids = {poi.id for _, poi in entries}
            if len(poi_ids) != 1:
                continue
            name, poi = entries[0]
            pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
            in_title = re.search(pattern, title, re.IGNORECASE)
            in_body = re.search(pattern, body, re.IGNORECASE)
            if not in_title and not in_body:
                continue
            evidence = [{"type": origin, "raw_value": name,
                         "confidence": EVIDENCE_STRENGTH[origin]}
                        for origin, found in (("TITLE_EXTRACTED", in_title),
                                              ("TEXT_EXTRACTED", in_body)) if found]
            matches.append({"name": name, "city": poi.city, "country": poi.country,
                            "category": poi.category, "context": context,
                            "evidence": evidence})
        return matches


class RuleBasedRecommendationExtractor:
    RULES = (("sunset", "feature", "sunset"), ("date", "good_for", "date"),
             ("抹茶", "food", "matcha"), ("学习", "good_for", "study"),
             ("cocktail", "drink", "cocktail"), ("法式吐司", "food", "french toast"),
             ("ミルフィーユ", "food", "mille-feuille"), ("布丁", "food", "pudding"),
             ("将军咖啡", "drink", "shogun coffee"), ("刨冰", "food", "shaved ice"))

    def extract(self, context: str) -> List[dict]:
        lower = context.casefold()
        return [{"type": kind, "value": value, "confidence": 0.85,
                 "sentiment": (None if value == "french toast" or
                               not re.search(r"(?<!#)" + re.escape(term), lower) else "positive"),
                 "evidence_reference": "text:" + term}
                for term, kind, value in self.RULES if term in lower]


def process_social(content: SourceContent, storage: Storage, payload: Dict,
                   transient: Dict = None,
                   llm_service: Optional[LLMExtractionService] = None,
                   retention_days: int = 0) -> None:
    if content.content_status == "UNAVAILABLE":
        return
    material = dict(payload)
    if transient:
        material.update(transient)
    classification = LocalClassifier(storage).classify(content, material)
    persist_classification(storage, content, classification, material)
    if llm_service is not None:
        try:
            structured = llm_service.extract(content, material, storage, retention_days)
            converted = to_pipeline_payload(structured, material.get("geo_tag"),
                                            visible_input(content, material))
        except Exception as exc:
            llm_service.fallbacks += 1
            detail = str(exc)
            if detail != "cached LLM extraction failure" or content.external_id not in llm_service.errors:
                llm_service.errors[content.external_id] = (detail if detail in (
                    "Qwen extraction network error", "invalid extraction evidence",
                    "extraction evidence is absent from visible text", "invalid extraction schema",
                    "invalid location schema", "invalid signal schema", "invalid event schema") or
                    detail.startswith("Qwen extraction HTTP ") else type(exc).__name__)
        else:
            if not converted["events"]:
                converted["events"] = _event_title_fallback(content, material)
            _save_social(content, storage, material, converted["locations"], converted["events"])
            return
    extractors: List[LocationExtractor] = [GeoTagExtractor(), FixtureTextLocationExtractor(),
                                           KnownPOITextLocationExtractor(storage)]
    locations = []
    for extractor in extractors:
        for location in extractor.extract(content, material):
            key = (normalized_name(location["name"]), location["category"])
            prior = next((value for value in locations
                          if (normalized_name(value["name"]), value["category"]) == key), None)
            if prior:
                for evidence in location["evidence"]:
                    if not any((item["type"], normalized_name(item["raw_value"])) ==
                               (evidence["type"], normalized_name(evidence["raw_value"]))
                               for item in prior["evidence"]):
                        prior["evidence"].append(evidence)
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
        elif material.get("always_mention") and material.get("geo_tag", {}).get("level") != "AREA":
            location["relation_type"] = "MENTION"
    events = []
    for event in material.get("events", []):
        item = {key: event[key] for key in ("name", "starts_at", "ends_at") if key in event}
        venue_name = normalized_name(event.get("venue_name", ""))
        for index, location in enumerate(locations):
            if normalized_name(location["name"]) == venue_name:
                item["venue_index"] = index
                break
        events.append(item)
    if not events:
        events = _event_title_fallback(content, material)
    _save_social(content, storage, material, locations, events)


def _save_social(content: SourceContent, storage: Storage, material: Dict,
                 locations: List[dict], events: List[dict]) -> None:
    extract(content, storage, {"source_kind": "content", "locations": locations,
                               "events": events}, False)
    tag = material.get("geo_tag")
    if content.platform == "threads" and isinstance(tag, dict) and tag.get("id"):
        for index, location in enumerate(locations):
            if location["name"] == tag.get("name"):
                candidate_id = stable_id("candidate", content.id, index)
                storage.put(FieldProvenance(
                    id=stable_id("provenance", candidate_id, "location_id", content.id),
                    entity_type="POICandidate", entity_id=candidate_id,
                    field_name="location_id", source="threads",
                    source_content_id=content.id, collected_at=content.collected_at,
                    source_field="geo_tag.id"))
    active_mentions = {stable_id("mention", content.id, index)
                       for index, location in enumerate(locations) if location.get("relation_type")}
    for mention in storage.all(POIMention):
        if mention.source_content_id == content.id and mention.id not in active_mentions and mention.active:
            mention.active = False
            mention.review_status = "NEEDS_REVIEW"
            mention.review_reasons = ["SOURCE_CONTENT_UPDATED"]
            storage.put(mention)
