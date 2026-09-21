"""Deterministic normalization, extraction, resolution and publication gates."""
import math
from typing import Dict, List, Optional

from .models import (Event, EventCandidate, FieldProvenance, LocationEvidence, POI,
                     POICandidate, POIMention, RawSourceRecord, RecommendationSignal,
                     SourceContent)
from .policy import SourcePolicy
from .storage import Storage
from .util import normalize_url, normalized_name, stable_id, utc_now


def normalize(record: RawSourceRecord, policy: SourcePolicy) -> SourceContent:
    data = {key: record.payload[key] for key in policy.normalized_fields if key in record.payload}
    metadata = {key: data[key] for key in ("locations", "events", "source_kind") if key in data}
    if "locations" in metadata:
        metadata["locations"] = [
            {key: value for key, value in location.items()
             if key not in ("evidence", "relation_type", "recommendation_summary", "recommendation_signals")}
            for location in metadata["locations"]]
    return SourceContent(
        id=stable_id(record.source, record.external_id), raw_record_id=record.id,
        platform=record.source, external_id=record.external_id,
        source_url=normalize_url(record.source_url) if record.source_url else None,
        content_type=str(data.get("content_type", "POST")),
        title=str(data.get("title", "")), text=str(data.get("text", "")),
        author_identifier=data.get("author_identifier"),
        published_at=data.get("published_at"), collected_at=record.collected_at,
        raw_metadata=metadata)


def extract(content: SourceContent, storage: Storage, source_payload: Dict,
            allow_coordinate_verification: bool = False,
            transient: Optional[Dict] = None) -> None:
    metadata = {key: source_payload[key] for key in ("locations", "events", "source_kind") if key in source_payload}
    if transient:
        metadata.update({key: value for key, value in transient.items() if key in ("locations", "events")})
    now = utc_now()
    for index, location in enumerate(metadata.get("locations", [])):
        candidate_id = stable_id("candidate", content.id, index)
        evidence_items = location.get("evidence", [])
        confidence = 0.0
        seen_types = set()
        for evidence in evidence_items:
            strength = float(evidence["confidence"])
            evidence_type = str(evidence["type"])
            if evidence_type not in seen_types:
                confidence = max(confidence, strength) if not seen_types else min(1.0, confidence + 0.1 * strength)
            seen_types.add(evidence_type)
        candidate = POICandidate(
            id=candidate_id, source_content_id=content.id, name=str(location["name"]),
            name_locale=str(location.get("name_locale", "und")),
            aliases=list(location.get("aliases", [])), address=str(location.get("address", "")),
            city=str(location["city"]), country=str(location["country"]),
            category=str(location.get("category", "other")), created_at=now,
            latitude=location.get("latitude"), longitude=location.get("longitude"),
            source_kind=str(metadata.get("source_kind", "content")),
            coordinate_verified=allow_coordinate_verification and location.get("coordinate_verified") is True,
            confidence_score=round(confidence, 3))
        previous = storage.get(POICandidate, candidate_id)
        if previous:
            candidate.created_at = previous.created_at
            candidate.resolved_poi_id = previous.resolved_poi_id
            candidate.review_status = previous.review_status
            candidate.review_reasons = previous.review_reasons
        storage.put(candidate)
        for evidence_index, evidence in enumerate(evidence_items):
            evidence_id = stable_id("evidence", candidate_id, evidence_index)
            previous_evidence = storage.get(LocationEvidence, evidence_id)
            storage.put(LocationEvidence(
                id=evidence_id, source_content_id=content.id, poi_candidate_id=candidate_id,
                type=str(evidence["type"]), raw_value=str(evidence["raw_value"]),
                confidence=float(evidence["confidence"]),
                created_at=previous_evidence.created_at if previous_evidence else now,
                matched_poi_id=previous_evidence.matched_poi_id if previous_evidence else None))
        relation = location.get("relation_type")
        if relation:
            mention_id = stable_id("mention", content.id, index)
            previous_mention = storage.get(POIMention, mention_id)
            mention = POIMention(
                id=mention_id, source_content_id=content.id, poi_candidate_id=candidate_id,
                poi_id=candidate.resolved_poi_id, relation_type=str(relation),
                recommendation_summary=str(location.get("recommendation_summary", "")),
                confidence_score=candidate.confidence_score,
                created_at=previous_mention.created_at if previous_mention else now,
                review_status=previous_mention.review_status if previous_mention else "NEEDS_REVIEW",
                review_reasons=previous_mention.review_reasons if previous_mention else [])
            storage.put(mention)
            for signal_index, value in enumerate(location.get("recommendation_signals", [])):
                signal_id = stable_id("signal", mention_id, signal_index)
                previous_signal = storage.get(RecommendationSignal, signal_id)
                storage.put(RecommendationSignal(
                    id=signal_id, poi_mention_id=mention_id, type=str(value["type"]),
                    value=str(value["value"]).lower(), sentiment=value.get("sentiment"),
                    confidence=float(value.get("confidence", 0.7)),
                    evidence_reference=str(value.get("evidence_reference", "text")),
                    created_at=previous_signal.created_at if previous_signal else now))
    for index, event in enumerate(metadata.get("events", [])):
        event_id = stable_id("event-candidate", content.id, index)
        previous_event = storage.get(EventCandidate, event_id)
        candidate = EventCandidate(
            id=event_id, source_content_id=content.id, name=str(event["name"]),
            starts_at=event.get("starts_at"), ends_at=event.get("ends_at"),
            venue_candidate_id=stable_id("candidate", content.id, event["venue_index"])
            if "venue_index" in event else None,
            created_at=previous_event.created_at if previous_event else now)
        if previous_event:
            candidate.review_status = previous_event.review_status
            candidate.review_reasons = previous_event.review_reasons
        storage.put(candidate)


