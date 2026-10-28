#!/usr/bin/env python3
"""Build a private, unlabelled Location Verification screening queue.

The script reads only the immutable SQLite copies in the pre-agent freeze.  It
does not access the live databases, network, model APIs, or review services.
The JSONL queue is private under ``.local``; the public report contains only
counts, distributions, rules, and hashes.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FREEZE = ROOT / ".local" / "baselines" / "location-verification-pre-agent-2026-10-28"
DEFAULT_PRIVATE_DIR = DEFAULT_FREEZE / "screening"
DEFAULT_PUBLIC_DIR = Path(__file__).resolve().parent
DEFAULT_SEED = 20261028
DEFAULT_NYC_COUNT = 800
DEFAULT_HANOI_COUNT = 200
QUEUE_NAME = "location-verification-screening-2026-10-28.jsonl"
PRIVATE_MANIFEST_NAME = "location-verification-screening-manifest-2026-10-28.json"
PUBLIC_JSON_NAME = "sampling-report-2026-10-28.json"
PUBLIC_MD_NAME = "sampling-report-2026-10-28.md"

FORBIDDEN_KEYS = {
    "review_status",
    "review_reasons",
    "resolved_poi_id",
    "matched_poi_id",
    "coordinate_verified",
    "coordinate_precision",
    "gold_action",
    "gold_value",
    "adjudication_note",
    "annotator_a",
    "annotator_b",
    "adjudicator",
    "split",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def opaque_digest(prefix: str, value: str) -> str:
    return "sha256:" + sha256_bytes((prefix + value).encode("utf-8"))


def connect_snapshot(path: Path) -> sqlite3.Connection:
    # Immutable mode prevents SQLite from touching the backup's -shm sidecar.
    connection = sqlite3.connect("file:{}?mode=ro&immutable=1".format(path.resolve()), uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def load_manifest(freeze_dir: Path) -> dict[str, Any]:
    manifest_path = freeze_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = {entry["snapshot"]: entry for entry in manifest.get("databases", [])}
    expected = {"land-data.db", "nyc-incremental-acceptance-v2.db"}
    if set(entries) != expected:
        raise ValueError("freeze manifest must contain exactly the two expected database snapshots")
    for snapshot, entry in entries.items():
        path = freeze_dir / snapshot
        if not path.is_file():
            raise FileNotFoundError(path)
        if sha256_file(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise ValueError("snapshot hash/size mismatch: {}".format(snapshot))
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise ValueError("non-empty WAL is not safe for immutable sampling: {}".format(wal))
        with connect_snapshot(path) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite quick_check failed: {}".format(snapshot))
    return manifest


def parse_data(row: sqlite3.Row, column: str) -> dict[str, Any]:
    payload = json.loads(row[column])
    if not isinstance(payload, dict):
        raise ValueError("{} payload is not an object".format(column))
    return payload


def candidate_entity_key(candidate: dict[str, Any], city: str) -> str:
    # Sampling and deduplication use only public candidate fields.  Historical
    # POI matches and review states are intentionally never consulted.
    return "fields:{}|{}|{}|{}".format(
        normalize(candidate.get("name")),
        normalize(candidate.get("address")),
        normalize(city),
        normalize(candidate.get("category")),
    )


def source_key(source: dict[str, Any]) -> str:
    # The public source identity is platform + external_id.  The internal
    # source_content UUID is retained for later lookup but is not a dedup key.
    return "source:{}|{}".format(
        normalize(source.get("platform")),
        normalize(source.get("external_id")),
    )


def candidate_fields(candidate: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "name",
        "address",
        "city",
        "country",
        "category",
        "latitude",
        "longitude",
        "website",
        "opening_hours",
    )
    return {key: candidate.get(key) for key in allowed}


def source_fields(source: dict[str, Any]) -> dict[str, Any]:
    # Raw metadata and author identifiers are intentionally excluded.  The
    # queue remains private, but it should contain only information needed for
    # manual location screening.
    return {
        "platform": source.get("platform"),
        "external_id": source.get("external_id"),
        "source_url": source.get("source_url") or source.get("canonical_url"),
        "content_type": source.get("content_type"),
        "content_status": source.get("content_status"),
        "collected_at": source.get("collected_at"),
        "published_at": source.get("published_at"),
        "title": str(source.get("title") or "")[:1000],
        "text": str(source.get("text") or "")[:4000],
    }


def evidence_fields(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": evidence.get("id"),
        "source_content_id": evidence.get("source_content_id"),
        "type": evidence.get("type"),
        "raw_value": evidence.get("raw_value"),
        "confidence": evidence.get("confidence"),
        "created_at": evidence.get("created_at"),
    }


def load_candidates(
    path: Path,
    city: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    connection = connect_snapshot(path)
    before = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    try:
        candidate_rows = connection.execute(
            """
            SELECT c.id AS candidate_id,c.source_content_id,c.city,c.data AS candidate_data,
                   s.data AS source_data
            FROM poi_candidates c JOIN source_contents s ON s.id=c.source_content_id
            WHERE c.city=? ORDER BY c.id
            """,
            (city,),
        ).fetchall()
        evidence_rows = connection.execute(
            """
            SELECT e.poi_candidate_id,e.data AS evidence_data
            FROM location_evidence e
            JOIN poi_candidates c ON c.id=e.poi_candidate_id
            WHERE c.city=? ORDER BY e.poi_candidate_id,e.id
            """,
            (city,),
        ).fetchall()
    finally:
        connection.rollback()
        connection.close()
    after = (sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns)
    if before != after:
        raise RuntimeError("snapshot changed during read: {}".format(path.name))

    evidence_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence_rows:
        evidence_by_candidate[row["poi_candidate_id"]].append(parse_data(row, "evidence_data"))
    records: list[dict[str, Any]] = []
    excluded_mock = 0
    excluded_xhs = 0
    excluded_missing_source = 0
    for row in candidate_rows:
        candidate = parse_data(row, "candidate_data")
        source = parse_data(row, "source_data")
        platform = str(source.get("platform") or "").casefold()
        source_kind = str(candidate.get("source_kind") or source.get("source_kind") or "").casefold()
        if platform == "xiaohongshu-browser" or source_kind == "xhs-verification":
            excluded_xhs += 1
            continue
        if platform.startswith("mock") or "mock" in source_kind:
            excluded_mock += 1
            continue
        if not row["source_content_id"] or not source.get("external_id"):
            excluded_missing_source += 1
            continue
        entity_key = candidate_entity_key(candidate, city)
        records.append(
            {
                "candidate_id": row["candidate_id"],
                "source_content_id": row["source_content_id"],
                "city": city,
                "candidate": candidate,
                "source": source,
                "evidence": evidence_by_candidate.get(row["candidate_id"], []),
                "entity_key": entity_key,
                "source_key": source_key(source),
                "platform": str(source.get("platform") or "<missing>"),
            }
        )
    return records, {
        "candidate_rows_in_snapshot": len(candidate_rows),
        "evidence_rows_in_snapshot": len(evidence_rows),
        "excluded_mock_rows": excluded_mock,
        "excluded_xhs_rows": excluded_xhs,
        "excluded_missing_source_rows": excluded_missing_source,
        "snapshot_sha256": before[0],
        "snapshot_bytes": before[1],
    }


def deterministic_sort_key(seed: int, city: str, candidate_id: str) -> str:
    return sha256_bytes("{}|{}|{}".format(seed, city, candidate_id).encode("utf-8"))


def select_records(records: list[dict[str, Any]], quota: int, seed: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    ordered = sorted(records, key=lambda row: deterministic_sort_key(seed, row["city"], row["candidate_id"]))
    platforms = sorted({row["platform"] for row in ordered})
    selected: list[dict[str, Any]] = []
    used_entities: set[str] = set()
    used_sources: set[str] = set()
    seen_duplicate_entity = 0
    seen_duplicate_source = 0

    # Keep a small deterministic presence from every available non-mock source
    # platform. This is a sampling guard, not a difficulty or gold quota.
    platform_floor = min(10, max(1, quota // max(1, len(platforms))))
    for platform in platforms:
        for row in ordered:
            if len(selected) >= quota:
                break
            if row["platform"] != platform:
                continue
            if row["entity_key"] in used_entities:
                seen_duplicate_entity += 1
                continue
            if row["source_key"] in used_sources:
                seen_duplicate_source += 1
                continue
            selected.append(row)
            used_entities.add(row["entity_key"])
            used_sources.add(row["source_key"])
            if sum(1 for item in selected if item["platform"] == platform) >= platform_floor:
                break

    for row in ordered:
        if len(selected) >= quota:
            break
        if row["entity_key"] in used_entities:
            seen_duplicate_entity += 1
            continue
        if row["source_key"] in used_sources:
            seen_duplicate_source += 1
            continue
        selected.append(row)
        used_entities.add(row["entity_key"])
        used_sources.add(row["source_key"])

    if len(selected) != quota:
        raise ValueError("city {} has only {} unique entity/source rows; quota is {}".format(
            records[0]["city"] if records else "<empty>", len(selected), quota
        ))
    return selected, {
        "eligible_rows": len(records),
        "unique_entity_count": len({row["entity_key"] for row in records}),
        "unique_source_count": len({row["source_key"] for row in records}),
        "dedup_entity_rejections_seen": seen_duplicate_entity,
        "dedup_source_rejections_seen": seen_duplicate_source,
    }


def private_row(record: dict[str, Any], screening_index: int, seed: int, snapshot: str) -> dict[str, Any]:
    entity_digest = opaque_digest("entity:", record["entity_key"])
    source_digest = opaque_digest("source:", record["source_key"])
    return {
        "schema_version": "lv-screening-v1",
        "screening_id": "screen-{}-{:04d}".format(record["city"].casefold().replace(" ", "-"), screening_index),
        "sampling_seed": seed,
        "city": record["city"],
        "candidate": {
            "candidate_id": record["candidate_id"],
            "source_content_id": record["source_content_id"],
            "entity_group_digest": entity_digest,
            "source_group_digest": source_digest,
            **candidate_fields(record["candidate"]),
        },
        "source": source_fields(record["source"]),
        "evidence": [evidence_fields(item) for item in record["evidence"]],
        "private_snapshot": snapshot,
    }


def find_forbidden_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_KEYS:
                found.append(path + "." + key)
            found.extend(find_forbidden_keys(item, path + "." + key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_forbidden_keys(item, "{}[{}]".format(path, index)))
    return found


def validate_queue(rows: list[dict[str, Any]], quotas: dict[str, int]) -> dict[str, Any]:
    if sum(quotas.values()) != len(rows):
        raise ValueError("queue count does not match quotas")
    candidate_ids = [row["candidate"]["candidate_id"] for row in rows]
    source_ids = [row["candidate"]["source_content_id"] for row in rows]
    entity_digests = [row["candidate"]["entity_group_digest"] for row in rows]
    source_digests = [row["candidate"]["source_group_digest"] for row in rows]
    for values, label in ((candidate_ids, "candidate_id"), (source_ids, "source_content_id"),
                          (entity_digests, "entity_group_digest"), (source_digests, "source_group_digest")):
        if len(values) != len(set(values)):
            raise ValueError("duplicate {} in screening queue".format(label))
    forbidden = []
    for row in rows:
        forbidden.extend(find_forbidden_keys(row))
        source = row["source"]
        platform = str(source.get("platform") or "").casefold()
        if platform.startswith("mock"):
            raise ValueError("mock source in queue")
        if platform == "xiaohongshu-browser":
            raise ValueError("xiaohongshu-browser source in queue")
        if not row["candidate"]["candidate_id"] or not row["candidate"]["source_content_id"]:
            raise ValueError("missing stable candidate/source ID")
    if forbidden:
        raise ValueError("label leakage keys in queue: {}".format(",".join(forbidden[:5])))
    city_counts = Counter(row["city"] for row in rows)
    if dict(city_counts) != quotas:
        raise ValueError("city quotas mismatch: {}".format(dict(city_counts)))
    return {
        "rows": len(rows),
        "city_counts": dict(sorted(city_counts.items())),
        "unique_candidate_ids": len(set(candidate_ids)),
        "unique_source_content_ids": len(set(source_ids)),
        "unique_entity_group_digests": len(set(entity_digests)),
        "unique_source_group_digests": len(set(source_digests)),
    }


def write_queue(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ``mkdir`` does not tighten an existing directory, so enforce the
    # private screening boundary on every run as well.
    path.parent.chmod(0o700)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)
    return sha256_file(path)


def distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    platforms = Counter(row["source"].get("platform") or "<missing>" for row in rows)
    categories = Counter(row["candidate"].get("category") or "<missing>" for row in rows)
    evidence_counts = Counter()
    evidence_types = Counter()
    collected = []
    coordinates = 0
    for row in rows:
        evidence_counts[len(row["evidence"])] += 1
        evidence_types.update(str(item.get("type") or "<missing>") for item in row["evidence"])
        timestamp = row["source"].get("collected_at")
        if timestamp:
            collected.append(timestamp)
        if row["candidate"].get("latitude") is not None and row["candidate"].get("longitude") is not None:
            coordinates += 1
    return {
        "city": dict(sorted(Counter(row["city"] for row in rows).items())),
        "platform": dict(sorted(platforms.items())),
        "category_top20": dict(sorted(categories.items(), key=lambda item: (-item[1], item[0]))[:20]),
        "evidence_count_distribution": {str(key): value for key, value in sorted(evidence_counts.items())},
        "evidence_type_counts": dict(sorted(evidence_types.items())),
        "rows_with_coordinates": coordinates,
        "collected_at_min": min(collected) if collected else None,
        "collected_at_max": max(collected) if collected else None,
    }


def public_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Location Verification 未标注候选筛查抽样",
        "",
        "本报告只公开计数、分布、筛选规则和哈希。原始地点字段、来源文本、稳定 ID 和证据摘要只保存在冻结目录下的 `.local/.../screening/`。该队列不是 500 条 Gold，也没有标记 dev/test split，不保证困难案件配额。公共导入里的 `AUTO_APPROVED` 只是既有流水线状态，不等于独立 Gold。",
        "",
        "- 生成时间：`{}`".format(report["generated_at_utc"]),
        "- 固定种子：`{}`".format(report["sampling_seed"]),
        "- 队列总量：`{}`".format(report["queue_validation"]["rows"]),
        "- 队列 SHA-256：`{}`".format(report["queue_sha256"]),
        "- 脚本 SHA-256：`{}`".format(report["script_sha256"]),
        "- 冻结清单 SHA-256：`{}`".format(report["freeze_manifest_sha256"]),
        "",
        "## 抽样分布",
        "",
        "```json",
        json.dumps(report["selected_distribution"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 来源池和去重",
        "",
        "| 城市 | 快照 | 输入候选行 | 排除 mock | 排除 XHS/历史核验 | 排除缺少来源 | 去重后实体数 | 去重后来源数 | 抽取数 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for city in ("New York", "Hanoi"):
        row = report["source_pool"][city]
        lines.append(
            "| {} | `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                city,
                row["snapshot"],
                row["candidate_rows_in_snapshot"],
                row["excluded_mock_rows"],
                row["excluded_xhs_rows"],
                row["excluded_missing_source_rows"],
                row["unique_entity_count"],
                row["unique_source_count"],
                row["selected_rows"],
            )
        )
    lines.extend(
        [
            "",
            "## 固定规则",
            "",
            "1. 只读取 pre-agent freeze 中的 `land-data.db`（Hanoi）和 `nyc-incremental-acceptance-v2.db`（New York），按 manifest SHA-256 校验；不读取 live DB。",
            "2. 固定种子 `20261028`，目标配额为 New York 800、Hanoi 200，共 1000 条；不创建或写入 split 标签。",
            "3. 排除 mock 平台、整个 `xiaohongshu-browser` 平台、`source_kind=xhs-verification` 及历史 XHS 核验导入，另外排除缺少稳定公开 source ID 的行。",
            "4. 只用公开来源 ID（platform + external_id）和规范化名称/地址/城市/类别去重；不同地址的分店保留为不同实体。抽样不读取或使用历史 POI 匹配、review 状态或坐标验证元数据。",
            "5. 每个可用来源平台最多先保留 10 条作为覆盖 guard，再按哈希排序填满城市配额；这不是困难案件或 Gold 配额。",
            "6. 队列不携带历史 review_status、review_reasons、resolved/matched POI、坐标验证元数据、Gold 或标注信息；来源文本和证据摘要仅在 `.local` 私有队列中。",
            "",
            "公共导入中的 `AUTO_APPROVED` 只是既有流水线状态，不等于独立 Gold。队列仍需逐案资格审查、独立标注和外部证据，不能直接形成锁定集。",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-dir", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--private-dir", type=Path, default=DEFAULT_PRIVATE_DIR)
    parser.add_argument("--public-dir", type=Path, default=DEFAULT_PUBLIC_DIR)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--nyc-count", type=int, default=DEFAULT_NYC_COUNT)
    parser.add_argument("--hanoi-count", type=int, default=DEFAULT_HANOI_COUNT)
    args = parser.parse_args(argv or sys.argv[1:])
    if args.nyc_count <= 0 or args.hanoi_count <= 0:
        parser.error("city quotas must be positive")
    freeze_dir = args.freeze_dir.resolve()
    private_dir = args.private_dir.resolve()
    public_dir = args.public_dir.resolve()
    manifest = load_manifest(freeze_dir)
    manifest_path = freeze_dir / "manifest.json"

    source_specs = {
        "New York": (freeze_dir / "nyc-incremental-acceptance-v2.db", "nyc-incremental-acceptance-v2.db"),
        "Hanoi": (freeze_dir / "land-data.db", "land-data.db"),
    }
    quotas = {"New York": args.nyc_count, "Hanoi": args.hanoi_count}
    all_selected: list[dict[str, Any]] = []
    source_pool: dict[str, Any] = {}
    for city in ("New York", "Hanoi"):
        path, snapshot_name = source_specs[city]
        records, inventory = load_candidates(path, city)
        selected, dedup = select_records(records, quotas[city], args.seed)
        source_pool[city] = {
            "snapshot": snapshot_name,
            **inventory,
            **dedup,
            "selected_rows": len(selected),
        }
        all_selected.extend(
            private_row(row, index + 1, args.seed, snapshot_name)
            for index, row in enumerate(selected)
        )

    # Queue order is fixed by city then the deterministic selection order. It
    # contains no split/dev/test marker.
    queue_validation = validate_queue(all_selected, quotas)
    queue_path = private_dir / QUEUE_NAME
    queue_sha256 = write_queue(queue_path, all_selected)
    private_manifest = {
        "schema_version": "lv-screening-manifest-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sampling_seed": args.seed,
        "queue_path": queue_path.name,
        "queue_sha256": queue_sha256,
        "queue_rows": queue_validation["rows"],
        "quotas": quotas,
        "source_snapshots": {city: source_pool[city]["snapshot_sha256"] for city in quotas},
        "source_pool": source_pool,
        "queue_validation": queue_validation,
        "privacy": "raw source title/text and evidence summaries are private under .local; no raw content is in public report",
    }
    private_dir.mkdir(parents=True, exist_ok=True)
    private_dir.chmod(0o700)
    private_manifest_path = private_dir / PRIVATE_MANIFEST_NAME
    private_manifest_path.write_text(json.dumps(private_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    private_manifest_path.chmod(0o600)

    script_path = Path(__file__).resolve()
    report = {
        "schema_version": "lv-sampling-report-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sampling_seed": args.seed,
        "quotas": quotas,
        "queue_sha256": queue_sha256,
        "queue_rows": queue_validation["rows"],
        "script_sha256": sha256_file(script_path),
        "freeze_manifest_sha256": sha256_file(manifest_path),
        "source_snapshots": {
            city: {"snapshot": source_pool[city]["snapshot"], "sha256": source_pool[city]["snapshot_sha256"]}
            for city in quotas
        },
        "source_pool": {
            city: {
                "snapshot": source_pool[city]["snapshot"],
                "candidate_rows_in_snapshot": source_pool[city]["candidate_rows_in_snapshot"],
                "excluded_mock_rows": source_pool[city]["excluded_mock_rows"],
                "excluded_xhs_rows": source_pool[city]["excluded_xhs_rows"],
                "excluded_missing_source_rows": source_pool[city]["excluded_missing_source_rows"],
                "unique_entity_count": source_pool[city]["unique_entity_count"],
                "unique_source_count": source_pool[city]["unique_source_count"],
                "selected_rows": source_pool[city]["selected_rows"],
            }
            for city in quotas
        },
        "queue_validation": queue_validation,
        "selected_distribution": distribution(all_selected),
        "is_gold": False,
        "split_labels_present": False,
        "difficulty_quotas_guaranteed": False,
        "status_boundary": (
            "AUTO_APPROVED in the source import is an existing pipeline status, not independent Gold; "
            "each queue row still requires case qualification, independent annotation, and external evidence "
            "before any locked-test consideration"
        ),
    }
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / PUBLIC_JSON_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (public_dir / PUBLIC_MD_NAME).write_text(public_markdown(report), encoding="utf-8")
    print(json.dumps({
        "queue": str(queue_path),
        "private_manifest": str(private_manifest_path),
        "public_json": str(public_dir / PUBLIC_JSON_NAME),
        "public_markdown": str(public_dir / PUBLIC_MD_NAME),
        "queue_sha256": queue_sha256,
        "rows": queue_validation["rows"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
