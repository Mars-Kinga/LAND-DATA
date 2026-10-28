"""Read-only, replayable location-verification harness development slice.

The decision client may propose a tool or stop. The harness alone controls
permissions, budgets, evidence admission, and the final goal decision. This
module does not publish POIs or claim a location-accuracy score.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Protocol


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Claim:
    field: str
    value: str
    required: bool = True
    tolerance_meters: float | None = None

    def __post_init__(self) -> None:
        if self.tolerance_meters is not None and self.tolerance_meters < 0:
            raise ValueError("coordinate tolerance cannot be negative")


@dataclass(frozen=True)
class Evidence:
    claim_field: str
    value: str
    source_id: str
    source_group: str
    observed_at: str
    identity_key: str
    content_hash: str
    lineage_key: str = ""

    def __post_init__(self) -> None:
        if not all((self.claim_field, self.value, self.source_id, self.source_group,
                    self.identity_key, self.content_hash)):
            raise ValueError("evidence requires claim, source, identity, and content hash")
        _time(self.observed_at)


@dataclass(frozen=True)
class VerificationCase:
    case_id: str
    identity_key: str
    as_of: str
    claims: tuple[Claim, ...]
    allowed_tools: tuple[str, ...]
    max_calls: int = 4
    max_decisions: int = 8
    max_evidence_age_days: int = 365

    def __post_init__(self) -> None:
        _time(self.as_of)
        if not self.case_id or not self.identity_key or not self.claims:
            raise ValueError("case id, identity, and claims are required")
        if len({claim.field for claim in self.claims}) != len(self.claims):
            raise ValueError("claim fields must be unique")
        if not any(claim.required for claim in self.claims):
            raise ValueError("at least one required claim is needed for verification")
        if self.max_calls < 1 or self.max_decisions < 1:
            raise ValueError("budgets must be positive")
        if self.max_evidence_age_days < 0:
            raise ValueError("evidence age limit cannot be negative")


@dataclass(frozen=True)
class Decision:
    kind: str  # "tool" or "stop"
    tool: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class ActionRecord:
    step: int
    tool: str
    arguments: dict[str, Any]
    status: str
    result_hash: str
    reason: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[[VerificationCase, Mapping[str, Any]], list[Evidence]]
    source_groups: frozenset[str]
    allowed_arguments: frozenset[str] = frozenset()
    required_arguments: frozenset[str] = frozenset()
    read_only: bool = True


class DecisionClient(Protocol):
    def choose(self, case: VerificationCase, evidence: tuple[Evidence, ...],
               actions: tuple[ActionRecord, ...], available_tools: tuple[str, ...]) -> Decision: ...


@dataclass(frozen=True)
class VerificationResult:
    case_id: str
    status: str  # VERIFIED or HANDOFF_HUMAN
    reason: str
    evidence: tuple[Evidence, ...]
    actions: tuple[ActionRecord, ...]
    decisions: int
    decision_log: tuple[Decision, ...]


def build_handoff_packet(case: VerificationCase, result: VerificationResult) -> dict[str, Any]:
    """Preserve the contested claim and trace for a human; never decide a correction."""
    if result.case_id != case.case_id or result.status != "HANDOFF_HUMAN":
        raise ValueError("handoff packet requires a matching human-handoff result")
    evidence_by_field = {
        claim.field: [asdict(item) for item in result.evidence if item.claim_field == claim.field]
        for claim in case.claims
    }
    review_questions = ["Confirm the evidence refers to the same physical venue and time."]
    if result.reason == "IDENTITY_CONFLICT":
        review_questions.append("Resolve whether the records describe different branches or boroughs.")
    elif result.reason in {"CLAIM_CONFLICT", "SOURCE_GROUP_CONFLICT"}:
        review_questions.append("Adjudicate the conflicting field values against the cited source records.")
    else:
        review_questions.append("Collect or assess the missing evidence before approving any claim.")
    return {
        "schema_version": "lv-handoff-v1", "case_id": case.case_id, "as_of": case.as_of,
        "status": result.status, "reason": result.reason,
        "claims": [asdict(claim) for claim in case.claims],
        "evidence_by_field": evidence_by_field,
        "actions": [asdict(action) for action in result.actions],
        "review_questions": review_questions,
        "proposed_correction": None,
    }


class GoalGate:
    """Independent deterministic gate; a model's stop proposal is advisory."""

    def inspect(self, case: VerificationCase, evidence: tuple[Evidence, ...]) -> tuple[str, str]:
        as_of = _time(case.as_of)
        claim_map = {claim.field: claim for claim in case.claims}
        groups: dict[str, dict[str, str]] = {field: {} for field in claim_map}
        lineages: dict[str, set[str]] = {field: set() for field in claim_map}
        for item in evidence:
            if item.claim_field not in claim_map:
                continue
            observed = _time(item.observed_at)
            if observed > as_of or as_of - observed > timedelta(days=case.max_evidence_age_days):
                continue
            if item.identity_key != case.identity_key:
                return "HANDOFF_HUMAN", "IDENTITY_CONFLICT"
            prior = groups[item.claim_field].get(item.source_group)
            if prior is not None and prior != item.value:
                return "HANDOFF_HUMAN", "SOURCE_GROUP_CONFLICT"
            groups[item.claim_field][item.source_group] = item.value
            lineages[item.claim_field].add(item.lineage_key or item.content_hash)
        for claim in case.claims:
            if not claim.required:
                continue
            values = groups[claim.field]
            try:
                agrees = all(_values_agree(claim, value) for value in values.values())
            except ValueError:
                return "HANDOFF_HUMAN", "INVALID_COORDINATE_EVIDENCE"
            if not agrees:
                return "HANDOFF_HUMAN", "CLAIM_CONFLICT"
            if len(values) < 2 or len(lineages[claim.field]) < 2:
                return "CONTINUE", f"EVIDENCE_GAP:{claim.field}"
        return "VERIFIED", "INDEPENDENT_EVIDENCE"


