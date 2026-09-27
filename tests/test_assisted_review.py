import tempfile
import unittest
from pathlib import Path

from land_data.assisted_review import (
    CODEX_REVIEW_MODE,
    CODEX_REVIEW_POLICY_VERSION,
    codex_assisted_review,
)
from land_data.cli import build_parser
from land_data.models import POI, POICandidate, RawSourceRecord, SourceContent
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import utc_now


class CodexAssistedReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = SQLiteStorage(str(Path(temporary.name) / "assisted-review.db"))
        self.addCleanup(self.storage.close)
        now = utc_now()
        raw = RawSourceRecord(
            id="raw-1", source="official-test", external_id="place-1",
            source_url="https://example.test/places/1", city="test-city",
            payload={"name": "Test Cafe"}, collected_at=now)
        content = SourceContent(
            id="content-1", raw_record_id=raw.id, platform="official-test",
            external_id=raw.external_id, source_url=raw.source_url,
            content_type="DIRECTORY", title="Test Cafe", text="Test Cafe",
            collected_at=now, canonical_url=raw.source_url)
        self.storage.put(raw)
        self.storage.put(content)

    def _poi(self, item_id="poi-1", **kwargs):
        now = utc_now()
        values = dict(
            id=item_id, names={"en": "Test Cafe"}, latitude=40.7,
            longitude=-74.0, city="test-city", country="US", category="cafe",
            created_at=now, updated_at=now, address="1 Main St",
            confidence_score=0.95, review_status="NEEDS_REVIEW",
        )
        values.update(kwargs)
        item = POI(**values)
        self.storage.put(item)
        return item

    def _review(self, target_id, decision="approve", **kwargs):
        values = dict(
            storage=self.storage,
            target_id=target_id,
            decision=decision,
            reviewer_model="codex:gpt-5",
            reason="Codex checked the stored source evidence.",
            evidence_digest="sha256:example-digest",
            evidence_metadata={"input_id": "review-bundle-1", "checked": ["source"]},
        )
        values.update(kwargs)
        return codex_assisted_review(**values)

    def test_approve_records_codex_metadata_and_uses_existing_gates(self):
        poi = self._poi()
        result = self._review(poi.id)

        self.assertEqual(result.new_status, "AUTO_APPROVED")
        self.assertEqual(result.action, "APPROVE")
        self.assertEqual(result.reviewer, "codex:gpt-5")
        self.assertEqual(result.metadata["review_mode"], CODEX_REVIEW_MODE)
        self.assertEqual(result.metadata["model"], "codex:gpt-5")
        self.assertEqual(result.metadata["reviewer_model"], "codex:gpt-5")
        self.assertEqual(result.metadata["evidence_digest"], "sha256:example-digest")
        self.assertEqual(result.metadata["evidence_metadata"]["input_id"], "review-bundle-1")
        self.assertEqual(result.metadata["policy_version"], CODEX_REVIEW_POLICY_VERSION)
        self.assertEqual(self.storage.review_history("POI", poi.id)[0].id, result.id)
        self.assertEqual(self.storage.get(POI, poi.id).review_status, "AUTO_APPROVED")

    def test_duplicate_approval_fails_without_appending_history(self):
        poi = self._poi()
        self._review(poi.id)
        with self.assertRaisesRegex(ValueError, "duplicate review operation"):
            self._review(poi.id)
        self.assertEqual(len(self.storage.review_history("POI", poi.id)), 1)

    def test_reject_records_codex_metadata_and_duplicate_reject_fails(self):
        poi = self._poi()
        result = self._review(poi.id, decision="reject", reason="The source is not reliable.")
        self.assertEqual(result.new_status, "REJECTED")
        self.assertEqual(result.action, "REJECT")
        self.assertEqual(result.metadata["review_mode"], CODEX_REVIEW_MODE)
        with self.assertRaisesRegex(ValueError, "duplicate review operation"):
            self._review(poi.id, decision="reject")
        self.assertEqual(len(self.storage.review_history("POI", poi.id)), 1)

    def test_invalid_reviewer_and_audit_inputs_fail_before_mutation(self):
        poi = self._poi()
        for kwargs, message in (
            ({"reviewer_model": "qwen:latest"}, "reviewer_model must start with codex:"),
            ({"evidence_digest": ""}, "evidence_digest must be nonempty"),
            ({"reason": ""}, "reason must be nonempty"),
            ({"decision": "maybe"}, "decision must be approve or reject"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                self._review(poi.id, **kwargs)
        self.assertEqual(self.storage.review_history("POI", poi.id), [])
        self.assertEqual(self.storage.get(POI, poi.id).review_status, "NEEDS_REVIEW")

    def test_missing_coordinates_and_unresolved_candidate_cannot_be_approved(self):
        missing = self._poi("poi-missing", latitude=None, longitude=None)
        with self.assertRaisesRegex(ValueError, "POI is missing required valid fields"):
            self._review(missing.id)
        self.assertEqual(self.storage.review_history("POI", missing.id), [])

        candidate = POICandidate(
            id="candidate-1", source_content_id="content-1", name="Candidate",
            city="test-city", country="US", category="cafe", created_at=utc_now(),
            latitude=40.7, longitude=-74.0)
        self.storage.put(candidate)
        with self.assertRaisesRegex(ValueError, "unresolved candidate"):
            self._review(candidate.id)
        self.assertEqual(self.storage.review_history("POICandidate", candidate.id), [])

    def test_metadata_must_be_json_compatible(self):
        poi = self._poi()
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            self._review(poi.id, evidence_metadata={"bad": object()})
        self.assertEqual(self.storage.review_history("POI", poi.id), [])

    def test_cli_exposes_explicit_codex_audit_inputs(self):
        args = build_parser().parse_args([
            "codex-review", "approve", "poi-1",
            "--reason", "Checked stored evidence",
            "--evidence-digest", "sha256:test",
        ])
        self.assertEqual(args.command, "codex-review")
        self.assertEqual(args.reviewer_model, "codex:gpt-5")


if __name__ == "__main__":
    unittest.main()
