"""Bounded, deterministic discovery of official structured datasets.

The first provider is the public Socrata catalog.  Discovery only produces
reviewable catalog candidates; it never feeds a newly found dataset into the
POI or Event pipeline.  All matching, qualification, and persistence here are
deterministic so a discovery run consumes zero LLM calls.
"""
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .models import (SourceDiscoveryCandidate, SourceDiscoveryCheckpoint,
                     SourceDiscoveryRunRecord)
from .util import safe_source_url, stable_id, utc_now


PROVIDER_NAME = "socrata-catalog"
DEFAULT_CATALOG_ENDPOINT = "https://api.us.socrata.com/api/catalog/v1"
DEFAULT_ALLOWED_DOMAIN = "data.cityofnewyork.us"
CHECKPOINT_STRATEGY = "SOCRATA_CATALOG_OFFSET_V1"
_DATASET_ID = re.compile(r"^[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$")


@dataclass(frozen=True)
class DiscoveryQuery:
    name: str
    terms: Tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryConfig:
    provider: str
    catalog_endpoint: str
    allowed_domains: Tuple[str, ...]
    queries: Tuple[DiscoveryQuery, ...]
    limit: int = 10
    max_pages: int = 1
    timeout: int = 15
    max_retries: int = 1
    retry_delay: float = 0.0
    schema_probe_limit: int = 10
    schedule: str = "weekly"
    supported_datasets: Dict[str, Dict[str, Any]] = None


def load_discovery_config(path: str) -> DiscoveryConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    provider = str(data.get("provider", PROVIDER_NAME)).strip()
    if provider != PROVIDER_NAME:
        raise ValueError("unsupported discovery provider: {}".format(provider))
    endpoint = safe_source_url(str(data.get("catalog_endpoint", DEFAULT_CATALOG_ENDPOINT)))
    domains = tuple(str(value).strip().lower().rstrip(".") for value in data.get(
        "allowed_domains", (DEFAULT_ALLOWED_DOMAIN,)) if str(value).strip())
    if domains != (DEFAULT_ALLOWED_DOMAIN,):
        raise ValueError("first discovery provider only allows {}".format(DEFAULT_ALLOWED_DOMAIN))
    queries: List[DiscoveryQuery] = []
    for value in data.get("queries", []):
        if not isinstance(value, dict):
            raise ValueError("discovery query must be an object")
        name = str(value.get("name", "")).strip()
        terms = tuple(dict.fromkeys(str(term).strip() for term in value.get("terms", ())
                                   if str(term).strip()))
        if not name or not terms:
            raise ValueError("discovery query requires name and terms")
        queries.append(DiscoveryQuery(name, terms))
    if not queries:
        raise ValueError("discovery configuration has no queries")
    limit = int(data.get("limit", 10))
    max_pages = int(data.get("max_pages", 1))
    timeout = int(data.get("timeout", 15))
    max_retries = int(data.get("max_retries", 1))
    schema_probe_limit = int(data.get("schema_probe_limit", 10))
    if not 1 <= limit <= 100:
        raise ValueError("discovery limit must be 1-100")
    if not 1 <= max_pages <= 20:
        raise ValueError("discovery max_pages must be 1-20")
    if not 2 <= timeout <= 60:
        raise ValueError("discovery timeout must be 2-60 seconds")
    if not 0 <= max_retries <= 3:
        raise ValueError("discovery max_retries must be 0-3")
    if not 0 <= schema_probe_limit <= 100:
        raise ValueError("schema_probe_limit must be 0-100")
    supported = {}
    for dataset_id, descriptor in (data.get("supported_datasets", {}) or {}).items():
        if not _DATASET_ID.fullmatch(str(dataset_id)) or not isinstance(descriptor, dict):
            raise ValueError("supported dataset mapping is invalid")
        required_fields = tuple(str(value).strip() for value in descriptor.get("required_fields", ())
                                if str(value).strip())
        supported[str(dataset_id)] = {
            "family": str(descriptor.get("family", dataset_id)).strip() or str(dataset_id),
            "required_fields": required_fields,
        }
    return DiscoveryConfig(
        provider=provider, catalog_endpoint=endpoint, allowed_domains=domains,
        queries=tuple(queries), limit=limit, max_pages=max_pages, timeout=timeout,
        max_retries=max_retries, retry_delay=float(data.get("retry_delay", 0.0)),
        schema_probe_limit=schema_probe_limit, schedule=str(data.get("schedule", "weekly")),
        supported_datasets=supported)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("name", "displayName", "display_name", "title", "value"):
            if value.get(key):
                return str(value[key]).strip()
        return ""
    return str(value).strip()


