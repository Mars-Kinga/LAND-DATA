#!/usr/bin/env python3
"""Normalize and replay the historical XHS verification reports.

This is a deterministic, offline audit of already reviewed records.  It does
not call the network, the database, an LLM, or any tool that could change the
repository state.  The resulting report is a provenance and schema check for
the historical reference set; it is deliberately not an accuracy evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SOURCE_FILENAMES = (
    "docs/xhs-verification-a.json",
    "docs/xhs-verification-b.json",
    "docs/xhs-verification-c.json",
)
FINAL_FILENAME = "docs/xhs-verification-final.json"
REQUIRED_RECORD_FIELDS = (
    "index",
    "name",
    "canonicalName",
    "address",
    "latitude",
    "longitude",
    "category",
    "status",
    "confidence",
    "primarySourceURL",
    "secondarySourceURL",
    "notes",
)
EXPECTED_STATUSES = ("CONFIRMED", "CONFLICT", "NOT_FOUND")
NORMALIZED_FIELDS = {
    "index": "index",
    "name": "name",
    "canonicalName": "canonical_name",
    "address": "address",
    "latitude": "latitude",
    "longitude": "longitude",
    "category": "category",
    "status": "status",
    "confidence": "confidence",
    "primarySourceURL": "primary_source_url",
    "secondarySourceURL": "secondary_source_url",
    "notes": "notes",
}


class ReplayValidationError(ValueError):
    """Raised when the historical inputs do not satisfy the expected schema."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplayValidationError(f"missing input file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReplayValidationError(f"invalid JSON in {path}: {exc}") from exc


