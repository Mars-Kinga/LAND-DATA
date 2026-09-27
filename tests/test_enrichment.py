"""Bounded official enrichment and parent/venue correctness, without live network."""
import tempfile
import unittest
from pathlib import Path

from land_data.adapters import SourceItem
from land_data.config import load_config
from land_data.enrichment import EnrichmentPlan, enrich
from land_data.export import dataset
from land_data.geoscope import GeoScope
from land_data.models import FieldProvenance, POI, POICandidate, POIMention, RecommendationSignal
from land_data.official_directory import (DirectoryReference, OfficialDirectoryAdapter,
                                          coordinate_from_map, tenant_page)
from land_data.osm import OSMSourceAdapter
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]
SCOPE = GeoScope("small-test", "JP", (35.679, 139.762, 35.686, 139.767), "CUSTOM", "Tokyo")
TENANT_URL = "https://www.marunouchi.com/en/tenants/12345/"
INDEX_URL = "https://marunouchi.jp-kitte.jp/gb/shopcat.jsp?cat=3"
ACCESS_URL = "https://marunouchi.jp-kitte.jp/gb/access.jsp"
BRAND_URL = "https://saza.coffee/Page/shop.aspx"
MAP_LINK = "https://goo.gl/maps/example"
MAP_FINAL = "https://www.google.com/maps/place/Sample+Coffee/!3d35.6796023!4d139.765377"
PAGES = {
    TENANT_URL: '<span>Cafe</span><h2>Sample Front Room</h2><a href="https://www.google.com/maps/search/?api=1&amp;query=35.6810403%2C139.7638027&amp;query_place_id=place123">map</a>',
    INDEX_URL: '<li class="item" id="id1020"><a href="shop.jsp?shopid=1020">Sample Coffee</a><span>store roasted coffee specialty shop</span></li>',
    ACCESS_URL: '<title>Access | Sample Mall (for english)</title><iframe src="https://www.google.com/maps/embed/v1/place?q=35.6796880,139.7650520&amp;key=test"></iframe>',
    BRAND_URL: '<h3>Sample Coffee Branch</h3><p>03-6268-0720</p><a href="https://goo.gl/maps/example">Map</a>',
    "https://marunouchi.jp-kitte.jp/gb/shop.jsp?shopid=1020": '<p>1F 03-6268-0720</p>',
}
REFERENCES = tuple(DirectoryReference(kind, url) for kind, url in (
    ("marunouchi_tenant", TENANT_URL), ("kitte_food_index", INDEX_URL),
    ("kitte_access", ACCESS_URL), ("brand_store_index", BRAND_URL)))


class OneSocial:
    name = "mock-social-2c"

    def __init__(self, external_id, title, name, category, text):
        self.item = SourceItem(self.name, external_id, "https://example.invalid/" + external_id,
                               {"content_type": "SOCIAL_POST", "title": title, "text": text,
                                "text_mentions": [{"name": name, "city": "Tokyo", "country": "JP",
                                                   "category": category, "origin": "title",
                                                   "context": text}]})

    def collect(self, _scope_name):
        return [self.item]


class EmptyOSM:
    name = "osm"

    def __init__(self, *_args, **_kwargs):
        pass

    def collect(self, _scope_name):
        return []


