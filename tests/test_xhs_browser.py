import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from land_data.auth import AuthRequired
from land_data.config import load_config
from land_data.models import RawSourceRecord
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage
from land_data.xhs_browser import (ChromeRemoteBrowser, XHSCaptureAdapter,
                                   capture_to_item, collect_xhs, collection_status,
                                   safe_post_url)


class FakeBrowser:
    def __init__(self, fail_auth=False):
        self.fail_auth = fail_auth
        self.discoveries = 0
        self.captures = 0

    def discover(self, query, posts, scrolls, pause_seconds):
        self.discoveries += 1
        return [{"post_id": "a" * 24,
                 "url": "https://www.xiaohongshu.com/search_result/" + "a" * 24 +
                        "?xsec_token=must-not-persist", "title": "card"}]

    def capture(self, card, query, city, pause_seconds):
        self.captures += 1
        if self.fail_auth:
            raise AuthRequired("AUTH_REQUIRED: xiaohongshu-browser")
        return {"post_id": card["post_id"], "url": card["url"],
                "title": "纽约新咖啡馆", "text": "在纽约喝了一杯咖啡。",
                "author": "local-author", "publish_label": "昨天",
                "hashtags": ["#纽约"], "media_metadata": [{"type": "image"}],
                "query": query, "city": city,
                "captured_at": "2026-09-25T00:00:{:02d}Z".format(self.captures),
                "completeness": "stable-rendered-description"}


class FakePage:
    def __init__(self):
        self.navigated = None

    def navigate(self, url):
        self.navigated = url

    def evaluate(self, expression):
        if "return {url:location.origin" in expression:
            return {"auth_required": False}
        if "const cards=Array.from" in expression:
            return {"auth_required": False, "cards": [{
                "url": "https://www.xiaohongshu.com/explore/" + "f" * 24,
                "title": "card",
            }]}
        return True

    def close(self):
        pass


class FakeRemoteBrowser(ChromeRemoteBrowser):
    def __init__(self, root):
        super().__init__(root)
        self.fake_page = FakePage()

    def page(self):
        return self.fake_page


