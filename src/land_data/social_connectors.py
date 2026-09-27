"""Provider-neutral contracts for community content collection.

This module deliberately stops at the source boundary.  A provider may be an
official public feed, or a user-controlled browser export, but it must hand
the same :class:`~land_data.adapters.SourceItem` shape to the existing
Pipeline.  The module does not automate a login, read browser cookies, solve
anti-bot challenges, or invent a search endpoint for a platform.

The small paged connector is useful for two cases:

* an official API adapter can implement ``fetch_page`` and keep its own HTTP
  policy outside this module; and
* a visible browser workflow can export the posts the user can see as JSON and
  feed them through ``BrowserVisibleInputProvider``.

Both cases get the same durable checkpoint and content-version de-duplication
when backed by ``SQLiteStorage``.  The resulting items are consumed by the
existing Pipeline, which writes RawSourceRecord, SourceContent, processing
results, and provenance exactly as it does for Xiaohongshu and Threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .adapters import AdapterCapability, SourceItem
from .auth import AuthRequired
from .util import stable_id, utc_now


_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_AUTH_STATES = {"PUBLIC", "AUTHENTICATED", "AUTH_REQUIRED", "UNKNOWN", "UNAVAILABLE"}
_ACCESS_MODES = {"PUBLIC_FEED", "AUTHENTICATED_BROWSER", "AUTHENTICATED_API", "USER_PROVIDED"}
_CAPTURE_METHODS = {"public_feed", "browser_visible_input", "manual_import"}
_MAX_PAGE_SIZE = 100
_MAX_TEXT_BYTES = 1_000_000


class SocialConnectorError(ValueError):
    """A safe, provider-neutral connector error."""


@dataclass(frozen=True)
class SocialProviderContract:
    """Declared provider behaviour, independent of the transport.

    ``supports_search`` is a capability declaration only.  A provider must
    supply a real implementation for search; this contract never fabricates a
    search URL or assumes that a platform exposes one.
    """

    provider: str
    access_mode: str
    capture_method: str
    supports_search: bool = False
    supports_browser_input: bool = False
    supports_public_input: bool = False
    supports_location_tags: bool = False
    requires_authentication: Optional[bool] = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _PROVIDER_NAME.fullmatch(self.provider):
            raise ValueError("provider must be a lowercase slug of 2-64 characters")
        if self.access_mode not in _ACCESS_MODES:
            raise ValueError("unsupported social provider access mode")
        if self.capture_method not in _CAPTURE_METHODS:
            raise ValueError("unsupported social capture method")
        expected = self.access_mode in {"AUTHENTICATED_BROWSER", "AUTHENTICATED_API"}
        if self.requires_authentication is not None and self.requires_authentication != expected:
            raise ValueError("authentication capability conflicts with access mode")
        if self.capture_method == "browser_visible_input" and not self.supports_browser_input:
            raise ValueError("browser_visible_input requires supports_browser_input")
        if self.capture_method == "public_feed" and not self.supports_public_input:
            raise ValueError("public_feed requires supports_public_input")

    @property
    def authenticated(self) -> bool:
        return self.access_mode in {"AUTHENTICATED_BROWSER", "AUTHENTICATED_API"}

    def adapter_capability(self) -> AdapterCapability:
        """Return the existing source-adapter capability used by Pipeline."""
        return AdapterCapability(
            "AUTHENTICATED" if self.authenticated else "PUBLIC",
            supports_geo_tag=self.supports_location_tags,
            supports_search=self.supports_search,
            supports_content_text=True,
            supports_media_metadata=True,
            supports_text=True,
            supports_title=True,
            supports_hashtags=True,
            supports_author=True,
        )


@dataclass(frozen=True)
class SocialProviderStatus:
    provider: str
    state: str
    access_mode: str
    authenticated: bool
    next_action: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in _AUTH_STATES:
            raise ValueError("unsupported provider status")


@dataclass(frozen=True)
class SocialRecord:
    """A platform record before it enters RawSourceRecord."""

    source_url: Optional[str]
    title: str = ""
    text: str = ""
    external_id: Optional[str] = None
    author_identifier: Optional[str] = None
    published_at: Optional[str] = None
    hashtags: Tuple[str, ...] = ()
    media_metadata: Tuple[Mapping[str, Any], ...] = ()
    geo_tag: Optional[Mapping[str, Any]] = None
    content_type: str = "SOCIAL_POST"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_url is not None:
            canonical_social_url(self.source_url)
        for field_name in ("title", "text", "content_type"):
            if not isinstance(getattr(self, field_name), str):
                raise ValueError("social record {} must be text".format(field_name))
        encoded_size = len((self.title + "\n" + self.text).encode("utf-8"))
        if encoded_size > _MAX_TEXT_BYTES:
            raise ValueError("social record text exceeds the 1 MB safety limit")
        if not self.external_id and not self.source_url:
            raise ValueError("social record requires a provider id or source URL")
        if self.geo_tag is not None and not isinstance(self.geo_tag, Mapping):
            raise ValueError("social record geo_tag must be an object")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SocialRecord":
        if not isinstance(value, Mapping):
            raise ValueError("social input record must be an object")
        hashtags = value.get("hashtags", ())
        if not isinstance(hashtags, (list, tuple)) or any(not isinstance(v, str) for v in hashtags):
            raise ValueError("social record hashtags must be strings")
        media = value.get("media_metadata", ())
        if not isinstance(media, (list, tuple)) or any(not isinstance(v, Mapping) for v in media):
            raise ValueError("social record media_metadata must be objects")
        metadata = value.get("metadata", value.get("source_metadata", {}))
        if not isinstance(metadata, Mapping):
            raise ValueError("social record metadata must be an object")
        return cls(
            source_url=value.get("source_url", value.get("url")),
            title=value.get("title", ""), text=value.get("text", value.get("fullText", "")),
            external_id=value.get("external_id", value.get("id")),
            author_identifier=value.get("author_identifier", value.get("author")),
            published_at=value.get("published_at"), hashtags=tuple(hashtags),
            media_metadata=tuple(media), geo_tag=value.get("geo_tag"),
            content_type=value.get("content_type", "SOCIAL_POST"), metadata=dict(metadata),
        )


@dataclass(frozen=True)
class SocialPage:
    records: Tuple[SocialRecord, ...]
    next_cursor: Optional[str] = None

    def __post_init__(self) -> None:
        if self.next_cursor is not None and (not isinstance(self.next_cursor, str) or not self.next_cursor):
            raise ValueError("page cursor must be a non-empty string")


class PagedSocialProvider(Protocol):
    contract: SocialProviderContract

    def status(self) -> SocialProviderStatus: ...

    def fetch_page(self, city: str, query: Optional[str], cursor: Optional[str], limit: int) -> SocialPage: ...


def canonical_social_url(value: str) -> str:
    """Normalize a visible source URL without retaining query tokens."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("social source URL is required")
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    local_http = parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and not local_http:
        raise ValueError("social source URL must use HTTPS")
    if not host or not parts.path:
        raise ValueError("social source URL is malformed")
    return urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))


