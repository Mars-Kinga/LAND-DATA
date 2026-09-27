"""Small, allowlisted collector for review-only JFK arrival guidance; no model inference."""
import hashlib
import re
from html import unescape
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import OfficialGuidancePageState, OfficialGuidanceSnapshot
from .util import stable_id, utc_now


class OfficialGuidanceUnavailable(ValueError):
    pass


class OfficialGuidanceParseError(ValueError):
    pass


def _text(value):
    value = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", value)).split())


class JFKArrivalGuidanceAdapter:
    """Fetch exactly two first-party public pages and retain only stable arrival snippets.

    It intentionally does not yield SourceItem: this is page evidence for human review,
    not a restaurant/POI input and not a verified LAND Guide.
    """
    name = "jfk-arrival-guidance"
    PAGES = (
        ("public-transport", "https://www.jfkairport.com/transportation/public-transport",
         "Navigate JFK with the AirTrain", "The AirTrain connects passengers to terminals, airport parking lots, the hotel shuttle area, the rental car center, and public transportation."),
        ("airtrain-connections", "https://www.jfkairport.com/transportation/airtrain",
         "AirTrain is the best way to navigate JFK", "JFK’s AirTrain connects you to parking areas, the Long Island Rail Road (LIRR), New York City Subway and local buses."),
        ("airtrain-public-transit", "https://www.jfkairport.com/transportation/airtrain",
         "To and from NYC public transportation", "You can access public transportation by taking the AirTrain to Jamaica or Howard Beach Station."),
    )

    def __init__(self, timeout=15, opener=urlopen):
        if not isinstance(timeout, int) or not 2 <= timeout <= 30:
            raise ValueError("JFK guidance timeout must be 2-30 seconds")
        self.timeout, self.opener = timeout, opener

    def _fetch(self, url):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "www.jfkairport.com":
            raise ValueError("JFK guidance URL must be allowlisted HTTPS")
        request = Request(url, headers={"User-Agent": "LAND-Data/official-guidance"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read(500_001)
        except OSError as exc:
            raise OfficialGuidanceUnavailable("official page unavailable") from exc
        if len(body) > 500_000:
            raise OfficialGuidanceUnavailable("official page response exceeded 500KB limit")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OfficialGuidanceUnavailable("official page was not UTF-8 HTML") from exc

    def _find_snippet(self, html, heading, expected):
        heading_pattern = re.escape(heading).replace("\\ ", r"\s+")
        match = re.search(r"<h[23][^>]*>.*?" + heading_pattern +
                          r".*?</h[23]>(.*?)(?=<h[23][^>]*>|$)", html, flags=re.I | re.S)
        if not match:
            raise OfficialGuidanceParseError("allowlisted section heading was not reliably located")
        section_html, section_version = match.group(0), _text(match.group(0))
        expected = _text(expected)
        if expected in section_version:
            return expected, section_version
        # A replacement sentence is evidence of a page edit, not an outage. Retain only
        # one bounded paragraph that excludes volatile fares, schedules and notices.
        candidates = [_text(value) for value in re.findall(r"<(?:p|li)[^>]*>(.*?)</(?:p|li)>",
                                                            section_html, flags=re.I | re.S)]
        prohibited = re.compile(r"\$|\bfare\b|\bminutes?\b|\bevery\s+\d|\bschedule\b|\bconstruction\b|\bdelay", re.I)
        safe = next((value for value in candidates if 1 <= len(value) <= 500 and not prohibited.search(value)), None)
        if not safe:
            raise OfficialGuidanceParseError("section changed but no bounded stable evidence was safely extractable")
        return safe, section_version

    def run_guidance(self, storage, _collection_key):
        html_by_url, sections, network_errors, parse_errors = {}, {}, {}, {}
        for url in {item[1] for item in self.PAGES}:
            try:
                html_by_url[url] = self._fetch(url)
            except OfficialGuidanceUnavailable as exc:
                network_errors[url] = str(exc)
        for page_key, url, heading, expected in self.PAGES:
            if url in network_errors:
                now = utc_now()
                state_id = stable_id("official-guidance-page", self.name, page_key)
                existing = storage.get(OfficialGuidancePageState, state_id)
                storage.put(OfficialGuidancePageState(
                    id=state_id, source=self.name, page_key=page_key, source_url=url,
                    current_snapshot_id=existing.current_snapshot_id if existing else None,
                    last_checked_at=now, availability="UNAVAILABLE", failure_reason=network_errors[url],
                    parse_status="NOT_ATTEMPTED"))
                continue
            try:
                content, version_material = self._find_snippet(html_by_url[url], heading, expected)
                sections[page_key] = (url, heading, content, version_material)
            except OfficialGuidanceParseError as exc:
                now = utc_now()
                state_id = stable_id("official-guidance-page", self.name, page_key)
                existing = storage.get(OfficialGuidancePageState, state_id)
                storage.put(OfficialGuidancePageState(
                    id=state_id, source=self.name, page_key=page_key, source_url=url,
                    current_snapshot_id=existing.current_snapshot_id if existing else None,
                    last_checked_at=now, availability="AVAILABLE", parse_status="PARSE_ERROR",
                    parse_failure_reason=str(exc)))
                parse_errors[page_key] = str(exc)

        counts = {"new_count": 0, "updated_count": 0, "duplicate_count": 0}
        now = utc_now()
        for page_key, (url, heading, content, version_material) in sections.items():
            content_version = hashlib.sha256((url + "\n" + heading + "\n" + version_material).encode()).hexdigest()
            snapshot_id = stable_id("official-guidance-snapshot", self.name, page_key, content_version)
            state_id = stable_id("official-guidance-page", self.name, page_key)
            state = storage.get(OfficialGuidancePageState, state_id)
            if state is None or state.current_snapshot_id is None:
                counts["new_count"] += 1
                storage.put(OfficialGuidanceSnapshot(
                    id=snapshot_id, source=self.name, page_key=page_key, source_url=url,
                    section_location="h2/h3: " + heading, content=content,
                    content_version=content_version, collected_at=now))
            elif state.current_snapshot_id == snapshot_id:
                counts["duplicate_count"] += 1
            else:
                counts["updated_count"] += 1
                storage.put(OfficialGuidanceSnapshot(
                    id=snapshot_id, source=self.name, page_key=page_key, source_url=url,
                    section_location="h2/h3: " + heading, content=content,
                    content_version=content_version, collected_at=now,
                    review_reasons=["OFFICIAL_GUIDANCE_CONTENT_CHANGED"] ))
            storage.put(OfficialGuidancePageState(
                id=state_id, source=self.name, page_key=page_key, source_url=url,
                current_snapshot_id=snapshot_id, last_checked_at=now, availability="AVAILABLE",
                parse_status="OK"))
        result = {**counts, "parse_error_count": len(parse_errors),
                  "collection": {"pages": len(html_by_url), "sections": len(self.PAGES), "request_limit_bytes": 500000,
                                 "mode": "allowlisted-stable-sections-only"}}
        self.last_guidance_counts = result
        if network_errors:
            raise OfficialGuidanceUnavailable("{} official page(s) unavailable".format(len(network_errors)))
        if parse_errors:
            result["run_status"] = "SUCCEEDED_WITH_REVIEW"
            result["next_run_status"] = "REVIEW_REQUIRED"
        return result
