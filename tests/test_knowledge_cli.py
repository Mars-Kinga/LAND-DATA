import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from land_data.cli import build_parser, main
from land_data.config import load_config
from land_data.models import RawSourceRecord
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import stable_id, utc_now
from land_data.xhs_processing import process_xhs


class FakeKnowledgeClient:
    model = "fake-knowledge-cli"
    last_usage = None

    def complete(self, _prompt_input):
        self.last_usage = {"prompt_tokens": 4, "completion_tokens": 3,
                           "total_tokens": 7}
        return {
            "scenario": "culture", "topic": "culture",
            "question": "参观时应该注意什么？",
            "direct_answer": None, "actions": [], "success_signals": [],
            "what_ifs": [{"trigger": "场所另有规定", "state": "unknown",
                          "answer": None, "basis": None,
                          "boundary": "需要查看场所的当前官方规定。"}],
            "evidence": [{"field": "text", "quote": "拍照礼仪",
                          "kind": "RULE_OR_DYNAMIC_FACT"}],
            "applicability": {"requirements": {"city": ["new-york"]},
                              "summary": "仅作为纽约旅行文化候选。",
                              "unconfirmed": ["具体场所规定未知。"]},
            "risk": ["场所规则可能不同。"], "verification_required": True,
            "experience_notes": [],
            "facts_requiring_verification": ["具体场所的拍照规定。"],
            "content_gaps": ["原帖没有提供可复用的明确规则。"],
        }


class KnowledgeCLITests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(str(self.root))

    def _candidate(self, db_path):
        now = utc_now()
        payload = {
            "content_type": "POST", "title": "纽约文化提醒",
            "text": "参观时需要注意拍照礼仪。", "hashtags": [],
            "media_metadata": [], "content_status": "ACTIVE",
            "source_metadata": {"query": "纽约 文化 礼仪",
                                "query_group": "culture"},
            "match_known_pois": True,
        }
        record = RawSourceRecord(
            id=stable_id("raw", "xiaohongshu-browser", "z" * 24, payload),
            source="xiaohongshu-browser", external_id="z" * 24,
            source_url="https://www.xiaohongshu.com/explore/" + "z" * 24,
            city="new-york", payload=payload, collected_at=now,
            expires_at=self.config.sources["xiaohongshu-browser"].policy.expiry(now))
        with SQLiteStorage(db_path) as storage:
            storage.put(record)
            process_xhs(storage, self.config, "new-york", llm_mode="never")
            return storage.connection.execute(
                "SELECT id FROM xhs_knowledge_candidates WHERE scenario='culture'").fetchone()[0]

    def test_parser_keeps_legacy_list_and_adds_review_commands(self):
        legacy = build_parser().parse_args(
            ["xhs", "knowledge", "--city", "new-york", "--scenario", "culture"])
        extract = build_parser().parse_args(
            ["xhs", "knowledge", "extract", "--id", "candidate", "--llm", "auto",
             "--limit", "1"])
        self.assertIsNone(legacy.knowledge_command)
        self.assertEqual(extract.knowledge_command, "extract")
        self.assertEqual(extract.id, ["candidate"])

    def test_terminal_extract_list_and_approve_remain_unpublished(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "test.db")
            candidate_id = self._candidate(db_path)
            output = io.StringIO()
            with patch("land_data.cli.qwen_knowledge_from_env",
                       return_value=FakeKnowledgeClient()), redirect_stdout(output):
                code = main(["--db", db_path, "xhs", "knowledge", "extract",
                             "--id", candidate_id, "--llm", "auto", "--limit", "1"])
            extracted = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(extracted["llm_calls"], 1)
            self.assertEqual(extracted["usage"]["total_tokens"], 7)
            draft_id = extracted["draft_ids"][0]

            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--db", db_path, "xhs", "knowledge", "approve", draft_id,
                             "--reviewer", "local-editor", "--reason", "official page checked",
                             "--reference-url", "https://example.gov/photo-policy",
                             "--reference-kind", "official",
                             "--reference-title", "Official photo policy",
                             "--verified-at", "2026-09-26T00:00:00Z",
                             "--valid-until", "2026-10-26T00:00:00Z"])
            approved = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(approved["new_status"], "EDITORIAL_ACCEPTED_FOR_BRIDGE")
            self.assertEqual(approved["publication_state"], "NOT_PUBLISHED")


if __name__ == "__main__":
    unittest.main()