def distance_meters(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1, math.sqrt(value)))


def valid_coordinates(latitude: Optional[float], longitude: Optional[float]) -> bool:
    return (latitude is not None and longitude is not None and
            isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)) and
            math.isfinite(latitude) and math.isfinite(longitude) and
            -90 <= latitude <= 90 and -180 <= longitude <= 180)


def possible_poi_matches(storage: Storage, candidate: POICandidate) -> List[dict]:
    """Name/alias matches in the same city, with distance exposed for review."""
    results = []
    for poi in storage.pois_in_city(candidate.city):
        if poi.country != candidate.country or poi.category != candidate.category:
            continue
        names = list(poi.names.values()) + poi.aliases
        if normalized_name(candidate.name) not in {normalized_name(name) for name in names}:
            continue
        distance = None
        if valid_coordinates(candidate.latitude, candidate.longitude):
            distance = round(distance_meters(candidate.latitude, candidate.longitude,
                                             poi.latitude, poi.longitude), 1)
        results.append({"poi": poi, "distance_meters": distance,
                        "within_250m": distance is None or distance <= 250})
    return sorted(results, key=lambda match: (match["distance_meters"] is None,
                                               match["distance_meters"] or 0, match["poi"].id))


def resolve(storage: Storage) -> None:
    candidates = sorted(storage.all(POICandidate), key=lambda value: (value.source_kind != "structured", value.id))
    for candidate in candidates:
        if candidate.review_status == "REJECTED":
            continue
        if candidate.resolved_poi_id:
            continue
        if (candidate.latitude is None) != (candidate.longitude is None) or (
            candidate.latitude is not None and not valid_coordinates(candidate.latitude, candidate.longitude)):
            candidate.review_reasons = ["INVALID_COORDINATES"]
            storage.put(candidate)
            continue
        possibilities = possible_poi_matches(storage, candidate)
        matches = [match["poi"] for match in possibilities if match["within_250m"]]
        coordinate_conflicts = any(not match["within_250m"] for match in possibilities)
        if len(matches) == 1:
            candidate.resolved_poi_id = matches[0].id
            candidate.review_status = "AUTO_APPROVED"
            candidate.review_reasons = []
        elif len(matches) > 1:
            candidate.review_reasons = ["AMBIGUOUS_POI_MATCH"]
        elif coordinate_conflicts:
            candidate.review_reasons = ["CONFLICTING_COORDINATES"]
        elif (candidate.source_kind == "structured" and candidate.coordinate_verified and
              valid_coordinates(candidate.latitude, candidate.longitude) and candidate.confidence_score >= 0.8):
            poi_id = stable_id("poi", candidate.id)
            now = utc_now()
            storage.put(POI(
                id=poi_id, names={candidate.name_locale: candidate.name}, aliases=candidate.aliases,
                latitude=candidate.latitude, longitude=candidate.longitude,
                address=candidate.address, city=candidate.city, country=candidate.country,
                category=candidate.category, created_at=now, updated_at=now,
                last_verified_at=now, confidence_score=candidate.confidence_score,
                coordinates_verified=True))
            source_content = storage.get(SourceContent, candidate.source_content_id)
            for field_name in ("names", "latitude", "longitude", "address", "category"):
                storage.put(FieldProvenance(
                    id=stable_id("provenance", poi_id, field_name, candidate.source_content_id),
                    entity_type="POI", entity_id=poi_id, field_name=field_name,
                    source=source_content.platform,
                    source_content_id=candidate.source_content_id,
                    collected_at=source_content.collected_at, verified_at=now))
            candidate.resolved_poi_id = poi_id
            candidate.review_status = "AUTO_APPROVED"
            candidate.review_reasons = []
        else:
            candidate.review_reasons = ["INSUFFICIENT_LOCATION_EVIDENCE"]
        storage.put(candidate)
        if candidate.resolved_poi_id:
            for mention in storage.all(POIMention):
                if mention.poi_candidate_id == candidate.id:
                    mention.poi_id = candidate.resolved_poi_id
                    storage.put(mention)
            for evidence in storage.all(LocationEvidence):
                if evidence.poi_candidate_id == candidate.id:
                    evidence.matched_poi_id = candidate.resolved_poi_id
                    storage.put(evidence)


