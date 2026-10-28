"""Audit independent-source availability for the 500 unlabelled intake cases."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "evals/location_verification/dot_cross_source"))
from dot_cross_source_check import (  # noqa: E402
    build_current_index, build_historical_index, compare_current_case,
    compare_historical_case, load_capture_manifest, load_snapshot,
)


PACKET = ROOT / ".local/location-verification-live-check/annotation_packet_500/cases.jsonl"
PACKET_SUMMARY = Path(__file__).resolve().parent / "packet-summary.json"
CURRENT = ROOT / ".local/location-verification-live-check/dot_batches/current.json"
HISTORICAL = ROOT / ".local/location-verification-live-check/dot_batches/historical.json"
CAPTURE = ROOT / ".local/location-verification-live-check/dot_batches/capture-manifest.json"
OUTPUT = Path(__file__).resolve().parent / "evidence-coverage-summary.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run() -> dict:
    packet_info = json.loads(PACKET_SUMMARY.read_text())
    if sha(PACKET) != packet_info["output_sha256"]:
        raise ValueError("500-case packet hash changed")
    cases = [json.loads(line) for line in PACKET.read_text().splitlines() if line]
    current, current_info = load_snapshot(CURRENT, "current")
    historical, historical_info = load_snapshot(HISTORICAL, "historical")
    capture = load_capture_manifest(CAPTURE, current_info, historical_info)
    current_index, _ = build_current_index(current)
    historical_index, _ = build_historical_index(historical)
    counts = Counter()
    for case in cases:
        counts["total"] += 1
        counts["city:" + case["city"]] += 1
        source = case["source"]
        platform = source["platform"]
        counts["platform:" + platform] += 1
        row = {"screening_id": case["screening_id"],
               "name": case["candidate"].get("name"),
               "address": case["candidate"].get("address"),
               "latitude": case["candidate"].get("latitude"),
               "longitude": case["candidate"].get("longitude"),
               "city": case["city"], "platform": platform,
               "external_id": source.get("external_id")}
        if case["city"] != "New York" or platform != "nyc-open-data-restaurants":
            continue
        current_case = compare_current_case(row, current_index)
        counts["current:" + current_case["match_type"]] += 1
        if current_case.get("coordinate_comparable"):
            counts["current:coordinate_comparable"] += 1
            if current_case["coordinate_distance_m"] > 10:
                counts["current:coordinate_over_10m"] += 1
        old_case = compare_historical_case(row, historical_index)
        counts["historical:" + old_case["match_type"]] += 1
    result = {
        "scope": "Source-availability screening only; not Gold, location accuracy, or a locked-test split",
        "packet_sha256": sha(PACKET),
        "current_dot_sha256": current_info["sha256"],
        "historical_dot_sha256": historical_info["sha256"],
        "capture_manifest_sha256": capture["sha256"],
        "counts": dict(sorted(counts.items())),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
