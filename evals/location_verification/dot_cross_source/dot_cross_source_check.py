#!/usr/bin/env python3
"""Compare frozen NYC screening candidates with two NYC DOT snapshots.

This checker is deliberately conservative and offline.  The current DOT
snapshot has no stable CAMIS key, so it is used only for a *unique normalized
name + full-address retrieval* and for field-level coordinate/status
diagnostics after that retrieval.  The historical DOT snapshot is joined by
``food_service_establishment`` (CAMIS); duplicate applications are retained
and diagnosed instead of selecting one row.

The resulting public summary contains counts, denominators, filters, hashes,
and claim boundaries only.  Per-case values are written to a private JSONL
under ``.local``.  Neither comparison is an independent human Gold label,
and historical DOT rows cannot establish that an establishment is open now.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_QUEUE = (
    ROOT
    / ".local"
    / "baselines"
    / "location-verification-pre-agent-final-2026-10-28"
    / "screening"
    / "location-verification-screening-2026-10-28.jsonl"
)
DEFAULT_DOT_DIR = ROOT / ".local" / "location-verification-live-check" / "dot_batches"
DEFAULT_CURRENT = DEFAULT_DOT_DIR / "current.json"
DEFAULT_HISTORICAL = DEFAULT_DOT_DIR / "historical.json"
DEFAULT_CAPTURE_MANIFEST = DEFAULT_DOT_DIR / "capture-manifest.json"
DEFAULT_PRIVATE_DIR = (
    ROOT / ".local" / "location-verification-live-check" / "dot_cross_source_results"
)
PUBLIC_DIR = Path(__file__).resolve().parent
DEFAULT_PRIVATE_CASES = DEFAULT_PRIVATE_DIR / "dot-cross-source-cases.jsonl"
DEFAULT_MANIFEST = DEFAULT_PRIVATE_DIR / "dot-cross-source-manifest.json"
DEFAULT_PUBLIC_JSON = PUBLIC_DIR / "dot-cross-source-summary.json"
DEFAULT_PUBLIC_MD = PUBLIC_DIR / "dot-cross-source-summary.md"

EXPECTED_PLATFORM = "nyc-open-data-restaurants"
SCHEMA_VERSION = "lv-dot-cross-source-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_value(value: Any) -> str:
    return "sha256:" + sha256_bytes(canonical_json(value).encode("utf-8"))


def canonical_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return str(int(value))
        return str(value).strip()
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text


def normalize_key_name(value: Any) -> str:
    """Canonicalize JSON field names for tolerant source-schema lookup."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(value: Any) -> str:
    return normalize_text(value)


def normalize_address(value: Any) -> str:
    """Apply only deterministic postal abbreviations and punctuation rules."""

    aliases = {
        "ave": "avenue",
        "av": "avenue",
        "blvd": "boulevard",
        "cir": "circle",
        "ct": "court",
        "dr": "drive",
        "e": "east",
        "expy": "expressway",
        "hwy": "highway",
        "ln": "lane",
        "n": "north",
        "ne": "northeast",
        "nw": "northwest",
        "pkwy": "parkway",
        "pl": "place",
        "rd": "road",
        "s": "south",
        "se": "southeast",
        "sq": "square",
        "st": "street",
        "sw": "southwest",
        "ter": "terrace",
        "w": "west",
    }
    tokens: list[str] = []
    for token in normalize_text(value).split():
        ordinal = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", token)
        if ordinal:
            token = ordinal.group(1)
        tokens.append(aliases.get(token, token))
    return " ".join(tokens)


