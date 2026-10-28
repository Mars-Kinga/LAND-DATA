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
    metadata = {key: data[key] for key in ("locations", "events", "source_kind",
                                          "osm_object_type", "osm_object_id", "source_metadata") if key in data}
    if "locations" in metadata:
        metadata["locations"] = [
            {key: value for key, value in location.items()
             if key not in ("evidence", "relation_type", "recommendation_summary", "recommendation_signals")}
            for location in metadata["locations"]]
    canonical_url = normalize_url(record.source_url) if record.source_url else None
    return SourceContent(
        id=stable_id(record.source, record.external_id), raw_record_id=record.id,
        platform=record.source, external_id=record.external_id,
        source_url=canonical_url, canonical_url=canonical_url,
        content_type=str(data.get("content_type", "POST")),
        title=str(data.get("title", "")), text=str(data.get("text", "")),
        author_identifier=data.get("author_identifier"),
        published_at=data.get("published_at"), collected_at=record.collected_at,
        raw_metadata=metadata, geo_tag=data.get("geo_tag"),
        hashtags=list(data.get("hashtags", [])),
        media_metadata=list(data.get("media_metadata", [])),
        language=data.get("language"), content_status=data.get("content_status", "ACTIVE"),
        collection_scope=record.city)


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
            website=str(location.get("website", "")),
            opening_hours=str(location.get("opening_hours", "")),
            source_tags=dict(location.get("osm_tags", {})),
            city=str(location["city"]), country=str(location["country"]),
            category=str(location.get("category", "other")), created_at=now,
            parent_name=str(location.get("parent_name", "")),
            coordinate_precision=str(location.get("coordinate_precision", "UNSPECIFIED")),
            field_sources=dict(location.get("field_sources", {})),
            extraction_context=dict(location.get("extraction_context", {})),
            coordinate_source_url=location.get("coordinate_source_url"),
            latitude=location.get("latitude"), longitude=location.get("longitude"),
            source_kind=str(metadata.get("source_kind", "content")),
            coordinate_verified=allow_coordinate_verification and location.get("coordinate_verified") is True,
            confidence_score=round(confidence, 3))
        source_review_reasons = list(location.get("review_reasons", []))
        previous = storage.get(POICandidate, candidate_id)
        identity_changed = bool(previous and candidate.source_kind == "content" and
                                (previous.name, previous.city, previous.country, previous.category,
                                 previous.latitude, previous.longitude) !=
                                (candidate.name, candidate.city, candidate.country, candidate.category,
                                 candidate.latitude, candidate.longitude))
        if previous:
            candidate.created_at = previous.created_at
            if not identity_changed and not source_review_reasons:
                candidate.resolved_poi_id = previous.resolved_poi_id
                candidate.review_status = previous.review_status
                candidate.review_reasons = previous.review_reasons
        if source_review_reasons:
            candidate.resolved_poi_id = None
            candidate.review_status = "NEEDS_REVIEW"
            candidate.review_reasons = source_review_reasons
        storage.put(candidate)
        for evidence_index, evidence in enumerate(evidence_items):
            evidence_id = stable_id("evidence", candidate_id, evidence_index)
            previous_evidence = storage.get(LocationEvidence, evidence_id)
            storage.put(LocationEvidence(
                id=evidence_id, source_content_id=content.id, poi_candidate_id=candidate_id,
                type=str(evidence["type"]), raw_value=str(evidence["raw_value"]),
                confidence=float(evidence["confidence"]),
                created_at=previous_evidence.created_at if previous_evidence else now,
                matched_poi_id=(previous_evidence.matched_poi_id
                                if previous_evidence and not identity_changed else None)))
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
                review_status=(previous_mention.review_status
                               if previous_mention and not identity_changed else "NEEDS_REVIEW"),
                review_reasons=(previous_mention.review_reasons
                                if previous_mention and not identity_changed else []))
            storage.put(mention)
            signal_ids = []
            for signal_index, value in enumerate(location.get("recommendation_signals", [])):
                signal_id = stable_id("signal", mention_id, signal_index)
                signal_ids.append(signal_id)
                previous_signal = storage.get(RecommendationSignal, signal_id)
                storage.put(RecommendationSignal(
                    id=signal_id, poi_mention_id=mention_id, type=str(value["type"]),
                    value=str(value["value"]).lower(), sentiment=value.get("sentiment"),
                    confidence=float(value.get("confidence", 0.7)),
                    evidence_reference=str(value.get("evidence_reference", "text")),
                    created_at=previous_signal.created_at if previous_signal else now))
            storage.prune_signals(mention_id, signal_ids)
    for index, event in enumerate(metadata.get("events", [])):
        # Structured sources provide their own stable event key.  Social
        # extraction keeps the historical content/index identity.
        source_event_id = event.get("source_event_id")
        event_id = (stable_id("event-candidate", content.platform, content.external_id,
                              str(source_event_id)) if source_event_id else
                    stable_id("event-candidate", content.id, index))
        previous_event = storage.get(EventCandidate, event_id)
        structured_event = bool(source_event_id)
        source_review_reasons = list(event.get("review_reasons", []))
        candidate = EventCandidate(
            id=event_id, source_content_id=content.id, name=str(event["name"]),
            starts_at=event.get("starts_at"), ends_at=event.get("ends_at"),
            venue_candidate_id=stable_id("candidate", content.id, event["venue_index"])
            if "venue_index" in event else None,
            created_at=previous_event.created_at if previous_event else now,
            source_event_id=str(source_event_id) if source_event_id else None,
            lifecycle_status="ACTIVE",
            event_url=event.get("event_url"), registration_url=event.get("registration_url"),
            registration_description=str(event.get("registration_description", "") or ""),
            park_ids=list(event.get("park_ids", [])), park_names=list(event.get("park_names", [])),
            location=str(event.get("location", "") or ""),
            categories=list(event.get("categories", [])),
            description=str(event.get("description", "") or ""),
            instructor=str(event.get("instructor", "") or ""),
            contact_phone=str(event.get("contact_phone", "") or ""),
            latitude=event.get("latitude"), longitude=event.get("longitude"),
            timezone=str(event.get("timezone", "America/New_York")),
            field_sources=dict(event.get("field_sources", {})))
        candidate.extraction_confidence = event.get("extraction_confidence")
        candidate.extraction_evidence = event.get("extraction_evidence")
        if previous_event:
            candidate.lifecycle_status = "ACTIVE"
            changed = structured_event and any(
                getattr(previous_event, field) != getattr(candidate, field)
                for field in ("name", "starts_at", "ends_at", "event_url", "registration_url",
                              "registration_description", "park_ids", "park_names", "location",
                              "categories", "description", "instructor", "contact_phone",
                              "latitude", "longitude"))
            if structured_event and previous_event.lifecycle_status in ("UNAVAILABLE", "EXPIRED"):
                candidate.review_status = "NEEDS_REVIEW"
                candidate.review_reasons = ["EVENT_REAPPEARED", *source_review_reasons]
            elif structured_event and changed:
                candidate.review_status = "NEEDS_REVIEW"
                candidate.review_reasons = ["EVENT_SOURCE_UPDATED", *source_review_reasons]
            elif source_review_reasons:
                candidate.review_status = "NEEDS_REVIEW"
                candidate.review_reasons = source_review_reasons
            else:
                candidate.review_status = previous_event.review_status
                candidate.review_reasons = previous_event.review_reasons
        elif structured_event:
            candidate.review_status = "NEEDS_REVIEW"
            candidate.review_reasons = source_review_reasons or ["EVENT_REQUIRES_REVIEW"]
        storage.put(candidate)
        if structured_event:
            for field_name, source_field in candidate.field_sources.items():
                storage.put(FieldProvenance(
                    id=stable_id("provenance", candidate.id, field_name, content.id),
                    entity_type="EventCandidate", entity_id=candidate.id, field_name=field_name,
                    source=content.platform, source_content_id=content.id,
                    collected_at=content.collected_at, source_field=source_field,
                    source_reference_url=candidate.event_url))


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
        if poi.country != candidate.country:
            continue
        if candidate.category == "unknown":
            content = storage.get(SourceContent, candidate.source_content_id)
            if not (content and content.platform == "threads" and
                    valid_coordinates(candidate.latitude, candidate.longitude)):
                continue
        elif poi.category != candidate.category:
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


