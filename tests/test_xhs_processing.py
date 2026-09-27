import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from land_data.config import load_config
from land_data.models import ContentClassification, RawSourceRecord, SourceContent
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import stable_id, utc_now
from land_data.xhs_processing import (_content_hash, knowledge_candidates, process_xhs,
                                      processing_status)


class EmptyClient:
    model = "offline-test"
    url = "offline-test"

    def __init__(self):
        self.calls = 0
        self.last_usage = None

    def complete(self, _fields):
        self.calls += 1
        self.last_usage = {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}
        return {"locations": [], "events": []}


class FailingClient(EmptyClient):
    def complete(self, _fields):
        self.calls += 1
        raise TimeoutError("temporary")


class XHSProcessingTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(Path(__file__).resolve().parents[1]))

    def record(self, external_id, title, text, query_group=None):
        now = utc_now()
        payload = {
            "content_type": "POST", "title": title, "text": text,
            "hashtags": [], "media_metadata": [], "content_status": "ACTIVE",
            "source_metadata": {"query": "纽约 咖啡馆",
                                "query_group": query_group},
            "match_known_pois": True,
        }
        return RawSourceRecord(
            id=stable_id("raw", "xiaohongshu-browser", external_id, payload),
            source="xiaohongshu-browser", external_id=external_id,
            source_url="https://www.xiaohongshu.com/explore/" + external_id,
            city="new-york", payload=payload, collected_at=now,
            expires_at=self.config.sources["xiaohongshu-browser"].policy.expiry(now))

    def test_default_never_calls_llm_and_keeps_place_and_practical_queues(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                storage.put(self.record("a" * 24, "纽约咖啡馆体验", "这家咖啡馆环境不错，周末去了。"))
                storage.put(self.record("b" * 24, "信用卡支付失败", "该怎么解决？"))
                result = process_xhs(storage, self.config, "new-york")
                self.assertEqual(result["llm_calls"], 0)
                self.assertEqual(result["pending_llm"], 1)
                self.assertEqual(result["knowledge_review"], 1)
                self.assertEqual(result["skipped"], 0)
                self.assertEqual(len(storage.all(SourceContent)), 2)
                self.assertEqual(processing_status(storage)["counts"],
                                 {"KNOWLEDGE_REVIEW": 1, "PENDING_LLM": 1})
                self.assertEqual(result["classifier_counts"],
                                 {"knowledge": 1, "unresolved_place": 1})
                self.assertEqual(len(storage.all(ContentClassification)), 2)

    def test_resume_does_not_add_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                storage.put(self.record("c" * 24, "纽约酒吧", "这家酒吧晚上氛围很好，值得去。"))
                first = process_xhs(storage, self.config, "new-york")
                second = process_xhs(storage, self.config, "new-york")
                attempts = storage.connection.execute(
                    "SELECT COUNT(*) FROM xhs_processing_attempts").fetchone()[0]
                self.assertEqual(first["processed"], 1)
                self.assertEqual(second["processed"], 0)
                self.assertEqual(second["resumed"], 1)
                self.assertEqual(attempts, 1)

    def test_auto_only_resumes_pending_and_records_real_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            client = EmptyClient()
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                storage.put(self.record("d" * 24, "纽约咖啡馆", "这家咖啡馆适合周末去。"))
                process_xhs(storage, self.config, "new-york", llm_mode="never")
                with patch("land_data.xhs_processing.qwen_from_env", return_value=client):
                    result = process_xhs(storage, self.config, "new-york", llm_mode="auto")
                attempts = storage.connection.execute(
                    "SELECT previous_status,new_status,usage_json FROM xhs_processing_attempts "
                    "ORDER BY created_at").fetchall()
                self.assertEqual(result["llm_calls"], 1)
                self.assertEqual(result["usage"]["total_tokens"], 11)
                self.assertEqual(client.calls, 1)
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[-1]["previous_status"], "PENDING_LLM")
                self.assertEqual(attempts[-1]["new_status"], "NEEDS_REVIEW")
                self.assertIn('"total_tokens": 11', attempts[-1]["usage_json"])

    def test_model_network_failure_is_error_not_completed_review(self):
        with tempfile.TemporaryDirectory() as temp:
            client = FailingClient()
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                storage.put(self.record("e" * 24, "纽约咖啡馆", "这家咖啡馆适合周末去。"))
                process_xhs(storage, self.config, "new-york", llm_mode="never")
                with patch("land_data.xhs_processing.qwen_from_env", return_value=client):
                    result = process_xhs(storage, self.config, "new-york", llm_mode="auto")
                self.assertEqual(result["llm_calls"], 1)
                self.assertEqual(result["errors"], 1)
                self.assertEqual(processing_status(storage)["counts"], {"ERROR": 1})

    def test_error_can_be_retried_after_model_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            failed = FailingClient()
            recovered = EmptyClient()
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                storage.put(self.record("1" * 24, "纽约咖啡馆", "这家咖啡馆适合周末去。"))
                with patch("land_data.xhs_processing.qwen_from_env", return_value=failed):
                    process_xhs(storage, self.config, "new-york", llm_mode="auto")
                # A short negative cache prevents tight retry loops. Simulate
                # its expiry before confirming the explicit error retry path.
                storage.connection.execute(
                    "UPDATE llm_extraction_cache SET expires_at='2000-01-01T00:00:00Z'")
                storage.connection.commit()
                with patch("land_data.xhs_processing.qwen_from_env", return_value=recovered):
                    result = process_xhs(storage, self.config, "new-york", llm_mode="auto",
                                         retry_errors=True)
                self.assertEqual(result["processed"], 1)
                self.assertEqual(result["llm_calls"], 1)
                self.assertEqual(result["errors"], 0)
                self.assertEqual(processing_status(storage)["counts"], {"NEEDS_REVIEW": 1})

    def test_content_change_marks_source_content_updated(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                first = self.record("f" * 24, "纽约咖啡馆", "这家咖啡馆环境不错，周末去了。")
                storage.put(first)
                process_xhs(storage, self.config, "new-york")
                second = self.record("f" * 24, "纽约咖啡馆", "这家咖啡馆更新了菜单，周末去了。")
                second.collected_at = "9999-12-31T23:59:59Z"
                storage.put(second)
                process_xhs(storage, self.config, "new-york")
                contents = storage.all(SourceContent)
                self.assertEqual(len(contents), 1)
                self.assertEqual(contents[0].content_status, "UPDATED")

    def test_culture_post_enters_knowledge_review_without_llm(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                storage.put(self.record(
                    "2" * 24, "旧金山文化礼仪", "第一次去旧金山需要了解当地社交礼仪和文化禁忌。",
                    query_group="culture"))
                result = process_xhs(storage, self.config, "new-york", llm_mode="never")
                candidate = storage.connection.execute(
                    "SELECT scenario,review_status FROM xhs_knowledge_candidates").fetchone()
                status = processing_status(storage)
                self.assertEqual(result["knowledge_review"], 1)
                self.assertEqual(result["llm_calls"], 0)
                self.assertEqual(candidate["scenario"], "culture")
                self.assertEqual(candidate["review_status"], "NEEDS_REVIEW")
                self.assertEqual(status["knowledge_counts"], {"culture": 1})
                listing = knowledge_candidates(
                    storage, city="new-york", scenario="culture", limit=10)
                self.assertEqual(len(listing), 1)
                self.assertEqual(listing[0]["scenario"], "culture")
                self.assertIn("社交礼仪", listing[0]["excerpt"])

    def test_culture_post_with_venue_keeps_both_knowledge_and_place_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                storage.put(self.record(
                    "3" * 24, "MOMA博物馆拍照礼仪", "参观MOMA博物馆时也要注意当地拍照礼仪和文化习惯。",
                    query_group="culture"))
                result = process_xhs(storage, self.config, "new-york", llm_mode="never")
                state = storage.connection.execute(
                    "SELECT status FROM xhs_processing_state").fetchone()[0]
                knowledge = storage.connection.execute(
                    "SELECT COUNT(*) FROM xhs_knowledge_candidates").fetchone()[0]
                self.assertEqual(state, "PENDING_LLM")
                self.assertEqual(result["pending_llm"], 1)
                self.assertEqual(result["knowledge_review"], 1)
                self.assertEqual(knowledge, 1)

    def test_multitopic_post_keeps_query_group_and_matching_scenarios(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                storage.put(self.record(
                    "4" * 24, "纽约机场支付和地铁", "落地后从机场到市区建议乘坐地铁，也支持信用卡支付。",
                    query_group="money"))
                result = process_xhs(storage, self.config, "new-york", llm_mode="never")
                rows = storage.connection.execute(
                    "SELECT scenario FROM xhs_knowledge_candidates ORDER BY rowid").fetchall()
                self.assertEqual([row["scenario"] for row in rows],
                                 ["money", "arrival", "transport"])
                self.assertEqual(result["knowledge_review"], 3)
                state = storage.connection.execute(
                    "SELECT route,reason FROM xhs_processing_state").fetchone()
                self.assertEqual((state["route"], state["reason"]),
                                 ("knowledge", "non_poi_money_knowledge"))

    def test_approved_knowledge_candidate_survives_same_content_reprocessing(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                record = self.record("5" * 24, "纽约支付经验", "当地支持信用卡支付，现金作为备用。",
                                     query_group="money")
                storage.put(record)
                process_xhs(storage, self.config, "new-york", llm_mode="never")
                storage.connection.execute(
                    "UPDATE xhs_knowledge_candidates SET review_status='APPROVED'")
                storage.connection.execute(
                    "UPDATE xhs_processing_state SET router_version='legacy'")
                storage.connection.commit()
                result = process_xhs(storage, self.config, "new-york", llm_mode="never")
                candidate = storage.connection.execute(
                    "SELECT review_status,data FROM xhs_knowledge_candidates").fetchone()
                self.assertEqual(result["processed"], 1)
                self.assertEqual(candidate["review_status"], "APPROVED")
                self.assertEqual(json.loads(candidate["data"])["content_hash"],
                                 _content_hash(record))

    def test_changed_knowledge_content_reopens_approved_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                record = self.record("6" * 24, "纽约支付经验", "当地支持信用卡支付，现金作为备用。",
                                     query_group="money")
                storage.put(record)
                process_xhs(storage, self.config, "new-york", llm_mode="never")
                before = storage.connection.execute(
                    "SELECT data FROM xhs_knowledge_candidates").fetchone()["data"]
                storage.connection.execute(
                    "UPDATE xhs_knowledge_candidates SET review_status='APPROVED'")
                record.payload["text"] = "当地支持信用卡支付，现金作为备用，机场也能使用。"
                storage.put(record)
                result = process_xhs(storage, self.config, "new-york", llm_mode="never")
                candidate = storage.connection.execute(
                    "SELECT review_status,data FROM xhs_knowledge_candidates").fetchone()
                self.assertEqual(result["processed"], 1)
                self.assertEqual(candidate["review_status"], "NEEDS_REVIEW")
                self.assertNotEqual(json.loads(before)["content_hash"],
                                    json.loads(candidate["data"])["content_hash"])

    def test_same_raw_id_payload_change_is_reprocessed(self):
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteStorage(str(Path(temp) / "test.db")) as storage:
                record = self.record("7" * 24, "纽约咖啡馆", "这家咖啡馆环境不错，周末去了。")
                storage.put(record)
                first = process_xhs(storage, self.config, "new-york", llm_mode="never")
                first_hash = storage.connection.execute(
                    "SELECT content_hash FROM xhs_processing_state").fetchone()["content_hash"]
                record.payload["text"] = "这家咖啡馆环境不错，周末去了，最近更新了菜单。"
                storage.put(record)
                second = process_xhs(storage, self.config, "new-york", llm_mode="never")
                second_hash = storage.connection.execute(
                    "SELECT content_hash FROM xhs_processing_state").fetchone()["content_hash"]
                attempts = storage.connection.execute(
                    "SELECT COUNT(*) FROM xhs_processing_attempts").fetchone()[0]
                self.assertEqual(first["processed"], 1)
                self.assertEqual(second["processed"], 1)
                self.assertEqual(second["resumed"], 0)
                self.assertEqual(attempts, 2)
                self.assertNotEqual(first_hash, second_hash)


if __name__ == "__main__":
    unittest.main()
