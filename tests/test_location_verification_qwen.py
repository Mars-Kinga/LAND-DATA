import io
import json
import unittest

from land_data.location_verification import Claim, VerificationCase
from land_data.location_verification_qwen import QwenVerificationDecisionClient


class QwenDecisionTests(unittest.TestCase):
    def test_model_proposal_is_constrained_to_registered_tool(self):
        seen = {}

        def opener(request, timeout):
            seen["request"] = json.loads(request.data)
            seen["timeout"] = timeout
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps({
                "kind": "tool", "tool": "official", "reason": "need independent address"
            })}}], "usage": {"total_tokens": 42}}).encode())

        client = QwenVerificationDecisionClient(
            "placeholder-key", "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "test-model", opener=opener,
        )
        case = VerificationCase("case-1", "venue-1", "2026-10-28T00:00:00Z",
                                (Claim("address", "9 Main Street"),), ("official",))
        decision = client.choose(case, (), (), ("official",))
        self.assertEqual((decision.kind, decision.tool), ("tool", "official"))
        self.assertEqual(client.last_usage["total_tokens"], 42)
        self.assertEqual(seen["request"]["model"], "test-model")
        self.assertIn("available_tools", seen["request"]["messages"][1]["content"])
        visible = json.loads(seen["request"]["messages"][1]["content"])
        self.assertEqual(visible["current_goal_gap"], "EVIDENCE_GAP:address")
        self.assertEqual(visible["claim_worklist"][0]["source_groups_seen"], [])
        self.assertTrue(visible["claim_worklist"][0]["needs_more_independent_evidence"])

    def test_unavailable_tool_fails_closed(self):
        def opener(_request, timeout):
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps({
                "kind": "tool", "tool": "publish", "reason": "unsafe"
            })}}]}).encode())

        client = QwenVerificationDecisionClient(
            "placeholder-key", "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "test-model", opener=opener,
        )
        case = VerificationCase("case-2", "venue-2", "2026-10-28T00:00:00Z",
                                (Claim("address", "9 Main Street"),), ("official",))
        with self.assertRaisesRegex(ValueError, "unavailable tool"):
            client.choose(case, (), (), ("official",))


if __name__ == "__main__":
    unittest.main()
