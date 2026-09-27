"""Safe POI -> LAND Place publication bridge.

This bridge is intentionally separate from the community/QAU bridge.  It only
publishes an explicitly selected set of already validated POIs.  Editorial
copy comes from a bilingual publication overlay supplied by a human; source
post text, recommendation text and inspection grades are never read while
building a Place.

The release directory contains a complete LAND ``ContentPack`` (status
``published``), a frozen input manifest and an append-only-ish verification
record.  Verification re-reads the POI, its provenance and review decisions so
that a changed source or a changed review cannot silently reach LAND.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .models import FieldProvenance, POI, POICandidate, ReviewDecision, SourceContent
from .storage import Storage
from .util import stable_id, utc_now


SCHEMA_VERSION = 1
PACK_FILE = "content-pack.json"
MANIFEST_FILE = "manifest.json"
RELEASE_PREFIX = "place-"
LANGUAGES = ("zh-Hans", "en")
PLACE_CATEGORY_MAP = {"restaurant": "food", "cafe": "food", "museum": "sight"}
PLACE_FIELDS = ("name", "localName", "address", "reason", "caution")
DESTINATION_FIELDS = ("name", "localName", "summary")
AUTO_APPROVAL_POLICY_VERSION = "deterministic_official_v1"
# This is deliberately an explicit allowlist. OSM and all community/social
# transports remain human-review only even though they can be structured.
DETERMINISTIC_OFFICIAL_SOURCES = frozenset({
    "official-directory",
    "nyc-open-data-restaurants",
})
AUTO_REQUIRED_PROVENANCE_FIELDS = ("names", "address", "latitude", "longitude", "category")
AUTO_REASON_EN = (
    "Verified location record from a structured public source; provided for planning and not a personal recommendation."
)
AUTO_REASON_ZH = "这是来自结构化公共来源的已核验地点记录，用于行程参考，不代表个人推荐。"
AUTO_CAUTION_EN = "Opening hours and availability can change; check the official source before departure."
AUTO_CAUTION_ZH = "营业时间和现场情况可能变化，出发前请复核官方来源。"
AUTO_SUMMARY_EN = "Verified structured places for {city}."
AUTO_SUMMARY_ZH = "{city} 的结构化已核验地点记录。"
_HEALTH_GRADE = (
    "卫生等级", "卫生评级", "inspection grade", "health grade", "grade a", "grade b", "grade c",
    "a级", "b级", "c级", "a 等级", "b 等级", "c 等级"
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    temporary.write_text(_canonical(value) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def _iso(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value.strip())
    return (parsed.scheme == "https" and bool(parsed.hostname) and
            not parsed.username and not parsed.password and
            " " not in value and "\n" not in value)


def _safe_id(value: str) -> bool:
    return bool(isinstance(value, str) and value and
                all(char.isalnum() or char in "_-" for char in value) and
                value[0].isalnum())


def _city_text(value: Any) -> str:
    """Return a comparison form for an explicitly configured city label.

    This deliberately only normalises case and surrounding/internal
    whitespace.  It does not slugify arbitrary text, translate names, or
    perform fuzzy matching: a display name or alias must be present in the
    configured city profile for it to match the stable city id.
    """
    return " ".join(str(value).strip().split()).casefold() if isinstance(value, str) else ""


def _profile_labels(city_id: str, profile: Optional[Dict[str, Any]]) -> List[str]:
    labels = [city_id]
    if not isinstance(profile, dict):
        return labels
    for key in ("id", "city_id", "display_name"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            labels.append(value)
    aliases = profile.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    if isinstance(aliases, list):
        labels.extend(value for value in aliases if isinstance(value, str) and value.strip())
    return labels


def resolve_city_id(requested: str, city_profiles: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Resolve a stable city id using only exact configured labels.

    The command line accepts a stable id, a configured display name, or an
    explicit alias.  Ambiguous and unknown labels are errors so a release can
    never silently select a similarly named city.
    """
    if not isinstance(requested, str) or not requested.strip() or not isinstance(city_profiles, dict):
        raise ValueError("city must be a configured stable id, display_name, or alias")
    wanted = _city_text(requested)
    matches: List[Tuple[str, Dict[str, Any]]] = []
    for city_id, profile in city_profiles.items():
        if not isinstance(city_id, str) or not isinstance(profile, dict):
            continue
        if any(wanted == _city_text(label) for label in _profile_labels(city_id, profile)):
            matches.append((city_id, profile))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("city label is ambiguous; use the stable city id")
    raise ValueError("unknown city label; add an explicit display_name or aliases entry")


def _city_matches(observed: Any, city: str, city_profile: Optional[Dict[str, Any]]) -> bool:
    """Check a POI/overlay city against one canonical id and its profile."""
    observed_text = _city_text(observed)
    return bool(observed_text) and any(
        observed_text == _city_text(label) for label in _profile_labels(city, city_profile)
    )


def _blocker(poi_id: str, code: str, message: str, **extra: Any) -> Dict[str, Any]:
    result = {"poi_id": poi_id, "code": code, "message": message}
    result.update(extra)
    return result


def _overlay_value(overlay: Dict[str, Any], key: str) -> Any:
    value = overlay.get(key)
    if value is None and key == "destination":
        value = overlay.get("destinations")
    return value


