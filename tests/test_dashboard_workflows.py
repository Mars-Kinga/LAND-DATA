import json
import tempfile
import unittest
from pathlib import Path

from land_data.dashboard_workflows import (DashboardWorkflowService,
                                           ensure_dashboard_job_schema)
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import utc_now


ROOT = Path(__file__).resolve().parents[1]


class DashboardWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "dashboard-workflows.db")
        self.service = DashboardWorkflowService(self.db_path, str(ROOT))

    def test_plan_lists_community_and_configured_sources_without_claiming_officiality(self):
        plan = self.service.collection_plan("new-york")
        self.assertTrue(plan["city_exists"])
        names = {item["source"] for item in plan["sources"]}
        self.assertIn("xiaohongshu-browser", names)
        self.assertIn("osm", names)
        xhs = next(item for item in plan["sources"] if item["source"] == "xiaohongshu-browser")
        self.assertEqual(xhs["authority_status"], "community_source_not_official")
        self.assertEqual(plan["llm_policy"]["default"], "never")
        other = self.service.collection_plan("mock-tokyo")
        other_xhs = next(item for item in other["sources"] if item["source"] == "xiaohongshu-browser")
        self.assertFalse(other_xhs["configured"])

    def test_plan_describes_live_community_platforms_without_overstating_dashboard_support(self):
        plan = self.service.collection_plan("new-york")
        self.assertEqual([item["source"] for item in plan["community_platforms"]],
                         ["xiaohongshu-browser", "threads"])

        xhs = plan["community_platforms"][0]
        self.assertTrue(xhs["dashboard_selectable"])
        self.assertTrue(xhs["supports_auto_search"])
        self.assertEqual(xhs["workflow_step"], "xhs_collect")
        self.assertEqual(xhs["llm_default"], "never")
        self.assertIn("capture_dir", xhs["storage"])

        threads = plan["community_platforms"][1]
        self.assertEqual(threads["status"], "AUTH_REQUIRED")
        self.assertTrue(threads["supports_search"])
        self.assertTrue(threads["supports_auto_search"])
        self.assertTrue(threads["dashboard_selectable"])
        self.assertEqual(threads["workflow_step"], "threads_collect")
        self.assertEqual(threads["auth_status"], "oauth_token_missing")
        self.assertIn("LAND_THREADS_ACCESS_TOKEN", threads["entrypoint"] + threads["notes"])
        self.assertEqual(threads["llm_default"], "never")

    def test_parameter_allowlist_and_explicit_qwen_envelope(self):
        with self.assertRaisesRegex(ValueError, "unsupported parameters"):
            self.service.start_job("new-york", "xhs_process", params={"shell": "echo"})
        with self.assertRaisesRegex(ValueError, "llm_opt_in"):
            self.service.start_job("new-york", "knowledge_extract",
                                   candidate_ids=["candidate-1"], model="qwen3.7-flash", llm_limit=1)
        with self.assertRaisesRegex(ValueError, "candidate_ids"):
            self.service.start_job("new-york", "knowledge_extract", llm_opt_in=True,
                                   model="qwen3.7-flash", llm_limit=1)
        with self.assertRaisesRegex(ValueError, "hard llm_limit"):
            self.service.start_job("new-york", "knowledge_extract", llm_opt_in=True,
                                   candidate_ids=["candidate-1"], model="qwen3.7-flash", llm_limit=0)
        with self.assertRaisesRegex(ValueError, "only valid"):
            self.service.start_job("new-york", "xhs_process", params={"llm_mode": "auto"})

    def test_threads_dashboard_job_requires_official_oauth_and_uses_zero_llm(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {}, clear=True):
            job = self.service.start_job(
                "new-york", "threads_collect",
                params={"query": "NYC cafe", "search_type": "RECENT",
                        "limit": 5, "pages": 1, "timeout": 15})
            result = self.service.wait_job(job["id"], timeout=10)
        self.assertEqual(result["status"], "AUTH_REQUIRED")
        self.assertEqual(result["llm_calls"], 0)
        self.assertEqual(result["output_summary"]["sources"], ["threads"])

    def test_deterministic_job_defaults_to_zero_llm_and_is_queryable(self):
        job = self.service.start_job("new-york", "xhs_process")
        result = self.service.wait_job(job["id"], timeout=10)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["llm_calls"], 0)
        self.assertEqual(result["model"], None)
        self.assertEqual(result["progress"], 1.0)
        self.assertIn("counts", result)
        listed = self.service.list_jobs(city="new-york")
        self.assertEqual(listed[0]["id"], job["id"])
        self.assertTrue(Path(result["log_path"]).is_file())

    def test_restart_marks_running_jobs_interrupted(self):
        with SQLiteStorage(self.db_path) as storage:
            ensure_dashboard_job_schema(storage.connection)
            storage.connection.execute(
                """INSERT INTO dashboard_jobs
                   (id,city,step,status,created_at,progress,progress_json,counts_json,
                    output_summary_json,params_json,cancel_requested)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("job-running", "new-york", "xhs_process", "RUNNING", utc_now(), 0.3,
                 json.dumps({"stage": "processing"}), "{}", "{}", "{}", 0),
            )
            storage.connection.commit()
        restarted = DashboardWorkflowService(self.db_path, str(ROOT))
        row = restarted.get_job("job-running")
        self.assertEqual(row["status"], "INTERRUPTED")
        self.assertEqual(row["error"], "dashboard process restarted")
        self.assertIsNotNone(row["ended_at"])

    def test_storage_summary_exposes_locations_and_counts_without_source_body(self):
        summary = self.service.storage_summary("new-york")
        self.assertIn("locations", summary)
        self.assertIn("database", summary["locations"])
        self.assertIn("dashboard_job_log_dir", summary["locations"])
        self.assertIn("dashboard_jobs", summary["counts"])
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("DASHSCOPE_API_KEY", serialized)

    def test_job_log_redacts_secret_like_values(self):
        job = self.service.start_job("new-york", "xhs_process")
        self.service._write_log(job["id"], "TEST", error="Bearer secret-token body should not be retained")
        result = self.service.wait_job(job["id"], timeout=10)
        text = Path(result["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn("secret-token", text)
        self.assertNotIn("body should not be retained", text)


if __name__ == "__main__":
    unittest.main()