def sync_osm_poi(storage: Storage, candidate: POICandidate) -> None:
    """Update only the POI originally created from this stable OSM object."""
    if candidate.resolved_poi_id != stable_id("poi", candidate.id):
        return
    content = storage.get(SourceContent, candidate.source_content_id)
    if not content or content.platform != "osm" or not candidate.name.strip():
        return
    if not valid_coordinates(candidate.latitude, candidate.longitude) or candidate.category == "unknown":
        return
    poi = storage.get(POI, candidate.resolved_poi_id)
    if poi is None:
        return
    values = {"names": {candidate.name_locale: candidate.name}, "aliases": candidate.aliases,
              "latitude": candidate.latitude, "longitude": candidate.longitude,
              "category": candidate.category, "address": candidate.address,
              "website": candidate.website, "opening_hours": candidate.opening_hours,
              "coordinate_precision": candidate.coordinate_precision}
    changed = [field for field, value in values.items() if getattr(poi, field) != value]
    if not changed:
        return
    now = utc_now()
    for field in changed:
        setattr(poi, field, values[field])
    poi.updated_at = now
    poi.last_verified_at = now
    poi.confidence_score = candidate.confidence_score
    storage.put(poi)
    tag_key = "amenity" if "amenity" in candidate.source_tags else "tourism"
    prefix = "center." if content.raw_metadata.get("osm_object_type") in ("way", "relation") else ""
    origin = {"names": "tags.name", "aliases": "tags.name:en", "latitude": prefix + "lat", "longitude": prefix + "lon",
              "category": "tags." + tag_key, "address": "tags.addr:full",
              "website": "tags.website" if "website" in candidate.source_tags else "tags.contact:website",
              "opening_hours": "tags.opening_hours", "coordinate_precision": "geometry"}
    for field in changed:
        storage.put(FieldProvenance(
            id=stable_id("provenance", poi.id, field, content.id),
            entity_type="POI", entity_id=poi.id, field_name=field,
            source="osm", source_content_id=content.id,
            collected_at=content.collected_at, verified_at=now,
            source_field=origin[field]))


