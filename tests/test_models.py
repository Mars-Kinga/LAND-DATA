import unittest

from land_data.models import (Event, FieldProvenance, LocationEvidence, POI, POICandidate,
                              POIMention, RecommendationSignal, SourceContent)


class ModelTests(unittest.TestCase):
    def test_poi_fields(self):
        poi = POI("p", {"ja": "名前", "en": "Name"}, 35.0, 139.0, "tokyo", "JP", "food", "t", "t")
        self.assertEqual(poi.names["ja"], "名前")
        self.assertFalse(poi.map_visibility)

    def test_source_content_preserves_source_identity(self):
        content = SourceContent("c", "r", "mock", "external", "https://example.invalid", "POST", "title", "text", "t")
        self.assertEqual((content.platform, content.external_id), ("mock", "external"))

    def test_candidate_starts_unresolved(self):
        candidate = POICandidate("c", "source", "Place", "tokyo", "JP", "food", "t")
        self.assertIsNone(candidate.resolved_poi_id)
        self.assertEqual(candidate.review_status, "NEEDS_REVIEW")

    def test_mention_requires_at_least_one_place_reference(self):
        with self.assertRaises(ValueError):
            POIMention("m", "s", "RECOMMENDATION", "t")
        self.assertEqual(POIMention("m", "s", "RECOMMENDATION", "t", poi_candidate_id="c").poi_candidate_id, "c")
        self.assertEqual(POIMention("m", "s", "RECOMMENDATION", "t", poi_id="p").poi_id, "p")

    def test_recommendation_signal_is_mention_scoped(self):
        signal = RecommendationSignal("s", "m", "feature", "sunset", 0.9, "text:0", "t")
        self.assertEqual((signal.poi_mention_id, signal.value), ("m", "sunset"))

    def test_event_is_not_a_poi(self):
        event = Event("e", "Pop-up", "source", "t", venue_poi_id="p")
        self.assertEqual(event.venue_poi_id, "p")
        self.assertEqual(event.review_status, "NEEDS_REVIEW")

    def test_evidence_and_provenance(self):
        evidence = LocationEvidence("e", "s", "c", "AUTHOR_GEO_TAG", "Name", 0.9, "t")
        provenance = FieldProvenance("f", "POI", "p", "names", "mock", "t", source_content_id="s")
        self.assertEqual(evidence.type, "AUTHOR_GEO_TAG")
        self.assertEqual(provenance.field_name, "names")
