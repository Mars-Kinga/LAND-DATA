import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from land_data.dashboard import (ACCESS_COOKIE, CSRF_HEADER, SESSION_COOKIE,
                                 DashboardApp)
from land_data.models import (Event, EventCandidate, OfficialGuidanceSnapshot, POI,
                              POICandidate, POIMention, RawSourceRecord,
                              SourceContent, SourceDiscoveryCandidate, SourceRunRecord)
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import utc_now
from land_data.xhs_processing import ensure_xhs_schema


ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "dashboard.db")
        self.storage = SQLiteStorage(self.db_path)
        self.addCleanup(self.storage.close)
        now = utc_now()
        raw = RawSourceRecord("raw-dashboard", "mock", "post-dashboard", "https://example.test/post",
                              "test-city", {"title": "Dashboard 内容", "text": "可查看内容"}, now)
        content = SourceContent("content-dashboard", raw.id, "mock", raw.external_id,
                               raw.source_url, "POST", "Dashboard 内容", "可查看内容", now)
        self.storage.put(raw)
        self.storage.put(content)
        self.candidate = POICandidate("candidate-dashboard", content.id, "测试地点", "test-city", "US",
                                      "food", now, latitude=40.7, longitude=-73.9,
                                      review_reasons=["LOW_CONFIDENCE"])
        self.poi = POI("poi-dashboard", {"zh-Hans": "已通过地点"}, 40.71, -73.91,
                        "test-city", "US", "food", now, now, review_status="AUTO_APPROVED")
        self.mention = POIMention("mention-dashboard", content.id, "RECOMMENDS", now,
                                  poi_id=self.poi.id, recommendation_summary="推荐摘要",
                                  review_status="AUTO_APPROVED")
        self.storage.put(self.poi)
        self.storage.put(self.candidate)
        self.storage.put(self.mention)
        self.storage.put(EventCandidate("event-candidate-dashboard", content.id, "测试活动", now))
        self.storage.put(Event("event-dashboard", "已记录活动", content.id, now,
                               review_status="AUTO_APPROVED"))
        self.storage.put(OfficialGuidanceSnapshot(
            "guidance-dashboard", "official", "arrival", "https://example.gov/arrival",
            "h2: Arrival", "正文", "version-1", now))
        self.storage.put(SourceDiscoveryCandidate(
            "discovery-dashboard", "socrata-catalog", "abcd-1234", "测试数据集",
            "data.example.gov", "Publisher", "说明", [], "https://data.example.gov/resource/abcd-1234.json",
            now, "test", now, now, status="NEEDS_REVIEW"))
        self.storage.put(SourceRunRecord("run-dashboard", "multi", "mock", "test", now, now,
                                         status="SUCCEEDED", next_run_status="NOT_NEEDED"))
        self.app = DashboardApp(self.db_path, str(ROOT))

    def test_dashboard_lists_reviewed_and_read_only_records(self):
        result = self.app.list_items({"limit": ["500"]})
        kinds = {item["kind"] for item in result["items"]}
        self.assertTrue({"candidate", "mention", "poi", "event_candidate", "event",
                         "official_guidance_snapshot", "source_discovery_candidate",
                         "source_content", "raw_source_record", "source_run"} <= kinds)
        candidate = next(item for item in result["items"] if item["id"] == self.candidate.id)
        poi = next(item for item in result["items"] if item["id"] == self.poi.id)
        discovery = next(item for item in result["items"] if item["id"] == "discovery-dashboard")
        self.assertEqual(candidate["bucket"], "REVIEW_REQUIRED")
        self.assertEqual(poi["bucket"], "AUTO_ACCEPTED")
        self.assertFalse(discovery["read_only"])
        self.assertEqual(discovery["payload"]["allowed_actions"], ["approve", "reject"])

    def test_local_csrf_and_external_access_token(self):
        sid, csrf, _ = self.app._session()
        with self.assertRaises(PermissionError):
            self.app.authenticate({"Host": "localhost"}, {SESSION_COOKIE: sid}, {}, mutation=True)
        context = self.app.authenticate({"Host": "localhost", CSRF_HEADER: csrf},
                                        {SESSION_COOKIE: sid}, {}, mutation=True)
        self.assertEqual(context["csrf"], csrf)

        external = DashboardApp(self.db_path, str(ROOT), bind_host="0.0.0.0", access_token="access-test")
        with self.assertRaises(PermissionError):
            external.authenticate({"Host": "192.168.1.20"}, {}, {})
        context = external.authenticate({"Host": "192.168.1.20"}, {},
                                        {"access_token": ["access-test"]})
        self.assertTrue(context["redirect"])
        self.assertTrue(any(ACCESS_COOKIE in value and "HttpOnly" in value and "SameSite=Lax" in value
                            for value in context["set_cookies"]))

    def test_review_mutation_uses_service_and_records_dashboard_reviewer(self):
        result = self.app.perform_action({"kind": "candidate", "id": self.candidate.id,
                                          "action": "reject", "reviewer": "editor",
                                          "reason": "地点证据不足"})
        self.assertEqual(result["new_status"], "REJECTED")
        self.assertEqual(result["dashboard_reviewer"], "editor")
        with SQLiteStorage(self.db_path) as storage:
            row = storage.connection.execute(
                "SELECT reviewer,reason FROM dashboard_action_log WHERE id=?", (result["audit_id"],)
            ).fetchone()
            self.assertEqual((row["reviewer"], row["reason"]), ("editor", "地点证据不足"))

    def test_extended_review_types_reuse_review_service(self):
        result = self.app.perform_action({"kind": "source_discovery_candidate",
                                          "id": "discovery-dashboard", "action": "reject",
                                          "reviewer": "source-editor", "reason": "字段不完整"})
        self.assertEqual(result["new_status"], "REJECTED")
        detail = self.app.get_item("source_discovery_candidate", "discovery-dashboard")
        self.assertEqual(detail["dashboard_kind"], "source_discovery_candidate")
        self.assertEqual(detail["service_kind"], "source_discovery")
        self.assertEqual(detail["status"], "REJECTED")

    def test_install_is_disabled_without_explicit_flag(self):
        with self.assertRaisesRegex(PermissionError, "--allow-install"):
            self.app.publish_action({"operation": "install", "release": "missing",
                                     "reviewer": "editor", "reason": "test", "confirm": "INSTALL"})
        with self.assertRaisesRegex(PermissionError, "--allow-install"):
            self.app.publish_action({"content_type": "place", "operation": "rollback",
                                     "release": "place-new-york-test", "reviewer": "editor",
                                     "reason": "test", "confirm": "ROLLBACK"})

    def test_place_publish_routes_bridge_and_freezes_blockers(self):
        self.app.releases_root = Path(self.temp.name) / "place-releases"
        overlay = {
            "city": "new-york",
            "destination": {
                "zh-Hans": {"name": "纽约", "localName": "纽约", "summary": "地点"},
                "en": {"name": "New York", "localName": "New York", "summary": "Places"},
            },
            "places": {"missing-poi": {
                "zh-Hans": {"name": "地点", "localName": "Place", "address": "地址",
                            "reason": "人工整理", "caution": "请查看官方页面"},
                "en": {"name": "Place", "localName": "Place", "address": "Address",
                       "reason": "Editorial copy", "caution": "Check the official page"},
            }},
        }
        prepared = self.app.publish_action({
            "content_type": "place", "operation": "prepare", "city": "new-york",
            "poi_ids": ["missing-poi"], "publication_overlay": overlay,
            "reviewer": "editor", "reason": "prepare place review", "confirm": "PREPARE",
        })
        self.assertEqual(prepared["content_type"], "place")
        self.assertTrue(prepared["blockers"])
        release = prepared["release_id"]
        diff = self.app.publish_action({
            "content_type": "place", "operation": "diff", "release": release,
            "reviewer": "editor", "reason": "inspect place diff", "confirm": "DIFF",
        })
        self.assertEqual(diff["content_type"], "place")
        self.assertEqual(diff["status"], "DRIFTED")
        verified = self.app.publish_action({
            "content_type": "place", "operation": "verify", "release": release,
            "reviewer": "editor", "reason": "verify place package", "confirm": "VERIFY",
        })
        self.assertFalse(verified["ok"])
        self.assertFalse(verified["allow_install"])

    def test_place_prepare_can_omit_overlay_and_returns_actionable_blockers(self):
        self.app.releases_root = Path(self.temp.name) / "place-auto-releases"
        prepared = self.app.publish_action({
            "content_type": "place", "operation": "prepare", "city": "new-york",
            "poi_ids": ["missing-poi"], "confirm": "PREPARE",
        })
        self.assertEqual(prepared["content_type"], "place")
        self.assertEqual(prepared["status"], "PREPARED")
        self.assertTrue(prepared["blockers"])
        self.assertEqual(prepared["approval_mode"], "human_reviewed")

    def test_place_preflight_is_read_only_and_does_not_need_reviewer(self):
        self.app.releases_root = Path(self.temp.name) / "preflight-releases"
        result = self.app.publish_action({
            "content_type": "place", "operation": "preflight", "city": "new-york",
            "poi_ids": [self.poi.id], "confirm": "PREFLIGHT",
        })
        self.assertEqual(result["content_type"], "place")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["release_dir_created"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["items"])
        self.assertFalse(self.app.releases_root.exists())
        with SQLiteStorage(self.db_path) as storage:
            table = storage.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='dashboard_action_log'"
            ).fetchone()
            self.assertIsNone(table)

    def test_beginner_ui_explains_place_and_knowledge_publish_modes(self):
        document = self.app.html("csrf-test").decode("utf-8")
        for text in ("知识内容（QAU）", "地点内容（Place）", "restaurant、cafe、museum",
                     "Event 和官方 Guide", "pub-overlay-json", "pub-template", "回滚已安装包",
                     "publish-preflight", "只读预检", "当前 Dashboard 数据库"):
            self.assertIn(text, document)
        self.assertIn('id="pub-content-type"', document)
        self.assertIn('content_type:place', document)
        self.assertIn("disabled=!place", document)

    def test_llm_review_queue_marks_only_pending_knowledge_candidates(self):
        self.storage.connection.executescript("""
        CREATE TABLE IF NOT EXISTS xhs_knowledge_candidates (
          id TEXT PRIMARY KEY, source_content_id TEXT NOT NULL, city TEXT NOT NULL,
          scenario TEXT NOT NULL, query TEXT NOT NULL, review_status TEXT NOT NULL,
          reason TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL
        );
        """)
        now = utc_now()
        self.storage.connection.execute(
            "INSERT INTO xhs_knowledge_candidates "
            "(id,source_content_id,city,scenario,query,review_status,reason,created_at,updated_at,data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("knowledge-dashboard", "content-dashboard",
             "test-city", "culture", "test query", "NEEDS_REVIEW", "rule", now, now, "{}"),
        )
        self.storage.connection.commit()
        items = self.app.list_items({"bucket": ["REVIEW_REQUIRED"]})["items"]
        knowledge = next(item for item in items if item["id"] == "knowledge-dashboard")
        poi_candidate = next(item for item in items if item["id"] == self.candidate.id)
        self.assertTrue(knowledge["payload"]["llm_eligible"])
        self.assertFalse(poi_candidate["payload"]["llm_eligible"])
        self.assertIn("不支持", poi_candidate["payload"]["llm_disabled_reason"])

    def test_llm_review_queue_rejects_non_knowledge_ids_before_job_creation(self):
        with self.assertRaisesRegex(ValueError, "只能选择当前城市"):
            self.app.workflow_action({
                "operation": "start_job", "confirm": "START_JOB", "city": "test-city",
                "step": "knowledge_extract", "candidate_ids": [self.candidate.id],
                "model": "qwen3.7-flash", "llm_limit": 1, "llm_opt_in": True,
                "reviewer": "dashboard-editor", "reason": "bounded model review",
            })

    def test_llm_review_queue_rejects_cross_city_and_already_decided_candidates(self):
        ensure_xhs_schema(self.storage.connection)
        now = utc_now()
        self.storage.connection.execute(
            "INSERT INTO xhs_knowledge_candidates "
            "(id,source_content_id,city,scenario,query,review_status,reason,created_at,updated_at,data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("knowledge-other-city", "content-dashboard", "other-city", "culture", "q",
             "NEEDS_REVIEW", "rule", now, now, "{}"),
        )
        self.storage.connection.execute(
            "INSERT INTO xhs_knowledge_candidates "
            "(id,source_content_id,city,scenario,query,review_status,reason,created_at,updated_at,data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("knowledge-already-decided", "content-dashboard", "test-city", "safety", "q",
             "APPROVED", "old decision", now, now, "{}"),
        )
        self.storage.connection.commit()
        for candidate_id in ("knowledge-other-city", "knowledge-already-decided"):
            with self.assertRaisesRegex(ValueError, "只能选择当前城市"):
                self.app.workflow_action({
                    "operation": "start_job", "confirm": "START_JOB", "city": "test-city",
                    "step": "knowledge_extract", "candidate_ids": [candidate_id],
                    "model": "qwen3.7-flash", "llm_limit": 1, "llm_opt_in": True,
                    "reviewer": "dashboard-editor", "reason": "bounded model review",
                })

    def test_beginner_ui_exposes_bounded_llm_selection_and_zero_default(self):
        document = self.app.html("csrf-test").decode("utf-8")
        for text in ("默认是 0 次模型调用", "选择当前列表前 N 条", "清空选择",
                     "预计最多调用 0 次", "DRAFT_NEEDS_REVIEW", "start-llm-review"):
            self.assertIn(text, document)
        self.assertIn('id="review-llm-model"', document)
        self.assertIn('id="review-llm-limit"', document)
        self.assertIn('id="llm-pick-count"', document)

    @unittest.skipUnless(shutil.which("node"), "node is required for browser script syntax validation")
    def test_generated_browser_script_has_valid_javascript(self):
        document = self.app.html("csrf-test").decode("utf-8")
        match = re.search(r"<script>(.*)</script>", document, re.DOTALL)
        self.assertIsNotNone(match)
        script_path = Path(self.temp.name) / "dashboard.js"
        script_path.write_text(match.group(1), encoding="utf-8")
        result = subprocess.run([shutil.which("node"), "--check", str(script_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workflow_control_exposes_plan_storage_and_zero_llm_jobs(self):
        plan = self.app.workflow_plan("new-york")
        self.assertTrue(plan["city_exists"])
        self.assertEqual(plan["llm_policy"]["default"], "never")
        storage = self.app.workflow_storage("new-york")
        self.assertEqual(storage["locations"]["database"], str(Path(self.db_path).resolve()))
        started = self.app.workflow_action({
            "operation": "start_job", "confirm": "START_JOB",
            "city": "new-york", "step": "xhs_process", "params": {},
            "reviewer": "dashboard-editor", "reason": "deterministic processing",
        })
        completed = self.app.workflows.wait_job(started["id"], timeout=10)
        self.assertEqual(completed["status"], "SUCCEEDED")
        self.assertEqual(completed["llm_calls"], 0)
        with self.assertRaisesRegex(ValueError, "llm_opt_in"):
            self.app.workflow_action({
                "operation": "start_job", "confirm": "START_JOB",
                "city": "new-york", "step": "knowledge_extract",
                "candidate_ids": ["candidate-1"], "model": "qwen3.7-flash", "llm_limit": 1,
                "reviewer": "dashboard-editor", "reason": "selected extraction",
            })

    def test_beginner_ui_explains_multi_platform_collection_boundaries(self):
        document = self.app.html("csrf-test").decode("utf-8")
        for text in ("选择此平台", "Dashboard 可自动搜索", "从 Threads 搜集公开帖子",
                     "OAuth token 未配置", "保存：", "chooseCommunitySource",
                     "wf-threads-query", "threads_collect"):
            self.assertIn(text, document)

    def test_homepage_registry_status_and_confirmation_boundary(self):
        homepage = self.app.workflow_homepage("new-york")
        self.assertEqual(homepage["status"], "PRESENT")
        self.assertTrue(homepage["ready"])
        self.assertEqual(homepage["codex_policy"]["model"], "gpt-5.6-sol")
        self.assertEqual(homepage["codex_policy"]["reasoning_effort"], "high")
        self.assertEqual(homepage["codex_policy"]["max_calls"], 1)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.app.workflow_action({
                "operation": "request_homepage", "confirm": "REQUEST_HOMEPAGE",
                "city": "new-york", "reviewer": "dashboard-editor",
                "reason": "request homepage",
            })

    def test_beginner_ui_explains_homepage_confirmation_and_fixed_codex_policy(self):
        document = self.app.html("csrf-test").decode("utf-8")
        for text in ("确定让 Codex 生成一个吗？", "REQUEST_HOMEPAGE", "TASK_READY",
                     "gpt-5.6-sol", "workspace-write", "最多 1 次 Codex 作业",
                     "city-home-registry.json", "scaffold / NEEDS_ACCEPTANCE",
                     "function renderPlan(d)"):
            self.assertIn(text, document)


if __name__ == "__main__":
    unittest.main()
