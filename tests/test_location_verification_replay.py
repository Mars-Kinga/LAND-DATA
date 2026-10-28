import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO

from land_data.cli import main
from land_data.location_verification_replay import load_replay, run


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "manifest.json"
        self.case = self.root / "case.json"
        self.journal = self.root / "journal.db"
        def item(source):
            return {"claim_field": "address", "value": "9 Example Street",
                    "source_id": source, "source_group": source,
                    "observed_at": "2026-10-27T00:00:00Z", "identity_key": "venue-1",
                    "content_hash": "sha256:" + source}
        payload = {"schema_version": "lv-replay-tools-v1", "case_id": "case-1",
                   "tools": {"existing": {"source_groups": ["existing"], "evidence": [item("existing")]},
                             "official": {"source_groups": ["official"], "evidence": [item("official")]}}}
        self.manifest.write_text(json.dumps(payload, sort_keys=True))
        case_data = {"schema_version": "lv-replay-input-v1",
                     "tool_manifest_sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                     "case": {"case_id": "case-1", "identity_key": "venue-1",
                              "as_of": "2026-10-28T00:00:00Z",
                              "claims": [{"field": "address", "value": "9 Example Street"}],
                              "allowed_tools": ["existing", "official"],
                              "max_calls": 2, "max_decisions": 3}}
        self.case.write_text(json.dumps(case_data, sort_keys=True))

    def test_hash_locked_replay_is_idempotent(self):
        first = run(self.case, self.manifest, self.journal, "fixed", self.root)
        second = run(self.case, self.manifest, self.journal, "fixed", self.root)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "VERIFIED")
        self.assertEqual(first["tool_calls"], 2)
        self.assertEqual(len(first["decision_log"]), 2)

    def test_manifest_tampering_or_source_group_forgery_fails_before_tool_call(self):
        payload = json.loads(self.manifest.read_text())
        payload["tools"]["official"]["evidence"][0]["value"] = "11 Example Street"
        self.manifest.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "hash lock"):
            load_replay(self.case, self.manifest)
        input_data = json.loads(self.case.read_text())
        input_data["tool_manifest_sha256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.case.write_text(json.dumps(input_data))
        payload["tools"]["official"]["evidence"][0]["source_group"] = "forged"
        self.manifest.write_text(json.dumps(payload))
        input_data["tool_manifest_sha256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.case.write_text(json.dumps(input_data))
        with self.assertRaisesRegex(ValueError, "unregistered source group"):
            load_replay(self.case, self.manifest)

    def test_answer_bearing_case_field_is_rejected(self):
        payload = json.loads(self.case.read_text())
        payload["gold_action"] = "KEEP"
        self.case.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "answer-bearing"):
            load_replay(self.case, self.manifest)

    def test_main_cli_runs_hash_locked_replay_without_app_db(self):
        output = StringIO()
        root = Path(__file__).resolve().parents[1]
        with redirect_stdout(output):
            code = main(["--root", str(root), "--db", str(self.root / "must-not-exist.db"),
                         "location-verify", "--case", str(self.case),
                         "--tool-manifest", str(self.manifest),
                         "--journal", str(self.journal), "--policy", "fixed"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "VERIFIED")
        self.assertFalse((self.root / "must-not-exist.db").exists())

    def test_handoff_packet_is_private_and_idempotent(self):
        data = json.loads(self.case.read_text())
        data["case"]["max_calls"] = 1
        self.case.write_text(json.dumps(data, sort_keys=True))
        packet = self.root / "review" / "handoff.json"
        first = run(self.case, self.manifest, self.journal, "fixed", self.root, packet)
        self.assertEqual((first["status"], first["reason"]),
                         ("HANDOFF_HUMAN", "BUDGET_EXHAUSTED"))
        self.assertEqual(packet.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(packet.read_text())["proposed_correction"], None)
        original = packet.read_bytes()
        self.assertEqual(run(self.case, self.manifest, self.journal, "fixed", self.root, packet), first)
        self.assertEqual(packet.read_bytes(), original)
        packet.write_text("changed")
        with self.assertRaisesRegex(ValueError, "differs from replay"):
            run(self.case, self.manifest, self.journal, "fixed", self.root, packet)

    def test_handoff_cannot_overwrite_case_or_journal(self):
        with self.assertRaisesRegex(ValueError, "cannot overwrite"):
            run(self.case, self.manifest, self.journal, "fixed", self.root, self.case)
        with self.assertRaisesRegex(ValueError, "cannot overwrite"):
            run(self.case, self.manifest, self.journal, "fixed", self.root, self.journal)

    def test_main_cli_lists_handoff_without_opening_app_db(self):
        data = json.loads(self.case.read_text())
        data["case"]["max_calls"] = 1
        self.case.write_text(json.dumps(data, sort_keys=True))
        run(self.case, self.manifest, self.journal, "fixed", self.root)
        output = StringIO()
        root = Path(__file__).resolve().parents[1]
        with redirect_stdout(output):
            code = main(["--root", str(root), "--db", str(self.root / "must-not-exist.db"),
                         "location-verify-queue", "--journal", str(self.journal)])
        self.assertEqual(code, 0)
        queue = json.loads(output.getvalue())
        self.assertEqual(queue["count"], 1)
        self.assertEqual(queue["cases"][0]["reason"], "BUDGET_EXHAUSTED")
        self.assertNotIn("evidence", queue["cases"][0])
        self.assertFalse((self.root / "must-not-exist.db").exists())


if __name__ == "__main__":
    unittest.main()