def stable_social_external_id(provider: str, record: SocialRecord) -> Tuple[str, str]:
    """Return a stable provider id and its identity quality.

    Provider ids are preferred.  URL hashing is an explicit fallback for
    browser exports whose visible page has no exposed post id; it is marked in
    metadata so later review can distinguish it from a provider identity.
    """
    if record.external_id is not None and str(record.external_id).strip():
        return str(record.external_id).strip(), "PROVIDER_ID"
    if record.source_url:
        url = canonical_social_url(record.source_url)
        return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32], "URL_HASH"
    raise ValueError("social record has no stable identity")


def social_source_item(contract: SocialProviderContract, record: SocialRecord,
                       *, query: Optional[str], auth_state: str) -> SourceItem:
    """Convert a provider record to the common adapter item shape."""
    if auth_state not in _AUTH_STATES:
        raise ValueError("invalid social authentication state")
    external_id, identity_quality = stable_social_external_id(contract.provider, record)
    source_url = canonical_social_url(record.source_url) if record.source_url else None
    metadata = dict(record.metadata)
    # Metadata is provenance only; credentials and browser state never enter it.
    for forbidden in ("access_token", "authorization", "cookie", "cookies", "session"):
        metadata.pop(forbidden, None)
    metadata.update({
        "platform": contract.provider,
        "provider": contract.provider,
        "query": query,
        "access_mode": contract.access_mode,
        "capture_method": contract.capture_method,
        "auth_state": auth_state,
        "identity_quality": identity_quality,
    })
    payload: Dict[str, Any] = {
        "content_type": record.content_type,
        "title": record.title,
        "text": record.text,
        "author_identifier": record.author_identifier,
        "published_at": record.published_at,
        "hashtags": list(record.hashtags),
        "media_metadata": [dict(item) for item in record.media_metadata],
        "source_metadata": metadata,
        "content_status": "ACTIVE",
        # Generic social items use rule based known-POI matching and queues;
        # LLM extraction remains an explicit Pipeline option.
        "match_known_pois": True,
    }
    if record.geo_tag is not None:
        payload["geo_tag"] = dict(record.geo_tag)
    return SourceItem(contract.provider, external_id, source_url, payload)


