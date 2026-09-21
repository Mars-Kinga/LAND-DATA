"""Per-source retention and field allowlists; mock rules are deliberately small."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

SENSITIVE_KEYS = ("password", "passwd", "cookie", "token", "secret", "authorization", "session")


def remove_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: remove_secrets(item) for key, item in value.items()
                if not any(word in key.lower() for word in SENSITIVE_KEYS)}
    if isinstance(value, list):
        return [remove_secrets(item) for item in value]
    return value


@dataclass(frozen=True)
class SourcePolicy:
    raw_fields: Tuple[str, ...]
    normalized_fields: Tuple[str, ...]
    temporary_fields: Tuple[str, ...] = ()
    export_fields: Tuple[str, ...] = ()
    retention_days: int = 30
    retain_source_url: bool = True

    def __post_init__(self) -> None:
        if self.retention_days < 0:
            raise ValueError("retention_days must be nonnegative")
        if not set(self.normalized_fields).issubset(self.raw_fields):
            raise ValueError("normalized fields must be retained in raw records")

    def retained_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return remove_secrets({key: payload[key] for key in self.raw_fields if key in payload})

    def temporary_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return remove_secrets({key: payload[key] for key in self.temporary_fields if key in payload})

    def expiry(self, collected_at: str) -> str:
        when = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        return (when + timedelta(days=self.retention_days)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
