"""Frozen, label-only evaluation for the local classifier.

This module is intentionally separate from :mod:`local_classifier` and from
the collection pipeline.  A JSONL evaluation set is read once, each record is
classified with the current deterministic classifier, and the human labels
are used only to calculate metrics.  No evaluation record is persisted and no
``fit`` or other rule-changing operation exists in this module.

The recommended JSONL shape is::

    {"id": "post-1", "platform": "threads", "city": "hanoi",
     "title": "...", "text": "...",
     "gold": {"primary_label": "knowledge", "route": "knowledge",
              "auto_approved": false}}

``gold`` may also be named ``expected``.  ``expected_label`` and
``expected_route`` are accepted as top-level compatibility aliases, as are
the frozen fixture's ``gold_primary_label`` and ``gold_route`` fields.  The
visible content and optional routing metadata are passed to the classifier;
gold labels never are.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .local_classifier import CLASSIFIER_VERSION, ClassificationResult, LocalClassifier
from .models import SourceContent


EVALUATION_SCHEMA_VERSION = "classifier-evaluation-v1"


@dataclass(frozen=True)
class EvaluationRecord:
    """One frozen row and the labels supplied by its human annotator."""

    id: str
    line_number: int
    raw: Mapping[str, Any]
    expected_label: str
    expected_route: str
    expected_auto_approved: Optional[bool]


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("{} must be a JSON object".format(name))
    return value


def _as_bool(value: Any, name: str) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    # Do not silently interpret arbitrary strings as labels.  JSON authors
    # should use true/false for review expectations.
    raise ValueError("{} must be true, false, or omitted".format(name))


def _gold(record: Mapping[str, Any], line_number: int) -> Tuple[str, str, Optional[bool]]:
    gold_value = record.get("gold", record.get("expected", {}))
    if isinstance(gold_value, str):
        gold: Mapping[str, Any] = {"primary_label": gold_value}
    else:
        gold = _as_mapping(gold_value, "gold")

    label = (gold.get("primary_label") or gold.get("label") or
             record.get("gold_primary_label") or record.get("expected_label") or
             record.get("gold_label"))
    if not isinstance(label, str) or not label.strip():
        raise ValueError("line {} has no gold.primary_label".format(line_number))
    label = label.strip()

    route = (gold.get("route") or record.get("gold_route") or
             record.get("expected_route") or label)
    if not isinstance(route, str) or not route.strip():
        raise ValueError("line {} has an invalid gold.route".format(line_number))
    route = route.strip()

    auto_value: Any = None
    for key in ("auto_approved", "expected_auto_approved", "should_auto_approve"):
        if key in gold:
            auto_value = gold[key]
            break
    if auto_value is None:
        for key in ("gold_auto_approved", "expected_auto_approved", "should_auto_approve"):
            if key in record:
                auto_value = record[key]
                break
    if auto_value is None:
        # Some annotators naturally record the opposite review state.
        needs_review = gold.get("needs_review", record.get("expected_needs_review"))
        if needs_review is not None:
            parsed = _as_bool(needs_review, "gold.needs_review")
            auto_value = None if parsed is None else not parsed
    return label, route, _as_bool(auto_value, "gold.auto_approved")


def _record_id(record: Mapping[str, Any], line_number: int) -> str:
    value = record.get("id") or record.get("record_id") or record.get("source_content_id")
    if value is None or not str(value).strip():
        # A deterministic fallback keeps small hand-authored sets usable while
        # still making the missing id visible in the prediction output.
        return "line-{}".format(line_number)
    return str(value).strip()


def parse_records(records: Sequence[Mapping[str, Any]]) -> List[EvaluationRecord]:
    """Validate JSON objects and extract gold labels without fitting anything."""
    result: List[EvaluationRecord] = []
    seen = set()
    for index, raw in enumerate(records, 1):
        if not isinstance(raw, Mapping):
            raise ValueError("line {} must be a JSON object".format(index))
        identifier = _record_id(raw, index)
        if identifier in seen:
            raise ValueError("duplicate evaluation id: {}".format(identifier))
        seen.add(identifier)
        expected_label, expected_route, auto_approved = _gold(raw, index)
        result.append(EvaluationRecord(
            id=identifier, line_number=index, raw=raw,
            expected_label=expected_label, expected_route=expected_route,
            expected_auto_approved=auto_approved))
    if not result:
        raise ValueError("evaluation JSONL contains no records")
    return result


def load_jsonl(path: str) -> Tuple[List[EvaluationRecord], str]:
    """Read a frozen JSONL set and return records plus its SHA-256 fingerprint."""
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise ValueError("evaluation dataset does not exist: {}".format(file_path))
    raw_records: List[Mapping[str, Any]] = []
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("cannot read evaluation dataset: {}".format(exc)) from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON on evaluation line {}: {}".format(
                line_number, exc.msg)) from exc
        if not isinstance(value, Mapping):
            raise ValueError("evaluation line {} must be a JSON object".format(line_number))
        # Retain the physical line number for diagnostics while fingerprinting
        # only semantic records.  Blank lines therefore do not change a set.
        raw_records.append(dict(value))
    parsed = parse_records(raw_records)
    fingerprint_payload = [dict(item.raw) for item in sorted(parsed, key=lambda item: item.id)]
    canonical = json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    fingerprint = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return parsed, fingerprint


def _content(record: EvaluationRecord) -> Tuple[SourceContent, Dict[str, Any]]:
    raw = record.raw
    content_id = str(raw.get("source_content_id") or raw.get("content_id") or record.id)
    platform = str(raw.get("platform") or raw.get("source") or "evaluation")
    city = raw.get("city") or raw.get("collection_scope")
    payload_value = raw.get("payload", {})
    payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
    # Only observed routing inputs are copied.  In particular, gold/expected
    # fields are intentionally excluded, so labels cannot become classifier
    # evidence or hidden training rules.
    for key in ("geo_tag", "content_type", "hashtags", "source_metadata", "events",
                "known_poi", "known_poi_match", "is_known_poi", "known_poi_id",
                "poi_id", "resolved_poi_id", "match_known_pois"):
        if key in raw:
            payload[key] = raw[key]
    hashtags = raw.get("hashtags", [])
    if not isinstance(hashtags, list):
        hashtags = []
    geo_tag = raw.get("geo_tag")
    if not isinstance(geo_tag, Mapping):
        geo_tag = None
    source_url = raw.get("source_url") or raw.get("url")
    content = SourceContent(
        id=content_id, raw_record_id=str(raw.get("raw_record_id") or "evaluation-" + record.id),
        platform=platform, external_id=str(raw.get("external_id") or record.id),
        source_url=str(source_url) if source_url else None,
        content_type=str(raw.get("content_type") or "POST"),
        title=str(raw.get("title") or ""), text=str(raw.get("text") or ""),
        collected_at=str(raw.get("collected_at") or "evaluation"),
        author_identifier=(str(raw["author_identifier"]) if raw.get("author_identifier") else None),
        published_at=(str(raw["published_at"]) if raw.get("published_at") else None),
        raw_metadata=(dict(raw["raw_metadata"]) if isinstance(raw.get("raw_metadata"), Mapping) else {}),
        canonical_url=(str(raw["canonical_url"]) if raw.get("canonical_url") else None),
        geo_tag=dict(geo_tag) if geo_tag else None, hashtags=[str(item) for item in hashtags],
        media_metadata=(list(raw["media_metadata"]) if isinstance(raw.get("media_metadata"), list) else []),
        language=(str(raw["language"]) if raw.get("language") else None),
        collection_scope=str(city) if city else None,
    )
    return content, payload


def _score(tp: int, fp: int, fn: int) -> Dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if precision + recall else 0.0)
    return {"tp": tp, "fp": fp, "fn": fn, "support": tp + fn,
            "predicted": tp + fp, "precision": round(precision, 6),
            "recall": round(recall, 6), "f1": round(f1, 6)}


def evaluate_records(records: Sequence[EvaluationRecord], dataset_fingerprint: str,
                     classifier: Optional[LocalClassifier] = None) -> Dict[str, Any]:
    """Run a read-only evaluation and return a machine-readable report."""
    if not records:
        raise ValueError("evaluation set is empty")
    classifier = classifier or LocalClassifier()
    predictions: List[Dict[str, Any]] = []
    labels = set()
    route_correct = 0
    wrong_auto_approvals = 0
    auto_approval_evaluated = 0
    model_recommended = 0
    gold_model_recommended = 0
    gold_model_evaluated = 0

    for record in records:
        content, payload = _content(record)
        result: ClassificationResult = classifier.classify(content, payload)
        predicted_label = result.primary_label
        predicted_route = result.route
        labels.update((record.expected_label, predicted_label))
        if predicted_route == record.expected_route:
            route_correct += 1
        if result.model_recommended:
            model_recommended += 1
        gold_value = record.raw.get("gold", record.raw.get("expected", {}))
        gold = gold_value if isinstance(gold_value, Mapping) else {}
        expected_model = gold.get("model_recommended", record.raw.get("expected_model_recommended"))
        if expected_model is not None:
            expected_model_bool = _as_bool(expected_model, "gold.model_recommended")
            gold_model_evaluated += 1
            gold_model_recommended += int(bool(expected_model_bool))
        wrong_auto = (record.expected_auto_approved is not None and
                      not result.needs_review and not record.expected_auto_approved)
        if record.expected_auto_approved is not None:
            auto_approval_evaluated += 1
            wrong_auto_approvals += int(wrong_auto)
        predictions.append({
            "id": record.id, "line_number": record.line_number,
            "expected_label": record.expected_label,
            "predicted_label": predicted_label,
            "expected_route": record.expected_route,
            "predicted_route": predicted_route,
            "correct": predicted_label == record.expected_label,
            "route_correct": predicted_route == record.expected_route,
            "confidence": result.confidence,
            "needs_review": result.needs_review,
            "model_recommended": result.model_recommended,
            "wrong_auto_approval": wrong_auto,
            "reasons": list(result.reasons),
            "signals": list(result.signals),
        })

    per_class: Dict[str, Dict[str, Any]] = {}
    confusion: Dict[str, Dict[str, int]] = {}
    for label in sorted(labels):
        tp = sum(1 for row in predictions
                 if row["expected_label"] == label and row["predicted_label"] == label)
        fp = sum(1 for row in predictions
                 if row["expected_label"] != label and row["predicted_label"] == label)
        fn = sum(1 for row in predictions
                 if row["expected_label"] == label and row["predicted_label"] != label)
        per_class[label] = _score(tp, fp, fn)
        confusion[label] = {predicted: sum(
            1 for row in predictions
            if row["expected_label"] == label and row["predicted_label"] == predicted)
                            for predicted in sorted(labels)}

    macro_precision = sum(item["precision"] for item in per_class.values()) / len(per_class)
    macro_recall = sum(item["recall"] for item in per_class.values()) / len(per_class)
    macro_f1 = sum(item["f1"] for item in per_class.values()) / len(per_class)
    total = len(predictions)
    micro_tp = sum(row["correct"] for row in predictions)
    micro_fp = total - micro_tp
    micro_fn = total - micro_tp
    micro = _score(micro_tp, micro_fp, micro_fn)
    return {
        "status": "SUCCEEDED",
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluation_only": True,
        "gold_labels_used_as_training": False,
        "fit_calls": 0,
        "classifier_version": CLASSIFIER_VERSION,
        "dataset_fingerprint": dataset_fingerprint,
        "dataset_records": total,
        "labels": sorted(labels),
        "per_class": per_class,
        "macro": {"precision": round(macro_precision, 6),
                  "recall": round(macro_recall, 6), "f1": round(macro_f1, 6)},
        "micro": micro,
        "accuracy": round(micro_tp / total, 6),
        "confusion_matrix": confusion,
        "route_accuracy": round(route_correct / total, 6),
        "route_correct": route_correct,
        "route_total": total,
        "model_recommended": model_recommended,
        "model_recommended_rate": round(model_recommended / total, 6),
        "gold_model_recommended": gold_model_recommended,
        "gold_model_evaluated": gold_model_evaluated,
        "wrong_auto_approvals": wrong_auto_approvals,
        "auto_approval_evaluated": auto_approval_evaluated,
        "llm_calls": 0,
        "predictions": predictions,
    }


def evaluate_jsonl(path: str, expected_fingerprint: Optional[str] = None) -> Dict[str, Any]:
    records, fingerprint = load_jsonl(path)
    if expected_fingerprint and expected_fingerprint != fingerprint:
        raise ValueError("evaluation dataset fingerprint mismatch: expected {}, got {}".format(
            expected_fingerprint, fingerprint))
    return evaluate_records(records, fingerprint)


__all__ = [
    "EVALUATION_SCHEMA_VERSION", "EvaluationRecord", "evaluate_jsonl",
    "evaluate_records", "load_jsonl", "parse_records",
]