class SQLiteSocialState:
    """Durable cursor and content-version state on an existing SQLite DB."""

    def __init__(self, connection) -> None:
        if connection is None or not hasattr(connection, "execute"):
            raise ValueError("social connector requires a SQLite connection")
        self.connection = connection
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS social_connector_checkpoints (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, city TEXT NOT NULL,
          query TEXT, cursor TEXT, complete INTEGER NOT NULL, status TEXT NOT NULL,
          updated_at TEXT NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS social_connector_seen (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, city TEXT NOT NULL,
          external_id TEXT NOT NULL, content_hash TEXT NOT NULL, source_url TEXT,
          seen_at TEXT NOT NULL, data TEXT NOT NULL,
          UNIQUE(provider, city, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_social_connector_seen_key
          ON social_connector_seen(provider, city, external_id);
        CREATE TABLE IF NOT EXISTS social_connector_runs (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, city TEXT NOT NULL,
          query TEXT, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
          pages INTEGER NOT NULL, emitted INTEGER NOT NULL, error TEXT
        );
        """)
        self.connection.commit()

    def checkpoint(self, provider: str, city: str, query: Optional[str]) -> Optional[dict]:
        key = stable_id("social-checkpoint", provider, city, query or "")
        row = self.connection.execute(
            "SELECT * FROM social_connector_checkpoints WHERE id=?", (key,)).fetchone()
        return dict(row) if row else None

    def seen_hash(self, provider: str, city: str, external_id: str) -> Optional[str]:
        row = self.connection.execute(
            "SELECT content_hash FROM social_connector_seen WHERE provider=? AND city=? AND external_id=?",
            (provider, city, external_id)).fetchone()
        return row[0] if row else None

    def commit_page(self, provider: str, city: str, query: Optional[str], page: SocialPage,
                    records: Sequence[Tuple[str, str, Optional[str], str]], status: str = "READY") -> None:
        now = utc_now()
        key = stable_id("social-checkpoint", provider, city, query or "")
        with self.connection:
            for external_id, content_hash, source_url, data in records:
                self.connection.execute(
                    "INSERT INTO social_connector_seen(id,provider,city,external_id,content_hash,source_url,seen_at,data) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(provider,city,external_id) DO UPDATE SET "
                    "id=excluded.id,content_hash=excluded.content_hash,source_url=excluded.source_url,"
                    "seen_at=excluded.seen_at,data=excluded.data",
                    (stable_id("social-seen", provider, city, external_id), provider, city,
                     external_id, content_hash, source_url, now, data))
            self.connection.execute(
                "INSERT INTO social_connector_checkpoints(id,provider,city,query,cursor,complete,status,updated_at,data) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,"
                "complete=excluded.complete,status=excluded.status,updated_at=excluded.updated_at,data=excluded.data",
                (key, provider, city, query, page.next_cursor, int(page.next_cursor is None),
                 status, now, json.dumps({"next_cursor": page.next_cursor}, ensure_ascii=False)))

    def start_run(self, provider: str, city: str, query: Optional[str]) -> str:
        run_id = stable_id("social-run", provider, city, query or "", utc_now())
        self.connection.execute(
            "INSERT INTO social_connector_runs(id,provider,city,query,started_at,ended_at,status,pages,emitted,error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, provider, city, query, utc_now(), None, "RUNNING", 0, 0, None))
        self.connection.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, pages: int, emitted: int, error: Optional[str] = None) -> None:
        self.connection.execute(
            "UPDATE social_connector_runs SET ended_at=?,status=?,pages=?,emitted=?,error=? WHERE id=?",
            (utc_now(), status, pages, emitted, error, run_id))
        self.connection.commit()


class SocialConnectorAdapter:
    """Paged adapter that feeds any declared social provider into Pipeline."""

    def __init__(self, provider: PagedSocialProvider, storage, *, query: Optional[str] = None,
                 page_size: int = 25, state: Optional[SQLiteSocialState] = None):
        self.provider = provider
        self.contract = provider.contract
        if type(page_size) is not int or not 1 <= page_size <= _MAX_PAGE_SIZE:
            raise ValueError("social page_size must be 1-100")
        self.page_size = page_size
        self.query = query
        self.state = state or SQLiteSocialState(getattr(storage, "connection", None))
        self.name = self.contract.provider
        self.capability = self.contract.adapter_capability()

    def raw_identity_payload(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        value = dict(payload)
        metadata = dict(value.get("source_metadata", {}))
        metadata.pop("captured_at", None)
        metadata.pop("query_run_id", None)
        value["source_metadata"] = metadata
        return value

    def collect(self, city: str) -> Iterable[SourceItem]:
        if not isinstance(city, str) or not city.strip():
            raise ValueError("social collection city is required")
        status = self.provider.status()
        if status.state == "AUTH_REQUIRED":
            raise AuthRequired("AUTH_REQUIRED: {}".format(self.contract.provider))
        if status.state in {"UNAVAILABLE", "UNKNOWN"}:
            raise SocialConnectorError("{} provider is {}".format(self.contract.provider, status.state))
        run_id = self.state.start_run(self.contract.provider, city, self.query)
        pages = 0
        emitted = 0
        cursor = None
        previous = self.state.checkpoint(self.contract.provider, city, self.query)
        # A complete cursor starts a new bounded scan.  The seen table prevents
        # unchanged posts from re-entering Pipeline while allowing new posts and
        # changed versions to be discovered on the next run.
        if previous and not previous.get("complete"):
            cursor = previous.get("cursor")
        try:
            while True:
                page = self.provider.fetch_page(city, self.query, cursor, self.page_size)
                if not isinstance(page, SocialPage):
                    raise SocialConnectorError("provider returned an invalid page")
                pages += 1
                outgoing: List[SourceItem] = []
                committed: List[Tuple[str, str, Optional[str], str]] = []
                next_ids = set()
                for record in page.records:
                    item = social_source_item(self.contract, record, query=self.query,
                                              auth_state=status.state)
                    if item.external_id in next_ids:
                        continue
                    next_ids.add(item.external_id)
                    content_hash = hashlib.sha256(
                        json.dumps(item.payload, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()
                    if self.state.seen_hash(self.contract.provider, city, item.external_id) == content_hash:
                        continue
                    outgoing.append(item)
                    committed.append((item.external_id, content_hash, item.source_url,
                                      json.dumps(item.payload, ensure_ascii=False, sort_keys=True)))
                # Commit only after the consumer has received this complete page.
                # If collection is interrupted halfway through, the cursor stays
                # at the previous page and retry remains safe and idempotent.
                for item in outgoing:
                    yield item
                    emitted += 1
                self.state.commit_page(self.contract.provider, city, self.query, page, committed)
                if page.next_cursor is None:
                    break
                if page.next_cursor == cursor:
                    raise SocialConnectorError("provider cursor did not advance")
                cursor = page.next_cursor
            self.state.finish_run(run_id, "SUCCEEDED", pages, emitted)
        except AuthRequired:
            self.state.finish_run(run_id, "AUTH_REQUIRED", pages, emitted, "login_or_authorization_required")
            raise
        except Exception as exc:
            detail = "{}: {}".format(type(exc).__name__, str(exc)[:180])
            self.state.finish_run(run_id, "FAILED", pages, emitted, detail)
            raise


class InputSocialProvider:
    """Bounded provider for user supplied browser captures or public JSON pages."""

    def __init__(self, contract: SocialProviderContract, pages: Sequence[SocialPage],
                 *, auth_state: str = "PUBLIC"):
        if not pages:
            raise ValueError("at least one social input page is required")
        if auth_state not in _AUTH_STATES:
            raise ValueError("invalid social input auth state")
        self.contract = contract
        self._pages = tuple(pages)
        self._auth_state = auth_state
        self._cursor_map = {None: self._pages[0]}
        for index, page in enumerate(self._pages[:-1]):
            cursor = page.next_cursor or "page-{}".format(index + 1)
            if cursor in self._cursor_map:
                raise ValueError("duplicate input page cursor")
            self._cursor_map[cursor] = self._pages[index + 1]
        if self._pages[-1].next_cursor is not None:
            # A final cursor is valid only when it has a matching page.
            if self._pages[-1].next_cursor not in self._cursor_map:
                raise ValueError("input page cursor has no following page")

    @property
    def auth_state(self) -> str:
        return self._auth_state

    def status(self) -> SocialProviderStatus:
        if self._auth_state == "AUTH_REQUIRED":
            return SocialProviderStatus(self.contract.provider, "AUTH_REQUIRED",
                                        self.contract.access_mode, True,
                                        "open the provider login page and retry")
        if self._auth_state == "UNAVAILABLE":
            return SocialProviderStatus(self.contract.provider, "UNAVAILABLE",
                                        self.contract.access_mode, self.contract.authenticated,
                                        "retry when the provider input is available")
        return SocialProviderStatus(self.contract.provider, self._auth_state,
                                    self.contract.access_mode,
                                    self.contract.authenticated,
                                    "ready")

    def fetch_page(self, city: str, query: Optional[str], cursor: Optional[str], limit: int) -> SocialPage:
        if self._auth_state == "AUTH_REQUIRED":
            raise AuthRequired("AUTH_REQUIRED: {}".format(self.contract.provider))
        if cursor not in self._cursor_map:
            raise SocialConnectorError("unknown input page cursor")
        page = self._cursor_map[cursor]
        if len(page.records) > limit:
            raise SocialConnectorError("input page exceeds connector page size")
        return page

    @classmethod
    def from_records(cls, contract: SocialProviderContract, records: Iterable[Mapping[str, Any]],
                     *, page_size: int = 25, auth_state: str = "PUBLIC") -> "InputSocialProvider":
        if type(page_size) is not int or not 1 <= page_size <= _MAX_PAGE_SIZE:
            raise ValueError("social page_size must be 1-100")
        normalized = [SocialRecord.from_mapping(item) for item in records]
        if not normalized:
            raise ValueError("at least one social input record is required")
        pages: List[SocialPage] = []
        for offset in range(0, len(normalized), page_size):
            end = min(offset + page_size, len(normalized))
            next_cursor = "page-{}".format(end) if end < len(normalized) else None
            pages.append(SocialPage(tuple(normalized[offset:end]), next_cursor))
        return cls(contract, pages, auth_state=auth_state)

    @classmethod
    def from_json(cls, contract: SocialProviderContract, path: str, *, page_size: int = 25,
                  auth_state: str = "PUBLIC") -> "InputSocialProvider":
        source = Path(path)
        value = json.loads(source.read_text(encoding="utf-8"))
        records = value.get("records") if isinstance(value, Mapping) else value
        if not isinstance(records, list):
            raise ValueError("social input JSON must contain a records array")
        return cls.from_records(contract, records, page_size=page_size, auth_state=auth_state)


class BrowserVisibleInputProvider(InputSocialProvider):
    """Input collected from content the user can see in a logged-in browser.

    This class accepts exported visible fields only.  It does not launch a
    browser, read cookies, or claim that a platform permits automated search.
    """

    @classmethod
    def from_records(cls, provider: str, records: Iterable[Mapping[str, Any]], *,
                     page_size: int = 25, authenticated: bool = True) -> "BrowserVisibleInputProvider":
        contract = SocialProviderContract(
            provider, "AUTHENTICATED_BROWSER", "browser_visible_input",
            supports_browser_input=True, supports_location_tags=False)
        instance = super().from_records(contract, records, page_size=page_size,
                                        auth_state="AUTHENTICATED" if authenticated else "AUTH_REQUIRED")
        return cls.__new_with_state(instance)

    @classmethod
    def from_json(cls, provider: str, path: str, *, page_size: int = 25,
                  authenticated: bool = True) -> "BrowserVisibleInputProvider":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        records = value.get("records") if isinstance(value, Mapping) else value
        if not isinstance(records, list):
            raise ValueError("social input JSON must contain a records array")
        return cls.from_records(provider, records, page_size=page_size,
                                authenticated=authenticated)

    @classmethod
    def __new_with_state(cls, instance: InputSocialProvider) -> "BrowserVisibleInputProvider":
        result = cls.__new__(cls)
        result.contract = instance.contract
        result._pages = instance._pages
        result._auth_state = instance._auth_state
        result._cursor_map = instance._cursor_map
        return result


class PublicFeedInputProvider(InputSocialProvider):
    """Input from a provider's documented public JSON feed."""

    @classmethod
    def from_records(cls, provider: str, records: Iterable[Mapping[str, Any]], *,
                     page_size: int = 25) -> "PublicFeedInputProvider":
        contract = SocialProviderContract(
            provider, "PUBLIC_FEED", "public_feed", supports_public_input=True,
            supports_location_tags=False)
        instance = super().from_records(contract, records, page_size=page_size,
                                        auth_state="PUBLIC")
        return cls.__new_with_state(instance)

    @classmethod
    def from_json(cls, provider: str, path: str, *, page_size: int = 25) -> "PublicFeedInputProvider":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        records = value.get("records") if isinstance(value, Mapping) else value
        if not isinstance(records, list):
            raise ValueError("social input JSON must contain a records array")
        return cls.from_records(provider, records, page_size=page_size)

    @classmethod
    def __new_with_state(cls, instance: InputSocialProvider) -> "PublicFeedInputProvider":
        result = cls.__new__(cls)
        result.contract = instance.contract
        result._pages = instance._pages
        result._auth_state = instance._auth_state
        result._cursor_map = instance._cursor_map
        return result
