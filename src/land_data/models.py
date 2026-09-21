"""Small storage-neutral domain models. Timestamps are UTC ISO-8601 strings."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RawSourceRecord:
    id: str
    source: str
    external_id: str
    source_url: Optional[str]
    city: str
    payload: Dict[str, Any]
    collected_at: str
    expires_at: Optional[str] = None


@dataclass
class SourceContent:
    id: str
    raw_record_id: str
    platform: str
    external_id: str
    source_url: Optional[str]
    content_type: str
    title: str
    text: str
    collected_at: str
    author_identifier: Optional[str] = None
    published_at: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class POI:
    id: str
    names: Dict[str, str]
    latitude: float
    longitude: float
    city: str
    country: str
    category: str
    created_at: str
    updated_at: str
    aliases: List[str] = field(default_factory=list)
    address: str = ""
    subcategory: str = ""
    tags: List[str] = field(default_factory=list)
    description: str = ""
    website: str = ""
    opening_hours: str = ""
    last_verified_at: Optional[str] = None
    confidence_score: float = 0.0
    quality_score: float = 0.0
    review_status: str = "NEEDS_REVIEW"
    review_reasons: List[str] = field(default_factory=list)
    active: bool = True
    coordinates_verified: bool = False
    validation_passed: bool = False
    map_visibility: bool = False


@dataclass
class POICandidate:
    id: str
    source_content_id: str
    name: str
    city: str
    country: str
    category: str
    created_at: str
    name_locale: str = "und"
    aliases: List[str] = field(default_factory=list)
    address: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    source_kind: str = "content"
    coordinate_verified: bool = False
    resolved_poi_id: Optional[str] = None
    confidence_score: float = 0.0
    review_status: str = "NEEDS_REVIEW"
    review_reasons: List[str] = field(default_factory=list)


@dataclass
class POIMention:
    id: str
    source_content_id: str
    relation_type: str
    created_at: str
    poi_id: Optional[str] = None
    poi_candidate_id: Optional[str] = None
    recommendation_summary: str = ""
    confidence_score: float = 0.0
    review_status: str = "NEEDS_REVIEW"
    review_reasons: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (self.poi_id or self.poi_candidate_id):
            raise ValueError("POIMention requires a POI or POICandidate")


@dataclass
class RecommendationSignal:
    id: str
    poi_mention_id: str
    type: str
    value: str
    confidence: float
    evidence_reference: str
    created_at: str
    sentiment: Optional[str] = None


@dataclass
class LocationEvidence:
    id: str
    source_content_id: str
    poi_candidate_id: str
    type: str
    raw_value: str
    confidence: float
    created_at: str
    matched_poi_id: Optional[str] = None


@dataclass
class EventCandidate:
    id: str
    source_content_id: str
    name: str
    created_at: str
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    venue_candidate_id: Optional[str] = None
    review_status: str = "NEEDS_REVIEW"
    review_reasons: List[str] = field(default_factory=lambda: ["EVENT_REQUIRES_REVIEW"])


@dataclass
class Event:
    id: str
    name: str
    source_content_id: str
    created_at: str
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    venue_poi_id: Optional[str] = None
    review_status: str = "NEEDS_REVIEW"
    review_reasons: List[str] = field(default_factory=list)


@dataclass
class FieldProvenance:
    id: str
    entity_type: str
    entity_id: str
    field_name: str
    source: str
    collected_at: str
    source_content_id: Optional[str] = None
    verified_at: Optional[str] = None


@dataclass(frozen=True)
class ReviewDecision:
    id: str
    target_type: str
    target_id: str
    previous_status: str
    new_status: str
    action: str
    reason: str
    reviewer: str
    created_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)
