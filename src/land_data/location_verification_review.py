"""Opt-in bridge from a verified conflict journal to business review holds."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sqlite3
from contextlib import closing
import uuid

from .location_verification import VerificationJournal
from .location_verification_db import _coordinate, _row
from .location_verification_replay import file_hash, load_replay
from .models import POI, POICandidate, ReviewDecision
from .sqlite_storage import SQLiteStorage
from .util import utc_now


CONFLICT_REASONS = frozenset({"CLAIM_CONFLICT", "IDENTITY_CONFLICT",
                              "SOURCE_GROUP_CONFLICT", "INVALID_COORDINATE_EVIDENCE"})
HOLD_MARKER = "LOCATION_VERIFICATION_HOLD"


def hold_conflict(db_path: Path, case_path: Path, manifest_path: Path,
                  journal_path: Path, *, apply: bool = False) -> dict:
    """Preview by default; only explicit apply writes auditable review holds."""
    db_path = Path(db_path)
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError("business database must be an existing regular file")
    if not Path(journal_path).is_file() or Path(journal_path).is_symlink():
        raise ValueError("verification journal must exist and not be a symlink")
    case, tools, hashes = load_replay(Path(case_path), Path(manifest_path))
    if case.identity_key != case.case_id or "read_candidate" not in tools:
        raise ValueError("handoff must originate from a business candidate case")
    state = VerificationJournal(Path(journal_path)).load(case)
    if state["status"] != "HANDOFF_HUMAN" or state["reason"] not in CONFLICT_REASONS:
        raise ValueError("only an evidence conflict can place a business review hold")
    claim_values = {claim.field: claim.value for claim in case.claims}
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        candidate = _row(db, "poi_candidates", case.case_id)
        if candidate is None:
            raise ValueError("business candidate not found")
        if (_coordinate(candidate) != claim_values.get("coordinates") or
                str(candidate.get("address") or "") != claim_values.get("address", "")):
            raise ValueError("business candidate changed since verification snapshot")
        poi = (_row(db, "pois", candidate["resolved_poi_id"])
               if candidate.get("resolved_poi_id") else None)
        if candidate.get("resolved_poi_id") and poi is None:
            raise ValueError("linked POI missing")
    targets = [{"type": "POICandidate", "id": case.case_id,
                "current_status": candidate["review_status"]}]
    if poi is not None:
        targets.append({"type": "POI", "id": poi["id"],
                        "current_status": poi["review_status"]})
    response = {"case_id": case.case_id, "verification_reason": state["reason"],
                "case_sha256": hashes["case_sha256"],
                "journal_sha256": file_hash(Path(journal_path)),
                "targets": targets, "applied": False}
    if not apply:
        return response
    decisions = []
    with SQLiteStorage(str(db_path)) as storage:
        items = []
        for model, item_id in ((POICandidate, case.case_id),
                               (POI, poi["id"] if poi is not None else None)):
            if item_id is None:
                continue
            item = storage.get(model, item_id)
            if item is None:
                raise ValueError("business review target disappeared")
            history = storage.review_history(type(item).__name__, item.id)
            matching_holds = [index for index, decision in enumerate(history)
                              if decision.action == "VERIFY_HOLD" and
                              decision.metadata.get("verification_case_sha256") ==
                              hashes["case_sha256"]]
            if matching_holds and any(decision.action == "APPROVE"
                                      for decision in history[matching_holds[-1] + 1:]):
                continue  # A later human adjudication outranks this old journal.
            if item.review_status != "AUTO_APPROVED":
                continue
            prior = item.review_status
            old_reasons = list(item.review_reasons)
            item.review_status = "NEEDS_REVIEW"
            item.review_reasons = list(dict.fromkeys([*old_reasons, HOLD_MARKER,
                                                     "LOCATION_VERIFICATION_" + state["reason"]]))
            if isinstance(item, POI):
                item.map_visibility = False
            decisions.append(ReviewDecision(
                id=uuid.uuid4().hex, target_type=type(item).__name__, target_id=item.id,
                previous_status=prior, new_status="NEEDS_REVIEW", action="VERIFY_HOLD",
                reason="Location verification evidence conflict: " + state["reason"],
                reviewer="location-verification-harness", created_at=utc_now(),
                metadata={"previous_review_reasons": old_reasons,
                          "verification_case_sha256": hashes["case_sha256"],
                          "verification_tool_manifest_sha256": hashes["tool_manifest_sha256"],
                          "verification_journal_sha256": response["journal_sha256"]}))
            items.append(item)
        if decisions:
            storage.apply_review(items, decisions[0], decisions[1:])
    response["applied"] = bool(decisions)
    response["review_decisions"] = [asdict(item) for item in decisions]
    return response