def _normalise_overlay(overlay: Any, city: str, poi_ids: Sequence[str],
                       city_profile: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Normalise one human publication overlay and return blockers."""
    blockers: List[Dict[str, Any]] = []
    if not isinstance(overlay, dict):
        code = "OVERLAY_MISSING" if overlay is None else "OVERLAY_INVALID"
        message = ("publication overlay is required before a Place can be published"
                   if overlay is None else "publication overlay must be a JSON object")
        return {}, [_blocker("*", code, message)]
    declared_city = overlay.get("city")
    if declared_city is not None and not _city_matches(declared_city, city, city_profile):
        blockers.append(_blocker("*", "CITY_MISMATCH", "overlay city differs from the explicit release city",
                                 expected_city=city, observed_city=declared_city))
    destination = _overlay_value(overlay, "destination")
    if not isinstance(destination, dict):
        blockers.append(_blocker("*", "DESTINATION_OVERLAY_MISSING", "bilingual destination overlay is required"))
        destination = {}
    places = overlay.get("places")
    if isinstance(places, list):
        converted = {}
        for item in places:
            if isinstance(item, dict) and isinstance(item.get("poi_id"), str):
                converted[item["poi_id"]] = item.get("translations", item)
        places = converted
    if not isinstance(places, dict):
        blockers.append(_blocker("*", "PLACE_OVERLAY_MISSING", "bilingual place overlays are required"))
        places = {}
    normalised = {"city": city, "destination": {}, "places": {},
                  "_metadata": {"mode": "human_bilingual", "fallback": False}}
    for language in LANGUAGES:
        value = destination.get(language)
        if not isinstance(value, dict):
            blockers.append(_blocker("*", "DESTINATION_TRANSLATION_MISSING",
                                     "destination overlay needs both languages", language=language))
            value = {}
        clean = {field: str(value.get(field, "")).strip() for field in DESTINATION_FIELDS}
        for field in DESTINATION_FIELDS:
            if not clean[field]:
                blockers.append(_blocker("*", "DESTINATION_FIELD_MISSING",
                                         "destination overlay field is empty", language=language, field=field))
        normalised["destination"][language] = clean
    selected = set(poi_ids)
    supplied = set(places)
    for extra in sorted(supplied - selected):
        blockers.append(_blocker(extra, "OVERLAY_POI_NOT_SELECTED", "overlay contains a POI not in --poi-id"))
    for poi_id in poi_ids:
        value = places.get(poi_id)
        if not isinstance(value, dict):
            blockers.append(_blocker(poi_id, "PLACE_TRANSLATION_MISSING", "place overlay is missing"))
            continue
        normalised["places"][poi_id] = {}
        for language in LANGUAGES:
            translation = value.get(language)
            if not isinstance(translation, dict):
                blockers.append(_blocker(poi_id, "PLACE_TRANSLATION_MISSING",
                                         "place overlay needs both languages", language=language))
                translation = {}
            clean = {field: str(translation.get(field, "")).strip() for field in PLACE_FIELDS}
            for field in PLACE_FIELDS:
                if not clean[field]:
                    blockers.append(_blocker(poi_id, "PLACE_FIELD_MISSING",
                                             "place overlay field is empty", language=language, field=field))
                # A health inspection grade is source data, not editorial copy
                # for a LAND reason/caution.  Reject it explicitly.
                if field in ("reason", "caution") and any(
                        marker in clean[field].lower() for marker in _HEALTH_GRADE):
                    blockers.append(_blocker(poi_id, "HEALTH_GRADE_IN_PUBLICATION_COPY",
                                             "reason/caution cannot use a hygiene grade"))
            normalised["places"][poi_id][language] = clean
    return normalised, blockers


def _poi_provenance(storage: Storage, poi_id: str) -> List[FieldProvenance]:
    return sorted([item for item in storage.all(FieldProvenance)
                   if item.entity_type == "POI" and item.entity_id == poi_id],
                  key=lambda item: (item.field_name, item.id))


def _poi_decisions(storage: Storage, poi: POI) -> List[ReviewDecision]:
    decisions = list(storage.review_history("POI", poi.id))
    for candidate in storage.all(POICandidate):
        if candidate.resolved_poi_id == poi.id:
            decisions.extend(storage.review_history("POICandidate", candidate.id))
    return sorted(decisions, key=lambda item: (item.created_at, item.id))


def _source_urls(storage: Storage, poi: POI, provenance: Sequence[FieldProvenance]) -> List[str]:
    urls = {item.source_reference_url for item in provenance if _url(item.source_reference_url)}
    if _url(poi.website):
        urls.add(poi.website.strip())
    content_ids = {item.source_content_id for item in provenance if item.source_content_id}
    for content in storage.all(SourceContent):
        if content.id in content_ids:
            for value in (content.source_url, content.canonical_url):
                if _url(value):
                    urls.add(value.strip())
    return sorted(urls)


def _has_conflict_marker(value: Any) -> bool:
    """Detect source-side conflict markers without interpreting source prose."""
    if isinstance(value, dict):
        for key, child in value.items():
            if "conflict" in str(key).casefold() and child:
                return True
            if _has_conflict_marker(child):
                return True
    elif isinstance(value, list):
        return any(_has_conflict_marker(child) for child in value)
    return False


def _auto_policy_evidence(storage: Storage, poi: Optional[POI]) -> Dict[str, Any]:
    """Return an auditable, read-only eligibility record for auto publication."""
    evidence: Dict[str, Any] = {
        "policy_version": AUTO_APPROVAL_POLICY_VERSION,
        "eligible": False,
        "source_allowlist": sorted(DETERMINISTIC_OFFICIAL_SOURCES),
        "required_provenance_fields": list(AUTO_REQUIRED_PROVENANCE_FIELDS),
        "source_platforms": [],
        "source_urls": [],
        "field_coverage": {},
        "thresholds": {
            "active": True,
            "map_visibility": True,
            "coordinates_verified": True,
            "validation_passed": True,
            "review_status": "AUTO_APPROVED",
            "category": sorted(PLACE_CATEGORY_MAP),
            "structured_source": True,
            "stable_https_source": True,
            "no_conflict_or_pending_reason": True,
        },
        "reasons": [],
    }
    if poi is None:
        evidence["reasons"].append("POI_NOT_FOUND")
        return evidence
    if not poi.active:
        evidence["reasons"].append("ACTIVE_REQUIRED")
    if not poi.map_visibility:
        evidence["reasons"].append("MAP_VISIBILITY_REQUIRED")
    if not poi.coordinates_verified:
        evidence["reasons"].append("COORDINATES_VERIFIED_REQUIRED")
    if not poi.validation_passed:
        evidence["reasons"].append("VALIDATION_PASSED_REQUIRED")
    if poi.review_status != "AUTO_APPROVED":
        evidence["reasons"].append("AUTO_APPROVED_REQUIRED")
    if poi.category not in PLACE_CATEGORY_MAP:
        evidence["reasons"].append("CATEGORY_NOT_ALLOWED")
    if not poi.names or not any(str(value).strip() for value in poi.names.values()):
        evidence["reasons"].append("NAME_REQUIRED")
    if not str(poi.address or "").strip():
        evidence["reasons"].append("ADDRESS_REQUIRED")
    if not _iso(poi.last_verified_at):
        evidence["reasons"].append("LAST_VERIFIED_AT_MISSING")
    if poi.review_reasons:
        evidence["reasons"].append("POI_HAS_REVIEW_REASON")

    provenance = _poi_provenance(storage, poi.id)
    field_coverage: Dict[str, int] = {}
    for item in provenance:
        field_coverage[item.field_name] = field_coverage.get(item.field_name, 0) + 1
    evidence["field_coverage"] = dict(sorted(field_coverage.items()))
    missing_fields = [field for field in AUTO_REQUIRED_PROVENANCE_FIELDS
                      if not field_coverage.get(field)]
    if missing_fields:
        evidence["reasons"].append("PROVENANCE_FIELDS_MISSING:" + ",".join(missing_fields))

    content_by_id = {item.id: item for item in storage.all(SourceContent)}
    platforms = set()
    urls = set()
    for item in provenance:
        source = str(item.source or "")
        platforms.add(source)
        content = content_by_id.get(item.source_content_id)
        if source not in DETERMINISTIC_OFFICIAL_SOURCES:
            evidence["reasons"].append("SOURCE_NOT_ALLOWLISTED:" + source)
        if content is None:
            evidence["reasons"].append("PROVENANCE_SOURCE_CONTENT_MISSING")
            continue
        platform = str(content.platform or "")
        platforms.add(platform)
        if platform not in DETERMINISTIC_OFFICIAL_SOURCES:
            evidence["reasons"].append("SOURCE_PLATFORM_NOT_ALLOWLISTED:" + platform)
        if platform != source:
            evidence["reasons"].append("PROVENANCE_SOURCE_MISMATCH")
        if content.content_status != "ACTIVE":
            evidence["reasons"].append("SOURCE_CONTENT_NOT_ACTIVE")
        raw_metadata = content.raw_metadata if isinstance(content.raw_metadata, dict) else {}
        if raw_metadata.get("source_kind") != "structured":
            evidence["reasons"].append("STRUCTURED_SOURCE_REQUIRED")
        if _has_conflict_marker(raw_metadata):
            evidence["reasons"].append("SOURCE_CONFLICT_MARKER")
        if not _iso(content.collected_at):
            evidence["reasons"].append("SOURCE_COLLECTED_AT_MISSING")
        for value in (content.source_url, content.canonical_url, item.source_reference_url):
            if _url(value):
                urls.add(value.strip())
        if not any(_url(value) for value in (content.source_url, content.canonical_url,
                                             item.source_reference_url)):
            evidence["reasons"].append("STABLE_HTTPS_SOURCE_MISSING")
        if not _iso(item.collected_at) or not _iso(item.verified_at):
            evidence["reasons"].append("PROVENANCE_VERIFICATION_TIMESTAMP_MISSING")
    evidence["source_platforms"] = sorted(platforms)
    evidence["source_urls"] = sorted(urls)
    evidence["reasons"] = sorted(set(evidence["reasons"]))
    evidence["eligible"] = not evidence["reasons"]
    return evidence


def _snapshot(storage: Storage, poi: POI, overlay: Dict[str, Any],
              approval_evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    provenance = [asdict(item) for item in _poi_provenance(storage, poi.id)]
    decisions = [asdict(item) for item in _poi_decisions(storage, poi)]
    return {"poi": asdict(poi), "provenance": provenance,
            "review_decisions": decisions, "overlay": overlay,
            "approval_evidence": approval_evidence}


def _check_poi(storage: Storage, poi: Optional[POI], city: str,
               overlay: Dict[str, Any], city_profile: Optional[Dict[str, Any]] = None,
               allow_deterministic_official: bool = False
               ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    if poi is None:
        return None, [_blocker("*", "POI_NOT_FOUND", "selected POI does not exist")]
    blockers: List[Dict[str, Any]] = []
    if not _city_matches(poi.city, city, city_profile):
        blockers.append(_blocker(poi.id, "CITY_MISMATCH", "POI is outside the explicit release city",
                                 expected_city=city, observed_city=poi.city))
    if poi.category not in PLACE_CATEGORY_MAP:
        blockers.append(_blocker(poi.id, "UNKNOWN_CATEGORY", "only restaurant, cafe and museum can become a LAND Place",
                                 category=poi.category))
    for field, expected in (("map_visibility", True), ("active", True),
                            ("coordinates_verified", True), ("validation_passed", True)):
        if getattr(poi, field) is not expected:
            blockers.append(_blocker(poi.id, "UNSAFE_POI_STATUS", "POI gate is not satisfied", field=field))
    if poi.review_status != "AUTO_APPROVED":
        blockers.append(_blocker(poi.id, "REVIEW_STATUS_UNSAFE", "POI must be AUTO_APPROVED",
                                 status=poi.review_status))
    if not _iso(poi.last_verified_at):
        blockers.append(_blocker(poi.id, "LAST_VERIFIED_AT_MISSING", "POI needs a timezone-aware last_verified_at"))
    auto_evidence = _auto_policy_evidence(storage, poi)
    provenance = _poi_provenance(storage, poi.id)
    if not provenance:
        blockers.append(_blocker(poi.id, "PROVENANCE_MISSING", "POI provenance is required"))
    if not _source_urls(storage, poi, provenance):
        blockers.append(_blocker(poi.id, "STABLE_HTTPS_SOURCE_MISSING", "POI needs a stable HTTPS source"))
    decisions = _poi_decisions(storage, poi)
    if (not decisions and not (allow_deterministic_official and auto_evidence["eligible"])) or (
            decisions and not any(item.new_status == "AUTO_APPROVED" for item in decisions)):
        blockers.append(_blocker(poi.id, "REVIEW_DECISION_MISSING", "an AUTO_APPROVED review decision must be frozen"))
    return _snapshot(storage, poi, overlay, auto_evidence), blockers


def _source_locale(storage: Storage, poi: POI,
                   provenance: Sequence[FieldProvenance]) -> Tuple[str, bool, Optional[str]]:
    """Choose a source locale for deterministic fallback copy.

    ``und`` and absent language metadata are intentionally treated as an
    English fallback.  The manifest records this choice; the generated LAND
    pack still carries both schema-required locales.
    """
    content_by_id = {item.id: item for item in storage.all(SourceContent)}
    observed: Optional[str] = None
    for item in provenance:
        content = content_by_id.get(item.source_content_id)
        language = content.language if content is not None else None
        if language in LANGUAGES:
            observed = language
            break
    if observed is None:
        for language in LANGUAGES:
            if str(poi.names.get(language, "")).strip():
                observed = language
                break
    if observed is None:
        observed = "en"
    return observed, True, observed


def _auto_overlay(storage: Storage, city: str, pois: Sequence[POI],
                  city_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a conservative, source-field-only overlay without LLM/editorial input."""
    city_name = (city_profile or {}).get("display_name") if isinstance(city_profile, dict) else None
    city_name = str(city_name or city).strip()
    destination_locale = "en"
    destination_summary = AUTO_SUMMARY_EN.format(city=city_name)
    if isinstance(city_profile, dict) and city_profile.get("language") in LANGUAGES:
        destination_locale = city_profile["language"]
        destination_summary = (AUTO_SUMMARY_ZH if destination_locale == "zh-Hans" else AUTO_SUMMARY_EN).format(
            city=city_name)
    destination_copy = {
        "name": city_name, "localName": city_name, "summary": destination_summary,
    }
    source_locales: Dict[str, str] = {}
    places: Dict[str, Dict[str, Dict[str, str]]] = {}
    for poi in sorted(pois, key=lambda item: item.id):
        provenance = _poi_provenance(storage, poi.id)
        locale, _fallback, _source_language = _source_locale(storage, poi, provenance)
        source_locales[poi.id] = locale
        source_name = next((str(value).strip() for value in (
            poi.names.get(locale, ""), *poi.names.values()) if str(value).strip()), "")
        if locale == "zh-Hans":
            reason, caution = AUTO_REASON_ZH, AUTO_CAUTION_ZH
        else:
            reason, caution = AUTO_REASON_EN, AUTO_CAUTION_EN
        copy = {"name": source_name, "localName": source_name,
                "address": str(poi.address).strip(), "reason": reason, "caution": caution}
        # LAND requires both locales.  The repeated copy is deliberately
        # marked in the release manifest as a fallback, never as translation.
        places[poi.id] = {language: dict(copy) for language in LANGUAGES}
    return {
        "city": city,
        "destination": {language: dict(destination_copy) for language in LANGUAGES},
        "places": places,
        "_metadata": {
            "mode": "deterministic_fallback",
            "fallback": True,
            "fallback_locale": destination_locale,
            "source_locales": source_locales,
            "reason": "no_editorial_overlay",
        },
    }


def _source_objects(storage: Storage, poi: POI, provenance: Sequence[FieldProvenance]) -> List[Dict[str, Any]]:
    sources = []
    seen = set()
    content_by_id = {item.id: item for item in storage.all(SourceContent)}
    for item in provenance:
        candidates = [item.source_reference_url]
        if item.source_content_id in content_by_id:
            content = content_by_id[item.source_content_id]
            candidates.extend([content.source_url, content.canonical_url])
        if item.field_name == "website":
            candidates.append(poi.website)
        url = next((value.strip() for value in candidates if _url(value)), None)
        if not url or url in seen:
            continue
        seen.add(url)
        collected = item.collected_at if _iso(item.collected_at) else poi.last_verified_at
        sources.append({"id": stable_id("land-place-source", poi.id, url),
                        "title": "POI provenance",
                        "url": url, "platform": item.source or "verified-source",
                        "author": item.source or "LAND provenance",
                        "publishedAt": None, "collectedAt": collected})
    if not sources:
        # _check_poi has already rejected this case; keep pack construction
        # deterministic if a caller bypasses it.
        raise ValueError("STABLE_HTTPS_SOURCE_MISSING")
    return sources


def _city_center(pois: Sequence[POI], city_profile: Optional[Dict[str, Any]]) -> Dict[str, float]:
    if isinstance(city_profile, dict):
        lat, lon = city_profile.get("latitude"), city_profile.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return {"latitude": lat, "longitude": lon}
    return {"latitude": pois[0].latitude, "longitude": pois[0].longitude}


def _build_pack(city: str, pois: Sequence[POI], overlay: Dict[str, Any],
                city_profile: Optional[Dict[str, Any]],
                provenance_for_pack: Dict[str, List[FieldProvenance]],
                source_objects_for_pack: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    # Package bytes must be stable for identical frozen inputs; using the
    # freshest POI verification timestamp also makes repeated Dashboard
    # prepares idempotent instead of creating a new hash every second.
    now = max(poi.last_verified_at for poi in pois)
    center = _city_center(pois, city_profile)
    destination = []
    for language in LANGUAGES:
        destination.append({
            "id": city,
            "name": overlay["destination"][language]["name"],
            "localName": overlay["destination"][language]["localName"],
            "summary": overlay["destination"][language]["summary"],
            "center": center, "image": None,
            "metadata": {"locale": language, "isDemo": False,
                          "scope": city + " · published places", "updatedAt": now,
                          "verifiedAt": max(poi.last_verified_at for poi in pois),
                          "sources": []},
        })
    places = []
    for poi in sorted(pois, key=lambda item: item.id):
        provenance = provenance_for_pack.get(poi.id, [])
        sources = source_objects_for_pack.get(poi.id, [])
        place_id = stable_id("land-place", city, poi.id)
        for language in LANGUAGES:
            copy = overlay["places"][poi.id][language]
            places.append({
                "id": place_id, "destinationID": city,
                "category": PLACE_CATEGORY_MAP[poi.category],
                "name": copy["name"], "localName": copy["localName"],
                "coordinate": {"latitude": poi.latitude, "longitude": poi.longitude},
                "address": copy["address"], "reason": copy["reason"],
                "caution": copy["caution"], "image": None,
                "metadata": {"locale": language, "isDemo": False,
                              "scope": city + " · published places", "updatedAt": now,
                              "verifiedAt": poi.last_verified_at, "sources": sources},
            })
    # Include the same source references on the destination so it is a valid
    # published ContentPack even when the app requests the city first.
    combined = []
    seen = set()
    for source in [source for value in source_objects_for_pack.values() for source in value]:
        if source["id"] not in seen:
            seen.add(source["id"])
            combined.append(source)
    for item in destination:
        item["metadata"]["sources"] = combined
    return {"schemaVersion": 1, "batchID": "place-" + city, "status": "published",
            "destinations": destination, "guides": [], "places": places}


def _published_base(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read the installed published pack that a new release must preserve.

    A missing file is a valid empty base.  A present but malformed file is not:
    silently dropping another city's installed content would be worse than
    refusing to prepare the next release.
    """
    if not path.is_file():
        return None, None
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("LAND published resource is not valid JSON") from exc
    if (not isinstance(value, dict) or value.get("schemaVersion") != 1 or
            value.get("status") != "published" or
            any(not isinstance(value.get(key), list)
                for key in ("destinations", "guides", "places"))):
        raise ValueError("LAND published resource is not a valid published ContentPack")
    return value, _sha(raw)


def _content_key(value: Dict[str, Any], *, place: bool = False) -> Tuple[str, str]:
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    identity = value.get("id")
    if place:
        identity = "{}:{}".format(value.get("destinationID", ""), identity or "")
    return str(identity or ""), str(metadata.get("locale") or "")


def _merge_sources(first: Any, second: Any) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for item in list(first or []) + list(second or []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            merged[item["id"]] = item
    return [merged[key] for key in sorted(merged)]


def _merge_published_pack(base: Optional[Dict[str, Any]], fragment: Dict[str, Any],
                          city: str) -> Dict[str, Any]:
    """Merge one explicit city fragment into the installed multi-city pack.

    Existing cities, guides, and unselected places remain byte-equivalent at
    the object level.  For an existing city the current destination copy stays
    in place while its source closure is extended with the new place sources;
    this prevents a conservative automatic fallback from overwriting edited
    city copy.  Withdrawal remains an explicit, separate operation.
    """
    if base is None:
        return dict(fragment)
    result = {
        "schemaVersion": 1,
        "batchID": fragment.get("batchID", "place-" + city),
        "status": "published",
        "destinations": [dict(item) for item in base["destinations"]],
        "guides": [dict(item) for item in base["guides"]],
        "places": [dict(item) for item in base["places"]],
    }

    destinations = {_content_key(item): item for item in result["destinations"]}
    for incoming in fragment.get("destinations", []):
        key = _content_key(incoming)
        existing = destinations.get(key)
        if existing is None:
            destinations[key] = dict(incoming)
            continue
        # Preserve already-published editorial text and presentation while
        # extending the provenance needed by newly added places.
        combined = dict(existing)
        old_metadata = existing.get("metadata", {})
        new_metadata = incoming.get("metadata", {})
        metadata = dict(old_metadata)
        metadata["sources"] = _merge_sources(old_metadata.get("sources"),
                                               new_metadata.get("sources"))
        for timestamp in ("updatedAt", "verifiedAt"):
            values = [value for value in (old_metadata.get(timestamp),
                                           new_metadata.get(timestamp))
                      if isinstance(value, str)]
            if values:
                metadata[timestamp] = max(values)
        combined["metadata"] = metadata
        destinations[key] = combined

    places = {_content_key(item, place=True): item for item in result["places"]}
    for incoming in fragment.get("places", []):
        places[_content_key(incoming, place=True)] = dict(incoming)

    result["destinations"] = [destinations[key] for key in sorted(destinations)]
    result["places"] = [places[key] for key in sorted(places)]
    result["guides"] = sorted(result["guides"], key=_content_key)
    return result


def _release_integrity(manifest: Dict[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_integrity"}
    return _sha(body)


def _load_release(release_dir: Path) -> Tuple[Dict[str, Any], bytes]:
    manifest_path, pack_path = release_dir / MANIFEST_FILE, release_dir / PACK_FILE
    if not manifest_path.is_file() or not pack_path.is_file():
        raise ValueError("release manifest or ContentPack is missing")
    manifest = _read_json(manifest_path)
    if manifest.get("manifest_integrity") != _release_integrity(manifest):
        raise ValueError("release manifest integrity check failed")
    raw = pack_path.read_bytes()
    expected = manifest.get("files", {}).get(PACK_FILE, {}).get("sha256")
    if not isinstance(expected, str) or _sha(raw) != expected:
        raise ValueError("ContentPack hash does not match manifest")
    pack = json.loads(raw)
    if pack.get("status") != "published":
        raise ValueError("place ContentPack must have status=published")
    return manifest, raw


def _release_context(storage: Storage, city: str, selected: Sequence[str],
                     publication_overlay: Optional[Dict[str, Any]],
                     city_profile: Optional[Dict[str, Any]]) -> Tuple[
                         Dict[str, Any], List[Dict[str, Any]], Dict[str, Dict[str, Any]],
                         bool, str]:
    """Resolve overlay and approval mode without writing to storage."""
    pois = [storage.get(POI, poi_id) for poi_id in selected]
    evidence_by_id = {
        poi_id: _auto_policy_evidence(storage, poi)
        for poi_id, poi in zip(selected, pois)
    }
    all_auto = bool(pois) and len(pois) == len(selected) and all(
        evidence_by_id[poi_id]["eligible"] for poi_id in selected)
    if publication_overlay is None and all_auto:
        overlay, overlay_blockers = _auto_overlay(
            storage, city, [poi for poi in pois if poi is not None], city_profile), []
    elif publication_overlay is None:
        # Preserve the existing detailed overlay blockers for community,
        # missing or otherwise unsafe sources.
        overlay, overlay_blockers = _normalise_overlay(None, city, selected, city_profile)
    else:
        overlay, overlay_blockers = _normalise_overlay(
            publication_overlay, city, selected, city_profile)
    missing_decision = any(not _poi_decisions(storage, poi)
                           for poi in pois if poi is not None)
    approval_mode = ("deterministic_official"
                     if all_auto and (missing_decision or publication_overlay is None)
                     else "human_reviewed")
    return overlay, overlay_blockers, evidence_by_id, all_auto, approval_mode


def prepare_place_release(storage: Storage, *, city: str, poi_ids: Sequence[str],
                          publication_overlay: Optional[Dict[str, Any]] = None,
                          releases_root: str = ".local/releases",
                          city_profile: Optional[Dict[str, Any]] = None,
                          base_content_path: Optional[str] = None) -> Dict[str, Any]:
    """Prepare a deterministic bilingual Place release for explicit POI IDs.

    An omitted overlay is accepted only when every selected POI satisfies the
    deterministic official-source policy.  The generated copy is deliberately
    factual and is recorded as a fallback in the manifest.
    """
    if not isinstance(city, str) or not _safe_id(city):
        raise ValueError("city must be a stable id")
    if isinstance(poi_ids, str) or not poi_ids:
        raise ValueError("at least one explicit POI id is required")
    selected = list(dict.fromkeys(str(value) for value in poi_ids))
    if len(selected) != len(poi_ids):
        raise ValueError("duplicate POI ids are not allowed")
    overlay, blockers, evidence_by_id, allow_auto, approval_mode = _release_context(
        storage, city, selected, publication_overlay, city_profile)
    overlay_mode = overlay.get("_metadata", {}).get("mode", "human_bilingual")
    valid_snapshots: Dict[str, Dict[str, Any]] = {}
    valid_pois: List[POI] = []
    provenance_for_pack: Dict[str, List[FieldProvenance]] = {}
    source_objects_for_pack: Dict[str, List[Dict[str, Any]]] = {}
    for poi_id in selected:
        poi = storage.get(POI, poi_id)
        snapshot, item_blockers = _check_poi(
            storage, poi, city, overlay, city_profile,
            allow_deterministic_official=allow_auto)
        blockers.extend(item_blockers)
        if snapshot and not item_blockers and poi is not None:
            provenance = _poi_provenance(storage, poi.id)
            valid_snapshots[poi.id] = snapshot
            valid_pois.append(poi)
            provenance_for_pack[poi.id] = provenance
            source_objects_for_pack[poi.id] = _source_objects(storage, poi, provenance)
    if blockers:
        # Never leave a valid subset in a release whose selection or overlay is
        # blocked.  The manifest remains useful for the Dashboard, but verify
        # can only succeed after a fresh, fully unblocked prepare.
        valid_pois = []
        valid_snapshots = {}
        provenance_for_pack.clear()
        source_objects_for_pack.clear()
    # Resolve and freeze the installed pack before calculating the release id.
    # This makes a release specific to the exact multi-city base it extends.
    base_path = (Path(base_content_path).resolve() if base_content_path else
                 Path(__file__).resolve().parents[2].parent / "LAND" / "LAND" / "Resources" / "published-content.json")
    base_pack, base_sha = _published_base(base_path)
    # A blocked selection must never leak a partial POI into a release that a
    # later caller could accidentally verify.  The package remains a complete
    # valid envelope with no places and is permanently blocked in the manifest.
    fragment = _build_pack(city, valid_pois, overlay, city_profile,
                           provenance_for_pack, source_objects_for_pack) if valid_pois else {
        "schemaVersion": 1, "batchID": "place-" + city, "status": "published",
        "destinations": [], "guides": [], "places": []}
    pack = _merge_published_pack(base_pack, fragment, city) if valid_pois else fragment
    input_payload = {"city": city, "poi_ids": selected, "snapshots": valid_snapshots,
                     "overlay": overlay, "city_profile": city_profile or {},
                     "base_sha256": base_sha}
    input_hash = _sha(input_payload)
    release_id = RELEASE_PREFIX + city + "-" + input_hash[:16]
    pack["batchID"] = release_id
    pack_raw = (_canonical(pack) + "\n").encode("utf-8")
    root = Path(releases_root).resolve()
    release_dir = root / release_id
    existing = release_dir / MANIFEST_FILE
    if existing.is_file():
        old = _read_json(existing)
        if old.get("input_hash") == input_hash and old.get("files", {}).get(PACK_FILE, {}).get("sha256") == _sha(pack_raw):
            return {"release_id": release_id, "release_dir": str(release_dir),
                    "status": old.get("status", "PREPARED"), "idempotent": True,
                    "blockers": old.get("blockers", blockers),
                    "selected": selected, "pack_sha256": _sha(pack_raw),
                    "approval_mode": old.get("approval_mode", approval_mode),
                    "overlay_mode": old.get("overlay_mode", overlay_mode),
                    "auto_approval_policy_version": old.get("auto_approval", {}).get(
                        "policy_version", AUTO_APPROVAL_POLICY_VERSION)}
    root.mkdir(parents=True, exist_ok=True)
    release_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "release_id": release_id, "kind": "land-place",
        "city": city, "poi_ids": selected, "status": "PREPARED", "blockers": blockers,
        "created_at": utc_now(), "input_hash": input_hash,
        "frozen_inputs": valid_snapshots, "publication_overlay": overlay,
        "approval_mode": approval_mode,
        "overlay_mode": overlay_mode,
        "overlay_metadata": overlay.get("_metadata", {}),
        "auto_approval": {
            "policy_version": AUTO_APPROVAL_POLICY_VERSION,
            "threshold_evidence": evidence_by_id,
        },
        "city_profile": city_profile or {},
        "base_resource": {"path": str(base_path), "sha256": base_sha,
                          "merge_mode": "preserve_existing_v1"},
        "files": {PACK_FILE: {"sha256": _sha(pack_raw), "bytes": len(pack_raw)}},
        "verification": None,
    }
    manifest["manifest_integrity"] = _release_integrity(manifest)
    _write_json(release_dir / PACK_FILE, pack)
    _write_json(release_dir / MANIFEST_FILE, manifest)
    return {"release_id": release_id, "release_dir": str(release_dir), "status": "PREPARED",
            "idempotent": False, "blockers": blockers, "selected": selected,
            "compiled": len(valid_pois), "pack_sha256": _sha(pack_raw),
            "approval_mode": approval_mode, "overlay_mode": overlay_mode,
            "auto_approval_policy_version": AUTO_APPROVAL_POLICY_VERSION}


def preflight_place_release(storage: Storage, *, city: str, poi_ids: Sequence[str],
                            publication_overlay: Optional[Dict[str, Any]] = None,
                            releases_root: str = ".local/releases",
                            city_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read-only readiness check for an explicitly selected set of POIs.

    This intentionally shares the same city and POI gates as ``prepare`` but
    never creates a release directory, writes a manifest, or mutates storage.
    The overlay is optional so the Dashboard/CLI can show what still needs
    human editorial input before a release is prepared.
    """
    if not isinstance(city, str) or not _safe_id(city):
        raise ValueError("city must be a stable id")
    if isinstance(poi_ids, str) or not poi_ids:
        raise ValueError("at least one explicit POI id is required")
    selected = list(dict.fromkeys(str(value) for value in poi_ids))
    if len(selected) != len(poi_ids):
        raise ValueError("duplicate POI ids are not allowed")
    overlay, overlay_blockers, evidence_by_id, allow_auto, approval_mode = _release_context(
        storage, city, selected, publication_overlay, city_profile)
    overlay_mode = overlay.get("_metadata", {}).get("mode", "human_bilingual")
    items: List[Dict[str, Any]] = []
    item_blockers: List[Dict[str, Any]] = []
    for poi_id in selected:
        poi = storage.get(POI, poi_id)
        _snapshot_value, blockers = _check_poi(
            storage, poi, city, overlay, city_profile,
            allow_deterministic_official=allow_auto)
        blockers = list(blockers)
        item_blockers.extend(blockers)
        combined: List[Dict[str, Any]] = []
        for blocker in overlay_blockers + blockers:
            scoped = dict(blocker)
            original_id = scoped.get("poi_id")
            scoped["poi_id"] = poi_id
            if original_id != poi_id:
                scoped["scope"] = "release"
            combined.append(scoped)
        items.append({"poi_id": poi_id, "ready": not combined,
                      "status": "READY" if not combined else "BLOCKED",
                      "approval_mode": approval_mode,
                      "overlay_mode": overlay_mode,
                      "approval_evidence": evidence_by_id.get(poi_id),
                      "blockers": combined})
    return {
        "status": "READY" if all(item["ready"] for item in items) else "BLOCKED",
        "city": city,
        "poi_ids": selected,
        "approval_mode": approval_mode,
        "overlay_mode": overlay_mode,
        "auto_approval": {
            "policy_version": AUTO_APPROVAL_POLICY_VERSION,
            "threshold_evidence": evidence_by_id,
        },
        "overlay_metadata": overlay.get("_metadata", {}),
        "items": items,
        "summary": {
            "total": len(items),
            "ready": sum(1 for item in items if item["ready"]),
            "blocked": sum(1 for item in items if not item["ready"]),
            "blocker_count": len(overlay_blockers) + len(item_blockers),
        },
        "blockers": overlay_blockers + item_blockers,
        "read_only": True,
        "release_dir_created": False,
        "releases_root": str(Path(releases_root).resolve()),
    }


def diff_place_release(release_dir: str, *, storage: Optional[Storage] = None) -> Dict[str, Any]:
    release_dir = Path(release_dir).resolve()
    manifest, raw = _load_release(release_dir)
    result = {"release_id": manifest["release_id"], "manifest_status": manifest.get("status"),
              "approval_mode": manifest.get("approval_mode", "human_reviewed"),
              "overlay_mode": manifest.get("overlay_mode", "human_bilingual"),
              "blockers": manifest.get("blockers", []),
              "content_pack": {"sha256": _sha(raw), "matches_manifest": True},
              "input": {"status": "NOT_CHECKED", "matches_manifest": None},
              "status": "CLEAN" if not manifest.get("blockers") else "BLOCKED"}
    if storage is not None:
        city, overlay = manifest["city"], manifest.get("publication_overlay", {})
        current = {}
        current_blockers = []
        for poi_id in manifest.get("poi_ids", []):
            snapshot, blockers = _check_poi(
                storage, storage.get(POI, poi_id), city, overlay,
                manifest.get("city_profile", {}),
                allow_deterministic_official=(manifest.get("approval_mode") ==
                                              "deterministic_official"))
            current_blockers.extend(blockers)
            if snapshot and not blockers:
                current[poi_id] = snapshot
        base_info = manifest.get("base_resource", {})
        base_path = Path(base_info.get("path", ""))
        _base_pack, current_base_sha = _published_base(base_path)
        payload = {"city": city, "poi_ids": manifest.get("poi_ids", []), "snapshots": current,
                   "overlay": overlay, "city_profile": manifest.get("city_profile", {}),
                   "base_sha256": current_base_sha}
        current_hash = _sha(payload)
        result["input"] = {"status": "DRIFTED" if current_hash != manifest.get("input_hash") else "CLEAN",
                            "matches_manifest": current_hash == manifest.get("input_hash"),
                            "sha256": current_hash, "blockers": current_blockers}
        if current_hash != manifest.get("input_hash") or current_blockers:
            result["status"] = "DRIFTED"
    return result


def _rebuild_pack_from_storage(storage: Storage, manifest: Dict[str, Any]) -> Tuple[Optional[bytes], List[Dict[str, Any]]]:
    """Recreate bytes from the frozen selection for tamper detection."""
    city = manifest["city"]
    overlay = manifest.get("publication_overlay", {})
    pois: List[POI] = []
    provenance_for_pack: Dict[str, List[FieldProvenance]] = {}
    source_objects_for_pack: Dict[str, List[Dict[str, Any]]] = {}
    blockers: List[Dict[str, Any]] = []
    for poi_id in manifest.get("poi_ids", []):
        poi = storage.get(POI, poi_id)
        snapshot, item_blockers = _check_poi(
            storage, poi, city, overlay, manifest.get("city_profile", {}),
            allow_deterministic_official=(manifest.get("approval_mode") ==
                                          "deterministic_official"))
        blockers.extend(item_blockers)
        if snapshot and not item_blockers and poi is not None:
            pois.append(poi)
            provenance = _poi_provenance(storage, poi.id)
            provenance_for_pack[poi.id] = provenance
            source_objects_for_pack[poi.id] = _source_objects(storage, poi, provenance)
    if blockers:
        return None, blockers
    fragment = _build_pack(city, pois, overlay, manifest.get("city_profile", {}),
                           provenance_for_pack, source_objects_for_pack)
    base_info = manifest.get("base_resource", {})
    base_path = Path(base_info.get("path", ""))
    base_pack, base_sha = _published_base(base_path)
    if base_sha != base_info.get("sha256"):
        return None, [_blocker("*", "BASE_RESOURCE_DRIFT",
                               "LAND published resource changed after prepare")]
    pack = _merge_published_pack(base_pack, fragment, city)
    pack["batchID"] = manifest["release_id"]
    return (_canonical(pack) + "\n").encode("utf-8"), []


def verify_place_release(release_dir: str, *, storage: Optional[Storage] = None,
                         reviewer: Optional[str] = None, reason: Optional[str] = None) -> Dict[str, Any]:
    release_dir = Path(release_dir).resolve()
    try:
        manifest, _raw = _load_release(release_dir)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "release_dir": str(release_dir)}
    deterministic = manifest.get("approval_mode") == "deterministic_official"
    if deterministic:
        reviewer = (reviewer or "deterministic-official-policy").strip()
        reason = (reason or "mechanical verification of deterministic official-source policy").strip()
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError("reviewer and reason are required for human-reviewed releases")
    errors: List[str] = []
    if manifest.get("status") not in ("PREPARED", "VERIFIED"):
        errors.append("INVALID_RELEASE_STATUS")
    if manifest.get("blockers"):
        errors.append("UNRESOLVED_BLOCKERS")
    if storage is not None:
        diff = diff_place_release(str(release_dir), storage=storage)
        if diff["input"].get("matches_manifest") is not True:
            errors.append("INPUT_DRIFT")
        if diff["input"].get("blockers"):
            errors.append("CURRENT_POI_BLOCKER")
        if not errors:
            expected_pack, pack_blockers = _rebuild_pack_from_storage(storage, manifest)
            if pack_blockers:
                errors.append("CURRENT_POI_BLOCKER")
            elif expected_pack is None or _sha(expected_pack) != _sha(_raw):
                errors.append("CONTENT_PACK_DRIFT")
    if errors:
        return {"ok": False, "errors": sorted(set(errors)), "release_id": manifest["release_id"]}
    manifest["status"] = "VERIFIED"
    manifest["verification"] = {"reviewer": reviewer.strip(), "reason": reason.strip(), "verified_at": utc_now()}
    manifest["manifest_integrity"] = _release_integrity(manifest)
    _write_json(release_dir / MANIFEST_FILE, manifest)
    return {"ok": True, "release_id": manifest["release_id"], "status": "VERIFIED",
            "reviewer": reviewer.strip()}


def _run_place_checks(land_root: Path, package_path: Path, release_id: str,
                      runner: Optional[Callable] = None, run_app_checks: bool = False) -> Tuple[bool, List[Dict[str, Any]]]:
    safe = "".join(char for char in release_id if char.isalnum() or char in "-_")[:80]
    environment = dict(os.environ)
    xcode_developer = Path("/Applications/Xcode.app/Contents/Developer")
    if xcode_developer.is_dir():
        environment["DEVELOPER_DIR"] = str(xcode_developer)
    environment.update({
        "CLANG_MODULE_CACHE_PATH": "/tmp/LAND-clang-module-cache",
        "SWIFTPM_MODULECACHE_OVERRIDE": "/tmp/LAND-swift-module-cache",
    })
    commands = [[sys.executable, "tools/validate_content.py", str(package_path), "--publish"]]
    # Keep the Swift/Xcode hook explicit and opt-in.  A local publication
    # install must always validate the ContentPack, while app builds remain a
    # separate, potentially long-running acceptance gate.
    if run_app_checks:
        commands.extend([
            ["swift", "test", "--disable-sandbox", "--scratch-path",
             "/tmp/LAND-{}-place-tests".format(safe), "-q"],
            ["xcodebuild", "-quiet", "-project", "LAND.xcodeproj", "-scheme", "LAND",
             "-configuration", "Debug", "-sdk", "iphonesimulator",
             "-destination", "generic/platform=iOS Simulator", "-derivedDataPath",
             "/tmp/LAND-{}-place-xcode".format(safe), "CODE_SIGNING_ALLOWED=NO", "build"],
        ])
    execute = runner or subprocess.run
    records = []
    for command in commands:
        try:
            completed = execute(command, cwd=str(land_root), env=environment,
                                capture_output=True, text=True, timeout=1800)
            code = int(completed.returncode)
            output = ((completed.stdout or "") + (completed.stderr or ""))[-4000:]
        except Exception as exc:
            code, output = 1, type(exc).__name__ + ": " + str(exc)[:500]
        records.append({"command": command[0], "returncode": code, "output_tail": output})
        if code:
            return False, records
    return True, records


def install_place_release(release_dir: str, land_root: str, *, runner: Optional[Callable] = None,
                          run_app_checks: bool = False) -> Dict[str, Any]:
    manifest, raw = _load_release(Path(release_dir).resolve())
    if manifest.get("status") != "VERIFIED" or manifest.get("blockers"):
        raise ValueError("only a verified unblocked place release can be installed")
    land_root = Path(land_root).resolve()
    target = land_root / "LAND" / "Resources" / "published-content.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_exists = target.is_file()
    previous = target.read_bytes() if previous_exists else b""
    base_sha = manifest.get("base_resource", {}).get("sha256")
    if previous and base_sha and _sha(previous) != base_sha and _sha(previous) != _sha(raw):
        raise ValueError("LAND published resource differs from the release base; prepare a new release")
    if previous == raw:
        return {"release_id": manifest["release_id"], "status": "INSTALLED", "idempotent": True,
                "target": str(target), "checks": []}
    state_root = land_root / ".local" / "land-data-publish"
    backup_dir = state_root / "place-backups" / manifest["release_id"]
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / target.name
    if not backup_path.exists():
        backup_path.write_bytes(previous)
    temporary = target.with_name(target.name + ".tmp-" + secrets.token_hex(6))
    temporary.write_bytes(raw)
    os.replace(str(temporary), str(target))
    ok, checks = _run_place_checks(land_root, target, manifest["release_id"], runner, run_app_checks)
    record = {"release_id": manifest["release_id"], "target": str(target),
              "previous_sha256": _sha(previous), "installed_sha256": _sha(raw),
              "previous_exists": previous_exists, "checks": checks, "at": utc_now()}
    if not ok:
        if previous_exists:
            temporary = target.with_name(target.name + ".rollback-" + secrets.token_hex(6))
            temporary.write_bytes(previous)
            os.replace(str(temporary), str(target))
        elif target.exists():
            target.unlink()
        record["status"] = "INSTALL_FAILED_ROLLED_BACK"
        _write_jsonl(state_root / "history.jsonl", record)
        return {"release_id": manifest["release_id"], "status": record["status"],
                "idempotent": False, "target": str(target), "checks": checks}
    record["status"] = "INSTALLED"
    _write_json(backup_dir / "install.json", record)
    _write_jsonl(state_root / "history.jsonl", record)
    return {"release_id": manifest["release_id"], "status": "INSTALLED", "idempotent": False,
            "target": str(target), "checks": checks}


def _write_jsonl(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(_canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def rollback_place_release(release_id: str, land_root: str, *, reason: str,
                           runner: Optional[Callable] = None,
                           run_app_checks: bool = False) -> Dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("rollback reason is required")
    land_root = Path(land_root).resolve()
    state_root = land_root / ".local" / "land-data-publish"
    backup_dir = state_root / "place-backups" / release_id
    record_path, backup_path = backup_dir / "install.json", backup_dir / "published-content.json"
    target = land_root / "LAND" / "Resources" / "published-content.json"
    if not record_path.is_file() or not backup_path.is_file() or not target.is_file():
        raise ValueError("verified place installation backup was not found")
    record = _read_json(record_path)
    current = target.read_bytes()
    if _sha(current) != record.get("installed_sha256"):
        raise ValueError("LAND published resource changed after this install; refusing unsafe rollback")
    previous = backup_path.read_bytes()
    if _sha(previous) != record.get("previous_sha256"):
        raise ValueError("rollback backup hash mismatch")
    previous_exists = record.get("previous_exists", True)
    if previous_exists:
        temporary = target.with_name(target.name + ".rollback-" + secrets.token_hex(6))
        temporary.write_bytes(previous)
        os.replace(str(temporary), str(target))
    elif target.exists():
        target.unlink()
    ok, checks = _run_place_checks(land_root, target, release_id + "-rollback", runner, run_app_checks)
    entry = {"release_id": release_id, "status": "ROLLED_BACK" if ok else "ROLLBACK_FAILED_RESTORED",
             "reason": reason.strip(), "at": utc_now(), "checks": checks}
    if not ok:
        temporary = target.with_name(target.name + ".rollback-failed-" + secrets.token_hex(6))
        temporary.write_bytes(current)
        os.replace(str(temporary), str(target))
    _write_jsonl(state_root / "history.jsonl", entry)
    return entry


# Short aliases used by callers that already refer to generic release verbs.
prepare_release = prepare_place_release
release_diff = diff_place_release
verify_release = verify_place_release
install_release = install_place_release
rollback_release = rollback_place_release
