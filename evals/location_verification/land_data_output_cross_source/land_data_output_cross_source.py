#!/usr/bin/env python3
"""Diagnose frozen LAND-DATA NYC outputs against saved NYC DOT snapshots.

This is an offline, read-only diagnostic for the pre-Agent LAND-DATA output.
It deliberately does not treat a cross-source match as a human Gold label or
as an Agent accuracy score.  The frozen SQLite database is opened with
``mode=ro&immutable=1`` after its SHA-256 is checked against the freeze
manifest.  Current NYC DOT rows are indexed by unique normalized name and
full address; historical DOT rows are joined by CAMIS while duplicate rows
remain visible and conflicting fields are excluded from field denominators.

Private per-candidate cases are written under ``.local`` and hashed.  Public
JSON/Markdown contains only aggregates, denominators, input hashes and claim
boundaries.
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
import sqlite3
import sys
from typing import Any, Iterable
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[3]
DOT_DIR = ROOT / "evals" / "location_verification" / "dot_cross_source"
if str(DOT_DIR) not in sys.path:
    sys.path.insert(0, str(DOT_DIR))

from dot_cross_source_check import (  # noqa: E402
    build_current_index,
    build_historical_index,
    canonical_id,
    compare_current_case,
    compare_historical_case,
    load_capture_manifest,
    load_snapshot,
    normalize_text,
    parse_coordinate,
    sha256_file,
)


DEFAULT_DB = (
    ROOT
    / ".local"
    / "baselines"
    / "location-verification-pre-agent-final-2026-10-28"
    / "nyc-incremental-acceptance-v2.db"
)
DEFAULT_FREEZE_MANIFEST = DEFAULT_DB.parent / "manifest.json"
DEFAULT_CURRENT = ROOT / ".local" / "location-verification-live-check" / "dot_batches" / "current.json"
DEFAULT_HISTORICAL = ROOT / ".local" / "location-verification-live-check" / "dot_batches" / "historical.json"
DEFAULT_CAPTURE_MANIFEST = (
    ROOT / ".local" / "location-verification-live-check" / "dot_batches" / "capture-manifest.json"
)
PUBLIC_DIR = Path(__file__).resolve().parent
DEFAULT_PRIVATE_DIR = ROOT / ".local" / "location-verification-live-check" / "land_data_output_cross_source"
DEFAULT_PRIVATE_CASES = DEFAULT_PRIVATE_DIR / "land-data-output-cross-source-cases.jsonl"
DEFAULT_PRIVATE_MANIFEST = DEFAULT_PRIVATE_DIR / "land-data-output-cross-source-manifest.json"
DEFAULT_PUBLIC_JSON = PUBLIC_DIR / "land-data-output-cross-source-summary.json"
DEFAULT_PUBLIC_MD = PUBLIC_DIR / "land-data-output-cross-source-summary.md"

EXPECTED_PLATFORM = "nyc-open-data-restaurants"
EXPECTED_CITY = "New York"
SCHEMA_VERSION = "lv-land-data-output-cross-source-v1"
FREEZE_SCHEMA_VERSION = "location-verification-pre-agent-freeze-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def rate(count: int, denominator: int) -> dict[str, Any]:
    return {
        "count": int(count),
        "denominator": int(denominator),
        "rate": round(count / denominator, 6) if denominator else None,
    }


def _ensure_new_file(path: Path, force: bool, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        os.chmod(path.parent, 0o700)
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite: {path}")
    if path.is_symlink():
        raise ValueError(f"refusing to write through symlink: {path}")


def write_json(path: Path, value: Any, force: bool, *, private: bool = False) -> str:
    _ensure_new_file(path, force, private=private)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if private:
        os.chmod(path, 0o600)
    return sha256_file(path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]], force: bool) -> str:
    _ensure_new_file(path, force, private=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")
    os.chmod(path, 0o600)
    return sha256_file(path)


def load_and_validate_freeze_manifest(path: Path, db_path: Path) -> dict[str, Any]:
    """Validate the immutable DB against the final freeze ledger."""

    if not path.is_file():
        raise FileNotFoundError(path)
    before = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    payload = json.loads(path.read_text(encoding="utf-8"))
    after = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    if before != after:
        raise RuntimeError(f"freeze manifest changed during read: {path}")
    if payload.get("schema_version") != FREEZE_SCHEMA_VERSION:
        raise ValueError(f"unexpected freeze schema: {payload.get('schema_version')!r}")
    expected = next(
        (item for item in payload.get("databases", []) if item.get("snapshot") == db_path.name),
        None,
    )
    if not isinstance(expected, dict):
        raise ValueError(f"freeze manifest has no database entry for {db_path.name}")
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    db_stat = db_path.stat()
    db_sha = sha256_file(db_path)
    if int(expected.get("bytes", -1)) != db_stat.st_size:
        raise ValueError("frozen database byte count does not match manifest")
    if expected.get("sha256") != db_sha:
        raise ValueError("frozen database SHA-256 does not match manifest")
    return {
        "path": str(path),
        "sha256": before[0],
        "bytes": before[1],
        "schema_version": payload.get("schema_version"),
        "git_head": payload.get("git_head"),
        "database": {
            "path": str(db_path),
            "snapshot": db_path.name,
            "bytes": db_stat.st_size,
            "sha256": db_sha,
            "quick_check_from_manifest": expected.get("quick_check"),
        },
    }


def open_immutable_database(path: Path) -> sqlite3.Connection:
    # quote() keeps this correct if a future snapshot path contains spaces.
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        connection.close()
        raise RuntimeError(f"SQLite quick_check failed: {quick_check}")
    return connection


def parse_json_object(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError(f"{label} is not JSON text")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _status(value: Any) -> str:
    text = str(value or "").strip()
    return text or "MISSING"


def load_dohmh_outputs(connection: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the frozen DOHMH output path, retaining candidate and POI fields."""

    source_count = connection.execute(
        "SELECT COUNT(*) FROM source_contents WHERE platform = ?", (EXPECTED_PLATFORM,)
    ).fetchone()[0]
    candidate_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM poi_candidates pc
        JOIN source_contents sc ON sc.id = pc.source_content_id
        WHERE pc.city = ? AND sc.platform = ?
        """,
        (EXPECTED_CITY, EXPECTED_PLATFORM),
    ).fetchone()[0]
    if source_count != candidate_count:
        raise ValueError(
            f"DOHMH source/candidate count mismatch: source_contents={source_count}, candidates={candidate_count}"
        )

    rows = connection.execute(
        """
        SELECT
            pc.id AS candidate_id,
            pc.source_content_id AS source_content_id,
            pc.resolved_poi_id AS resolved_poi_id,
            pc.city AS candidate_city,
            pc.data AS candidate_data,
            sc.platform AS platform,
            sc.external_id AS source_external_id,
            sc.data AS source_data,
            p.id AS poi_id,
            p.city AS poi_city,
            p.data AS poi_data
        FROM poi_candidates pc
        JOIN source_contents sc ON sc.id = pc.source_content_id
        LEFT JOIN pois p ON p.id = pc.resolved_poi_id
        WHERE pc.city = ? AND sc.platform = ?
        ORDER BY pc.id
        """,
        (EXPECTED_CITY, EXPECTED_PLATFORM),
    ).fetchall()
    if len(rows) != candidate_count:
        raise RuntimeError("SQLite result changed while reading candidate outputs")

    outputs: list[dict[str, Any]] = []
    for row in rows:
        candidate_data = parse_json_object(row["candidate_data"], "candidate_data")
        source_data = parse_json_object(row["source_data"], "source_data")
        poi_data = parse_json_object(row["poi_data"], "poi_data") if row["poi_data"] else {}
        name = candidate_data.get("name")
        address = candidate_data.get("address")
        latitude = parse_coordinate(candidate_data.get("latitude"))
        longitude = parse_coordinate(candidate_data.get("longitude"))
        external_id = canonical_id(row["source_external_id"])
        outputs.append(
            {
                "candidate_id": row["candidate_id"],
                "source_content_id": row["source_content_id"],
                "resolved_poi_id": row["resolved_poi_id"],
                "candidate_city": row["candidate_city"],
                "platform": row["platform"],
                "external_id": external_id,
                "name": name,
                "address": address,
                "latitude": latitude,
                "longitude": longitude,
                "candidate_review_status": _status(candidate_data.get("review_status")),
                "candidate_confidence": candidate_data.get("confidence_score"),
                "source_collected_at": source_data.get("collected_at"),
                "source_record_date": (source_data.get("raw_metadata") or {}).get("source_metadata", {}).get("record_date")
                if isinstance(source_data.get("raw_metadata"), dict)
                else None,
                "poi_id": row["poi_id"],
                "poi_review_status": _status(poi_data.get("review_status")) if poi_data else "MISSING",
                "poi_active": poi_data.get("active") if poi_data else None,
            }
        )
    statuses = Counter(row["candidate_review_status"] for row in outputs)
    resolved_by_status = Counter(
        row["candidate_review_status"] for row in outputs if row["resolved_poi_id"]
    )
    distinct_poi_ids = {row["resolved_poi_id"] for row in outputs if row["resolved_poi_id"]}
    poi_statuses = Counter()
    poi_status_by_id: dict[str, set[str]] = defaultdict(set)
    poi_missing = 0
    for row in outputs:
        if not row["resolved_poi_id"]:
            continue
        if row["poi_id"] is None:
            poi_missing += 1
        else:
            poi_statuses[row["poi_review_status"]] += 1
            poi_status_by_id[row["poi_id"]].add(row["poi_review_status"])
    poi_statuses_distinct = Counter(
        next(iter(values)) if len(values) == 1 else "CONFLICTING_STATUS"
        for values in poi_status_by_id.values()
    )
    return outputs, {
        "source_contents": source_count,
        "poi_candidates": candidate_count,
        "candidate_status_counts": dict(sorted(statuses.items())),
        "resolved_candidate_counts_by_status": dict(sorted(resolved_by_status.items())),
        "resolved_candidate_count": sum(bool(row["resolved_poi_id"]) for row in outputs),
        "unresolved_candidate_count": sum(not bool(row["resolved_poi_id"]) for row in outputs),
        "distinct_resolved_poi_ids": len(distinct_poi_ids),
        "linked_poi_status_counts_by_candidate_row": dict(sorted(poi_statuses.items())),
        "distinct_poi_status_counts": dict(sorted(poi_statuses_distinct.items())),
        "distinct_poi_status_conflict_count": sum(len(values) > 1 for values in poi_status_by_id.values()),
        "linked_resolved_rows_missing_poi": poi_missing,
        "candidate_city_counts": dict(sorted(Counter(row["candidate_city"] for row in outputs).items())),
        "source_record_date_counts": dict(sorted(Counter(row["source_record_date"] for row in outputs).items())),
    }


def _candidate_row_for_dot(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "screening_id": output["candidate_id"],
        "candidate_id": output["candidate_id"],
        "name": output["name"],
        "address": output["address"],
        "city": output["candidate_city"],
        "latitude": output["latitude"],
        "longitude": output["longitude"],
        "platform": output["platform"],
        "external_id": output["external_id"],
    }


def compact_current_case(output: dict[str, Any], index: dict[tuple[str, str], list[dict[str, Any]]]) -> dict[str, Any]:
    comparison = compare_current_case(_candidate_row_for_dot(output), index)
    return {
        "mode": "current",
        "candidate_id_digest": digest_value(output["candidate_id"]),
        "source_content_id_digest": digest_value(output["source_content_id"]),
        "external_id_digest": digest_value(output["external_id"]),
        "candidate_review_status": output["candidate_review_status"],
        "resolved_poi": bool(output["resolved_poi_id"]),
        "poi_review_status": output["poi_review_status"],
        "match_type": comparison["match_type"],
        "match_count": comparison["match_count"],
        "coordinate_comparable": comparison.get("coordinate_comparable"),
        "coordinate_distance_m": comparison.get("coordinate_distance_m"),
        "coordinate_within_10m": comparison.get("coordinate_within_10m"),
        "coordinate_within_50m": comparison.get("coordinate_within_50m"),
        "dot_status_present": comparison.get("dot_status_present"),
    }


def compact_historical_case(output: dict[str, Any], index: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    comparison = compare_historical_case(_candidate_row_for_dot(output), index)
    return {
        "mode": "historical",
        "candidate_id_digest": digest_value(output["candidate_id"]),
        "source_content_id_digest": digest_value(output["source_content_id"]),
        "external_id_digest": digest_value(output["external_id"]),
        "candidate_review_status": output["candidate_review_status"],
        "resolved_poi": bool(output["resolved_poi_id"]),
        "poi_review_status": output["poi_review_status"],
        "camis_join_eligible": bool(output["external_id"] and output["external_id"].isdigit()),
        "match_type": comparison["match_type"],
        "match_count": comparison["match_count"],
        "duplicate_consistency": comparison.get("duplicate_consistency"),
        "address_comparable": comparison.get("address_comparable"),
        "address_equal": comparison.get("address_equal"),
        "coordinate_comparable": comparison.get("coordinate_comparable"),
        "coordinate_distance_m": comparison.get("coordinate_distance_m"),
        "coordinate_within_10m": comparison.get("coordinate_within_10m"),
        "coordinate_within_50m": comparison.get("coordinate_within_50m"),
    }


def _group_cases(cases: list[dict[str, Any]], status: str | None = None) -> list[dict[str, Any]]:
    if status is None:
        return cases
    return [case for case in cases if case["candidate_review_status"] == status]


def current_diagnostics(
    outputs: list[dict[str, Any]],
    current_records: list[dict[str, Any]],
    current_index: dict[tuple[str, str], list[dict[str, Any]]],
    current_index_stats: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cases = [compact_current_case(output, current_index) for output in outputs]
    eligible = [
        case
        for case, output in zip(cases, outputs)
        if bool(str(output["name"] or "").strip()) and bool(str(output["address"] or "").strip())
    ]
    unique = [case for case in cases if case["match_type"] == "unique_name_address_match"]
    any_match = [
        case
        for case in cases
        if case["match_type"] in {"unique_name_address_match", "ambiguous_name_address_match"}
    ]

    by_status: dict[str, Any] = {}
    statuses = sorted({case["candidate_review_status"] for case in cases})
    for status in statuses + ["__all__"]:
        group = _group_cases(cases, None if status == "__all__" else status)
        group_eligible = [
            case
            for case, output in zip(cases, outputs)
            if (status == "__all__" or output["candidate_review_status"] == status)
            and bool(str(output["name"] or "").strip())
            and bool(str(output["address"] or "").strip())
        ]
        group_unique = [case for case in group if case["match_type"] == "unique_name_address_match"]
        comparable = [case for case in group_unique if case.get("coordinate_comparable")]
        over_10 = [case for case in comparable if case.get("coordinate_distance_m") is not None and case["coordinate_distance_m"] > 10]
        over_50 = [case for case in comparable if case.get("coordinate_distance_m") is not None and case["coordinate_distance_m"] > 50]
        by_status[status] = {
            "candidate_rows": len(group),
            "name_address_key_eligible": len(group_eligible),
            "unique_name_address_match": rate(len(group_unique), len(group_eligible)),
            "coordinate_comparable": rate(len(comparable), len(group_unique)),
            "coordinate_within_10m": rate(len(comparable) - len(over_10), len(comparable)),
            "coordinate_over_10m": rate(len(over_10), len(comparable)),
            "coordinate_within_50m": rate(len(comparable) - len(over_50), len(comparable)),
            "coordinate_over_50m": rate(len(over_50), len(comparable)),
            "ambiguous_name_address_match": sum(case["match_type"] == "ambiguous_name_address_match" for case in group),
            "no_unique_name_address_match": sum(case["match_type"] == "no_unique_name_address_match" for case in group),
            "candidate_missing_name_or_address": sum(case["match_type"] == "candidate_missing_name_or_address" for case in group),
        }

    match_counts = Counter(case["match_type"] for case in cases)
    summary = {
        "snapshot_rows": len(current_records),
        "snapshot_filter": {
            "candidate_scope": "all frozen New York DOHMH poi_candidates joined to source_contents",
            "dot_rows_used": "all current DOT rows with non-empty normalized name and full address",
            "status_filter": "none",
            "match_key": "exact normalized candidate name + exact normalized candidate full address; unique DOT row required",
        },
        "candidate_scope": {
            "rows": len(outputs),
            "name_address_key_eligible": len(eligible),
        },
        "match_counts": dict(sorted(match_counts.items())),
        "any_name_address_match": rate(len(any_match), len(eligible)),
        "unique_name_address_match": rate(len(unique), len(eligible)),
        "metrics_after_unique_match": {
            "coordinate_comparable": rate(sum(bool(case.get("coordinate_comparable")) for case in unique), len(unique)),
            "coordinate_within_10m": rate(
                sum(bool(case.get("coordinate_within_10m")) for case in unique if case.get("coordinate_comparable")),
                sum(bool(case.get("coordinate_comparable")) for case in unique),
            ),
            "coordinate_within_50m": rate(
                sum(bool(case.get("coordinate_within_50m")) for case in unique if case.get("coordinate_comparable")),
                sum(bool(case.get("coordinate_comparable")) for case in unique),
            ),
            "coordinate_over_10m": rate(
                sum(bool(case.get("coordinate_comparable")) and case.get("coordinate_distance_m", 0) > 10 for case in unique),
                sum(bool(case.get("coordinate_comparable")) for case in unique),
            ),
            "coordinate_over_50m": rate(
                sum(bool(case.get("coordinate_comparable")) and case.get("coordinate_distance_m", 0) > 50 for case in unique),
                sum(bool(case.get("coordinate_comparable")) for case in unique),
            ),
        },
        "legacy_status_breakdown": by_status,
        "index": current_index_stats,
        "denominator_note": (
            "The current DOT name+address key is a retrieval/coverage condition, not a correctness label. "
            "Coordinate rates use only unique matches with two valid coordinates. Values over 10m or 50m are "
            "field conflicts and are grouped by the frozen legacy candidate review_status."
        ),
    }
    return summary, cases


def historical_diagnostics(
    outputs: list[dict[str, Any]],
    historical_records: list[dict[str, Any]],
    historical_index: dict[str, list[dict[str, Any]]],
    historical_index_stats: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cases = [compact_historical_case(output, historical_index) for output in outputs]
    eligible = [case for case in cases if case["camis_join_eligible"]]
    matched = [case for case in cases if case["match_count"] > 0]
    address_cases = [case for case in matched if case.get("address_comparable")]
    coordinate_cases = [case for case in matched if case.get("coordinate_comparable")]
    statuses = sorted({case["candidate_review_status"] for case in cases})
    by_status: dict[str, Any] = {}
    for status in statuses + ["__all__"]:
        group = _group_cases(cases, None if status == "__all__" else status)
        group_matched = [case for case in group if case["match_count"] > 0]
        group_address = [case for case in group_matched if case.get("address_comparable")]
        group_coordinate = [case for case in group_matched if case.get("coordinate_comparable")]
        by_status[status] = {
            "candidate_rows": len(group),
            "camis_match": rate(len(group_matched), len(group)),
            "address_equal": rate(sum(bool(case.get("address_equal")) for case in group_address), len(group_address)),
            "coordinate_within_10m": rate(sum(bool(case.get("coordinate_within_10m")) for case in group_coordinate), len(group_coordinate)),
            "coordinate_within_50m": rate(sum(bool(case.get("coordinate_within_50m")) for case in group_coordinate), len(group_coordinate)),
            "duplicate_rows": sum(case["match_type"] == "duplicate_camis_rows" for case in group),
            "duplicate_field_conflict_or_missing": sum(case.get("duplicate_consistency") == "field_missing_or_conflict" for case in group),
        }
    match_counts = Counter(case["match_type"] for case in cases)
    summary = {
        "snapshot_rows": len(historical_records),
        "snapshot_filter": {
            "candidate_scope": "all frozen New York DOHMH poi_candidates joined to source_contents",
            "dot_rows_used": "all historical DOT rows with non-empty food_service_establishment/CAMIS",
            "join_scope": "numeric source_contents.external_id/CAMIS",
            "duplicate_policy": "retain all rows; a field enters its denominator only when every duplicate row is non-empty and identical",
        },
        "candidate_scope": {"rows": len(outputs), "camis_join_eligible": len(eligible)},
        "match_counts": dict(sorted(match_counts.items())),
        "camis_match": rate(len(matched), len(eligible)),
        "duplicate_diagnostics": {
            "matched_cases_with_duplicate_rows": sum(case["match_type"] == "duplicate_camis_rows" for case in matched),
            "uniform_selected_fields": sum(case.get("duplicate_consistency") == "uniform_selected_fields" for case in matched),
            "field_missing_or_conflict": sum(case.get("duplicate_consistency") == "field_missing_or_conflict" for case in matched),
        },
        "metrics_after_camis_match": {
            "address_equal": rate(sum(bool(case.get("address_equal")) for case in address_cases), len(address_cases)),
            "coordinate_within_10m": rate(sum(bool(case.get("coordinate_within_10m")) for case in coordinate_cases), len(coordinate_cases)),
            "coordinate_within_50m": rate(sum(bool(case.get("coordinate_within_50m")) for case in coordinate_cases), len(coordinate_cases)),
        },
        "legacy_status_breakdown": by_status,
        "index": historical_index_stats,
        "denominator_note": (
            "Historical DOT CAMIS linkage is a cross-source historical consistency diagnostic. "
            "Address and coordinate rates exclude missing candidate fields and duplicate CAMIS conflicts. "
            "The 2023 historical snapshot cannot prove that a place is currently open and is not a current-business Gold label."
        ),
    }
    return summary, cases


def build_public_markdown(summary: dict[str, Any]) -> str:
    def fmt_metric(metric: dict[str, Any]) -> str:
        return f"{metric.get('count', 0)}/{metric.get('denominator', 0)} ({metric.get('rate')})"

    source = summary["land_data_output"]
    current = summary["current_dot"]
    historical = summary["historical_dot"]
    status = source["candidate_status_counts"]
    current_all = current["legacy_status_breakdown"]["__all__"]
    historical_all = historical["legacy_status_breakdown"]["__all__"]
    lines = [
        "# LAND-DATA 全量 NYC 输出跨来源诊断",
        "",
        "本报告只读读取冻结的 LAND-DATA NYC SQLite 输出，再与已保存的 NYC DOT 当前快照和历史快照做字段级诊断。它用于说明旧流程的输出分布、跨来源覆盖和坐标冲突，不是人工 Gold，也不能直接写成地点核验准确率或 Agent 提升。",
        "",
        f"- 运行标识：`{summary['run_id']}`；脚本 SHA-256：`{summary['script_sha256']}`。",
        f"- 冻结 SQLite：`{source['database']['snapshot']}`；SHA-256：`{source['database']['sha256']}`；字节数：`{source['database']['bytes']}`。",
        f"- 冻结清单 SHA-256：`{source['freeze_manifest_sha256']}`；SQLite `quick_check`：`{source['database']['quick_check']}`。",
        f"- DOHMH `source_contents`：`{source['source_contents']}`；对应 `poi_candidates`：`{source['poi_candidates']}`；去重后的已解析 POI：`{source['distinct_resolved_poi_ids']}`。同城数据库另有 11 条 OSM 候选，本诊断不纳入。",
        "",
        "## 改前旧系统输出分布",
        "",
        f"- `AUTO_APPROVED`：`{status.get('AUTO_APPROVED', 0)}`；`NEEDS_REVIEW`：`{status.get('NEEDS_REVIEW', 0)}`；其他/缺失状态：`{sum(value for key, value in status.items() if key not in {'AUTO_APPROVED', 'NEEDS_REVIEW'})}`。",
        f"- 已解析到 POI 的候选：`{source['resolved_candidate_count']}`；未解析：`{source['unresolved_candidate_count']}`。",
        f"- 候选行关联的 POI 状态分布：`{json.dumps(source['linked_poi_status_counts_by_candidate_row'], ensure_ascii=False, sort_keys=True)}`。",
        f"- 去重 POI 输出状态分布：`{json.dumps(source['distinct_poi_status_counts'], ensure_ascii=False, sort_keys=True)}`；POI 状态冲突 ID：`{source['distinct_poi_status_conflict_count']}`。",
        "",
        "## 当前 NYC DOT（独立来源，但不是 Gold）",
        "",
        f"- 当前快照行数：`{current['snapshot_rows']}`；名称＋地址可比较候选：`{current['candidate_scope']['name_address_key_eligible']}`。",
        f"- 唯一规范化名称＋完整地址匹配：`{current['unique_name_address_match']['count']}/{current['unique_name_address_match']['denominator']}`（覆盖率 `{current['unique_name_address_match']['rate']}`）；任意匹配（含多行）：`{current['any_name_address_match']['count']}/{current['any_name_address_match']['denominator']}`。",
        f"- 唯一匹配坐标可比较：`{current['metrics_after_unique_match']['coordinate_comparable']['count']}/{current['metrics_after_unique_match']['coordinate_comparable']['denominator']}`；坐标 `>10m`：`{current['metrics_after_unique_match']['coordinate_over_10m']['count']}/{current['metrics_after_unique_match']['coordinate_over_10m']['denominator']}`；`>50m`：`{current['metrics_after_unique_match']['coordinate_over_50m']['count']}/{current['metrics_after_unique_match']['coordinate_over_50m']['denominator']}`。",
        f"- 全体候选旧状态分层：`AUTO_APPROVED` 唯一匹配 `{fmt_metric(current['legacy_status_breakdown'].get('AUTO_APPROVED', {}).get('unique_name_address_match', {}))}`；`NEEDS_REVIEW` 唯一匹配 `{fmt_metric(current['legacy_status_breakdown'].get('NEEDS_REVIEW', {}).get('unique_name_address_match', {}))}`。",
        f"- 全体候选旧状态下坐标冲突：`AUTO_APPROVED` >10m `{fmt_metric(current['legacy_status_breakdown'].get('AUTO_APPROVED', {}).get('coordinate_over_10m', {}))}`、>50m `{fmt_metric(current['legacy_status_breakdown'].get('AUTO_APPROVED', {}).get('coordinate_over_50m', {}))}`；`NEEDS_REVIEW` >10m `{fmt_metric(current['legacy_status_breakdown'].get('NEEDS_REVIEW', {}).get('coordinate_over_10m', {}))}`、>50m `{fmt_metric(current['legacy_status_breakdown'].get('NEEDS_REVIEW', {}).get('coordinate_over_50m', {}))}`。",
        "- 解释：当前 DOT 没有 CAMIS，名称＋地址只做保守检索键；唯一匹配和坐标阈值是跨来源一致性/冲突诊断，不是地点身份正确率。",
        "",
        "## 历史 NYC DOT（CAMIS 连接）",
        "",
        f"- 历史快照行数：`{historical['snapshot_rows']}`；CAMIS 连接：`{historical['camis_match']['count']}/{historical['camis_match']['denominator']}`（覆盖率 `{historical['camis_match']['rate']}`）。",
        f"- 匹配后地址相同：`{fmt_metric(historical['metrics_after_camis_match']['address_equal'])}`；坐标 ≤10m：`{fmt_metric(historical['metrics_after_camis_match']['coordinate_within_10m'])}`；≤50m：`{fmt_metric(historical['metrics_after_camis_match']['coordinate_within_50m'])}`。",
        f"- 重复 CAMIS 匹配行：`{historical['duplicate_diagnostics']['matched_cases_with_duplicate_rows']}`；字段缺失或冲突：`{historical['duplicate_diagnostics']['field_missing_or_conflict']}`。",
        "- 历史 DOT 数据按 CAMIS 连接但保留重复申请；它只支持历史字段一致性诊断，不能证明当前营业，也不作为当前营业 Gold。",
        "",
        "## 复现与证据边界",
        "",
        "脚本通过冻结清单校验数据库字节数和 SHA-256，再以 SQLite immutable read-only 模式读取；DOT 快照和抓取清单也会校验哈希。逐案结果只放在 `.local/location-verification-live-check/land_data_output_cross_source/` 并单独哈希，公开文件只保留聚合计数和分母。正式 Agent 前后提升仍需独立人工 Gold、相同锁定案例和可审计运行轨迹。",
        "",
        f"- 当前全体候选唯一匹配汇总（便于查阅）：`{fmt_metric(current_all['unique_name_address_match'])}`。",
        f"- 历史全体候选 CAMIS/字段汇总（便于查阅）：`{fmt_metric(historical_all['camis_match'])}`。",
        "",
        "复现命令：",
        "",
        "```sh",
        "python3 evals/location_verification/land_data_output_cross_source/land_data_output_cross_source.py \\",
        "  --run-id 20261028T000000Z --force",
        "```",
        "",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    freeze = load_and_validate_freeze_manifest(args.freeze_manifest, args.db)
    capture_current, current_info = load_snapshot(args.current_input, "current")
    capture_historical, historical_info = load_snapshot(args.historical_input, "historical")
    capture_manifest = load_capture_manifest(args.capture_manifest, current_info, historical_info)

    connection = open_immutable_database(args.db)
    try:
        outputs, output_summary = load_dohmh_outputs(connection)
    finally:
        connection.close()

    current_index, current_index_stats = build_current_index(capture_current)
    historical_index, historical_index_stats = build_historical_index(capture_historical)
    current_summary, current_cases = current_diagnostics(
        outputs, capture_current, current_index, current_index_stats
    )
    historical_summary, historical_cases = historical_diagnostics(
        outputs, capture_historical, historical_index, historical_index_stats
    )

    private_cases = current_cases + historical_cases
    private_hash = write_jsonl(args.private_output, private_cases, args.force)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    script_sha = sha256_file(Path(__file__))
    source_public = {
        **output_summary,
        "database": {
            **freeze["database"],
            "quick_check": "ok",
        },
        "freeze_manifest_sha256": freeze["sha256"],
        "freeze_manifest_schema": freeze["schema_version"],
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "script_sha256": script_sha,
        "land_data_output": source_public,
        "inputs": {
            "freeze_manifest": freeze,
            "current_dot": current_info,
            "historical_dot": historical_info,
            "dot_capture_manifest": capture_manifest,
        },
        "current_dot": current_summary,
        "historical_dot": historical_summary,
        "private_cases": {
            "path": str(args.private_output),
            "rows": len(private_cases),
            "sha256": private_hash,
        },
        "claim_boundary": (
            "This is a frozen LAND-DATA output and cross-source consistency diagnostic. "
            "Current DOT unique name+address and historical CAMIS linkage are not human Gold labels; "
            "historical DOT cannot prove current operation. No Agent accuracy or pre/post improvement is claimed."
        ),
    }
    public_hash = write_json(args.public_json, summary, args.force)
    _ensure_new_file(args.public_markdown, args.force)
    args.public_markdown.write_text(build_public_markdown(summary), encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "script_sha256": script_sha,
        "freeze_manifest_sha256": freeze["sha256"],
        "database": freeze["database"],
        "inputs": summary["inputs"],
        "land_data_output": output_summary,
        "current_dot": current_summary,
        "historical_dot": historical_summary,
        "private_cases": summary["private_cases"],
        "public_json": {"path": str(args.public_json), "sha256": public_hash},
        "public_markdown": {"path": str(args.public_markdown), "sha256": sha256_file(args.public_markdown)},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    manifest_hash = write_json(args.private_manifest, manifest, args.force, private=True)
    summary["private_manifest"] = {"path": str(args.private_manifest), "sha256": manifest_hash}
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--freeze-manifest", type=Path, default=DEFAULT_FREEZE_MANIFEST)
    parser.add_argument("--current-input", type=Path, default=DEFAULT_CURRENT)
    parser.add_argument("--historical-input", type=Path, default=DEFAULT_HISTORICAL)
    parser.add_argument("--capture-manifest", type=Path, default=DEFAULT_CAPTURE_MANIFEST)
    parser.add_argument("--private-output", type=Path, default=DEFAULT_PRIVATE_CASES)
    parser.add_argument("--private-manifest", type=Path, default=DEFAULT_PRIVATE_MANIFEST)
    parser.add_argument("--public-json", type=Path, default=DEFAULT_PUBLIC_JSON)
    parser.add_argument("--public-markdown", type=Path, default=DEFAULT_PUBLIC_MD)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
