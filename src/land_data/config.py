"""JSON configuration; source and city definitions are data, not code paths."""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .adapters import AdapterCapability, MockSocialAdapter, MockSourceAdapter
from .auth import MockSessionStore
from .geoscope import GeoScope
from .osm import OSMSourceAdapter
from .nyc_open_data import NYCOpenDataRestaurantAdapter
from .jfk_guidance import JFKArrivalGuidanceAdapter
from .nyc_parks_events import NYCParksEventsAdapter
from .policy import SourcePolicy
from .threads import ThreadsAdapter


@dataclass(frozen=True)
class SourceConfig:
    name: str
    fixture: Optional[str]
    capability: AdapterCapability
    policy: SourcePolicy
    allow_coordinate_verification: bool = False
    processing_kind: str = "standard"


@dataclass(frozen=True)
class Config:
    root: Path
    cities: Dict[str, dict]
    sources: Dict[str, SourceConfig]
    visibility_threshold: float

    def adapter(self, name: str, sessions: MockSessionStore, scope: Optional[GeoScope] = None,
                categories=None, limit=20, timeout=15, *, threads_query=None,
                threads_post_id=None, threads_search_type="RECENT", threads_pages=1,
                threads_token=None, storage=None, collection_key=None, max_pages=3,
                initial_updated_at=None, overlap_seconds=86400, safety_rescan_days=7,
                endpoints=None, max_retries=2):
        source = self.sources[name]
        if name == "osm":
            if scope is None:
                raise ValueError("osm requires --scope")
            return OSMSourceAdapter(scope, categories or ("restaurant", "cafe", "museum"), limit, timeout,
                                    endpoints=endpoints or None, max_retries=max_retries)
        if name == "threads":
            return ThreadsAdapter(threads_token, query=threads_query, post_id=threads_post_id,
                                  search_type=threads_search_type, limit=limit, pages=threads_pages,
                                  timeout=timeout)
        if name == "official-directory":
            raise ValueError("official-directory requires an explicit enrichment plan")
        if name == "nyc-open-data-restaurants":
            return NYCOpenDataRestaurantAdapter(borough=categories[0] if categories else "Manhattan",
                                                limit=limit, timeout=timeout, storage=storage,
                                                collection_key=collection_key or "new-york", max_pages=max_pages,
                                                initial_updated_at=(initial_updated_at or "2026-09-01T00:00:00.000"),
                                                overlap_seconds=overlap_seconds,
                                                safety_rescan_days=safety_rescan_days)
        if name == "jfk-arrival-guidance":
            return JFKArrivalGuidanceAdapter(timeout=timeout)
        if name == "nyc-parks-events":
            return NYCParksEventsAdapter(limit=limit, max_pages=max_pages, timeout=timeout,
                                         storage=storage, collection_key=collection_key or "new-york",
                                         max_retries=max_retries)
        if name == "xiaohongshu-browser":
            raise ValueError("xiaohongshu-browser is available through the xhs command")
        if source.processing_kind == "social":
            return MockSocialAdapter(name, str(self.root / source.fixture), source.capability, sessions)
        return MockSourceAdapter(name, str(self.root / source.fixture), source.capability, sessions)


def load_config(root: str) -> Config:
    root_path = Path(root).resolve()
    cities = json.loads((root_path / "config" / "cities.json").read_text(encoding="utf-8"))
    source_data = json.loads((root_path / "config" / "sources.json").read_text(encoding="utf-8"))
    sources = {}
    for name, value in source_data["sources"].items():
        policy = value["policy"]
        sources[name] = SourceConfig(
            name=name, fixture=value.get("fixture"),
            capability=AdapterCapability(**value["capability"]),
            allow_coordinate_verification=value.get("allow_coordinate_verification") is True,
            processing_kind=value.get("processing_kind", "standard"),
            policy=SourcePolicy(
                raw_fields=tuple(policy["raw_fields"]),
                normalized_fields=tuple(policy["normalized_fields"]),
                temporary_fields=tuple(policy.get("temporary_fields", [])),
                export_fields=tuple(policy.get("export_fields", [])),
                retention_days=int(policy.get("retention_days", 30)),
                retain_source_url=policy.get("retain_source_url", True) is True,
                export_enabled=policy.get("export_enabled", True) is True,
                allow_verified_rules=policy.get("allow_verified_rules", False) is True,
                allow_source_republication=policy.get("allow_source_republication", False) is True,
                allow_experience_derivation=policy.get("allow_experience_derivation", False) is True))
    threshold = float(source_data.get("visibility_threshold", 0.8))
    if not 0 <= threshold <= 1:
        raise ValueError("invalid visibility threshold")
    return Config(root_path, cities, sources, threshold)
