import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

import audit_evidence_dossiers
from build_evidence_dossiers import source_record, strip_legacy_answers, validate_dossiers


class EvidenceDossierTests(unittest.TestCase):
    def test_reviewer_packet_has_exact_coverage_and_provenance_without_labels(self):
        shortlist = [{"candidate_id": "a"}]
        record = source_record("DOHMH_FROZEN", Path("frozen.db"), "snapshot-hash", "source-a",
                               {"name": "Cafe"}, "2026-10-18T00:00:00Z", "source_collected_at",
                               "https://example.invalid/a")
        packet = {"schema_version": "lv-evidence-dossier-v3", "case_id": "a",
                  "as_of": "2026-10-28T23:59:59Z", "candidate_claims": {"name": "Cafe"},
                  "source_records": [record]}
        validate_dossiers(shortlist, [packet])
        with self.assertRaisesRegex(ValueError, "exactly once"):
            validate_dossiers(shortlist, [packet, packet])
        with self.assertRaisesRegex(ValueError, "answer-bearing"):
            validate_dossiers(shortlist, [{**packet, "gold_label": "correct"}])
        with self.assertRaisesRegex(ValueError, "provenance"):
            validate_dossiers(shortlist, [{**packet, "source_records": [{**record, "row_sha256": ""}]}])
        contaminated = {**record, "raw_record": {"locations": [{"coordinate_verified": True}]}}
        with self.assertRaisesRegex(ValueError, "leaked legacy"):
            validate_dossiers(shortlist, [{**packet, "source_records": [contaminated]}])
        self.assertEqual(strip_legacy_answers(contaminated["raw_record"]), {"locations": [{}]})

    def test_auditor_rejects_changed_dossier_bytes_before_reading_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            altered = Path(tmp) / "altered.jsonl"
            altered.write_text("{}\n")
            with patch.object(audit_evidence_dossiers, "DOSSIERS", altered):
                with self.assertRaisesRegex(ValueError, "hash differs"):
                    audit_evidence_dossiers.audit()


if __name__ == "__main__":
    unittest.main()
