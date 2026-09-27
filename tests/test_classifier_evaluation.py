import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from land_data.classifier_evaluation import (
    EVALUATION_SCHEMA_VERSION,
    evaluate_jsonl,
    load_jsonl,
)
from land_data.cli import main
from land_data.local_classifier import CLASSIFIER_VERSION


ROOT = Path(__file__).resolve().parents[1]


class ClassifierEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dataset = Path(self.temp.name) / "frozen.jsonl"
        self.rows = [
            {"id": "knowledge-1", "platform": "xiaohongshu",
             "city": "hanoi", "title": "河内文化礼仪",
             "text": "进入寺庙要注意着装和拍照禁忌",
             "gold": {"primary_label": "knowledge", "auto_approved": False}},
            {"id": "place-1", "platform": "threads", "city": "hanoi",
             "title": "Luna Cafe", "text": "这家咖啡店适合工作，位置在市中心",
             "gold": {"primary_label": "unresolved_place", "route": "unresolved_place",
                      "auto_approved": False, "model_recommended": True}},
            {"id": "irrelevant-1", "platform": "reddit", "city": "hanoi",
             "title": "今天下雨了", "text": "还不错",
             "gold": {"primary_label": "irrelevant", "auto_approved": True}},
            {"id": "event-1", "platform": "threads", "city": "hanoi",
             "content_type": "EVENT", "title": "河内摄影展",
             "text": "本周末开放，欢迎报名", "events": [{"name": "河内摄影展"}],
             "gold": {"primary_label": "event", "auto_approved": False}},
        ]
        self.dataset.write_text("\n".join(json.dumps(row, ensure_ascii=False)
                                         for row in self.rows) + "\n", encoding="utf-8")

    def test_evaluation_is_read_only_and_reports_metrics(self):
        records, fingerprint = load_jsonl(str(self.dataset))
        result = evaluate_jsonl(str(self.dataset), expected_fingerprint=fingerprint)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["schema_version"], EVALUATION_SCHEMA_VERSION)
        self.assertEqual(result["classifier_version"], CLASSIFIER_VERSION)
        self.assertTrue(result["evaluation_only"])
        self.assertFalse(result["gold_labels_used_as_training"])
        self.assertEqual(result["fit_calls"], 0)
        self.assertEqual(result["dataset_records"], len(records))
        self.assertEqual(result["accuracy"], 1.0)
        self.assertEqual(result["route_accuracy"], 1.0)
        self.assertEqual(result["model_recommended"], 1)
        self.assertEqual(result["wrong_auto_approvals"], 0)
        self.assertEqual(result["llm_calls"], 0)
        self.assertEqual(result["confusion_matrix"]["knowledge"]["knowledge"], 1)
        self.assertEqual(result["per_class"]["event"]["f1"], 1.0)
        self.assertEqual(result["micro"]["f1"], 1.0)
        self.assertEqual(result["macro"]["f1"], 1.0)

    def test_fingerprint_guard_rejects_changed_frozen_set(self):
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            evaluate_jsonl(str(self.dataset), expected_fingerprint="sha256:changed")

    def test_labels_do_not_become_classifier_evidence(self):
        row = {"id": "label-only", "title": "没有地点的长篇日记",
               "text": "今天记录一些无法被规则识别的生活片段。" * 4,
               "gold": {"primary_label": "structured_poi", "route": "structured_poi"}}
        path = Path(self.temp.name) / "label-only.jsonl"
        path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        result = evaluate_jsonl(str(path))
        self.assertNotEqual(result["predictions"][0]["predicted_label"], "structured_poi")
        self.assertEqual(result["predictions"][0]["expected_label"], "structured_poi")
        self.assertEqual(result["llm_calls"], 0)

    def test_cli_evaluate_prints_machine_readable_report(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--root", str(ROOT), "classifier", "evaluate",
                         "--dataset", str(self.dataset)])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["llm_calls"], 0)
        self.assertIn("dataset_fingerprint", result)

    def test_frozen_regression_fixture_top_level_gold_aliases_are_supported(self):
        fixture = ROOT / "evals" / "classifier_regression.jsonl"
        records, fingerprint = load_jsonl(str(fixture))
        self.assertEqual(len(records), 45)
        result = evaluate_jsonl(str(fixture), expected_fingerprint=fingerprint)
        self.assertEqual(result["dataset_records"], 45)
        self.assertEqual(result["classifier_version"], CLASSIFIER_VERSION)
        self.assertEqual(result["llm_calls"], 0)
        self.assertIn("structured_poi", result["labels"])
        self.assertIn("knowledge", result["labels"])


if __name__ == "__main__":
    unittest.main()
