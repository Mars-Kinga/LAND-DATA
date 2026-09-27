"""The one-command, zero-LLM local collector entry point."""
from pathlib import Path
from typing import Iterable, Optional

from .multi_source import MultiSourceRunner, load_specs
from .pipeline import Pipeline
from .source_discovery import SourceDiscoveryRunner, load_discovery_config


def select_auto_specs(specs, city):
    """Select only explicitly enabled sources assigned to this auto city."""
    return [spec for spec in specs if spec.city == city and spec.auto_collect]


def run_auto_collect(storage, config, sessions, *, root: Path, city: str,
                     discovery_config_path: str, source_config_path: str,
                     sources: Optional[Iterable[str]] = None,
                     discovery_only: bool = False,
                     collection_only: bool = False):
    """Run bounded discovery followed by explicitly configured collection.

    Discovery never mutates the collection configuration.  Unknown dataset
    candidates remain in the discovery table and therefore cannot silently
    become POIs or Events.
    """
    result = {"status": "SUCCEEDED", "llm_calls": 0}
    if not collection_only:
        try:
            discovery_config = load_discovery_config(discovery_config_path)
            result["discovery"] = SourceDiscoveryRunner(storage, discovery_config).run()
        except Exception as exc:
            result["discovery"] = {"status": "FAILED", "next_run_status": "RETRY_REQUIRED",
                                    "failure_reason": "{}: {}".format(type(exc).__name__, str(exc)),
                                    "llm_calls": 0}
    else:
        result["discovery"] = {"status": "NOT_REQUESTED", "llm_calls": 0}

    if not discovery_only:
        selected = list(sources or ())
        specs = load_specs(source_config_path, root, selected or None)
        if not selected:
            specs = select_auto_specs(specs, city)
        if not specs:
            result["collection"] = {"status": "NOT_REQUESTED", "sources": [],
                                     "reason": "no_enabled_sources_for_city"}
        else:
            def make_adapter(spec):
                scope = spec.scope
                categories = list(spec.categories) if spec.categories else None
                if spec.borough:
                    categories = [spec.borough]
                return config.adapter(
                    spec.name, sessions, scope, categories, spec.limit, spec.timeout,
                    storage=storage, collection_key=spec.collection_key,
                    max_pages=spec.max_pages, initial_updated_at=spec.initial_updated_at,
                    overlap_seconds=spec.overlap_seconds,
                    safety_rescan_days=spec.safety_rescan_days, endpoints=spec.endpoints,
                    max_retries=spec.max_retries)

            result["collection"] = MultiSourceRunner(
                Pipeline(storage, config), storage, specs, make_adapter).run()
    else:
        result["collection"] = {"status": "NOT_REQUESTED", "sources": []}

    stage_statuses = {stage.get("status") for key, stage in result.items()
                      if key in ("discovery", "collection") and isinstance(stage, dict)}
    if "FAILED" in stage_statuses or "PARTIAL_FAILURE" in stage_statuses:
        result["status"] = "PARTIAL_FAILURE"
    elif "SUCCEEDED_WITH_REVIEW" in stage_statuses:
        result["status"] = "SUCCEEDED_WITH_REVIEW"
    return result
