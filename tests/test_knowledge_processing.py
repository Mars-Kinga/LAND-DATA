import tempfile
import unittest
from pathlib import Path

from land_data.config import load_config
from land_data.knowledge_processing import (
    ACCEPTED_STATUS,
    DRAFT_STATUS,
    KnowledgeProcessingService,
    knowledge_drafts,
    knowledge_review_history,
    review_knowledge_draft,
)
from land_data.models import RawSourceRecord
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import stable_id, utc_now
from land_data.xhs_processing import process_xhs


class FakeKnowledgeClient:
    model = "fake-qau-v1"

    def __init__(self, response=None, error=None):
        self.response = response or valid_draft()
        self.error = error
        self.calls = 0
        self.last_usage = None

    def complete(self, _prompt_input):
        self.calls += 1
        if self.error:
            raise self.error
        self.last_usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        return self.response


def valid_draft(*, quote="当地刷卡更方便", verification=True):
    return {
        "scenario": "money", "topic": "payment", "question": "在当地如何支付？",
        "direct_answer": {"text": "优先准备可用的支付方式。", "basis": "NEEDS_OFFICIAL_VERIFICATION"},
        "actions": [{"text": "出发前确认自己的支付工具。", "basis": "PERSONAL_EXPERIENCE"}],
        "success_signals": [{"text": "交易前已确认可用方式。", "basis": "PERSONAL_EXPERIENCE"}],
        "what_ifs": [{"trigger": "支付失败", "state": "unknown", "answer": None,
                      "basis": None, "boundary": "原帖没有提供可靠的失败处理步骤。"}],
        "evidence": [{"field": "text", "quote": quote, "kind": "RULE_OR_DYNAMIC_FACT"}],
        "applicability": {"requirements": {"city": ["new-york"]},
                          "summary": "适用于该帖描述的旅行支付场景。", "unconfirmed": ["商户接受方式会变化。"]},
        "risk": ["支付接受方式可能变化。"],
        "verification_required": verification,
        "experience_notes": ["该帖作者认为刷卡更方便。"],
        "facts_requiring_verification": ["商户接受方式属于动态事实。"],
        "content_gaps": [],
    }


class KnowledgeProcessingTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(Path(__file__).resolve().parents[1]))

    def _record(self):
        now = utc_now()
        payload = {
            "content_type": "POST", "title": "纽约支付经验", "text": "当地刷卡更方便，现金作为备用。",
            "hashtags": ["纽约旅行"], "media_metadata": [], "content_status": "ACTIVE",
            "source_metadata": {"query": "纽约 支付", "query_group": "money"},
            "match_known_pois": True,
        }
        return RawSourceRecord(
            id=stable_id("raw", "xiaohongshu-browser", "k" * 24, payload),
            source="xiaohongshu-browser", external_id="k" * 24,
            source_url="https://www.xiaohongshu.com/explore/" + "k" * 24,
            city="new-york", payload=payload, collected_at=now,
            expires_at=self.config.sources["xiaohongshu-browser"].policy.expiry(now))

    def _candidate_id(self, storage):
        process_xhs(storage, self.config, "new-york", llm_mode="never")
        return storage.connection.execute("SELECT id FROM xhs_knowledge_candidates").fetchone()[0]

    def test_never_mode_makes_zero_llm_calls(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "test.db")) as storage:
            storage.put(self._record())
            candidate_id = self._candidate_id(storage)
            client = FakeKnowledgeClient()
            result = KnowledgeProcessingService(storage, client).process(
                mode="never", candidate_ids=[candidate_id])
            self.assertEqual(result["llm_calls"], 0)
            self.assertEqual(client.calls, 0)
            self.assertEqual(knowledge_drafts(storage), [])

    def test_auto_success_stores_qau_draft_evidence_usage_and_needs_review(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "test.db")) as storage:
            storage.put(self._record())
            candidate_id = self._candidate_id(storage)
            client = FakeKnowledgeClient()
            result = KnowledgeProcessingService(storage, client).process(
                mode="auto", candidate_ids=[candidate_id], limit=1)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(client.calls, 1)
            self.assertEqual(result["usage"]["total_tokens"], 10)
            draft = knowledge_drafts(storage)[0]
            self.assertEqual(draft["status"], DRAFT_STATUS)
            self.assertTrue(draft["verification_required"])
            self.assertEqual(draft["evidence"][0]["quote"], "当地刷卡更方便")
            attempt = storage.connection.execute(
                "SELECT outcome,usage_json,model FROM xhs_knowledge_attempts").fetchone()
            self.assertEqual(attempt["outcome"], "SUCCEEDED")
            self.assertIn('"total_tokens": 10', attempt["usage_json"])
            self.assertEqual(attempt["model"], client.model)

    def test_out_of_bounds_evidence_rejects_the_whole_draft(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "test.db")) as storage:
            storage.put(self._record())
            candidate_id = self._candidate_id(storage)
            client = FakeKnowledgeClient(valid_draft(quote="模型杜撰的引用"))
            result = KnowledgeProcessingService(storage, client).process(
                mode="auto", candidate_ids=[candidate_id], limit=1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(knowledge_drafts(storage), [])
            attempt = storage.connection.execute(
                "SELECT outcome,error FROM xhs_knowledge_attempts").fetchone()
            self.assertEqual(attempt["outcome"], "FAILED")
            self.assertIn("evidence", attempt["error"])

    def test_model_cannot_change_selected_scenario_or_city_scope(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "test.db")) as storage:
            storage.put(self._record())
            candidate_id = self._candidate_id(storage)
            wrong_scenario = valid_draft()
            wrong_scenario["scenario"] = "transport"
            first = KnowledgeProcessingService(
                storage, FakeKnowledgeClient(wrong_scenario)).process(
                    mode="auto", candidate_ids=[candidate_id], limit=1)
            self.assertEqual(first["failed"], 1)
            wrong_city = valid_draft()
            wrong_city["applicability"]["requirements"]["city"] = ["tokyo"]
            second = KnowledgeProcessingService(
                storage, FakeKnowledgeClient(wrong_city)).process(
                    mode="auto", candidate_ids=[candidate_id], limit=1,
                    retry_errors=True)
            self.assertEqual(second["failed"], 1)
            self.assertEqual(knowledge_drafts(storage), [])

    def test_same_input_is_cached_and_failure_can_retry(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "test.db")) as storage:
            storage.put(self._record())
            candidate_id = self._candidate_id(storage)
            client = FakeKnowledgeClient()
            service = KnowledgeProcessingService(storage, client)
            first = service.process(mode="auto", candidate_ids=[candidate_id], limit=1)
            second = service.process(mode="auto", candidate_ids=[candidate_id], limit=1)
            self.assertEqual(first["processed"], 1)
            self.assertEqual(second["cached"], 1)
            self.assertEqual(client.calls, 1)
            self.assertEqual(len(knowledge_drafts(storage)), 1)

        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "retry.db")) as storage:
            storage.put(self._record())
            candidate_id = self._candidate_id(storage)
            failing = FakeKnowledgeClient(error=TimeoutError("temporary"))
            failed = KnowledgeProcessingService(storage, failing).process(
                mode="auto", candidate_ids=[candidate_id], limit=1)
            recovered = FakeKnowledgeClient()
            cooled = KnowledgeProcessingService(storage, recovered).process(
                mode="auto", candidate_ids=[candidate_id], limit=1)
            retried = KnowledgeProcessingService(storage, recovered).process(
                mode="auto", candidate_ids=[candidate_id], limit=1, retry_errors=True)
            self.assertEqual(failed["failed"], 1)
            self.assertEqual(cooled["deferred"], 1)
            self.assertEqual(cooled["llm_calls"], 0)
            self.assertEqual(retried["processed"], 1)
            self.assertEqual(recovered.calls, 1)
            self.assertEqual(len(knowledge_drafts(storage)), 1)

    def test_missing_answer_is_kept_as_a_content_gap(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "test.db")) as storage:
            storage.put(self._record())
            candidate_id = self._candidate_id(storage)
            draft = valid_draft()
            draft["direct_answer"] = None
            draft["content_gaps"] = ["原帖没有提供可复用的支付规则或处理步骤。"]
            draft["verification_required"] = True
            result = KnowledgeProcessingService(storage, FakeKnowledgeClient(draft)).process(
                mode="auto", candidate_ids=[candidate_id], limit=1)
            self.assertEqual(result["processed"], 1)
            saved = knowledge_drafts(storage)[0]["draft"]["qau_draft"]
            self.assertIsNone(saved["direct_answer"])
            self.assertTrue(saved["content_gaps"])

    def test_review_requires_verification_and_stays_unpublished(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "test.db")) as storage:
            storage.put(self._record())
            candidate_id = self._candidate_id(storage)
            result = KnowledgeProcessingService(storage, FakeKnowledgeClient()).process(
                mode="auto", candidate_ids=[candidate_id], limit=1)
            draft_id = result["draft_ids"][0]
            with self.assertRaisesRegex(ValueError, "official/operator"):
                review_knowledge_draft(storage, draft_id, action="approve", reviewer="editor",
                                       reason="Checked the content")
            decision = review_knowledge_draft(
                storage, draft_id, action="approve", reviewer="editor", reason="Checked the content",
                reference_url="https://example.gov/payments",
                reference_kind="official", reference_title="Official payment guidance",
                verified_at="2026-09-26T00:00:00Z",
                valid_until="2026-10-26T00:00:00Z")
            self.assertEqual(decision["new_status"], ACCEPTED_STATUS)
            self.assertEqual(decision["publication_state"], "NOT_PUBLISHED")
            self.assertEqual(knowledge_drafts(storage)[0]["status"], ACCEPTED_STATUS)
            history = knowledge_review_history(storage, draft_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["reference_url"], "https://example.gov/payments")
            self.assertEqual(history[0]["detail"]["reference_kind"], "official")
            with self.assertRaisesRegex(ValueError, "already received"):
                review_knowledge_draft(storage, draft_id, action="reject", reviewer="editor", reason="late")

    def test_auto_requires_explicit_bounded_candidates(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "test.db")) as storage:
            with self.assertRaisesRegex(ValueError, "explicit candidate ids"):
                KnowledgeProcessingService(storage, FakeKnowledgeClient()).process(mode="auto", limit=1)
            with self.assertRaisesRegex(ValueError, "explicit 1-100 limit"):
                KnowledgeProcessingService(storage, FakeKnowledgeClient()).process(
                    mode="auto", candidate_ids=["candidate"], limit=None)


if __name__ == "__main__":
    unittest.main()