def source_items(payload: Any, filename: str) -> list[dict[str, Any]]:
    if filename.endswith("-c.json"):
        if not isinstance(payload, list):
            raise ReplayValidationError(f"{filename}: expected a top-level array")
        return payload
    if not isinstance(payload, dict):
        raise ReplayValidationError(f"{filename}: expected a top-level object")
    if not isinstance(payload.get("items"), list):
        raise ReplayValidationError(f"{filename}: items must be an array")
    return payload["items"]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_record(record: Any, origin: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return [f"{origin}: record must be an object"]
    missing = [field for field in REQUIRED_RECORD_FIELDS if field not in record]
    if missing:
        errors.append(f"{origin}: missing fields: {', '.join(missing)}")
    if "index" in record and (
        not isinstance(record["index"], int) or isinstance(record["index"], bool)
    ):
        errors.append(f"{origin}: index must be an integer")
    for field in ("name", "canonicalName", "category", "status", "notes"):
        if field in record and not isinstance(record[field], str):
            errors.append(f"{origin}: {field} must be a string")
    for field in ("latitude", "longitude"):
        if field in record and record[field] is not None and not _is_number(record[field]):
            errors.append(f"{origin}: {field} must be numeric or null")
    if "confidence" in record and (
        not _is_number(record["confidence"])
        or not 0 <= float(record["confidence"]) <= 1
    ):
        errors.append(f"{origin}: confidence must be a number between 0 and 1")
    for field in ("primarySourceURL", "secondarySourceURL"):
        if field in record and record[field] is not None and not isinstance(record[field], str):
            errors.append(f"{origin}: {field} must be a string or null")
    if record.get("status") not in EXPECTED_STATUSES:
        errors.append(f"{origin}: unexpected status {record.get('status')!r}")
    return errors


def validate_records(records: Iterable[dict[str, Any]], origin: str) -> None:
    errors = []
    for position, record in enumerate(records):
        errors.extend(validate_record(record, f"{origin}[{position}]"))
    if errors:
        raise ReplayValidationError("\n".join(errors))


def normalize_key(value: str) -> str:
    """Stable, conservative key used only for grouping duplicate candidates."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_record(record: dict[str, Any], source_report: str) -> dict[str, Any]:
    normalized = {target: record.get(source) for source, target in NORMALIZED_FIELDS.items()}
    normalized["source_report"] = source_report
    normalized["dedupe_key"] = normalize_key(record["canonicalName"])
    return normalized


def group_records(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[normalize_key(record["canonicalName"])].append(record)
    return dict(sorted(groups.items()))


def missing_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        for field in REQUIRED_RECORD_FIELDS:
            value = record.get(field)
            if value is None or value == "":
                counts[field] += 1
    return {field: counts.get(field, 0) for field in REQUIRED_RECORD_FIELDS}


def source_coverage(records: list[dict[str, Any]]) -> dict[str, int]:
    primary_missing = sum(not record.get("primarySourceURL") for record in records)
    secondary_missing = sum(not record.get("secondarySourceURL") for record in records)
    either_missing = sum(
        not record.get("primarySourceURL") or not record.get("secondarySourceURL")
        for record in records
    )
    both_present = len(records) - either_missing
    return {
        "records": len(records),
        "primary_missing": primary_missing,
        "secondary_missing": secondary_missing,
        "either_missing": either_missing,
        "both_present": both_present,
    }


def by_status(records: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(record["status"] for record in records)
    return {status: counter.get(status, 0) for status in EXPECTED_STATUSES}


def by_status_missing(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["status"]].append(record)
    return {
        status: missing_counts(grouped.get(status, []))
        for status in EXPECTED_STATUSES
    }


def compare_record_fields(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in REQUIRED_RECORD_FIELDS)


def replay_checks(
    source_records: list[dict[str, Any]], final_payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    final_raw = final_payload.get("rawResults")
    final_unique = final_payload.get("uniqueEntities")
    if not isinstance(final_raw, list):
        failures.append("final.rawResults is not an array")
        final_raw = []
    if not isinstance(final_unique, list):
        failures.append("final.uniqueEntities is not an array")
        final_unique = []

    source_sorted = sorted(source_records, key=lambda record: record["index"])
    final_sorted = sorted(final_raw, key=lambda record: record.get("index", -1))
    if len(source_sorted) != len(final_sorted):
        failures.append(
            f"source/raw length mismatch: {len(source_sorted)} != {len(final_sorted)}"
        )
    else:
        for position, (source_record, final_record) in enumerate(
            zip(source_sorted, final_sorted)
        ):
            if not compare_record_fields(source_record, final_record):
                failures.append(
                    f"source/raw record mismatch at position {position} "
                    f"(index={source_record.get('index')})"
                )
                break

    groups = group_records(source_records)
    expected_candidates = sorted(
        sorted(record["index"] for record in records) for records in groups.values()
    )
    actual_candidates = sorted(
        sorted(entity.get("candidateIndexes", [])) for entity in final_unique
    )
    if expected_candidates != actual_candidates:
        failures.append("source dedupe groups do not match final.uniqueEntities")

    raw_status = by_status(source_records)
    summary = final_payload.get("summary")
    expected_summary = {
        "confirmed": raw_status["CONFIRMED"],
        "conflict": raw_status["CONFLICT"],
        "not_found": raw_status["NOT_FOUND"],
    }
    if not isinstance(summary, dict) or summary.get("raw") != expected_summary:
        failures.append("final.summary.raw does not match source status counts")
    if not isinstance(summary, dict) or summary.get("rawConfirmed") != raw_status["CONFIRMED"]:
        failures.append("final.summary.rawConfirmed does not match source")
    if not isinstance(summary, dict) or summary.get("rawConflict") != raw_status["CONFLICT"]:
        failures.append("final.summary.rawConflict does not match source")
    if not isinstance(summary, dict) or summary.get("rawNotFound") != raw_status["NOT_FOUND"]:
        failures.append("final.summary.rawNotFound does not match source")
    if not isinstance(summary, dict) or summary.get("duplicateCollapsedCount") != (
        len(source_records) - len(groups)
    ):
        failures.append("final.summary.duplicateCollapsedCount does not match source")

    checks = {
        "source_indices_contiguous_1_to_51": [record["index"] for record in source_sorted]
        == list(range(1, len(source_sorted) + 1)),
        "source_records_match_final_rawResults": not any(
            "source/raw record mismatch" in failure for failure in failures
        )
        and len(source_sorted) == len(final_sorted),
        "source_dedupe_matches_final_uniqueEntities": "source dedupe groups do not match final.uniqueEntities"
        not in failures,
        "final_summary_matches_source": not any(
            failure.startswith("final.summary") for failure in failures
        ),
    }
    checks["all_passed"] = not failures
    return checks, failures


def build_report(repo_root: Path) -> dict[str, Any]:
    source_payloads: dict[str, Any] = {}
    source_records: list[dict[str, Any]] = []
    input_hashes: dict[str, str] = {}
    source_shapes: dict[str, dict[str, Any]] = {}

    for filename in SOURCE_FILENAMES:
        path = repo_root / filename
        payload = load_json(path)
        source_payloads[filename] = payload
        input_hashes[filename] = sha256_file(path)
        items = source_items(payload, filename)
        validate_records(items, filename)
        source_records.extend(
            {**record, "_source_report": Path(filename).name} for record in items
        )
        source_shapes[filename] = {
            "top_level": "array" if isinstance(payload, list) else "object",
            "record_count": len(items),
            "keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
        }

    final_path = repo_root / FINAL_FILENAME
    final_payload = load_json(final_path)
    input_hashes[FINAL_FILENAME] = sha256_file(final_path)
    if not isinstance(final_payload, dict):
        raise ReplayValidationError("final verification file must be an object")
    final_raw = final_payload.get("rawResults")
    final_unique = final_payload.get("uniqueEntities")
    validate_records(final_raw or [], f"{FINAL_FILENAME}.rawResults")
    validate_records(final_unique or [], f"{FINAL_FILENAME}.uniqueEntities")

    source_records.sort(key=lambda record: record["index"])
    groups = group_records(source_records)
    normalized_records = [
        normalize_record(record, record.pop("_source_report")) for record in source_records
    ]
    # ``normalized_records`` is intentionally sorted by the source index so its
    # hash remains stable even if the input reports are passed in another order.
    normalized_records.sort(key=lambda record: record["index"])
    normalized_sha = sha256_bytes(canonical_json(normalized_records))
    record_hashes = {
        str(record["index"]): sha256_bytes(canonical_json(record))
        for record in normalized_records
    }

    checks, failures = replay_checks(source_records, final_payload)
    if failures:
        raise ReplayValidationError("\n".join(failures))

    duplicate_groups = [
        {
            "dedupe_key": key,
            "canonical_name": records[0]["canonicalName"],
            "candidate_indexes": sorted(record["index"] for record in records),
            "count": len(records),
        }
        for key, records in groups.items()
        if len(records) > 1
    ]

    report = {
        "dataset": {
            "kind": "historical_reference_set",
            "independent_accuracy_evaluation": False,
            "interpretation": (
                "These 51 records were already reviewed. They are suitable for "
                "schema/provenance/replay checks and historical reference, but "
                "must not be reported as an independent before/after accuracy test."
            ),
            "verified_at": final_payload.get("verifiedAt"),
            "scope": final_payload.get("scope"),
        },
        "input_files": input_hashes,
        "source_shapes": source_shapes,
        "record_counts": {
            "source_records": len(source_records),
            "final_rawResults": len(final_payload.get("rawResults", [])),
            "final_uniqueEntities": len(final_payload.get("uniqueEntities", [])),
            "dedupe_removed": len(source_records) - len(groups),
            "duplicate_groups": len(duplicate_groups),
        },
        "status_distribution": by_status(source_records),
        "missing_fields": {
            "all_records": missing_counts(source_records),
            "by_status": by_status_missing(source_records),
        },
        "source_coverage": source_coverage(source_records),
        "duplicate_groups": duplicate_groups,
        "replay_checks": checks,
        "normalized_records_sha256": normalized_sha,
        "record_sha256": record_hashes,
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="LAND-DATA repository root (default: inferred from this file)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for the JSON report; stdout is always emitted",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.repo_root.resolve())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
