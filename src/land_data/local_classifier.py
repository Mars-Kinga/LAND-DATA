"""Deterministic, zero-LLM routing for collected content.

The classifier is deliberately a *triage* component.  It identifies signals
that are already visible in a :class:`SourceContent` record and chooses the
next processing route.  It does not claim that a community post is true,
does not enrich a place, and never calls a model.

The public entry point is :class:`LocalClassifier`::

    result = LocalClassifier(storage).classify(content, raw_payload)
    result.route                 # e.g. ``"known_poi"``
    result.to_dict()             # stable JSON-friendly envelope

``classify_content`` and ``classify`` are small functional aliases for
callers that do not need to keep a classifier instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .models import ContentClassification, SourceContent
from .util import stable_id, utc_now


# Change this only when the decision semantics change.  Keeping it separate
# from the source or model name makes persisted routing records comparable.
CLASSIFIER_VERSION = "local-classifier-v4"
ROUTER_VERSION = CLASSIFIER_VERSION


# These expressions intentionally describe visible language, rather than
# attempting named entity recognition.  A positive match is a routing hint,
# never proof of a fact.
_PLACE_CUE = re.compile(
    r"(?:📍|\[poi_v2\]|地址|位于|在\s*[^，。.!?]{1,28}(?:附近|里面|内|旁边)|"
    r"店|馆|餐厅|咖啡(?:馆|店)?|酒吧|酒店|旅馆|民宿|公园|广场|博物馆|美术馆|画廊|"
    r"展馆|剧院|剧场|商场|市场|市集|车站|地铁|机场|大厦|街|路|大道|码头|寺|庙|"
    r"海滩|海湾|桥|restaurant|cafe|coffee(?: shop)?|bar|pub|hotel|hostel|"
    r"museum|gallery|park|garden|square|market|mall|theater|theatre|station|"
    r"airport|terminal|building|street|avenue|road|pier|bridge|beach|temple|"
    r"shrine|palace|tower|district|neighborhood|neighbourhood|old town|campus|"
    r"landmark|attraction)",
    re.IGNORECASE,
)
_GENERIC_TOPIC = re.compile(
    r"签证|入境|海关|护照|信用卡|银行卡|支付|换汇|atm|小费|地铁攻略|"
    r"地铁指南|入境攻略|visa|immigration|customs|passport|payment|currency|"
    r"exchange|subway guide|transit guide",
    re.IGNORECASE,
)
_EVENT_CUE = re.compile(
    r"展览|展会|演出|音乐节|艺术节|电影节|市集|快闪|开幕|讲座|工作坊|"
    r"演讲|发布会|售票|门票|报名|festival|exhibition|concert|performance|"
    r"workshop|lecture|opening|popup|pop-up|screening|tickets?",
    re.IGNORECASE,
)
_NAMED_PLACE_CUE = re.compile(
    r"(?:📍|\[poi_v2\]|地址|位于|在\s*[^，。.!?]{1,28}(?:附近|里面|内|旁边)|"
    r"[A-Z][A-Za-z0-9'&.-]{2,}(?:\s+[A-Z][A-Za-z0-9'&.-]{2,}){0,5}\s+"
    r"(?:Cafe|Coffee|Restaurant|Bar|Pub|Hotel|Hostel|Museum|Park|Gallery|"
    r"Market|Mall|Station|Temple|Bridge|Beach|Theatre|Theater)\b|"
    r"[A-Za-z0-9一-龥]{2,18}(?:餐厅|咖啡馆|咖啡店|酒吧|酒店|旅馆|民宿|博物馆|美术馆|"
    r"公园|画廊|剧院|剧场|商场|市场|大厦|码头|海滩|大桥))",
    re.IGNORECASE,
)


# Keep these names aligned with the existing knowledge pipeline.  The order
# is stable and is also the order returned in ``scenario_labels``.
_SCENARIO_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("culture", re.compile(
        r"文化|礼仪|礼貌|禁忌|习俗|餐桌|社交|拍照规定|着装要求|dress\s*code|"
        r"etiquette|local customs|cultural customs|local culture|manners", re.IGNORECASE)),
    ("entry", re.compile(
        r"签证|入境|海关|护照检查|visa|immigration|customs", re.IGNORECASE)),
    ("arrival", re.compile(
        r"落地|机场到|机场.{0,8}市区|到市区|arrival|from the airport", re.IGNORECASE)),
    ("packing", re.compile(
        r"行前|打包|行李清单|插头|电压|穿衣|packing|what to pack|outfit", re.IGNORECASE)),
    ("money", re.compile(
        r"支付|信用卡|现金|换汇|ATM|小费|apple\s*pay|payment|cash|tipping|currency",
        re.IGNORECASE)),
    ("transport", re.compile(
        r"地铁|公交|打车|租车|自驾|停车|交通|transit|subway|metro|bus|taxi|parking",
        re.IGNORECASE)),
    ("stay", re.compile(
        r"入住|退房|行李寄存|住宿|酒店|check\s*-?in|check\s*-?out|accommodation",
        re.IGNORECASE)),
    ("safety", re.compile(
        r"安全|防骗|诈骗|紧急|报警|护照丢失|领事|scam|emergency|consulate|safety",
        re.IGNORECASE)),
    ("connectivity", re.compile(
        r"eSIM|SIM卡?|Wi-?Fi|上网|漫游|通信|connectivity|roaming|internet",
        re.IGNORECASE)),
    ("shopping", re.compile(
        r"购物|退税|免税|退换货|伴手礼|shopping|tax\s*free|souvenir",
        re.IGNORECASE)),
    ("health", re.compile(
        r"看病|医院|药房|买药|医疗|保险理赔|health|hospital|pharmacy|insurance",
        re.IGNORECASE)),
    ("departure", re.compile(
        r"返程|离境|值机|安检|转机|departure|check\s*in|connection|transfer",
        re.IGNORECASE)),
    ("special-needs", re.compile(
        r"亲子|儿童|婴儿|长者|老人|无障碍|轮椅|宠物|accessib|family|kids?|stroller",
        re.IGNORECASE)),
    ("food", re.compile(
        r"点餐|过敏|素食|餐饮|food|dining|vegetarian|vegan|allerg",
        re.IGNORECASE)),
    ("sightseeing", re.compile(
        # Event words are intentionally handled by ``_EVENT_CUE``.  Keeping
        # them out here prevents an exhibition from becoming a false
        # ``knowledge + event`` mixed item merely because it is sightseeing.
        r"游览|景点|夜生活|sightseeing|attraction|nightlife|things to do",
        re.IGNORECASE)),
)


@dataclass
class ClassificationResult:
    """Stable, explainable output from :class:`LocalClassifier`.

    Lists are used instead of sets so the JSON representation is stable.  The
    object supports both attribute access and ``result["route"]`` to make it
    convenient for existing route code and dashboard serializers.
    """

    version: str
    primary_label: str
    route: str
    confidence: float
    needs_review: bool
    model_recommended: bool
    labels: List[str] = field(default_factory=list)
    scenario_labels: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.confidence = _bounded_score(self.confidence)
        self.labels = _ordered_unique(self.labels)
        self.scenario_labels = _ordered_unique(self.scenario_labels)
        self.signals = _ordered_unique(self.signals)
        self.reasons = _ordered_unique(self.reasons)
        self.scores = {str(key): _bounded_score(value)
                       for key, value in sorted(self.scores.items())}

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-ready copy with the public classifier schema."""
        return {
            "version": self.version,
            "primary_label": self.primary_label,
            "route": self.route,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "model_recommended": self.model_recommended,
            "labels": list(self.labels),
            "scenario_labels": list(self.scenario_labels),
            "signals": list(self.signals),
            "reasons": list(self.reasons),
            "scores": dict(self.scores),
        }

    # A few call sites naturally treat classifier output as a mapping.
    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


