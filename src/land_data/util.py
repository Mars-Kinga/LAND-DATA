import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_id(*parts: object) -> str:
    value = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def normalized_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def normalize_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid source URL")
    host = parsed.hostname.lower()
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or
                     (parsed.scheme.lower() == "https" and port == 443)):
        host = "{}:{}".format(host, port)
    query = urlencode([(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                       if not key.lower().startswith("utm_")])
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", query, ""))


def safe_source_url(value: str) -> str:
    """Drop credentials and obvious auth query parameters before any persistence."""
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid source URL")
    host = parsed.hostname
    if parsed.port:
        host += ":{}".format(parsed.port)
    query = urlencode([(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                       if not any(word in key.lower() for word in
                                  ("token", "secret", "session", "cookie", "auth", "password"))])
    return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