def parse_coordinate(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def valid_pair(latitude: float | None, longitude: float | None) -> bool:
    return (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius_m = 6_371_008.8
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * radius_m * math.asin(min(1.0, math.sqrt(value)))


def _field_map(record: dict[str, Any]) -> dict[str, Any]:
    return {normalize_key_name(key): value for key, value in record.items()}


def get_field(record: dict[str, Any], *aliases: str) -> Any:
    fields = _field_map(record)
    for alias in aliases:
        key = normalize_key_name(alias)
        if key in fields:
            return fields[key]
    return None


def first_text(record: dict[str, Any], aliases: Iterable[str]) -> str:
    for alias in aliases:
        value = get_field(record, alias)
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


NAME_ALIASES = (
    "name",
    "assumed_name_s",
    "restaurant_name",
    "business_name",
    "business_legal_name",
    "doing_business_as",
    "doing_business_as_dba",
    "dba",
    "establishment_name",
    "applicant_name",
    "permittee_name",
)
ADDRESS_ALIASES = (
    "address",
    "full_address",
    "address_full",
    "street_address",
    "restaurant_address",
    "permit_address",
    "site_address",
    "address_line_1",
    "addressline1",
)
BUILDING_ALIASES = (
    "building",
    "building_number",
    "bulding_number",
    "house_number",
    "street_number",
)
STREET_ALIASES = ("street", "street_name", "street_address_line")
BOROUGH_ALIASES = ("borough", "boro")
ZIP_ALIASES = ("zipcode", "zip", "zip_code", "postal_code")
LATITUDE_ALIASES = ("latitude", "lat")
LONGITUDE_ALIASES = ("longitude", "lon", "lng")
STATUS_ALIASES = (
    "status",
    "application_status",
    "permit_status",
    "current_status",
    "sidewalk_cafe_status",
    "approved",
    "approval_status",
    "license_status",
)
CAMIS_ALIASES = (
    "food_service_establishment",
    "camis",
    "food_service_establishment_number",
    "food_service_establishment_id",
    "permit_number",
)
ROW_ID_ALIASES = (
    "objectid",
    "object_id",
    "application_id",
    "permit_id",
    "id",
    "globalid",
)


def source_name(record: dict[str, Any]) -> str:
    return first_text(record, NAME_ALIASES)


def source_address(record: dict[str, Any]) -> str:
    direct = first_text(record, ADDRESS_ALIASES)
    if direct:
        return direct
    building = first_text(record, BUILDING_ALIASES)
    street = first_text(record, STREET_ALIASES)
    if building and street:
        return f"{building} {street}"
    if street:
        return street
    if building:
        return building
    # Historical DOT exports sometimes provide only a composite business
    # address. It is a last resort so a more precise street/building field
    # wins when both are present.
    composite = first_text(record, ("business_address",))
    if composite:
        return composite
    return ""


def source_coordinate(record: dict[str, Any]) -> tuple[float | None, float | None]:
    latitude = parse_coordinate(first_text(record, LATITUDE_ALIASES))
    longitude = parse_coordinate(first_text(record, LONGITUDE_ALIASES))
    if valid_pair(latitude, longitude):
        return latitude, longitude
    # x/y in DOT exports are only accepted when they already look like WGS84;
    # projected state-plane values are intentionally treated as missing.
    x = parse_coordinate(first_text(record, ("x",)))
    y = parse_coordinate(first_text(record, ("y",)))
    if valid_pair(y, x):
        return y, x
    return None, None


def source_status(record: dict[str, Any]) -> str:
    return first_text(record, STATUS_ALIASES)


def source_camis(record: dict[str, Any]) -> str:
    value = first_text(record, CAMIS_ALIASES)
    return canonical_id(value)


def source_row_id(record: dict[str, Any]) -> str:
    return first_text(record, ROW_ID_ALIASES)


def parse_json_file(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata: dict[str, Any] = {}
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in (
            "schema_version",
            "dataset_id",
            "platform",
            "retrieved_at",
            "captured_at",
            "as_of",
            "endpoint",
        ):
            if key in payload:
                metadata[key] = payload[key]
        records = None
        for key in ("records", "results", "rows", "items", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                records = candidate
                break
        if records is None and source_name(payload):
            records = [payload]
        if records is None:
            raise ValueError(f"JSON object has no records/results/rows/items/data array: {path}")
    else:
        raise ValueError(f"top-level JSON must be an array or object: {path}")
    if not all(isinstance(row, dict) for row in records):
        raise ValueError(f"snapshot contains a non-object row: {path}")
    return [dict(row) for row in records], metadata


def load_snapshot(path: Path, label: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    before = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    records, metadata = parse_json_file(path)
    after = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    if before != after:
        raise RuntimeError(f"snapshot changed during read: {path}")
    return records, {
        "label": label,
        "path": str(path),
        "sha256": before[0],
        "bytes": before[1],
        "records": len(records),
        "metadata": metadata,
    }


def load_capture_manifest(
    path: Path,
    current_info: dict[str, Any],
    historical_info: dict[str, Any],
) -> dict[str, Any]:
    """Validate the capture ledger against both raw snapshot hashes.

    The ledger is produced beside the raw API captures.  A stale or edited
    ledger is rejected, and its own hash is retained so a later rerun can
    prove which source metadata (dataset id, update time, query fields) was
    in force.
    """

    if not path.is_file():
        raise FileNotFoundError(path)
    before = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    payload = json.loads(path.read_text(encoding="utf-8"))
    after = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    if before != after:
        raise RuntimeError(f"capture manifest changed during read: {path}")
    if not isinstance(payload, dict) or not isinstance(payload.get("datasets"), dict):
        raise ValueError("capture manifest must contain a datasets object")
    datasets = payload["datasets"]
    validated: dict[str, Any] = {}
    for label, info in (("current", current_info), ("historical", historical_info)):
        entry = datasets.get(label)
        if not isinstance(entry, dict):
            raise ValueError(f"capture manifest missing datasets.{label}")
        files = entry.get("files") or {}
        file_entry = files.get(f"{label}.json")
        if not isinstance(file_entry, dict):
            raise ValueError(f"capture manifest missing datasets.{label}.files.{label}.json")
        if file_entry.get("sha256") != info["sha256"]:
            raise ValueError(f"capture manifest hash mismatch for {label}.json")
        if int(entry.get("rows", -1)) != info["records"]:
            raise ValueError(f"capture manifest row count mismatch for {label}.json")
        validated[label] = {
            "dataset_id": entry.get("dataset_id"),
            "resource_url": entry.get("resource_url"),
            "rows": entry.get("rows"),
            "rows_updated_at": entry.get("rows_updated_at"),
            "select": entry.get("select"),
            "capture_file_mtime": entry.get("capture_file_mtime"),
            "count_matches_payload": entry.get("count_matches_payload"),
        }
    return {
        "path": str(path),
        "sha256": before[0],
        "bytes": before[1],
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "datasets": validated,
    }


def load_queue(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    before = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    rows: list[dict[str, Any]] = []
    invalid = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid queue JSON at line {line_number}") from exc
            if not isinstance(row, dict):
                invalid += 1
                continue
            candidate = row.get("candidate") or {}
            source = row.get("source") or {}
            if not isinstance(candidate, dict) or not isinstance(source, dict):
                invalid += 1
                continue
            rows.append(
                {
                    "line_number": line_number,
                    "screening_id": row.get("screening_id"),
                    "candidate_id": candidate.get("candidate_id"),
                    "name": candidate.get("name"),
                    "address": candidate.get("address"),
                    "city": row.get("city") or candidate.get("city"),
                    "latitude": parse_coordinate(candidate.get("latitude")),
                    "longitude": parse_coordinate(candidate.get("longitude")),
                    "platform": source.get("platform"),
                    "external_id": canonical_id(source.get("external_id")),
                    "source_collected_at": source.get("collected_at"),
                    "source_url": source.get("source_url"),
                }
            )
    after = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    if before != after:
        raise RuntimeError(f"queue changed during read: {path}")
    city_counts = Counter(str(row.get("city") or "") for row in rows)
    platform_counts = Counter(str(row.get("platform") or "") for row in rows)
    nyc_rows = [row for row in rows if normalize_text(row.get("city")) == "new york"]
    camis_rows = [
        row
        for row in nyc_rows
        if row.get("platform") == EXPECTED_PLATFORM and re.fullmatch(r"\d+", row.get("external_id", ""))
    ]
    return rows, {
        "path": str(path),
        "sha256": before[0],
        "bytes": before[1],
        "rows": len(rows),
        "invalid_rows": invalid,
        "city_counts": dict(sorted(city_counts.items())),
        "platform_counts": dict(sorted(platform_counts.items())),
        "nyc_rows": len(nyc_rows),
        "nyc_camis_rows": len(camis_rows),
        "source_collected_at_min": min(
            (str(row["source_collected_at"]) for row in rows if row.get("source_collected_at")),
            default=None,
        ),
        "source_collected_at_max": max(
            (str(row["source_collected_at"]) for row in rows if row.get("source_collected_at")),
            default=None,
        ),
    }


def compact_source_row(record: dict[str, Any], index: int) -> dict[str, Any]:
    latitude, longitude = source_coordinate(record)
    name = source_name(record)
    address = source_address(record)
    camis = source_camis(record)
    return {
        "record_index": index,
        "row_id_digest": digest_value(source_row_id(record)) if source_row_id(record) else None,
        "name": name,
        "address": address,
        "normalized_name": normalize_name(name),
        "normalized_address": normalize_address(address),
        "camis": camis,
        "latitude": latitude,
        "longitude": longitude,
        "status_present": bool(source_status(record)),
        "status_digest": digest_value(source_status(record)) if source_status(record) else None,
    }


def field_uniform(rows: list[dict[str, Any]], field: str, normalizer) -> dict[str, Any]:
    """Describe duplicates without silently selecting a row.

    ``all_nonempty_same`` is required for a duplicate field to enter a
    comparison denominator. Missing values and differing values remain visible
    in the private case record.
    """

    values = [normalizer(row.get(field)) for row in rows]
    nonempty = [value for value in values if value not in (None, "")]
    distinct = sorted(set(nonempty))
    return {
        "rows": len(rows),
        "nonempty_values": len(nonempty),
        "distinct_values": len(distinct),
        "all_nonempty_same": bool(rows) and len(nonempty) == len(rows) and len(distinct) == 1,
        "conflict": len(distinct) > 1,
    }


def _normal_float(value: Any) -> str:
    number = parse_coordinate(value)
    return "" if number is None else f"{number:.8f}"


def build_current_index(records: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, Any]]:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    missing_key = 0
    for index, record in enumerate(records):
        compact = compact_source_row(record, index)
        key = (compact["normalized_name"], compact["normalized_address"])
        if not key[0] or not key[1]:
            missing_key += 1
            continue
        by_key[key].append(compact)
    duplicate_keys = sum(1 for rows in by_key.values() if len(rows) > 1)
    return by_key, {
        "rows_total": len(records),
        "rows_missing_name_or_address": missing_key,
        "unique_name_address_keys": sum(1 for rows in by_key.values() if len(rows) == 1),
        "ambiguous_name_address_keys": duplicate_keys,
    }


def build_historical_index(records: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    by_camis: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_camis = 0
    for index, record in enumerate(records):
        compact = compact_source_row(record, index)
        camis = compact["camis"]
        if not camis:
            missing_camis += 1
            continue
        by_camis[camis].append(compact)
    duplicate_groups = {camis: rows for camis, rows in by_camis.items() if len(rows) > 1}
    return by_camis, {
        "rows_total": len(records),
        "rows_missing_camis": missing_camis,
        "unique_camis": len(by_camis),
        "duplicate_camis_groups": len(duplicate_groups),
        "duplicate_extra_rows": sum(len(rows) - 1 for rows in duplicate_groups.values()),
    }


def _base_case(queue_row: dict[str, Any], mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "screening_id": queue_row.get("screening_id"),
        "candidate_id_digest": digest_value(queue_row.get("candidate_id") or ""),
        "candidate_name": queue_row.get("name"),
        "candidate_address": queue_row.get("address"),
        "candidate_normalized_name": normalize_name(queue_row.get("name")),
        "candidate_normalized_address": normalize_address(queue_row.get("address")),
        "candidate_latitude": queue_row.get("latitude"),
        "candidate_longitude": queue_row.get("longitude"),
        "candidate_platform": queue_row.get("platform"),
        "candidate_external_id": queue_row.get("external_id"),
    }


def compare_coordinates(
    case: dict[str, Any],
    dot_latitude: float | None,
    dot_longitude: float | None,
    *,
    zero_zero_missing: bool = False,
) -> None:
    candidate_latitude = case.get("candidate_latitude")
    candidate_longitude = case.get("candidate_longitude")
    candidate_zero_zero = candidate_latitude == 0 and candidate_longitude == 0
    dot_zero_zero = dot_latitude == 0 and dot_longitude == 0
    if (
        not valid_pair(candidate_latitude, candidate_longitude)
        or not valid_pair(dot_latitude, dot_longitude)
        or (zero_zero_missing and (candidate_zero_zero or dot_zero_zero))
    ):
        case["coordinate_comparable"] = False
        case["coordinate_distance_m"] = None
        case["coordinate_within_10m"] = None
        case["coordinate_within_50m"] = None
        return
    distance = haversine_meters(
        candidate_latitude,
        candidate_longitude,
        dot_latitude,
        dot_longitude,
    )
    case["coordinate_comparable"] = True
    case["coordinate_distance_m"] = round(distance, 3)
    case["coordinate_within_10m"] = distance <= 10
    case["coordinate_within_50m"] = distance <= 50


def compare_current_case(
    queue_row: dict[str, Any],
    by_key: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    case = _base_case(queue_row, "current")
    key = (case["candidate_normalized_name"], case["candidate_normalized_address"])
    case["match_key_available"] = bool(key[0] and key[1])
    matches = by_key.get(key, []) if case["match_key_available"] else []
    case["match_count"] = len(matches)
    if not case["match_key_available"]:
        case["match_type"] = "candidate_missing_name_or_address"
        case["dot_row"] = None
        return case
    if not matches:
        case["match_type"] = "no_unique_name_address_match"
        case["dot_row"] = None
        return case
    if len(matches) != 1:
        case["match_type"] = "ambiguous_name_address_match"
        case["dot_row"] = None
        return case
    dot_row = matches[0]
    case["match_type"] = "unique_name_address_match"
    case["dot_row"] = dot_row
    # The NYC queue uses (0, 0) as a missing-coordinate sentinel. Keep the
    # historical comparator's broader legacy denominator unchanged, while
    # treating this sentinel as unavailable for the current DOT diagnostic.
    compare_coordinates(
        case,
        dot_row.get("latitude"),
        dot_row.get("longitude"),
        zero_zero_missing=True,
    )
    case["dot_status_present"] = bool(dot_row.get("status_present"))
    return case


def compare_historical_case(
    queue_row: dict[str, Any],
    by_camis: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    case = _base_case(queue_row, "historical")
    camis = queue_row.get("external_id") if queue_row.get("platform") == EXPECTED_PLATFORM else ""
    case["join_camis"] = camis
    case["camis_join_eligible"] = bool(camis)
    matches = by_camis.get(camis, []) if camis else []
    case["match_count"] = len(matches)
    if not camis:
        case["match_type"] = "queue_row_not_dohmh_camis"
        case["dot_rows"] = []
        return case
    if not matches:
        case["match_type"] = "camis_not_found"
        case["dot_rows"] = []
        return case

    case["match_type"] = "single_camis_row" if len(matches) == 1 else "duplicate_camis_rows"
    case["dot_rows"] = matches
    case["duplicate_fields"] = {
        "name": field_uniform(matches, "normalized_name", lambda value: str(value or "")),
        "address": field_uniform(matches, "normalized_address", lambda value: str(value or "")),
        "latitude": field_uniform(matches, "latitude", _normal_float),
        "longitude": field_uniform(matches, "longitude", _normal_float),
        "status": field_uniform(matches, "status_digest", lambda value: str(value or "")),
    }
    case["duplicate_consistency"] = (
        "single_row"
        if len(matches) == 1
        else (
            "uniform_selected_fields"
            if all(item["all_nonempty_same"] for item in case["duplicate_fields"].values())
            else "field_missing_or_conflict"
        )
    )

    # A field enters its own denominator only when every duplicate row has a
    # non-empty, identical value. This preserves conflicts and missing values.
    address_info = case["duplicate_fields"]["address"]
    if address_info["all_nonempty_same"]:
        dot_address = matches[0].get("normalized_address")
        candidate_address = case["candidate_normalized_address"]
        case["address_comparable"] = bool(candidate_address and dot_address)
        case["address_equal"] = (
            candidate_address == dot_address if case["address_comparable"] else None
        )
    else:
        case["address_comparable"] = False
        case["address_equal"] = None

    latitude_info = case["duplicate_fields"]["latitude"]
    longitude_info = case["duplicate_fields"]["longitude"]
    if latitude_info["all_nonempty_same"] and longitude_info["all_nonempty_same"]:
        compare_coordinates(case, matches[0].get("latitude"), matches[0].get("longitude"))
    else:
        case["coordinate_comparable"] = False
        case["coordinate_distance_m"] = None
        case["coordinate_within_10m"] = None
        case["coordinate_within_50m"] = None
    return case


def rate(count: int, denominator: int) -> dict[str, Any]:
    return {
        "count": count,
        "denominator": denominator,
        "rate": round(count / denominator, 6) if denominator else None,
    }


def current_summary(
    queue_rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
    by_key: dict[tuple[str, str], list[dict[str, Any]]],
    index_stats: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    nyc_rows = [row for row in queue_rows if normalize_text(row.get("city")) == "new york"]
    cases = [compare_current_case(row, by_key) for row in nyc_rows]
    eligible = [case for case in cases if case["match_key_available"]]
    unique = [case for case in cases if case["match_type"] == "unique_name_address_match"]
    any_match = [
        case
        for case in cases
        if case["match_type"]
        in {"unique_name_address_match", "ambiguous_name_address_match"}
    ]
    coordinate = [case for case in unique if case.get("coordinate_comparable")]
    status_present = [case for case in unique if case.get("dot_status_present")]
    counts = Counter(case["match_type"] for case in cases)
    summary = {
        "snapshot_rows": len(records),
        "snapshot_filter": {
            "candidate_scope": "queue city normalized to New York",
            "dot_rows_used": "all rows with non-empty name and full address",
            "dot_rows_without_key": index_stats["rows_missing_name_or_address"],
            "status_filter": "none",
            "match_key": "exact normalized name + exact normalized full address; unique DOT row required",
        },
        "queue_scope": {
            "nyc_rows": len(nyc_rows),
            "name_address_key_eligible": len(eligible),
        },
        "match_counts": dict(sorted(counts.items())),
        "coverage": rate(len(unique), len(eligible)),
        "any_name_address_match": rate(len(any_match), len(eligible)),
        "metrics_after_unique_match": {
            "coordinate_comparable_coverage": rate(len(coordinate), len(unique)),
            "coordinate_within_10m": rate(
                sum(bool(case.get("coordinate_within_10m")) for case in coordinate),
                len(coordinate),
            ),
            "coordinate_within_50m": rate(
                sum(bool(case.get("coordinate_within_50m")) for case in coordinate),
                len(coordinate),
            ),
            "dot_status_field_present": rate(len(status_present), len(unique)),
        },
        "denominator_note": (
            "Unique name+address is a conservative retrieval/coverage condition, not a correctness label. "
            "Coordinate comparable coverage is reported over unique matches; coordinate threshold rates use only "
            "the comparable subset. Status results are field-level consistency diagnostics among unique matches."
        ),
        "index": index_stats,
    }
    return summary, cases


def historical_summary(
    queue_rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
    by_camis: dict[str, list[dict[str, Any]]],
    index_stats: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    nyc_rows = [row for row in queue_rows if normalize_text(row.get("city")) == "new york"]
    eligible = [
        row
        for row in nyc_rows
        if row.get("platform") == EXPECTED_PLATFORM and re.fullmatch(r"\d+", row.get("external_id", ""))
    ]
    cases = [compare_historical_case(row, by_camis) for row in nyc_rows]
    eligible_cases = [case for case in cases if case["camis_join_eligible"]]
    matched = [case for case in eligible_cases if case["match_count"] > 0]
    address_cases = [case for case in matched if case.get("address_comparable")]
    coordinate_cases = [case for case in matched if case.get("coordinate_comparable")]
    counts = Counter(case["match_type"] for case in cases)
    summary = {
        "snapshot_rows": len(records),
        "snapshot_filter": {
            "candidate_scope": "queue city normalized to New York",
            "join_scope": "platform=nyc-open-data-restaurants and numeric source.external_id",
            "dot_rows_used": "all rows with non-empty food_service_establishment/CAMIS",
            "duplicate_policy": "retain all rows; a field enters comparison denominator only when every duplicate row has a non-empty identical value",
        },
        "queue_scope": {
            "nyc_rows": len(nyc_rows),
            "camis_join_eligible": len(eligible),
        },
        "match_counts": dict(sorted(counts.items())),
        "coverage": rate(len(matched), len(eligible_cases)),
        "duplicate_diagnostics": {
            "matched_cases_with_duplicate_rows": sum(1 for case in matched if case["match_count"] > 1),
            "uniform_selected_fields": sum(
                case.get("duplicate_consistency") == "uniform_selected_fields" for case in matched
            ),
            "field_missing_or_conflict": sum(
                case.get("duplicate_consistency") == "field_missing_or_conflict" for case in matched
            ),
        },
        "metrics_after_camis_match": {
            "address_equal": rate(
                sum(bool(case.get("address_equal")) for case in address_cases),
                len(address_cases),
            ),
            "coordinate_within_10m": rate(
                sum(bool(case.get("coordinate_within_10m")) for case in coordinate_cases),
                len(coordinate_cases),
            ),
            "coordinate_within_50m": rate(
                sum(bool(case.get("coordinate_within_50m")) for case in coordinate_cases),
                len(coordinate_cases),
            ),
        },
        "denominator_note": (
            "CAMIS join coverage is a cross-source linkage diagnostic. Address and coordinate rates exclude "
            "missing candidate fields and any duplicate CAMIS field conflict. Historical DOT data cannot prove current operation."
        ),
        "index": index_stats,
    }
    return summary, cases


def _ensure_new_file(path: Path, force: bool, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        os.chmod(path.parent, 0o700)
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite: {path}")
    if path.is_symlink():
        raise ValueError(f"refusing to write through symlink: {path}")


def write_private_jsonl(path: Path, rows: Iterable[dict[str, Any]], force: bool) -> str:
    _ensure_new_file(path, force, private=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")
    os.chmod(path, 0o600)
    return sha256_file(path)


def write_json(path: Path, value: Any, force: bool, private: bool = False) -> str:
    _ensure_new_file(path, force, private=private)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if private:
        os.chmod(path, 0o600)
    return sha256_file(path)


def build_public_markdown(summary: dict[str, Any]) -> str:
    current = summary["current_dot"]
    historical = summary["historical_dot"]
    lines = [
        "# NYC DOT 跨来源字段一致性检查",
        "",
        "这份离线报告把冻结筛查队列与两份已保存的 NYC DOT 快照做字段级比较。它不是独立人工 Gold，也不能直接写成地点核验准确率。当前快照没有 CAMIS，使用规范化名称＋完整地址作为唯一检索条件；历史快照按 `food_service_establishment`/CAMIS 连接，并保留重复申请的冲突诊断。",
        "",
        f"- 运行标识：`{summary['run_id']}`；脚本 SHA-256：`{summary['script_sha256']}`。",
        f"- 冻结队列 SHA-256：`{summary['queue']['sha256']}`；队列行数：`{summary['queue']['rows']}`。",
        f"- 当前 DOT 输入 SHA-256：`{summary['inputs']['current']['sha256']}`；原始行数：`{summary['inputs']['current']['records']}`。",
        f"- 历史 DOT 输入 SHA-256：`{summary['inputs']['historical']['sha256']}`；原始行数：`{summary['inputs']['historical']['records']}`。",
        f"- 原始抓取清单 SHA-256：`{summary['capture_manifest']['sha256']}`；脚本会校验清单中的数据集、行数与输入文件哈希一致。",
        "",
        "## 当前 DOT（无 CAMIS）",
        "",
        f"- 纽约队列行数：`{current['queue_scope']['nyc_rows']}`；具备名称＋地址键：`{current['queue_scope']['name_address_key_eligible']}`。",
        f"- 至少匹配一条 DOT 许可行：`{current['any_name_address_match']['count']}/{current['any_name_address_match']['denominator']}`（其中唯一匹配 `{current['coverage']['count']}`，多许可行 `{current['match_counts'].get('ambiguous_name_address_match', 0)}`）。",
        f"- 唯一规范化名称＋完整地址匹配覆盖：`{current['coverage']['count']}/{current['coverage']['denominator']}`（`{current['coverage']['rate']}`）。",
        f"- 唯一匹配后的坐标可比较覆盖：`{current['metrics_after_unique_match']['coordinate_comparable_coverage']['count']}/{current['metrics_after_unique_match']['coordinate_comparable_coverage']['denominator']}`；可比较者坐标 ≤10m：`{current['metrics_after_unique_match']['coordinate_within_10m']['count']}/{current['metrics_after_unique_match']['coordinate_within_10m']['denominator']}`；≤50m：`{current['metrics_after_unique_match']['coordinate_within_50m']['count']}/{current['metrics_after_unique_match']['coordinate_within_50m']['denominator']}`。",
        f"- DOT 状态字段存在：`{current['metrics_after_unique_match']['dot_status_field_present']['count']}/{current['metrics_after_unique_match']['dot_status_field_present']['denominator']}`。",
        "- 解释：名称＋地址匹配条件本身只是覆盖/检索门槛，不能当作名称或地点准确率；其后的坐标和状态只做字段级一致性诊断。",
        "",
        "## 历史 DOT（CAMIS）",
        "",
        f"- 纽约队列中可用 DOHMH CAMIS 行数：`{historical['queue_scope']['camis_join_eligible']}`。",
        f"- CAMIS 连接覆盖：`{historical['coverage']['count']}/{historical['coverage']['denominator']}`（`{historical['coverage']['rate']}`）。",
        f"- 地址相同：`{historical['metrics_after_camis_match']['address_equal']['count']}/{historical['metrics_after_camis_match']['address_equal']['denominator']}`；坐标 ≤10m：`{historical['metrics_after_camis_match']['coordinate_within_10m']['count']}/{historical['metrics_after_camis_match']['coordinate_within_10m']['denominator']}`；≤50m：`{historical['metrics_after_camis_match']['coordinate_within_50m']['count']}/{historical['metrics_after_camis_match']['coordinate_within_50m']['denominator']}`。",
        f"- 重复 CAMIS：匹配案例中重复行 `{historical['duplicate_diagnostics']['matched_cases_with_duplicate_rows']}`，其中统一字段 `{historical['duplicate_diagnostics']['uniform_selected_fields']}`，存在字段缺失或冲突 `{historical['duplicate_diagnostics']['field_missing_or_conflict']}`。",
        "- 解释：历史快照只能支持历史字段的连接与一致性诊断，不能证明地点当前营业；重复申请没有被静默选一行。",
        "",
        "逐案结果和私有输入清单位于 `.local/location-verification-live-check/dot_cross_source_results/`，公开 JSON 仅保留汇总、分母、过滤规则和哈希。",
    ]
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    queue_rows, queue_info = load_queue(args.queue)
    current_records, current_info = load_snapshot(args.current_input, "current")
    historical_records, historical_info = load_snapshot(args.historical_input, "historical")
    capture_manifest = load_capture_manifest(
        args.capture_manifest, current_info, historical_info
    )

    current_index, current_index_info = build_current_index(current_records)
    historical_index, historical_index_info = build_historical_index(historical_records)
    current_summary_value, current_cases = current_summary(
        queue_rows, current_records, current_index, current_index_info
    )
    historical_summary_value, historical_cases = historical_summary(
        queue_rows, historical_records, historical_index, historical_index_info
    )

    private_cases = current_cases + historical_cases
    private_hash = write_private_jsonl(args.private_output, private_cases, args.force)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "script_sha256": sha256_file(Path(__file__)),
        "queue": queue_info,
        "inputs": {"current": current_info, "historical": historical_info},
        "capture_manifest": capture_manifest,
        "current_dot": current_summary_value,
        "historical_dot": historical_summary_value,
        "private_cases": {
            "path": str(args.private_output),
            "rows": len(private_cases),
            "sha256": private_hash,
        },
        "manifest": {"path": str(args.manifest_output)},
        "claim_boundary": (
            "Current DOT unique name+address is retrieval coverage only; field metrics are cross-source consistency diagnostics. "
            "Historical DOT CAMIS comparisons are historical and cannot prove current operation. Neither is an independent Gold or formal Agent accuracy."
        ),
    }
    public_json_hash = write_json(args.public_json, summary, args.force)
    _ensure_new_file(args.public_markdown, args.force)
    args.public_markdown.write_text(build_public_markdown(summary), encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "script_sha256": summary["script_sha256"],
        "queue": queue_info,
        "inputs": {"current": current_info, "historical": historical_info},
        "capture_manifest": capture_manifest,
        "filters": {
            "current": current_summary_value["snapshot_filter"],
            "historical": historical_summary_value["snapshot_filter"],
        },
        "private_cases": summary["private_cases"],
        "public_json": {"path": str(args.public_json), "sha256": public_json_hash},
        "public_markdown": {"path": str(args.public_markdown), "sha256": sha256_file(args.public_markdown)},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    manifest_hash = write_json(args.manifest_output, manifest, args.force, private=True)
    summary["manifest"] = {"path": str(args.manifest_output), "sha256": manifest_hash}
    # The public JSON is intentionally written before the manifest so its hash
    # is stable and the manifest can remain the final private audit record.
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--current-input", type=Path, default=DEFAULT_CURRENT)
    parser.add_argument("--historical-input", type=Path, default=DEFAULT_HISTORICAL)
    parser.add_argument("--capture-manifest", type=Path, default=DEFAULT_CAPTURE_MANIFEST)
    parser.add_argument("--private-output", type=Path, default=DEFAULT_PRIVATE_CASES)
    parser.add_argument("--manifest-output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--public-json", type=Path, default=DEFAULT_PUBLIC_JSON)
    parser.add_argument("--public-markdown", type=Path, default=DEFAULT_PUBLIC_MD)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
