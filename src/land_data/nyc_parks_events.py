"""Deterministic collector for NYC Parks' rolling 14-day public events feed.

The dataset is already a structured event source.  This adapter deliberately
does not create POIs, geocode park names, or call an LLM.  It pages by the
stable ``guid`` key and only applies disappearance lifecycle changes after an
entire successful snapshot has been consumed.
"""
import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .adapters import AdapterCapability, SourceItem
from .models import (Event, EventCandidate, FieldProvenance, RawSourceRecord,
                     SourceCollectionCheckpoint, SourceContent)
from .util import safe_source_url, stable_id, utc_now


ENDPOINT = "https://data.cityofnewyork.us/resource/w3wp-dpdi.json"
DATASET_ID = "w3wp-dpdi"
CHECKPOINT_STRATEGY = "NYC_PARKS_EVENTS_V1"
EVENT_TIMEZONE = "America/New_York"
_LOCAL_ZONE = ZoneInfo(EVENT_TIMEZONE)

EVENT_FIELDS = (
    "guid,title,instructor,link,description,registration_url,registration_description,"
    "parkids,parknames,starttime,endtime,contact_phone,location,categories,coordinates"
)


def parse_nyc_event_datetime(value: Any) -> Optional[str]:
    """Parse Socrata's floating NYC timestamp and return a UTC ISO timestamp.

    A floating timestamp has no offset and is explicitly interpreted in
    America/New_York.  Invalid or blank values return ``None`` so the event
    remains reviewable instead of receiving a guessed time.
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # A few Socrata exports use a space and omit fractional seconds.
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_LOCAL_ZONE)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_past(value: Optional[str], now: str) -> bool:
    if not value:
        return False
    try:
        event_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
        now_time = datetime.fromisoformat(now.replace("Z", "+00:00"))
        return event_time <= now_time
    except (AttributeError, TypeError, ValueError):
        return False


def _split_values(value: Any, *, split_commas: bool = False) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    # Park IDs may be comma separated.  Names keep commas because a park name
    # itself can contain one.
    pattern = r"[;,|\n]+" if split_commas else r"[;|\n]+"
    return [item.strip() for item in re.split(pattern, text) if item.strip()]


def _coordinates(value: Any) -> tuple:
    if value is None:
        return None, None
    if isinstance(value, dict):
        if "latitude" in value and "longitude" in value:
            return _number(value.get("latitude")), _number(value.get("longitude"))
        if "lat" in value and "lon" in value:
            return _number(value.get("lat")), _number(value.get("lon"))
        values = value.get("coordinates")
        if isinstance(values, (list, tuple)) and len(values) >= 2:
            # GeoJSON order is longitude, latitude.
            return _number(values[1]), _number(values[0])
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _number(value[0]), _number(value[1])
    text = str(value).strip()
    if not text:
        return None, None
    try:
        decoded = json.loads(text)
        if decoded != value:
            return _coordinates(decoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    if len(numbers) < 2:
        return None, None
    first, second = _number(numbers[0]), _number(numbers[1])
    if first is None or second is None:
        return None, None
    # The dataset documents this text field as latitude,longitude.  Do not
    # silently swap out-of-range values: they must remain reviewable as bad
    # source coordinates.
    return first, second


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_url(value: Any) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    try:
        return safe_source_url(str(value).strip())
    except ValueError:
        return None


def _event_row(row: Dict[str, Any]) -> Dict[str, Any]:
    external_id = str(row.get("guid", "")).strip()
    if not external_id:
        raise ValueError("NYC Parks event row lacks guid")
    latitude, longitude = _coordinates(row.get("coordinates"))
    link = _safe_url(row.get("link"))
    registration_url = _safe_url(row.get("registration_url"))
    starts_at = parse_nyc_event_datetime(row.get("starttime"))
    ends_at = parse_nyc_event_datetime(row.get("endtime"))
    reasons = []
    if not starts_at or not ends_at or (starts_at and ends_at and ends_at < starts_at):
        reasons.append("INVALID_EVENT_TIME")
    if latitude is None or longitude is None:
        reasons.append("MISSING_EVENT_COORDINATES")
    elif not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        reasons.append("INVALID_EVENT_COORDINATES")
    return {
        "source_event_id": external_id,
        "name": str(row.get("title", "") or "").strip(),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "starts_at_raw": row.get("starttime"),
        "ends_at_raw": row.get("endtime"),
        "event_url": link,
        "registration_url": registration_url,
        "registration_description": str(row.get("registration_description", "") or ""),
        "park_ids": _split_values(row.get("parkids"), split_commas=True),
        "park_names": _split_values(row.get("parknames")),
        "location": str(row.get("location", "") or ""),
        "categories": _split_values(row.get("categories")),
        "description": str(row.get("description", "") or ""),
        "instructor": str(row.get("instructor", "") or ""),
        "contact_phone": str(row.get("contact_phone", "") or ""),
        "latitude": latitude,
        "longitude": longitude,
        "timezone": EVENT_TIMEZONE,
        "review_reasons": reasons,
        "field_sources": {
            "name": "title", "starts_at": "starttime", "ends_at": "endtime",
            "event_url": "link", "registration_url": "registration_url",
            "park_ids": "parkids", "park_names": "parknames", "location": "location",
            "categories": "categories", "description": "description",
            "instructor": "instructor", "contact_phone": "contact_phone",
            "latitude": "coordinates", "longitude": "coordinates",
        },
    }


class NYCParksEventsAdapter:
    name = "nyc-parks-events"
    # A failed page can leave earlier Raw rows persisted before parsing.  On
    # the eventual resumed completion, parse all this source's latest rows so
    # those earlier events receive their derived Event records as well.
    parse_all_source_records = True
    capability = AdapterCapability("PUBLIC", supports_search=True, supports_content_text=True,
                                   supports_title=True)

    def __init__(self, limit=100, max_pages=20, timeout=15, opener=urlopen,
                 *, storage=None, collection_key="new-york", max_retries=1,
                 retry_delay=0.0):
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("NYC Parks events page limit must be 1-1000")
        if not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
            raise ValueError("NYC Parks events max pages must be 1-100")
        if not isinstance(timeout, int) or not 2 <= timeout <= 60:
            raise ValueError("NYC Parks events timeout must be 2-60 seconds")
        self.limit, self.max_pages, self.timeout, self.opener = limit, max_pages, timeout, opener
        self.storage, self.collection_key = storage, collection_key
        self.max_retries, self.retry_delay = max_retries, retry_delay
        self.last_collection_stats: Dict[str, Any] = {}
        self.seen_external_ids = set()

    @property
    def checkpoint_key(self):
        return self.collection_key

    def _checkpoint(self):
        return self.storage.get_checkpoint(self.name, self.checkpoint_key) if self.storage else None

    @staticmethod
    def raw_identity_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Exclude the per-run snapshot marker from Raw snapshot identity.

        The marker is persisted for complete-snapshot absence detection, but a
        same event with unchanged source fields must remain a duplicate across
        daily runs rather than creating a new Raw row every day.
        """
        result = dict(payload)
        metadata = dict(result.get("source_metadata", {}))
        metadata.pop("snapshot_id", None)
        result["source_metadata"] = metadata
        return result

    def _request(self, params: Dict[str, str]) -> List[Dict[str, Any]]:
        request = Request(ENDPOINT + "?" + urlencode(params),
                          headers={"User-Agent": "LAND-Data/nyc-parks-events"})
        attempt = 0
        while True:
            self.last_collection_stats["http_requests"] += 1
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    body = response.read(500_001)
                if len(body) > 500_000:
                    raise ValueError("NYC Parks events response exceeded 500KB")
                rows = json.loads(body)
                if not isinstance(rows, list) or len(rows) > self.limit:
                    raise ValueError("NYC Parks events malformed or oversized response")
                return rows
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                if attempt >= self.max_retries:
                    raise ValueError("NYC Parks events request failed: {}".format(type(exc).__name__)) from exc
                attempt += 1
                self.last_collection_stats["retry_count"] += 1
                if self.retry_delay:
                    time.sleep(self.retry_delay * (2 ** (attempt - 1)))

    def _save_checkpoint(self, snapshot_token: str, cursor: Optional[str], complete: bool):
        if self.storage is not None:
            self.storage.put(SourceCollectionCheckpoint(
                id=stable_id("source-checkpoint", self.name, self.checkpoint_key),
                source=self.name, collection_key=self.checkpoint_key,
                cursor_updated_at=snapshot_token, cursor_row_id=cursor,
                cycle_complete=complete, updated_at=utc_now(), strategy=CHECKPOINT_STRATEGY))

    def _starting_cursor(self):
        checkpoint = self._checkpoint()
        if (checkpoint and checkpoint.strategy == CHECKPOINT_STRATEGY and
                not checkpoint.cycle_complete and checkpoint.cursor_updated_at):
            return checkpoint.cursor_updated_at, checkpoint.cursor_row_id
        # Each complete rolling-window read gets a unique token.  A resumed
        # partial run keeps its prior token so persisted earlier pages belong
        # to the same snapshot for absence detection.
        return utc_now(), None

    def _page(self, cursor: Optional[str]) -> List[Dict[str, Any]]:
        where = "guid IS NOT NULL"
        if cursor:
            where += " AND guid > {}".format(cursor)
        return self._request({"$select": EVENT_FIELDS, "$where": where,
                              "$order": "guid ASC", "$limit": str(self.limit)})

    def collect(self, city: str) -> Iterable[SourceItem]:
        if city != "new-york":
            raise ValueError("NYC Parks events source requires city new-york")
        self.last_collection_stats = {
            "http_requests": 0, "retry_count": 0, "request_breakdown": {"events_page": 0},
            "endpoint": ENDPOINT, "pages": 0, "cycle_complete": False,
        }
        self.seen_external_ids = set()
        snapshot_token, cursor = self._starting_cursor()
        self.last_collection_stats["snapshot_token"] = snapshot_token
        for _page_number in range(self.max_pages):
            rows = self._page(cursor)
            self.last_collection_stats["request_breakdown"]["events_page"] += 1
            self.last_collection_stats["pages"] += 1
            if not rows:
                self._save_checkpoint(snapshot_token, cursor, True)
                self.last_collection_stats.update({"cycle_complete": True,
                                                   "run_status": "COMPLETE",
                                                   "next_run_status": "NOT_NEEDED"})
                return
            for row in rows:
                event = _event_row(row)
                external_id = event["source_event_id"]
                self.seen_external_ids.add(external_id)
                source_url = event.get("event_url") or ENDPOINT + "?guid=" + external_id
                payload = {
                    "content_type": "GOVERNMENT_EVENT",
                    "title": event["name"], "text": event["description"],
                    "source_kind": "structured_event", "events": [event],
                    "source_metadata": {"dataset_id": DATASET_ID,
                                         "event_timezone": EVENT_TIMEZONE,
                                         # Avoid the policy's secret-key
                                         # scrubber, which drops nested keys
                                         # containing the word "token".
                                         "snapshot_id": snapshot_token},
                }
                yield SourceItem(self.name, external_id, source_url, payload)
            cursor = str(rows[-1].get("guid"))
            self._save_checkpoint(snapshot_token, cursor, False)
            self.last_collection_stats.update({"cursor_guid": cursor,
                                               "run_status": "CONTINUE_REQUIRED",
                                               "next_run_status": "CONTINUE_REQUIRED"})
        self.last_collection_stats.update({"run_status": "CONTINUE_REQUIRED",
                                           "next_run_status": "CONTINUE_REQUIRED"})

    def finalize_snapshot(self, storage, collection_key: str) -> None:
        """Apply absence/expiry only after a complete successful page walk."""
        if self.last_collection_stats.get("run_status") != "COMPLETE":
            return
        now = utc_now()
        snapshot_token = self.last_collection_stats.get("snapshot_token")
        # A resumed run only yields pages after its checkpoint.  Include all
        # Raw rows already persisted for this same token so the first pages
        # are not mistaken for missing events when the final page completes.
        seen_external_ids = set(self.seen_external_ids)
        if snapshot_token:
            for raw in storage.all(RawSourceRecord):
                if raw.source != self.name or raw.city != collection_key:
                    continue
                metadata = raw.payload.get("source_metadata", {})
                if metadata.get("snapshot_id") == snapshot_token:
                    seen_external_ids.add(raw.external_id)
        for candidate in storage.all(EventCandidate):
            if not candidate.source_event_id:
                continue
            content = storage.get(SourceContent, candidate.source_content_id)
            if not content or content.platform != self.name or content.collection_scope != collection_key:
                continue
            if candidate.source_event_id in seen_external_ids:
                lifecycle = "EXPIRED" if _is_past(candidate.ends_at, now) else "ACTIVE"
                reason = "EVENT_EXPIRED" if lifecycle == "EXPIRED" else None
            else:
                lifecycle = "EXPIRED" if _is_past(candidate.ends_at, now) else "UNAVAILABLE"
                reason = "EVENT_EXPIRED" if lifecycle == "EXPIRED" else "EVENT_MISSING_FROM_COMPLETE_SNAPSHOT"
            candidate.lifecycle_status = lifecycle
            if lifecycle != "ACTIVE" or reason:
                candidate.review_status = "NEEDS_REVIEW"
                candidate.review_reasons = [reason] if reason else ["EVENT_REQUIRES_REVIEW"]
            storage.put(candidate)
            event = storage.get(Event, stable_id("event", candidate.id))
            if event:
                event.lifecycle_status = lifecycle
                event.review_status = candidate.review_status
                event.review_reasons = list(candidate.review_reasons)
                storage.put(event)
