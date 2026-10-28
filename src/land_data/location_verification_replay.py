"""Development-only CLI for a hash-locked, read-only evidence replay.

Run with ``python -m land_data.location_verification_replay``. The decision
client sees only the case, prior evidence and registered tool names. The
manifest is still in this process, so this is not the formal blind-test
isolation boundary described in the evaluation protocol.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .llm_extraction import load_local_env
from .location_verification import (
    Claim, Decision, Evidence, ToolSpec, VerificationCase, VerificationHarness,
    VerificationJournal, build_handoff_packet,
)
from .location_verification_qwen import QwenVerificationDecisionClient


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


class FixedOrderDecisionClient:
    def choose(self, _case, _evidence, actions, available_tools):
        used = {action.tool for action in actions}
        for name in available_tools:
            if name not in used:
                return Decision("tool", name, reason="next frozen tool in fixed order")
        return Decision("stop", reason="fixed read order exhausted")


def load_replay(case_path: Path, manifest_path: Path) -> tuple[VerificationCase, dict[str, ToolSpec], dict[str, str]]:
    input_data = json.loads(case_path.read_text(encoding="utf-8"))
    if set(input_data) != {"schema_version", "tool_manifest_sha256", "case"}:
        raise ValueError("case input contains unexpected or answer-bearing fields")
    if input_data.get("schema_version") != "lv-replay-input-v1":
        raise ValueError("unsupported case schema")
    expected_hash = input_data.get("tool_manifest_sha256")
    actual_hash = file_hash(manifest_path)
    if expected_hash != actual_hash:
        raise ValueError("tool manifest does not match case hash lock")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != {"schema_version", "case_id", "tools"}:
        raise ValueError("tool manifest contains unexpected fields")
    if manifest.get("schema_version") != "lv-replay-tools-v1":
        raise ValueError("unsupported tool manifest schema")
    raw_case = dict(input_data["case"])
    raw_case["claims"] = tuple(Claim(**item) for item in raw_case["claims"])
    raw_case["allowed_tools"] = tuple(raw_case["allowed_tools"])
    case = VerificationCase(**raw_case)
    if manifest.get("case_id") != case.case_id:
        raise ValueError("tool manifest case id mismatch")
    registered = manifest.get("tools")
    if not isinstance(registered, dict) or set(registered) != set(case.allowed_tools):
        raise ValueError("registered tools must equal the case allowlist")
    tools = {}
    for name, entry in registered.items():
        if not isinstance(entry, dict) or set(entry) != {"source_groups", "evidence"}:
            raise ValueError("tool entry may contain only source groups and evidence")
        source_groups = frozenset(entry["source_groups"])
        if not source_groups or any(not isinstance(group, str) or not group for group in source_groups):
            raise ValueError("invalid source group registration")
        found = [Evidence(**item) for item in entry["evidence"]]
        if any(item.source_group not in source_groups for item in found):
            raise ValueError("evidence claims an unregistered source group")
        tools[name] = ToolSpec(name, lambda _case, _args, frozen=found: list(frozen), source_groups)
    return case, tools, {"case_sha256": file_hash(case_path), "tool_manifest_sha256": actual_hash}


def run(case_path: Path, manifest_path: Path, journal_path: Path, policy: str,
        root: Path, handoff_path: Path | None = None) -> dict[str, Any]:
    if handoff_path is not None:
        protected = (root.resolve() / ".local" / "baselines").resolve()
        if handoff_path.resolve().is_relative_to(protected):
            raise ValueError("handoff packet must be outside frozen baselines")
        if handoff_path.resolve() in {case_path.resolve(), manifest_path.resolve(), journal_path.resolve()}:
            raise ValueError("handoff packet cannot overwrite replay input or journal")
    case, tools, hashes = load_replay(case_path, manifest_path)
    if policy == "fixed":
        client = FixedOrderDecisionClient()
    elif policy == "qwen":
        load_local_env(root)
        client = QwenVerificationDecisionClient(
            os.environ["DASHSCOPE_API_KEY"], os.environ["LAND_LLM_BASE_URL"],
            os.environ["LAND_LLM_MODEL"], timeout=20,
        )
    else:
        raise ValueError("unknown decision policy")
    result = VerificationHarness(tools, VerificationJournal(journal_path), client).run(case)
    summary = {
        "scope": "development-only replay; not blinded Gold or location accuracy",
        "policy": policy, "case_id": case.case_id, "input_hashes": hashes,
        "status": result.status, "reason": result.reason,
        "decisions": result.decisions, "tool_calls": len(result.actions),
        "actions": [asdict(item) for item in result.actions],
        "decision_log": [asdict(item) for item in result.decision_log],
        "model_usage_history": getattr(client, "usage_history", []),
    }
    if handoff_path is not None and result.status == "HANDOFF_HUMAN":
        packet = build_handoff_packet(case, result)
        rendered = (json.dumps(packet, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")) + "\n").encode()
        if handoff_path.exists() or handoff_path.is_symlink():
            if handoff_path.is_symlink() or handoff_path.read_bytes() != rendered:
                raise ValueError("existing handoff packet differs from replay")
        else:
            handoff_path.parent.mkdir(parents=True, exist_ok=True)
            handoff_path.write_bytes(rendered)
            handoff_path.chmod(0o600)
        summary["handoff_packet_sha256"] = hashlib.sha256(rendered).hexdigest()
        summary["handoff_packet_path"] = str(handoff_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--tool-manifest", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--policy", choices=("fixed", "qwen"), default="fixed")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, help="optional private JSON run summary")
    parser.add_argument("--handoff-output", type=Path, help="private reviewer packet when handed off")
    args = parser.parse_args()
    protected = (args.root.resolve() / ".local" / "baselines").resolve()
    if args.journal.resolve().is_relative_to(protected):
        raise ValueError("journal must be outside frozen baselines")
    if args.journal.resolve() in {args.case.resolve(), args.tool_manifest.resolve()}:
        raise ValueError("journal cannot overwrite replay input")
    summary = run(args.case, args.tool_manifest, args.journal, args.policy, args.root,
                  args.handoff_output)
    rendered = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    if args.output:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("refusing to overwrite run summary")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered)


if __name__ == "__main__":
    main()
