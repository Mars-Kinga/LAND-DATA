import sqlite3
import tempfile
import unittest
from pathlib import Path

from land_data.models import (Event, EventCandidate, OfficialGuidancePageState,
                              OfficialGuidanceSnapshot, POI, POICandidate,
                              POIMention, RawSourceRecord, SourceContent,
                              SourceDiscoveryCandidate)
from land_data.review import ReviewService
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import stable_id


class ExtendedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = SQLiteStorage(str(Path(self.temp.name) / "review.db"))
        self.addCleanup(self.storage.close)
        self.raw = RawSourceRecord(
            id="raw-review-extended", source="official-test", external_id="post-1",
            source_url="https://example.test/post-1", city="test-city",
            payload={"title": "Test source", "text": "A source record"},
            collected_at="2026-09-26T00:00:00Z")
        self.storage.put(self.raw)
        self.content = SourceContent(
            id="content-review-extended", raw_record_id=self.raw.id,
            platform="official-test", external_id="post-1",
            source_url=self.raw.source_url, content_type="POST", title="Test source",
            text="A source record", collected_at=self.raw.collected_at,
            collection_scope="test-city")
        self.storage.put(self.content)
        self.review = ReviewService(self.storage, 0.7)

    def _event_candidate(self, item_id="event-candidate-1", **kwargs):
        values = dict(
            id=item_id, source_content_id=self.content.id, name="Museum late opening",
            created_at="2026-09-26T00:00:00Z", starts_at="2026-10-01T18:00:00Z",
            ends_at="2026-10-01T20:00:00Z")
        values.update(kwargs)
        item = EventCandidate(**values)
        self.storage.put(item)
        return item

    def _event(self, item_id="event-1", **kwargs):
        values = dict(
            id=item_id, name="Museum late opening", source_content_id=self.content.id,
            created_at="2026-09-26T00:00:00Z", starts_at="2026-10-01T18:00:00Z",
            ends_at="2026-10-01T20:00:00Z")
        values.update(kwargs)
        item = Event(**values)
        self.storage.put(item)
        return item

    def _guidance(self, item_id="guidance-1", **kwargs):
        values = dict(
            id=item_id, source="official-test", page_key="arrival",
            source_url="https://official.example.test/arrival",
            section_location="h2/h3: Arrival", content="Follow the marked signs.",
            content_version="version-1", collected_at="2026-09-26T00:00:00Z")
        values.update(kwargs)
        item = OfficialGuidanceSnapshot(**values)
        self.storage.put(item)
        return item

    def _source(self, item_id="source-1", **kwargs):
        values = dict(
            id=item_id, provider="catalog", dataset_id="abcd-1234",
            title="Official places", domain="data.example.test", publisher="City",
            description="A public dataset", tags=["places"],
            api_endpoint="https://data.example.test/resource/abcd-1234.json",
            updated_at="2026-09-25T00:00:00Z",
            discovered_query="places", discovered_at="2026-09-26T00:00:00Z",
            last_checked_at="2026-09-26T00:00:00Z")
        values.update(kwargs)
        item = SourceDiscoveryCandidate(**values)
        self.storage.put(item)
        return item

    def test_pending_and_list_all_include_non_poi_review_domains(self):
        event_candidate = self._event_candidate()
        event = self._event()
        guidance = self._guidance()
        source = self._source()
        pending = self.review.list()
        self.assertEqual({row["type"] for row in pending["items"]},
                         {"event_candidate", "event", "official_guidance", "source_discovery"})
        all_items = self.review.list_all()
        self.assertEqual(all_items["count"], 4)
        self.assertEqual({row["status"] for row in all_items["items"]}, {"NEEDS_REVIEW", "DISCOVERED"})
        shown_event = self.review.show(event.id)
        self.assertEqual(shown_event["source"]["source_content"]["id"], self.content.id)
        shown_guidance = self.review.show(guidance.id)
        self.assertEqual(shown_guidance["type"], "official_guidance")
        shown_source = self.review.show(source.id)
        self.assertFalse(shown_source["approval_semantics"]["public_content_approved"])
        self.assertEqual(shown_source["detail"]["dataset_id"], "abcd-1234")

    def test_approve_each_domain_records_decision_and_semantics(self):
        event_candidate = self._event_candidate()
        event = self._event()
        guidance = self._guidance()
        source = self._source()
        self.assertEqual(self.review.approve(event_candidate.id).new_status, "AUTO_APPROVED")
        self.assertEqual(self.review.approve(event.id).new_status, "AUTO_APPROVED")
        self.storage.put(OfficialGuidancePageState(
            id="guidance-state", source="official-test", page_key="arrival",
            source_url=guidance.source_url, current_snapshot_id=guidance.id,
            last_checked_at="2026-09-26T00:00:00Z", availability="AVAILABLE",
            parse_status="OK"))
        self.assertEqual(self.review.approve(guidance.id).new_status, "AUTO_APPROVED")
        source_decision = self.review.approve(source.id)
        self.assertEqual((source_decision.action, source_decision.new_status),
                         ("APPROVE_SOURCE", "SUPPORTED"))
        self.assertFalse(source_decision.metadata["public_content_approved"])
        self.assertEqual(self.storage.get(SourceDiscoveryCandidate, source.id).status, "SUPPORTED")
        self.assertEqual(len(self.review.history(source.id)), 1)
        self.assertEqual(self.review.list_all(status="SUPPORTED")["count"], 1)

    def test_approval_constraints_are_strict(self):
        invalid_event = self._event_candidate("event-invalid", ends_at=None)
        with self.assertRaisesRegex(ValueError, "INVALID_EVENT_TIME"):
            self.review.approve(invalid_event.id)
        invalid_guidance = self._guidance("guidance-invalid", source_url="http://official.example.test/arrival")
        with self.assertRaisesRegex(ValueError, "OFFICIAL_GUIDANCE_INVALID_SOURCE"):
            self.review.approve(invalid_guidance.id)
        invalid_source = self._source("source-invalid", api_endpoint="https://other.example.test/data.json")
        with self.assertRaisesRegex(ValueError, "SOURCE_INVALID_ENDPOINT"):
            self.review.approve(invalid_source.id)
        self.assertEqual(self.review.history(invalid_event.id), [])
        self.assertEqual(self.review.history(invalid_guidance.id), [])
        self.assertEqual(self.review.history(invalid_source.id), [])

    def test_rejections_are_duplicate_safe_append_only_and_survive_put(self):
        objects = [self._event_candidate(), self._event(), self._guidance(), self._source()]
        for item in objects:
            decision = self.review.reject(item.id, "Not suitable for this release")
            self.assertEqual(decision.new_status, "REJECTED")
            with self.assertRaisesRegex(ValueError, "duplicate review operation"):
                self.review.reject(item.id, "Again")
            history = self.review.history(item.id)
            self.assertEqual(len(history), 1)
            model = type(item)
            refreshed = self.storage.get(model, item.id)
            if model is SourceDiscoveryCandidate:
                refreshed.status = "DISCOVERED"
            else:
                refreshed.review_status = "NEEDS_REVIEW"
            self.storage.put(refreshed)
            self.assertEqual(self.storage.get(model, item.id).status if model is SourceDiscoveryCandidate
                             else self.storage.get(model, item.id).review_status, "REJECTED")
            with self.assertRaises(sqlite3.IntegrityError):
                self.storage.connection.execute(
                    "UPDATE review_decisions SET action='tampered' WHERE id=?", (decision.id,))
            with self.assertRaises(sqlite3.IntegrityError):
                self.storage.connection.execute("DELETE FROM review_decisions WHERE id=?", (decision.id,))

    def test_rejected_items_cannot_be_approved(self):
        for item in (self._event_candidate(), self._event(), self._guidance(), self._source()):
            self.review.reject(item.id, "Rejected by reviewer")
            with self.assertRaisesRegex(ValueError, "cannot be approved"):
                self.review.approve(item.id)

    def test_custom_reviewer_is_recorded_for_all_actions_and_related_decisions(self):
        approved = self._event_candidate("event-reviewed")
        approved_decision = self.review.approve(approved.id, reviewer="alice")
        self.assertEqual(approved_decision.reviewer, "alice")
        self.assertEqual(self.review.history(approved.id)[0]["reviewer"], "alice")

        rejected = self._event("event-rejected")
        rejected_decision = self.review.reject(rejected.id, "Out of scope", reviewer="bob")
        self.assertEqual(rejected_decision.reviewer, "bob")
        self.assertEqual(self.review.history(rejected.id)[0]["reviewer"], "bob")

        resolve_candidate = POICandidate(
            id="resolve-candidate", source_content_id=self.content.id, name="Resolve Cafe",
            city="test-city", country="US", category="food",
            created_at="2026-09-26T00:00:00Z", latitude=1.0, longitude=2.0,
            confidence_score=0.9)
        resolve_poi = POI(
            id="resolve-poi", names={"und": "Resolve Cafe"}, latitude=1.0, longitude=2.0,
            city="test-city", country="US", category="food",
            created_at="2026-09-26T00:00:00Z", updated_at="2026-09-26T00:00:00Z",
            coordinates_verified=True, review_status="AUTO_APPROVED")
        resolve_mention = POIMention(
            id="resolve-mention", source_content_id=self.content.id,
            relation_type="RECOMMENDATION", created_at="2026-09-26T00:00:00Z",
            poi_candidate_id=resolve_candidate.id, confidence_score=0.9)
        for item in (resolve_candidate, resolve_poi, resolve_mention):
            self.storage.put(item)
        resolve_decision = self.review.resolve(resolve_candidate.id, resolve_poi.id,
                                               reviewer="carol")
        self.assertEqual(resolve_decision.reviewer, "carol")
        self.assertEqual(self.review.history(resolve_mention.id)[0]["reviewer"], "carol")

        promote_candidate = POICandidate(
            id="promote-candidate", source_content_id=self.content.id, name="Promote Cafe",
            city="test-city", country="US", category="food",
            created_at="2026-09-26T00:00:00Z", latitude=3.0, longitude=4.0,
            confidence_score=0.9)
        promote_mention = POIMention(
            id="promote-mention", source_content_id=self.content.id,
            relation_type="RECOMMENDATION", created_at="2026-09-26T00:00:00Z",
            poi_candidate_id=promote_candidate.id, confidence_score=0.9)
        self.storage.put(promote_candidate)
        self.storage.put(promote_mention)
        promote_decision = self.review.promote(promote_candidate.id, reviewer="dave")
        self.assertEqual(promote_decision.reviewer, "dave")
        self.assertEqual(self.review.history(promote_mention.id)[0]["reviewer"], "dave")


if __name__ == "__main__":
    unittest.main()
