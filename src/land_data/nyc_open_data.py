"""Bounded, page-safe NYC Open Data restaurant snapshot collection; no model inference."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .adapters import AdapterCapability, SourceItem
from .models import SourceCollectionCheckpoint
from .util import stable_id, utc_now


ENDPOINT = "https://data.cityofnewyork.us/resource/43nn-pn8j.json"
# One group per CAMIS. record_date has a distinct alias so SoQL does not resolve WHERE to it.
SNAPSHOT_FIELDS = (
    "camis,max(dba) as dba,max(boro) as boro,max(building) as building,max(street) as street,"
    "max(zipcode) as zipcode,max(cuisine_description) as cuisine_description,"
    "max(inspection_date) as inspection_date,max(record_date) as snapshot_record_date,"
    "max(latitude) as latitude,max(longitude) as longitude,"
    "min(dba) as min_dba,min(boro) as min_boro,min(building) as min_building,"
    "min(street) as min_street,min(zipcode) as min_zipcode,"
    "min(cuisine_description) as min_cuisine_description,min(latitude) as min_latitude,"
    "min(longitude) as min_longitude"
)
IDENTITY_FIELDS = (
    "camis,max(dba) as agg_dba,max(boro) as agg_boro,max(building) as agg_building,"
    "max(street) as agg_street,max(zipcode) as agg_zipcode,"
    "max(cuisine_description) as agg_cuisine_description,max(inspection_date) as agg_inspection_date,"
    "max(record_date) as agg_snapshot_record_date"
)
COORDINATE_FIELDS = "camis,max(latitude) as latitude,max(longitude) as longitude"
CONSISTENCY_FIELDS = (
    "camis,count(distinct dba) as dba_values",
    "camis,count(distinct boro) as boro_values",
    "camis,count(distinct building) as building_values",
    "camis,count(distinct street) as street_values",
    "camis,count(distinct zipcode) as zipcode_values",
    "camis,count(distinct cuisine_description) as cuisine_values",
    "camis,count(distinct latitude) as latitude_values",
    "camis,count(distinct longitude) as longitude_values",
)
DEFAULT_INITIAL_UPDATED_AT = "2026-09-01T00:00:00.000"
CHECKPOINT_STRATEGY = "NYC_CAMIS_SNAPSHOT_V1"


def _as_iso(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("NYC Open Data record_date is invalid") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _day_bounds(value):
    start = _as_iso(value).astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    # record_date is exposed by this dataset as a text timestamp without a zone
    # suffix. Keeping that representation avoids Socrata text-vs-timestamp errors
    # while retaining an exact half-open day window.
    return tuple(item.isoformat(timespec="milliseconds").replace("+00:00", "") for item in (start, end))


class NYCOpenDataRestaurantAdapter:
    name = "nyc-open-data-restaurants"
    capability = AdapterCapability("PUBLIC", supports_search=True, supports_content_text=False)

    def __init__(self, borough="Manhattan", limit=100, timeout=15, opener=urlopen, *,
                 storage=None, collection_key="new-york", max_pages=20,
                 initial_updated_at=DEFAULT_INITIAL_UPDATED_AT, overlap_seconds=0,
                 safety_rescan_days=7):
        if borough not in ("Manhattan", "Queens", "Brooklyn", "Bronx", "Staten Island"):
            raise ValueError("unsupported NYC borough")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("NYC Open Data page limit must be 1-100")
        if not isinstance(max_pages, int) or not 1 <= max_pages <= 20:
            raise ValueError("NYC Open Data max pages must be 1-20")
        if not isinstance(timeout, int) or not 2 <= timeout <= 30:
            raise ValueError("NYC Open Data timeout must be 2-30 seconds")
        _as_iso(initial_updated_at)
        if not isinstance(safety_rescan_days, (int, float)) or safety_rescan_days < 0:
            raise ValueError("NYC safety rescan interval must be non-negative")
        self.borough, self.limit, self.timeout, self.opener = borough, limit, timeout, opener
        self.storage, self.collection_key = storage, collection_key
        self.max_pages, self.initial_updated_at = max_pages, initial_updated_at
        self.safety_rescan_days = safety_rescan_days
        self.last_collection_stats = {}

    @property
    def checkpoint_key(self):
        return "{}:{}".format(self.collection_key, self.borough)

    def _checkpoint(self):
        return self.storage.get_checkpoint(self.name, self.checkpoint_key) if self.storage else None

    def _request(self, params, request_kind=None):
        if request_kind and self.last_collection_stats:
            self.last_collection_stats["http_requests"] += 1
            breakdown = self.last_collection_stats.setdefault("request_breakdown", {})
            breakdown[request_kind] = breakdown.get(request_kind, 0) + 1
            if request_kind == "snapshot_anchor":
                self.last_collection_stats["snapshot_requests"] += 1
        request = Request(ENDPOINT + "?" + urlencode(params),
                          headers={"User-Agent": "LAND-Data/nyc-open-data"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                rows = json.loads(response.read(500_001))
        except OSError as exc:
            raise ValueError("NYC Open Data network error") from exc
        if not isinstance(rows, list) or len(rows) > self.limit:
            raise ValueError("NYC Open Data malformed or oversized response")
        return rows

    def _latest_snapshot_day(self):
        rows = self._request({"$select": "max(record_date) as latest_record_date",
                              "$where": "boro='{}' AND record_date >= '{}'".format(
                                  self.borough, self.initial_updated_at), "$limit": "1"},
                             "snapshot_anchor")
        if len(rows) != 1 or not rows[0].get("latest_record_date"):
            raise ValueError("NYC Open Data has no bounded current snapshot")
        return _day_bounds(rows[0]["latest_record_date"])[0]

    def _save_checkpoint(self, snapshot_day, camis, complete):
        if self.storage is not None:
            self.storage.put(SourceCollectionCheckpoint(
                id=stable_id("source-checkpoint", self.name, self.checkpoint_key),
                source=self.name, collection_key=self.checkpoint_key,
                cursor_updated_at=snapshot_day, cursor_row_id=str(camis),
                cycle_complete=complete, updated_at=utc_now(), strategy=CHECKPOINT_STRATEGY))

    def _starting_cursor(self):
        checkpoint = self._checkpoint()
        if (checkpoint and checkpoint.strategy == CHECKPOINT_STRATEGY and
                not checkpoint.cycle_complete and checkpoint.cursor_updated_at):
            return checkpoint.cursor_updated_at, checkpoint.cursor_row_id
        return self._latest_snapshot_day(), None

    def _safe_to_skip_completed_cycle(self, checkpoint, snapshot_day):
        if not checkpoint or not checkpoint.cycle_complete or checkpoint.cursor_updated_at != snapshot_day:
            return False
        if self.safety_rescan_days == 0:
            return False
        try:
            age = _as_iso(utc_now()) - _as_iso(checkpoint.updated_at)
        except ValueError:
            return False
        return age < timedelta(days=self.safety_rescan_days)

    def _page(self, snapshot_day, camis):
        next_day = _day_bounds(snapshot_day)[1]
        clauses = ["boro='{}'".format(self.borough), "record_date >= '{}'".format(snapshot_day),
                   "record_date < '{}'".format(next_day)]
        if camis is not None:
            clauses.append("camis > '{}'".format(camis))
        query = {"$where": " AND ".join(clauses), "$group": "camis",
                 "$order": "camis ASC", "$limit": str(self.limit)}
        rows = self._request({**query, "$select": IDENTITY_FIELDS}, "camis_page")
        coordinates = self._request({**query, "$select": COORDINATE_FIELDS}, "coordinate")
        consistency_rows = [self._request({**query, "$select": fields}, "conflict_check")
                            for fields in CONSISTENCY_FIELDS]
        coordinate_by_camis = {row.get("camis"): row for row in coordinates}
        consistency_by_camis = {}
        for page in consistency_rows:
            for row in page:
                consistency_by_camis.setdefault(row.get("camis"), {}).update(row)
        camis_set = {row.get("camis") for row in rows}
        if (camis_set != set(coordinate_by_camis) or camis_set != set(consistency_by_camis) or
                any(set(page_row.get("camis") for page_row in page) != camis_set
                    for page in consistency_rows)):
            raise ValueError("NYC Open Data grouped query pages disagree")
        for row in rows:
            for source_field, output_field in (
                ("agg_dba", "dba"), ("agg_boro", "boro"), ("agg_building", "building"),
                ("agg_street", "street"), ("agg_zipcode", "zipcode"),
                ("agg_cuisine_description", "cuisine_description"),
                ("agg_inspection_date", "inspection_date"),
                ("agg_snapshot_record_date", "snapshot_record_date")):
                if source_field in row:
                    row[output_field] = row.pop(source_field)
            row.update({key: value for key, value in coordinate_by_camis[row["camis"]].items()
                        if key != "camis"})
            row.update({key: value for key, value in consistency_by_camis[row["camis"]].items()
                        if key != "camis"})
        for row in rows:
            if not isinstance(row, dict) or not row.get("camis") or not row.get("snapshot_record_date"):
                raise ValueError("NYC Open Data row lacks a usable snapshot cursor")
        return rows

    def _item(self, row):
        latitude = longitude = None
        try:
            latitude, longitude = float(row["latitude"]), float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            pass
        name = row.get("dba", "").strip()
        if not name:
            return None
        address = " ".join(value for value in (row.get("building", ""), row.get("street", "")) if value)
        snapshot = {"record_date": row["snapshot_record_date"],
                    "latest_inspection_date": row.get("inspection_date", ""),
                    "snapshot_kind": "CAMIS_AGGREGATE"}
        aggregate_conflicts = [field for field, alias in (
            ("dba", "dba_values"), ("boro", "boro_values"), ("building", "building_values"),
            ("street", "street_values"), ("zipcode", "zipcode_values"),
            ("cuisine_description", "cuisine_values"), ("latitude", "latitude_values"),
            ("longitude", "longitude_values")) if int(row.get(alias, "0")) > 1]
        location = {"name": name, "city": "New York", "country": "US", "category": "restaurant",
                    "address": address, "latitude": latitude, "longitude": longitude,
                    "coordinate_verified": latitude is not None and longitude is not None,
                    "coordinate_precision": "GOVERNMENT_DATASET" if latitude is not None and longitude is not None
                    else "UNAVAILABLE",
                    "source_tags": {"camis": row["camis"], "boro": row.get("boro", ""),
                                    "cuisine": row.get("cuisine_description", ""), **snapshot},
                    "field_sources": {"name": "max(dba)", "address": "max(building)+max(street)",
                                      "latitude": "max(latitude)", "longitude": "max(longitude)",
                                      "category": "max(cuisine_description)"},
                    "evidence": [{"type": "GOVERNMENT_RECORD", "raw_value": row["camis"], "confidence": 0.95},
                                 *([{"type": "GOVERNMENT_COORDINATES",
                                    "raw_value": "{},{}".format(latitude, longitude), "confidence": 0.92}]
                                   if latitude is not None and longitude is not None else [])]}
        if latitude is None or longitude is None:
            location["review_reasons"] = ["MISSING_COORDINATES"]
            location["extraction_context"] = {"reason": "official CAMIS has no reliable latitude/longitude",
                                               "camis": row["camis"], "provenance": "NYC Open Data 43nn-pn8j"}
        if aggregate_conflicts:
            location["review_reasons"] = ["NYC_AGGREGATE_FIELD_CONFLICT"]
            location["extraction_context"] = {"aggregate_conflicts": aggregate_conflicts,
                                                "detector": "count(distinct field) within grouped CAMIS"}
        return SourceItem(self.name, row["camis"], ENDPOINT + "/" + row["camis"],
                          {"content_type": "GOVERNMENT_RECORD", "title": name, "text": "",
                           "source_kind": "structured", "locations": [location],
                           "source_metadata": {"dataset_id": "43nn-pn8j", "borough": self.borough, **snapshot}})

    def collect(self, city):
        if city != "new-york":
            raise ValueError("NYC restaurant source requires city new-york")
        self.last_collection_stats = {"snapshot_requests": 0, "request_pages": 0,
                                      "http_requests": 0, "retry_count": 0,
                                      "request_breakdown": {},
                                      "endpoint": ENDPOINT,
                                      "restaurants_emitted": 0, "cycle_complete": False}
        checkpoint = self._checkpoint()
        anchor_day = self._latest_snapshot_day()
        self.last_collection_stats["snapshot_day"] = anchor_day
        if self._safe_to_skip_completed_cycle(checkpoint, anchor_day):
            self.last_collection_stats.update({"run_status": "NOT_NEEDED", "next_run_status": "NOT_NEEDED",
                                               "cycle_complete": True})
            return
        if (checkpoint and checkpoint.strategy == CHECKPOINT_STRATEGY and
                not checkpoint.cycle_complete and checkpoint.cursor_updated_at):
            snapshot_day, camis = checkpoint.cursor_updated_at, checkpoint.cursor_row_id
        else:
            snapshot_day, camis = anchor_day, None
        seen_camis = set()
        for _page_number in range(self.max_pages):
            rows = self._page(snapshot_day, camis)
            self.last_collection_stats["request_pages"] += 1
            if not rows:
                self._save_checkpoint(snapshot_day, camis or "", True)
                self.last_collection_stats.update({"cycle_complete": True, "run_status": "COMPLETE",
                                                   "next_run_status": "NOT_NEEDED"})
                return
            for row in rows:
                item = self._item(row)
                if item is not None and item.external_id not in seen_camis:
                    seen_camis.add(item.external_id)
                    self.last_collection_stats["restaurants_emitted"] += 1
                    yield item
            camis = rows[-1]["camis"]
            # A generator resumes here only after Pipeline.collect persisted this page's items.
            complete = len(rows) < self.limit
            self._save_checkpoint(snapshot_day, camis, complete)
            self.last_collection_stats.update({"cycle_complete": complete,
                                               "run_status": "COMPLETE" if complete else "CONTINUE_REQUIRED",
                                               "next_run_status": "NOT_NEEDED" if complete else "CONTINUE_REQUIRED"})
            self.last_collection_stats["cursor_camis"] = camis
            if complete:
                return
