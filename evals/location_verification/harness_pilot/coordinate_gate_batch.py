"""Offline risk-routing pilot on frozen LAND-DATA output and NYC DOT snapshot.

This is a fixed-order control-layer run, not an LLM policy or truth benchmark.
Unique name/address matching is only a retrieval assumption. VERIFIED here
means coordinate-field agreement within 10m, not verified venue identity.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals/location_verification/land_data_output_cross_source"))

from land_data.location_verification import (  # noqa: E402
    Claim, Decision, Evidence, ToolSpec, VerificationCase, VerificationHarness, VerificationJournal,
    build_handoff_packet,
)
from land_data_output_cross_source import (  # noqa: E402
    DEFAULT_CAPTURE_MANIFEST, DEFAULT_CURRENT, DEFAULT_DB, DEFAULT_FREEZE_MANIFEST,
    build_current_index, compare_current_case, load_and_validate_freeze_manifest,
    load_capture_manifest, load_dohmh_outputs, load_snapshot, open_immutable_database,
)


PRIVATE = ROOT / ".local/location-verification-live-check/harness_pilot"
PUBLIC = Path(__file__).resolve().parent / "coordinate-gate-summary.json"
AS_OF = "2026-10-28T23:59:59Z"


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class FixedReadClient:
    def choose(self, _case, _evidence, actions, available_tools):
        used = {action.tool for action in actions}
        for name in available_tools:
            if name not in used:
                return Decision("tool", name)
        return Decision("stop", reason="fixed read order exhausted")


def run() -> dict:
    freeze = load_and_validate_freeze_manifest(DEFAULT_FREEZE_MANIFEST, DEFAULT_DB)
    current_rows, current_info = load_snapshot(DEFAULT_CURRENT, "current")
    historical_path = ROOT / ".local/location-verification-live-check/dot_batches/historical.json"
    historical_rows, historical_info = load_snapshot(historical_path, "historical")
    capture = load_capture_manifest(DEFAULT_CAPTURE_MANIFEST, current_info, historical_info)
    del historical_rows
    db = open_immutable_database(DEFAULT_DB)
    try:
        outputs, _ = load_dohmh_outputs(db)
        candidate_borough = {
            row[0]: ((json.loads(row[1]).get("raw_metadata") or {}).get("source_metadata") or {}).get("borough")
            for row in db.execute("""
                SELECT pc.id, sc.data FROM poi_candidates pc
                JOIN source_contents sc ON sc.id = pc.source_content_id
                WHERE sc.platform = 'nyc-open-data-restaurants'
            """)
        }
    finally:
        db.close()
    index, _ = build_current_index(current_rows)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    PRIVATE.chmod(0o700)
    journal = VerificationJournal(PRIVATE / "coordinate-gate-batch-v2.db")
    count = Counter()
    violations = []
    handoff_packets = []
    capture_at = capture["datasets"]["current"]["capture_file_mtime"]
    for output in outputs:
        row = {"screening_id": output["candidate_id"], "name": output["name"],
               "address": output["address"], "city": output["candidate_city"],
               "latitude": output["latitude"], "longitude": output["longitude"],
               "platform": output["platform"], "external_id": output["external_id"]}
        comparison = compare_current_case(row, index)
        if comparison["match_type"] != "unique_name_address_match" or not comparison.get("coordinate_comparable"):
            continue
        dot = comparison["dot_row"]
        assert dot is not None
        candidate_value = f'{output["latitude"]},{output["longitude"]}'
        dot_value = f'{dot["latitude"]},{dot["longitude"]}'
        identity = str(output["candidate_id"])
        source_borough = str(candidate_borough.get(output["candidate_id"]) or "").strip().casefold()
        dot_borough = str(current_rows[dot["record_index"]].get("borough") or "").strip().casefold()
        borough_relation = ("same" if source_borough and source_borough == dot_borough
                            else "different" if source_borough and dot_borough else "missing")
        dot_identity = identity if borough_relation != "different" else identity + ":other-borough"
        case = VerificationCase(
            identity, identity, AS_OF,
            (Claim("coordinates", candidate_value, tolerance_meters=10),),
            ("read_frozen_candidate", "read_current_dot"), max_calls=2, max_decisions=2,
        )
        candidate_evidence = Evidence("coordinates", candidate_value, identity, "DOHMH",
                                      output["source_collected_at"], identity, digest(row))
        dot_evidence = Evidence("coordinates", dot_value, str(dot.get("row_id") or identity),
                                "DOT_CURRENT", capture_at, dot_identity, digest(dot))
        tools = {
            "read_frozen_candidate": ToolSpec("read_frozen_candidate", lambda *_: [candidate_evidence], frozenset({"DOHMH"})),
            "read_current_dot": ToolSpec("read_current_dot", lambda *_: [dot_evidence], frozenset({"DOT_CURRENT"})),
        }
        result = VerificationHarness(tools, journal, FixedReadClient()).run(case)
        conflict = comparison["coordinate_distance_m"] > 10
        key = "over_10m" if conflict else "within_10m"
        count[key] += 1
        if conflict:
            count[f"over_10m_borough:{borough_relation}"] += 1
            if comparison["coordinate_distance_m"] > 50:
                count[f"over_50m_borough:{borough_relation}"] += 1
        count[f"{key}:{result.status}:{result.reason}"] += 1
        count[f"legacy:{output['candidate_review_status']}"] += 1
        if result.status == "HANDOFF_HUMAN":
            handoff_packets.append(build_handoff_packet(case, result))
        expected_reason = ("IDENTITY_CONFLICT" if borough_relation == "different" else
                           "CLAIM_CONFLICT" if conflict else "INDEPENDENT_EVIDENCE")
        if len(result.actions) != 2 or result.reason != expected_reason:
            violations.append(digest(identity))
    if violations:
        raise AssertionError(f"{len(violations)} case outcomes differ from prespecified field-distance contract")
    handoff_path = PRIVATE / "coordinate-handoffs-v1.jsonl"
    handoff_bytes = "".join(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                            for packet in handoff_packets).encode("utf-8")
    if handoff_path.exists():
        if handoff_path.read_bytes() != handoff_bytes:
            raise ValueError("existing handoff packet differs from replay")
    else:
        handoff_path.write_bytes(handoff_bytes)
        handoff_path.chmod(0o600)
    summary = {
        "scope": "Development control-layer pilot: unique name+address matched, coordinate-comparable DOHMH/DOT rows only",
        "fixed_order_tool_calls_per_case": 2,
        "model_calls": 0,
        "claim_boundary": "Field agreement and conflict routing only; not human Gold, venue identity, current operation, or location accuracy",
        "frozen_db_sha256": freeze["database"]["sha256"],
        "current_dot_sha256": current_info["sha256"],
        "capture_manifest_sha256": capture["sha256"],
        "counts": dict(sorted(count.items())),
        "human_handoff_packet_count": len(handoff_packets),
        "human_handoff_packet_sha256": hashlib.sha256(handoff_bytes).hexdigest(),
        "journal_sha256": hashlib.sha256(journal.path.read_bytes()).hexdigest(),
    }
    PUBLIC.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
