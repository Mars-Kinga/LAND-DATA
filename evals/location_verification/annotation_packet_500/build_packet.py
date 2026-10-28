"""Prepare 500 blinded, unlabelled LAND-DATA cases for human screening."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT = ROOT / ".local/baselines/location-verification-pre-agent-final-2026-10-28/screening/location-verification-screening-2026-10-28.jsonl"
OUTPUT_DIR = ROOT / ".local/location-verification-live-check/annotation_packet_500"
OUTPUT = OUTPUT_DIR / "cases.jsonl"
REPORT = Path(__file__).resolve().parent / "packet-summary.json"
EXPECTED_INPUT_SHA = "9de97eeae5d4ea9badb5677e727faa6d4659887c913e8b394fdca66f112b1e86"
FORBIDDEN = {"review_status", "review_reasons", "resolved_poi_id", "matched_poi_id",
             "coordinate_verified", "gold", "gold_action", "adjudication_note"}
QUOTAS = {"New York": 400, "Hanoi": 100}
SEED = "land-data-annotation-intake-20261028-v1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def forbidden_paths(value, prefix="") -> list[str]:
    if isinstance(value, dict):
        return [found for key, item in value.items()
                for found in ([prefix + "." + key] if key in FORBIDDEN else [])
                + forbidden_paths(item, prefix + "." + key)]
    if isinstance(value, list):
        return [found for i, item in enumerate(value)
                for found in forbidden_paths(item, f"{prefix}[{i}]")]
    return []


def select(rows: list[dict]) -> list[dict]:
    if len(rows) != 1000 or any(forbidden_paths(row) for row in rows):
        raise ValueError("input count or contamination check failed")
    ids = [row["screening_id"] for row in rows]
    entities = [row["candidate"]["entity_group_digest"] for row in rows]
    if len(set(ids)) != len(ids) or len(set(entities)) != len(entities):
        raise ValueError("screening ids or entity groups are not unique")
    selected = []
    for city, quota in QUOTAS.items():
        pool = [row for row in rows if row["city"] == city]
        if len(pool) < quota:
            raise ValueError(f"not enough {city} cases")
        pool.sort(key=lambda row: hashlib.sha256(
            (SEED + ":" + row["candidate"]["entity_group_digest"]).encode()).hexdigest())
        selected.extend(pool[:quota])
    selected.sort(key=lambda row: row["screening_id"])
    return selected


def main() -> None:
    if sha(INPUT) != EXPECTED_INPUT_SHA:
        raise ValueError("frozen screening input hash changed")
    rows = [json.loads(line) for line in INPUT.read_text().splitlines() if line]
    chosen = select(rows)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.chmod(0o700)
    if OUTPUT.exists():
        raise FileExistsError("packet already exists; do not silently replace reviewer input")
    with OUTPUT.open("w") as stream:
        for row in chosen:
            packet = {"schema_version": "lv-human-intake-v1", "screening_id": row["screening_id"],
                      "city": row["city"], "candidate": row["candidate"],
                      "source": row["source"], "evidence": row["evidence"],
                      "annotation": {"eligible": None, "entity_group_confirmed": None,
                                     "primary_stratum": None, "claim_labels": None,
                                     "gold_action": None, "annotator": None,
                                     "independent_review": False}}
            stream.write(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    OUTPUT.chmod(0o600)
    summary = {"schema_version": "lv-human-intake-summary-v1", "input_sha256": EXPECTED_INPUT_SHA,
               "output_sha256": sha(OUTPUT), "count": len(chosen),
               "city_counts": dict(Counter(row["city"] for row in chosen)),
               "gold_labels": 0, "split_assigned": False,
               "note": "Unlabelled human-screening packet; not Gold or a locked evaluation set."}
    REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
