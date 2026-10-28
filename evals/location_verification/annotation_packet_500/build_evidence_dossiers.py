"""Build private, unlabelled evidence packets for the frozen hard-case shortlist."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "evals/location_verification/land_data_output_cross_source"))
from land_data_output_cross_source import (  # noqa: E402
    DEFAULT_CAPTURE_MANIFEST, DEFAULT_CURRENT, DEFAULT_DB, DEFAULT_FREEZE_MANIFEST,
    DEFAULT_HISTORICAL, build_current_index, build_historical_index,
    compare_current_case, compare_historical_case, load_and_validate_freeze_manifest,
    load_capture_manifest, load_dohmh_outputs, load_snapshot, open_immutable_database,
)


HERE = Path(__file__).resolve().parent
SHORTLIST = ROOT / ".local/location-verification-live-check/annotation_packet_500/hard-case-shortlist-v2.jsonl"
SHORTLIST_SUMMARY = HERE / "hard-shortlist-summary-v2.json"
TEMPORAL = ROOT / ".local/location-verification-live-check/temporal_results/temporal-cases-integrity-v2.jsonl"
PRIVATE = ROOT / ".local/location-verification-live-check/annotation_packet_500"
OUTPUT = PRIVATE / "hard-evidence-dossiers-v3.jsonl"
PUBLIC = HERE / "hard-evidence-dossier-summary-v3.json"
TEMPORAL_SHA = "a00d856205f79afae701815cbd391cf2a90adb707cffbae6c9b69139bf2ae8ba"
FORBIDDEN = {"gold", "gold_label", "gold_action", "review_status", "review_reasons",
             "resolved_poi_id", "matched_poi_id", "coordinate_verified", "adjudication_note",
             "selection_signal", "risk_signals"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def row_hash(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def strip_legacy_answers(value):
    if isinstance(value, dict):
        return {key: strip_legacy_answers(item) for key, item in value.items() if key not in FORBIDDEN}
    if isinstance(value, list):
        return [strip_legacy_answers(item) for item in value]
    return value


def has_forbidden(value) -> bool:
    if isinstance(value, dict):
        return bool(FORBIDDEN.intersection(value)) or any(has_forbidden(item) for item in value.values())
    if isinstance(value, list):
        return any(has_forbidden(item) for item in value)
    return False


def source_record(group: str, path: Path, snapshot_sha: str, locator: str | int, row: dict,
                  timestamp: str | None, timestamp_basis: str, resource_url: str | None) -> dict:
    return {"source_group": group, "lineage_key": group.split("_")[0],
            "snapshot_sha256": snapshot_sha, "snapshot_path": str(path),
            "row_locator": locator, "row_sha256": row_hash(row),
            "timestamp": timestamp, "timestamp_basis": timestamp_basis,
            "resource_url": resource_url, "raw_record": row}


def validate_dossiers(shortlist: list[dict], dossiers: list[dict]) -> None:
    expected = {item["candidate_id"] for item in shortlist}
    actual = [item["case_id"] for item in dossiers]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("dossiers do not cover the shortlist exactly once")
    for packet in dossiers:
        if set(packet) != {"schema_version", "case_id", "as_of", "candidate_claims", "source_records"}:
            raise ValueError("dossier contains unexpected or answer-bearing fields")
        if not packet["source_records"] or packet["source_records"][0]["source_group"] != "DOHMH_FROZEN":
            raise ValueError("dossier lacks frozen source provenance")
        if any(not record.get("row_sha256") or not record.get("snapshot_sha256")
               or not record.get("lineage_key") for record in packet["source_records"]):
            raise ValueError("dossier source provenance is incomplete")
        if has_forbidden(packet):
            raise ValueError("dossier contains leaked legacy answer fields")


def run() -> dict:
    shortlist_summary = json.loads(SHORTLIST_SUMMARY.read_text())
    if sha(SHORTLIST) != shortlist_summary["shortlist_sha256"] or sha(TEMPORAL) != TEMPORAL_SHA:
        raise ValueError("shortlist or temporal source changed")
    shortlist = [json.loads(line) for line in SHORTLIST.read_text().splitlines() if line]
    freeze = load_and_validate_freeze_manifest(DEFAULT_FREEZE_MANIFEST, DEFAULT_DB)
    current, current_info = load_snapshot(DEFAULT_CURRENT, "current")
    historical, historical_info = load_snapshot(DEFAULT_HISTORICAL, "historical")
    capture = load_capture_manifest(DEFAULT_CAPTURE_MANIFEST, current_info, historical_info)
    current_index, _ = build_current_index(current)
    historical_index, _ = build_historical_index(historical)
    latest = {item["camis"]: item for item in (json.loads(line) for line in TEMPORAL.read_text().splitlines() if line)}
    db = open_immutable_database(DEFAULT_DB)
    try:
        outputs, _ = load_dohmh_outputs(db)
        by_id = {item["candidate_id"]: item for item in outputs}
        source_ids = [by_id[item["candidate_id"]]["source_content_id"] for item in shortlist]
        source_data = {}
        for source_id in source_ids:
            raw = db.execute("SELECT data FROM source_contents WHERE id=?", (source_id,)).fetchone()
            if raw is None:
                raise ValueError("missing frozen DOHMH source content")
            source_data[source_id] = json.loads(raw[0])
    finally:
        db.close()
    dossiers = []
    for item in shortlist:
        output = by_id[item["candidate_id"]]
        source = source_data[output["source_content_id"]]
        candidate = {"name": output["name"], "address": output["address"],
                     "latitude": output["latitude"], "longitude": output["longitude"],
                     "city": output["candidate_city"], "external_id": output["external_id"]}
        query = {"screening_id": item["candidate_id"], "platform": output["platform"], **candidate}
        now = compare_current_case(query, current_index)
        old = compare_historical_case(query, historical_index)
        frozen_row = strip_legacy_answers({key: source.get(key) for key in
                                           ("source_url", "canonical_url", "collected_at", "published_at",
                                            "raw_record_id", "title", "text", "raw_metadata")})
        records = [source_record("DOHMH_FROZEN", DEFAULT_DB, freeze["database"]["sha256"],
                                 output["source_content_id"], frozen_row,
                                 source.get("collected_at"), "source_collected_at", source.get("source_url"))]
        if now.get("dot_row"):
            index = now["dot_row"]["record_index"]
            records.append(source_record("DOT_CURRENT", DEFAULT_CURRENT, current_info["sha256"], index,
                                         current[index], capture["datasets"]["current"]["capture_file_mtime"],
                                         "snapshot_file_mtime",
                                         capture["datasets"]["current"]["resource_url"]))
        for compact in old.get("dot_rows", []):
            index = compact["record_index"]
            records.append(source_record("DOT_HISTORICAL", DEFAULT_HISTORICAL, historical_info["sha256"],
                                         index, historical[index],
                                         capture["datasets"]["historical"]["capture_file_mtime"],
                                         "snapshot_file_mtime",
                                         capture["datasets"]["historical"]["resource_url"]))
        temporal = latest.get(output["external_id"])
        if temporal and temporal.get("official"):
            records.append(source_record("DOHMH_LATEST", TEMPORAL, TEMPORAL_SHA, output["external_id"],
                                         temporal["official"], temporal.get("official", {}).get("record_date"),
                                         "official_record_date_not_capture_time",
                                         temporal.get("frozen", {}).get("source_url")))
        dossiers.append({"schema_version": "lv-evidence-dossier-v3", "case_id": item["candidate_id"],
                         "as_of": "2026-10-28T23:59:59Z", "candidate_claims": candidate,
                         "source_records": records})
    validate_dossiers(shortlist, dossiers)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    PRIVATE.chmod(0o700)
    rendered = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                       for row in dossiers).encode("utf-8")
    if OUTPUT.exists() or OUTPUT.is_symlink():
        if OUTPUT.is_symlink() or OUTPUT.read_bytes() != rendered:
            raise ValueError("existing evidence dossiers differ from replay")
    else:
        OUTPUT.write_bytes(rendered)
        OUTPUT.chmod(0o600)
    summary = {"scope": "Unlabelled reviewer evidence; not Gold or Agent-visible test input",
               "case_count": len(dossiers),
               "source_record_counts": dict(sorted(Counter(record["source_group"] for packet in dossiers
                                                           for record in packet["source_records"]).items())),
               "input_hashes": {"shortlist": sha(SHORTLIST), "db": freeze["database"]["sha256"],
                                "current_dot": current_info["sha256"], "historical_dot": historical_info["sha256"],
                                "temporal": TEMPORAL_SHA, "capture_manifest": capture["sha256"]},
               "dossiers_sha256": hashlib.sha256(rendered).hexdigest(), "gold_labels": 0}
    PUBLIC.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
