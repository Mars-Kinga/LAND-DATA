import tempfile
import unittest
import sqlite3
from pathlib import Path

from land_data.location_verification import (
    Claim, Decision, Evidence, ToolSpec, VerificationCase,
    VerificationHarness, VerificationJournal, build_handoff_packet,
)


def case(**overrides):
    values = dict(case_id="c-1", identity_key="venue-1", as_of="2026-10-28T00:00:00Z",
                  claims=(Claim("address", "9 Main Street"),),
                  allowed_tools=("existing", "official"), max_calls=3, max_decisions=5)
    values.update(overrides)
    return VerificationCase(**values)


def evidence(source, value="9 Main Street", identity="venue-1", date="2026-10-27T00:00:00Z"):
    return Evidence("address", value, source, source, date, identity, "sha256:" + source)


class AdaptiveClient:
    def __init__(self, stop_first=False, interrupt_after_first=False):
        self.stop_first = stop_first
        self.interrupt_after_first = interrupt_after_first
        self.calls = 0

    def choose(self, case, found, actions, available):
        self.calls += 1
        if self.stop_first and self.calls == 1:
            return Decision("stop", reason="model believes it is done")
        if self.interrupt_after_first and actions:
            raise KeyboardInterrupt("simulated process interruption")
        return Decision("tool", "existing" if not found else "official")


class VerificationHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = VerificationJournal(Path(self.tmp.name) / "journal.db")

    def harness(self, client, official_value="9 Main Street", official_identity="venue-1"):
        self.tool_calls = []

        def existing(_case, _arguments):
            self.tool_calls.append("existing")
            return [evidence("existing")]

        def official(_case, _arguments):
            self.tool_calls.append("official")
            return [evidence("official", official_value, official_identity)]

        tools = {"existing": ToolSpec("existing", existing, frozenset({"existing"})),
                 "official": ToolSpec("official", official, frozenset({"official"}))}
        return VerificationHarness(tools, self.journal, client)

    def test_decision_uses_evidence_gap_and_resume_does_not_repeat_tools(self):
        client = AdaptiveClient()
        harness = self.harness(client)
        result = harness.run(case())
        self.assertEqual((result.status, result.reason), ("VERIFIED", "INDEPENDENT_EVIDENCE"))
        self.assertEqual(self.tool_calls, ["existing", "official"])
        self.assertEqual(harness.run(case()), result)
        self.assertEqual(self.tool_calls, ["existing", "official"])

    def test_early_stop_is_rejected_by_independent_goal_gate(self):
        harness = self.harness(AdaptiveClient(stop_first=True))
        result = harness.run(case())
        self.assertEqual(result.status, "VERIFIED")
        self.assertEqual(result.decisions, 3)
        self.assertEqual(len(result.actions), 2)
        self.assertEqual([item.kind for item in result.decision_log], ["stop", "tool", "tool"])

    def test_conflict_and_branch_identity_cannot_auto_verify(self):
        conflict = self.harness(AdaptiveClient(), official_value="11 Main Street").run(case())
        self.assertEqual((conflict.status, conflict.reason), ("HANDOFF_HUMAN", "CLAIM_CONFLICT"))
        packet = build_handoff_packet(case(), conflict)
        self.assertEqual(packet["reason"], "CLAIM_CONFLICT")
        self.assertEqual(len(packet["evidence_by_field"]["address"]), 2)
        self.assertIsNone(packet["proposed_correction"])
        self.assertEqual(len(packet["actions"]), 2)
        other = case(case_id="c-2")
        branch = self.harness(AdaptiveClient(), official_identity="venue-2").run(other)
        self.assertEqual((branch.status, branch.reason), ("HANDOFF_HUMAN", "IDENTITY_CONFLICT"))
        self.assertIn("branches", build_handoff_packet(other, branch)["review_questions"][1])
        with self.assertRaisesRegex(ValueError, "matching human-handoff"):
            build_handoff_packet(case(), branch)

    def test_permission_denial_prevents_handler_execution(self):
        class ForbiddenClient:
            def choose(self, *_):
                return Decision("tool", "publish")

        harness = self.harness(ForbiddenClient())
        result = harness.run(case())
        self.assertEqual((result.status, result.reason), ("HANDOFF_HUMAN", "PERMISSION_DENIED"))
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(result.actions[0].status, "DENIED")

    def test_call_budget_hands_off_with_missing_independent_source(self):
        harness = self.harness(AdaptiveClient())
        result = harness.run(case(max_calls=1))
        self.assertEqual((result.status, result.reason), ("HANDOFF_HUMAN", "BUDGET_EXHAUSTED"))
        self.assertEqual(self.tool_calls, ["existing"])

    def test_tool_cannot_claim_an_unregistered_independent_source(self):
        class OfficialClient:
            def choose(self, *_):
                return Decision("tool", "official")

        tools = {"official": ToolSpec("official", lambda *_: [evidence("forged")],
                                      frozenset({"official"}))}
        result = VerificationHarness(tools, self.journal, OfficialClient()).run(
            case(allowed_tools=("official",), max_calls=1))
        self.assertEqual((result.status, result.reason), ("HANDOFF_HUMAN", "BUDGET_EXHAUSTED"))
        self.assertEqual(result.actions[0].status, "ERROR")
        self.assertEqual(result.evidence, ())

    def test_checkpoint_recovers_after_interruption_without_repeating_read(self):
        harness = self.harness(AdaptiveClient(interrupt_after_first=True))
        with self.assertRaisesRegex(KeyboardInterrupt, "simulated process interruption"):
            harness.run(case())
        self.assertEqual(self.tool_calls, ["existing"])
        resumed = VerificationHarness(harness.tools, self.journal, AdaptiveClient())
        result = resumed.run(case())
        self.assertEqual(result.status, "VERIFIED")
        self.assertEqual(self.tool_calls, ["existing", "official"])

    def test_decision_client_error_handoffs_and_is_checkpointed(self):
        class FailingClient:
            calls = 0

            def choose(self, *_):
                self.calls += 1
                raise ValueError("network detail must not enter journal")

        client = FailingClient()
        harness = self.harness(client)
        result = harness.run(case())
        self.assertEqual((result.status, result.reason),
                         ("HANDOFF_HUMAN", "DECISION_CLIENT_ERROR"))
        self.assertEqual(result.actions, ())
        self.assertEqual(result.decisions, 1)
        self.assertEqual(result.decision_log[0].reason, "ValueError")
        self.assertEqual(harness.run(case()), result)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.tool_calls, [])

    def test_stale_evidence_does_not_satisfy_goal(self):
        stale = evidence("old", date="2020-01-01T00:00:00Z")
        self.assertEqual(self.harness(AdaptiveClient()).goal_gate.inspect(case(), (stale,)),
                         ("CONTINUE", "EVIDENCE_GAP:address"))

    def test_reposted_evidence_does_not_count_as_two_independent_sources(self):
        first = Evidence("address", "9 Main Street", "a", "site-a", "2026-10-27T00:00:00Z",
                         "venue-1", "sha256:a", "original-post")
        repost = Evidence("address", "9 Main Street", "b", "site-b", "2026-10-27T00:00:00Z",
                          "venue-1", "sha256:b", "original-post")
        gate = self.harness(AdaptiveClient()).goal_gate
        self.assertEqual(gate.inspect(case(), (first, repost)),
                         ("CONTINUE", "EVIDENCE_GAP:address"))
        independent = Evidence("address", "9 Main Street", "c", "official", "2026-10-27T00:00:00Z",
                               "venue-1", "sha256:c", "official-record")
        self.assertEqual(gate.inspect(case(), (first, repost, independent))[0], "VERIFIED")

    def test_case_cannot_verify_without_required_claim(self):
        with self.assertRaisesRegex(ValueError, "required claim"):
            case(claims=(Claim("address", "9 Main Street", required=False),))
        with self.assertRaisesRegex(ValueError, "age limit"):
            case(max_evidence_age_days=-1)

    def test_coordinate_tolerance_flags_real_distance_not_string_format(self):
        coordinate_case = case(claims=(Claim("coordinates", "40.000000,-73.000000",
                                             tolerance_meters=10),))
        near = (Evidence("coordinates", "40,-73", "a", "a", "2026-10-27T00:00:00Z",
                         "venue-1", "sha256:a"),
                Evidence("coordinates", "40.00005,-73", "b", "b", "2026-10-27T00:00:00Z",
                         "venue-1", "sha256:b"))
        far = near[:1] + (Evidence("coordinates", "40.001,-73", "b", "b",
                                   "2026-10-27T00:00:00Z", "venue-1", "sha256:b"),)
        gate = self.harness(AdaptiveClient()).goal_gate
        self.assertEqual(gate.inspect(coordinate_case, near)[0], "VERIFIED")
        self.assertEqual(gate.inspect(coordinate_case, far), ("HANDOFF_HUMAN", "CLAIM_CONFLICT"))

    def test_zero_zero_coordinate_fails_closed_even_when_sources_agree(self):
        coordinate_case = case(claims=(Claim("coordinates", "0,0", tolerance_meters=10),))
        zero = (Evidence("coordinates", "0,0", "a", "a", "2026-10-27T00:00:00Z",
                         "venue-1", "sha256:a"),
                Evidence("coordinates", "0.0,0.0", "b", "b", "2026-10-27T00:00:00Z",
                         "venue-1", "sha256:b"))
        gate = self.harness(AdaptiveClient()).goal_gate
        self.assertEqual(gate.inspect(coordinate_case, zero),
                         ("HANDOFF_HUMAN", "INVALID_COORDINATE_EVIDENCE"))

    def test_existing_non_journal_database_is_not_modified(self):
        path = Path(self.tmp.name) / "source.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE pois (id TEXT)")
        db.commit()
        db.close()
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "not a verification journal"):
            VerificationJournal(path)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
