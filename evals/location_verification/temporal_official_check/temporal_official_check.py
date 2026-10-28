#!/usr/bin/env python3
"""Compare a frozen NYC queue with captured DOHMH API responses.

This is an offline, same-source temporal-consistency check.  It never calls
the Socrata API, a map service, or an LLM.  The frozen queue is one snapshot
and ``official_batches`` is a later snapshot of the same 43nn-pn8j source.
The output must therefore not be described as an independent Gold set or as
real-world location-verification accuracy.

The private JSONL contains case-level names, addresses and coordinates.  The
public JSON/Markdown output contains only counts, hashes, and field-level
denominators.  A CAMIS appearing more than once in the captured responses is
treated as an unresolved duplicate conflict; this script never picks one
duplicate row merely to make a comparison possible.
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
import tempfile
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
DEFAULT_OFFICIAL_DIR = ROOT / ".local" / "location-verification-live-check" / "official_batches"
DEFAULT_QUERIES_DIR = ROOT / ".local" / "location-verification-live-check" / "official_queries"
DEFAULT_PRIVATE_DIR = ROOT / ".local" / "location-verification-live-check" / "temporal_results"
DEFAULT_MANIFEST = ROOT / ".local" / "location-verification-live-check" / "official_snapshot_manifest.json"
PUBLIC_DIR = Path(__file__).resolve().parent

EXPECTED_PLATFORM = "nyc-open-data-restaurants"
DATASET_ID = "43nn-pn8j"
API_URL = "https://data.cityofnewyork.us/resource/43nn-pn8j.json"
SELECT_FIELDS = [
    "camis",
    "dba",
    "boro",
    "building",
    "street",
    "zipcode",
    "latitude",
    "longitude",
    "record_date",
]
SCHEMA_VERSION = "lv-temporal-official-check-v1"
MANIFEST_SCHEMA_VERSION = "lv-official-snapshot-manifest-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(value: Any) -> str:
    return normalize_text(value)


def normalize_address(value: Any) -> str:
    # Keep the full street/building token sequence.  Only common postal
    # abbreviations and ordinal suffixes are canonicalized; no geocoder or
    # fuzzy aliasing is applied, so a real mismatch remains inspectable.
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
    tokens = []
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


def get_field(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record:
            return record[name]
    lowered = {str(key).casefold(): value for key, value in record.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return None


def official_address(record: dict[str, Any]) -> str:
    building = str(get_field(record, "building") or "").strip()
    street = str(get_field(record, "street") or "").strip()
    if building and street:
        return "{} {}".format(building, street)
    if street:
        return street
    if building:
        return building
    return str(get_field(record, "address") or "").strip()


def selected_official_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "camis": canonical_id(get_field(record, "camis", "CAMIS")),
        "dba": get_field(record, "dba", "DBA"),
        "boro": get_field(record, "boro", "BORO"),
        "building": get_field(record, "building", "BUILDING"),
        "street": get_field(record, "street", "STREET"),
        "zipcode": get_field(record, "zipcode", "ZIPCODE"),
        "latitude": get_field(record, "latitude", "LATITUDE"),
        "longitude": get_field(record, "longitude", "LONGITUDE"),
        "record_date": get_field(record, "record_date", "RECORD_DATE"),
    }


def parse_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def extract_records(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Accept a raw JSON array or a small capture envelope.

    The current captures are arrays.  Supporting common envelope keys keeps
    the checker independent of whether a later capture tool adds metadata.
    """

    metadata: dict[str, Any] = {}
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("schema_version", "dataset_id", "platform", "retrieved_at", "captured_at", "as_of", "endpoint"):
            if key in payload:
                metadata[key] = payload[key]
        records = None
        for key in ("records", "results", "rows", "items", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                records = candidate
                break
            if isinstance(candidate, dict):
                nested, nested_meta = extract_records(candidate)
                if nested:
                    metadata.update(nested_meta)
                    records = nested
                    break
        if records is None and get_field(payload, "camis", "CAMIS") is not None:
            records = [payload]
        if records is None:
            raise ValueError("JSON object has no records/results/rows/items/data array")
    else:
        raise ValueError("top-level JSON must be an array or object")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError("record array contains a non-object value")
    return [dict(record) for record in records], metadata


def load_queue(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = sha256_file(path)
    rows: list[dict[str, Any]] = []
    skipped_platform = 0
    missing_camis = 0
    duplicate_camis: list[str] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            source = row.get("source") or {}
            if source.get("platform") != EXPECTED_PLATFORM:
                skipped_platform += 1
                continue
            camis = canonical_id(source.get("external_id"))
            if not camis:
                missing_camis += 1
                continue
            if camis in seen:
                duplicate_camis.append(camis)
            seen.add(camis)
            candidate = row.get("candidate") or {}
            rows.append(
                {
                    "line_number": line_number,
                    "screening_id": row.get("screening_id"),
                    "camis": camis,
                    "name": candidate.get("name"),
                    "address": candidate.get("address"),
                    "latitude": parse_coordinate(candidate.get("latitude")),
                    "longitude": parse_coordinate(candidate.get("longitude")),
                    "source_collected_at": source.get("collected_at"),
                    "source_url": source.get("source_url"),
                    "private_snapshot": row.get("private_snapshot"),
                }
            )
    if duplicate_camis:
        raise ValueError(
            "frozen queue contains duplicate CAMIS values: {}".format(
                ",".join(sorted(set(duplicate_camis))[:5])
            )
        )
    collected_at = sorted(
        str(row["source_collected_at"])
        for row in rows
        if row.get("source_collected_at")
    )
    return rows, {
        "path": path.name,
        "sha256": digest,
        "bytes": path.stat().st_size,
        "rows": len(rows),
        "unique_camis": len(seen),
        "skipped_platform_rows": skipped_platform,
        "missing_camis_rows": missing_camis,
        "source_collected_at_min": collected_at[0] if collected_at else None,
        "source_collected_at_max": collected_at[-1] if collected_at else None,
    }


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_query_camis(path: Path) -> list[str]:
    """Return every quoted CAMIS token, preserving duplicates and order.

    The query captures are generated as ``camis in ("...")`` clauses.  A
    list, rather than a set, is intentional: duplicate IDs are an integrity
    error and must remain visible to the fail-closed validator.
    """

    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return [canonical_id(value) for value in re.findall(r"[\"'](\d+)[\"']", text)]


def _sample_ids(values: Iterable[str], limit: int = 5) -> str:
    ordered = sorted(set(values))
    suffix = "..." if len(ordered) > limit else ""
    return ",".join(ordered[:limit]) + suffix


def validate_query_files(
    files: list[Path],
    queries_dir: Path,
    expected_camis: set[str] | None,
) -> dict[str, dict[str, Any]]:
    """Validate the query partition before accepting any API response.

    The checker must know which IDs each response was requested for.  It
    therefore fails closed for a missing/orphan query file, duplicate IDs
    within or across query files, and a query union that differs from the
    frozen queue.  The returned mapping retains both the ordered IDs and the
    set used for per-batch response checks.
    """

    if not queries_dir.is_dir():
        raise FileNotFoundError(queries_dir)
    expected_files = {path.stem + ".where" for path in files}
    query_files = {
        path.name
        for path in queries_dir.glob("*.where")
        if path.is_file()
    }
    missing_files = sorted(expected_files - query_files)
    orphan_files = sorted(query_files - expected_files)
    if missing_files:
        raise ValueError(
            "missing query files for official batches: {}".format(
                ",".join(missing_files)
            )
        )
    if orphan_files:
        raise ValueError(
            "orphan query files without official batches: {}".format(
                ",".join(orphan_files)
            )
        )

    details: dict[str, dict[str, Any]] = {}
    occurrences: defaultdict[str, list[str]] = defaultdict(list)
    for path in files:
        query_path = queries_dir / (path.stem + ".where")
        ids = parse_query_camis(query_path)
        if not ids:
            raise ValueError(
                "query file contains no CAMIS IDs: {}".format(query_path.name)
            )
        duplicate_ids = sorted(
            camis for camis, count in Counter(ids).items() if count > 1
        )
        if duplicate_ids:
            raise ValueError(
                "query file contains duplicate CAMIS IDs: {} ({})".format(
                    query_path.name, _sample_ids(duplicate_ids)
                )
            )
        id_set = set(ids)
        for camis in ids:
            occurrences[camis].append(query_path.name)
        details[path.name] = {
            "path": query_path,
            "ids": ids,
            "id_set": id_set,
            "sha256": sha256_file(query_path),
            "bytes": query_path.stat().st_size,
        }

    duplicate_batches = {
        camis: batch_files
        for camis, batch_files in occurrences.items()
        if len(batch_files) > 1
    }
    if duplicate_batches:
        examples = [
            "{}=[{}]".format(camis, "/".join(batch_files))
            for camis, batch_files in sorted(duplicate_batches.items())[:5]
        ]
        raise ValueError(
            "CAMIS IDs are duplicated across query files: {}".format(
                ";".join(examples)
            )
        )

    query_union = set(occurrences)
    if expected_camis is not None:
        missing_ids = sorted(expected_camis - query_union)
        extra_ids = sorted(query_union - expected_camis)
        if missing_ids or extra_ids:
            fragments = []
            if missing_ids:
                fragments.append(
                    "missing_from_queries={} ({})".format(
                        len(missing_ids), _sample_ids(missing_ids)
                    )
                )
            if extra_ids:
                fragments.append(
                    "extra_in_queries={} ({})".format(
                        len(extra_ids), _sample_ids(extra_ids)
                    )
                )
            raise ValueError(
                "query CAMIS union does not equal frozen queue: {}".format(
                    "; ".join(fragments)
                )
            )
    return details


def load_official_batches(
    official_dir: Path,
    queries_dir: Path,
    expected_camis: set[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    if not official_dir.is_dir():
        raise FileNotFoundError(official_dir)
    files = sorted(path for path in official_dir.glob("*.json") if path.is_file())
    if not files:
        raise ValueError("no .json official batch files under {}".format(official_dir))
    query_details = validate_query_files(files, queries_dir, expected_camis)
    by_camis: dict[str, list[dict[str, Any]]] = defaultdict(list)
    batch_manifest: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    total_records = 0
    all_record_dates: list[str] = []
    for path in files:
        batch_sha = sha256_file(path)
        try:
            records, metadata = extract_records(parse_json_file(path))
        except Exception as exc:  # noqa: BLE001 - public capture validation should name the file
            parse_errors.append({"file": path.name, "error": str(exc)})
            continue
        ids_in_file: list[str] = []
        invalid_rows = 0
        invalid_row_indexes: list[int] = []
        dates: list[str] = []
        for index, record in enumerate(records):
            camis = canonical_id(get_field(record, "camis", "CAMIS"))
            if not camis:
                invalid_rows += 1
                invalid_row_indexes.append(index)
                continue
            query_ids = query_details[path.name]["id_set"]
            if camis not in query_ids:
                raise ValueError(
                    "official response CAMIS is outside its batch query: "
                    "{} -> {} (row {})".format(path.name, camis, index)
                )
            selected = selected_official_fields(record)
            record_date = str(selected.get("record_date") or "")
            if record_date:
                dates.append(record_date)
                all_record_dates.append(record_date)
            ids_in_file.append(camis)
            by_camis[camis].append(
                {
                    "file": path.name,
                    "index": index,
                    "metadata": metadata,
                    "record": selected,
                }
            )
        if invalid_row_indexes:
            raise ValueError(
                "official response contains rows without CAMIS: {} ({})".format(
                    path.name, ",".join(str(index) for index in invalid_row_indexes[:5])
                )
            )
        total_records += len(records)
        query_detail = query_details[path.name]
        query_info: dict[str, Any] = {
            "query_file": query_detail["path"].name,
            "query_sha256": query_detail["sha256"],
            "query_bytes": query_detail["bytes"],
            "query_camis": len(query_detail["ids"]),
            "query_unique_camis": len(query_detail["id_set"]),
        }
        batch_manifest.append(
            {
                "batch_file": path.name,
                "batch_sha256": batch_sha,
                "result_sha256": batch_sha,
                "bytes": path.stat().st_size,
                "local_mtime": iso_mtime(path),
                "record_count": len(records),
                "valid_camis": len(ids_in_file),
                "unique_camis": len(set(ids_in_file)),
                "duplicate_camis": len(ids_in_file) - len(set(ids_in_file)),
                "invalid_rows": invalid_rows,
                "record_date_min": min(dates) if dates else None,
                "record_date_max": max(dates) if dates else None,
                **query_info,
                "capture_metadata": {
                    key: metadata[key]
                    for key in ("dataset_id", "platform", "retrieved_at", "captured_at", "as_of")
                    if key in metadata
                },
            }
        )
    if parse_errors:
        raise ValueError("official batch parse errors: {}".format(canonical_json(parse_errors)))
    duplicate_ids = sorted(camis for camis, rows in by_camis.items() if len(rows) > 1)
    field_variations = 0
    for camis in duplicate_ids:
        signatures = {
            canonical_json(
                {
                    key: rows["record"].get(key)
                    for key in ("dba", "building", "street", "latitude", "longitude")
                }
            )
            for rows in by_camis[camis]
        }
        if len(signatures) > 1:
            field_variations += 1
    stats = {
        "directory": official_dir.name,
        "files": len(files),
        "total_records": total_records,
        "unique_camis": len(by_camis),
        "duplicate_camis": len(duplicate_ids),
        # Every repeated CAMIS remains unresolved, even when the selected
        # fields happen to agree and only record_date differs.  The separate
        # field_variations count is diagnostic and never licenses row choice.
        "duplicate_conflicts": len(duplicate_ids),
        "duplicate_field_variations": field_variations,
        "query_files": len(query_details),
        "query_id_count": sum(len(item["ids"]) for item in query_details.values()),
        "query_unique_id_count": len({camis for item in query_details.values() for camis in item["id_set"]}),
        "query_union_matches_expected": expected_camis is None
        or {camis for item in query_details.values() for camis in item["id_set"]}
        == expected_camis,
        "response_camis_within_batch_queries": True,
        "record_date_min": min(all_record_dates) if all_record_dates else None,
        "record_date_max": max(all_record_dates) if all_record_dates else None,
        "batch_manifest": batch_manifest,
    }
    return by_camis, batch_manifest, stats


def rate(count: int, denominator: int) -> dict[str, Any]:
    return {
        "count": count,
        "denominator": denominator,
        "percent": round(count * 100.0 / denominator, 3) if denominator else None,
    }


def compare_case(queue_row: dict[str, Any], official_rows: list[dict[str, Any]]) -> dict[str, Any]:
    base: dict[str, Any] = {
        "screening_id": queue_row.get("screening_id"),
        "camis": queue_row["camis"],
        "frozen": {
            "name": queue_row.get("name"),
            "address": queue_row.get("address"),
            "latitude": queue_row.get("latitude"),
            "longitude": queue_row.get("longitude"),
            "source_collected_at": queue_row.get("source_collected_at"),
            "source_url": queue_row.get("source_url"),
            "private_snapshot": queue_row.get("private_snapshot"),
        },
        "comparison_basis": {
            "platform": EXPECTED_PLATFORM,
            "dataset_id": DATASET_ID,
            "same_source_temporal_check": True,
        },
    }
    if not official_rows:
        base.update(
            {
                "status": "missing_in_latest_official_snapshot",
                "interpretation": "not_present_in_captured_batch; this is not a closure determination",
                "official_rows": [],
            }
        )
        return base
    if len(official_rows) > 1:
        base.update(
            {
                "status": "official_duplicate_conflict",
                "interpretation": "multiple_rows_for_same_camis; no row was selected for comparison",
                "official_rows": [row["record"] for row in official_rows],
                "official_batch_files": [row["file"] for row in official_rows],
            }
        )
        return base
    official = official_rows[0]["record"]
    queue_name = normalize_name(queue_row.get("name"))
    official_name = normalize_name(official.get("dba"))
    queue_address = normalize_address(queue_row.get("address"))
    official_address_value = official_address(official)
    official_address_normalized = normalize_address(official_address_value)
    queue_lat = queue_row.get("latitude")
    queue_lon = queue_row.get("longitude")
    official_lat = parse_coordinate(official.get("latitude"))
    official_lon = parse_coordinate(official.get("longitude"))
    name_status = "match" if queue_name and official_name and queue_name == official_name else (
        "missing" if not queue_name or not official_name else "mismatch"
    )
    address_status = (
        "match"
        if queue_address and official_address_normalized and queue_address == official_address_normalized
        else "missing"
        if not queue_address or not official_address_normalized
        else "mismatch"
    )
    if not valid_pair(queue_lat, queue_lon):
        coordinate_status = "queue_missing_or_invalid"
        distance_m = None
    elif not valid_pair(official_lat, official_lon):
        coordinate_status = "official_missing_or_invalid"
        distance_m = None
    else:
        distance_m = haversine_meters(queue_lat, queue_lon, official_lat, official_lon)
        if distance_m <= 10:
            coordinate_status = "within_10m"
        elif distance_m <= 50:
            coordinate_status = "within_50m"
        else:
            coordinate_status = "over_50m"
    material_mismatch = name_status == "mismatch" or address_status == "mismatch" or coordinate_status == "over_50m"
    comparable_all = (
        name_status == "match"
        and address_status == "match"
        and coordinate_status in {"within_10m", "within_50m"}
    )
    status = "field_mismatch" if material_mismatch else (
        "consistent_with_snapshot" if comparable_all else "consistent_without_coordinate_comparison"
    )
    base.update(
        {
            "status": status,
            "interpretation": "same-source temporal field comparison; not independent Gold",
            "official_batch_files": [official_rows[0]["file"]],
            "official": official,
            "fields": {
                "name": {
                    "queue_normalized": queue_name,
                    "official_normalized": official_name,
                    "status": name_status,
                },
                "address": {
                    "queue_normalized": queue_address,
                    "official_raw_joined": official_address_value,
                    "official_normalized": official_address_normalized,
                    "status": address_status,
                },
                "coordinates": {
                    "queue": [queue_lat, queue_lon],
                    "official": [official_lat, official_lon],
                    "distance_m": round(distance_m, 3) if distance_m is not None else None,
                    "status": coordinate_status,
                    "within_10m": coordinate_status == "within_10m",
                    "within_50m": coordinate_status in {"within_10m", "within_50m"},
                },
            },
        }
    )
    return base


def write_private_jsonl(path: Path, rows: Iterable[dict[str, Any]], force: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    if path.exists() and not force:
        raise FileExistsError("refusing to overwrite private output: {}".format(path))
    if path.is_symlink():
        raise ValueError("refusing to write through symlink: {}".format(path))
    fd, temp_name = tempfile.mkstemp(prefix=".temporal-cases-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(canonical_json(row) + "\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    os.chmod(path, 0o600)
    return sha256_file(path)


def write_json(path: Path, value: Any, force: bool, private: bool = False) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    if path.exists() and not force:
        raise FileExistsError("refusing to overwrite: {}".format(path))
    if path.is_symlink():
        raise ValueError("refusing to write through symlink: {}".format(path))
    payload = (canonical_json(value) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=".temporal-json-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(temp_name, 0o600 if private else 0o644)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    os.chmod(path, 0o600 if private else 0o644)
    return sha256_file(path)


def build_public_markdown(summary: dict[str, Any], manifest_hash: str, private_hash: str) -> str:
    coverage = summary["coverage"]
    metrics = summary["metrics"]
    lines = [
        "# NYC DOHMH same-source temporal consistency check",
        "",
        "这份报告比较冻结筛查队列与同一 NYC DOHMH 43nn-pn8j 数据源的后续 API 快照。它不是独立 Gold，也不是地点核验准确率；缺失记录不能直接解释为停业。重复 CAMIS 被保留为多行待逐案处理，不会任选一行；所选字段是否有分歧另行报告。",
        "",
        "## 输入与完整性",
        "",
        "- 冻结队列：`{}`，SHA-256 `{}`。".format(summary["queue"]["path"], summary["queue"]["sha256"]),
        "- 官方批次：`{}` 个文件，结果 SHA 记录在私有 manifest 中。".format(summary["official_input"]["files"]),
        "- 冻结队列来源采集时间范围：`{}` 至 `{}`；官方记录 `record_date` 范围：`{}` 至 `{}`。".format(
            summary["queue"]["source_collected_at_min"],
            summary["queue"]["source_collected_at_max"],
            summary["official_input"]["record_date_min"],
            summary["official_input"]["record_date_max"],
        ),
        "- 队列行数：`{}`；观察到的唯一 CAMIS：`{}`；缺失：`{}`；额外官方 CAMIS：`{}`。".format(
            summary["queue"]["rows"],
            coverage["observed_unique_camis"],
            coverage["missing_count"],
            coverage["extra_official_count"],
        ),
        "- 当前输入覆盖完整：`{}`；多行待逐案处理：`{}`（其中所选名称、地址、坐标有分歧：`{}`）。".format(
            coverage["complete"],
            summary["official_input"]["duplicate_camis"],
            summary["official_input"]["duplicate_field_variations"],
        ),
        "- 批次完整性：查询文件 `{} / {}`；查询 ID 并集匹配冻结队列：`{}`；响应 CAMIS 均属于对应批次请求：`{}`。".format(
            summary["integrity"]["query_files"],
            summary["official_input"]["files"],
            summary["integrity"]["query_union_matches_expected"],
            summary["integrity"]["response_camis_within_batch_queries"],
        ),
        "",
        "## 字段级结果",
        "",
        "| 指标 | 分子 / 分母 | 百分比 |",
        "| --- | ---: | ---: |",
        "| 官方表出现率 | {}/{} | {}% |".format(
            metrics["official_appearance"]["count"],
            metrics["official_appearance"]["denominator"],
            metrics["official_appearance"]["percent"],
        ),
        "| 身份名称规范化相同 | {}/{} | {}% |".format(
            metrics["name_match"]["count"], metrics["name_match"]["denominator"], metrics["name_match"]["percent"]
        ),
        "| 门牌+街道规范化相同 | {}/{} | {}% |".format(
            metrics["address_match"]["count"], metrics["address_match"]["denominator"], metrics["address_match"]["percent"]
        ),
        "| 坐标 ≤10m | {}/{} | {}% |".format(
            metrics["coordinate_within_10m"]["count"], metrics["coordinate_within_10m"]["denominator"], metrics["coordinate_within_10m"]["percent"]
        ),
        "| 坐标 ≤50m | {}/{} | {}% |".format(
            metrics["coordinate_within_50m"]["count"], metrics["coordinate_within_50m"]["denominator"], metrics["coordinate_within_50m"]["percent"]
        ),
        "| 名称、地址、坐标均可比较且一致 | {}/{} | {}% |".format(
            metrics["fully_comparable_consistent"]["count"],
            metrics["fully_comparable_consistent"]["denominator"],
            metrics["fully_comparable_consistent"]["percent"],
        ),
        "",
        "私有逐案结果 SHA-256：`{}`。私有 API 快照 manifest SHA-256：`{}`。".format(private_hash, manifest_hash),
        "",
        "这些数值只描述两个同源时间快照之间的字段一致性；不能证明地点真实存在、当前营业、跨来源正确或 Agent 的独立核验准确率。",
        "",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    queue_rows, queue_stats = load_queue(args.queue)
    queue_ids = {row["camis"] for row in queue_rows}
    official_by_camis, batch_manifest, official_stats = load_official_batches(
        args.official_dir,
        args.queries_dir,
        expected_camis=queue_ids,
    )
    observed_ids = set(official_by_camis)
    missing_ids = sorted(queue_ids - observed_ids)
    extra_ids = sorted(observed_ids - queue_ids)
    duplicate_ids = {camis for camis, rows in official_by_camis.items() if len(rows) > 1}
    if args.require_complete and missing_ids:
        raise ValueError("official input is incomplete: {} queue CAMIS are missing".format(len(missing_ids)))

    cases = [compare_case(row, official_by_camis.get(row["camis"], [])) for row in queue_rows]
    found_cases = [case for case in cases if case["status"] != "missing_in_latest_official_snapshot"]
    single_cases = [case for case in found_cases if case["status"] != "official_duplicate_conflict"]
    name_comparable = [case for case in single_cases if case["fields"]["name"]["status"] == "match"]
    name_denominator = [case for case in single_cases if case["fields"]["name"]["status"] in {"match", "mismatch"}]
    address_comparable = [case for case in single_cases if case["fields"]["address"]["status"] == "match"]
    address_denominator = [case for case in single_cases if case["fields"]["address"]["status"] in {"match", "mismatch"}]
    coordinate_cases = [
        case
        for case in single_cases
        if case["fields"]["coordinates"]["status"] in {"within_10m", "within_50m", "over_50m"}
    ]
    fully_comparable = [
        case
        for case in single_cases
        if case["fields"]["name"]["status"] in {"match", "mismatch"}
        and case["fields"]["address"]["status"] in {"match", "mismatch"}
        and case["fields"]["coordinates"]["status"] in {"within_10m", "within_50m", "over_50m"}
    ]
    metrics = {
        "official_appearance": rate(len(found_cases), len(queue_rows)),
        "name_match": rate(len(name_comparable), len(name_denominator)),
        "address_match": rate(len(address_comparable), len(address_denominator)),
        "coordinate_within_10m": rate(
            sum(case["fields"]["coordinates"]["status"] == "within_10m" for case in coordinate_cases),
            len(coordinate_cases),
        ),
        "coordinate_within_50m": rate(
            sum(case["fields"]["coordinates"]["status"] in {"within_10m", "within_50m"} for case in coordinate_cases),
            len(coordinate_cases),
        ),
        "fully_comparable_consistent": rate(
            sum(case["status"] == "consistent_with_snapshot" for case in fully_comparable),
            len(fully_comparable),
        ),
    }
    private_hash = write_private_jsonl(args.private_output, cases, args.force)
    captured_at = args.captured_at
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "captured_at": captured_at,
        "capture_time_note": "CLI-provided capture time; null means the API capture time was not recorded",
        "endpoint": {
            "url": args.endpoint_url,
            "dataset_id": DATASET_ID,
            "platform": EXPECTED_PLATFORM,
            "select_fields": SELECT_FIELDS,
            "where_files_are_local_only": True,
        },
        "queue": queue_stats,
        "batches": batch_manifest,
        "integrity": {
            "query_files": official_stats["query_files"],
            "query_id_count": official_stats["query_id_count"],
            "query_unique_id_count": official_stats["query_unique_id_count"],
            "query_union_matches_expected": official_stats["query_union_matches_expected"],
            "response_camis_within_batch_queries": official_stats["response_camis_within_batch_queries"],
        },
        "coverage": {
            "queue_unique_camis": len(queue_ids),
            "observed_unique_camis": len(observed_ids),
            "missing_count": len(missing_ids),
            "missing_camis_sha256": sha256_bytes(canonical_json(missing_ids).encode("utf-8")),
            "extra_official_count": len(extra_ids),
            "duplicate_camis": len(duplicate_ids),
            "duplicate_conflicts": official_stats["duplicate_conflicts"],
            "duplicate_field_variations": official_stats["duplicate_field_variations"],
            "record_date_min": official_stats["record_date_min"],
            "record_date_max": official_stats["record_date_max"],
            "complete": not missing_ids,
        },
        "result": {
            "private_cases_file": args.private_output.name,
            "private_cases_sha256": private_hash,
            "case_count": len(cases),
        },
        "claim_boundary": "same-source temporal consistency only; not independent Gold, not real-world location accuracy, and missing CAMIS is not a closure finding",
    }
    manifest_hash = write_json(args.manifest_output, manifest, args.force, private=True)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id,
        "generated_at": manifest["generated_at"],
        "captured_at": captured_at,
        "claim_boundary": manifest["claim_boundary"],
        "queue": queue_stats,
        "official_input": {
            "directory": official_stats["directory"],
            "files": official_stats["files"],
            "total_records": official_stats["total_records"],
            "unique_camis": official_stats["unique_camis"],
            "duplicate_camis": official_stats["duplicate_camis"],
            "duplicate_conflicts": official_stats["duplicate_conflicts"],
            "duplicate_field_variations": official_stats["duplicate_field_variations"],
            "record_date_min": official_stats["record_date_min"],
            "record_date_max": official_stats["record_date_max"],
            "batch_hashes": [
                {"file": item["batch_file"], "sha256": item["batch_sha256"], "query_sha256": item["query_sha256"]}
                for item in batch_manifest
            ],
        },
        "integrity": {
            "query_files": official_stats["query_files"],
            "query_id_count": official_stats["query_id_count"],
            "query_unique_id_count": official_stats["query_unique_id_count"],
            "query_union_matches_expected": official_stats["query_union_matches_expected"],
            "response_camis_within_batch_queries": official_stats["response_camis_within_batch_queries"],
        },
        "coverage": manifest["coverage"],
        "metrics": metrics,
        "case_status_counts": dict(sorted(Counter(case["status"] for case in cases).items())),
        "private_cases_sha256": private_hash,
        "private_manifest_sha256": manifest_hash,
        "denominator_note": "Field rates exclude missing official rows and CAMIS cases with multiple rows requiring case-level review; each denominator is shown explicitly.",
    }
    public_json_hash = write_json(args.public_json, summary, args.force)
    markdown = build_public_markdown(summary, manifest_hash, private_hash)
    if args.public_markdown.exists() and not args.force:
        raise FileExistsError("refusing to overwrite: {}".format(args.public_markdown))
    args.public_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.public_markdown.write_text(markdown, encoding="utf-8")
    os.chmod(args.public_markdown, 0o644)
    summary["public_json_sha256"] = public_json_hash
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--official-dir", type=Path, default=DEFAULT_OFFICIAL_DIR)
    parser.add_argument("--queries-dir", type=Path, default=DEFAULT_QUERIES_DIR)
    parser.add_argument("--private-output", type=Path, default=DEFAULT_PRIVATE_DIR / "temporal-cases.jsonl")
    parser.add_argument("--manifest-output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--public-json", type=Path, default=PUBLIC_DIR / "temporal-official-summary.json")
    parser.add_argument("--public-markdown", type=Path, default=PUBLIC_DIR / "temporal-official-summary.md")
    parser.add_argument("--endpoint-url", default=API_URL)
    parser.add_argument("--captured-at", default=None, help="capture time supplied by the API capture process")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except (FileNotFoundError, FileExistsError, ValueError, json.JSONDecodeError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
