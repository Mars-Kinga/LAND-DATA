"""Freeze one existing LAND-DATA candidate into a read-only verification case.

The source database is opened read-only. Existing approval flags and derived
POI values are context, never independent proof of a venue's identity.
"""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from pathlib import Path
import sqlite3

from .location_verification import Claim, Evidence, VerificationCase, _time
from .location_verification_prepare import _sha, _write_locked


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _row(db: sqlite3.Connection, table: str, item_id: str) -> dict | None:
    row = db.execute(f"SELECT data FROM {table} WHERE id=?", (item_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _coordinate(candidate: dict) -> str | None:
    try:
        lat, lon = float(candidate["latitude"]), float(candidate["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if (not math.isfinite(lat) or not math.isfinite(lon) or
            not -90 <= lat <= 90 or not -180 <= lon <= 180 or (lat, lon) == (0, 0)):
        return None
    return f"{lat},{lon}"


def prepare_db(db_path: Path, candidate_id: str, output_dir: Path,
               as_of: str) -> dict:
    """Create hash-locked replay inputs without changing the business DB."""
    db_path, output_dir = Path(db_path), Path(output_dir)
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError("business database must be an existing regular file")
    if not candidate_id or "/" in candidate_id or "\\" in candidate_id or candidate_id in {".", ".."}:
        raise ValueError("invalid candidate id")
    if output_dir.resolve().is_relative_to(db_path.resolve().parent / "baselines"):
        raise ValueError("prepared files must be outside frozen baselines")
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        candidate = _row(db, "poi_candidates", candidate_id)
        if candidate is None:
            raise ValueError("candidate not found")
        content = _row(db, "source_contents", candidate["source_content_id"])
        if content is None:
            raise ValueError("candidate source content missing")
        cutoff = _time(as_of)
        if _time(str(content["collected_at"])) > cutoff:
            raise ValueError("candidate source was collected after as-of cutoff")
        raw = _row(db, "raw_source_records", content["raw_record_id"])
        if raw is None:
            raise ValueError("source raw record missing")
        poi = (_row(db, "pois", candidate["resolved_poi_id"])
               if candidate.get("resolved_poi_id") else None)
        if candidate.get("resolved_poi_id") and poi is None:
            raise ValueError("linked POI missing")
        if poi is not None and _time(str(poi["updated_at"])) > cutoff:
            raise ValueError("linked POI was updated after as-of cutoff")
        provenance = []
        if poi is not None:
            provenance = [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM field_provenance WHERE entity_type='POI' AND entity_id=? ORDER BY id",
                (poi["id"],))]
            provenance = [item for item in provenance
                          if _time(str(item["collected_at"])) <= cutoff]

    coordinate = _coordinate(candidate)
    if coordinate is None:
        raise ValueError("candidate needs valid coordinates")
    identity = candidate_id
    claims = [Claim("coordinates", coordinate, tolerance_meters=10),
              Claim("identity", identity)]
    if candidate.get("address"):
        claims.append(Claim("address", str(candidate["address"])))
    source_group = str(content.get("platform") or "UNKNOWN").upper()
    # For dated posts, collection time is not the date of the claimed fact.
    observed_at = str(content.get("published_at") or content["collected_at"])
    _time(observed_at)
    lineage = str(content["raw_record_id"])
    candidate_evidence = [Evidence(
        "coordinates", coordinate, f"candidate:{candidate_id}", source_group,
        observed_at, identity, _digest({"candidate": candidate_id, "coordinates": coordinate,
                                       "content": content["id"]}), lineage)]
    if candidate.get("address"):
        candidate_evidence.append(Evidence(
            "address", str(candidate["address"]), f"candidate:{candidate_id}",
            source_group, observed_at, identity,
            _digest({"candidate": candidate_id, "address": candidate["address"],
                     "content": content["id"]}), lineage))
    # Source metadata helps the planner find the next gap, but is not a
    # verified identity/address/coordinate claim in the independent Goal Gate.
    source_context = Evidence(
        "source_context", str(content.get("source_url") or raw.get("source_url") or content["id"])[:512],
        f"source_content:{content['id']}", source_group, str(content["collected_at"]), identity,
        _digest({"content_id": content["id"], "raw_id": raw["id"],
                 "source_url": content.get("source_url") or raw.get("source_url")}), lineage)
    tools = {
        "read_candidate": {"source_groups": [source_group],
                           "evidence": [item.__dict__ for item in candidate_evidence]},
        "read_source_metadata": {"source_groups": [source_group],
                                 "evidence": [source_context.__dict__]},
    }
    if poi is not None:
        # A linked POI may itself have been created from this candidate. Its
        # current fields and provenance are context, not new independent proof.
        context = {"poi_id": poi["id"], "name": poi.get("names"),
                   "address": poi.get("address"), "coordinates": _coordinate(poi),
                   "provenance_count": len(provenance),
                   "provenance": [{"source": item.get("source"),
                                   "source_content_id": item.get("source_content_id"),
                                   "field_name": item.get("field_name"),
                                   "source_reference_url": str(item.get("source_reference_url") or "")[:512]}
                                  for item in provenance[:20]]}
        linked = Evidence("linked_poi_context", json.dumps(context, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")), f"poi:{poi['id']}",
                          "LOCAL_POI", observed_at, identity, _digest(context), lineage)
        tools["read_linked_poi"] = {"source_groups": ["LOCAL_POI"],
                                    "evidence": [linked.__dict__]}
    names = list(tools)
    case = VerificationCase(candidate_id, identity, as_of, tuple(claims),
                            tuple(names), max_calls=len(names),
                            max_decisions=max(2, len(names) * 2))
    manifest = {"schema_version": "lv-replay-tools-v1", "case_id": candidate_id,
                "tools": tools}
    if not output_dir.exists():
        output_dir.mkdir(parents=True)
        output_dir.chmod(0o700)
    elif not output_dir.is_dir() or output_dir.is_symlink():
        raise ValueError("output directory must be a real directory")
    manifest_path = output_dir / f"{candidate_id}-tools.json"
    case_path = output_dir / f"{candidate_id}-case.json"
    _write_locked(manifest_path, manifest)
    case_input = {"schema_version": "lv-replay-input-v1",
                  "tool_manifest_sha256": _sha(manifest_path),
                  "case": {"case_id": case.case_id, "identity_key": case.identity_key,
                           "as_of": case.as_of,
                           "claims": [item.__dict__ for item in case.claims],
                           "allowed_tools": names, "max_calls": case.max_calls,
                           "max_decisions": case.max_decisions,
                           "max_evidence_age_days": case.max_evidence_age_days}}
    _write_locked(case_path, case_input)
    return {"scope": "read-only business candidate snapshot; not Gold or accuracy",
            "candidate_id": candidate_id, "case_path": str(case_path),
            "case_sha256": _sha(case_path), "tool_manifest_path": str(manifest_path),
            "tool_manifest_sha256": _sha(manifest_path), "tool_count": len(names)}


def attach_business_review_targets(db_path: Path, queue: list[dict]) -> list[dict]:
    """Point a verification handoff at the existing review target, read-only."""
    db_path = Path(db_path)
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError("business database must be an existing regular file")
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        enriched = []
        for item in queue:
            candidate = _row(db, "poi_candidates", item["case_id"])
            target = None if candidate is None else {
                "type": "POICandidate", "id": item["case_id"],
                "review_status": candidate.get("review_status"),
            }
            enriched.append({**item, "business_review_target": target})
    return enriched
