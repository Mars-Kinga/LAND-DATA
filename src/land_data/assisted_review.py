"""Audited Codex-assisted review over the existing local review service.

This module is deliberately local-only.  It does not call a model, make a
network request, or write SQLite directly.  A Codex turn reads the evidence
that is already available in LAND-DATA and then calls :func:`codex_assisted_review`
with the resulting decision and an evidence digest.  The decision is applied
through the same validation and atomic storage path used by ``ReviewService``.

The function is kept separate from the CLI and dashboard so those interfaces
can adopt it without changing the review or publication contracts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Dict, Optional

from .models import POI, POICandidate, ReviewDecision
from .processing import validate_score_review, valid_coordinates
from .review import ReviewService
from .storage import Storage
from .util import utc_now


CODEX_REVIEW_MODE = "CODEX_ASSISTED"
CODEX_REVIEW_POLICY_VERSION = "codex-assisted-review/v1"
_SUPPORTED_DECISIONS = {"approve", "reject"}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be nonempty".format(field))
    return value.strip()


def _reviewer_model(value: Any) -> str:
    model = _required_text(value, "reviewer_model")
    if not model.startswith("codex:") or not model[len("codex:"):].strip():
        raise ValueError("reviewer_model must start with codex:")
    # ReviewService applies the same upper bound to reviewer identifiers.
    return model[:160]


def _json_mapping(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("evidence_metadata must be a mapping")
    try:
        # A round trip gives the storage layer an ordinary JSON-compatible
        # object and prevents callers from changing the metadata after the
        # ReviewDecision has been created.
        return json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence_metadata must be JSON serializable") from exc


def _metadata(reviewer_model: str, evidence_digest: str,
              policy_version: str,
              evidence_metadata: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    evidence = _json_mapping(evidence_metadata)
    return {
        "review_mode": CODEX_REVIEW_MODE,
        "model": reviewer_model,
        "reviewer_model": reviewer_model,
        "evidence_digest": evidence_digest,
        "evidence_metadata": evidence,
        "policy_version": policy_version,
    }


def _validate_approval(service: ReviewService, kind: str, item: object) -> None:
    """Run the same target-specific approval gates as ``ReviewService.approve``.

    ``ReviewService.approve`` does not expose metadata injection, so the
    assisted path uses its existing target resolution, validators, decision
    builder, and storage transaction while keeping this gate list in lockstep
    with the public method.  No gate is relaxed for Codex.
    """
    if service._status(item) == "REJECTED":
        raise ValueError("rejected review target cannot be approved")
    if kind == "candidate":
        if not item.resolved_poi_id:
            raise ValueError("unresolved candidate requires review resolve or review promote")
    elif kind == "mention":
        if not item.poi_id or not service.storage.get(POI, item.poi_id):
            raise ValueError("mention must be bound to an existing POI before approval")
        candidate = (service.storage.get(POICandidate, item.poi_candidate_id)
                     if item.poi_candidate_id else None)
        if candidate and candidate.review_status == "REJECTED":
            raise ValueError("mention candidate is rejected")
    elif kind == "poi":
        if (not valid_coordinates(item.latitude, item.longitude) or
                not item.names or not item.city):
            raise ValueError("POI is missing required valid fields")
    elif kind in ("event_candidate", "event"):
        service._validate_event(item)
    elif kind == "official_guidance":
        service._validate_guidance(item)
    elif kind == "source_discovery":
        service._validate_source_candidate(item)


def _approve(service: ReviewService, target_id: str, reviewer_model: str,
             reason: str, metadata: Dict[str, Any]) -> ReviewDecision:
    kind, item = service._target(target_id)
    _validate_approval(service, kind, item)
    now = utc_now()
    new_status = "SUPPORTED" if kind == "source_discovery" else "AUTO_APPROVED"
    action = "APPROVE_SOURCE" if kind == "source_discovery" else "APPROVE"
    decision_metadata = dict(metadata)
    if kind == "source_discovery":
        decision_metadata.update({
            "approval_semantics": "SOURCE_SUPPORT_ONLY",
            "public_content_approved": False,
        })
    decision = service._decision(item, new_status, action, reason,
                                 decision_metadata, reviewer_model)
    service._set_status(item, new_status)
    service._set_reasons(
        item,
        ([*service._review_reasons(item), "HUMAN_ACCEPTED_SOURCE"]
         if kind == "source_discovery" else []),
    )
    if kind == "poi":
        item.coordinates_verified = True
        item.last_verified_at = now
        item.updated_at = now
    # This is the same atomic persistence contract as ReviewService.approve.
    # In particular, do not replace it with a direct SQL UPDATE/INSERT.
    service.storage.apply_review([item], decision)
    if kind in ("candidate", "mention", "poi"):
        validate_score_review(service.storage, service.visibility_threshold)
    return decision


def _reject(service: ReviewService, target_id: str, reviewer_model: str,
            reason: str, metadata: Dict[str, Any]) -> ReviewDecision:
    kind, item = service._target(target_id)
    if service._status(item) == "REJECTED":
        raise ValueError("duplicate review operation")
    decision = service._decision(item, "REJECTED", "REJECT", reason,
                                 metadata, reviewer_model)
    service._set_status(item, "REJECTED")
    service._set_reasons(
        item,
        (["HUMAN_REJECTED_SOURCE"] if kind == "source_discovery"
         else ["HUMAN_REJECTED"]),
    )
    # Keep rejection atomic and let ReviewService's storage safeguards reject
    # stale or duplicate decisions.
    service.storage.apply_review([item], decision)
    if kind in ("candidate", "mention", "poi"):
        validate_score_review(service.storage, service.visibility_threshold)
    return decision


def codex_assisted_review(
    storage: Storage,
    target_id: str,
    decision: str,
    reviewer_model: str,
    reason: str,
    evidence_digest: str,
    *,
    evidence_metadata: Optional[Mapping[str, Any]] = None,
    policy_version: str = CODEX_REVIEW_POLICY_VERSION,
    visibility_threshold: float = 0.8,
) -> ReviewDecision:
    """Record one Codex-reviewed approval or rejection.

    Parameters are intentionally explicit.  ``evidence_digest`` should be a
    stable hash or an input bundle identifier that the calling Codex turn
    inspected; the function does not fetch or interpret evidence.  Optional
    ``evidence_metadata`` may contain bounded, JSON-compatible audit fields
    such as ``input_id``, ``source_ids`` or ``checked_fields``.

    The function has no LLM or network dependency.  It records the model name
    as the audit reviewer and delegates target validation and atomic
    ReviewDecision persistence to the existing review/storage contracts.
    Approval still only changes the review state.  Place publication must pass
    the separate bridge preflight, verify, and install gates.
    """
    target_id = _required_text(target_id, "target_id")
    decision_value = _required_text(decision, "decision").lower()
    if decision_value not in _SUPPORTED_DECISIONS:
        raise ValueError("decision must be approve or reject")
    model = _reviewer_model(reviewer_model)
    reason_value = _required_text(reason, "reason")
    digest = _required_text(evidence_digest, "evidence_digest")
    policy = _required_text(policy_version, "policy_version")
    metadata = _metadata(model, digest, policy, evidence_metadata)
    service = ReviewService(storage, visibility_threshold)
    if decision_value == "approve":
        return _approve(service, target_id, model, reason_value, metadata)
    return _reject(service, target_id, model, reason_value, metadata)


__all__ = [
    "CODEX_REVIEW_MODE",
    "CODEX_REVIEW_POLICY_VERSION",
    "codex_assisted_review",
]
