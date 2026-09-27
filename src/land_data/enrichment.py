"""Small, bounded POI enrichment pass for unresolved social candidates."""
from dataclasses import dataclass
from typing import Tuple

from .geoscope import GeoScope
from .models import FieldProvenance, POI, POICandidate, SourceContent
from .official_directory import DirectoryReference, OfficialDirectoryAdapter
from .osm import CATEGORY_TAGS, OSMSourceAdapter
from .processing import resolve
from .storage import Storage
from .util import normalized_name, stable_id, utc_now


@dataclass(frozen=True)
class EnrichmentPlan:
    scope: GeoScope
    directory_references: Tuple[DirectoryReference, ...] = ()
    osm_limit: int = 5
    timeout: int = 15

    def __post_init__(self):
        if not 1 <= self.osm_limit <= 20 or not 2 <= self.timeout <= 20:
            raise ValueError("enrichment limit or timeout out of range")
        if len(self.directory_references) > 5:
            raise ValueError("enrichment accepts at most five directory references")


def pending_social_candidates(storage: Storage, scope: GeoScope):
    contents = {item.id: item for item in storage.all(SourceContent)}
    return [candidate for candidate in storage.all(POICandidate)
            if (candidate.source_kind == "content" and not candidate.resolved_poi_id and
                candidate.review_status != "REJECTED" and candidate.name.strip() and
                candidate.city == scope.city and candidate.country == scope.country and
                (content := contents.get(candidate.source_content_id)) is not None and
                content.platform not in ("osm", "official-directory"))]


def link_official_parents(storage: Storage) -> int:
    """Link only an explicit directory child to a unique, existing facility POI."""
    linked = 0
    pois = storage.all(POI)
    for candidate in storage.all(POICandidate):
        if candidate.source_kind != "structured" or not candidate.parent_name or not candidate.resolved_poi_id:
            continue
        content = storage.get(SourceContent, candidate.source_content_id)
        if not content or content.platform != "official-directory":
            continue
        child = storage.get(POI, candidate.resolved_poi_id)
        matches = [poi for poi in pois if poi.id != child.id and poi.city == child.city and
                   poi.country == child.country and poi.category in ("shopping_mall", "hotel") and
                   normalized_name(candidate.parent_name) in
                   {normalized_name(name) for name in poi.names.values()}]
        if len(matches) != 1 or child.parent_poi_id == matches[0].id:
            continue
        parent = matches[0]
        ancestor = parent
        while ancestor.parent_poi_id:
            if ancestor.parent_poi_id == child.id:
                raise ValueError("POI parent cycle")
            ancestor = storage.get(POI, ancestor.parent_poi_id)
            if ancestor is None:
                raise ValueError("POI parent is missing")
        child.parent_poi_id = parent.id
        child.updated_at = utc_now()
        storage.put(child)
        storage.put(FieldProvenance(
            id=stable_id("provenance", child.id, "parent_poi_id", content.id),
            entity_type="POI", entity_id=child.id, field_name="parent_poi_id",
            source="official-directory", source_content_id=content.id,
            collected_at=content.collected_at, verified_at=child.updated_at,
            source_field="official_facility_directory", source_reference_url=content.source_url))
        linked += 1
    return linked


def enrich(pipeline, plan: EnrichmentPlan, *, osm_factory=None,
           directory_factory=None) -> dict:
    """All discovered records enter the ordinary collect/parse/resolve path."""
    osm_factory = osm_factory or OSMSourceAdapter
    directory_factory = directory_factory or OfficialDirectoryAdapter
    resolve(pipeline.storage)
    pending = pending_social_candidates(pipeline.storage, plan.scope)
    if not pending:
        return {"osm_queries": 0, "directory_records": 0, "parents_linked": 0, "errors": []}
    result = {"osm_queries": 0, "directory_records": 0, "parents_linked": 0, "errors": []}
    if pending and plan.directory_references:
        adapter = directory_factory(plan.scope, plan.directory_references,
                                    [candidate.name for candidate in pending], timeout=plan.timeout)
        try:
            result["directory_records"] = pipeline.collect(plan.scope.name, [adapter], plan.scope)
            if result["directory_records"]:
                pipeline.parse(plan.scope.name)
                resolve(pipeline.storage)
                result["parents_linked"] = link_official_parents(pipeline.storage)
        except ValueError as exc:
            result["errors"].append("Official directory: " + str(exc))
    pending = pending_social_candidates(pipeline.storage, plan.scope)
    for candidate in pending[:5]:
        categories = ((candidate.category,) if candidate.category in CATEGORY_TAGS else
                      ("restaurant", "cafe", "hotel", "museum"))
        adapter = osm_factory(plan.scope, categories=categories, limit=plan.osm_limit,
                              timeout=plan.timeout, name_query=candidate.name)
        try:
            pipeline.collect(plan.scope.name, [adapter], plan.scope)
            result["osm_queries"] += 1
        except ValueError as exc:
            result["errors"].append("OSM: " + str(exc))
    if result["osm_queries"]:
        pipeline.parse(plan.scope.name)
        resolve(pipeline.storage)
    return result
