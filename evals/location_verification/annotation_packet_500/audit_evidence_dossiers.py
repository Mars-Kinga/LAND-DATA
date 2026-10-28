"""Independently audit the reviewer dossier against frozen source rows."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PRIVATE = ROOT / ".local/location-verification-live-check/annotation_packet_500"
DOSSIERS = PRIVATE / "hard-evidence-dossiers-v3.jsonl"
SUMMARY = HERE / "hard-evidence-dossier-summary-v3.json"
SHORTLIST = PRIVATE / "hard-case-shortlist-v2.jsonl"
DB = ROOT / ".local/baselines/location-verification-pre-agent-final-2026-10-28/nyc-incremental-acceptance-v2.db"
CURRENT = ROOT / ".local/location-verification-live-check/dot_batches/current.json"
HISTORICAL = ROOT / ".local/location-verification-live-check/dot_batches/historical.json"
TEMPORAL = ROOT / ".local/location-verification-live-check/temporal_results/temporal-cases-integrity-v2.jsonl"
FORBIDDEN = {"gold", "gold_label", "gold_action", "review_status", "review_reasons",
             "resolved_poi_id", "matched_poi_id", "coordinate_verified", "adjudication_note",
             "selection_signal", "risk_signals"}
SOURCE_KEYS = ("source_url", "canonical_url", "collected_at", "published_at", "raw_record_id",
               "title", "text", "raw_metadata")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def row_sha(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def scrub(value):
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items() if key not in FORBIDDEN}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def forbidden_paths(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN:
                yield prefix + "." + key
            yield from forbidden_paths(item, prefix + "." + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from forbidden_paths(item, f"{prefix}[{index}]")


def rows(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data if isinstance(data, list) else data.get("records", data.get("rows", data.get("data")))


def audit() -> dict:
    summary = json.loads(SUMMARY.read_text())
    if sha(DOSSIERS) != summary["dossiers_sha256"]:
        raise ValueError("dossier file hash differs from public summary")
    expected_hashes = summary["input_hashes"]
    for label, path in (("db", DB), ("current_dot", CURRENT), ("historical_dot", HISTORICAL),
                        ("temporal", TEMPORAL), ("shortlist", SHORTLIST)):
        if sha(path) != expected_hashes[label]:
            raise ValueError(f"{label} snapshot hash mismatch")
    dossiers = [json.loads(line) for line in DOSSIERS.read_text().splitlines() if line]
    shortlist = [json.loads(line) for line in SHORTLIST.read_text().splitlines() if line]
    if len(dossiers) != len(shortlist) or {x["case_id"] for x in dossiers} != {x["candidate_id"] for x in shortlist}:
        raise ValueError("dossier case coverage mismatch")
    leaks = list(forbidden_paths(dossiers))
    if leaks:
        raise ValueError(f"legacy answer field leakage: {leaks[:3]}")
    current, historical = rows(CURRENT), rows(HISTORICAL)
    latest = {item["camis"]: item for item in (json.loads(line) for line in TEMPORAL.read_text().splitlines() if line)}
    db = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        db.close()
        raise ValueError("frozen SQLite quick_check failed")
    counts = Counter()
    try:
        for packet in dossiers:
            if packet["schema_version"] != "lv-evidence-dossier-v3":
                raise ValueError("wrong dossier schema")
            for record in packet["source_records"]:
                group = record["source_group"]
                if row_sha(record["raw_record"]) != record["row_sha256"]:
                    raise ValueError("row payload hash mismatch")
                if group == "DOHMH_FROZEN":
                    if record["snapshot_sha256"] != expected_hashes["db"] or record["timestamp_basis"] != "source_collected_at":
                        raise ValueError("frozen source provenance mismatch")
                    found = db.execute("SELECT data FROM source_contents WHERE id=?",
                                       (record["row_locator"],)).fetchone()
                    if not found:
                        raise ValueError("frozen source locator missing")
                    source = json.loads(found[0])
                    expected = scrub({key: source.get(key) for key in SOURCE_KEYS})
                elif group in {"DOT_CURRENT", "DOT_HISTORICAL"}:
                    path_rows = current if group == "DOT_CURRENT" else historical
                    source_hash = expected_hashes["current_dot" if group == "DOT_CURRENT" else "historical_dot"]
                    if record["snapshot_sha256"] != source_hash or record["timestamp_basis"] != "snapshot_file_mtime":
                        raise ValueError("DOT provenance mismatch")
                    expected = path_rows[record["row_locator"]]
                elif group == "DOHMH_LATEST":
                    if record["snapshot_sha256"] != expected_hashes["temporal"] or record["timestamp_basis"] != "official_record_date_not_capture_time":
                        raise ValueError("latest source provenance mismatch")
                    expected = latest[record["row_locator"]]["official"]
                else:
                    raise ValueError("unknown source group")
                if record["raw_record"] != expected:
                    raise ValueError("dossier row differs from frozen source")
                counts[group] += 1
    finally:
        db.close()
    if dict(counts) != summary["source_record_counts"]:
        raise ValueError("source record counts mismatch")
    return {"status": "VERIFIED", "dossiers": len(dossiers), "source_records": dict(sorted(counts.items())),
            "forbidden_field_paths": 0, "dossiers_sha256": sha(DOSSIERS)}


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, sort_keys=True))