class XHSBrowserTests(unittest.TestCase):
    def test_discover_opens_normal_search_result_page(self):
        with tempfile.TemporaryDirectory() as temp:
            browser = FakeRemoteBrowser(temp)
            cards = browser.discover("纽约 咖啡馆", 1, 0, 0.5)
        parts = urlsplit(browser.fake_page.navigated)
        self.assertEqual(parts.path, "/search_result")
        self.assertEqual(parse_qs(parts.query)["keyword"], ["纽约 咖啡馆"])
        self.assertEqual(cards[0]["post_id"], "f" * 24)

    def test_discover_accepts_xsec_query_without_persisting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            browser = FakeRemoteBrowser(temp)
            browser.fake_page.evaluate = lambda expression: (
                {"auth_required": False} if "return {url:location.origin" in expression else
                {"auth_required": False, "cards": [{
                    "url": "https://www.xiaohongshu.com/search_result/" + "e" * 24 +
                           "?xsec_token=temporary",
                    "title": "card",
                }]} if "const cards=Array.from" in expression else True)
            cards = browser.discover("纽约", 1, 0, 0.5)
        self.assertEqual(cards[0]["post_id"], "e" * 24)
        self.assertNotIn("xsec_token", cards[0]["url"])

    def test_relative_publish_label_does_not_create_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            browser = FakeBrowser()
            with SQLiteStorage(str(root / "data.db")) as storage:
                pipeline = Pipeline(
                    storage, load_config(str(Path(__file__).resolve().parents[1])))
                sink = lambda item: pipeline.collect("new-york", [XHSCaptureAdapter(item)])
                collect_xhs(storage, root, "new-york", ["纽约 咖啡馆"], browser=browser,
                            sink=sink, pause_seconds=0.5)
                original_capture = browser.capture
                def changed_label(card, query, city, pause_seconds):
                    value = original_capture(card, query, city, pause_seconds)
                    value["publish_label"] = "2天前"
                    return value
                browser.capture = changed_label
                second = collect_xhs(storage, root, "new-york", ["纽约 咖啡馆"],
                                     browser=browser, sink=sink, pause_seconds=0.5)
                raw_count = len(storage.all(RawSourceRecord))
            self.assertEqual(second["duplicate_count"], 1)
            self.assertEqual(raw_count, 1)

    def test_url_removes_all_query_credentials(self):
        url = safe_post_url("https://www.xiaohongshu.com/explore/" + "b" * 24 +
                            "?xsec_token=secret&source=web")
        self.assertEqual(url, "https://www.xiaohongshu.com/explore/" + "b" * 24)

    def test_capture_payload_has_no_html_urls_or_credentials(self):
        capture = FakeBrowser().capture(
            {"post_id": "a" * 24,
             "url": "https://www.xiaohongshu.com/explore/" + "a" * 24},
            "纽约 咖啡馆", "new-york", 0)
        item = capture_to_item(capture)
        self.assertNotIn("xsec_token", item.source_url)
        self.assertNotIn("html", str(item.payload).lower())
        self.assertEqual(item.payload["media_metadata"], [{"type": "image"}])

    def test_collect_checkpoint_and_no_llm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            browser = FakeBrowser()
            seen = []
            with SQLiteStorage(str(root / "data.db")) as storage:
                first = collect_xhs(storage, root, "new-york", ["纽约 咖啡馆"],
                                    browser=browser, sink=seen.append, pause_seconds=0.5)
                checkpoint_json = storage.connection.execute(
                    "SELECT cards_json FROM xhs_query_checkpoints").fetchone()[0]
                second = collect_xhs(storage, root, "new-york", ["纽约 咖啡馆"],
                                     browser=browser, sink=seen.append, pause_seconds=0.5)
                state = collection_status(storage)
            self.assertEqual(first["new_count"], 1)
            self.assertEqual(first["llm_calls"], 0)
            self.assertNotIn("xsec_token", checkpoint_json)
            self.assertEqual(second["duplicate_count"], 1)
            self.assertEqual(browser.discoveries, 2)
            self.assertEqual(browser.captures, 2)
            self.assertEqual(len(seen), 2)
            self.assertEqual(state["capture_versions_seen"], 1)
            self.assertEqual(state["capture_files_retained"], 1)
            self.assertTrue(state["queries"][0]["complete"])

    def test_auth_required_keeps_checkpoint_for_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            browser = FakeBrowser(fail_auth=True)
            with SQLiteStorage(str(root / "data.db")) as storage:
                result = collect_xhs(storage, root, "new-york", ["纽约 酒吧"],
                                     browser=browser, pause_seconds=0.5)
                state = collection_status(storage)
            self.assertEqual(result["status"], "AUTH_REQUIRED")
            self.assertEqual(result["next_run_status"], "RETRY_REQUIRED")
            self.assertEqual(state["queries"][0]["next_index"], 0)
            self.assertFalse(state["queries"][0]["complete"])

    def test_duplicate_capture_does_not_create_second_raw(self):
        config = load_config(str(Path(__file__).resolve().parents[1]))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            browser = FakeBrowser()
            with SQLiteStorage(str(root / "data.db")) as storage:
                pipeline = Pipeline(storage, config)
                sink = lambda item: pipeline.collect("new-york", [XHSCaptureAdapter(item)])
                collect_xhs(storage, root, "new-york", ["纽约 咖啡馆"], browser=browser,
                            sink=sink, pause_seconds=0.5)
                second = collect_xhs(storage, root, "new-york", ["纽约 咖啡馆"], browser=browser,
                                     sink=sink, pause_seconds=0.5)
                self.assertEqual(second["duplicate_count"], 1)
                self.assertEqual(len(storage.all(RawSourceRecord)), 1)


if __name__ == "__main__":
    unittest.main()
