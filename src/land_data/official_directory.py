"""Bounded, opt-in parsing of public venue and facility directory pages.

The reference list is supplied for a geographic run. No site search, broad crawl,
HTML body retention, account session, or inferred child coordinates are used.
"""
import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

from .adapters import AdapterCapability, SourceItem
from .geoscope import GeoScope
from .util import normalized_name, stable_id


USER_AGENT = "LAND-Data/POI-Enrichment (bounded official directory validation)"
REFERENCE_HOSTS = {
    "marunouchi_tenant": "www.marunouchi.com",
    "kitte_food_index": "marunouchi.jp-kitte.jp",
    "kitte_access": "marunouchi.jp-kitte.jp",
    "brand_store_index": "saza.coffee",
}


@dataclass(frozen=True)
class DirectoryReference:
    kind: str
    url: str

    def __post_init__(self):
        parsed = urlparse(self.url)
        if (self.kind not in REFERENCE_HOSTS or parsed.scheme != "https" or
                parsed.hostname != REFERENCE_HOSTS[self.kind] or parsed.username or
                parsed.password or parsed.fragment):
            raise ValueError("unsupported official directory reference")
        paths = {"marunouchi_tenant": r"/en/tenants/\d+/?",
                 "kitte_food_index": r"/gb/shopcat\.jsp",
                 "kitte_access": r"/gb/access\.jsp",
                 "brand_store_index": r"/Page/shop\.aspx"}
        if not re.fullmatch(paths[self.kind], parsed.path):
            raise ValueError("unexpected official directory path")


def fetch_html(url: str, timeout: int) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            if urlparse(response.url).hostname != urlparse(url).hostname:
                raise ValueError("official directory redirected to another host")
            body = response.read(1_000_001)
            if len(body) > 1_000_000:
                raise ValueError("official directory page exceeds 1 MB")
            charset = response.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")
    except OSError as exc:
        raise ValueError("official directory request failed: {}".format(exc)) from exc


def resolve_map_url(url: str, timeout: int) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("goo.gl", "maps.app.goo.gl"):
        raise ValueError("unsupported official map link")
    request = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            final_url = response.url
    except OSError as exc:
        raise ValueError("official map link failed: {}".format(exc)) from exc
    if urlparse(final_url).hostname not in ("www.google.com", "www.google.co.jp"):
        raise ValueError("official map link has unexpected destination")
    return final_url


