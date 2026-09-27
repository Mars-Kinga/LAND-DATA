"""Minimal orchestrator. All persistence goes through Storage."""
from typing import Dict, Iterable, Optional

from .adapters import SourceAdapter
from .config import Config
from .enrichment import EnrichmentPlan, enrich
from .geoscope import GeoScope
from .llm_extraction import LLMExtractionService
from .models import (Event, EventCandidate, LocationEvidence, POI, POICandidate,
                     POIMention, RawSourceRecord, RecommendationSignal, SourceContent)
from .policy import SourcePolicy
from .processing import extract, normalize, resolve, validate_score_review
from .social_processing import process_social
from .storage import Storage
from .util import safe_source_url, stable_id, utc_now


class Pipeline:
    def __init__(self, storage: Storage, config: Config,
                 llm_service: Optional[LLMExtractionService] = None):
        self.storage = storage
        self.config = config
        self._transient_by_raw_id: Dict[str, Dict] = {}
        self.last_enrichment_result = None
        self.llm_service = llm_service

    def collect(self, city: str, adapters: Iterable[SourceAdapter],
                scope: Optional[GeoScope] = None) -> int:
        if scope is not None and city != scope.name:
            raise ValueError("collection key must match GeoScope name")
        adapters = list(adapters)
        if scope is None and city not in self.config.cities and any(adapter.name != "threads" for adapter in adapters):
            raise ValueError("unknown city: {}".format(city))
        self.storage.purge_expired_raw(utc_now())
        count = 0
        for adapter in adapters:
            policy = self.config.sources[adapter.name].policy
            for item in adapter.collect(city):
                if (item.payload.get("content_status") == "UNAVAILABLE" and
                        self.storage.get(SourceContent, stable_id(item.source, item.external_id)) is None):
                    continue
                now = utc_now()
                payload = policy.retained_payload(item.payload)
                source_url = safe_source_url(item.source_url) if item.source_url and policy.retain_source_url else None
                identity_payload = (adapter.raw_identity_payload(payload)
                                    if hasattr(adapter, "raw_identity_payload") else payload)
                record = RawSourceRecord(
                    id=stable_id("raw", item.source, item.external_id, source_url, identity_payload),
                    source=item.source, external_id=item.external_id, source_url=source_url, city=city,
                    payload=payload, collected_at=now, expires_at=policy.expiry(now))
                self.storage.put(record)
                # Only this process can use temporary fields; they never enter storage.
                self._transient_by_raw_id[record.id] = policy.temporary_payload(item.payload)
                count += 1
        return count

    def _parse_one(self, record: RawSourceRecord, policy: SourcePolicy,
                   transient: Optional[Dict] = None) -> None:
        content = normalize(record, policy)
        previous = self.storage.get(SourceContent, content.id)
        if self.config.sources[record.source].processing_kind == "social":
            if content.content_status not in ("ACTIVE", "UPDATED", "UNAVAILABLE"):
                raise ValueError("invalid social content status")
            fields = ("title", "text", "geo_tag", "hashtags", "media_metadata",
                      "language", "author_identifier", "published_at")
            if (previous and content.content_status == "ACTIVE" and
                    (previous.content_status == "UNAVAILABLE" or
                     any(getattr(previous, field) != getattr(content, field) for field in fields) or
                     previous.raw_metadata != content.raw_metadata)):
                content.content_status = "UPDATED"
            elif previous and previous.content_status == "UPDATED" and content.content_status == "ACTIVE":
                content.content_status = "UPDATED"
            if previous and content.content_status == "UNAVAILABLE":
                content.source_url = previous.source_url
                content.canonical_url = previous.canonical_url
                content.published_at = previous.published_at
        self.storage.put(content)
        if self.config.sources[record.source].processing_kind == "social":
            process_social(content, self.storage, record.payload, transient,
                           self.llm_service, policy.retention_days)
        else:
            extract(content, self.storage, record.payload,
                    self.config.sources[record.source].allow_coordinate_verification, transient)

    def parse(self, city: str, raw_ids=None) -> int:
        self.storage.purge_expired_raw(utc_now())
        count = 0
        latest = {}
        for record in self.storage.all(RawSourceRecord):
            if record.city == city and (raw_ids is None or record.id in raw_ids):
                key = (record.source, record.external_id)
                if key not in latest or record.collected_at > latest[key].collected_at:
                    latest[key] = record
        for record in latest.values():
            if record.source not in self.config.sources:
                continue
            self._parse_one(record, self.config.sources[record.source].policy,
                            self._transient_by_raw_id.get(record.id))
            count += 1
        return count

    def validate(self, enrichment_plan: Optional[EnrichmentPlan] = None,
                 only_candidate_ids=None) -> None:
        if enrichment_plan is not None:
            self.last_enrichment_result = enrich(self, enrichment_plan)
        resolve(self.storage, only_candidate_ids=only_candidate_ids)
        validate_score_review(self.storage, self.config.visibility_threshold)

    def review_queue(self) -> Dict[str, list]:
        types = (POICandidate, POIMention, POI, EventCandidate, Event)
        return {model.__name__: [{"id": item.id, "reasons": getattr(item, "review_reasons", [])}
                                  for item in self.storage.all(model)
                                  if item.review_status == "NEEDS_REVIEW"] for model in types}

    def counts(self) -> Dict[str, int]:
        types = (RawSourceRecord, SourceContent, POICandidate, POI, POIMention,
                 RecommendationSignal, LocationEvidence, EventCandidate, Event)
        return {model.__name__: len(self.storage.all(model)) for model in types}

    def run(self, city: str, adapters: Iterable[SourceAdapter],
            scope: Optional[GeoScope] = None,
            enrichment_plan: Optional[EnrichmentPlan] = None) -> Dict[str, int]:
        self.collect(city, adapters, scope)
        self.parse(city)
        self.validate(enrichment_plan)
        return self.counts()