def _bounded_score(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return round(max(0.0, min(1.0, number)), 4)


def _ordered_unique(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return result


def _value(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _visible_parts(content: Any, payload: Mapping[str, Any]) -> Tuple[str, str, str]:
    title = _value(content, "title", "")
    text = _value(content, "text", "")
    hashtags = _value(content, "hashtags", [])
    if not isinstance(title, str):
        title = ""
    if not isinstance(text, str):
        text = ""
    if not isinstance(hashtags, (list, tuple)):
        hashtags = []
    tags = " ".join(str(item) for item in hashtags if isinstance(item, str))
    # Payload may be a transient browser capture with fresher visible fields.
    if isinstance(payload.get("title"), str) and payload["title"]:
        title = payload["title"]
    if isinstance(payload.get("text"), str) and payload["text"]:
        text = payload["text"]
    if isinstance(payload.get("hashtags"), (list, tuple)):
        tags = " ".join(str(item) for item in payload["hashtags"] if isinstance(item, str))
    return title, text, " ".join(part for part in (title, text, tags) if part).strip()


def _geo_tag(content: Any, payload: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for candidate in (payload.get("geo_tag"), _value(content, "geo_tag", None)):
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _known_poi_hint(content: Any, payload: Mapping[str, Any], storage: Any) -> bool:
    # Explicit caller hints are useful for adapters that already resolved an
    # entity and avoid requiring a database lookup.
    for key in ("known_poi", "known_poi_match", "is_known_poi"):
        value = payload.get(key)
        if isinstance(value, bool) and value:
            return True
    for key in ("known_poi_id", "poi_id", "resolved_poi_id"):
        if payload.get(key):
            return True
    if storage is None:
        return False
    try:
        # Import lazily: social_processing is a consumer of route decisions in
        # some deployments, so importing it at module load can create cycles.
        from .social_processing import KnownPOITextLocationExtractor

        material = dict(payload)
        material["match_known_pois"] = True
        material.setdefault("title", _value(content, "title", ""))
        material.setdefault("text", _value(content, "text", ""))
        return bool(KnownPOITextLocationExtractor(storage).extract(content, material))
    except (AttributeError, KeyError, TypeError, ValueError):
        # A classifier should remain usable with a light-weight storage stub.
        return False


def _event_hint(content: Any, payload: Mapping[str, Any], visible: str) -> Tuple[bool, float, List[str]]:
    signals: List[str] = []
    content_type = str(payload.get("content_type", _value(content, "content_type", ""))).lower()
    explicit_events = payload.get("events")
    explicit = isinstance(explicit_events, (list, tuple)) and bool(explicit_events)
    explicit = explicit or content_type in {"event", "events", "event_post"}
    cue = bool(_EVENT_CUE.search(visible))
    if explicit:
        signals.append("event.structured_input")
    if cue:
        signals.append("event.text_cue")
    if explicit or cue:
        return True, (0.96 if explicit else 0.82), signals
    return False, 0.0, signals


def _scenarios(visible: str, payload: Mapping[str, Any]) -> List[str]:
    result: List[str] = []
    for name, pattern in _SCENARIO_PATTERNS:
        if pattern.search(visible) and name not in result:
            result.append(name)
    metadata = payload.get("source_metadata", {})
    group = metadata.get("query_group") if isinstance(metadata, Mapping) else None
    # Search group affects display order only when the body independently
    # supports that scenario. It never creates a label by itself.
    if group in result:
        result.remove(group)
        result.insert(0, group)
    return result


def _has_place_signal(visible: str, tag: Optional[Mapping[str, Any]]) -> Tuple[bool, List[str]]:
    signals: List[str] = []
    if bool(_PLACE_CUE.search(visible)):
        signals.append("place.text_cue")
    if isinstance(tag, Mapping) and tag.get("name"):
        level = str(tag.get("level", "UNKNOWN")).upper()
        if level == "POI":
            signals.append("place.geo_tag.poi")
            return True, signals
        signals.append("place.geo_tag.{}".format(level.lower()))
        return True, signals
    return bool(signals), signals


def _has_named_place_signal(visible: str, tag: Optional[Mapping[str, Any]]) -> bool:
    """Return whether text points at a named venue, rather than a generic noun.

    Generic words such as ``hotel`` and ``寺庙`` frequently occur inside
    etiquette or arrival advice.  They should not turn otherwise useful
    knowledge into an unresolved POI.  A proper name, address marker, or
    structured geotag is a stronger place signal.
    """
    if isinstance(tag, Mapping) and tag.get("name"):
        return True
    match = _NAMED_PLACE_CUE.search(visible)
    if not match:
        return False
    # Generic noun phrases are not venue identities. Search intent and prose
    # such as "这些餐厅" or "当地酒店" must not force knowledge into POI extraction.
    if re.search(r"(?:这些|那些|所有|任何|一家|这家|那家|某家|当地|附近|很多|各种)"
                 r"(?:餐厅|咖啡馆|咖啡店|酒吧|酒店|旅馆|民宿|博物馆|美术馆|公园|"
                 r"画廊|剧院|剧场|商场|市场)", match.group(0)):
        return False
    return True


class LocalClassifier:
    """Classify visible source material without network or model access.

    ``storage`` is optional.  If supplied, a unique exact name match against
    existing POIs is used as an additional deterministic signal.  This only
    identifies a candidate; it does not verify the community claim.
    """

    version = CLASSIFIER_VERSION

    def __init__(self, storage: Any = None):
        self.storage = storage

    def classify(self, content: SourceContent, payload: Optional[Mapping[str, Any]] = None) -> ClassificationResult:
        material: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        title, text, visible = _visible_parts(content, material)
        tag = _geo_tag(content, material)
        scenarios = _scenarios(visible, material)
        has_place, place_signals = _has_place_signal(visible, tag)
        named_place = _has_named_place_signal(visible, tag)
        known = _known_poi_hint(content, material, self.storage)
        structured = bool(isinstance(tag, Mapping) and
                          str(tag.get("level", "")).upper() == "POI" and tag.get("name"))
        event, event_score, event_signals = _event_hint(content, material, visible)
        generic = bool(_GENERIC_TOPIC.search(title))
        short = len(visible) < 45
        empty = not visible

        # Scenario text often mentions a generic venue (for example, temple
        # dress rules or hotel check-in).  Treat it as knowledge until a
        # named venue, address marker, or structured tag is present.
        if scenarios and not named_place and not structured and not known and not event:
            has_place = False

        signals = list(place_signals) + list(event_signals)
        if known:
            signals.append("place.known_poi_exact_name")
        if scenarios:
            signals.extend("scenario." + item for item in scenarios)
        metadata = material.get("source_metadata", {})
        query_group = metadata.get("query_group") if isinstance(metadata, Mapping) else None
        if isinstance(query_group, str) and query_group:
            signals.append("query_group." + query_group)
        if generic:
            signals.append("topic.generic_travel_info")
        if short and not empty:
            signals.append("text.short")
        if empty:
            signals.append("text.empty")

        scores: Dict[str, float] = {}
        if structured:
            scores["structured_poi"] = 0.99
        if known and not structured:
            scores["known_poi"] = 0.96
        if scenarios:
            scores["knowledge"] = min(0.96, 0.76 + 0.04 * len(scenarios))
        if event:
            scores["event"] = event_score
        if has_place and not structured and not known:
            scores["unresolved_place"] = 0.66 if short else 0.78
        if empty:
            scores["irrelevant"] = 0.99
        elif not has_place and not scenarios and not event:
            scores["irrelevant" if short else "ambiguous"] = 0.88 if short else 0.55
        elif generic and not scenarios and not has_place and not event:
            scores["irrelevant"] = 0.84

        evidence_labels: List[str] = []
        if structured:
            evidence_labels.append("structured_poi")
        elif known:
            evidence_labels.append("known_poi")
        if event:
            evidence_labels.append("event")
        if scenarios:
            evidence_labels.append("knowledge")
        if has_place and not structured and not known:
            evidence_labels.append("unresolved_place")
        if not evidence_labels:
            evidence_labels.append("irrelevant" if short or empty or generic else "ambiguous")

        if len(evidence_labels) > 1:
            primary = "mixed"
            # Mixed confidence is the least confident participating signal.
            participating = [scores.get(label, 0.0) for label in evidence_labels]
            confidence = min(participating) if participating else 0.5
            scores["mixed"] = confidence
            labels = ["mixed"] + evidence_labels
            reasons = ["multiple_content_kinds"]
            needs_review = True
            model_recommended = "unresolved_place" in evidence_labels
        else:
            primary = evidence_labels[0]
            confidence = scores.get(primary, 0.55)
            labels = [primary]
            reasons = []
            if primary == "structured_poi":
                reasons.append("author_geo_tag")
            elif primary == "known_poi":
                reasons.append("unique_exact_poi_name")
            elif primary == "knowledge":
                reasons.append("non_poi_travel_knowledge")
            elif primary == "event":
                reasons.append("event_signal")
            elif primary == "unresolved_place":
                reasons.append("place_without_structured_identity")
            elif primary == "ambiguous":
                reasons.append("long_text_without_actionable_signal")
            else:
                reasons.append("empty_visible_text" if empty else "no_actionable_place_or_event_signal")
            # These two deterministic routes do not need an LLM.  They can
            # still be reviewed by the downstream provenance/review layer.
            needs_review = primary in {"knowledge", "event", "unresolved_place", "ambiguous"}
            model_recommended = primary == "unresolved_place"

        if generic and "generic_topic" not in reasons:
            reasons.append("generic_travel_topic")
        if short and not empty:
            reasons.append("short_visible_text")
        if tag is not None and str(tag.get("level", "")).upper() != "POI":
            reasons.append("non_poi_geo_tag")

        # A classifier can recommend a model for extraction, but it must never
        # pretend that extraction happened or that a community fact is true.
        return ClassificationResult(
            version=self.version,
            primary_label=primary,
            route=primary,
            confidence=confidence,
            needs_review=needs_review,
            model_recommended=model_recommended,
            labels=labels,
            scenario_labels=scenarios,
            signals=signals,
            reasons=reasons,
            scores=scores,
        )

    __call__ = classify


def classify_content(content: SourceContent, payload: Optional[Mapping[str, Any]] = None,
                     storage: Any = None) -> ClassificationResult:
    """Functional wrapper around :class:`LocalClassifier`."""
    return LocalClassifier(storage).classify(content, payload)


def classify(content: SourceContent, payload: Optional[Mapping[str, Any]] = None,
             storage: Any = None) -> ClassificationResult:
    """Short alias for callers migrating from a function based router."""
    return classify_content(content, payload, storage)


def persist_classification(storage: Any, content: SourceContent,
                           result: ClassificationResult,
                           payload: Optional[Mapping[str, Any]] = None) -> ContentClassification:
    """Persist one immutable content/version decision without source text.

    The fingerprint changes when the visible content or the retained routing
    metadata changes. Re-running the same classifier on the same material is
    idempotent; a later content edit or classifier version remains auditable.
    """
    material = payload if isinstance(payload, Mapping) else {}
    fingerprint_input = {
        "title": _value(content, "title", ""),
        "text": _value(content, "text", ""),
        "hashtags": list(_value(content, "hashtags", []) or []),
        "geo_tag": material.get("geo_tag", _value(content, "geo_tag", None)),
        "content_type": material.get("content_type", _value(content, "content_type", "")),
        "source_metadata": material.get("source_metadata", {}),
        "events": material.get("events", []),
    }
    fingerprint = stable_id("classifier-input", content.id, fingerprint_input)
    item = ContentClassification(
        id=stable_id("content-classification", content.id, result.version, fingerprint),
        source_content_id=content.id, classifier_version=result.version,
        content_fingerprint=fingerprint, primary_label=result.primary_label,
        route=result.route, confidence=result.confidence,
        needs_review=result.needs_review,
        model_recommended=result.model_recommended, created_at=utc_now(),
        labels=list(result.labels), scenario_labels=list(result.scenario_labels),
        signals=list(result.signals), reasons=list(result.reasons),
        scores=dict(result.scores))
    existing = storage.get(ContentClassification, item.id)
    if existing is not None:
        return existing
    storage.put(item)
    return item


def current_classifications(storage: Any) -> List[ContentClassification]:
    """Return the newest decision per content for the active classifier version."""
    newest: Dict[str, ContentClassification] = {}
    for item in storage.all(ContentClassification):
        if item.classifier_version != CLASSIFIER_VERSION:
            continue
        previous = newest.get(item.source_content_id)
        if previous is None or (item.created_at, item.id) > (previous.created_at, previous.id):
            newest[item.source_content_id] = item
    return sorted(newest.values(), key=lambda item: (item.created_at, item.id), reverse=True)


__all__ = [
    "CLASSIFIER_VERSION", "ROUTER_VERSION", "ClassificationResult",
    "LocalClassifier", "classify", "classify_content", "persist_classification",
    "current_classifications",
]
