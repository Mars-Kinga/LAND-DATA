import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from land_data.city_homepage import (CODEX_MODEL, CODEX_REASONING_EFFORT,
                                     _run_homepage_checks,
                                     inspect_city_homepage, launch_codex_task,
                                     request_homepage_task)


class CityHomepageTests(unittest.TestCase):
    def test_fixed_acceptance_checks_compile_core_and_ios_page(self):
        calls = []

        def runner(command, **kwargs):
            from types import SimpleNamespace
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        records = _run_homepage_checks(self.land_root, "homepage-hanoi-safe", runner=runner)
        self.assertEqual([item["command"] for item in records], ["xcrun", "xcodebuild"])
        self.assertIn("swift", calls[0][0])
        self.assertIn("generic/platform=iOS Simulator", calls[1][0])
        self.assertEqual(calls[0][1]["cwd"], str(self.land_root.resolve()))
        self.assertEqual(calls[1][1]["env"].get("DEVELOPER_DIR"),
                         "/Applications/Xcode.app/Contents/Developer")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "LAND-DATA"
        self.root.mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "cities.json").write_text(json.dumps({
            "hanoi": {"display_name": "河内", "country": "VN",
                      "latitude": 21.0278, "longitude": 105.8342},
            "new-york": {"display_name": "New York", "country": "US"},
        }), encoding="utf-8")
        self.land_root = Path(self.temp.name) / "LAND"
        resources = self.land_root / "LAND" / "Resources"
        resources.mkdir(parents=True)
        (resources / "city-home-registry.json").write_text(json.dumps({
            "schemaVersion": 1,
            "cities": [
                {"cityID": "new-york", "reviewState": "ready",
                 "localTitle": "纽约", "englishTitle": "New York"},
                {"cityID": "hanoi", "reviewState": "scaffold",
                 "localTitle": "HÀ NỘI", "englishTitle": "Hanoi"},
            ],
        }), encoding="utf-8")
        (resources / "published-content.json").write_text(json.dumps({
            "destinations": [{"id": "new-york"}], "places": [], "guides": []
        }), encoding="utf-8")

    def test_registry_ready_is_present_and_scaffold_is_preview(self):
        ready = inspect_city_homepage("new-york", land_root=self.land_root)
        self.assertEqual(ready["status"], "PRESENT")
        self.assertTrue(ready["ready"])
        self.assertTrue(ready["published_content_present"])

        scaffold = inspect_city_homepage("hanoi", land_root=self.land_root)
        self.assertEqual(scaffold["status"], "PREVIEW_NEEDS_ACCEPTANCE")
        self.assertTrue(scaffold["homepage_present"])
        self.assertFalse(scaffold["ready"])
        self.assertFalse(scaffold["confirmation_required"])

    def test_published_destination_without_registry_is_not_homepage(self):
        path = self.land_root / "LAND" / "Resources" / "city-home-registry.json"
        path.write_text(json.dumps({"schemaVersion": 1, "cities": []}), encoding="utf-8")
        result = inspect_city_homepage("new-york", land_root=self.land_root)
        self.assertEqual(result["status"], "MISSING")
        self.assertTrue(result["published_content_present"])
        self.assertFalse(result["homepage_present"])

    def test_request_writes_machine_task_with_fixed_codex_policy(self):
        result = request_homepage_task(
            self.root, "hanoi-new", city_profile={"display_name": "河内新城", "country": "VN"},
            land_root=self.land_root, reviewer="mars", reason="新城市首次建档")
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "TASK_READY")
        task = result["task"]
        self.assertEqual(task["status"], "TASK_READY")
        self.assertEqual(task["codex"]["model"], CODEX_MODEL)
        self.assertEqual(task["codex"]["reasoning_effort"], CODEX_REASONING_EFFORT)
        self.assertEqual(task["codex"]["max_calls"], 1)
        self.assertEqual(task["codex"]["qwen_calls"], 0)
        self.assertTrue(task["data_import"]["required_after_collection"])
        self.assertTrue(Path(task["task_path"]).is_file())

    def test_scaffold_cannot_create_duplicate_generation_task(self):
        with self.assertRaisesRegex(ValueError, "scaffold exists"):
            request_homepage_task(
                self.root, "hanoi", city_profile={"display_name": "河内", "country": "VN"},
                land_root=self.land_root, reviewer="mars", reason="检查主页")

    def test_unavailable_runner_keeps_task_ready_and_exposes_command(self):
        result = request_homepage_task(
            self.root, "hanoi-new", city_profile={"display_name": "河内新城", "country": "VN"},
            land_root=self.land_root, reviewer="mars", reason="新城市首次建档")
        task_id = result["task"]["task_id"]
        with mock.patch("land_data.city_homepage._codex_executable", return_value=None):
            task = launch_codex_task(self.root, self.land_root, task_id)
        self.assertEqual(task["status"], "TASK_READY")
        self.assertFalse(task["execution"]["runner_available"])
        self.assertTrue(task["execution"]["command"])
        command = task["execution"]["command"]
        self.assertIn("workspace-write", command)
        self.assertIn(CODEX_MODEL, command)
        self.assertLess(command.index("--no-alt-screen"), command.index("exec"))
        self.assertIn("--approve-for-me", command)
        self.assertNotIn("--add-dir", command)
        snapshot = Path(task["execution"]["task_snapshot_path"])
        self.assertTrue(snapshot.is_file())
        self.assertTrue(snapshot.is_relative_to(self.land_root.resolve()))


if __name__ == "__main__":
    unittest.main()
