"""Offline Phase 2C acceptance: mock OSM response plus synthetic social posts."""
import argparse
import json
from pathlib import Path

from land_data.auth import MockSessionStore
from land_data.config import load_config
from land_data.export import write_dataset
from land_data.geoscope import load_scope
from land_data.models import FieldProvenance, POI, POIMention, SourceContent
from land_data.osm import OSMSourceAdapter
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class MockResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, _limit):
        return self.data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / ".local" / "social-acceptance.db"))
    parser.add_argument("--output", default=str(ROOT / ".local" / "exports" / "social-acceptance.json"))
    args = parser.parse_args()
    scope = load_scope(str(ROOT / "config" / "social-acceptance-scope.json"))
    config = load_config(str(ROOT))
    response = (ROOT / "fixtures" / "osm_social_2c.json").read_bytes()
    osm = OSMSourceAdapter(scope, opener=lambda _request, timeout: MockResponse(response))
    sessions = MockSessionStore(str(ROOT / ".local" / "social-acceptance-session.json"))
    social = config.adapter("mock-social-2c", sessions)
    with SQLiteStorage(args.db) as storage:
        pipeline = Pipeline(storage, config)
        pipeline.run(scope.name, [osm], scope)
        counts = pipeline.run(scope.name, [social], scope)
        exported = write_dataset(storage, config, scope.city, args.output, scope_id=scope.name)
        contents = {item.id: item for item in storage.all(SourceContent)}
        mentions = storage.all(POIMention)
        provenance = storage.all(FieldProvenance)
        pois = []
        for poi in storage.all(POI):
            linked = [mention for mention in mentions if mention.poi_id == poi.id]
            poi_sources = sorted(
                {contents[value.source_content_id].platform for value in provenance
                 if value.entity_type == "POI" and value.entity_id == poi.id
                 and value.source_content_id in contents} |
                {contents[mention.source_content_id].platform for mention in linked
                 if mention.source_content_id in contents})
            pois.append({"id": poi.id, "name": next(iter(poi.names.values())),
                         "mention_count": len(linked),
                         "recommendation_counts": storage.recommendation_counts(poi.id),
                         "source_provenance": poi_sources,
                         "review_status": poi.review_status})
        print(json.dumps({"counts": counts, "export_pois": len(exported["pois"]),
                          "export_mentions": len(exported["mentions"]), "pois": pois},
                         ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
