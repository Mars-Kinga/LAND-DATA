"""Prepare a conservative replay case from a frozen LAND-DATA evidence dossier."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .location_verification import Claim, Evidence, VerificationCase


FORBIDDEN = {"gold", "gold_label", "gold_action", "review_status", "review_reasons",
             "resolved_poi_id", "matched_poi_id", "coordinate_verified", "adjudication_note",
             "selection_signal", "risk_signals"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _has_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(FORBIDDEN.intersection(value)) or any(_has_forbidden(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_forbidden(v) for v in value)
    return False


def _coordinate(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _write_locked(path: Path, value: dict) -> None:
    rendered = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != rendered:
            raise ValueError("existing prepared file differs from replay")
    else:
        path.write_bytes(rendered)
        path.chmod(0o600)


def prepare(dossier_path: Path, expected_sha256: str, case_id: str,
            output_dir: Path, as_of: str | None = None) -> dict:
    dossier_path, output_dir = Path(dossier_path), Path(output_dir)
    if _sha(dossier_path) != expected_sha256:
        raise ValueError("dossier hash mismatch")
    matches = []
    for line in dossier_path.read_text().splitlines():
        if line:
            item = json.loads(line)
            if item.get("case_id") == case_id:
                matches.append(item)
    if len(matches) != 1:
        raise ValueError("case id must occur exactly once in dossier")
    dossier = matches[0]
    if _has_forbidden(dossier):
        raise ValueError("dossier contains answer-bearing fields")
    cutoff = as_of or dossier.get("as_of")
    if not cutoff:
        raise ValueError("as-of timestamp required")
    candidate = dossier["candidate_claims"]
    records = dossier["source_records"]
    if not records or records[0]["source_group"] not in {"DOHMH_FROZEN", "DOHMH_INTAKE", "OSM_INTAKE"}:
        raise ValueError("dossier lacks supported intake source")
    lat, lon = _coordinate(candidate.get("latitude")), _coordinate(candidate.get("longitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0, 0):
        raise ValueError("candidate needs valid coordinates for this replay adapter")
    identity = case_id
    tools = {}
    for record in records:
        group = record["source_group"]
        if group not in {"DOHMH_FROZEN", "DOHMH_INTAKE", "OSM_INTAKE", "DOT_CURRENT"}:
            continue  # Historical rows are context, not current-location proof.
        if group == "DOT_CURRENT":
            raw = record["raw_record"]
            source_lat, source_lon = _coordinate(raw.get("latitude")), _coordinate(raw.get("longitude"))
            if source_lat is None or source_lon is None or (source_lat, source_lon) == (0, 0):
                continue
            source_group, tool_name = "DOT_CURRENT", "read_current_dot"
        else:
            source_lat, source_lon = lat, lon
            source_group, tool_name = group.split("_")[0], "read_frozen_candidate"
        if tool_name in tools:
            raise ValueError("duplicate current source tool")
        if not record.get("timestamp") or not record.get("row_sha256") or not record.get("lineage_key"):
            raise ValueError("source provenance incomplete")
        actual_row_hash = hashlib.sha256(json.dumps(record["raw_record"], ensure_ascii=False,
                                                 sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if actual_row_hash != record["row_sha256"]:
            raise ValueError("source row hash mismatch")
        evidence = {"claim_field": "coordinates", "value": f"{source_lat},{source_lon}",
                    "source_id": str(record["row_locator"]), "source_group": source_group,
                    "observed_at": record["timestamp"], "identity_key": identity,
                    "content_hash": record["row_sha256"], "lineage_key": record["lineage_key"]}
        Evidence(**evidence)
        tools[tool_name] = {"source_groups": [source_group], "evidence": [evidence]}
    if "read_frozen_candidate" not in tools:
        raise ValueError("candidate tool missing")
    # Identity is required but has no evidence: coordinate agreement alone never verifies a venue.
    names = [name for name in ("read_frozen_candidate", "read_current_dot") if name in tools]
    VerificationCase(case_id, identity, cutoff,
                     (Claim("coordinates", f"{lat},{lon}", tolerance_meters=10),
                      Claim("identity", identity)), tuple(names),
                     max_calls=len(names), max_decisions=max(2, len(names) * 2))
    manifest = {"schema_version": "lv-replay-tools-v1", "case_id": case_id, "tools": tools}
    if not output_dir.exists():
        output_dir.mkdir(parents=True)
        output_dir.chmod(0o700)
    elif not output_dir.is_dir() or output_dir.is_symlink():
        raise ValueError("output directory must be a real directory")
    manifest_path = output_dir / f"{case_id}-tools.json"
    case_path = output_dir / f"{case_id}-case.json"
    _write_locked(manifest_path, manifest)
    case_input = {"schema_version": "lv-replay-input-v1", "tool_manifest_sha256": _sha(manifest_path),
                  "case": {"case_id": case_id, "identity_key": identity, "as_of": cutoff,
                           "claims": [{"field": "coordinates", "value": f"{lat},{lon}",
                                       "tolerance_meters": 10},
                                      {"field": "identity", "value": identity}],
                           "allowed_tools": names, "max_calls": len(names),
                           "max_decisions": max(2, len(names) * 2)}}
    _write_locked(case_path, case_input)
    return {"scope": "development-only candidate replay; not Gold or location accuracy",
            "case_id": case_id, "dossier_sha256": expected_sha256,
            "case_path": str(case_path), "case_sha256": _sha(case_path),
            "tool_manifest_path": str(manifest_path), "tool_manifest_sha256": _sha(manifest_path),
            "tool_count": len(names)}
