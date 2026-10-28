"""Add frozen daily cross-source relationships to provisional, non-Gold labels."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from build_ai_provisional_annotations import evidence_ref, sha, validate


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "evals/location_verification/dot_cross_source"))
from dot_cross_source_check import haversine_meters, source_coordinate  # noqa: E402


PRIVATE = ROOT / ".local/location-verification-live-check/annotation_packet_500"
DAILY = PRIVATE / "cases.jsonl"
HARD = PRIVATE / "hard-evidence-dossiers-v3.jsonl"
DOSSIERS = PRIVATE / "daily-evidence-dossiers-v1.jsonl"
DOSSIER_SUMMARY = HERE / "daily-evidence-dossier-summary-v1.json"
V1 = PRIVATE / "ai-provisional-evidence-annotations-v1.jsonl"
V1_SUMMARY = HERE / "ai-provisional-evidence-annotations-summary.json"
OUTPUT = PRIVATE / "ai-provisional-evidence-annotations-v2.jsonl"
SUMMARY = HERE / "ai-provisional-evidence-annotations-summary-v2.json"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def enrich_daily(label: dict, dossier: dict) -> dict:
    label = dict(label)
    label["schema_version"] = "lv-ai-provisional-evidence-v2"
    label["daily_dossier_sha256"] = sha(DOSSIERS)
    records = dossier["source_records"]
    current = next((r for r in records if r["source_group"] == "DOT_CURRENT"), None)
    if current is None:
        return label
    refs = [evidence_ref(records[0]), evidence_ref(current)]
    claims = {key: dict(value) for key, value in label["claim_reviews"].items()}
    claims["identity"] = {"status": "PLAUSIBLE_MATCH_NOT_CONFIRMED", "evidence_refs": refs,
                          "reason": "Unique normalized name-address retrieval; physical venue identity remains unconfirmed."}
    claims["address"] = {"status": "CROSS_SOURCE_TEXT_AGREEMENT", "evidence_refs": refs,
                         "reason": "Exact normalized name and address were used for DOT retrieval; agreement is selection-conditioned."}
    lat, lon = dossier["candidate_claims"]["latitude"], dossier["candidate_claims"]["longitude"]
    dot_lat, dot_lon = source_coordinate(current["raw_record"])
    if (lat is not None and lon is not None and dot_lat is not None and dot_lon is not None
            and (lat, lon) != (0, 0) and (dot_lat, dot_lon) != (0, 0)):
        distance = round(haversine_meters(lat, lon, dot_lat, dot_lon), 3)
        claims["coordinates"] = {
            "status": "CROSS_SOURCE_CONFLICT" if distance > 10 else "CROSS_SOURCE_AGREEMENT",
            "distance_m": distance, "evidence_refs": refs,
            "reason": "Frozen intake and current DOT coordinates differ by more than 10m." if distance > 10
                      else "Frozen intake and current DOT coordinates are within 10m; neither is Gold."}
        if distance > 10:
            label["next_action"] = "HUMAN_COORDINATE_REVIEW"
    else:
        claims["coordinates"] = {"status": "UNKNOWN", "distance_m": None,
                                 "evidence_refs": refs,
                                 "reason": "No valid comparable coordinate; zero-zero is a missing sentinel."}
    label["claim_reviews"] = claims
    return label


def run() -> dict:
    dossier_sha = sha(DOSSIERS)
    if dossier_sha != json.loads(DOSSIER_SUMMARY.read_text())["dossiers_sha256"]:
        raise ValueError("daily evidence dossier hash changed")
    v1_sha = sha(V1)
    if v1_sha != json.loads(V1_SUMMARY.read_text())["output_sha256"]:
        raise ValueError("v1 provisional annotations changed")
    daily, hard, dossiers, v1 = (read_jsonl(p) for p in (DAILY, HARD, DOSSIERS, V1))
    by_case = {item["case_id"]: item for item in dossiers}
    if len(by_case) != len(daily) or set(by_case) != {item["screening_id"] for item in daily}:
        raise ValueError("daily dossier coverage mismatch")
    labels = []
    for item in v1:
        if item["cohort"] == "daily_intake":
            labels.append(enrich_daily(item, by_case[item["case_id"]]))
        else:
            labels.append({**item, "schema_version": "lv-ai-provisional-evidence-v2"})
    validate(daily, hard, labels)
    rendered = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                       for item in labels).encode()
    if OUTPUT.exists() or OUTPUT.is_symlink():
        if OUTPUT.is_symlink() or OUTPUT.read_bytes() != rendered:
            raise ValueError("existing v2 labels differ from frozen replay")
    else:
        OUTPUT.write_bytes(rendered)
        OUTPUT.chmod(0o600)
    result = {"scope": "Single-AI provisional source relationship labels; not location Gold",
              "annotation_count": len(labels), "gold_labels": 0, "locked_split_assigned": False,
              "input_hashes": {"v1_annotations": v1_sha, "daily_dossiers": dossier_sha},
              "daily_coordinate_status_counts": dict(sorted(Counter(
                  item["claim_reviews"]["coordinates"]["status"] for item in labels
                  if item["cohort"] == "daily_intake").items())),
              "daily_identity_status_counts": dict(sorted(Counter(
                  item["claim_reviews"]["identity"]["status"] for item in labels
                  if item["cohort"] == "daily_intake").items())),
              "next_action_counts": dict(sorted(Counter(item["next_action"] for item in labels).items())),
              "output_sha256": hashlib.sha256(rendered).hexdigest()}
    SUMMARY.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
