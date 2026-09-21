"""JSON configuration; source and city definitions are data, not code paths."""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from .adapters import AdapterCapability, MockSourceAdapter
from .auth import MockSessionStore
from .policy import SourcePolicy


@dataclass(frozen=True)
class SourceConfig:
    name: str
    fixture: str
    capability: AdapterCapability
    policy: SourcePolicy
    allow_coordinate_verification: bool = False


@dataclass(frozen=True)
class Config:
    root: Path
    cities: Dict[str, dict]
    sources: Dict[str, SourceConfig]
    visibility_threshold: float

    def adapter(self, name: str, sessions: MockSessionStore) -> MockSourceAdapter:
        source = self.sources[name]
        return MockSourceAdapter(name, str(self.root / source.fixture), source.capability, sessions)


def load_config(root: str) -> Config:
    root_path = Path(root).resolve()
    cities = json.loads((root_path / "config" / "cities.json").read_text(encoding="utf-8"))
    source_data = json.loads((root_path / "config" / "sources.json").read_text(encoding="utf-8"))
    sources = {}
    for name, value in source_data["sources"].items():
        policy = value["policy"]
        sources[name] = SourceConfig(
            name=name, fixture=value["fixture"],
            capability=AdapterCapability(**value["capability"]),
            allow_coordinate_verification=value.get("allow_coordinate_verification") is True,
            policy=SourcePolicy(
                raw_fields=tuple(policy["raw_fields"]),
                normalized_fields=tuple(policy["normalized_fields"]),
                temporary_fields=tuple(policy.get("temporary_fields", [])),
                export_fields=tuple(policy.get("export_fields", [])),
                retention_days=int(policy.get("retention_days", 30)),
                retain_source_url=policy.get("retain_source_url", True) is True))
    threshold = float(source_data.get("visibility_threshold", 0.8))
    if not 0 <= threshold <= 1:
        raise ValueError("invalid visibility threshold")
    return Config(root_path, cities, sources, threshold)
