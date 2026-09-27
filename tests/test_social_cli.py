import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from land_data.cli import main


ROOT = Path(__file__).resolve().parents[1]


class SocialCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "social-cli.db"

    def run_cli(self, *args):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--root", str(ROOT), "--db", str(self.db), *args])
        return code, json.loads(output.getvalue())

    def test_list_reports_real_and_extensible_zero_llm_entrypoints(self):
        code, result = self.run_cli("social", "list")
        self.assertEqual(code, 0)
        self.assertEqual(result["llm_calls"], 0)
        providers = {item["provider"] for item in result["platforms"]}
        self.assertEqual(providers, {"xiaohongshu", "threads", "other"})

    def test_browser_visible_import_uses_common_pipeline_and_is_idempotent(self):
        source = Path(self.temp.name) / "reddit-visible.json"
        source.write_text(json.dumps({"records": [{
            "id": "post-1", "url": "https://www.reddit.com/r/travel/comments/post-1",
            "title": "Hanoi cafe", "text": "A visible public travel note",
            "author": "visible-author"
        }]}), encoding="utf-8")
        args = ("social", "import", "--provider", "reddit", "--city", "new-york",
                "--input", str(source), "--mode", "browser")
        first_code, first = self.run_cli(*args)
        second_code, second = self.run_cli(*args)
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first["llm_calls"], 0)
        self.assertEqual(first["collection"]["RawSourceRecord"], 1)
        self.assertEqual(second["collection"]["RawSourceRecord"], 1)


if __name__ == "__main__":
    unittest.main()
