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
    canonical_url: Optional[str] = None
    geo_tag: Optional[Dict[str, Any]] = None
    hashtags: List[str] = field(default_factory=list)
    media_metadata: List[Dict[str, Any]] = field(default_factory=list)
    language: Optional[str] = None
    content_status: str = "ACTIVE"
    collection_scope: Optional[str] = None


@dataclass
class ContentClassification:
    """Immutable, explainable result from the local zero-LLM classifier."""
    id: str
    source_content_id: str
    classifier_version: str
    content_fingerprint: str
    primary_label: str
    route: str
    confidence: float
    needs_review: bool
    model_recommended: bool
    created_at: str
    labels: List[str] = field(default_factory=list)
    scenario_labels: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)


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
    parent_poi_id: Optional[str] = None
    coordinate_precision: str = "UNSPECIFIED"


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
    website: str = ""
    opening_hours: str = ""
    source_tags: Dict[str, str] = field(default_factory=dict)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    source_kind: str = "content"
    coordinate_verified: bool = False
    resolved_poi_id: Optional[str] = None
    confidence_score: float = 0.0
    review_status: str = "NEEDS_REVIEW"
    review_reasons: List[str] = field(default_factory=list)
    parent_name: str = ""
    coordinate_precision: str = "UNSPECIFIED"
    field_sources: Dict[str, str] = field(default_factory=dict)
    coordinate_source_url: Optional[str] = None
    extraction_context: Dict[str, Any] = field(default_factory=dict)


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
    active: bool = True

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
    extraction_confidence: Optional[float] = None
    extraction_evidence: Optional[str] = None
    # Structured event sources may provide a stable identity and richer
    # schedule/location fields.  Defaults keep older social-derived rows
    # readable from their existing SQLite JSON payloads.
    source_event_id: Optional[str] = None
    lifecycle_status: str = "ACTIVE"
    event_url: Optional[str] = None
    registration_url: Optional[str] = None
    registration_description: str = ""
    park_ids: List[str] = field(default_factory=list)
    park_names: List[str] = field(default_factory=list)
    location: str = ""
    categories: List[str] = field(default_factory=list)
    description: str = ""
    instructor: str = ""
    contact_phone: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: str = "America/New_York"
    field_sources: Dict[str, str] = field(default_factory=dict)


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
    source_event_id: Optional[str] = None
    lifecycle_status: str = "ACTIVE"
    event_url: Optional[str] = None
    registration_url: Optional[str] = None
    registration_description: str = ""
    park_ids: List[str] = field(default_factory=list)
    park_names: List[str] = field(default_factory=list)
    location: str = ""
    categories: List[str] = field(default_factory=list)
    description: str = ""
    instructor: str = ""
    contact_phone: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: str = "America/New_York"
    field_sources: Dict[str, str] = field(default_factory=dict)


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
    source_field: Optional[str] = None
    source_reference_url: Optional[str] = None


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


@dataclass
class SourceRunRecord:
    """Durable per-source outcome of one multi-source collection run."""
    id: str
    multi_run_id: str
    source: str
    collection_key: str
    started_at: str
    ended_at: Optional[str]
    new_count: int = 0
    updated_count: int = 0
    duplicate_count: int = 0
    status: str = "RUNNING"
    failure_reason: Optional[str] = None
    next_run_status: str = "RUNNING"
    http_requests: Optional[int] = None
    retry_count: Optional[int] = None
    endpoint: Optional[str] = None
    runtime_seconds: Optional[float] = None


@dataclass
class SourceCollectionCheckpoint:
    """A page-safe source cursor; it advances only after its page was consumed."""
    id: str
    source: str
    collection_key: str
    cursor_updated_at: Optional[str] = None
    cursor_row_id: Optional[str] = None
    cycle_complete: bool = False
    updated_at: str = ""
    strategy: str = ""


@dataclass
class SourceDiscoveryCandidate:
    """A catalog dataset candidate discovered without interpreting content with an LLM."""
    id: str
    provider: str
    dataset_id: str
    title: str
    domain: str
    publisher: str
    description: str
    tags: List[str]
    api_endpoint: Optional[str]
    updated_at: Optional[str]
    discovered_query: str
    discovered_at: str
    last_checked_at: str
    status: str = "DISCOVERED"
    reasons: List[str] = field(default_factory=list)
    asset_type: str = "dataset"
    metadata_url: Optional[str] = None
    catalog_url: Optional[str] = None
    schema_fields: List[str] = field(default_factory=list)
    schema_checked: bool = False
    schema_fingerprint: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceDiscoveryRunRecord:
    """Durable metrics for one bounded catalog discovery run."""
    id: str
    provider: str
    started_at: str
    ended_at: Optional[str]
    status: str = "RUNNING"
    next_run_status: str = "RUNNING"
    new_count: int = 0
    updated_count: int = 0
    duplicate_count: int = 0
    filtered_count: int = 0
    http_requests: int = 0
    retry_count: int = 0
    query_count: int = 0
    failure_reason: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceDiscoveryCheckpoint:
    """Page-safe offset for one provider/query pair."""
    id: str
    provider: str
    query: str
    cursor: int = 0
    cycle_complete: bool = False
    updated_at: str = ""
    strategy: str = "SOCRATA_CATALOG_OFFSET_V1"


@dataclass
class OfficialGuidanceSnapshot:
    """An immutable, review-only version of a small allowlisted official page section."""
    id: str
    source: str
    page_key: str
    source_url: str
    section_location: str
    content: str
    content_version: str
    collected_at: str
    review_status: str = "NEEDS_REVIEW"
    review_reasons: List[str] = field(default_factory=lambda: ["OFFICIAL_GUIDANCE_REQUIRES_REVIEW"])


@dataclass
class OfficialGuidancePageState:
    """Mutable check state; snapshots above are retained only when content changes."""
    id: str
    source: str
    page_key: str
    source_url: str
    current_snapshot_id: Optional[str]
    last_checked_at: str
    availability: str
    failure_reason: Optional[str] = None
    parse_status: str = "OK"
    parse_failure_reason: Optional[str] = None
