import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import land_data.nyc_parks_events as nyc_parks_events_module
from land_data.config import load_config
from land_data.models import (Event, EventCandidate, FieldProvenance, POI,
                              RawSourceRecord, RecommendationSignal, SourceContent)
from land_data.multi_source import MultiSourceRunner, MultiSourceSpec
from land_data.nyc_parks_events import (EVENT_TIMEZONE, NYCParksEventsAdapter,
                                        parse_nyc_event_datetime)
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


# The adapter marks events relative to its collection clock.  Keep the clock
# before the fixture event so lifecycle assertions stay stable over time.
REFERENCE_NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
DEFAULT_EVENT_START = (REFERENCE_NOW + timedelta(days=1, hours=10)).strftime(
    "%Y-%m-%dT%H:%M:%S.000")
DEFAULT_EVENT_END = (REFERENCE_NOW + timedelta(days=1, hours=11)).strftime(
    "%Y-%m-%dT%H:%M:%S.000")


class Response:
    def __init__(self, rows):
        import json
        self.body = json.dumps(rows).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read(self, _limit):
        return self.body


def event_row(guid, title=None, *, start=DEFAULT_EVENT_START,
              end=DEFAULT_EVENT_END, coords="40.7000,-74.0000"):
    return {
        "guid": str(guid), "title": title or "Park event {}".format(guid),
        "instructor": "Parks Team", "link": "https://www.nycgovparks.org/events/{}".format(guid),
        "description": "A public park event.",
        "registration_url": "https://www.nycgovparks.org/register/{}".format(guid),
        "registration_description": "Registration details", "parkids": "M001;M002",
        "parknames": "Example Park;Second Park", "starttime": start, "endtime": end,
        "contact_phone": "212-555-0100", "location": "Main lawn", "categories": "Nature;Arts",
        "coordinates": coords,
    }


class NYCParksEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = SQLiteStorage(str(Path(self.temp.name) / "events.db"))
        self.addCleanup(self.storage.close)
        self.clock_tick = 0
        self.clock_patch = patch.object(
            nyc_parks_events_module, "utc_now", side_effect=self._utc_now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.config = load_config(str(ROOT))
        self.pipeline = Pipeline(self.storage, self.config)

    def _utc_now(self):
        """Return deterministic, distinct timestamps for each snapshot call."""
        self.clock_tick += 1
        value = REFERENCE_NOW + timedelta(seconds=self.clock_tick)
        return value.isoformat().replace("+00:00", "Z")

    def runner(self, batches, *, limit=2, max_pages=20):
        calls = []
        remaining = list(batches)

        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            calls.append(query)
            value = remaining.pop(0)
            if isinstance(value, Exception):
                raise value
            return Response(value)

        adapter = NYCParksEventsAdapter(limit=limit, max_pages=max_pages,
                                        opener=opener, storage=self.storage,
                                        collection_key="new-york", max_retries=0)
        runner = MultiSourceRunner(
            self.pipeline, self.storage,
            [MultiSourceSpec("nyc-parks-events", "new-york", limit=limit, max_pages=max_pages)],
            lambda _spec: adapter)
        return runner, calls

    def test_datetime_explicitly_uses_new_york_and_invalid_is_not_guessed(self):
        self.assertEqual(parse_nyc_event_datetime("2026-01-15T10:00:00.000"),
                         "2026-01-15T15:00:00Z")
        self.assertEqual(parse_nyc_event_datetime("not-a-time"), None)

    def test_new_duplicate_update_and_provenance_without_poi_or_signals(self):
        rows = [event_row("1"), event_row("2"), event_row("3")]
        first, calls = self.runner([rows[:2], rows[2:], []])
        outcome = first.run()["sources"][0]
        self.assertEqual((outcome["status"], outcome["new_count"], outcome["duplicate_count"]),
                         ("COMPLETE", 3, 0))
        self.assertEqual(len(self.storage.all(EventCandidate)), 3)
        self.assertEqual(len(self.storage.all(Event)), 3)
        self.assertEqual(len(self.storage.all(POI)), 0)
        self.assertEqual(len(self.storage.all(RecommendationSignal)), 0)
        self.assertTrue(all(item.timezone == EVENT_TIMEZONE for item in self.storage.all(Event)))
        self.assertTrue(all(item.entity_type == "EventCandidate"
                            for item in self.storage.all(FieldProvenance)))
        self.assertIn("guid > 2", calls[1]["$where"][0])

        replay, _ = self.runner([rows[:2], rows[2:], []])
        replay_outcome = replay.run()["sources"][0]
        self.assertEqual((replay_outcome["new_count"], replay_outcome["updated_count"],
                          replay_outcome["duplicate_count"]), (0, 0, 3))
        self.assertEqual(len(self.storage.all(RawSourceRecord)), 3)

        changed = [event_row("1", "Updated park event"), rows[1], rows[2]]
        update, _ = self.runner([changed[:2], changed[2:], []])
        update_outcome = update.run()["sources"][0]
        self.assertEqual((update_outcome["new_count"], update_outcome["updated_count"],
                          update_outcome["duplicate_count"]), (0, 1, 2))
        candidate = next(item for item in self.storage.all(EventCandidate)
                         if item.source_event_id == "1")
        self.assertEqual(candidate.name, "Updated park event")
        self.assertIn("EVENT_SOURCE_UPDATED", candidate.review_reasons)

    def test_invalid_time_and_coordinate_remain_reviewable(self):
        invalid = event_row("bad", start="bad", end=DEFAULT_EVENT_END,
                            coords="95.0,-74.0")
        runner, _ = self.runner([[invalid], []], limit=2)
        result = runner.run()["sources"][0]
        self.assertEqual(result["status"], "COMPLETE")
        candidate = self.storage.all(EventCandidate)[0]
        self.assertIsNone(candidate.starts_at)
        self.assertIn("INVALID_EVENT_TIME", candidate.review_reasons)
        self.assertIn("INVALID_EVENT_COORDINATES", candidate.review_reasons)
        self.assertEqual(self.storage.all(Event), [])

    def test_partial_failure_then_resume_does_not_retract_first_page(self):
        first, _ = self.runner([[event_row("1"), event_row("2")], OSError("offline")], limit=2)
        failed = first.run()["sources"][0]
        self.assertEqual((failed["status"], failed["next_run_status"]), ("FAILED", "RETRY_REQUIRED"))
        self.assertEqual(self.storage.all(EventCandidate), [])
        resumed, _ = self.runner([[event_row("3")], []], limit=2)
        recovered = resumed.run()["sources"][0]
        self.assertEqual(recovered["status"], "COMPLETE")
        self.assertEqual({item.source_event_id for item in self.storage.all(EventCandidate)}, {"1", "2", "3"})
        self.assertTrue(all(item.lifecycle_status == "ACTIVE" for item in self.storage.all(EventCandidate)))

    def test_complete_snapshot_absence_and_reappearance(self):
        initial_rows = [event_row("1"), event_row("2")]
        first, _ = self.runner([initial_rows, []], limit=2)
        self.assertEqual(first.run()["sources"][0]["status"], "COMPLETE")

        missing, _ = self.runner([[event_row("1")], []], limit=2)
        self.assertEqual(missing.run()["sources"][0]["status"], "COMPLETE")
        gone = next(item for item in self.storage.all(EventCandidate) if item.source_event_id == "2")
        self.assertEqual(gone.lifecycle_status, "UNAVAILABLE")
        self.assertIn("EVENT_MISSING_FROM_COMPLETE_SNAPSHOT", gone.review_reasons)

        returned, _ = self.runner([[event_row("1"), event_row("2")], []], limit=2)
        self.assertEqual(returned.run()["sources"][0]["status"], "COMPLETE")
        back = next(item for item in self.storage.all(EventCandidate) if item.source_event_id == "2")
        self.assertEqual(back.lifecycle_status, "ACTIVE")
        self.assertIn("EVENT_REAPPEARED", back.review_reasons)

    def test_expired_event_is_not_marked_unavailable(self):
        past = event_row("past", start="2020-01-01T10:00:00.000", end="2020-01-01T11:00:00.000")
        runner, _ = self.runner([[past], []], limit=2)
        self.assertEqual(runner.run()["sources"][0]["status"], "COMPLETE")
        candidate = self.storage.all(EventCandidate)[0]
        event = self.storage.all(Event)[0]
        self.assertEqual(candidate.lifecycle_status, "EXPIRED")
        self.assertEqual(event.lifecycle_status, "EXPIRED")
        self.assertNotIn("EVENT_MISSING_FROM_COMPLETE_SNAPSHOT", candidate.review_reasons)


if __name__ == "__main__":
    unittest.main()
