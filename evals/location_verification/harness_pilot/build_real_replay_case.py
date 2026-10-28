"""Prepare one known-risk LAND-DATA case for development-only CLI replay."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = ROOT / ".local/location-verification-live-check"
PACKETS = PRIVATE / "annotation_packet_500"
PILOT = PRIVATE / "harness_pilot"
DOSSIERS = PACKETS / "hard-evidence-dossiers-v3.jsonl"
ANNOTATIONS = PACKETS / "ai-provisional-evidence-annotations-v2.jsonl"
DOSSIER_SUMMARY = ROOT / "evals/location_verification/annotation_packet_500/hard-evidence-dossier-summary-v3.json"
ANNOTATION_SUMMARY = ROOT / "evals/location_verification/annotation_packet_500/ai-provisional-evidence-annotations-summary-v2.json"
MANIFEST = PILOT / "real-conflict-tools-v1.json"
CASE = PILOT / "real-conflict-case-v1.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_locked(path: Path, payload: dict) -> None:
    rendered = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != rendered:
            raise ValueError(f"existing pilot input differs from replay: {path.name}")
    else:
        path.write_bytes(rendered)
        path.chmod(0o600)


def run() -> dict:
    if sha(DOSSIERS) != json.loads(DOSSIER_SUMMARY.read_text())["dossiers_sha256"]:
        raise ValueError("hard dossier hash changed")
    if sha(ANNOTATIONS) != json.loads(ANNOTATION_SUMMARY.read_text())["output_sha256"]:
        raise ValueError("provisional annotations changed")
    dossiers = {x["case_id"]: x for x in (json.loads(line) for line in DOSSIERS.read_text().splitlines())}
    candidates = sorted(x["case_id"] for x in (json.loads(line) for line in ANNOTATIONS.read_text().splitlines())
                        if x["cohort"] == "hard_shortlist" and x["next_action"] == "HUMAN_COORDINATE_REVIEW")
    if not candidates:
        raise ValueError("no frozen coordinate-conflict development case")
    dossier = dossiers[candidates[0]]
    records = dossier["source_records"]
    dohmh = next(x for x in records if x["source_group"] == "DOHMH_FROZEN")
    dot = next(x for x in records if x["source_group"] == "DOT_CURRENT")
    candidate = dossier["candidate_claims"]
    lat, lon = candidate["latitude"], candidate["longitude"]
    if lat is None or lon is None or (lat, lon) == (0, 0):
        raise ValueError("pilot candidate has invalid coordinates")
    from sys import path as sys_path
    sys_path.insert(0, str(ROOT / "evals/location_verification/dot_cross_source"))
    from dot_cross_source_check import source_coordinate  # noqa: E402
    dot_lat, dot_lon = source_coordinate(dot["raw_record"])
    if dot_lat is None or dot_lon is None or (dot_lat, dot_lon) == (0, 0):
        raise ValueError("pilot DOT row has invalid coordinates")
    identity = dossier["case_id"]
    def evidence(record, value, group):
        return {"claim_field": "coordinates", "value": value,
                "source_id": str(record["row_locator"]), "source_group": group,
                "observed_at": record["timestamp"], "identity_key": identity,
                "content_hash": record["row_sha256"], "lineage_key": record["lineage_key"]}
    tools = {"read_frozen_candidate": {"source_groups": ["DOHMH"],
                                       "evidence": [evidence(dohmh, f"{lat},{lon}", "DOHMH")]},
             "read_current_dot": {"source_groups": ["DOT_CURRENT"],
                                  "evidence": [evidence(dot, f"{dot_lat},{dot_lon}", "DOT_CURRENT")]}}
    manifest = {"schema_version": "lv-replay-tools-v1", "case_id": identity, "tools": tools}
    PILOT.mkdir(parents=True, exist_ok=True)
    PILOT.chmod(0o700)
    write_locked(MANIFEST, manifest)
    case = {"schema_version": "lv-replay-input-v1", "tool_manifest_sha256": sha(MANIFEST),
            "case": {"case_id": identity, "identity_key": identity, "as_of": dossier["as_of"],
                     "claims": [{"field": "coordinates", "value": f"{lat},{lon}",
                                 "tolerance_meters": 10},
                                {"field": "identity", "value": identity}],
                     "allowed_tools": ["read_frozen_candidate", "read_current_dot"],
                     "max_calls": 2, "max_decisions": 2}}
    write_locked(CASE, case)
    return {"scope": "known-risk development case, not Gold or location accuracy",
            "case_id": identity, "source_dossier_sha256": sha(DOSSIERS),
            "case_sha256": sha(CASE), "tool_manifest_sha256": sha(MANIFEST)}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
