"""One bounded, sequential Overpass request; outputs SourceItems only."""
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .adapters import AdapterCapability, SourceItem
from .geoscope import GeoScope


CATEGORY_TAGS = {"restaurant": ("amenity", "restaurant"),
                 "cafe": ("amenity", "cafe"), "museum": ("tourism", "museum")}
ENDPOINT = "https://overpass-api.de/api/interpreter"
USER_AGENT = "LAND-Data/2B (small integration validation; https://github.com/Mars-Kinga/LAND-DATA)"


def category_for(tags):
    for category, (key, value) in CATEGORY_TAGS.items():
        if tags.get(key) == value:
            return category
    return None


def parse_element(element, scope):
    if not isinstance(element, dict) or element.get("type") not in ("node", "way", "relation"):
        raise ValueError("malformed OSM object type")
    obj_id = element.get("id")
    if type(obj_id) is not int or obj_id <= 0 or not isinstance(element.get("tags", {}), dict):
        raise ValueError("malformed OSM object id or tags")
    obj_type = element["type"]
    tags = {key: value for key, value in element.get("tags", {}).items()
            if isinstance(key, str) and isinstance(value, str)}
    position = element if obj_type == "node" else element.get("center", {})
    position = position if isinstance(position, dict) else {}
    latitude, longitude = position.get("lat"), position.get("lon")
    category = category_for(tags) or "unknown"
    name = tags.get("name", "")
    external_id = "{}/{}".format(obj_type, obj_id)
    location = {"name": name, "city": scope.city or scope.name, "country": scope.country,
                "category": category, "latitude": latitude, "longitude": longitude,
                "coordinate_verified": latitude is not None and longitude is not None,
                "name_locale": "und", "address": tags.get("addr:full", ""),
                "website": tags.get("website", tags.get("contact:website", "")),
                "osm_tags": tags, "evidence": [
                    {"type": "OSM_OBJECT", "raw_value": external_id, "confidence": 0.92},
                    *([{"type": "OSM_COORDINATES", "raw_value": "{},{}".format(latitude, longitude),
                        "confidence": 0.9}] if latitude is not None and longitude is not None else [])]}
    return SourceItem("osm", external_id, "https://www.openstreetmap.org/" + external_id,
                      {"content_type": "OSM_OBJECT", "title": name, "text": "",
                       "source_kind": "structured", "osm_object_type": obj_type,
                       "osm_object_id": obj_id, "locations": [location]})


class OSMSourceAdapter:
    name = "osm"
    capability = AdapterCapability("PUBLIC", supports_geo_tag=True, supports_search=True)

    def __init__(self, scope: GeoScope, categories=("restaurant", "cafe", "museum"),
                 limit=20, timeout=15, opener=urlopen, endpoint=ENDPOINT):
        if not categories or any(value not in CATEGORY_TAGS for value in categories):
            raise ValueError("OSM categories must be restaurant, cafe or museum")
        if not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("OSM result limit must be 1-30")
        if not isinstance(timeout, int) or not 2 <= timeout <= 30:
            raise ValueError("OSM timeout must be 2-30 seconds")
        south, west, north, east = scope.bbox
        if north - south > 0.02 or east - west > 0.02:
            raise ValueError("OSM integration scope must be at most 0.02 degrees per axis")
        if scope.center is not None:
            raise ValueError("OSM integration currently queries bounding boxes only")
        self.scope = scope
        self.categories = tuple(dict.fromkeys(categories))
        self.limit = limit
        self.timeout = timeout
        self.opener = opener
        self.endpoint = endpoint

    def query(self):
        bbox = ",".join(str(value) for value in self.scope.bbox)
        selectors = []
        for category in self.categories:
            key, value = CATEGORY_TAGS[category]
            for obj_type in ("node", "way", "relation"):
                selectors.append('{}["{}"="{}"]({});'.format(obj_type, key, value, bbox))
        return "[out:json][timeout:{}];({});out center {};".format(
            self.timeout, "".join(selectors), self.limit)

    def collect(self, city):
        if city != self.scope.name:
            raise ValueError("collection key must match GeoScope name")
        request = Request(self.endpoint, data=urlencode({"data": self.query()}).encode("utf-8"),
                          headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
        for attempt in range(2):
            try:
                with self.opener(request, timeout=self.timeout + 3) as response:
                    body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise ValueError("Overpass response exceeds 2 MB")
                payload = json.loads(body)
                elements = payload.get("elements")
                if not isinstance(elements, list):
                    raise ValueError("Overpass response missing elements")
                if len(elements) > self.limit:
                    raise ValueError("Overpass response exceeds result limit")
                seen = set()
                for element in elements:
                    item = parse_element(element, self.scope)
                    if item.external_id not in seen:
                        seen.add(item.external_id)
                        yield item
                return
            except HTTPError as exc:
                if attempt == 0 and exc.code in (429, 502, 503, 504):
                    time.sleep(1)
                    continue
                raise ValueError("Overpass HTTP {}".format(exc.code)) from exc
            except OSError as exc:
                retryable = isinstance(exc, (URLError, TimeoutError)) or "timed out" in str(exc).lower()
                if attempt == 0 and retryable:
                    time.sleep(1)
                    continue
                raise ValueError("Overpass network/timeout error: {}".format(exc)) from exc
