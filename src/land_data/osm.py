"""One bounded, sequential Overpass request; outputs SourceItems only."""
import json
import random
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .adapters import AdapterCapability, SourceItem
from .geoscope import GeoScope


CATEGORY_TAGS = {"restaurant": ("amenity", "restaurant"),
                 "cafe": ("amenity", "cafe"), "museum": ("tourism", "museum"),
                 "hotel": ("tourism", "hotel")}
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
                "coordinate_precision": "OSM_OBJECT",
                "name_locale": "und", "aliases": [tags["name:en"]] if tags.get("name:en") and tags["name:en"] != name else [],
                "address": tags.get("addr:full", ""),
                "website": tags.get("website", tags.get("contact:website", "")),
                "opening_hours": tags.get("opening_hours", ""),
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
                 limit=20, timeout=15, opener=urlopen, endpoint=ENDPOINT,
                 name_query=None, endpoints=None, max_retries=1, sleeper=None):
        if not categories or any(value not in CATEGORY_TAGS for value in categories):
            raise ValueError("unsupported OSM category")
        if not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("OSM result limit must be 1-30")
        if not isinstance(timeout, int) or not 2 <= timeout <= 30:
            raise ValueError("OSM timeout must be 2-30 seconds")
        south, west, north, east = scope.bbox
        if north - south > 0.02 or east - west > 0.02:
            raise ValueError("OSM integration scope must be at most 0.02 degrees per axis")
        if scope.center is not None:
            raise ValueError("OSM integration currently queries bounding boxes only")
        if name_query is not None and (not isinstance(name_query, str) or
                                       not 2 <= len(name_query.strip()) <= 80):
            raise ValueError("OSM name query must be 2-80 characters")
        self.scope = scope
        self.categories = tuple(dict.fromkeys(categories))
        self.limit = limit
        self.timeout = timeout
        self.opener = opener
        self.endpoints = tuple(dict.fromkeys(endpoints or (endpoint,)))
        if not self.endpoints or any(not isinstance(value, str) or not value.startswith("https://")
                                     for value in self.endpoints):
            raise ValueError("OSM endpoints must be HTTPS URLs")
        if not isinstance(max_retries, int) or not 0 <= max_retries <= 3:
            raise ValueError("OSM max retries must be 0-3")
        self.max_retries = max_retries
        self.sleeper = sleeper or time.sleep
        self.endpoint = self.endpoints[0]
        self.name_query = name_query.strip() if name_query else None
        self.last_collection_stats = {"http_requests": 0, "retry_count": 0,
                                      "endpoint": None, "endpoints": list(self.endpoints)}

    def query(self):
        bbox = ",".join(str(value) for value in self.scope.bbox)
        selectors = []
        for category in self.categories:
            key, value = CATEGORY_TAGS[category]
            for obj_type in ("node", "way", "relation"):
                base = '{}["{}"="{}"]'.format(obj_type, key, value)
                if self.name_query:
                    pattern = json.dumps("^" + re.escape(self.name_query) + "$", ensure_ascii=False)
                    for name_key in ("name", "name:en"):
                        selectors.append('{}["{}"~{},i]({});'.format(base, name_key, pattern, bbox))
                else:
                    selectors.append('{}({});'.format(base, bbox))
        return "[out:json][timeout:{}];({});out center {};".format(
            self.timeout, "".join(selectors), self.limit)

    def collect(self, city):
        if city != self.scope.name:
            raise ValueError("collection key must match GeoScope name")
        self.last_collection_stats = {"http_requests": 0, "retry_count": 0,
                                      "endpoint": None, "endpoints": list(self.endpoints)}
        last_error = None
        jitter = lambda: random.uniform(0.0, 0.25) if len(self.endpoints) > 1 else 0.0
        for endpoint in self.endpoints:
            for attempt in range(self.max_retries + 1):
                request = Request(endpoint, data=urlencode({"data": self.query()}).encode("utf-8"),
                                  headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
                self.last_collection_stats["http_requests"] += 1
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
                    self.last_collection_stats["endpoint"] = endpoint
                    seen = set()
                    for element in elements:
                        item = parse_element(element, self.scope)
                        if item.external_id not in seen:
                            seen.add(item.external_id)
                            yield item
                    return
                except HTTPError as exc:
                    retryable = exc.code == 429 or 500 <= exc.code <= 599
                    last_error = ValueError("Overpass HTTP {}".format(exc.code))
                    if not retryable or attempt >= self.max_retries:
                        break
                    retry_after = 1.0
                    if exc.code == 429:
                        try:
                            retry_after = max(0.0, min(float(exc.headers.get("Retry-After", "1")), 60.0))
                        except (AttributeError, TypeError, ValueError):
                            retry_after = 1.0
                    self.last_collection_stats["retry_count"] += 1
                    self.sleeper(retry_after + jitter())
                except OSError as exc:
                    retryable = isinstance(exc, (URLError, TimeoutError)) or "timed out" in str(exc).lower()
                    last_error = ValueError("Overpass network/timeout error: {}".format(exc))
                    if not retryable or attempt >= self.max_retries:
                        break
                    self.last_collection_stats["retry_count"] += 1
                    self.sleeper((2 ** attempt) + jitter())
                except ValueError:
                    raise
        raise last_error or ValueError("Overpass endpoints unavailable")
