"""Human review operations over the storage-neutral model."""
import re
import uuid
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from .models import (FieldProvenance, LocationEvidence, POI, POICandidate,
                     POIMention, RawSourceRecord, RecommendationSignal,
                     ReviewDecision, SourceContent)
from .processing import possible_poi_matches, valid_coordinates, validate_score_review
from .storage import Storage
from .util import stable_id, utc_now


REVIEW_TYPES = {"candidate": POICandidate, "mention": POIMention, "poi": POI}
REASON_EXPLANATIONS = {
    "AMBIGUOUS_POI_MATCH": "Multiple existing places match this candidate.",
    "INSUFFICIENT_LOCATION_EVIDENCE": "The source does not establish a specific place confidently.",
    "CONFLICTING_COORDINATES": "A name match has coordinates too far from this candidate.",
    "INVALID_COORDINATES": "Coordinates are missing a valid geographic range or pair.",
    "LOW_CONFIDENCE": "The relation confidence is below automatic approval threshold.",
    "MISSING_REQUIRED_FIELD": "A required place field is missing.",
    "UNRESOLVED_POI": "The mention is still linked only to a candidate.",
    "REJECTED_CANDIDATE": "The candidate linked to this mention was rejected.",
    "HUMAN_REJECTED": "A local reviewer rejected this item.",
    "UNKNOWN_CATEGORY": "The source tags do not map to a supported category.",
}

SECRET_PATTERN = re.compile(
    r"(?i)\b(?:access[_-]?token|token|cookie|password|passwd|secret|session)\s*[:=]\s*[^\s,;]+|\bBearer\s+[^\s,;]+")


def safe_text(value: Optional[str], limit: int = 800) -> Optional[str]:
    if value is None:
        return None
    return SECRET_PATTERN.sub("[REDACTED]", value[:limit])


