#!/usr/bin/env python3
"""Read-only SQLite storage-path performance baseline.

This benchmark deliberately stops at the SQLite storage boundary.  It does not
call an LLM, network source, or Location Verification Agent.  Connections use
the SQLite URI ``mode=ro`` and a read transaction, and the report records the
database hashes before and after the run so that a read-only run is auditable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASES = (
    ROOT / ".local" / "land-data.db",
    ROOT / ".local" / "nyc-incremental-acceptance-v2.db",
)


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        key = candidate.name
        if candidate.exists():
            stat = candidate.stat()
            result[key] = {
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": sha256_file(candidate),
            }
        else:
            result[key] = None
    return result


def connect_read_only(path: Path) -> sqlite3.Connection:
    # ``resolve`` makes the URI unambiguous and avoids accidentally opening a
    # second database when the caller passes a relative path.
    # ``immutable=1`` prevents SQLite from updating the WAL shared-memory
    # sidecar while this benchmark is running.  The selected databases have
    # empty WAL files; if a caller supplies a non-empty WAL, the report still
    # records the sidecar hash and the caller should snapshot it first.
    uri = "file:{}?mode=ro&immutable=1".format(path.resolve())
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    return connection


def pragma_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in (
        "data_version",
        "schema_version",
        "page_count",
        "page_size",
        "freelist_count",
        "journal_mode",
    ):
        try:
            values[name] = connection.execute("PRAGMA {}".format(name)).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            values[name] = "error:{}".format(exc)
    return values


def table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def table_counts(connection: sqlite3.Connection, tables: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        # Names come from sqlite_master, not from user input.
        counts[table] = int(connection.execute('SELECT COUNT(*) FROM "{}"'.format(table)).fetchone()[0])
    return counts


def row_digest(rows: Sequence[sqlite3.Row]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            if value is None:
                encoded = b"<NULL>"
            elif isinstance(value, bytes):
                encoded = value
            else:
                encoded = str(value).encode("utf-8", "surrogatepass")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return "sha256:" + digest.hexdigest()


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def json_decode_fields(rows: Sequence[sqlite3.Row], fields: Iterable[str]) -> int:
    decoded = 0
    for row in rows:
        for field in fields:
            value = row[field]
            if value:
                json.loads(value)
                decoded += 1
    return decoded


def filter_pending_candidates(rows: Sequence[sqlite3.Row]) -> int:
    pending = 0
    for row in rows:
        payload = json.loads(row["data"])
        if payload.get("review_status") == "NEEDS_REVIEW":
            pending += 1
    return pending


@dataclass(frozen=True)
class Benchmark:
    name: str
    sql: str
    cases: tuple[tuple[str, tuple[Any, ...]], ...]
    decode_fields: tuple[str, ...] = ()
    postprocess: str | None = None


def candidate_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = set(table_names(connection))
    if not {"poi_candidates", "source_contents", "location_evidence"}.issubset(tables):
        return {"available": False, "reason": "required POI/source/evidence tables are missing"}
    summary_sql = """
        WITH evidence AS (
            SELECT e.poi_candidate_id,
                   COUNT(*) AS evidence_rows,
                   COUNT(DISTINCT json_extract(e.data, '$.type')) AS evidence_types,
                   COUNT(DISTINCT s.platform) AS evidence_platforms,
                   MIN(json_extract(e.data, '$.created_at')) AS evidence_created_min,
                   MAX(json_extract(e.data, '$.created_at')) AS evidence_created_max
            FROM location_evidence e
            LEFT JOIN source_contents s ON s.id = e.source_content_id
            GROUP BY e.poi_candidate_id
        )
        SELECT c.city,
               COUNT(*) AS candidates,
               COUNT(DISTINCT c.id) AS distinct_candidate_ids,
               COUNT(DISTINCT c.source_content_id) AS distinct_source_content_ids,
               COUNT(DISTINCT s.external_id) AS distinct_external_ids,
               SUM(CASE WHEN e.poi_candidate_id IS NOT NULL THEN 1 ELSE 0 END)
                   AS candidates_with_evidence,
               SUM(CASE WHEN e.evidence_types >= 2 THEN 1 ELSE 0 END)
                   AS candidates_with_two_evidence_types,
               SUM(CASE WHEN e.evidence_platforms >= 2 THEN 1 ELSE 0 END)
                   AS candidates_with_two_evidence_platforms,
               SUM(COALESCE(e.evidence_rows, 0)) AS evidence_rows,
               SUM(CASE WHEN s.id IS NOT NULL THEN 1 ELSE 0 END)
                   AS candidates_with_source_content,
               SUM(CASE WHEN json_extract(c.data, '$.review_status') IS NULL THEN 1 ELSE 0 END)
                   AS candidates_without_review_status,
               MIN(json_extract(c.data, '$.created_at')) AS candidate_created_min,
               MAX(json_extract(c.data, '$.created_at')) AS candidate_created_max,
               MIN(json_extract(s.data, '$.collected_at')) AS source_collected_min,
               MAX(json_extract(s.data, '$.collected_at')) AS source_collected_max,
               MIN(e.evidence_created_min) AS evidence_created_min,
               MAX(e.evidence_created_max) AS evidence_created_max
        FROM poi_candidates c
        LEFT JOIN source_contents s ON s.id = c.source_content_id
        LEFT JOIN evidence e ON e.poi_candidate_id = c.id
        GROUP BY c.city ORDER BY candidates DESC, c.city
    """
    source_sql = """
        WITH evidence AS (SELECT DISTINCT poi_candidate_id FROM location_evidence)
        SELECT c.city, COALESCE(s.platform, '<missing>') AS platform,
               COUNT(*) AS candidates,
               COUNT(DISTINCT s.external_id) AS distinct_external_ids,
               SUM(CASE WHEN e.poi_candidate_id IS NOT NULL THEN 1 ELSE 0 END)
                   AS candidates_with_evidence,
               MIN(json_extract(s.data, '$.collected_at')) AS source_collected_min,
               MAX(json_extract(s.data, '$.collected_at')) AS source_collected_max
        FROM poi_candidates c
        LEFT JOIN source_contents s ON s.id = c.source_content_id
        LEFT JOIN evidence e ON e.poi_candidate_id = c.id
        GROUP BY c.city, platform ORDER BY candidates DESC, c.city, platform
    """
    label_table_rows = connection.execute(
        """SELECT name FROM sqlite_master WHERE type='table'
           AND (lower(name) LIKE '%gold%' OR lower(name) LIKE '%label%' OR lower(name) LIKE '%eval%')
           ORDER BY name"""
    ).fetchall()
    summary = [dict(row) for row in connection.execute(summary_sql)]
    source_breakdown = [dict(row) for row in connection.execute(source_sql)]
    stable_sample_sql = """
        SELECT c.id AS candidate_id, c.resolved_poi_id AS poi_id, c.city,
               c.source_content_id, s.platform, s.external_id,
               COALESCE(json_extract(s.data, '$.source_url'),
                        json_extract(s.data, '$.canonical_url')) AS source_reference,
               json_extract(s.data, '$.content_status') AS source_status,
               json_extract(c.data, '$.created_at') AS candidate_created_at,
               json_extract(s.data, '$.collected_at') AS source_collected_at
        FROM poi_candidates c LEFT JOIN source_contents s ON s.id = c.source_content_id
        ORDER BY c.city, c.id LIMIT 20
    """
    stable_sample = [dict(row) for row in connection.execute(stable_sample_sql)]
    totals = {
        "candidates": sum(int(row["candidates"]) for row in summary),
        "distinct_candidate_ids": sum(int(row["distinct_candidate_ids"]) for row in summary),
        "candidates_with_evidence": sum(int(row["candidates_with_evidence"]) for row in summary),
        "candidates_with_two_evidence_types": sum(int(row["candidates_with_two_evidence_types"]) for row in summary),
        "candidates_with_two_evidence_platforms": sum(int(row["candidates_with_two_evidence_platforms"]) for row in summary),
        "evidence_rows": sum(int(row["evidence_rows"]) for row in summary),
        "candidates_with_source_content": sum(int(row["candidates_with_source_content"]) for row in summary),
    }
    return {
        "available": True,
        "by_city": summary,
        "by_city_and_platform": source_breakdown,
        "totals": totals,
        "stable_id_source_sample": stable_sample,
        "independent_label_tables": [row[0] for row in label_table_rows],
        "independent_gold_label_status": (
            "no dedicated gold/label/eval table detected; existing review_status is not an independent gold label"
            if not label_table_rows
            else "dedicated label-like tables exist; inspect manually before treating them as gold"
        ),
        "can_supply_500_raw_candidates": totals["candidates"] >= 500,
        "can_supply_500_candidates_with_source_and_evidence": (
            totals["candidates_with_source_content"] >= 500
            and totals["candidates_with_evidence"] >= 500
        ),
        "can_supply_500_candidates_with_two_evidence_types": totals["candidates_with_two_evidence_types"] >= 500,
        "can_supply_500_candidates_with_two_evidence_platforms": totals["candidates_with_two_evidence_platforms"] >= 500,
    }


def top_cities(connection: sqlite3.Connection, limit: int = 3) -> list[str]:
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT city FROM poi_candidates GROUP BY city ORDER BY COUNT(*) DESC, city LIMIT ?",
                (limit,),
            )
        ]
    except sqlite3.DatabaseError:
        return []


def candidate_ids(connection: sqlite3.Connection, limit: int = 12) -> list[str]:
    try:
        rows = connection.execute(
            """
            SELECT c.id
            FROM poi_candidates c JOIN location_evidence e ON e.poi_candidate_id = c.id
            GROUP BY c.id ORDER BY c.id LIMIT ?
            """,
            (limit,),
        )
        return [row[0] for row in rows]
    except sqlite3.DatabaseError:
        return []


def history_cases(connection: sqlite3.Connection, limit: int = 8) -> list[tuple[str, str]]:
    try:
        return [
            (row[0], row[1])
            for row in connection.execute(
                """
                SELECT target_type, target_id FROM review_decisions
                GROUP BY target_type, target_id ORDER BY target_type, target_id LIMIT ?
                """,
                (limit,),
            )
        ]
    except sqlite3.DatabaseError:
        return []


def build_benchmarks(connection: sqlite3.Connection, limit: int) -> list[Benchmark]:
    cities = top_cities(connection)
    city_cases = tuple((city, (city, limit)) for city in cities)
    ids = candidate_ids(connection)
    evidence_cases = tuple((candidate_id, (candidate_id,)) for candidate_id in ids)
    history = history_cases(connection)
    history_bench = ()
    if history:
        history_bench = (
            Benchmark(
                "review_history_lookup",
                """
                SELECT id,target_type,target_id,previous_status,new_status,action,created_at,data
                FROM review_decisions WHERE target_type=? AND target_id=?
                ORDER BY created_at DESC,id DESC
                """,
                tuple(("{}:{}".format(kind, target_id), (kind, target_id)) for kind, target_id in history),
                decode_fields=("data",),
            ),
        )
    return [
        Benchmark(
            "candidate_storage_scan_and_pending_filter",
            """
            SELECT id,source_content_id,resolved_poi_id,city,data
            FROM poi_candidates ORDER BY city,id
            """,
            (("all", ()),),
            decode_fields=("data",),
            postprocess="pending_filter",
        ),
        Benchmark(
            "candidate_city_page",
            """
            SELECT id,source_content_id,resolved_poi_id,city,data
            FROM poi_candidates WHERE city=? ORDER BY id LIMIT ?
            """,
            city_cases,
            decode_fields=("data",),
        ),
        Benchmark(
            "poi_city_page",
            """
            SELECT p.id,p.city,p.country,p.map_visibility,p.data
            FROM pois p
            WHERE p.city=? AND p.map_visibility=1
            ORDER BY p.id LIMIT ?
            """,
            city_cases,
            decode_fields=("data",),
        ),
        Benchmark(
            "candidate_evidence_source_lookup",
            """
            SELECT c.id AS candidate_id,c.city,e.id AS evidence_id,e.data AS evidence_data,
                   s.id AS source_content_id,s.platform,s.external_id,s.data AS source_data
            FROM poi_candidates c
            LEFT JOIN location_evidence e ON e.poi_candidate_id=c.id
            LEFT JOIN source_contents s ON s.id=c.source_content_id
            WHERE c.id=? ORDER BY e.id
            """,
            evidence_cases,
            decode_fields=("evidence_data", "source_data"),
        ),
        Benchmark(
            "evidence_rollup_by_city",
            """
            SELECT c.city,COUNT(DISTINCT c.id) AS candidates,COUNT(e.id) AS evidence_rows
            FROM poi_candidates c LEFT JOIN location_evidence e
              ON e.poi_candidate_id=c.id
            GROUP BY c.city ORDER BY c.city
            """,
            (("all", ()),),
        ),
        *history_bench,
    ]


def plan_rows(connection: sqlite3.Connection, benchmark: Benchmark) -> list[str]:
    if not benchmark.cases:
        return []
    try:
        rows = connection.execute("EXPLAIN QUERY PLAN " + benchmark.sql, benchmark.cases[0][1]).fetchall()
    except sqlite3.DatabaseError as exc:
        return ["error:{}".format(exc)]
    return ["{}|{}|{}|{}".format(row[0], row[1], row[2], row[3]) for row in rows]


def run_benchmark(
    connection: sqlite3.Connection,
    benchmark: Benchmark,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    if not benchmark.cases:
        return {"name": benchmark.name, "status": "skipped", "reason": "no sample cases available"}
    timings: list[float] = []
    query_rows: list[int] = []
    logical_rows: list[int] = []
    digests: dict[str, str] = {}
    digest_mismatches = 0
    errors: list[str] = []

    for case_name, params in benchmark.cases:
        try:
            for _ in range(warmups):
                rows = connection.execute(benchmark.sql, params).fetchall()
                if benchmark.decode_fields:
                    json_decode_fields(rows, benchmark.decode_fields)
                if benchmark.postprocess == "pending_filter":
                    filter_pending_candidates(rows)
            for _ in range(iterations):
                started = time.perf_counter_ns()
                rows = connection.execute(benchmark.sql, params).fetchall()
                logical = len(rows)
                if benchmark.decode_fields:
                    json_decode_fields(rows, benchmark.decode_fields)
                if benchmark.postprocess == "pending_filter":
                    logical = filter_pending_candidates(rows)
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                timings.append(elapsed_ms)
                query_rows.append(len(rows))
                logical_rows.append(logical)
                digest = row_digest(rows)
                previous = digests.setdefault(case_name, digest)
                if digest != previous:
                    digest_mismatches += 1
        except (sqlite3.DatabaseError, json.JSONDecodeError, KeyError) as exc:
            errors.append("{}:{}".format(case_name, exc))

    if not timings:
        return {
            "name": benchmark.name,
            "status": "error" if errors else "skipped",
            "errors": errors,
            "query_plan": plan_rows(connection, benchmark),
        }
    mean_ms = statistics.fmean(timings)
    mean_rows = statistics.fmean(query_rows)
    return {
        "name": benchmark.name,
        "status": "ok" if not errors else "partial",
        "cases": len(benchmark.cases),
        "warmups_per_case": warmups,
        "iterations_per_case": iterations,
        "queries": len(timings),
        "query_rows_mean": mean_rows,
        "logical_rows_mean": statistics.fmean(logical_rows),
        "latency_ms": {
            "min": min(timings),
            "p50": percentile(timings, 0.50),
            "p95": percentile(timings, 0.95),
            "mean": mean_ms,
            "max": max(timings),
        },
        "query_rows_per_second_mean": (mean_rows / (mean_ms / 1000.0)) if mean_ms else None,
        "repeat_digest_mismatches": digest_mismatches,
        "case_digests": digests,
        "query_plan": plan_rows(connection, benchmark),
        "errors": errors,
        "scope_note": (
            "SQLite read/fetch plus selected JSON decoding; excludes network, LLM, dynamic tool choice, "
            "human review, and Agent end-to-end latency."
        ),
    }


def run_database(path: Path, warmups: int, iterations: int, limit: int) -> dict[str, Any]:
    before = file_fingerprint(path)
    started_at = datetime.now(timezone.utc).isoformat()
    connection = connect_read_only(path)
    try:
        tables = table_names(connection)
        snapshot = pragma_snapshot(connection)
        counts = table_counts(connection, tables)
        inventory = candidate_inventory(connection)
        benchmarks = build_benchmarks(connection, limit)
        results = [run_benchmark(connection, benchmark, warmups, iterations) for benchmark in benchmarks]
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
    after = file_fingerprint(path)
    return {
        "path": str(path),
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "file_fingerprint_before": before,
        "file_fingerprint_after": after,
        "database_unchanged": before == after,
        "sqlite_snapshot": snapshot,
        "tables": tables,
        "table_counts": counts,
        "candidate_inventory": inventory,
        "benchmarks": results,
    }


def format_number(value: Any) -> str:
    if isinstance(value, float):
        return "{:.3f}".format(value)
    return str(value)


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# SQLite 地点数据只读性能基线",
        "",
        "本报告只测 LAND-DATA 的 SQLite 读取/反序列化路径，不代表 Location Verification Agent 端到端表现；没有网络、LLM、动态工具选择或人工审核。连接使用 `mode=ro` 和只读事务。",
        "",
        "- 生成时间：`{}`".format(report["generated_at"]),
        "- 运行参数：warmups={}，iterations={}，page limit={}".format(
            report["parameters"]["warmups"], report["parameters"]["iterations"], report["parameters"]["limit"]
        ),
        "",
    ]
    for database in report["databases"]:
        name = Path(database["path"]).name
        lines.extend(
            [
                "## `{}`".format(name),
                "",
                "- 主库 SHA-256：`{}`".format(database["file_fingerprint_before"][name]["sha256"]),
                "- 只读运行前后文件指纹一致：`{}`".format(database["database_unchanged"]),
                "- SQLite 快照：`{}`".format(json.dumps(database["sqlite_snapshot"], ensure_ascii=False, sort_keys=True)),
                "- 候选总量：`{}`；有 source_content：`{}`；有 location_evidence：`{}`；证据行：`{}`".format(
                    database["candidate_inventory"].get("totals", {}).get("candidates", "n/a"),
                    database["candidate_inventory"].get("totals", {}).get("candidates_with_source_content", "n/a"),
                    database["candidate_inventory"].get("totals", {}).get("candidates_with_evidence", "n/a"),
                    database["candidate_inventory"].get("totals", {}).get("evidence_rows", "n/a"),
                ),
                "- 至少 500 条原始候选可抽样：`{}`；至少 500 条同时具备来源和证据：`{}`；至少 500 条有两类证据：`{}`；至少 500 条跨两个来源平台：`{}`".format(
                    database["candidate_inventory"].get("can_supply_500_raw_candidates", False),
                    database["candidate_inventory"].get("can_supply_500_candidates_with_source_and_evidence", False),
                    database["candidate_inventory"].get("can_supply_500_candidates_with_two_evidence_types", False),
                    database["candidate_inventory"].get("can_supply_500_candidates_with_two_evidence_platforms", False),
                ),
                "- 独立金标准状态：{}".format(database["candidate_inventory"].get("independent_gold_label_status", "n/a")),
                "",
                "| 查询路径 | cases | queries | p50 ms | p95 ms | mean rows/s | digest mismatch |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for benchmark in database["benchmarks"]:
            if benchmark["status"] in {"ok", "partial"}:
                latency = benchmark["latency_ms"]
                lines.append(
                    "| `{}` | {} | {} | {} | {} | {} | {} |".format(
                        benchmark["name"],
                        benchmark.get("cases", 0),
                        benchmark.get("queries", 0),
                        format_number(latency["p50"]),
                        format_number(latency["p95"]),
                        format_number(benchmark.get("query_rows_per_second_mean")),
                        benchmark.get("repeat_digest_mismatches", "n/a"),
                    )
                )
            else:
                lines.append("| `{}` | — | — | — | — | — | skipped/error: {} |".format(benchmark["name"], benchmark.get("reason", benchmark.get("errors"))))
        lines.extend(["", "### 来源与时间范围", "", "```json", json.dumps(database["candidate_inventory"].get("by_city_and_platform", []), ensure_ascii=False, indent=2), "```", ""])
    lines.extend(
        [
            "## 解释边界",
            "",
            "数据库记录数、稳定候选/POI ID、source platform/external_id、来源时间和 evidence 行数可以用于构造后续 500+ 案例的抽样框。这里的‘两类证据/两个来源平台’只是结构性覆盖信号，不能自动证明来源独立或结论正确；已有 `review_status` 只表示历史流程状态，不能直接当作地点核验真值。正式改前/改后评测仍需冻结证据快照并完成独立标注。",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", dest="databases", type=Path, help="SQLite database path; repeatable")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--limit", type=int, default=1000, help="per-city page limit")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--json-name", default="sqlite-performance-baseline-2026-10-28.json")
    parser.add_argument("--markdown-name", default="sqlite-performance-baseline-2026-10-28.md")
    args = parser.parse_args(argv)
    if args.warmups < 0 or args.iterations <= 0 or args.limit <= 0:
        parser.error("warmups must be >= 0; iterations and limit must be > 0")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    paths = [path.resolve() for path in (args.databases or list(DEFAULT_DATABASES))]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        print("missing database(s): {}".format(", ".join(missing)), file=sys.stderr)
        return 2
    databases = []
    for path in paths:
        try:
            databases.append(run_database(path, args.warmups, args.iterations, args.limit))
        except sqlite3.DatabaseError as exc:
            print("database error for {}: {}".format(path, exc), file=sys.stderr)
            return 3
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": str(Path(__file__).resolve()),
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "parameters": {"warmups": args.warmups, "iterations": args.iterations, "limit": args.limit},
        "databases": databases,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / args.json_name
    markdown_path = args.output_dir / args.markdown_name
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path), "script_sha256": report["script_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