def validate_score_review(storage: Storage, visibility_threshold: float) -> None:
    for poi in storage.all(POI):
        poi.validation_passed = valid_coordinates(poi.latitude, poi.longitude) and bool(poi.names) and bool(poi.city)
        poi.quality_score = round(min(1.0, poi.confidence_score * (1.0 if poi.address else 0.9)), 3)
        if poi.review_status != "REJECTED":
            poi.review_reasons = [] if poi.validation_passed else [
                "INVALID_COORDINATES" if not valid_coordinates(poi.latitude, poi.longitude)
                else "MISSING_REQUIRED_FIELD"]
            poi.review_status = "AUTO_APPROVED" if poi.validation_passed and poi.coordinates_verified else "NEEDS_REVIEW"
        poi.map_visibility = (poi.validation_passed and poi.coordinates_verified and poi.active and
                              poi.review_status == "AUTO_APPROVED" and poi.confidence_score >= visibility_threshold)
        storage.put(poi)
    for mention in storage.all(POIMention):
        if mention.review_status == "REJECTED":
            continue
        poi = storage.get(POI, mention.poi_id) if mention.poi_id else None
        candidate = storage.get(POICandidate, mention.poi_candidate_id) if mention.poi_candidate_id else None
        if candidate and candidate.review_status == "REJECTED":
            mention.review_reasons = ["REJECTED_CANDIDATE"]
        else:
            history = storage.review_history("POIMention", mention.id)
            human_approved = bool(history and history[-1].action == "APPROVE" and
                                  history[-1].new_status == "AUTO_APPROVED")
            mention.review_reasons = (
                [] if poi and (mention.confidence_score >= 0.7 or human_approved)
                else ["UNRESOLVED_POI" if not poi else "LOW_CONFIDENCE"])
        mention.review_status = "AUTO_APPROVED" if not mention.review_reasons else "NEEDS_REVIEW"
        storage.put(mention)
    for candidate in storage.all(EventCandidate):
        if candidate.starts_at and candidate.ends_at and candidate.ends_at >= candidate.starts_at:
            venue = storage.get(POICandidate, candidate.venue_candidate_id) if candidate.venue_candidate_id else None
            event_id = stable_id("event", candidate.id)
            previous_event = storage.get(Event, event_id)
            storage.put(Event(id=event_id, name=candidate.name, source_content_id=candidate.source_content_id,
                              starts_at=candidate.starts_at, ends_at=candidate.ends_at,
                              venue_poi_id=venue.resolved_poi_id if venue else None,
                              created_at=previous_event.created_at if previous_event else candidate.created_at,
                              review_status=previous_event.review_status if previous_event else "NEEDS_REVIEW",
                              review_reasons=previous_event.review_reasons if previous_event else ["EVENT_REQUIRES_REVIEW"]))
