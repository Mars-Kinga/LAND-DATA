import tempfile
import unittest
from pathlib import Path

from land_data.config import load_config
from land_data.jfk_guidance import JFKArrivalGuidanceAdapter
from land_data.models import OfficialGuidancePageState, OfficialGuidanceSnapshot
from land_data.multi_source import MultiSourceRunner, MultiSourceSpec
from land_data.pipeline import Pipeline
from land_data.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, html): self.html = html
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, _limit): return self.html.encode()


PUBLIC = """<h2>Navigate JFK with the AirTrain</h2><p>The AirTrain connects passengers to terminals, airport parking lots, the hotel shuttle area, the rental car center, and public transportation.</p>"""
AIRTRAIN = """<h2>AirTrain is the best way to navigate JFK</h2><p>JFK’s AirTrain connects you to parking areas, the Long Island Rail Road (LIRR), New York City Subway and local buses.</p><h2>AirTrain connections</h2><h3>To and from NYC public transportation</h3><p>You can access public transportation by taking the AirTrain to Jamaica or Howard Beach Station.</p>"""


class JFKGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = SQLiteStorage(str(Path(self.temp.name) / "test.db"))
        self.addCleanup(self.storage.close)
        self.pipeline = Pipeline(self.storage, load_config(str(ROOT)))

    def runner(self, opener):
        return MultiSourceRunner(self.pipeline, self.storage,
                                 [MultiSourceSpec("jfk-arrival-guidance", "jfk-guidance", timeout=15)],
                                 lambda _spec: JFKArrivalGuidanceAdapter(opener=opener))

    def test_replaced_body_creates_version_without_treating_page_as_unavailable(self):
        def first(request, timeout):
            return Response(PUBLIC if "public-transport" in request.full_url else AIRTRAIN)
        initial = self.runner(first).run()["sources"][0]
        self.assertEqual((initial["new_count"], initial["updated_count"]), (3, 0))
        replay = self.runner(first).run()["sources"][0]
        self.assertEqual((replay["new_count"], replay["updated_count"], replay["duplicate_count"]), (0, 0, 3))

        replaced_public = """<h2>Navigate JFK with the AirTrain</h2><p>The AirTrain links airport terminals and public transportation connections.</p>"""
        def changed(request, timeout):
            if "public-transport" in request.full_url:
                return Response(replaced_public)
            return Response(AIRTRAIN)
        update = self.runner(changed).run()["sources"][0]
        self.assertEqual((update["new_count"], update["updated_count"], update["duplicate_count"]), (0, 1, 2))
        self.assertEqual(len(self.storage.all(OfficialGuidanceSnapshot)), 4)
        public = next(item for item in self.storage.all(OfficialGuidancePageState)
                      if item.page_key == "public-transport")
        self.assertEqual((public.availability, public.parse_status), ("AVAILABLE", "OK"))

    def test_one_readable_page_can_have_changed_and_parse_error_sections(self):
        def first(request, timeout):
            return Response(PUBLIC if "public-transport" in request.full_url else AIRTRAIN)
        self.runner(first).run()
        multi_status_airtrain = """<h2>AirTrain is the best way to navigate JFK</h2><p>JFK’s AirTrain links travelers to public transportation connections.</p><h2>AirTrain connections</h2><p>Editorial page restructure.</p>"""
        def restructured(request, timeout):
            return Response(PUBLIC if "public-transport" in request.full_url else multi_status_airtrain)
        result = self.runner(restructured).run()
        self.assertEqual(result["status"], "SUCCEEDED_WITH_REVIEW")
        outcome = result["sources"][0]
        self.assertEqual((outcome["status"], outcome["updated_count"], outcome["duplicate_count"]),
                         ("SUCCEEDED_WITH_REVIEW", 1, 1))
        states = {item.page_key: item for item in self.storage.all(OfficialGuidancePageState)}
        self.assertEqual((states["airtrain-connections"].availability,
                          states["airtrain-connections"].parse_status), ("AVAILABLE", "OK"))
        self.assertEqual((states["airtrain-public-transit"].availability,
                          states["airtrain-public-transit"].parse_status), ("AVAILABLE", "PARSE_ERROR"))
        self.assertIsNotNone(states["airtrain-public-transit"].current_snapshot_id)

    def test_unavailable_page_preserves_snapshots_and_recovery_resumes(self):
        def first(request, timeout):
            return Response(PUBLIC if "public-transport" in request.full_url else AIRTRAIN)
        self.runner(first).run()

        def unavailable(request, timeout):
            if "public-transport" in request.full_url:
                raise OSError("offline")
            return Response(AIRTRAIN)
        failed = self.runner(unavailable).run()["sources"][0]
        self.assertEqual((failed["status"], failed["next_run_status"]), ("FAILED", "RETRY_REQUIRED"))
        states = {state.page_key: state for state in self.storage.all(OfficialGuidancePageState)}
        self.assertEqual(states["public-transport"].availability, "UNAVAILABLE")
        self.assertTrue(all(state.availability == "AVAILABLE" for key, state in states.items()
                            if key != "public-transport"))

        recovered = self.runner(first).run()["sources"][0]
        self.assertEqual((recovered["new_count"], recovered["updated_count"], recovered["duplicate_count"]), (0, 0, 3))
        self.assertTrue(all(state.availability == "AVAILABLE"
                            for state in self.storage.all(OfficialGuidancePageState)))
        self.assertTrue(all(snapshot.review_status == "NEEDS_REVIEW"
                            for snapshot in self.storage.all(OfficialGuidanceSnapshot)))


if __name__ == "__main__":
    unittest.main()
