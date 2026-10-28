"""Freeze reviewer-only source rows for the 500 daily intake cases."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from build_evidence_dossiers import has_forbidden, source_record


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "evals/location_verification/dot_cross_source"))
from dot_cross_source_check import (  # noqa: E402
    build_current_index, build_historical_index, compare_current_case,
    compare_historical_case, load_capture_manifest, load_snapshot,
)


PRIVATE = ROOT / ".local/location-verification-live-check/annotation_packet_500"
PACKET = PRIVATE / "cases.jsonl"
CURRENT = ROOT / ".local/location-verification-live-check/dot_batches/current.json"
HISTORICAL = ROOT / ".local/location-verification-live-check/dot_batches/historical.json"
CAPTURE = ROOT / ".local/location-verification-live-check/dot_batches/capture-manifest.json"
OUTPUT = PRIVATE / "daily-evidence-dossiers-v1.jsonl"
SUMMARY = HERE / "daily-evidence-dossier-summary-v1.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(cases: list[dict], dossiers: list[dict]) -> None:
    expected = [case["screening_id"] for case in cases]
    actual = [item["case_id"] for item in dossiers]
    if len(expected) != 500 or len(set(expected)) != 500 or actual != expected:
        raise ValueError("daily dossier coverage or order changed")
    for item in dossiers:
        if has_forbidden(item):
            raise ValueError("daily dossier contains leaked answer")
        if set(item) != {"schema_version", "case_id", "candidate_claims", "source_records"}:
            raise ValueError("daily dossier has unexpected fields")
        records = item["source_records"]
        if not records or not records[0]["source_group"].endswith("_INTAKE"):
            raise ValueError("daily dossier lacks intake provenance")
        if any(not r["snapshot_sha256"] or not r["row_sha256"] or not r["lineage_key"]
               for r in records):
            raise ValueError("daily dossier has missing provenance")


def run() -> dict:
    packet_info = json.loads((HERE / "packet-summary.json").read_text())
    packet_sha = sha(PACKET)
    if packet_sha != packet_info["output_sha256"]:
        raise ValueError("500-case packet changed")
    cases = [json.loads(line) for line in PACKET.read_text().splitlines() if line]
    current, current_info = load_snapshot(CURRENT, "current")
    historical, historical_info = load_snapshot(HISTORICAL, "historical")
    capture = load_capture_manifest(CAPTURE, current_info, historical_info)
    current_index, _ = build_current_index(current)
    historical_index, _ = build_historical_index(historical)
    dossiers = []
    for case in cases:
        source = case["source"]
        platform = source["platform"]
        group = ("DOHMH" if platform == "nyc-open-data-restaurants"
                 else "OSM" if platform == "osm" else "OTHER") + "_INTAKE"
        base = {key: case[key] for key in ("city", "candidate", "source", "evidence")}
        records = [source_record(group, PACKET, packet_sha, case["screening_id"], base,
                                 source.get("collected_at"), "source_collected_at",
                                 source.get("source_url"))]
        if case["city"] == "New York" and platform == "nyc-open-data-restaurants":
            row = {"screening_id": case["screening_id"], "name": case["candidate"].get("name"),
                   "address": case["candidate"].get("address"),
                   "latitude": case["candidate"].get("latitude"),
                   "longitude": case["candidate"].get("longitude"),
                   "city": case["city"], "platform": platform,
                   "external_id": source.get("external_id")}
            now = compare_current_case(row, current_index)
            old = compare_historical_case(row, historical_index)
            if now.get("dot_row"):
                index = now["dot_row"]["record_index"]
                records.append(source_record(
                    "DOT_CURRENT", CURRENT, current_info["sha256"], index, current[index],
                    capture["datasets"]["current"]["capture_file_mtime"],
                    "snapshot_file_mtime", capture["datasets"]["current"]["resource_url"]))
            for compact in old.get("dot_rows", []):
                index = compact["record_index"]
                records.append(source_record(
                    "DOT_HISTORICAL", HISTORICAL, historical_info["sha256"], index,
                    historical[index],
                    capture["datasets"]["historical"]["capture_file_mtime"],
                    "snapshot_file_mtime", capture["datasets"]["historical"]["resource_url"]))
        dossiers.append({"schema_version": "lv-daily-evidence-dossier-v1",
                         "case_id": case["screening_id"],
                         "candidate_claims": {key: case["candidate"].get(key)
                                              for key in ("name", "address", "latitude", "longitude", "city")},
                         "source_records": records})
    validate(cases, dossiers)
    rendered = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                       for item in dossiers).encode()
    if OUTPUT.exists() or OUTPUT.is_symlink():
        if OUTPUT.is_symlink() or OUTPUT.read_bytes() != rendered:
            raise ValueError("existing daily dossiers differ from frozen replay")
    else:
        OUTPUT.write_bytes(rendered)
        OUTPUT.chmod(0o600)
    summary = {"scope": "Reviewer-only evidence; no Gold or location accuracy",
               "case_count": len(dossiers),
               "source_record_counts": dict(sorted(Counter(r["source_group"] for item in dossiers
                                                           for r in item["source_records"]).items())),
               "input_hashes": {"packet": packet_sha, "current_dot": current_info["sha256"],
                                "historical_dot": historical_info["sha256"],
                                "capture_manifest": capture["sha256"]},
               "dossiers_sha256": hashlib.sha256(rendered).hexdigest(), "gold_labels": 0}
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
