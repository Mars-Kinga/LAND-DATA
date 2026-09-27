import json
import tempfile
import unittest
from pathlib import Path

from land_data.config import load_config
from land_data.knowledge_bridge import prepare_release, release_diff, verify_release
from land_data.knowledge_processing import KnowledgeProcessingService, review_knowledge_draft
from land_data.models import RawSourceRecord
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import stable_id, utc_now
from land_data.xhs_processing import process_xhs


# Keep the bridge contract test independent of the adjacent LAND application.
# This fixture is the smallest valid QuestionContentIndex v1 base; the bridge
# adds the reviewed question under test to it.
BASE_INDEX = Path(__file__).resolve().parents[1] / "fixtures" / "question-content-index-base.json"


class FakeClient:
    model = "bridge-test-model"

    def __init__(self, draft):
        self.draft = draft

    def complete(self, _input):
        return self.draft


def draft(*, dynamic=False, gap=False):
    basis = "NEEDS_OFFICIAL_VERIFICATION" if dynamic else "PERSONAL_EXPERIENCE"
    return {
        "scenario": "money", "topic": "payment", "question": "旅行时怎样准备支付方式？",
        "direct_answer": {"text": "先确认自己可用的支付方式。", "basis": basis},
        "actions": [{"text": "在付款前确认商户接受的方式。", "basis": basis}],
        "success_signals": [{"text": "已确认可以完成付款。", "basis": basis}],
        "what_ifs": [{"trigger": "付款失败", "state": "supported", "answer": "改用现场可接受的另一种方式。",
                      "basis": basis, "boundary": "具体接受方式以商户和官方说明为准。"}],
        "evidence": [{"field": "text", "quote": "当地刷卡更方便", "kind": "RULE_OR_DYNAMIC_FACT" if dynamic else "PERSONAL_EXPERIENCE"}],
        "applicability": {"requirements": {"city": ["new-york"]}, "summary": "适用于纽约旅行支付准备。", "unconfirmed": []},
        "risk": [], "verification_required": dynamic,
        "experience_notes": [] if dynamic else ["作者觉得刷卡更方便。"],
        "facts_requiring_verification": ["商户接受的支付方式会变化。"] if dynamic else [],
        "content_gaps": ["原帖没有可发布的完整答案。"] if gap else [],
    }


class KnowledgeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(Path(__file__).resolve().parents[1]))

    def _record(self):
        now = utc_now()
        payload = {"content_type": "POST", "title": "纽约支付经验", "text": "当地刷卡更方便，现金作为备用。", "author_identifier": "测试旅行者",
                   "hashtags": ["纽约旅行"], "media_metadata": [], "content_status": "ACTIVE",
                   "source_metadata": {"query": "纽约 支付", "query_group": "money"}, "match_known_pois": True}
        return RawSourceRecord(id=stable_id("raw", "xiaohongshu-browser", "b" * 24, payload),
                               source="xiaohongshu-browser", external_id="b" * 24,
                               source_url="https://www.xiaohongshu.com/explore/" + "b" * 24,
                               city="new-york", payload=payload, collected_at=now,
                               expires_at=self.config.sources["xiaohongshu-browser"].policy.expiry(now))

    def _accepted(self, storage, value, *, official=False, community=False,
                  verified_at="2026-09-26T00:00:00Z",
                  valid_until="2026-10-26T00:00:00Z"):
        storage.put(self._record())
        process_xhs(storage, self.config, "new-york", llm_mode="never")
        candidate = storage.connection.execute("SELECT id FROM xhs_knowledge_candidates").fetchone()[0]
        draft_id = KnowledgeProcessingService(storage, FakeClient(value)).process(
            mode="auto", candidate_ids=[candidate], limit=1)["draft_ids"][0]
        arguments = {"action": "approve", "reviewer": "editor", "reason": "checked"}
        if official or community:
            arguments.update(reference_url=("https://xiaohongshu.com/explore/example" if community else "https://example.gov/payments"),
                             reference_kind="official", reference_title="Payment guidance",
                             verified_at=verified_at, valid_until=valid_until)
        review_knowledge_draft(storage, draft_id, **arguments)
        return draft_id

    def _prepare(self, storage, root, *, policy):
        return prepare_release(storage, city="new-york", base_index_path=BASE_INDEX, releases_root=root,
                               city_countries={"new-york": "US"}, source_policies={"xiaohongshu-browser": policy})

    def test_personal_experience_compiles_with_adjacent_attribution(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft())
            result = self._prepare(storage, Path(temp) / "releases", policy={
                "export_enabled": False,
                "allow_source_republication": False,
                "allow_experience_derivation": True,
            })
            index = json.loads((Path(result["release_dir"]) / "question-content-index-v1.json").read_text())
            question = index["questions"][-1]
            self.assertEqual(result["compiled"], 1)
            self.assertEqual(len(index["questions"]), 2)
            self.assertEqual(question["supportBindings"][0]["role"], "experience")
            self.assertTrue(question["directAnswer"]["attribution"])
            detail = index["references"][-1]["details"][0]
            self.assertEqual(detail["kind"], "editorial_experience")
            self.assertIsNone(detail["url"])
            self.assertEqual(detail["title"], "匿名旅行者经验")
            self.assertEqual(detail["author"], "LAND 编辑整理")
            self.assertEqual(detail["platform"], "LAND")

            # Derivation carries only the short reviewed answer and the
            # contract-required anonymous attribution detail.  The source
            # post body, evidence quote, URL, author, title, and raw payload
            # fields never enter LAND.
            # The frozen LAND base may contain historical community
            # references.  Scope this privacy assertion to the newly added
            # question/reference payload rather than rewriting that base.
            serialized = json.dumps({"question": question, "reference": index["references"][-1]}, ensure_ascii=False)
            self.assertNotIn("当地刷卡更方便，现金作为备用。", serialized)
            self.assertNotIn("当地刷卡更方便", serialized)
            self.assertNotIn("xiaohongshu.com", serialized)
            self.assertNotIn("测试旅行者", serialized)
            self.assertNotIn("纽约支付经验", serialized)
            self.assertNotIn("b" * 24, serialized)
            self.assertNotIn("author_identifier", serialized)
            self.assertNotIn("source_metadata", serialized)
            verified = verify_release(result["release_dir"], storage=storage,
                                      reviewer="release-editor", reason="checked anonymous experience package")
            self.assertTrue(verified["ok"])

    def test_experience_derivation_requires_explicit_policy_and_acceptance(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            # An accepted draft is still blocked when the new permission is
            # omitted.  This keeps old configs closed by default.
            self._accepted(storage, draft())
            blocked = self._prepare(storage, Path(temp) / "closed", policy={
                # Legacy public-export permission must not silently grant the
                # newer, narrower derivative permission.
                "export_enabled": True,
                "allow_source_republication": False,
            })
            self.assertEqual(blocked["blockers"][0]["code"], "EXPERIENCE_DERIVATION_DISABLED")

        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            # A non-accepted draft never enters the bridge query even when the
            # source policy explicitly permits derivation.
            storage.put(self._record())
            process_xhs(storage, self.config, "new-york", llm_mode="never")
            self.assertEqual(self._prepare(storage, Path(temp) / "unaccepted", policy={
                "allow_experience_derivation": True,
            })["compiled"], 0)

    def test_verbatim_source_material_in_derived_answer_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            leaky = draft()
            leaky["direct_answer"]["text"] = "纽约支付经验"
            self._accepted(storage, leaky)
            result = self._prepare(storage, Path(temp) / "leak", policy={
                "allow_source_republication": False,
                "allow_experience_derivation": True,
            })
            self.assertEqual(result["blockers"][0]["code"], "SOURCE_CONTENT_LEAK")

    def test_dynamic_fact_with_official_verification_compiles_and_verifies(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft(dynamic=True), official=True)
            result = self._prepare(storage, Path(temp) / "releases",
                                   policy={"export_enabled": False, "allow_verified_rules": True})
            index = json.loads((Path(result["release_dir"]) / "question-content-index-v1.json").read_text())
            question = index["questions"][-1]
            self.assertEqual(question["supportBindings"][0]["role"], "rule")
            self.assertEqual(question["supportBindings"][0]["validUntil"], "2026-10-26T00:00:00Z")
            verified = verify_release(result["release_dir"], storage=storage, reviewer="release-editor", reason="checked package")
            self.assertTrue(verified["ok"])

    def test_missing_verification_and_community_rule_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            draft_id = self._accepted(storage, draft())
            row = storage.connection.execute("SELECT data FROM xhs_knowledge_drafts WHERE id=?", (draft_id,)).fetchone()
            payload = json.loads(row[0]); payload["qau_draft"] = draft(dynamic=True)
            storage.connection.execute("UPDATE xhs_knowledge_drafts SET data=? WHERE id=?", (json.dumps(payload), draft_id)); storage.connection.commit()
            missing = self._prepare(storage, Path(temp) / "missing", policy={"export_enabled": True})
            self.assertEqual(missing["blockers"][0]["code"], "OFFICIAL_VERIFICATION_MISSING")
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft(dynamic=True), community=True)
            blocked = self._prepare(storage, Path(temp) / "community", policy={"allow_verified_rules": True})
            self.assertEqual(blocked["blockers"][0]["code"], "OFFICIAL_OR_OPERATOR_REQUIRED")

    def test_content_gap_and_disabled_experience_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft(gap=True))
            result = self._prepare(storage, Path(temp) / "releases", policy={
                "export_enabled": False, "allow_experience_derivation": True})
            self.assertEqual(result["blockers"][0]["code"], "CONTENT_GAP")
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft())
            result = self._prepare(storage, Path(temp) / "releases", policy={})
            self.assertEqual(result["blockers"][0]["code"], "EXPERIENCE_DERIVATION_DISABLED")

    def test_expired_official_verification_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft(dynamic=True), official=True,
                           verified_at="2026-08-01T00:00:00Z",
                           valid_until="2026-09-01T00:00:00Z")
            result = self._prepare(storage, Path(temp) / "releases",
                                   policy={"allow_verified_rules": True})
            self.assertEqual(result["blockers"][0]["code"], "RULE_VERIFICATION_EXPIRED")

    def test_blocked_or_empty_release_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft(gap=True))
            blocked = self._prepare(storage, Path(temp) / "blocked", policy={"export_enabled": True})
            checked = verify_release(blocked["release_dir"], storage=storage,
                                     reviewer="editor", reason="reviewed")
            self.assertFalse(checked["ok"])
            self.assertIn("UNRESOLVED_BLOCKERS", checked["errors"])
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            empty = self._prepare(storage, Path(temp) / "empty", policy={"export_enabled": True})
            checked = verify_release(empty["release_dir"], storage=storage,
                                     reviewer="editor", reason="reviewed")
            self.assertFalse(checked["ok"])
            self.assertIn("EMPTY_RELEASE", checked["errors"])

    def test_same_inputs_are_idempotent_and_tampering_fails_verification(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft(dynamic=True), official=True)
            root = Path(temp) / "releases"
            first = self._prepare(storage, root, policy={"allow_verified_rules": True})
            second = self._prepare(storage, root, policy={"allow_verified_rules": True})
            self.assertFalse(first["idempotent"]); self.assertTrue(second["idempotent"])
            self.assertEqual(release_diff(first["release_dir"])["added"], json.loads((Path(first["release_dir"]) / "manifest.json").read_text())["release_question_ids"])
            index_path = Path(first["release_dir"]) / "question-content-index-v1.json"
            index_path.write_text(index_path.read_text() + " ", encoding="utf-8")
            failed = verify_release(first["release_dir"], storage=storage, reviewer="editor", reason="verify")
            self.assertFalse(failed["ok"])
            self.assertIn("FILE_SHA256:question-content-index-v1.json", failed["errors"])

    def test_installed_draft_is_skipped_on_next_prepare(self):
        with tempfile.TemporaryDirectory() as temp, SQLiteStorage(str(Path(temp) / "db.sqlite")) as storage:
            self._accepted(storage, draft(dynamic=True), official=True)
            releases = Path(temp) / "releases"
            first = self._prepare(storage, releases, policy={"allow_verified_rules": True})
            verified = verify_release(first["release_dir"], storage=storage,
                                      reviewer="release-editor", reason="checked package")
            self.assertTrue(verified["ok"])
            installed_base = Path(first["release_dir"]) / "question-content-index-v1.json"
            second = prepare_release(
                storage, city="new-york", base_index_path=installed_base,
                releases_root=releases, city_countries={"new-york": "US"},
                source_policies={"xiaohongshu-browser": {"allow_verified_rules": True}})
            self.assertEqual(second["compiled"], 0)
            self.assertEqual(second["already_present"], 1)
            self.assertEqual(second["blockers"], [])
            self.assertEqual(release_diff(second["release_dir"])["added"], [])
            manifest = json.loads((Path(second["release_dir"]) / "manifest.json").read_text())
            self.assertEqual(manifest["parent_release_id"], first["release_id"])


if __name__ == "__main__":
    unittest.main()
