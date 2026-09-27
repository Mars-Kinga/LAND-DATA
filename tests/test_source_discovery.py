import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from land_data.models import (SourceDiscoveryCandidate, SourceDiscoveryCheckpoint,
                              SourceDiscoveryRunRecord)
from land_data.auto_collect import select_auto_specs
from land_data.cli import build_parser
from land_data.multi_source import load_specs
from land_data.source_discovery import (DiscoveryConfig, DiscoveryQuery,
                                        SourceDiscoveryRunner, load_discovery_config)
from land_data.sqlite_storage import SQLiteStorage


class FakeResponse:
    def __init__(self, payload, status=200):
        self.body = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.body


class CatalogTransport:
    def __init__(self):
        self.fail_terms = set()
        self.fail_schema_ids = set()
        self.updated = False
        self.calls = []

    @staticmethod
    def row(dataset_id, title, *, domain="data.cityofnewyork.us", asset_type="dataset"):
        return {"resource": {"id": dataset_id, "name": title, "type": asset_type,
                              "domain": domain, "description": title + " description",
                              "tags": ["travel"], "updatedAt": "2026-09-22T00:00:00Z"},
                "classification": {"assetType": asset_type},
                "permalink": "https://{}/d/{}".format(domain, dataset_id)}

    def __call__(self, request, timeout=0):
        url = request.full_url
        self.calls.append(url)
        parsed = urlsplit(url)
        if parsed.path.endswith("/api/catalog/v1"):
            query = parse_qs(parsed.query).get("q", [""])[0]
            if query in self.fail_terms:
                raise OSError("temporary catalog failure")
            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
            if offset:
                return FakeResponse({"results": []})
            rows = [self.row("abcd-efgh", "City Parks"),
                    self.row("ijkl-mnop", "Museum Dataset", asset_type="dataset"),
                    self.row("evil-evil", "External", domain="evil.example"),
                    self.row("view-view", "A View", asset_type="view"),
                    self.row("badid", "Invalid ID")]
            if self.updated:
                rows[0]["resource"]["description"] = "City Parks changed"
                rows[0]["resource"]["updatedAt"] = "2026-09-23T00:00:00Z"
            return FakeResponse({"results": rows})
        dataset_id = parsed.path.rsplit("/", 1)[-1]
        if dataset_id in self.fail_schema_ids:
            raise OSError("schema unavailable")
        return FakeResponse({"columns": [{"fieldName": "id"}, {"fieldName": "starttime"},
                                           {"fieldName": "latitude"}]})


def make_config(*terms):
    return DiscoveryConfig(
        provider="socrata-catalog", catalog_endpoint="https://api.us.socrata.com/api/catalog/v1",
        allowed_domains=("data.cityofnewyork.us",),
        queries=(DiscoveryQuery("places", tuple(terms or ("park",))),),
        limit=10, max_pages=1, timeout=2, max_retries=0, schema_probe_limit=10)


class SourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = SQLiteStorage(str(Path(self.temp.name) / "discovery.db"))
        self.addCleanup(self.storage.close)
        self.transport = CatalogTransport()

    def test_queries_are_bounded_filtered_and_idempotent(self):
        config = DiscoveryConfig(
            provider="socrata-catalog", catalog_endpoint="https://api.us.socrata.com/api/catalog/v1",
            allowed_domains=("data.cityofnewyork.us",),
            queries=(DiscoveryQuery("places", ("park",)), DiscoveryQuery("events", ("park",))),
            limit=10, max_pages=1, timeout=2, max_retries=0, schema_probe_limit=10)
        first = SourceDiscoveryRunner(self.storage, config, opener=self.transport).run()
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(first["new_count"], 2)
        self.assertEqual(first["duplicate_count"], 2)
        self.assertEqual(first["filtered_count"], 6)
        self.assertEqual(len(self.storage.all(SourceDiscoveryCandidate)), 2)
        self.assertEqual(first["llm_calls"], 0)
        second = SourceDiscoveryRunner(self.storage, config, opener=self.transport).run()
        self.assertEqual(second["new_count"], 0)
        self.assertGreaterEqual(second["duplicate_count"], 2)
        self.assertEqual(len(self.storage.all(SourceDiscoveryCandidate)), 2)

    def test_schema_failure_is_reviewable_and_metadata_update_is_counted(self):
        self.transport.fail_schema_ids.add("ijkl-mnop")
        first = SourceDiscoveryRunner(self.storage, make_config("park"), opener=self.transport).run()
        candidates = {item.dataset_id: item for item in self.storage.all(SourceDiscoveryCandidate)}
        self.assertEqual(candidates["ijkl-mnop"].status, "NEEDS_REVIEW")
        self.assertIn("SCHEMA_UNAVAILABLE", candidates["ijkl-mnop"].reasons)
        self.transport.fail_schema_ids.clear()
        self.transport.updated = True
        second = SourceDiscoveryRunner(self.storage, make_config("park"), opener=self.transport).run()
        self.assertEqual(second["updated_count"], 2)
        self.assertIn("SOURCE_METADATA_UPDATED", self.storage.get(
            SourceDiscoveryCandidate, candidates["abcd-efgh"].id).reasons)

    def test_partial_query_failure_keeps_checkpoint_and_recovers(self):
        config = DiscoveryConfig(
            provider="socrata-catalog", catalog_endpoint="https://api.us.socrata.com/api/catalog/v1",
            allowed_domains=("data.cityofnewyork.us",),
            queries=(DiscoveryQuery("places", ("park",)), DiscoveryQuery("events", ("museum",))),
            limit=10, max_pages=1, timeout=2, max_retries=0, schema_probe_limit=1)
        self.transport.fail_terms.add("museum")
        failed = SourceDiscoveryRunner(self.storage, config, opener=self.transport).run()
        self.assertEqual(failed["status"], "PARTIAL_FAILURE")
        checkpoint = self.storage.get_discovery_checkpoint("socrata-catalog", "places:park")
        self.assertIsInstance(checkpoint, SourceDiscoveryCheckpoint)
        self.assertTrue(checkpoint.cycle_complete)
        self.transport.fail_terms.clear()
        recovered = SourceDiscoveryRunner(self.storage, config, opener=self.transport).run()
        self.assertEqual(recovered["status"], "SUCCEEDED")
        self.assertEqual(len(self.storage.all(SourceDiscoveryRunRecord)), 2)

    def test_cli_exposes_discovery_and_zero_llm_auto_collect_modes(self):
        args = build_parser().parse_args(["discover-sources", "--list-candidates"])
        self.assertEqual(args.command, "discover-sources")
        self.assertTrue(args.list_candidates)
        args = build_parser().parse_args(["auto-collect", "--city", "new-york",
                                          "--discovery-only"])
        self.assertEqual(args.command, "auto-collect")
        self.assertTrue(args.discovery_only)
        self.assertFalse(getattr(args, "llm_extract", False))

    def test_config_keeps_explicit_supported_mapping_out_of_collection(self):
        config = load_discovery_config(str(Path(__file__).resolve().parents[1] /
                                           "config" / "source-discovery.json"))
        self.assertIn("43nn-pn8j", config.supported_datasets)
        self.assertEqual(config.supported_datasets["w3wp-dpdi"]["family"], "nyc-parks-events")

    def test_auto_collect_selects_city_sources_and_excludes_jfk_guidance(self):
        root = Path(__file__).resolve().parents[1]
        specs = load_specs(str(root / "config" / "multi-source.json"), root)
        selected = select_auto_specs(specs, "new-york")
        self.assertEqual({spec.name for spec in selected}, {
            "nyc-open-data-restaurants", "osm", "nyc-parks-events"})
        self.assertNotIn("jfk-arrival-guidance", {spec.name for spec in selected})
        self.assertEqual({spec.city for spec in selected}, {"new-york"})
        self.assertTrue(all(spec.auto_collect for spec in selected))


if __name__ == "__main__":
    unittest.main()
