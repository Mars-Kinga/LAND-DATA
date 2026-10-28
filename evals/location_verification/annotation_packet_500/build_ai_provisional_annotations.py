"""Conservative AI evidence triage; never emits independent human Gold."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "evals/location_verification/dot_cross_source"))
from dot_cross_source_check import get_field, haversine_meters, source_coordinate  # noqa: E402


PRIVATE = ROOT / ".local/location-verification-live-check/annotation_packet_500"
DAILY = PRIVATE / "cases.jsonl"
HARD = PRIVATE / "hard-evidence-dossiers-v3.jsonl"
SHORTLIST = PRIVATE / "hard-case-shortlist-v2.jsonl"
DAILY_SUMMARY = HERE / "packet-summary.json"
HARD_SUMMARY = HERE / "hard-evidence-dossier-summary-v3.json"
SHORTLIST_SUMMARY = HERE / "hard-shortlist-summary-v2.json"
OUTPUT = PRIVATE / "ai-provisional-evidence-annotations-v1.jsonl"
PUBLIC = HERE / "ai-provisional-evidence-annotations-summary.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def evidence_ref(record: dict) -> str:
    return f"{record['source_group']}:{record['snapshot_sha256']}:{record['row_locator']}:{record['row_sha256']}"


def hard_annotation(packet: dict, suggested_stratum: str) -> dict:
    records = packet["source_records"]
    current = next((record for record in records if record["source_group"] == "DOT_CURRENT"), None)
    frozen = next(record for record in records if record["source_group"] == "DOHMH_FROZEN")
    candidate = packet["candidate_claims"]
    candidate_lat, candidate_lon = candidate["latitude"], candidate["longitude"]
    coordinate = {"status": "UNKNOWN", "distance_m": None, "evidence_refs": [],
                  "reason": "No comparable current independent-source coordinate in frozen packet."}
    identity = {"status": "UNKNOWN", "evidence_refs": [evidence_ref(frozen)],
                "reason": "Candidate source alone cannot establish unique current venue identity."}
    address = {"status": "UNKNOWN", "evidence_refs": [evidence_ref(frozen)],
               "reason": "Current physical address is not independently established."}
    if current is not None:
        refs = [evidence_ref(frozen), evidence_ref(current)]
        dot_lat, dot_lon = source_coordinate(current["raw_record"])
        source_borough = str((frozen["raw_record"].get("raw_metadata") or {}).get("source_metadata", {}).get("borough") or "").strip().casefold()
        dot_borough = str(get_field(current["raw_record"], "borough") or "").strip().casefold()
        if source_borough and dot_borough and source_borough != dot_borough:
            identity = {"status": "CONFLICTING", "evidence_refs": refs,
                        "reason": "Same name and street text resolve to different boroughs; branch identity unresolved."}
        else:
            identity = {"status": "PLAUSIBLE_MATCH_NOT_CONFIRMED", "evidence_refs": refs,
                        "reason": "Unique normalized name-address retrieval, not independent physical-venue confirmation."}
        address = {"status": "CROSS_SOURCE_TEXT_AGREEMENT", "evidence_refs": refs,
                   "reason": "Current DOT row was retrieved by exact normalized name and address; this is selection-conditioned agreement."}
        if (candidate_lat is not None and candidate_lon is not None and dot_lat is not None and dot_lon is not None
                and not (candidate_lat == 0 and candidate_lon == 0) and not (dot_lat == 0 and dot_lon == 0)):
            distance = round(haversine_meters(candidate_lat, candidate_lon, dot_lat, dot_lon), 3)
            coordinate = {"status": "CROSS_SOURCE_CONFLICT" if distance > 10 else "CROSS_SOURCE_AGREEMENT",
                          "distance_m": distance, "evidence_refs": refs,
                          "reason": "Frozen DOHMH and current DOT coordinates differ by more than 10m." if distance > 10
                                    else "Frozen DOHMH and current DOT coordinates are within 10m; neither is independent Gold."}
    if identity["status"] == "CONFLICTING":
        next_action = "HUMAN_DISAMBIGUATION"
    elif coordinate["status"] == "CROSS_SOURCE_CONFLICT":
        next_action = "HUMAN_COORDINATE_REVIEW"
    else:
        next_action = "COLLECT_CURRENT_INDEPENDENT_EVIDENCE"
    return {
        "schema_version": "lv-ai-provisional-evidence-v1", "case_id": packet["case_id"],
        "cohort": "hard_shortlist", "suggested_stratum": suggested_stratum,
        "annotator": "Codex AI (single, unadjudicated)", "split": None, "gold_label": None,
        "eligible_for_accuracy": False, "source_dossier_sha256": sha(HARD),
        "claim_reviews": {"identity": identity, "address": address, "coordinates": coordinate,
                          "operational_status": {"status": "UNKNOWN", "evidence_refs": [],
                                                 "reason": "Neither restaurant record nor outdoor-dining permit proves current operation."}},
        "next_action": next_action,
        "limitation": "Evidence triage only; no independent human Gold, adjudication, or current-operation verification.",
    }


def daily_annotation(packet: dict) -> dict:
    candidate = packet["candidate"]
    missing = []
    if not str(candidate.get("name") or "").strip():
        missing.append("name")
    if not str(candidate.get("address") or "").strip():
        missing.append("address")
    lat, lon = candidate.get("latitude"), candidate.get("longitude")
    if lat is None or lon is None or (lat == 0 and lon == 0):
        missing.append("coordinates")
    source = packet["source"]
    return {
        "schema_version": "lv-ai-provisional-evidence-v1", "case_id": packet["screening_id"],
        "cohort": "daily_intake", "city": packet["city"],
        "annotator": "Codex AI (single, unadjudicated)", "split": None, "gold_label": None,
        "eligible_for_accuracy": False, "source_packet_sha256": sha(DAILY),
        "provided_source": {"platform": source.get("platform"), "source_url": source.get("source_url"),
                            "source_content_id": candidate.get("source_content_id"),
                            "evidence_ids": [item["evidence_id"] for item in packet["evidence"]]},
        "claim_reviews": {field: {"status": "UNKNOWN", "reason": "Only the intake source was reviewed; no independent factual adjudication."}
                          for field in ("identity", "address", "coordinates", "operational_status")},
        "missing_candidate_fields": missing,
        "next_action": "COLLECT_CURRENT_INDEPENDENT_EVIDENCE" if not missing else "REPAIR_INPUT_AND_COLLECT_EVIDENCE",
        "limitation": "Input-quality and evidence-sufficiency triage only; not a location-truth label.",
    }


def validate(daily: list[dict], hard: list[dict], labels: list[dict]) -> None:
    expected = {item["screening_id"] for item in daily} | {item["case_id"] for item in hard}
    ids = [item["case_id"] for item in labels]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValueError("annotation coverage or uniqueness mismatch")
    if any(item.get("gold_label") is not None or item.get("split") is not None
           or item.get("eligible_for_accuracy") is not False
           or not item.get("limitation") for item in labels):
        raise ValueError("provisional annotation contains Gold or split leakage")


def run() -> dict:
    if sha(DAILY) != json.loads(DAILY_SUMMARY.read_text())["output_sha256"]:
        raise ValueError("daily packet hash changed")
    if sha(HARD) != json.loads(HARD_SUMMARY.read_text())["dossiers_sha256"]:
        raise ValueError("hard dossier hash changed")
    if sha(SHORTLIST) != json.loads(SHORTLIST_SUMMARY.read_text())["shortlist_sha256"]:
        raise ValueError("shortlist hash changed")
    daily, hard, shortlist = read_jsonl(DAILY), read_jsonl(HARD), read_jsonl(SHORTLIST)
    strata = {item["candidate_id"]: item["suggested_stratum"] for item in shortlist}
    labels = [daily_annotation(item) for item in daily]
    labels.extend(hard_annotation(item, strata[item["case_id"]]) for item in hard)
    labels.sort(key=lambda item: (item["cohort"], item["case_id"]))
    validate(daily, hard, labels)
    rendered = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                       for item in labels).encode()
    if OUTPUT.exists() or OUTPUT.is_symlink():
        if OUTPUT.is_symlink() or OUTPUT.read_bytes() != rendered:
            raise ValueError("existing provisional annotations differ from frozen replay")
    else:
        OUTPUT.write_bytes(rendered)
        OUTPUT.chmod(0o600)
    summary = {
        "scope": "Single-AI provisional evidence triage, not human Gold or location accuracy",
        "annotation_count": len(labels), "daily_count": len(daily), "hard_count": len(hard),
        "input_hashes": {"daily": sha(DAILY), "hard_dossiers": sha(HARD), "shortlist": sha(SHORTLIST)},
        "hard_coordinate_status_counts": dict(sorted(Counter(item["claim_reviews"]["coordinates"]["status"]
                                                     for item in labels if item["cohort"] == "hard_shortlist").items())),
        "hard_identity_status_counts": dict(sorted(Counter(item["claim_reviews"]["identity"]["status"]
                                                   for item in labels if item["cohort"] == "hard_shortlist").items())),
        "daily_missing_field_counts": dict(sorted(Counter(field for item in labels if item["cohort"] == "daily_intake"
                                                         for field in item["missing_candidate_fields"]).items())),
        "next_action_counts": dict(sorted(Counter(item["next_action"] for item in labels).items())),
        "gold_labels": 0, "locked_split_assigned": False,
        "output_sha256": hashlib.sha256(rendered).hexdigest(),
    }
    PUBLIC.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