def _values_agree(claim: Claim, observed: str) -> bool:
    if claim.tolerance_meters is None:
        return observed == claim.value
    try:
        a_lat, a_lon = (float(piece.strip()) for piece in claim.value.split(","))
        b_lat, b_lon = (float(piece.strip()) for piece in observed.split(","))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid coordinate pair") from exc
    if not all(math.isfinite(number) for number in (a_lat, a_lon, b_lat, b_lon)):
        raise ValueError("nonfinite coordinate")
    if not all(-90 <= lat <= 90 for lat in (a_lat, b_lat)) or not all(
        -180 <= lon <= 180 for lon in (a_lon, b_lon)
    ):
        raise ValueError("coordinate outside WGS84 bounds")
    if (a_lat, a_lon) == (0, 0) or (b_lat, b_lon) == (0, 0):
        raise ValueError("zero-zero coordinate is a missing-data sentinel")
    lat1, lon1, lat2, lon2 = map(math.radians, (a_lat, a_lon, b_lat, b_lon))
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    hav = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    distance = 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(hav)))
    return distance <= claim.tolerance_meters


class VerificationJournal:
    """Separate SQLite checkpoint; never opens or writes the source POI DB."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError("journal must not be a symlink")
        if self.path.exists():
            existing = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)
            try:
                tables = {row[0] for row in existing.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('runs','actions')")}
            finally:
                existing.close()
            if tables != {"runs", "actions"}:
                raise ValueError("existing database is not a verification journal")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (case_id TEXT PRIMARY KEY, case_hash TEXT NOT NULL, state TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS actions (case_id TEXT NOT NULL, step INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(case_id, step))")
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def load(self, case: VerificationCase) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT case_hash, state FROM runs WHERE case_id=?", (case.case_id,)).fetchone()
        expected = _digest(asdict(case))
        if row is None:
            return {"evidence": [], "actions": [], "decisions": 0,
                    "decision_log": [], "status": "", "reason": ""}
        if row[0] != expected:
            raise ValueError("case changed after checkpoint")
        return json.loads(row[1])

    def handoff_queue(self) -> list[dict[str, Any]]:
        """List reviewer work without exposing evidence payloads or modifying the journal."""
        with closing(sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT case_id, state FROM runs ORDER BY case_id").fetchall()
        queue = []
        for case_id, payload in rows:
            state = json.loads(payload)
            if state.get("status") == "HANDOFF_HUMAN":
                queue.append({"case_id": case_id, "reason": state["reason"],
                              "tool_calls": len(state["actions"]),
                              "decisions": state["decisions"]})
        return queue

    def save(self, case: VerificationCase, state: dict[str, Any], action: ActionRecord | None = None) -> None:
        payload = _json(state)
        with self._connect() as db:
            if action is not None:
                db.execute("INSERT INTO actions(case_id, step, data) VALUES(?,?,?)",
                           (case.case_id, action.step, _json(asdict(action))))
            db.execute("INSERT INTO runs(case_id, case_hash, state) VALUES(?,?,?) "
                       "ON CONFLICT(case_id) DO UPDATE SET state=excluded.state",
                       (case.case_id, _digest(asdict(case)), payload))


class VerificationHarness:
    def __init__(self, tools: Mapping[str, ToolSpec], journal: VerificationJournal,
                 decision_client: DecisionClient, goal_gate: GoalGate | None = None):
        self.tools = dict(tools)
        self.journal = journal
        self.decision_client = decision_client
        self.goal_gate = goal_gate or GoalGate()

    def run(self, case: VerificationCase) -> VerificationResult:
        state = self.journal.load(case)
        state.setdefault("decision_log", [])  # Older checkpoints remain readable.
        while not state["status"]:
            evidence = tuple(Evidence(**item) for item in state["evidence"])
            actions = tuple(ActionRecord(**item) for item in state["actions"])
            gate, reason = self.goal_gate.inspect(case, evidence)
            if gate != "CONTINUE":
                state.update(status=gate, reason=reason)
                self.journal.save(case, state)
                break
            if len(actions) >= case.max_calls or state["decisions"] >= case.max_decisions:
                state.update(status="HANDOFF_HUMAN", reason="BUDGET_EXHAUSTED")
                self.journal.save(case, state)
                break
            available = tuple(name for name in case.allowed_tools if name in self.tools)
            try:
                proposal = self.decision_client.choose(case, evidence, actions, available)
            except Exception as exc:
                state["decisions"] += 1
                state["decision_log"].append(asdict(Decision("error", reason=type(exc).__name__)))
                state.update(status="HANDOFF_HUMAN", reason="DECISION_CLIENT_ERROR")
                self.journal.save(case, state)
                break
            state["decisions"] += 1
            state["decision_log"].append(asdict(proposal))
            if proposal.kind == "stop":
                # Repeated unsupported stops consume decision budget, not tool budget.
                self.journal.save(case, state)
                continue
            if proposal.kind != "tool":
                state.update(status="HANDOFF_HUMAN", reason="INVALID_DECISION")
                self.journal.save(case, state)
                break
            spec = self.tools.get(proposal.tool)
            arguments = dict(proposal.arguments)
            if (spec is None or proposal.tool not in case.allowed_tools or not spec.read_only):
                status, detail, found = "DENIED", "PERMISSION_DENIED", []
            elif (set(arguments) - spec.allowed_arguments or
                  not spec.required_arguments.issubset(arguments)):
                status, detail, found = "DENIED", "INVALID_ARGUMENTS", []
            else:
                try:
                    found = spec.handler(case, arguments)
                    if not isinstance(found, list) or not all(isinstance(item, Evidence) for item in found):
                        raise TypeError("tool must return a list of Evidence")
                    if any(item.source_group not in spec.source_groups for item in found):
                        raise ValueError("tool returned an unregistered source group")
                    status, detail = "OK", ""
                except Exception as exc:
                    found = []
                    status, detail = "ERROR", type(exc).__name__
            action = ActionRecord(len(actions) + 1, proposal.tool, arguments,
                                  status, _digest([asdict(item) for item in found]), detail)
            state["actions"].append(asdict(action))
            if status == "OK":
                state["evidence"].extend(asdict(item) for item in found)
            elif status == "DENIED":
                state.update(status="HANDOFF_HUMAN", reason=detail)
            self.journal.save(case, state, action)
        return VerificationResult(case.case_id, state["status"], state["reason"],
                                  tuple(Evidence(**item) for item in state["evidence"]),
                                  tuple(ActionRecord(**item) for item in state["actions"]),
                                  state["decisions"],
                                  tuple(Decision(**item) for item in state["decision_log"]))