def safe_display(value):
    if isinstance(value, dict):
        return {key: safe_display(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_display(item) for item in value]
    if isinstance(value, str):
        return safe_text(value)
    return value


def reason_details(codes: List[str]) -> List[dict]:
    return [{"code": code, "explanation": REASON_EXPLANATIONS.get(code, code.replace("_", " ").capitalize())}
            for code in codes]


class ReviewService:
    def __init__(self, storage: Storage, visibility_threshold: float):
        self.storage = storage
        self.visibility_threshold = visibility_threshold

    def _target(self, item_id: str) -> Tuple[str, object]:
        found = [(name, self.storage.get(model, item_id)) for name, model in REVIEW_TYPES.items()]
        matches = [(name, item) for name, item in found if item is not None]
        if len(matches) != 1:
            raise ValueError("review target not found or ambiguous: {}".format(item_id))
        return matches[0]

    def _city(self, kind: str, item: object) -> Optional[str]:
        if kind in ("candidate", "poi"):
            return item.city
        candidate = self.storage.get(POICandidate, item.poi_candidate_id) if item.poi_candidate_id else None
        if candidate:
            return candidate.city
        poi = self.storage.get(POI, item.poi_id) if item.poi_id else None
        return poi.city if poi else None

    def list(self, kind: Optional[str] = None, city: Optional[str] = None) -> Dict:
        if kind is not None and kind not in REVIEW_TYPES:
            raise ValueError("unsupported review type: {}".format(kind))
        items = []
        for name, model in REVIEW_TYPES.items():
            if kind is not None and name != kind:
                continue
            for item in self.storage.all(model):
                item_city = self._city(name, item)
                if item.review_status != "NEEDS_REVIEW" or (city and item_city != city):
                    continue
                label = item.name if name == "candidate" else (
                    next(iter(item.names.values()), "") if name == "poi" else item.recommendation_summary)
                items.append({"id": item.id, "type": name, "label": safe_text(label, 160),
                              "city": item_city, "confidence": item.confidence_score,
                              "reasons": reason_details(item.review_reasons)})
        items.sort(key=lambda item: (item["city"] or "", item["type"], item["id"]))
        return {"count": len(items), "items": items}

    def _source_context(self, content_id: str) -> Dict:
        content = self.storage.get(SourceContent, content_id)
        if content is None:
            raise ValueError("source content not found: {}".format(content_id))
        raw = self.storage.get(RawSourceRecord, content.raw_record_id)
        return {
            "source_content": {"id": content.id, "platform": content.platform,
                               "external_id": safe_text(content.external_id, 160),
                               "source_url": content.source_url,
                               "content_type": content.content_type,
                               "title": safe_text(content.title), "text": safe_text(content.text),
                               "published_at": content.published_at, "collected_at": content.collected_at},
            "raw_source_record": ({"id": raw.id, "retained": True, "source": raw.source,
                                   "external_id": safe_text(raw.external_id, 160),
                                   "source_url": raw.source_url, "collected_at": raw.collected_at,
                                   "expires_at": raw.expires_at,
                                   "retained_fields": sorted(raw.payload)} if raw else
                                  {"id": content.raw_record_id, "retained": False}),
        }

    def _evidence(self, candidate_id: str) -> List[dict]:
        return [asdict(value) for value in self.storage.all(LocationEvidence)
                if value.poi_candidate_id == candidate_id]

    def _history(self, target_type: str, target_id: str) -> List[dict]:
        result = []
        for decision in self.storage.review_history(target_type, target_id):
            value = asdict(decision)
            value["reason"] = safe_text(value["reason"])
            result.append(value)
        return safe_display(result)

    def show(self, item_id: str) -> Dict:
        kind, item = self._target(item_id)
        result = {"id": item.id, "type": kind, "status": item.review_status,
                  "reasons": reason_details(item.review_reasons), "detail": asdict(item),
                  "history": self._history(type(item).__name__, item.id)}
        if kind == "candidate":
            result["source"] = self._source_context(item.source_content_id)
            result["location_evidence"] = self._evidence(item.id)
            result["possible_poi_matches"] = [
                {"poi_id": match["poi"].id, "names": match["poi"].names,
                 "address": match["poi"].address, "category": match["poi"].category,
                 "latitude": match["poi"].latitude, "longitude": match["poi"].longitude,
                 "distance_meters": match["distance_meters"], "within_250m": match["within_250m"]}
                for match in possible_poi_matches(self.storage, item)]
            result["mention_ids"] = [mention.id for mention in self.storage.all(POIMention)
                                     if mention.poi_candidate_id == item.id]
        elif kind == "mention":
            result["source"] = self._source_context(item.source_content_id)
            candidate = self.storage.get(POICandidate, item.poi_candidate_id) if item.poi_candidate_id else None
            poi = self.storage.get(POI, item.poi_id) if item.poi_id else None
            result["candidate"] = ({"id": candidate.id, "name": candidate.name,
                                    "city": candidate.city, "status": candidate.review_status}
                                   if candidate else None)
            result["poi"] = ({"id": poi.id, "names": poi.names, "city": poi.city} if poi else None)
            result["recommendation_signals"] = [asdict(value) for value in self.storage.all(RecommendationSignal)
                                                if value.poi_mention_id == item.id]
            result["location_evidence"] = self._evidence(candidate.id) if candidate else []
        else:
            provenance = [value for value in self.storage.all(FieldProvenance)
                          if value.entity_type == "POI" and value.entity_id == item.id]
            result["provenance"] = [asdict(value) for value in provenance]
            linked_mentions = [mention for mention in self.storage.all(POIMention)
                               if mention.poi_id == item.id]
            linked_candidates = [candidate for candidate in self.storage.all(POICandidate)
                                 if candidate.resolved_poi_id == item.id]
            source_ids = sorted(
                {value.source_content_id for value in provenance if value.source_content_id} |
                {mention.source_content_id for mention in linked_mentions} |
                {candidate.source_content_id for candidate in linked_candidates})
            result["sources"] = [self._source_context(source_id) for source_id in source_ids]
            result["mention_ids"] = [mention.id for mention in linked_mentions]
            result["candidate_ids"] = [candidate.id for candidate in linked_candidates]
        return safe_display(result)

    def history(self, item_id: str) -> List[dict]:
        _kind, item = self._target(item_id)
        return self._history(type(item).__name__, item.id)

    def _decision(self, item: object, status: str, action: str, reason: str,
                  metadata: Optional[dict] = None) -> ReviewDecision:
        context = {"previous_review_reasons": list(item.review_reasons)}
        context.update(metadata or {})
        return ReviewDecision(id=uuid.uuid4().hex, target_type=type(item).__name__, target_id=item.id,
                              previous_status=item.review_status, new_status=status, action=action,
                              reason=reason, reviewer="local-user", created_at=utc_now(),
                              metadata=context)

    def approve(self, item_id: str, reason: str = "Confirmed by local reviewer") -> ReviewDecision:
        kind, item = self._target(item_id)
        now = utc_now()
        if kind == "candidate":
            if not item.resolved_poi_id:
                raise ValueError("unresolved candidate requires review resolve or review promote")
        elif kind == "mention":
            if not item.poi_id or not self.storage.get(POI, item.poi_id):
                raise ValueError("mention must be bound to an existing POI before approval")
            candidate = self.storage.get(POICandidate, item.poi_candidate_id) if item.poi_candidate_id else None
            if candidate and candidate.review_status == "REJECTED":
                raise ValueError("mention candidate is rejected")
        elif not valid_coordinates(item.latitude, item.longitude) or not item.names or not item.city:
            raise ValueError("POI is missing required valid fields")
        decision = self._decision(item, "AUTO_APPROVED", "APPROVE", reason)
        item.review_status = "AUTO_APPROVED"
        item.review_reasons = []
        if kind == "poi":
            item.coordinates_verified = True
            item.last_verified_at = now
            item.updated_at = now
        self.storage.apply_review([item], decision)
        validate_score_review(self.storage, self.visibility_threshold)
        return decision

    def reject(self, item_id: str, reason: str) -> ReviewDecision:
        if not reason or not reason.strip():
            raise ValueError("reject requires a nonempty reason")
        _kind, item = self._target(item_id)
        if item.review_status == "REJECTED":
            raise ValueError("duplicate review operation")
        decision = self._decision(item, "REJECTED", "REJECT", reason.strip())
        item.review_status = "REJECTED"
        item.review_reasons = ["HUMAN_REJECTED"]
        self.storage.apply_review([item], decision)
        validate_score_review(self.storage, self.visibility_threshold)
        return decision

    def resolve(self, candidate_id: str, poi_id: str, reason: str = "Matched to existing POI") -> ReviewDecision:
        candidate = self.storage.get(POICandidate, candidate_id)
        poi = self.storage.get(POI, poi_id)
        if candidate is None:
            raise ValueError("candidate not found: {}".format(candidate_id))
        if poi is None:
            raise ValueError("POI not found: {}".format(poi_id))
        if poi.review_status == "REJECTED":
            raise ValueError("rejected POI cannot receive candidate resolution")
        if candidate.resolved_poi_id:
            raise ValueError("candidate is already resolved")
        if candidate.review_status == "REJECTED":
            raise ValueError("rejected candidate cannot be resolved")
        if candidate.category == "unknown":
            raise ValueError("UNKNOWN_CATEGORY: candidate category requires correction before resolution")
        if (candidate.city, candidate.country, candidate.category) != (poi.city, poi.country, poi.category):
            raise ValueError("POI city, country or category does not match candidate")
        now = utc_now()
        decision = self._decision(candidate, "AUTO_APPROVED", "RESOLVE", reason,
                                  {"poi_id": poi_id})
        candidate.resolved_poi_id = poi_id
        candidate.review_status = "AUTO_APPROVED"
        candidate.review_reasons = []
        items: List[object] = [candidate]
        related_decisions = []
        for mention in self.storage.all(POIMention):
            if mention.poi_candidate_id == candidate_id:
                new_status = "AUTO_APPROVED" if mention.confidence_score >= 0.7 else "NEEDS_REVIEW"
                related_decisions.append(self._decision(
                    mention, new_status, "BIND", "Bound through candidate resolution",
                    {"candidate_id": candidate_id, "poi_id": poi_id}))
                mention.poi_id = poi_id
                mention.review_status = new_status
                mention.review_reasons = [] if new_status == "AUTO_APPROVED" else ["LOW_CONFIDENCE"]
                items.append(mention)
        for evidence in self.storage.all(LocationEvidence):
            if evidence.poi_candidate_id == candidate_id:
                evidence.matched_poi_id = poi_id
                items.append(evidence)
        content = self.storage.get(SourceContent, candidate.source_content_id)
        items.append(FieldProvenance(
            id=stable_id("review-binding", candidate_id, poi_id),
            entity_type="POICandidate", entity_id=candidate_id, field_name="resolved_poi_id",
            source="HUMAN_REVIEW", collected_at=content.collected_at,
            source_content_id=content.id, verified_at=now))
        self.storage.apply_review(items, decision, related_decisions)
        validate_score_review(self.storage, self.visibility_threshold)
        return decision

    def promote(self, candidate_id: str, reason: str = "Confirmed as a new POI") -> ReviewDecision:
        candidate = self.storage.get(POICandidate, candidate_id)
        if candidate is None:
            raise ValueError("candidate not found: {}".format(candidate_id))
        if candidate.resolved_poi_id:
            raise ValueError("candidate is already resolved")
        if candidate.review_status == "REJECTED":
            raise ValueError("rejected candidate cannot be promoted")
        if candidate.category == "unknown":
            raise ValueError("UNKNOWN_CATEGORY: candidate category requires correction before promotion")
        if not (candidate.name.strip() and candidate.city.strip() and candidate.country.strip() and
                candidate.category.strip()):
            raise ValueError("MISSING_REQUIRED_FIELD: candidate name, city, country or category")
        if not valid_coordinates(candidate.latitude, candidate.longitude):
            raise ValueError("INVALID_COORDINATES: promotion requires valid latitude and longitude")
        poi_id = stable_id("poi", candidate.id)
        if self.storage.get(POI, poi_id):
            raise ValueError("promoted POI already exists")
        now = utc_now()
        content = self.storage.get(SourceContent, candidate.source_content_id)
        poi = POI(id=poi_id, names={candidate.name_locale: candidate.name}, aliases=candidate.aliases,
                  latitude=candidate.latitude, longitude=candidate.longitude, address=candidate.address,
                  website=candidate.website,
                  city=candidate.city, country=candidate.country, category=candidate.category,
                  created_at=now, updated_at=now, last_verified_at=now,
                  confidence_score=max(candidate.confidence_score, self.visibility_threshold),
                  coordinates_verified=True, review_status="AUTO_APPROVED")
        decision = self._decision(candidate, "AUTO_APPROVED", "PROMOTE", reason,
                                  {"poi_id": poi_id})
        candidate.resolved_poi_id = poi_id
        candidate.coordinate_verified = True
        candidate.review_status = "AUTO_APPROVED"
        candidate.review_reasons = []
        items: List[object] = [poi, candidate]
        related_decisions = []
        for mention in self.storage.all(POIMention):
            if mention.poi_candidate_id == candidate_id:
                new_status = "AUTO_APPROVED" if mention.confidence_score >= 0.7 else "NEEDS_REVIEW"
                related_decisions.append(self._decision(
                    mention, new_status, "BIND", "Bound through candidate promotion",
                    {"candidate_id": candidate_id, "poi_id": poi_id}))
                mention.poi_id = poi_id
                mention.review_status = new_status
                mention.review_reasons = [] if new_status == "AUTO_APPROVED" else ["LOW_CONFIDENCE"]
                items.append(mention)
        for evidence in self.storage.all(LocationEvidence):
            if evidence.poi_candidate_id == candidate_id:
                evidence.matched_poi_id = poi_id
                items.append(evidence)
        fields = ["names", "latitude", "longitude", "address", "category"]
        if candidate.website:
            fields.append("website")
        geometry_prefix = "center." if content.raw_metadata.get("osm_object_type") in ("way", "relation") else ""
        origin = {"names": "tags.name", "latitude": geometry_prefix + "lat",
                  "longitude": geometry_prefix + "lon",
                  "category": "tags.amenity" if "amenity" in candidate.source_tags else "tags.tourism",
                  "address": "tags.addr:full",
                  "website": "tags.website" if "website" in candidate.source_tags else "tags.contact:website"}
        for field_name in fields:
            items.append(FieldProvenance(
                id=stable_id("provenance", poi_id, field_name, content.id),
                entity_type="POI", entity_id=poi_id, field_name=field_name,
                source=content.platform, collected_at=content.collected_at,
                source_content_id=content.id, verified_at=now,
                source_field=origin[field_name] if content.platform == "osm" else None))
        self.storage.apply_review(items, decision, related_decisions)
        validate_score_review(self.storage, self.visibility_threshold)
        return decision
