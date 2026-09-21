"""Source access contract and synthetic fixture adapter."""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Protocol

from .auth import AuthRequired, MockSessionStore


@dataclass(frozen=True)
class AdapterCapability:
    access_mode: str
    supports_geo_tag: bool = False
    supports_search: bool = False
    supports_content_text: bool = False
    supports_media_metadata: bool = False
    supports_text: bool = False
    supports_title: bool = False
    supports_hashtags: bool = False
    supports_author: bool = False
    requires_authentication: Optional[bool] = None

    def __post_init__(self):
        if self.requires_authentication is None:
            object.__setattr__(self, "requires_authentication", self.access_mode == "AUTHENTICATED")
        elif self.requires_authentication != (self.access_mode == "AUTHENTICATED"):
            raise ValueError("authentication capability conflicts with access mode")


@dataclass(frozen=True)
class SourceItem:
    source: str
    external_id: str
    source_url: str
    payload: Dict[str, Any]


class SourceAdapter(Protocol):
    name: str
    capability: AdapterCapability

    def collect(self, city: str) -> Iterable[SourceItem]: ...


class MockSourceAdapter:
    def __init__(self, name: str, fixture: str, capability: AdapterCapability,
                 sessions: MockSessionStore):
        if capability.access_mode not in ("PUBLIC", "AUTHENTICATED"):
            raise ValueError("invalid access mode")
        self.name = name
        self.fixture = Path(fixture)
        self.capability = capability
        self.sessions = sessions

    def collect(self, city: str) -> Iterable[SourceItem]:
        if self.capability.access_mode == "AUTHENTICATED" and self.sessions.status(self.name) != "AUTHENTICATED":
            raise AuthRequired("AUTH_REQUIRED: {}".format(self.name))
        records = json.loads(self.fixture.read_text(encoding="utf-8"))["records"]
        for record in records:
            if record["city"] == city:
                yield SourceItem(self.name, record["external_id"], record["source_url"], record["payload"])


class MockSocialAdapter(MockSourceAdapter):
    """Fixture transport only; interpretation belongs to social_processing."""