class FakeDirectory(OfficialDirectoryAdapter):
    def __init__(self, scope, references, requested_names, timeout):
        super().__init__(scope, references, requested_names, timeout,
                         fetcher=lambda url, _timeout: PAGES[url],
                         map_resolver=lambda _url, _timeout: MAP_FINAL)


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage = SQLiteStorage(str(Path(temp.name) / "enrichment.db"))
        self.addCleanup(self.storage.close)
        self.pipeline = Pipeline(self.storage, load_config(str(ROOT)))

    def test_map_point_requires_venue_identity(self):
        self.assertIsNone(coordinate_from_map("https://www.google.com/maps?q=35.679688,139.765052", True))
        self.assertEqual(coordinate_from_map(MAP_FINAL, True), (35.6796023, 139.765377))
        self.assertIsNotNone(tenant_page(PAGES[TENANT_URL], TENANT_URL, SCOPE))

    def test_official_directory_uses_store_pin_not_facility_point(self):
        adapter = FakeDirectory(SCOPE, REFERENCES, ["Sample Front Room", "Sample Coffee"], 15)
        places = {item.payload["title"]: item.payload["locations"][0]
                  for item in adapter.collect(SCOPE.name)}
        self.assertEqual(set(places), {"Sample Front Room", "Sample Mall", "Sample Coffee"})
        self.assertEqual(places["Sample Coffee"]["coordinate_precision"], "VENUE_PIN")
        self.assertEqual(places["Sample Coffee"]["parent_name"], "Sample Mall")
        self.assertNotEqual(places["Sample Coffee"]["longitude"], places["Sample Mall"]["longitude"])

    def test_missing_store_pin_does_not_use_facility_coordinates(self):
        class NoStoreMap(FakeDirectory):
            def __init__(self, scope, references, requested_names, timeout):
                OfficialDirectoryAdapter.__init__(self,
                    scope, references, requested_names, timeout,
                    fetcher=lambda url, _timeout: PAGES[url],
                    map_resolver=lambda _url, _timeout: "https://www.google.com/maps?q=35.679688,139.765052")
        names = [item.payload["title"] for item in NoStoreMap(
            SCOPE, REFERENCES, ["Sample Coffee"], 15).collect(SCOPE.name)]
        self.assertEqual(names, [])

    def test_social_enrichment_creates_venue_and_parent_with_provenance(self):
        self.pipeline.collect(SCOPE.name, [OneSocial("sample", "📍 Sample Coffee", "Sample Coffee",
                                                    "cafe", "date coffee")], SCOPE)
        self.pipeline.parse(SCOPE.name)
        plan = EnrichmentPlan(SCOPE, REFERENCES)
        result = enrich(self.pipeline, plan, osm_factory=EmptyOSM, directory_factory=FakeDirectory)
        self.pipeline.validate()
        self.assertEqual(result["directory_records"], 2)
        self.assertEqual(result["parents_linked"], 1)
        pois = {next(iter(poi.names.values())): poi for poi in self.storage.all(POI)}
        self.assertEqual(set(pois), {"Sample Coffee", "Sample Mall"})
        child, parent = pois["Sample Coffee"], pois["Sample Mall"]
        self.assertEqual(child.parent_poi_id, parent.id)
        self.assertEqual((child.latitude, child.longitude), (35.6796023, 139.765377))
        self.assertNotEqual((child.latitude, child.longitude), (parent.latitude, parent.longitude))
        candidate = next(c for c in self.storage.all(POICandidate) if c.source_kind == "content")
        self.assertEqual(candidate.resolved_poi_id, child.id)
        mention = next(iter(self.storage.all(POIMention)))
        self.assertEqual(mention.poi_id, child.id)
        self.assertTrue(any(p.entity_id == child.id and p.field_name == "latitude" and
                            p.source_reference_url == BRAND_URL for p in self.storage.all(FieldProvenance)))
        self.assertTrue(any(p.entity_id == child.id and p.field_name == "parent_poi_id"
                            for p in self.storage.all(FieldProvenance)))
        exported = dataset(self.storage, self.pipeline.config, "Tokyo", SCOPE.name)
        child_export = next(item for item in exported["pois"] if item["id"] == child.id)
        self.assertEqual(child_export["parent_poi_id"], parent.id)
        self.assertEqual(child_export["coordinate_precision"], "VENUE_PIN")
        before = self.pipeline.counts()
        enrich(self.pipeline, plan, osm_factory=EmptyOSM, directory_factory=FakeDirectory)
        self.pipeline.validate()
        self.assertEqual(self.pipeline.counts(), before)

    def test_hotel_food_mention_stays_matched_but_needs_review(self):
        from land_data.osm import parse_element
        item = parse_element({"type": "node", "id": 8811, "lat": 35.6855, "lon": 139.7653,
                              "tags": {"name": "Example Hotel", "tourism": "hotel"}}, SCOPE)
        class HotelAdapter:
            name = "osm"
            def collect(self, _scope_name):
                return [item]
        self.pipeline.run(SCOPE.name, [HotelAdapter()], SCOPE)
        self.pipeline.run(SCOPE.name, [OneSocial("hotel", "📍 Example Hotel", "Example Hotel",
                                                "hotel", "刨冰 #cafe")], SCOPE)
        candidate = next(c for c in self.storage.all(POICandidate) if c.source_kind == "content")
        mention = next(iter(self.storage.all(POIMention)))
        self.assertIsNotNone(candidate.resolved_poi_id)
        self.assertEqual(mention.poi_id, candidate.resolved_poi_id)
        self.assertEqual(mention.review_reasons, ["POSSIBLE_INTERNAL_VENUE"])
        self.assertEqual(mention.review_status, "NEEDS_REVIEW")
        self.assertEqual([s.value for s in self.storage.all(RecommendationSignal)], ["shaved ice"])
        self.assertEqual(dataset(self.storage, self.pipeline.config, "Tokyo")["mentions"], [])

    def test_osm_name_search_remains_bounded_and_escapes_input(self):
        query = OSMSourceAdapter(SCOPE, categories=("hotel",), limit=5, timeout=15,
                                 name_query="Hotel (Central)").query()
        self.assertIn('tourism', query)
        self.assertIn('name:en', query)
        self.assertIn('35.679,139.762,35.686,139.767', query)
        self.assertIn('out center 5', query)
        self.assertIn('Hotel', query)


if __name__ == "__main__":
    unittest.main()
