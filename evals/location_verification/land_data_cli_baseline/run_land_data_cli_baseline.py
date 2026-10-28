#!/usr/bin/env python3
"""Run the pre-Agent LAND-DATA validate/review path on a private DB copy.

The input database is opened read-only for the before snapshot.  The CLI is
run only against a copied database, so the frozen source is never modified.
This script reports operational status and review coverage; it deliberately
does not turn AUTO_APPROVED into an accuracy label.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = "file:{}?mode=ro".format(path.resolve())
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def grouped(connection: sqlite3.Connection, sql: str) -> Dict[str, int]:
    return {str(row[0]): int(row[1]) for row in connection.execute(sql)}


def user_tables(connection: sqlite3.Connection) -> Tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )


def db_summary(path: Path) -> Dict[str, Any]:
    with connect_read_only(path) as connection:
        summary: Dict[str, Any] = {
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "table_counts": {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in user_tables(connection)
            },
            "candidate_status": grouped(
                connection,
                "SELECT json_extract(data, '$.review_status'), COUNT(*) "
                "FROM poi_candidates GROUP BY 1",
            ),
            "candidate_status_city": {
                "{}:{}".format(row[0], row[1]): int(row[2])
                for row in connection.execute(
                    "SELECT city, json_extract(data, '$.review_status'), COUNT(*) "
                    "FROM poi_candidates GROUP BY city, 2"
                )
            },
            "candidate_source_platform": grouped(
                connection,
                "SELECT sc.platform, COUNT(*) FROM poi_candidates pc "
                "JOIN source_contents sc ON sc.id = pc.source_content_id GROUP BY sc.platform",
            ),
            "resolved_candidates": int(
                connection.execute(
                    "SELECT COUNT(*) FROM poi_candidates WHERE resolved_poi_id IS NOT NULL"
                ).fetchone()[0]
            ),
            "evidence_rows": int(
                connection.execute("SELECT COUNT(*) FROM location_evidence").fetchone()[0]
            ),
            "evidence_candidate_count": int(
                connection.execute(
                    "SELECT COUNT(DISTINCT poi_candidate_id) FROM location_evidence"
                ).fetchone()[0]
            ),
            "poi_status": grouped(
                connection,
                "SELECT json_extract(data, '$.review_status'), COUNT(*) "
                "FROM pois GROUP BY 1",
            ),
            "visible_pois": int(
                connection.execute("SELECT COUNT(*) FROM pois WHERE map_visibility = 1").fetchone()[0]
            ),
        }
    return summary


def _table_rows(connection: sqlite3.Connection, table: str) -> Tuple[Tuple[str, ...], list]:
    columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
    if not columns:
        return (), []
    rows = [tuple(row[column] for column in columns) for row in connection.execute(f'SELECT * FROM "{table}"')]
    return columns, rows


def logical_diff(before: Path, after: Path) -> Dict[str, Any]:
    """Compare all rows in the known application tables, ignoring file metadata."""
    diff: Dict[str, Any] = {}
    with connect_read_only(before) as old, connect_read_only(after) as new:
        tables = sorted(set(user_tables(old)) | set(user_tables(new)))
        for table in tables:
            _, old_rows = _table_rows(old, table)
            _, new_rows = _table_rows(new, table)
            old_counter = collections.Counter(old_rows)
            new_counter = collections.Counter(new_rows)
            added = sum((new_counter - old_counter).values())
            removed = sum((old_counter - new_counter).values())
            diff[table] = {
                "before": len(old_rows),
                "after": len(new_rows),
                "added": added,
                "removed": removed,
                "changed": min(added, removed),
            }
    return diff


def run_cli(root: Path, db: Path, command: Iterable[str]) -> Tuple[int, float, str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    started = time.perf_counter()
    process = subprocess.run(
        [sys.executable, "-m", "land_data", "--root", str(root), "--db", str(db), *command],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    return process.returncode, elapsed, process.stdout, process.stderr


def run(input_db: Path, run_dir: Path, root: Path) -> Dict[str, Any]:
    if not input_db.is_file():
        raise FileNotFoundError(input_db)
    run_dir.mkdir(parents=True, exist_ok=True)
    after_db = run_dir / "nyc-validate-after.db"
    if after_db.exists():
        raise FileExistsError(f"refusing to overwrite existing run: {after_db}")
    shutil.copy2(input_db, after_db)
    after_db.chmod(0o600)
    before = db_summary(input_db)
    validate_code, validate_seconds, validate_stdout, validate_stderr = run_cli(
        root, after_db, ("validate",)
    )
    if validate_code != 0:
        raise RuntimeError(
            "validate failed ({}): {}".format(validate_code, validate_stderr.strip())
        )
    review_code, review_seconds, review_stdout, review_stderr = run_cli(
        root,
        after_db,
        ("review", "list", "--type", "candidate", "--city", "New York"),
    )
    if review_code != 0:
        raise RuntimeError(
            "review failed ({}): {}".format(review_code, review_stderr.strip())
        )
    review_payload = json.loads(review_stdout)
    review_reason_counts = collections.Counter(
        reason["code"]
        for item in review_payload.get("items", [])
        for reason in item.get("reasons", [])
    )
    after = db_summary(after_db)
    diff = logical_diff(input_db, after_db)
    return {
        "protocol": "LAND-DATA pre-Agent validate/review operational baseline",
        "input_db": str(input_db),
        "output_db": str(after_db),
        "network_calls": 0,
        "llm_calls": 0,
        "before": before,
        "validate": {
            "exit_code": validate_code,
            "elapsed_seconds": round(validate_seconds, 4),
            "stdout": json.loads(validate_stdout),
        },
        "review": {
            "exit_code": review_code,
            "elapsed_seconds": round(review_seconds, 4),
            "city": "New York",
            "type": "candidate",
            "pending_count": int(review_payload.get("count", 0)),
            "reason_counts": dict(sorted(review_reason_counts.items())),
        },
        "after": after,
        "logical_diff": diff,
        "logical_row_diff_total": sum(
            item["added"] + item["removed"] for item in diff.values()
        ),
        "limitations": [
            "AUTO_APPROVED is an internal workflow status, not an independently verified truth label.",
            "The existing CLI has no dynamic next-tool selection or independent Gold labels.",
            "This run measures deterministic throughput, review routing, and idempotence only.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-db", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[3], type=Path)
    parser.add_argument("--output", type=Path, help="optional JSON summary path")
    args = parser.parse_args()
    summary = run(args.input_db.resolve(), args.run_dir.resolve(), args.root.resolve())
    rendered = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