def _tags(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        values = value
    elif isinstance(value, str):
        values = re.split(r"[,;|]", value)
    else:
        values = []
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _updated_at(*values: Any) -> Optional[str]:
    for value in values:
        text = _as_text(value)
        if not text:
            continue
        # Keep the upstream representation but reject arbitrary prose.  This
        # is used for comparison/audit only, never for scheduling decisions.
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        return text
    return None


def _host(value: Optional[str]) -> str:
    if not value:
        return ""
    return (urlsplit(value).hostname or "").lower().rstrip(".")


def _safe_catalog_url(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        return safe_source_url(str(value))
    except ValueError:
        return None


def _field_names(payload: Any) -> List[str]:
    columns = payload.get("columns") if isinstance(payload, dict) else None
    if not isinstance(columns, list):
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        columns = metadata.get("columns") if isinstance(metadata, dict) else None
    names = []
    for column in columns or ():
        if isinstance(column, dict):
            name = column.get("fieldName") or column.get("name") or column.get("field_name")
        else:
            name = column
        if name and str(name).strip():
            names.append(str(name).strip())
    return list(dict.fromkeys(names))


def _schema_qualification_reasons(fields: Sequence[str]) -> List[str]:
    """Require deterministic identity, time, and location clues for review."""
    normalized = [re.sub(r"[^a-z0-9]", "", str(value).casefold()) for value in fields]
    identity = any(value in ("id", "guid", "globalid", "objectid", "camis", "parkid",
                             "gisobjid", "uniqueid") or value.endswith("id")
                   for value in normalized)
    temporal = any(any(token in value for token in ("time", "date", "start", "end",
                                                    "updated", "created", "year"))
                   for value in normalized)
    location = any(any(token in value for token in ("lat", "lon", "longitude", "latitude",
                                                     "location", "address", "borough", "parkname",
                                                     "coordinates", "geom", "point"))
                   for value in normalized)
    reasons = []
    if not identity:
        reasons.append("SCHEMA_IDENTITY_FIELD_MISSING")
    if not temporal:
        reasons.append("SCHEMA_TIME_FIELD_MISSING")
    if not location:
        reasons.append("SCHEMA_LOCATION_FIELD_MISSING")
    return reasons


class SocrataCatalogProvider:
    """Small HTTP client with bounded retries and request counters."""

    name = PROVIDER_NAME

    def __init__(self, config: DiscoveryConfig, *, opener=urlopen):
        self.config = config
        self.opener = opener
        self.http_requests = 0
        self.retry_count = 0
        self.endpoints: List[str] = []

    def _request_json(self, url: str) -> Dict[str, Any]:
        attempt = 0
        while True:
            self.http_requests += 1
            request = Request(url, headers={"User-Agent": "LAND-Data/source-discovery"})
            try:
                with self.opener(request, timeout=self.config.timeout) as response:
                    body = response.read(500_001)
                    status = getattr(response, "status", 200)
                if status >= 400:
                    raise ValueError("HTTP_{}".format(status))
                if len(body) > 500_000:
                    raise ValueError("RESPONSE_TOO_LARGE")
                decoded = json.loads(body)
                if not isinstance(decoded, dict):
                    raise ValueError("MALFORMED_JSON")
                self.endpoints.append(urlsplit(url).netloc)
                return decoded
            except (OSError, ValueError, json.JSONDecodeError, HTTPError, URLError) as exc:
                if attempt >= self.config.max_retries:
                    raise RuntimeError("{}: {}".format(type(exc).__name__, str(exc))) from exc
                attempt += 1
                self.retry_count += 1
                if self.config.retry_delay:
                    time.sleep(self.config.retry_delay * (2 ** (attempt - 1)))

    def search(self, term: str, offset: int) -> List[Dict[str, Any]]:
        params = urlencode({"q": term, "domains": ",".join(self.config.allowed_domains),
                            "only": "dataset", "limit": str(self.config.limit),
                            "offset": str(offset)})
        payload = self._request_json(self.config.catalog_endpoint + "?" + params)
        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError("CATALOG_RESULTS_MISSING")
        return [row for row in results if isinstance(row, dict)]

    def probe_schema(self, domain: str, dataset_id: str) -> Tuple[List[str], str, str]:
        metadata_url = "https://{}/api/views/{}".format(domain, dataset_id)
        payload = self._request_json(metadata_url)
        fields = _field_names(payload)
        if not fields:
            raise RuntimeError("SCHEMA_FIELDS_MISSING")
        fingerprint = stable_id("schema", fields)
        return fields, metadata_url, fingerprint


class SourceDiscoveryRunner:
    """Run configured catalog queries and persist only source candidates."""

    def __init__(self, storage, config: DiscoveryConfig, *, opener=urlopen,
                 now=utc_now):
        self.storage = storage
        self.config = config
        self.now = now
        self.provider = SocrataCatalogProvider(config, opener=opener)
        self._schema_probes = 0
        self._run_seen = set()

    def _candidate_from_result(self, result: Dict[str, Any], term: str,
                               discovered_at: str) -> Tuple[Optional[SourceDiscoveryCandidate], Optional[str]]:
        resource = result.get("resource") if isinstance(result.get("resource"), dict) else {}
        classification = result.get("classification") if isinstance(result.get("classification"), dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        dataset_id = _as_text(resource.get("id") or result.get("id") or metadata.get("id"))
        title = _as_text(resource.get("name") or result.get("name") or result.get("title"))
        asset_type = _as_text(resource.get("type") or classification.get("assetType") or result.get("assetType"))
        if asset_type and asset_type.casefold() not in ("dataset", "data"):
            return None, "NON_DATASET_ASSET"
        if not dataset_id or not _DATASET_ID.fullmatch(dataset_id):
            return None, "INVALID_DATASET_ID"
        catalog_url = _safe_catalog_url(result.get("permalink") or resource.get("url"))
        domain = (_as_text(resource.get("domain") or metadata.get("domain")) or
                  _host(catalog_url)).lower().rstrip(".")
        if domain not in self.config.allowed_domains:
            return None, "NON_ALLOWLIST_DOMAIN"
        if catalog_url and _host(catalog_url) not in self.config.allowed_domains:
            return None, "NON_ALLOWLIST_CATALOG_URL"
        api_endpoint = "https://{}/resource/{}.json".format(domain, dataset_id)
        publisher = _as_text(resource.get("publisher") or metadata.get("publisher") or
                             metadata.get("owner") or result.get("publisher") or
                             result.get("owner") or result.get("creator"))
        description = _as_text(resource.get("description") or metadata.get("description") or
                               result.get("description"))
        tags = _tags(resource.get("tags") or metadata.get("tags") or result.get("tags") or
                     classification.get("domain_tags") or classification.get("categories"))
        updated = _updated_at(resource.get("updatedAt"), resource.get("updated_at"),
                              resource.get("data_updated_at"),
                              metadata.get("updatedAt"), metadata.get("updated_at"),
                              metadata.get("publicationDate"), result.get("updatedAt"))
        candidate_id = stable_id("source-discovery", self.config.provider, domain, dataset_id)
        existing = self.storage.get(SourceDiscoveryCandidate, candidate_id)
        reasons = ["CATALOG_MATCH"]
        if not asset_type:
            reasons.append("ASSET_TYPE_MISSING")
        if not updated:
            reasons.append("UPDATED_AT_MISSING")
        schema_fields: List[str] = list(existing.schema_fields) if existing else []
        schema_checked = existing.schema_checked if existing else False
        schema_fingerprint = existing.schema_fingerprint if existing else None
        metadata_url = (existing.metadata_url if existing and existing.metadata_url else
                        "https://{}/api/views/{}".format(domain, dataset_id))
        status = "NEEDS_REVIEW" if (not asset_type or not updated) else "DISCOVERED"
        if self._schema_probes < self.config.schema_probe_limit:
            try:
                schema_fields, metadata_url, schema_fingerprint = self.provider.probe_schema(domain, dataset_id)
                schema_checked = True
                reasons.append("SCHEMA_PROBED")
                schema_reasons = _schema_qualification_reasons(schema_fields)
                if schema_reasons:
                    status = "NEEDS_REVIEW"
                    reasons.extend(schema_reasons)
                supported = (self.config.supported_datasets or {}).get(dataset_id)
                if supported:
                    required = {str(value).casefold() for value in supported.get("required_fields", ())}
                    actual = {str(value).casefold() for value in schema_fields}
                    if required.issubset(actual) and not schema_reasons:
                        status = "SUPPORTED"
                        reasons.append("SUPPORTED_FAMILY:{}".format(supported["family"]))
                    elif required:
                        status = "NEEDS_REVIEW"
                        reasons.append("SUPPORTED_SCHEMA_FIELDS_MISSING")
            except RuntimeError as exc:
                status = "NEEDS_REVIEW"
                error_code = str(exc).split(":", 1)[0]
                if error_code in ("OSError", "URLError", "HTTPError", "RuntimeError"):
                    error_code = "SCHEMA_UNAVAILABLE"
                reasons.append(error_code)
            finally:
                self._schema_probes += 1
        else:
            if not schema_checked:
                status = "NEEDS_REVIEW"
                reasons.append("SCHEMA_NOT_PROBED_BOUNDED")
        discovered_queries = ([existing.discovered_query] if existing else [])
        if existing:
            discovered_queries.extend(existing.metadata.get("discovered_queries", ()))
        discovered_queries.append(term)
        discovered_queries = list(dict.fromkeys(value for value in discovered_queries if value))
        data = {
            "catalog_url": catalog_url,
            "fingerprint": stable_id("candidate", dataset_id, title, domain, publisher,
                                      description, tags, api_endpoint, updated, asset_type,
                                      schema_fields),
            "discovered_queries": discovered_queries,
        }
        if existing and existing.metadata.get("fingerprint") == data["fingerprint"]:
            reasons = list(dict.fromkeys(existing.reasons + reasons))
            status = existing.status if existing.status in ("REJECTED", "SUPPORTED") else status
        elif existing:
            reasons.append("SOURCE_METADATA_UPDATED")
            if existing.status not in ("REJECTED", "SUPPORTED"):
                status = "NEEDS_REVIEW"
        candidate = SourceDiscoveryCandidate(
            id=candidate_id, provider=self.config.provider, dataset_id=dataset_id,
            title=title, domain=domain, publisher=publisher, description=description,
            tags=tags, api_endpoint=api_endpoint, updated_at=updated,
            discovered_query=(existing.discovered_query if existing else term),
            discovered_at=(existing.discovered_at if existing else discovered_at),
            last_checked_at=discovered_at, status=status, reasons=list(dict.fromkeys(reasons)),
            asset_type=asset_type or "dataset", metadata_url=metadata_url,
            catalog_url=catalog_url, schema_fields=schema_fields, schema_checked=schema_checked,
            schema_fingerprint=schema_fingerprint, metadata=data)
        return candidate, None

    def _save_checkpoint(self, query: str, cursor: int, complete: bool) -> None:
        self.storage.put(SourceDiscoveryCheckpoint(
            id=stable_id("source-discovery-checkpoint", self.config.provider, query),
            provider=self.config.provider, query=query, cursor=cursor,
            cycle_complete=complete, updated_at=self.now(), strategy=CHECKPOINT_STRATEGY))

    def _run_query(self, query_key: str, search_term: str,
                   run_stats: Dict[str, int], discovered_at: str) -> Dict[str, Any]:
        checkpoint = self.storage.get_discovery_checkpoint(self.config.provider, query_key)
        offset = checkpoint.cursor if checkpoint and not checkpoint.cycle_complete else 0
        pages = 0
        complete = False
        while pages < self.config.max_pages:
            rows = self.provider.search(search_term, offset)
            pages += 1
            run_stats["query_pages"] += 1
            if not rows:
                complete = True
                self._save_checkpoint(query_key, offset, True)
                break
            for result in rows:
                candidate, filtered_reason = self._candidate_from_result(result, search_term, discovered_at)
                if filtered_reason:
                    run_stats["filtered_count"] += 1
                    continue
                if candidate is None:
                    continue
                existing = self.storage.get(SourceDiscoveryCandidate, candidate.id)
                if existing and existing.metadata.get("fingerprint") == candidate.metadata.get("fingerprint"):
                    run_stats["duplicate_count"] += 1
                elif existing:
                    run_stats["updated_count"] += 1
                else:
                    run_stats["new_count"] += 1
                self.storage.put(candidate)
                self._run_seen.add(candidate.id)
            offset += len(rows)
            self._save_checkpoint(query_key, offset, False)
            if len(rows) < self.config.limit:
                complete = True
                self._save_checkpoint(query_key, offset, True)
                break
        return {"query": query_key, "search_term": search_term, "pages": pages,
                "cursor": offset, "cycle_complete": complete}

    def run(self) -> Dict[str, Any]:
        started = self.now()
        run_id = stable_id("source-discovery-run", self.config.provider, started)
        run = SourceDiscoveryRunRecord(id=run_id, provider=self.config.provider,
                                       started_at=started, ended_at=None)
        self.storage.put(run)
        stats = {"new_count": 0, "updated_count": 0, "duplicate_count": 0,
                 "filtered_count": 0, "query_pages": 0}
        query_results = []
        failures = []
        self._schema_probes = 0
        self._run_seen = set()
        query_pairs = [(topic.name, term) for topic in self.config.queries for term in topic.terms]
        query_pairs = list(dict.fromkeys(query_pairs))
        for topic_name, term in query_pairs:
            query_key = "{}:{}".format(topic_name, term)
            try:
                query_results.append(self._run_query(query_key, term, stats, started))
            except Exception as exc:
                failures.append({"query": query_key, "reason": "{}: {}".format(type(exc).__name__, str(exc))})
        ended = self.now()
        run.http_requests = self.provider.http_requests
        run.retry_count = self.provider.retry_count
        run.query_count = len(query_pairs)
        run.new_count = stats["new_count"]
        run.updated_count = stats["updated_count"]
        run.duplicate_count = stats["duplicate_count"]
        run.filtered_count = stats["filtered_count"]
        run.ended_at = ended
        run.failure_reason = "; ".join(item["query"] + "=" + item["reason"] for item in failures) or None
        incomplete = any(not item["cycle_complete"] for item in query_results)
        if failures and not query_results:
            run.status = "FAILED"
        elif failures:
            run.status = "PARTIAL_FAILURE"
        else:
            run.status = "SUCCEEDED"
        run.next_run_status = "RETRY_REQUIRED" if failures else ("CONTINUE_REQUIRED" if incomplete else "NOT_NEEDED")
        run.data = {"queries": query_results, "failures": failures,
                    "query_pages": stats["query_pages"],
                    "endpoints": list(dict.fromkeys(self.provider.endpoints)),
                    "schema_probes": self._schema_probes,
                    "llm_calls": 0}
        self.storage.put(run)
        return {"provider": self.config.provider, "status": run.status,
                "next_run_status": run.next_run_status, "started_at": started,
                "ended_at": ended, "new_count": run.new_count,
                "updated_count": run.updated_count, "duplicate_count": run.duplicate_count,
                "filtered_count": run.filtered_count, "http_requests": run.http_requests,
                "retry_count": run.retry_count, "query_count": run.query_count,
                "schema_probes": self._schema_probes, "queries": query_results,
                "failures": failures, "llm_calls": 0}


def candidate_dicts(storage) -> List[Dict[str, Any]]:
    return [asdict(item) for item in storage.all(SourceDiscoveryCandidate)]


def run_dicts(storage, limit: int = 20) -> List[Dict[str, Any]]:
    items = storage.all(SourceDiscoveryRunRecord)
    items = sorted(items, key=lambda item: item.started_at, reverse=True)
    return [asdict(item) for item in items[:max(1, limit)]]
