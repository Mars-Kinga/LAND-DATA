"""Deployable mock data, separate from raw and normalized storage."""
import json
from pathlib import Path
from typing import Dict

from .config import Config
from .models import FieldProvenance, POI, POIMention, RecommendationSignal, SourceContent
from .storage import Storage
from .util import utc_now


def dataset(storage: Storage, config: Config, city: str, scope_id: str = None) -> Dict:
    visible = [poi for poi in storage.pois_in_city(city) if poi.map_visibility]
    if scope_id:
        contents_by_id = {item.id: item for item in storage.all(SourceContent)}
        scoped_poi_ids = set()
        for provenance in storage.all(FieldProvenance):
            content = contents_by_id.get(provenance.source_content_id)
            if provenance.entity_type == "POI" and content and content.collection_scope == scope_id:
                scoped_poi_ids.add(provenance.entity_id)
        visible = [poi for poi in visible if poi.id in scoped_poi_ids]
    poi_ids = {poi.id for poi in visible}
    osm_ids = {value.entity_id for value in storage.all(FieldProvenance)
               if value.entity_type == "POI" and value.source == "osm"}
    contents = {content.id: content for content in storage.all(SourceContent)}
    signals = storage.all(RecommendationSignal)
    mentions = []
    for mention in storage.all(POIMention):
        if mention.poi_id not in poi_ids or mention.review_status != "AUTO_APPROVED" or not mention.active:
            continue
        source = contents[mention.source_content_id]
        if source.content_status == "UNAVAILABLE":
            continue
        policy = config.sources[source.platform].policy
        item = {
            "id": mention.id, "poi_id": mention.poi_id,
            "relation_type": mention.relation_type,
            "source": {"platform": source.platform,
                       **({"source_url": source.source_url} if
                          source.source_url and "source_url" in policy.export_fields else {})}}
        if "recommendation_summary" in policy.export_fields:
            item["recommendation_summary"] = mention.recommendation_summary
        if "recommendation_signals" in policy.export_fields:
            item["signals"] = [{"type": signal.type, "value": signal.value, "confidence": signal.confidence}
                               for signal in signals if signal.poi_mention_id == mention.id]
        mentions.append(item)
    return {
        "schema_version": 1, "city": city, "generated_at": utc_now(),
        **({"scope": scope_id} if scope_id else {}),
        "source_attributions": ([{"name": "OpenStreetMap", "attribution": "© OpenStreetMap contributors",
                                  "license": "ODbL 1.0", "url": "https://www.openstreetmap.org/copyright"}]
                                if poi_ids & osm_ids else []),
        "pois": [{"id": poi.id, "names": poi.names, "aliases": poi.aliases,
                  "latitude": poi.latitude, "longitude": poi.longitude,
                  "address": poi.address, "category": poi.category,
                  **({"website": poi.website} if poi.website else {}),
                  "subcategory": poi.subcategory, "tags": poi.tags,
                  "confidence_score": poi.confidence_score, "quality_score": poi.quality_score,
                  "last_verified_at": poi.last_verified_at,
                  "recommendation_counts": storage.recommendation_counts(poi.id)} for poi in visible],
        "mentions": mentions,
    }


def write_dataset(storage: Storage, config: Config, city: str, path: str,
                  scope_id: str = None) -> Dict:
    output = dataset(storage, config, city, scope_id)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output
