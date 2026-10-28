"""Recheck every daily reviewer dossier row against the frozen input files."""

from collections import Counter
import hashlib
import json
from pathlib import Path

from audit_evidence_dossiers import forbidden_paths, row_sha, rows


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PRIVATE = ROOT / ".local/location-verification-live-check/annotation_packet_500"
PACKET = PRIVATE / "cases.jsonl"
DOSSIERS = PRIVATE / "daily-evidence-dossiers-v1.jsonl"
SUMMARY = HERE / "daily-evidence-dossier-summary-v1.json"
CURRENT = ROOT / ".local/location-verification-live-check/dot_batches/current.json"
HISTORICAL = ROOT / ".local/location-verification-live-check/dot_batches/historical.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit() -> dict:
    summary = json.loads(SUMMARY.read_text())
    if sha(DOSSIERS) != summary["dossiers_sha256"]:
        raise ValueError("daily dossier file hash differs from summary")
    for key, path in (("packet", PACKET), ("current_dot", CURRENT), ("historical_dot", HISTORICAL)):
        if sha(path) != summary["input_hashes"][key]:
            raise ValueError(f"{key} input hash differs from summary")
    cases = [json.loads(line) for line in PACKET.read_text().splitlines() if line]
    dossiers = [json.loads(line) for line in DOSSIERS.read_text().splitlines() if line]
    if len(cases) != 500 or len(dossiers) != 500 or [x["screening_id"] for x in cases] != [x["case_id"] for x in dossiers]:
        raise ValueError("daily dossier coverage mismatch")
    if list(forbidden_paths(dossiers)):
        raise ValueError("daily dossier contains legacy answer fields")
    current, historical = rows(CURRENT), rows(HISTORICAL)
    counts = Counter()
    for case, dossier in zip(cases, dossiers):
        if dossier["schema_version"] != "lv-daily-evidence-dossier-v1":
            raise ValueError("wrong daily dossier schema")
        expected_claims = {key: case["candidate"].get(key)
                           for key in ("name", "address", "latitude", "longitude", "city")}
        if dossier["candidate_claims"] != expected_claims:
            raise ValueError("candidate claims differ from intake")
        for position, record in enumerate(dossier["source_records"]):
            group = record["source_group"]
            if row_sha(record["raw_record"]) != record["row_sha256"]:
                raise ValueError("daily evidence payload hash mismatch")
            if position == 0:
                expected_group = ("DOHMH" if case["source"]["platform"] == "nyc-open-data-restaurants"
                                  else "OSM" if case["source"]["platform"] == "osm" else "OTHER") + "_INTAKE"
                expected = {key: case[key] for key in ("city", "candidate", "source", "evidence")}
                if (group != expected_group or record["row_locator"] != case["screening_id"]
                        or record["snapshot_sha256"] != summary["input_hashes"]["packet"]
                        or record["timestamp_basis"] != "source_collected_at"):
                    raise ValueError("intake evidence provenance mismatch")
            elif group in ("DOT_CURRENT", "DOT_HISTORICAL"):
                key = "current_dot" if group == "DOT_CURRENT" else "historical_dot"
                source_rows = current if group == "DOT_CURRENT" else historical
                index = record["row_locator"]
                if (not isinstance(index, int) or index < 0 or index >= len(source_rows)
                        or record["snapshot_sha256"] != summary["input_hashes"][key]
                        or record["timestamp_basis"] != "snapshot_file_mtime"):
                    raise ValueError("DOT evidence provenance mismatch")
                expected = source_rows[index]
            else:
                raise ValueError("unexpected source group")
            if record["raw_record"] != expected:
                raise ValueError("daily dossier differs from frozen source row")
            counts[group] += 1
    if dict(counts) != summary["source_record_counts"]:
        raise ValueError("daily evidence source counts mismatch")
    return {"status": "VERIFIED", "dossiers": len(dossiers),
            "source_records": dict(sorted(counts.items())), "forbidden_field_paths": 0,
            "dossiers_sha256": sha(DOSSIERS)}


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, sort_keys=True))
