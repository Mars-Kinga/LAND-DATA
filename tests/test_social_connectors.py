import tempfile
import unittest
from pathlib import Path

from dataclasses import replace

from land_data.config import load_config
from land_data.models import RawSourceRecord, SourceContent
from land_data.pipeline import Pipeline
from land_data.social_connectors import (
    BrowserVisibleInputProvider,
    InputSocialProvider,
    PublicFeedInputProvider,
    SQLiteSocialState,
    SocialConnectorAdapter,
    SocialConnectorError,
    SocialPage,
    SocialProviderContract,
    SocialRecord,
    canonical_social_url,
    social_source_item,
    stable_social_external_id,
)
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class FailingProvider(InputSocialProvider):
    def __init__(self, contract, pages, *, fail_cursor=None):
        super().__init__(contract, pages, auth_state="PUBLIC")
        self.fail_cursor = fail_cursor

    def fetch_page(self, city, query, cursor, limit):
        if cursor == self.fail_cursor:
            raise TimeoutError("fixture provider timeout")
        return super().fetch_page(city, query, cursor, limit)


class SocialConnectorTests(unittest.TestCase):
    def contract(self, provider="example-social", **kwargs):
        return SocialProviderContract(provider, "PUBLIC_FEED", "public_feed",
                                      supports_public_input=True, **kwargs)

    def record(self, ident, text="Example cafe is lovely", url=None):
        return {"id": ident, "url": url or "https://social.example/posts/" + ident,
                "title": "Example cafe", "text": text,
                "author": "author-1", "hashtags": ["travel"],
                "metadata": {"query_run_id": "ignored", "platform_hint": "fixture"}}

    def test_contract_rejects_unsupported_claims(self):
        with self.assertRaises(ValueError):
            SocialProviderContract("example-social", "PUBLIC_FEED", "public_feed")
        with self.assertRaises(ValueError):
            SocialProviderContract("example-social", "PUBLIC_FEED", "public_feed",
                                   supports_public_input=True, requires_authentication=True)

    def test_source_item_has_platform_auth_and_stable_identity_markers(self):
        contract = self.contract()
        record = SocialRecord.from_mapping(self.record("post-1"))
        item = social_source_item(contract, record, query="cafes", auth_state="PUBLIC")
        self.assertEqual(item.source, "example-social")
        self.assertEqual(item.external_id, "post-1")
        self.assertEqual(item.payload["source_metadata"]["platform"], "example-social")
        self.assertEqual(item.payload["source_metadata"]["auth_state"], "PUBLIC")
        self.assertEqual(item.payload["source_metadata"]["identity_quality"], "PROVIDER_ID")
        self.assertNotIn("access_token", item.payload["source_metadata"])

        url_record = SocialRecord.from_mapping(self.record(
            None, url="https://social.example/posts/url-only?tracking=secret"))
        first = stable_social_external_id("example-social", url_record)
        second = stable_social_external_id("example-social", url_record)
        self.assertEqual(first, second)
        self.assertEqual(first[1], "URL_HASH")

    def test_url_normalization_requires_https_except_local_fixture(self):
        self.assertEqual(canonical_social_url("HTTPS://Social.Example/posts/1?x=y"),
                         "https://social.example/posts/1")
        self.assertEqual(canonical_social_url("http://127.0.0.1:8000/posts/1"),
                         "http://127.0.0.1/posts/1")
        with self.assertRaises(ValueError):
            canonical_social_url("http://social.example/posts/1")

    def test_browser_input_reports_login_state_without_automating_login(self):
        provider = BrowserVisibleInputProvider.from_records(
            "threads", [self.record("post-1")], authenticated=False)
        self.assertEqual(provider.status().state, "AUTH_REQUIRED")
        self.assertIn("login", provider.status().next_action)
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStorage(str(Path(temp) / "state.db"))
            self.addCleanup(storage.close)
            adapter = SocialConnectorAdapter(provider, storage)
            with self.assertRaisesRegex(Exception, "AUTH_REQUIRED: threads"):
                list(adapter.collect("san-francisco"))

    def test_public_pages_checkpoint_and_skip_unchanged_records(self):
        provider = PublicFeedInputProvider.from_records(
            "mastodon", [self.record("post-1"), self.record("post-2"), self.record("post-3")],
            page_size=2)
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStorage(str(Path(temp) / "state.db"))
            self.addCleanup(storage.close)
            adapter = SocialConnectorAdapter(provider, storage, query="cafes", page_size=2)
            first = list(adapter.collect("san-francisco"))
            self.assertEqual([item.external_id for item in first], ["post-1", "post-2", "post-3"])
            second = list(adapter.collect("san-francisco"))
            self.assertEqual(second, [])
            checkpoint = SQLiteSocialState(storage.connection).checkpoint(
                "mastodon", "san-francisco", "cafes")
            self.assertEqual(checkpoint["complete"], 1)
            self.assertEqual(checkpoint["cursor"], None)
            rows = storage.connection.execute(
                "SELECT COUNT(*) FROM social_connector_seen").fetchone()[0]
            self.assertEqual(rows, 3)

    def test_changed_record_is_emitted_with_same_external_id(self):
        first_provider = PublicFeedInputProvider.from_records(
            "bluesky", [self.record("post-1", "old text")])
        changed_provider = PublicFeedInputProvider.from_records(
            "bluesky", [self.record("post-1", "new text")])
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStorage(str(Path(temp) / "state.db"))
            self.addCleanup(storage.close)
            first = list(SocialConnectorAdapter(first_provider, storage).collect("hanoi"))
            second = list(SocialConnectorAdapter(changed_provider, storage).collect("hanoi"))
            self.assertEqual(first[0].external_id, second[0].external_id)
            self.assertEqual(second[0].payload["text"], "new text")
            self.assertEqual(storage.connection.execute(
                "SELECT COUNT(*) FROM social_connector_seen WHERE external_id='post-1'"
            ).fetchone()[0], 1)

    def test_page_failure_preserves_checkpoint_and_resumes(self):
        contract = self.contract("browser-fixture")
        page_one = SocialPage((SocialRecord.from_mapping(self.record("post-1")),), "page-2")
        page_two = SocialPage((SocialRecord.from_mapping(self.record("post-2")),), None)
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStorage(str(Path(temp) / "state.db"))
            self.addCleanup(storage.close)
            failing = FailingProvider(contract, [page_one, page_two], fail_cursor="page-2")
            adapter = SocialConnectorAdapter(failing, storage, page_size=1)
            with self.assertRaisesRegex(TimeoutError, "fixture provider timeout"):
                list(adapter.collect("hanoi"))
            checkpoint = SQLiteSocialState(storage.connection).checkpoint(
                "browser-fixture", "hanoi", None)
            self.assertEqual(checkpoint["cursor"], "page-2")
            self.assertEqual(checkpoint["complete"], 0)

            recovered = InputSocialProvider(contract, [page_one, page_two])
            resumed = list(SocialConnectorAdapter(recovered, storage, page_size=1).collect("hanoi"))
            self.assertEqual([item.external_id for item in resumed], ["post-2"])

    def test_existing_pipeline_receives_source_content_without_llm(self):
        provider = PublicFeedInputProvider.from_records(
            "community-feed", [self.record("post-1", "Example cafe is lovely")])
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStorage(str(Path(temp) / "state.db"))
            self.addCleanup(storage.close)
            config = load_config(str(ROOT))
            config.sources["community-feed"] = replace(
                config.sources["mock-social-2c"], name="community-feed")
            pipeline = Pipeline(storage, config)
            counts = pipeline.run("new-york", [SocialConnectorAdapter(provider, storage)])
            self.assertEqual(counts["RawSourceRecord"], 1)
            self.assertEqual(counts["SourceContent"], 1)
            content = storage.all(SourceContent)[0]
            self.assertEqual(content.platform, "community-feed")
            raw = storage.all(RawSourceRecord)[0]
            self.assertEqual(raw.payload["source_metadata"]["capture_method"], "public_feed")
            self.assertEqual(raw.payload["source_metadata"]["auth_state"], "PUBLIC")


if __name__ == "__main__":
    unittest.main()
