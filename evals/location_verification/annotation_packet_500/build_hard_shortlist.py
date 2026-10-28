"""Build an unlabelled hard-case shortlist without overlapping daily intake."""

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "evals/location_verification/land_data_output_cross_source"))
from land_data_output_cross_source import (  # noqa: E402
    DEFAULT_DB, DEFAULT_FREEZE_MANIFEST, DEFAULT_CURRENT, DEFAULT_HISTORICAL,
    build_current_index, build_historical_index, compare_current_case,
    compare_historical_case, load_and_validate_freeze_manifest,
    load_dohmh_outputs, load_snapshot, normalize_text, open_immutable_database,
)


DAILY = ROOT / ".local/location-verification-live-check/annotation_packet_500/cases.jsonl"
DAILY_SUMMARY = Path(__file__).resolve().parent / "packet-summary.json"
TEMPORAL = ROOT / ".local/location-verification-live-check/temporal_results/temporal-cases-integrity-v2.jsonl"
TEMPORAL_SHA = "a00d856205f79afae701815cbd391cf2a90adb707cffbae6c9b69139bf2ae8ba"
PRIVATE_DIR = ROOT / ".local/location-verification-live-check/annotation_packet_500"
PRIVATE_OUTPUT = PRIVATE_DIR / "hard-case-shortlist-v2.jsonl"
PUBLIC = Path(__file__).resolve().parent / "hard-shortlist-summary-v2.json"
SEED = "land-data-hard-shortlist-20261028-v2"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entity_key(name, address, city):
    return (normalize_text(name), normalize_text(address), normalize_text(city))


def rank(item):
    return hashlib.sha256((SEED + ":" + item["candidate_id"]).encode()).hexdigest()


def validate_shortlist(daily: list[dict], selected: list[dict]) -> None:
    daily_entities = {entity_key(item["candidate"]["name"], item["candidate"]["address"], item["city"])
                      for item in daily}
    ids = [item["candidate_id"] for item in selected]
    entities = [entity_key(item["name"], item["address"], item["city"]) for item in selected]
    if len(ids) != len(set(ids)) or len(entities) != len(set(entities)):
        raise ValueError("duplicate shortlist candidate or entity")
    if daily_entities.intersection(entities):
        raise ValueError("hard shortlist overlaps daily packet")
    if any(item.get("gold_label") is not None or item.get("eligibility_checked") is not False
           or not item.get("risk_signals") or item.get("selection_signal") not in {
               "address_coordinate_conflict_current", "address_coordinate_conflict_historical",
               "moved_or_closed_screening_only", "same_name_branch_screening_only",
               "stale_or_conflicting_source_screening_only"} for item in selected):
        raise ValueError("shortlist contains labels or lacks a registered risk signal")


