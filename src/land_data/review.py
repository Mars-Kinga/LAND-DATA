"""Human review operations over the storage-neutral model."""
import re
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .models import (Event, EventCandidate, FieldProvenance,
                     LocationEvidence, OfficialGuidancePageState,
                     OfficialGuidanceSnapshot, POI, POICandidate, POIMention,
                     RawSourceRecord, RecommendationSignal, ReviewDecision,
                     SourceContent, SourceDiscoveryCandidate)
from .processing import possible_poi_matches, valid_coordinates, validate_score_review
from .storage import Storage
from .util import stable_id, utc_now


# These short names are the stable API used by the local dashboard.  Keep the
# original three names unchanged for the existing CLI and integrations.
REVIEW_TYPES = {
    "candidate": POICandidate,
    "mention": POIMention,
    "poi": POI,
    "event_candidate": EventCandidate,
    "event": Event,
    "official_guidance": OfficialGuidanceSnapshot,
    "source_discovery": SourceDiscoveryCandidate,
}
REVIEW_TYPE_ALIASES = {
    "EventCandidate": "event_candidate",
    "Event": "event",
    "OfficialGuidanceSnapshot": "official_guidance",
    "SourceDiscoveryCandidate": "source_discovery",
}
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
    "UNKNOWN_LOCATION_TYPE": "The source location cannot be confirmed as a specific POI.",
    "SOURCE_UNAVAILABLE": "The source content is unavailable and its recommendation cannot be published.",
    "SOURCE_CONTENT_UPDATED": "The updated source no longer supports this recommendation.",
    "EVENT_REQUIRES_REVIEW": "The event has not been approved by a local reviewer.",
    "EVENT_SOURCE_UPDATED": "The event source changed and needs a fresh review.",
    "EVENT_REAPPEARED": "The event reappeared after being absent from a complete source snapshot.",
    "INVALID_EVENT_TIME": "The event start and end times are missing or inconsistent.",
    "EVENT_UNAVAILABLE": "The event source is unavailable or the event is no longer active.",
    "OFFICIAL_GUIDANCE_REQUIRES_REVIEW": "The official guidance snapshot is waiting for local review.",
    "OFFICIAL_GUIDANCE_CONTENT_CHANGED": "The official guidance content changed and needs review.",
    "SOURCE_METADATA_UPDATED": "The discovered source metadata changed and needs review.",
    "HUMAN_REJECTED_SOURCE": "A local reviewer rejected this source candidate.",
    "HUMAN_ACCEPTED_SOURCE": "A local reviewer accepted this source as a supported input; this does not publish its content.",
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

    @staticmethod
    def _kind(kind: str) -> str:
        kind = REVIEW_TYPE_ALIASES.get(kind, kind)
        if kind not in REVIEW_TYPES:
            raise ValueError("unsupported review type: {}".format(kind))
        return kind

    @staticmethod
    def _status(item: object) -> str:
        """Return the review state without conflating source lifecycle state.

        SourceDiscoveryCandidate predates the review API and uses ``status``
        for its catalog lifecycle.  Its values are deliberately kept intact:
        SUPPORTED means that the source is accepted for future ingestion, not
        that any records from it are approved for public release.
        """
        return getattr(item, "review_status", getattr(item, "status", ""))

    @staticmethod
    def _review_reasons(item: object) -> List[str]:
        return list(getattr(item, "review_reasons", getattr(item, "reasons", [])) or [])

    @staticmethod
    def _set_status(item: object, status: str) -> None:
        if isinstance(item, SourceDiscoveryCandidate):
            item.status = status
        else:
            item.review_status = status

    @staticmethod
    def _set_reasons(item: object, reasons: List[str]) -> None:
        if isinstance(item, SourceDiscoveryCandidate):
            item.reasons = reasons
        else:
            item.review_reasons = reasons

    def _label(self, kind: str, item: object) -> str:
        if kind in ("candidate", "event_candidate", "event"):
            return item.name
        if kind == "poi":
            return next(iter(item.names.values()), "")
        if kind == "mention":
            return item.recommendation_summary
        if kind == "official_guidance":
            return "{}: {}".format(item.page_key, item.section_location)
        if kind == "source_discovery":
            return item.title or item.dataset_id
        return item.id

    def _confidence(self, kind: str, item: object) -> Optional[float]:
        if kind == "event_candidate":
            return item.extraction_confidence
        return getattr(item, "confidence_score", None)

    def _pending(self, kind: str, item: object) -> bool:
        status = self._status(item)
        if kind == "source_discovery":
            return status in ("DISCOVERED", "NEEDS_REVIEW")
        return status == "NEEDS_REVIEW"

    def _row(self, kind: str, item: object) -> Dict:
        status = self._status(item)
        return {"id": item.id, "type": kind, "label": safe_text(self._label(kind, item), 160),
                "city": self._city(kind, item), "status": status,
                "confidence": self._confidence(kind, item),
                "reasons": reason_details(self._review_reasons(item))}

    def _city(self, kind: str, item: object) -> Optional[str]:
        if kind in ("candidate", "poi"):
            return item.city
        candidate = self.storage.get(POICandidate, item.poi_candidate_id) if getattr(item, "poi_candidate_id", None) else None
        if candidate:
            return candidate.city
        poi = self.storage.get(POI, item.poi_id) if getattr(item, "poi_id", None) else None
        if poi:
            return poi.city
        if kind in ("event_candidate", "event"):
            content = self.storage.get(SourceContent, item.source_content_id)
            return content.collection_scope if content else None
        return None

    def list(self, kind: Optional[str] = None, city: Optional[str] = None) -> Dict:
        if kind is not None:
            kind = self._kind(kind)
        items = []
        for name, model in REVIEW_TYPES.items():
            if kind is not None and name != kind:
                continue
            for item in self.storage.all(model):
                item_city = self._city(name, item)
                if not self._pending(name, item) or (city and item_city != city):
                    continue
                items.append(self._row(name, item))
        items.sort(key=lambda item: (item["city"] or "", item["type"], item["id"]))
        return {"count": len(items), "items": items}

    def list_all(self, kind: Optional[str] = None, city: Optional[str] = None,
                 status: Optional[str] = None) -> Dict:
        """Return every reviewable object, including completed decisions.

        The existing ``list`` method remains a pending-review queue.  The
        dashboard uses this method to make automatic approvals, rejections,
        source support decisions, and pending items visible in one place.
        """
        if kind is not None:
            kind = self._kind(kind)
        items = []
        for name, model in REVIEW_TYPES.items():
            if kind is not None and name != kind:
                continue
            for item in self.storage.all(model):
                item_city = self._city(name, item)
                item_status = self._status(item)
                if status is not None and item_status != status:
                    continue
                if city and item_city != city:
                    continue
                items.append(self._row(name, item))
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
        result = {"id": item.id, "type": kind, "status": self._status(item),
                  "reasons": reason_details(self._review_reasons(item)), "detail": asdict(item),
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
        elif kind == "poi":
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
        elif kind in ("event_candidate", "event"):
            result["source"] = self._source_context(item.source_content_id)
            if kind == "event_candidate":
                venue = (self.storage.get(POICandidate, item.venue_candidate_id)
                         if item.venue_candidate_id else None)
                result["venue_candidate"] = ({"id": venue.id, "name": venue.name,
                                               "status": venue.review_status,
                                               "resolved_poi_id": venue.resolved_poi_id}
                                              if venue else None)
                event = self.storage.get(Event, stable_id("event", item.id))
                result["event_id"] = event.id if event else None
            else:
                venue = self.storage.get(POI, item.venue_poi_id) if item.venue_poi_id else None
                result["venue_poi"] = ({"id": venue.id, "names": venue.names,
                                         "city": venue.city, "status": venue.review_status}
                                        if venue else None)
                candidates = [candidate for candidate in self.storage.all(EventCandidate)
                              if stable_id("event", candidate.id) == item.id]
                result["event_candidate_id"] = candidates[0].id if candidates else None
        elif kind == "official_guidance":
            state = next((value for value in self.storage.all(OfficialGuidancePageState)
                          if value.source == item.source and value.page_key == item.page_key), None)
            result["page_state"] = asdict(state) if state else None
            result["versions"] = [asdict(value) for value in self.storage.all(OfficialGuidanceSnapshot)
                                  if value.source == item.source and value.page_key == item.page_key]
        elif kind == "source_discovery":
            # This is intentionally explicit for the dashboard and API
            # consumers: supporting a source does not publish its contents.
            result["approval_semantics"] = {
                "source_supported": self._status(item) == "SUPPORTED",
                "public_content_approved": False,
                "description": "Accepting this catalog source only permits future source integration; each collected record still follows its own review and publication gates.",
            }
        return safe_display(result)

    def history(self, item_id: str) -> List[dict]:
        _kind, item = self._target(item_id)
        return self._history(type(item).__name__, item.id)

    @staticmethod
    def _reviewer(value: Optional[str]) -> str:
        if value is None:
            return "local-user"
        value = str(value).strip()
        if not value:
            raise ValueError("reviewer must be nonempty")
        return value[:160]

    def _decision(self, item: object, status: str, action: str, reason: str,
                  metadata: Optional[dict] = None,
                  reviewer: Optional[str] = "local-user") -> ReviewDecision:
        context = {"previous_review_reasons": self._review_reasons(item)}
        context.update(metadata or {})
        return ReviewDecision(id=uuid.uuid4().hex, target_type=type(item).__name__, target_id=item.id,
                              previous_status=self._status(item), new_status=status, action=action,
                              reason=reason, reviewer=self._reviewer(reviewer), created_at=utc_now(),
                              metadata=context)

    def _validate_event(self, item: object) -> None:
        if not item.name or not item.name.strip():
            raise ValueError("MISSING_REQUIRED_FIELD: event name")
        if item.lifecycle_status != "ACTIVE":
            raise ValueError("EVENT_UNAVAILABLE: only active events can be approved")
        content = self.storage.get(SourceContent, item.source_content_id)
        if content is None:
            raise ValueError("source content not found: {}".format(item.source_content_id))
        if content.content_status == "UNAVAILABLE":
            raise ValueError("EVENT_UNAVAILABLE: source content is unavailable")
        if not item.starts_at or not item.ends_at:
            raise ValueError("INVALID_EVENT_TIME: event approval requires start and end times")
        try:
            starts_at = datetime.fromisoformat(str(item.starts_at).replace("Z", "+00:00"))
            ends_at = datetime.fromisoformat(str(item.ends_at).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_EVENT_TIME: event timestamps must be ISO-8601") from exc
        try:
            valid_order = ends_at >= starts_at
        except TypeError as exc:
            raise ValueError("INVALID_EVENT_TIME: event timestamps use incompatible time zones") from exc
        if not valid_order:
            raise ValueError("INVALID_EVENT_TIME: event end must not precede start")
        if isinstance(item, EventCandidate) and item.venue_candidate_id:
            venue_candidate = self.storage.get(POICandidate, item.venue_candidate_id)
            if venue_candidate is None or not venue_candidate.resolved_poi_id:
                raise ValueError("UNRESOLVED_POI: event venue candidate must resolve to a POI")
            venue = self.storage.get(POI, venue_candidate.resolved_poi_id)
            if venue is None or venue.review_status == "REJECTED":
                raise ValueError("UNRESOLVED_POI: event venue POI is unavailable")
        if isinstance(item, Event) and item.venue_poi_id:
            venue = self.storage.get(POI, item.venue_poi_id)
            if venue is None or venue.review_status == "REJECTED":
                raise ValueError("UNRESOLVED_POI: event venue POI is unavailable")

    def _validate_guidance(self, item: OfficialGuidanceSnapshot) -> None:
        parsed = urlsplit(item.source_url or "")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("OFFICIAL_GUIDANCE_INVALID_SOURCE: source URL must be HTTPS")
        if not item.source or not item.page_key or not item.content_version:
            raise ValueError("MISSING_REQUIRED_FIELD: official guidance identity is incomplete")
        if not item.content or not item.content.strip():
            raise ValueError("OFFICIAL_GUIDANCE_EMPTY: official guidance has no stable content")
        state = next((value for value in self.storage.all(OfficialGuidancePageState)
                      if value.source == item.source and value.page_key == item.page_key), None)
        if state is not None:
            if state.current_snapshot_id != item.id:
                raise ValueError("OFFICIAL_GUIDANCE_STALE: only the current snapshot can be approved")
            if state.availability != "AVAILABLE" or state.parse_status != "OK":
                raise ValueError("OFFICIAL_GUIDANCE_UNAVAILABLE: page is not currently available and parsed")

    def _validate_source_candidate(self, item: SourceDiscoveryCandidate) -> None:
        parsed = urlsplit(item.api_endpoint or "")
        if parsed.scheme != "https" or parsed.hostname != item.domain or parsed.username or parsed.password:
            raise ValueError("SOURCE_INVALID_ENDPOINT: source API endpoint must be HTTPS on the discovered domain")
        if not item.provider or not item.dataset_id or not item.title or not item.domain:
            raise ValueError("MISSING_REQUIRED_FIELD: source candidate identity is incomplete")

    def approve(self, item_id: str, reason: str = "Confirmed by local reviewer",
                reviewer: Optional[str] = "local-user") -> ReviewDecision:
        kind, item = self._target(item_id)
        reviewer = self._reviewer(reviewer)
        now = utc_now()
        if self._status(item) == "REJECTED":
            raise ValueError("rejected review target cannot be approved")
        if kind == "candidate":
            if not item.resolved_poi_id:
                raise ValueError("unresolved candidate requires review resolve or review promote")
        elif kind == "mention":
            if not item.poi_id or not self.storage.get(POI, item.poi_id):
                raise ValueError("mention must be bound to an existing POI before approval")
            candidate = self.storage.get(POICandidate, item.poi_candidate_id) if item.poi_candidate_id else None
            if candidate and candidate.review_status == "REJECTED":
                raise ValueError("mention candidate is rejected")
        elif kind == "poi" and (not valid_coordinates(item.latitude, item.longitude) or
                                 not item.names or not item.city):
            raise ValueError("POI is missing required valid fields")
        elif kind in ("event_candidate", "event"):
            self._validate_event(item)
        elif kind == "official_guidance":
            self._validate_guidance(item)
        elif kind == "source_discovery":
            self._validate_source_candidate(item)
        new_status = "SUPPORTED" if kind == "source_discovery" else "AUTO_APPROVED"
        action = "APPROVE_SOURCE" if kind == "source_discovery" else "APPROVE"
        metadata = ({"approval_semantics": "SOURCE_SUPPORT_ONLY",
                     "public_content_approved": False}
                    if kind == "source_discovery" else None)
        decision = self._decision(item, new_status, action, reason, metadata, reviewer)
        self._set_status(item, new_status)
        self._set_reasons(item, ([*self._review_reasons(item), "HUMAN_ACCEPTED_SOURCE"]
                                 if kind == "source_discovery" else []))
        if kind == "poi":
            item.coordinates_verified = True
            item.last_verified_at = now
            item.updated_at = now
        self.storage.apply_review([item], decision)
        if kind in ("candidate", "mention", "poi"):
            validate_score_review(self.storage, self.visibility_threshold)
        return decision

    def reject(self, item_id: str, reason: str, reviewer: Optional[str] = "local-user") -> ReviewDecision:
        if not reason or not reason.strip():
            raise ValueError("reject requires a nonempty reason")
        _kind, item = self._target(item_id)
        reviewer = self._reviewer(reviewer)
        if self._status(item) == "REJECTED":
            raise ValueError("duplicate review operation")
        decision = self._decision(item, "REJECTED", "REJECT", reason.strip(), reviewer=reviewer)
        self._set_status(item, "REJECTED")
        self._set_reasons(item, (["HUMAN_REJECTED_SOURCE"] if _kind == "source_discovery"
                                 else ["HUMAN_REJECTED"]))
        self.storage.apply_review([item], decision)
        if _kind in ("candidate", "mention", "poi"):
            validate_score_review(self.storage, self.visibility_threshold)
        return decision

    def resolve(self, candidate_id: str, poi_id: str, reason: str = "Matched to existing POI",
                reviewer: Optional[str] = "local-user") -> ReviewDecision:
        candidate = self.storage.get(POICandidate, candidate_id)
        poi = self.storage.get(POI, poi_id)
        reviewer = self._reviewer(reviewer)
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
        content = self.storage.get(SourceContent, candidate.source_content_id)
        threads_unknown = candidate.category == "unknown" and content and content.platform == "threads"
        if candidate.category == "unknown" and not threads_unknown:
            raise ValueError("UNKNOWN_CATEGORY: candidate category requires correction before resolution")
        if (candidate.city, candidate.country) != (poi.city, poi.country) or (
                candidate.category != poi.category and not threads_unknown):
            raise ValueError("POI city, country or category does not match candidate")
        now = utc_now()
        decision = self._decision(candidate, "AUTO_APPROVED", "RESOLVE", reason,
                                  {"poi_id": poi_id}, reviewer)
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
                    {"candidate_id": candidate_id, "poi_id": poi_id}, reviewer))
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

    def promote(self, candidate_id: str, reason: str = "Confirmed as a new POI",
                reviewer: Optional[str] = "local-user") -> ReviewDecision:
        candidate = self.storage.get(POICandidate, candidate_id)
        reviewer = self._reviewer(reviewer)
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
                                  {"poi_id": poi_id}, reviewer)
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
                    {"candidate_id": candidate_id, "poi_id": poi_id}, reviewer))
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