def resolve(storage: Storage, only_candidate_ids=None) -> None:
    candidates = sorted(storage.all(POICandidate), key=lambda value: (value.source_kind != "structured", value.id))
    if only_candidate_ids is not None:
        candidates = [candidate for candidate in candidates if candidate.id in only_candidate_ids]
    for candidate in candidates:
        if candidate.review_status == "REJECTED":
            continue
        if "NYC_AGGREGATE_FIELD_CONFLICT" in candidate.review_reasons:
            candidate.review_status = "NEEDS_REVIEW"
            storage.put(candidate)
            continue
        source_content = storage.get(SourceContent, candidate.source_content_id)
        is_nyc_structured = bool(source_content and source_content.platform == "nyc-open-data-restaurants"
                                  and candidate.source_kind == "structured")
        if candidate.resolved_poi_id:
            if candidate.source_kind == "structured":
                sync_osm_poi(storage, candidate)
            continue
        if not candidate.name.strip() or not candidate.city.strip() or not candidate.country.strip():
            candidate.review_reasons = ["MISSING_REQUIRED_FIELD"]
            storage.put(candidate)
            continue
        if candidate.category == "unknown":
            possibilities = possible_poi_matches(storage, candidate)
            matches = [match["poi"] for match in possibilities if match["within_250m"]]
            content = storage.get(SourceContent, candidate.source_content_id)
            is_threads = bool(content and content.platform == "threads")
            poi_like = bool(is_threads and isinstance(content.geo_tag, dict) and
                            content.geo_tag.get("level") == "POI")
            if poi_like and len(matches) == 1:
                candidate.resolved_poi_id = matches[0].id
                candidate.review_status = "AUTO_APPROVED"
                candidate.review_reasons = []
                storage.put(candidate)
                for mention in storage.all(POIMention):
                    if mention.poi_candidate_id == candidate.id:
                        mention.poi_id = candidate.resolved_poi_id
                        storage.put(mention)
                for evidence in storage.all(LocationEvidence):
                    if evidence.poi_candidate_id == candidate.id:
                        evidence.matched_poi_id = candidate.resolved_poi_id
                        storage.put(evidence)
            else:
                candidate.review_reasons = (["UNKNOWN_LOCATION_TYPE"] if is_threads and not poi_like else
                                            ["AMBIGUOUS_POI_MATCH"] if len(matches) > 1 else
                                            ["UNKNOWN_CATEGORY"])
                storage.put(candidate)
            continue
        if candidate.source_kind == "structured" and not valid_coordinates(candidate.latitude, candidate.longitude):
            candidate.review_reasons = ["MISSING_REQUIRED_FIELD" if candidate.latitude is None or candidate.longitude is None
                                        else "INVALID_COORDINATES"]
            storage.put(candidate)
            continue
        if (candidate.latitude is None) != (candidate.longitude is None) or (
            candidate.latitude is not None and not valid_coordinates(candidate.latitude, candidate.longitude)):
            candidate.review_reasons = ["INVALID_COORDINATES"]
            storage.put(candidate)
            continue
        # NYC CAMIS is the stable source identity. Avoid an O(n²) name/distance
        # scan across the entire Manhattan snapshot; the candidate id already
        # prevents duplicate creation on replay, while conflicts remain review-only.
        possibilities = [] if is_nyc_structured else possible_poi_matches(storage, candidate)
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
                website=candidate.website, opening_hours=candidate.opening_hours,
                last_verified_at=now, confidence_score=candidate.confidence_score,
                coordinates_verified=True, coordinate_precision=candidate.coordinate_precision))
            source_content = storage.get(SourceContent, candidate.source_content_id)
            fields = ["names", "latitude", "longitude", "category", "coordinate_precision"]
            if candidate.aliases and source_content.platform == "osm":
                fields.append("aliases")
            if candidate.address:
                fields.append("address")
            if candidate.website:
                fields.append("website")
            if candidate.opening_hours:
                fields.append("opening_hours")
            tag_key = "amenity" if "amenity" in candidate.source_tags else "tourism"
            geometry_prefix = ("center." if source_content.raw_metadata.get("osm_object_type") != "node"
                               else "")
            origin = {"names": "tags.name", "aliases": "tags.name:en", "latitude": geometry_prefix + "lat",
                      "longitude": geometry_prefix + "lon", "category": "tags." + tag_key,
                      "address": "tags.addr:full",
                      "website": "tags.website" if "website" in candidate.source_tags else "tags.contact:website",
                      "opening_hours": "tags.opening_hours", "coordinate_precision": "geometry"}
            for field_name in fields:
                source_field = (origin[field_name] if source_content.platform == "osm"
                                else candidate.field_sources.get(field_name))
                reference_url = (candidate.coordinate_source_url
                                 if field_name in ("latitude", "longitude") else None)
                storage.put(FieldProvenance(
                    id=stable_id("provenance", poi_id, field_name, candidate.source_content_id),
                    entity_type="POI", entity_id=poi_id, field_name=field_name,
                    source=source_content.platform,
                    source_content_id=candidate.source_content_id,
                    collected_at=source_content.collected_at, verified_at=now,
                    source_field=source_field, source_reference_url=reference_url))
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
        verification_hold = "LOCATION_VERIFICATION_HOLD" in poi.review_reasons
        poi.validation_passed = valid_coordinates(poi.latitude, poi.longitude) and bool(poi.names) and bool(poi.city)
        poi.quality_score = round(min(1.0, poi.confidence_score * (1.0 if poi.address else 0.9)), 3)
        if poi.review_status != "REJECTED":
            if verification_hold:
                poi.review_status = "NEEDS_REVIEW"
            else:
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
        if not mention.active:
            mention.review_reasons = ["SOURCE_CONTENT_UPDATED"]
            mention.review_status = "NEEDS_REVIEW"
            storage.put(mention)
            continue
        source_content = storage.get(SourceContent, mention.source_content_id)
        if source_content and source_content.content_status == "UNAVAILABLE":
            mention.review_reasons = ["SOURCE_UNAVAILABLE"]
            mention.review_status = "NEEDS_REVIEW"
            storage.put(mention)
            continue
        poi = storage.get(POI, mention.poi_id) if mention.poi_id else None
        candidate = storage.get(POICandidate, mention.poi_candidate_id) if mention.poi_candidate_id else None
        if candidate and candidate.review_status == "REJECTED":
            mention.review_reasons = ["REJECTED_CANDIDATE"]
        else:
            history = storage.review_history("POIMention", mention.id)
            human_approved = bool(history and history[-1].action == "APPROVE" and
                                  history[-1].new_status == "AUTO_APPROVED")
            signals = [signal for signal in storage.all(RecommendationSignal)
                       if signal.poi_mention_id == mention.id]
            possible_internal_venue = bool(
                poi and poi.category == "hotel" and source_content and
                source_content.platform not in ("osm", "official-directory") and
                any(signal.type in ("food", "drink") for signal in signals))
            mention.review_reasons = (
                [] if human_approved else
                ["POSSIBLE_INTERNAL_VENUE"] if possible_internal_venue else
                [] if poi and mention.confidence_score >= 0.7 else
                ["UNRESOLVED_POI" if not poi else "LOW_CONFIDENCE"])
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
                              review_status=(candidate.review_status if candidate.source_event_id else
                                             previous_event.review_status if previous_event else "NEEDS_REVIEW"),
                              review_reasons=(candidate.review_reasons if candidate.source_event_id else
                                              previous_event.review_reasons if previous_event else
                                              ["EVENT_REQUIRES_REVIEW"]),
                              source_event_id=candidate.source_event_id,
                              lifecycle_status=candidate.lifecycle_status,
                              event_url=candidate.event_url,
                              registration_url=candidate.registration_url,
                              registration_description=candidate.registration_description,
                              park_ids=list(candidate.park_ids), park_names=list(candidate.park_names),
                              location=candidate.location, categories=list(candidate.categories),
                              description=candidate.description, instructor=candidate.instructor,
                              contact_phone=candidate.contact_phone,
                              latitude=candidate.latitude, longitude=candidate.longitude,
                              timezone=candidate.timezone, field_sources=dict(candidate.field_sources)))
        elif candidate.source_event_id:
            # Keep the stable Event row, but never invent a time when the
            # source omitted or malformed one.  It remains internal review.
            previous_event = storage.get(Event, stable_id("event", candidate.id))
            if previous_event:
                previous_event.lifecycle_status = candidate.lifecycle_status
                previous_event.review_status = "NEEDS_REVIEW"
                previous_event.review_reasons = list(candidate.review_reasons or ["INVALID_EVENT_TIME"])
                storage.put(previous_event)