def run() -> dict:
    daily_info = json.loads(DAILY_SUMMARY.read_text())
    if sha(DAILY) != daily_info["output_sha256"] or sha(TEMPORAL) != TEMPORAL_SHA:
        raise ValueError("daily packet or temporal snapshot changed")
    daily = [json.loads(line) for line in DAILY.read_text().splitlines() if line]
    excluded = {entity_key(item["candidate"]["name"], item["candidate"]["address"], item["city"])
                for item in daily}
    freeze = load_and_validate_freeze_manifest(DEFAULT_FREEZE_MANIFEST, DEFAULT_DB)
    db = open_immutable_database(DEFAULT_DB)
    try:
        outputs, _ = load_dohmh_outputs(db)
    finally:
        db.close()
    current, current_info = load_snapshot(DEFAULT_CURRENT, "current")
    historical, historical_info = load_snapshot(DEFAULT_HISTORICAL, "historical")
    ci, _ = build_current_index(current)
    hi, _ = build_historical_index(historical)
    temporal_missing = {item["camis"] for item in (
        json.loads(line) for line in TEMPORAL.read_text().splitlines() if line
    ) if item["status"] == "missing_in_latest_official_snapshot"}
    name_addresses = defaultdict(set)
    for item in outputs:
        name_addresses[normalize_text(item["name"])].add(normalize_text(item["address"]))
    groups = defaultdict(list)
    for item in outputs:
        if entity_key(item["name"], item["address"], item["candidate_city"]) in excluded:
            continue
        row = {"screening_id": item["candidate_id"], "name": item["name"],
               "address": item["address"], "city": item["candidate_city"],
               "latitude": item["latitude"], "longitude": item["longitude"],
               "platform": item["platform"], "external_id": item["external_id"]}
        now = compare_current_case(row, ci)
        old = compare_historical_case(row, hi)
        risk = {"candidate_id": item["candidate_id"], "external_id": item["external_id"],
                "name": item["name"], "address": item["address"], "city": item["candidate_city"],
                "source_platform": item["platform"], "risk_signals": []}
        if now.get("coordinate_comparable") and now["coordinate_distance_m"] > 10:
            risk["risk_signals"].append("current_dot_coordinate_over_10m")
            groups["address_coordinate_conflict_current"].append(risk)
        if old.get("coordinate_comparable") and old["coordinate_distance_m"] > 10:
            risk["risk_signals"].append("historical_dot_coordinate_over_10m")
            groups["address_coordinate_conflict_historical"].append(risk)
        if item["external_id"] in temporal_missing:
            risk["risk_signals"].append("missing_in_latest_dohmh_snapshot")
            groups["moved_or_closed_screening_only"].append(risk)
        if len(name_addresses[normalize_text(item["name"])]) > 1:
            risk["risk_signals"].append("same_name_multiple_addresses")
            groups["same_name_branch_screening_only"].append(risk)
        if old.get("address_comparable") and old["address_equal"] is False:
            risk["risk_signals"].append("historical_dot_address_disagrees")
            groups["stale_or_conflicting_source_screening_only"].append(risk)
    selected = []
    chosen = set()
    chosen_entities = set()
    target = [
        ("address_coordinate_conflict_current", 20, "address_coordinate_conflict"),
        ("moved_or_closed_screening_only", 50, "moved_or_closed"),
        ("same_name_branch_screening_only", 50, "same_name_branch"),
        ("stale_or_conflicting_source_screening_only", 50, "stale_or_conflicting_source"),
        ("address_coordinate_conflict_historical", 30, "address_coordinate_conflict"),
    ]
    availability = {name: len({item["candidate_id"] for item in values}) for name, values in groups.items()}
    for signal, quota, stratum in target:
        for item in sorted(groups[signal], key=rank):
            key = entity_key(item["name"], item["address"], item["city"])
            if (item["candidate_id"] in chosen or key in chosen_entities or
                    len([x for x in selected if x["selection_signal"] == signal]) >= quota):
                continue
            chosen.add(item["candidate_id"])
            chosen_entities.add(key)
            selected.append({**item, "selection_signal": signal, "suggested_stratum": stratum,
                             "gold_label": None, "eligibility_checked": False})
    validate_shortlist(daily, selected)
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    PRIVATE_DIR.chmod(0o700)
    if PRIVATE_OUTPUT.exists():
        raise FileExistsError("hard shortlist already exists; do not replace it")
    with PRIVATE_OUTPUT.open("w") as stream:
        for item in selected:
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    PRIVATE_OUTPUT.chmod(0o600)
    summary = {
        "scope": "Unlabelled risk shortlist, not hard-case Gold or locked evaluation split",
        "input_hashes": {"db": freeze["database"]["sha256"], "daily_packet": sha(DAILY),
                         "temporal": sha(TEMPORAL), "current_dot": current_info["sha256"],
                         "historical_dot": historical_info["sha256"]},
        "available_signal_counts": dict(sorted(availability.items())),
        "selected_signal_counts": dict(sorted(Counter(item["selection_signal"] for item in selected).items())),
        "selected_stratum_counts": dict(sorted(Counter(item["suggested_stratum"] for item in selected).items())),
        "selected_total": len(selected), "shortlist_sha256": sha(PRIVATE_OUTPUT),
        "gold_labels": 0, "quota_note": "Four hard groups require 50 eligible Gold each; risk signals alone do not qualify cases.",
    }
    PUBLIC.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
