"""Independent, scheduler-friendly orchestration for configured sources."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional
import json

from .adapters import SourceAdapter, SourceItem
from .geoscope import load_scope
from .models import POICandidate, RawSourceRecord, SourceContent, SourceRunRecord
from .pipeline import Pipeline
from .util import safe_source_url, stable_id, utc_now


@dataclass(frozen=True)
class MultiSourceSpec:
    name: str
    collection_key: str
    scope: object = None
    categories: tuple = ()
    borough: Optional[str] = None
    limit: int = 20
    timeout: int = 15
    max_pages: int = 1
    initial_updated_at: Optional[str] = None
    overlap_seconds: int = 86400
    safety_rescan_days: int = 7
    endpoints: tuple = ()
    max_retries: int = 2
    city: Optional[str] = None
    auto_collect: bool = False


class _CountingAdapter:
    def __init__(self, adapter: SourceAdapter, storage, spec: MultiSourceSpec, pipeline: Pipeline):
        self.adapter = adapter
        self.storage = storage
        self.spec = spec
        self.name = adapter.name
        self.capability = adapter.capability
        self.new_count = 0
        self.updated_count = 0
        self.duplicate_count = 0
        self.collection_stats = {}
        self.raw_record_ids = set()
        self._known_ids = {item.id for item in storage.all(RawSourceRecord)
                           if item.source == adapter.name}
        self._known_external_ids = {item.external_id for item in storage.all(RawSourceRecord)
                                    if item.source == adapter.name}
        self._policy = pipeline.config.sources[adapter.name].policy

    def collect(self, city: str) -> Iterable[SourceItem]:
        for item in self.adapter.collect(city):
            payload = self._policy.retained_payload(item.payload)
            source_url = safe_source_url(item.source_url) if item.source_url and self._policy.retain_source_url else None
            identity_payload = (self.adapter.raw_identity_payload(payload)
                                if hasattr(self.adapter, "raw_identity_payload") else payload)
            raw_id = stable_id("raw", item.source, item.external_id, source_url, identity_payload)
            self.raw_record_ids.add(raw_id)
            if raw_id in self._known_ids:
                self.duplicate_count += 1
            elif item.external_id in self._known_external_ids:
                self.updated_count += 1
            else:
                self.new_count += 1
            self._known_ids.add(raw_id)
            self._known_external_ids.add(item.external_id)
            yield item
        self.collection_stats = getattr(self.adapter, "last_collection_stats", {})

    def raw_identity_payload(self, payload):
        hook = getattr(self.adapter, "raw_identity_payload", None)
        return hook(payload) if hook else payload


def load_specs(path: str, root: Path, only_sources: Optional[Iterable[str]] = None) -> List[MultiSourceSpec]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    allowed = set(only_sources or ())
    specs = []
    for value in data.get("sources", []):
        if value.get("enabled", True) is not True:
            continue
        name = value["name"]
        if allowed and name not in allowed:
            continue
        scope = value.get("scope")
        specs.append(MultiSourceSpec(
            name=name, collection_key=value["collection_key"],
            scope=load_scope(str((root / scope).resolve())) if scope else None,
            categories=tuple(value.get("categories", ())),
            borough=value.get("borough"), limit=int(value.get("limit", 20)),
            timeout=int(value.get("timeout", 15)), max_pages=int(value.get("max_pages", 1)),
            initial_updated_at=value.get("initial_updated_at"),
            overlap_seconds=int(value.get("overlap_seconds", 86400)),
            safety_rescan_days=int(value.get("safety_rescan_days", 7)),
            endpoints=tuple(value.get("endpoints", ())),
            max_retries=int(value.get("max_retries", 2)),
            city=value.get("city"), auto_collect=value.get("auto_collect", False) is True))
    if not specs:
        raise ValueError("multi-source configuration selected no enabled sources")
    return specs


class MultiSourceRunner:
    def __init__(self, pipeline: Pipeline, storage, specs: Iterable[MultiSourceSpec],
                 adapter_factory: Callable[[MultiSourceSpec], SourceAdapter]):
        self.pipeline = pipeline
        self.storage = storage
        self.specs = list(specs)
        self.adapter_factory = adapter_factory

    def run(self) -> Dict:
        multi_run_id = stable_id("multi-source-run", utc_now())
        outcomes = []
        for spec in self.specs:
            started = utc_now()
            run = SourceRunRecord(
                id=stable_id("source-run", multi_run_id, spec.name),
                multi_run_id=multi_run_id, source=spec.name,
                collection_key=spec.collection_key, started_at=started,
                ended_at=None)
            self.storage.put(run)
            counted = None
            adapter = None
            counts = {}
            try:
                adapter = self.adapter_factory(spec)
                if hasattr(adapter, "run_guidance"):
                    counts = adapter.run_guidance(self.storage, spec.collection_key)
                    run.new_count = counts["new_count"]
                    run.updated_count = counts["updated_count"]
                    run.duplicate_count = counts["duplicate_count"]
                else:
                    counted = _CountingAdapter(adapter, self.storage, spec, self.pipeline)
                    self.pipeline.collect(spec.collection_key, [counted], spec.scope)
                    parse_raw_ids = counted.raw_record_ids
                    if getattr(adapter, "parse_all_source_records", False):
                        parse_raw_ids = {record.id for record in self.storage.all(RawSourceRecord)
                                         if record.source == adapter.name and
                                         record.city == spec.collection_key}
                    self.pipeline.parse(spec.collection_key, raw_ids=parse_raw_ids)
                    # Rolling structured snapshots may need to mark records
                    # absent from a source.  The adapter decides whether its
                    # page walk really completed; this hook is never called
                    # as a substitute for a failed/partial collection.
                    if hasattr(adapter, "finalize_snapshot"):
                        adapter.finalize_snapshot(self.storage, spec.collection_key)
                    raw_ids = counted.raw_record_ids
                    candidate_ids = {candidate.id for candidate in self.storage.all(POICandidate)
                                     if self.storage.get(SourceContent, candidate.source_content_id)
                                     and self.storage.get(SourceContent, candidate.source_content_id).raw_record_id in raw_ids}
                    self.pipeline.validate(only_candidate_ids=candidate_ids)
                    counts = self.pipeline.counts()
                    run.new_count = counted.new_count
                    run.updated_count = counted.updated_count
                    run.duplicate_count = counted.duplicate_count
                collection = counts.get("collection", {}) if isinstance(counts, dict) else {}
                if counted and counted.collection_stats:
                    collection = counted.collection_stats
                run.status = counts.get("run_status", collection.get("run_status", "SUCCEEDED")) if hasattr(adapter, "run_guidance") else collection.get("run_status", "SUCCEEDED")
                run.next_run_status = counts.get("next_run_status", collection.get("next_run_status", "NOT_NEEDED")) if hasattr(adapter, "run_guidance") else collection.get("next_run_status", "NOT_NEEDED")
                result = {"source": spec.name, "collection_key": spec.collection_key,
                          "status": run.status, "started_at": started,
                          "ended_at": None, "new_count": run.new_count, "updated_count": run.updated_count,
                          "duplicate_count": run.duplicate_count, "failure_reason": None,
                          "next_run_status": run.next_run_status, "counts": counts,
                          **({"parse_error_count": counts["parse_error_count"]}
                             if isinstance(counts, dict) and "parse_error_count" in counts else {}),
                          **({"collection": counts["collection"]} if isinstance(counts, dict) and "collection" in counts else {}),
                          **({"collection": counted.collection_stats} if counted and counted.collection_stats else {})}
            except Exception as exc:  # isolate one adapter/pipeline from the rest
                if counted is not None:
                    run.new_count = counted.new_count
                    run.updated_count = counted.updated_count
                    run.duplicate_count = counted.duplicate_count
                elif adapter is not None and hasattr(adapter, "last_guidance_counts"):
                    run.new_count = adapter.last_guidance_counts["new_count"]
                    run.updated_count = adapter.last_guidance_counts["updated_count"]
                    run.duplicate_count = adapter.last_guidance_counts["duplicate_count"]
                run.status = "FAILED"
                run.failure_reason = "{}: {}".format(type(exc).__name__, str(exc))
                run.next_run_status = "RETRY_REQUIRED"
                failed_collection = getattr(adapter, "last_collection_stats", {}) if adapter is not None else {}
                result = {"source": spec.name, "collection_key": spec.collection_key,
                          "status": run.status, "started_at": started,
                          "ended_at": None, "new_count": run.new_count, "updated_count": run.updated_count,
                          "duplicate_count": run.duplicate_count,
                          "failure_reason": run.failure_reason,
                          "next_run_status": run.next_run_status,
                          **({"collection": counted.collection_stats} if counted and counted.collection_stats else
                             ({"collection": failed_collection} if failed_collection else {}))}
            ended = utc_now()
            run.ended_at = ended
            collection = (counted.collection_stats if counted and counted.collection_stats else
                          (counts.get("collection", {}) if isinstance(counts, dict) else {}) or
                          (getattr(adapter, "last_collection_stats", {}) if adapter is not None else {}))
            run.http_requests = collection.get("http_requests")
            run.retry_count = collection.get("retry_count")
            run.endpoint = collection.get("endpoint")
            try:
                run.runtime_seconds = max(0.0, (datetime.fromisoformat(ended.replace("Z", "+00:00")) -
                                                datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds())
            except (TypeError, ValueError):
                run.runtime_seconds = None
            self.storage.put(run)
            result["ended_at"] = ended
            outcomes.append(result)
        statuses = {item["status"] for item in outcomes}
        return {"multi_run_id": multi_run_id, "status": ("PARTIAL_FAILURE" if "FAILED" in statuses else
                "SUCCEEDED_WITH_REVIEW" if "SUCCEEDED_WITH_REVIEW" in statuses else "SUCCEEDED"),
                "sources": outcomes}
