"""Small, manual-visible-text acceptance. Reads posts from stdin; never saves input files."""
import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from land_data.adapters import SourceItem
from land_data.config import load_config
from land_data.llm_extraction import LLMExtractionService, qwen_from_env
from land_data.models import (EventCandidate, POI, POICandidate, POIMention,
                              RecommendationSignal, SourceContent)
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class VisiblePosts:
    name = "xiaohongshu-observed"

    def __init__(self, posts):
        self.posts = posts

    def collect(self, _city):
        return [SourceItem(self.name, post["id"],
                           "https://www.xiaohongshu.com/explore/" + post["id"],
                           {"content_type": "SOCIAL_POST", "title": post["title"],
                            "text": post.get("text", ""), "hashtags": post.get("hashtags", []),
                            **({"geo_tag": post["visible_location"]}
                               if post.get("visible_location") else {})})
                for post in self.posts]


def stats(storage, post_ids, existing_poi_ids):
    contents = {value.id: value.external_id for value in storage.all(SourceContent)
                if value.platform == VisiblePosts.name and value.external_id in post_ids}
    candidates = [value for value in storage.all(POICandidate) if value.source_content_id in contents]
    mentions = [value for value in storage.all(POIMention) if value.source_content_id in contents and value.active]
    signal_rows = storage.all(RecommendationSignal)
    events = [value for value in storage.all(EventCandidate) if value.source_content_id in contents]
    by_candidate = {value.id: value for value in candidates}
    return {
        "recognized_posts": len({contents[value.source_content_id] for value in candidates}),
        "mentions": len(mentions),
        "direct_existing_poi_mentions": sum(value.poi_id in existing_poi_ids for value in mentions),
        "needs_review_mentions": sum(value.review_status == "NEEDS_REVIEW" for value in mentions),
        "signals": sum(value.poi_mention_id in {mention.id for mention in mentions} for value in signal_rows),
        "event_candidates": len(events),
        "by_post": [{"id": post_id, "places": [
            {"name": by_candidate[mention.poi_candidate_id].name,
             "context": by_candidate[mention.poi_candidate_id].extraction_context,
             "parent_name": by_candidate[mention.poi_candidate_id].parent_name,
             "poi_id": mention.poi_id, "review_status": mention.review_status,
             "review_reasons": mention.review_reasons,
             "signals": [signal.value for signal in signal_rows if signal.poi_mention_id == mention.id]}
            for mention in mentions if contents[mention.source_content_id] == post_id],
            "events": [{"name": event.name, "confidence": event.extraction_confidence,
                        "review_status": event.review_status}
                       for event in events if contents[event.source_content_id] == post_id]
            } for post_id in post_ids],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="mock-tokyo")
    parser.add_argument("--base-db", default=str(ROOT / ".local" / "osm-2b-acceptance.db"))
    parser.add_argument("--model", help="Override .env model for this bounded acceptance only")
    args = parser.parse_args()
    posts = json.load(sys.stdin)
    if not isinstance(posts, list) or len(posts) != 10 or any(
            not isinstance(post, dict) or not isinstance(post.get("id"), str) or
            not isinstance(post.get("title"), str) for post in posts):
        raise ValueError("acceptance input must contain exactly 10 visible posts")
    if len({post["id"] for post in posts}) != 10:
        raise ValueError("duplicate post id")
    config = load_config(str(ROOT))
    source = config.sources["mock-social-2c"]
    config = replace(config, sources={**config.sources,
                                      VisiblePosts.name: replace(source, name=VisiblePosts.name, fixture=None)})
    client = qwen_from_env(ROOT)
    if args.model:
        client.model = args.model
    client.timeout = max(client.timeout, 60)  # Bounded acceptance; large JSON schemas need longer responses.
    service = LLMExtractionService(client)
    with SQLiteStorage(":memory:") as storage:
        with SQLiteStorage(args.base_db) as base:
            existing_ids = {value.id for value in base.all(POI)}
            for poi in base.all(POI):
                storage.put(poi)
        pipeline = Pipeline(storage, config, service)
        adapter = VisiblePosts(posts)
        pipeline.run(args.city, [adapter])
        first = stats(storage, [post["id"] for post in posts], existing_ids)
        first_calls = service.calls
        first_latencies = dict(service.latencies_by_content)
        pipeline.run(args.city, [adapter])
        second = stats(storage, [post["id"] for post in posts], existing_ids)
        print(json.dumps({"model": client.model, "first": first, "second": second,
                          "llm_calls_first": first_calls, "llm_calls_second_total": service.calls,
                          "llm_calls_second_new": service.calls - first_calls,
                          "average_seconds_per_post_first": round(sum(first_latencies.values()) / len(posts), 2),
                          "latencies_by_post": first_latencies,
                          "cache_hits_second_total": service.cache_hits,
                          "fallbacks": service.fallbacks,
                          "fallback_reasons": service.errors}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
