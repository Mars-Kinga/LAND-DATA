import json
import tempfile
import unittest
from pathlib import Path

from land_data.adapters import AdapterCapability, MockSourceAdapter
from land_data.auth import AuthRequired, MockSessionStore
from land_data.policy import SourcePolicy
from land_data.util import normalize_url, safe_source_url


class PolicyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sessions = MockSessionStore(str(Path(self.temp.name) / "sessions.json"))
        self.fixture = Path(self.temp.name) / "fixture.json"
        self.fixture.write_text(json.dumps({"records": [{"city": "mock", "external_id": "1",
            "source_url": "https://example.invalid/1", "payload": {"text": "hello"}}]}), encoding="utf-8")

    def test_capability_is_source_specific(self):
        capability = AdapterCapability("PUBLIC", supports_geo_tag=True)
        self.assertTrue(capability.supports_geo_tag)
        self.assertFalse(capability.supports_search)

    def test_public_adapter_collects_without_session(self):
        adapter = MockSourceAdapter("public", str(self.fixture), AdapterCapability("PUBLIC"), self.sessions)
        self.assertEqual(len(list(adapter.collect("mock"))), 1)

    def test_authenticated_adapter_requires_mock_session(self):
        adapter = MockSourceAdapter("private", str(self.fixture), AdapterCapability("AUTHENTICATED"), self.sessions)
        with self.assertRaises(AuthRequired):
            list(adapter.collect("mock"))
        self.sessions.authenticate("private")
        self.assertEqual(self.sessions.status("private"), "AUTHENTICATED")
        self.assertEqual(len(list(adapter.collect("mock"))), 1)
        self.sessions.revoke("private")
        self.assertEqual(self.sessions.status("private"), "AUTH_REQUIRED")

    def test_invalid_access_mode_rejected(self):
        with self.assertRaises(ValueError):
            MockSourceAdapter("bad", str(self.fixture), AdapterCapability("BYPASS"), self.sessions)

    def test_policy_drops_secrets_and_temporary_fields(self):
        policy = SourcePolicy(("text", "locations", "session_token"), ("text",), ("scratch",), (), 7)
        payload = {"text": "hello", "locations": [{"name": "a", "cookie": "bad"}],
                   "session_token": "bad", "scratch": "temporary"}
        self.assertEqual(policy.retained_payload(payload), {"text": "hello", "locations": [{"name": "a"}]})
        self.assertEqual(policy.temporary_payload(payload), {"scratch": "temporary"})

    def test_policy_rejects_normalized_field_not_retained(self):
        with self.assertRaises(ValueError):
            SourcePolicy(("title",), ("text",))

    def test_url_normalization_and_secret_removal(self):
        self.assertEqual(normalize_url("HTTPS://EXAMPLE.INVALID/a?utm_source=x&id=1#part"),
                         "https://example.invalid/a?id=1")
        self.assertEqual(safe_source_url("https://user:pass@example.invalid/a?token=bad&id=1"),
                         "https://example.invalid/a?id=1")