def clean_text(fragment: str) -> str:
    fragment = re.sub(r"<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>",
                      " ", fragment, flags=re.I | re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def coordinate_from_map(url: str, venue_pin: bool):
    """Return a map point only with place identity for a venue, or for a facility."""
    value = html.unescape(url)
    parsed = urlparse(value)
    if parsed.hostname not in ("www.google.com", "www.google.co.jp", "maps.google.co.jp"):
        return None
    query = parse_qs(parsed.query)
    if venue_pin and "query_place_id" not in query and "/maps/place/" not in parsed.path:
        return None
    raw = query.get("query", query.get("q", [""]))[0]
    match = re.fullmatch(r"\s*(-?\d{1,2}\.\d+),\s*(-?\d{1,3}\.\d+)\s*", raw)
    if not match:
        match = re.search(r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{1,3}\.\d+)", value)
    if not match:
        return None
    latitude, longitude = float(match.group(1)), float(match.group(2))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def in_scope(scope: GeoScope, latitude: float, longitude: float) -> bool:
    south, west, north, east = scope.bbox
    return south <= latitude <= north and west <= longitude <= east


def first_map_link(markup: str) -> str:
    links = re.findall(r'href="([^"]+)"', markup, flags=re.I)
    return next((html.unescape(link) for link in links if "google.com/maps/" in link), "")


def tenant_page(markup: str, url: str, scope: GeoScope):
    heading = re.search(r"<h2\b[^>]*>(.*?)</h2>", markup, flags=re.I | re.S)
    if not heading:
        return None
    name = clean_text(heading.group(1))
    vicinity = markup[max(0, heading.start() - 8000):heading.end() + 8000]
    category = "cafe" if re.search(r">\s*Cafe\s*<", vicinity, re.I) else (
        "restaurant" if re.search(r">\s*Restaurant\s*<", vicinity, re.I) else "unknown")
    map_url = first_map_link(markup)
    position = coordinate_from_map(map_url, venue_pin=True) if map_url else None
    if not name or category == "unknown" or not position or not in_scope(scope, *position):
        return None
    address_match = re.search(r">\s*([^<>]{5,120}\bTokyo)\s*<", markup, re.I)
    address = clean_text(address_match.group(1)) if address_match else ""
    return {"name": name, "category": category, "address": address,
            "latitude": position[0], "longitude": position[1], "website": url,
            "coordinate_precision": "VENUE_PIN", "coordinate_source_url": url,
            "field_sources": {"names": "h2", "category": "tenant.category",
                              "address": "tenant.address", "website": "canonical_url",
                              "latitude": "map_link.query", "longitude": "map_link.query",
                              "coordinate_precision": "map_link.query_place_id"}}


def kitte_index(markup: str, index_url: str):
    entries = []
    for block in re.findall(r'<li\s+class="item"[^>]*>.*?</li>', markup, flags=re.I | re.S):
        match = re.search(r'<a\s+href="([^"]*shop\.jsp\?shopid=\d+)"[^>]*>(.*?)</a>\s*<span>(.*?)</span>',
                          block, flags=re.I | re.S)
        if not match:
            continue
        name, descriptor = clean_text(match.group(2)), clean_text(match.group(3)).lower()
        category = ("cafe" if any(word in descriptor for word in ("coffee", "cafe", "café")) else
                    "restaurant" if any(word in descriptor for word in ("restaurant", "dining")) else "unknown")
        entries.append((name, category, urljoin(index_url, html.unescape(match.group(1)))))
    return entries


def brand_stores(markup: str):
    headings = list(re.finditer(r"<h3\b[^>]*>(.*?)</h3>", markup, flags=re.I | re.S))
    stores = []
    for index, heading in enumerate(headings):
        segment = markup[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(markup)]
        phone = re.search(r"(?<!\d)0\d{1,3}-\d{2,4}-\d{4}(?!\d)", clean_text(segment))
        map_link = re.search(r'href="(https://(?:goo\.gl/maps|maps\.app\.goo\.gl)/[^\"]+)"', segment)
        if phone and map_link:
            stores.append((clean_text(heading.group(1)), phone.group(0), html.unescape(map_link.group(1))))
    return stores


def facility_page(markup: str, url: str, scope: GeoScope):
    title = re.search(r"<title[^>]*>(.*?)</title>", markup, flags=re.I | re.S)
    name = clean_text(title.group(1)).split("|")[-1].replace("(for english)", "").strip() if title else ""
    map_links = re.findall(r'(?:src|href)="([^"]+(?:maps/embed|/maps\?)[^"]*)"', markup, flags=re.I)
    position = next((point for link in map_links
                     if (point := coordinate_from_map(link, venue_pin=False)) is not None), None)
    if not name or not position or not in_scope(scope, *position):
        return None
    return {"name": name, "category": "shopping_mall", "address": "",
            "latitude": position[0], "longitude": position[1], "website": url,
            "coordinate_precision": "BUILDING", "coordinate_source_url": url,
            "field_sources": {"names": "title", "category": "facility_directory",
                              "website": "canonical_url", "latitude": "facility_map.q",
                              "longitude": "facility_map.q", "coordinate_precision": "facility_map.q"}}


class OfficialDirectoryAdapter:
    """Fetch a tiny configured reference set and emit structured SourceItems only."""
    name = "official-directory"
    capability = AdapterCapability("PUBLIC", supports_search=True)

    def __init__(self, scope: GeoScope, references, requested_names, timeout=10,
                 fetcher=fetch_html, map_resolver=resolve_map_url):
        references = tuple(references)
        if len(references) > 5 or timeout < 2 or timeout > 20:
            raise ValueError("official enrichment requires at most five references and 2-20s timeout")
        self.scope, self.references = scope, references
        self.requested_names = {normalized_name(name) for name in requested_names if name.strip()}
        self.timeout, self.fetcher, self.map_resolver = timeout, fetcher, map_resolver

    def _item(self, url: str, place: dict, evidence_type: str) -> SourceItem:
        location = {**place, "city": self.scope.city or self.scope.name,
                    "country": self.scope.country, "coordinate_verified": True,
                    "evidence": [{"type": evidence_type, "raw_value": url, "confidence": 0.92},
                                 {"type": "OFFICIAL_MAP_PIN", "raw_value": place["coordinate_precision"],
                                  "confidence": 0.90}]}
        return SourceItem(self.name, stable_id("official-directory", url), url,
                          {"content_type": "OFFICIAL_DIRECTORY", "title": place["name"],
                           "text": "", "source_kind": "structured", "locations": [location]})

    def collect(self, city):
        if city != self.scope.name:
            raise ValueError("collection key must match GeoScope name")
        pages = {ref.kind: (ref.url, self.fetcher(ref.url, self.timeout)) for ref in self.references}
        outputs = []
        tenant = pages.get("marunouchi_tenant")
        if tenant:
            place = tenant_page(tenant[1], tenant[0], self.scope)
            if place and normalized_name(place["name"]) in self.requested_names:
                outputs.append(self._item(tenant[0], place, "OFFICIAL_DIRECTORY_LISTING"))
        kitte = pages.get("kitte_food_index")
        if kitte:
            brand = brand_stores(pages["brand_store_index"][1]) if "brand_store_index" in pages else []
            facility = None
            if "kitte_access" in pages:
                access_url, access_html = pages["kitte_access"]
                facility = facility_page(access_html, access_url, self.scope)
            for name, category, detail_url in kitte_index(kitte[1], kitte[0]):
                if normalized_name(name) not in self.requested_names or category == "unknown":
                    continue
                detail = self.fetcher(detail_url, self.timeout)
                phone_match = re.search(r"(?<!\d)0\d{1,3}-\d{2,4}-\d{4}(?!\d)", clean_text(detail))
                matching_stores = [store for store in brand if phone_match and store[1] == phone_match.group(0)]
                if len(matching_stores) != 1:
                    continue
                _, _, map_link = matching_stores[0]
                position = coordinate_from_map(self.map_resolver(map_link, self.timeout), venue_pin=True)
                if not position or not in_scope(self.scope, *position):
                    continue
                if facility:
                    outputs.append(self._item(pages["kitte_access"][0], facility,
                                              "OFFICIAL_FACILITY_DIRECTORY"))
                place = {"name": name, "category": category, "address": "", "website": detail_url,
                         "latitude": position[0], "longitude": position[1],
                         "coordinate_precision": "VENUE_PIN", "coordinate_source_url": pages["brand_store_index"][0],
                         "parent_name": facility["name"] if facility else "",
                         "field_sources": {"names": "shop_index.name", "category": "shop_index.description",
                                           "website": "shop_detail.url", "latitude": "official_store_map",
                                           "longitude": "official_store_map",
                                           "coordinate_precision": "official_store_map"}}
                outputs.append(self._item(detail_url, place, "OFFICIAL_DIRECTORY_LISTING"))
        seen = set()
        for item in outputs:
            if item.external_id not in seen:
                seen.add(item.external_id)
                yield item
