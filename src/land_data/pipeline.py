"""Minimal orchestrator. All persistence goes through Storage."""
from typing import Dict, Iterable, Optional

from .adapters import SourceAdapter
from .config import Config
from .models import (Event, EventCandidate, LocationEvidence, POI, POICandidate,
                     POIMention, RawSourceRecord, RecommendationSignal, SourceContent)
from .policy import SourcePolicy
from .processing import extract, normalize, resolve, validate_score_review
from .storage import Storage
from .util import safe_source_url, stable_id, utc_now


class Pipeline:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config
        self._transient_by_raw_id: Dict[str, Dict] = {}

    def collect(self, city: str, adapters: Iterable[SourceAdapter]) -> int:
        if city not in self.config.cities:
            raise ValueError("unknown city: {}".format(city))
        self.storage.purge_expired_raw(utc_now())
        count = 0
        for adapter in adapters:
            policy = self.config.sources[adapter.name].policy
            for item in adapter.collect(city):
                now = utc_now()
                payload = policy.retained_payload(item.payload)
                source_url = safe_source_url(item.source_url) if policy.retain_source_url else None
                record = RawSourceRecord(
                    id=stable_id("raw", item.source, item.external_id, source_url, payload),
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
        self.storage.put(content)
        extract(content, self.storage, record.payload,
                self.config.sources[record.source].allow_coordinate_verification, transient)

    def parse(self, city: str) -> int:
        self.storage.purge_expired_raw(utc_now())
        count = 0
        for record in self.storage.all(RawSourceRecord):
            if record.source not in self.config.sources:
                continue
            if record.city == city:
                self._parse_one(record, self.config.sources[record.source].policy,
                                self._transient_by_raw_id.get(record.id))
                count += 1
        return count

    def validate(self) -> None:
        resolve(self.storage)
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

    def run(self, city: str, adapters: Iterable[SourceAdapter]) -> Dict[str, int]:
        self.collect(city, adapters)
        self.parse(city)
        self.validate()
        return self.counts()
