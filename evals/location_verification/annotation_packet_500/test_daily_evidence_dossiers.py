import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import audit_daily_evidence_dossiers
from build_daily_evidence_dossiers import validate


class DailyEvidenceDossierTests(unittest.TestCase):
    def test_reviewer_dossier_requires_coverage_and_no_old_answers(self):
        cases = [{"screening_id": f"case-{i}"} for i in range(500)]
        record = {"source_group": "OSM_INTAKE", "snapshot_sha256": "snapshot",
                  "row_sha256": "row", "lineage_key": "OSM"}
        dossiers = [{"schema_version": "lv-daily-evidence-dossier-v1", "case_id": case["screening_id"],
                     "candidate_claims": {}, "source_records": [record]} for case in cases]
        validate(cases, dossiers)
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate(cases, dossiers[:-1])
        with self.assertRaisesRegex(ValueError, "leaked answer"):
            validate(cases, [{**dossiers[0], "gold_label": "KEEP"}, *dossiers[1:]])

    def test_audit_rejects_dossier_changed_after_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            altered = Path(tmp) / "altered.jsonl"
            altered.write_text(json.dumps({"case_id": "tampered"}) + "\n")
            with patch.object(audit_daily_evidence_dossiers, "DOSSIERS", altered):
                with self.assertRaisesRegex(ValueError, "hash differs"):
                    audit_daily_evidence_dossiers.audit()


if __name__ == "__main__":
    unittest.main()
