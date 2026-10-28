"""One synthetic, bounded Qwen decision smoke test; never an accuracy score."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from land_data.llm_extraction import load_local_env  # noqa: E402
from land_data.location_verification import (  # noqa: E402
    Claim, Evidence, ToolSpec, VerificationCase, VerificationHarness, VerificationJournal,
)
from land_data.location_verification_qwen import QwenVerificationDecisionClient  # noqa: E402


def main() -> None:
    load_local_env(ROOT)
    client = QwenVerificationDecisionClient(
        os.environ["DASHSCOPE_API_KEY"], os.environ["LAND_LLM_BASE_URL"],
        os.environ["LAND_LLM_MODEL"], timeout=20,
    )
    case = VerificationCase(
        "synthetic-qwen-smoke-v3", "synthetic-venue-1", "2026-10-28T00:00:00Z",
        (Claim("address", "9 Example Street"),),
        ("lookup_existing_poi", "search_official_source"), max_calls=2, max_decisions=3,
    )

    def tool(source: str):
        def read(_case, _arguments):
            return [Evidence("address", "9 Example Street", source, source,
                             "2026-10-27T00:00:00Z", "synthetic-venue-1", "sha256:synthetic-" + source)]
        return read

    tools = {name: ToolSpec(name, tool(name), frozenset({name})) for name in case.allowed_tools}
    private = ROOT / ".local" / "location-verification-live-check" / "harness_pilot"
    private.mkdir(parents=True, exist_ok=True)
    private.chmod(0o700)
    result = VerificationHarness(tools, VerificationJournal(private / "qwen-smoke-v3.db"), client).run(case)
    summary = {
        "scope": "synthetic read-only smoke; not real-location accuracy",
        "status": result.status,
        "reason": result.reason,
        "decisions": result.decisions,
        "tools": [{"tool": action.tool, "status": action.status} for action in result.actions],
        "decision_log": [{"kind": item.kind, "tool": item.tool, "reason": item.reason}
                         for item in result.decision_log],
        "evidence_count": len(result.evidence),
        "last_model_usage": client.last_usage,
        "model_usage_history": client.usage_history,
    }
    output = private / "qwen-smoke-v3-summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    output.chmod(0o600)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
