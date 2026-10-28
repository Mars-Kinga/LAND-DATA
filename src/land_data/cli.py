"""CLI entry point; business operations live in Pipeline and processing modules."""
import argparse
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .auth import AuthRequired, MockSessionStore
from .assisted_review import codex_assisted_review
from .city_setup import ensure_city
from .config import load_config
from .enrichment import EnrichmentPlan
from .export import write_dataset
from .geoscope import load_scope
from .llm_extraction import LLMExtractionService, qwen_from_env
from .knowledge_processing import (
    knowledge_drafts,
    knowledge_review_history,
    process_knowledge_candidates,
    qwen_knowledge_from_env,
    review_knowledge_draft,
)
from .knowledge_bridge import prepare_release, release_diff, verify_release
from .release_install import install_release, rollback_release
from .place_release_bridge import (diff_place_release, install_place_release,
                                    prepare_place_release, preflight_place_release,
                                    resolve_city_id, rollback_place_release,
                                    verify_place_release)
from .dashboard import serve_dashboard
from .classifier_evaluation import evaluate_jsonl
from .land_raw import process_land_raw
from .local_classifier import current_classifications
from .models import ContentClassification, SourceContent
from .pipeline import Pipeline
from .multi_source import MultiSourceRunner, load_specs
from .auto_collect import run_auto_collect
from .official_directory import DirectoryReference
from .review import ReviewService
from .sqlite_storage import SQLiteStorage
from .source_discovery import (SourceDiscoveryRunner, candidate_dicts,
                                load_discovery_config, run_dicts)
from .social_connectors import (BrowserVisibleInputProvider, PublicFeedInputProvider,
                                SocialConnectorAdapter)
from .threads import ThreadsAdapter
from .xhs_browser import (ChromeRemoteBrowser, XHSCaptureAdapter, collect_xhs,
                          collection_runs, collection_status)
from .xhs_processing import knowledge_candidates, process_xhs, processing_status


def _xhs_query_plan(root, query_file, explicit_queries, selected_groups,
                    city, posts, scrolls, pause_seconds):
    path = (Path(root) / query_file).resolve()
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    grouped = data.get("query_groups", {})
    if explicit_queries and selected_groups:
        raise ValueError("use either --query or --group, not both")
    query_groups = {}
    if explicit_queries:
        queries = explicit_queries
        query_groups = {query: "custom" for query in queries}
    elif grouped:
        if not isinstance(grouped, dict) or any(
                not isinstance(name, str) or not isinstance(values, list)
                for name, values in grouped.items()):
            raise ValueError("xhs query_groups must map group names to query lists")
        wanted = selected_groups or list(grouped)
        unknown = sorted(set(wanted) - set(grouped))
        if unknown:
            raise ValueError("unknown xhs query group: {}".format(", ".join(unknown)))
        queries = []
        for group in wanted:
            for query in grouped[group]:
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("xhs query group contains an invalid query")
                if query not in query_groups:
                    queries.append(query)
                    query_groups[query] = group
    else:
        queries = data.get("queries", [])
        query_groups = {query: "legacy" for query in queries}
    resolved_city = city or data.get("city")
    limits = data.get("limits", {})
    if not resolved_city:
        raise ValueError("xhs city is required")
    return {
        "city": resolved_city,
        "queries": queries,
        "query_groups": query_groups,
        "posts": posts if posts is not None else int(limits.get("posts_per_query", 20)),
        "scrolls": scrolls if scrolls is not None else int(limits.get("scrolls_per_query", 3)),
        "pause_seconds": (pause_seconds if pause_seconds is not None
                          else float(limits.get("pause_seconds", 1.5))),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="land-data")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--db", help="SQLite file; default <root>/.local/land-data.db")
    parser.add_argument("--session-file", help="Mock session state; default <root>/.local/mock-sessions.json")
    commands = parser.add_subparsers(dest="command", required=True)
    land_raw = commands.add_parser("process-land-raw")
    land_raw.add_argument("--raw-dir", required=True)
    land_raw.add_argument("--llm-extract", action="store_true")
    land_raw.add_argument("--limit", type=int)
    land_raw.add_argument("--retry-errors", action="store_true")
    land_raw.add_argument("--allow-historical", action="store_true")
    multi = commands.add_parser("multi-pipeline",
                                help="run all enabled NYC Open Data and OSM source configurations")
    multi.add_argument("--source-config", default="config/multi-source.json")
    multi.add_argument("--only-source", action="append", help="run one configured source; repeatable")
    discover = commands.add_parser("discover-sources",
                                   help="bounded zero-LLM discovery of official structured datasets")
    discover.add_argument("--discovery-config", default="config/source-discovery.json")
    discover.add_argument("--list-candidates", action="store_true")
    discover.add_argument("--list-runs", action="store_true")
    discover.add_argument("--limit", type=int, default=20)
    auto = commands.add_parser("auto-collect",
                               help="run source discovery and configured collection without an LLM")
    auto.add_argument("--city", default="new-york")
    auto.add_argument("--discovery-config", default="config/source-discovery.json")
    auto.add_argument("--source-config", default="config/multi-source.json")
    auto.add_argument("--source", action="append", help="explicit configured source; repeatable")
    auto.add_argument("--discovery-only", action="store_true")
    auto.add_argument("--collection-only", action="store_true")
    social = commands.add_parser(
        "social", help="list community platforms or import visible/public social JSON without an LLM")
    social_commands = social.add_subparsers(dest="social_command", required=True)
    social_commands.add_parser("list", help="show available community platform entrypoints")
    social_import = social_commands.add_parser(
        "import", help="import authorized visible browser content or a documented public feed")
    social_import.add_argument("--provider", required=True,
                               help="lowercase platform slug, for example reddit or instagram")
    social_import.add_argument("--city", required=True)
    social_import.add_argument("--input", required=True, help="JSON file with a records array")
    social_import.add_argument("--mode", choices=("browser", "public"), default="browser")
    social_import.add_argument("--page-size", type=int, default=25)
    classifier = commands.add_parser(
        "classifier", help="inspect zero-LLM content classifications")
    classifier_commands = classifier.add_subparsers(dest="classifier_command", required=True)
    classifier_status = classifier_commands.add_parser(
        "status", help="show classifier counts and recent explainable decisions")
    classifier_status.add_argument("--city")
    classifier_status.add_argument("--source")
    classifier_status.add_argument("--label")
    classifier_status.add_argument("--model-recommended", action="store_true")
    classifier_status.add_argument("--limit", type=int, default=50)
    classifier_evaluate = classifier_commands.add_parser(
        "evaluate", help="evaluate the current classifier against a frozen JSONL set")
    classifier_evaluate.add_argument("--dataset", required=True,
                                     help="frozen JSONL file with gold/expected labels")
    classifier_evaluate.add_argument(
        "--expected-fingerprint",
        help="optional sha256 fingerprint; fail if the frozen file changed")
    location_verify = commands.add_parser(
        "location-verify", help="run the isolated, read-only location verification harness")
    location_verify.add_argument("--case", type=Path, required=True)
    location_verify.add_argument("--tool-manifest", type=Path, required=True)
    location_verify.add_argument("--journal", type=Path, required=True)
    location_verify.add_argument("--policy", choices=("fixed", "qwen"), default="fixed")
    location_verify.add_argument("--output", type=Path)
    location_verify.add_argument("--handoff-output", type=Path)
    location_queue = commands.add_parser(
        "location-verify-queue", help="list human handoffs in a verification journal")
    location_queue.add_argument("--journal", type=Path, required=True)
    location_queue.add_argument("--with-business-candidates", action="store_true",
                                help="read-only link to current POICandidate review targets")
    location_prepare = commands.add_parser(
        "location-verify-prepare", help="prepare one frozen LAND-DATA dossier for read-only replay")
    location_prepare.add_argument("--dossier", type=Path, required=True)
    location_prepare.add_argument("--expected-dossier-sha256", required=True)
    location_prepare.add_argument("--case-id", required=True)
    location_prepare.add_argument("--output-dir", type=Path, required=True)
    location_prepare.add_argument("--as-of", help="required for daily dossiers without an as-of timestamp")
    location_prepare_db = commands.add_parser(
        "location-verify-prepare-db",
        help="freeze one existing business candidate into a read-only verification case")
    location_prepare_db.add_argument("--candidate-id", required=True)
    location_prepare_db.add_argument("--output-dir", type=Path, required=True)
    location_prepare_db.add_argument("--as-of", required=True,
                                     help="timezone-aware snapshot cutoff, for example 2026-10-28T00:00:00Z")
    location_hold = commands.add_parser(
        "location-verify-review-hold",
        help="preview or apply a business review hold for a verified conflict")
    location_hold.add_argument("--case", type=Path, required=True)
    location_hold.add_argument("--tool-manifest", type=Path, required=True)
    location_hold.add_argument("--journal", type=Path, required=True)
    location_hold.add_argument("--apply", action="store_true",
                               help="write NEEDS_REVIEW and append-only decisions; default is preview")
    xhs = commands.add_parser("xhs", help="collect visible Xiaohongshu posts through your logged-in Chrome")
    xhs_commands = xhs.add_subparsers(dest="xhs_command", required=True)
    for name in ("login", "status"):
        command = xhs_commands.add_parser(name)
        command.add_argument("--chrome-path", default=None)
        command.add_argument("--port", type=int, default=9222)
    for name in ("collect", "run"):
        command = xhs_commands.add_parser(name)
        command.add_argument("--city")
        command.add_argument("--city-name",
                             help="name used to create and search a previously unknown city")
        command.add_argument("--country",
                             help="ISO two-letter country code required for a new city")
        command.add_argument("--query", action="append")
        command.add_argument("--group", action="append",
                             help="run one query group such as culture; repeatable")
        command.add_argument("--query-file", default="config/xiaohongshu-queries.json")
        command.add_argument("--posts", type=int)
        command.add_argument("--scrolls", type=int)
        command.add_argument("--pause-seconds", type=float)
        command.add_argument("--refresh", action="store_true")
        command.add_argument("--chrome-path", default=None)
        command.add_argument("--port", type=int, default=9222)
        if name == "run":
            command.add_argument("--llm", choices=("never", "auto"), default="never")
            command.add_argument("--llm-limit", type=int)
    process = xhs_commands.add_parser("process")
    process.add_argument("--city", default="new-york")
    process.add_argument("--llm", choices=("never", "auto"), default="never")
    process.add_argument("--limit", type=int)
    process.add_argument("--retry-errors", action="store_true")
    runs = xhs_commands.add_parser("runs")
    runs.add_argument("--limit", type=int, default=20)
    knowledge = xhs_commands.add_parser("knowledge")
    knowledge.add_argument("--city")
    knowledge.add_argument("--scenario")
    knowledge.add_argument("--limit", type=int, default=50)
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command")
    candidates = knowledge_commands.add_parser("candidates")
    candidates.add_argument("--city")
    candidates.add_argument("--scenario")
    candidates.add_argument("--limit", type=int, default=50)
    extract = knowledge_commands.add_parser("extract")
    extract.add_argument("--id", action="append", required=True,
                         help="knowledge candidate id; repeatable")
    extract.add_argument("--llm", choices=("never", "auto"), default="never")
    extract.add_argument("--limit", type=int, required=True,
                         help="hard maximum number of selected candidates")
    extract.add_argument("--retry-errors", action="store_true")
    drafts = knowledge_commands.add_parser("drafts")
    drafts.add_argument("--candidate-id")
    drafts.add_argument("--status")
    drafts.add_argument("--limit", type=int, default=50)
    history = knowledge_commands.add_parser("history")
    history.add_argument("id", help="knowledge draft id")
    approve = knowledge_commands.add_parser("approve")
    approve.add_argument("id", help="knowledge draft id")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--reference-url")
    approve.add_argument("--reference-kind", choices=("official", "operator"))
    approve.add_argument("--reference-title")
    approve.add_argument("--verified-at")
    approve.add_argument("--valid-until")
    reject = knowledge_commands.add_parser("reject")
    reject.add_argument("id", help="knowledge draft id")
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--reason", required=True)
    publish = commands.add_parser("publish", help="prepare, verify and install LAND QAU releases")
    publish_commands = publish.add_subparsers(dest="publish_command", required=True)
    publish_prepare = publish_commands.add_parser("prepare")
    publish_prepare.add_argument("--city", required=True)
    publish_prepare.add_argument("--land-root", default="../LAND")
    publish_prepare.add_argument("--releases-root", default=".local/releases")
    publish_diff = publish_commands.add_parser("diff")
    publish_diff.add_argument("--release", required=True)
    publish_diff.add_argument("--releases-root", default=".local/releases")
    publish_verify = publish_commands.add_parser("verify")
    publish_verify.add_argument("--release", required=True)
    publish_verify.add_argument("--releases-root", default=".local/releases")
    publish_verify.add_argument("--reviewer", required=True)
    publish_verify.add_argument("--reason", required=True)
    publish_install = publish_commands.add_parser("install")
    publish_install.add_argument("--release", required=True)
    publish_install.add_argument("--releases-root", default=".local/releases")
    publish_install.add_argument("--land-root", default="../LAND")
    publish_rollback = publish_commands.add_parser("rollback")
    publish_rollback.add_argument("--release", required=True,
                                  help="installed release id to roll back")
    publish_rollback.add_argument("--land-root", default="../LAND")
    publish_rollback.add_argument("--reason", required=True)
    place_publish = commands.add_parser(
        "place-publish", help="将明确选择的 POI 发布为 LAND Place 内容包；官方结构化来源可自动生成保守 fallback")
    place_commands = place_publish.add_subparsers(dest="place_publish_command", required=True)
    place_prepare = place_commands.add_parser("prepare")
    place_prepare.add_argument("--city", required=True)
    place_prepare.add_argument("--poi-id", action="append", required=True,
                               help="明确选择一个 POI；可重复使用多个")
    place_prepare.add_argument("--overlay",
                               help="可选的人工双语 publication overlay JSON；官方结构化来源可省略")
    place_prepare.add_argument("--releases-root", default=".local/releases")
    place_preflight = place_commands.add_parser(
        "preflight", help="只读检查明确 POI 是否满足发布前条件；不会创建发布文件")
    place_preflight.add_argument("--city", required=True)
    place_preflight.add_argument("--poi-id", action="append", required=True,
                                 help="明确选择一个 POI；可重复使用多个")
    place_preflight.add_argument("--overlay",
                                 help="可选的人工双语 publication overlay JSON")
    place_preflight.add_argument("--releases-root", default=".local/releases")
    place_diff = place_commands.add_parser("diff")
    place_diff.add_argument("--release", required=True)
    place_diff.add_argument("--releases-root", default=".local/releases")
    place_verify = place_commands.add_parser("verify")
    place_verify.add_argument("--release", required=True)
    place_verify.add_argument("--releases-root", default=".local/releases")
    place_verify.add_argument("--reviewer",
                              help="人工审核 release 时填写；deterministic_official 可省略")
    place_verify.add_argument("--reason",
                              help="人工审核原因；deterministic_official 可省略")
    place_install = place_commands.add_parser("install")
    place_install.add_argument("--release", required=True)
    place_install.add_argument("--releases-root", default=".local/releases")
    place_install.add_argument("--land-root", default="../LAND")
    place_install.add_argument("--run-app-checks", action="store_true",
                               help="验证后继续运行 Swift/Xcode 检查")
    place_rollback = place_commands.add_parser("rollback")
    place_rollback.add_argument("--release", required=True)
    place_rollback.add_argument("--land-root", default="../LAND")
    place_rollback.add_argument("--reason", required=True)
    place_rollback.add_argument("--run-app-checks", action="store_true")
    dashboard = commands.add_parser(
        "dashboard", help="启动局域网人工审核 Dashboard；默认仅监听本机且不安装 LAND")
    dashboard.add_argument("--host", default="127.0.0.1",
                           help="监听地址；局域网访问使用 0.0.0.0（会生成访问 token）")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--allow-install", action="store_true",
                           help="显式允许 Dashboard 执行已验证发布包的 LAND 安装")
    dashboard.add_argument("--land-root", default="../LAND")
    dashboard.add_argument("--releases-root", default=".local/releases")
    codex_review = commands.add_parser(
        "codex-review",
        help="记录 Codex 已完成的本地证据审核；本命令自身不调用任何模型")
    codex_review.add_argument("decision", choices=("approve", "reject"))
    codex_review.add_argument("id")
    codex_review.add_argument("--reviewer-model", default="codex:gpt-5")
    codex_review.add_argument("--reason", required=True)
    codex_review.add_argument("--evidence-digest", required=True)
    codex_review.add_argument(
        "--evidence-metadata",
        help="可选 JSON 文件，记录本次实际核对的来源 ID 和字段")
    for name in ("auth", "auth-status"):
        command = commands.add_parser(name)
        command.add_argument("source")
    for name in ("collect", "parse", "validate", "export", "pipeline"):
        command = commands.add_parser(name)
        if name in ("parse", "pipeline"):
            command.add_argument("--llm-extract", action="store_true",
                                 help="Opt in to Qwen text extraction; local .env supplies credentials")
        if name in ("collect", "parse", "export", "pipeline"):
            command.add_argument("--city")
            command.add_argument("--scope", help="JSON GeoScope file for bounded OSM collection")
        if name in ("validate", "pipeline"):
            if name == "validate":
                command.add_argument("--scope", help="JSON GeoScope file for enrichment")
            command.add_argument("--enrichment-references", help="JSON list of official directory references")
        if name in ("collect", "pipeline"):
            command.add_argument("--source", action="append", help="Configured source; repeatable for mock sources")
            command.add_argument("--limit", type=int, help="OSM default 20 (max 30); Threads default 5 (max 5)")
            command.add_argument("--category", action="append", help="OSM restaurant/cafe/museum; repeatable")
            command.add_argument("--borough", choices=("Manhattan", "Queens", "Brooklyn", "Bronx", "Staten Island"),
                                 help="NYC Open Data restaurant borough")
            command.add_argument("--timeout", type=int, default=15, help="OSM request timeout in seconds")
            command.add_argument("--query", help="Threads keyword search query")
            command.add_argument("--post-id", help="Recheck one known Threads post by official post ID")
            command.add_argument("--search-type", choices=("TOP", "RECENT"), default="RECENT")
            command.add_argument("--pages", type=int, default=1, help="Threads search pages, at most 2")
        if name in ("export", "pipeline"):
            command.add_argument("--output", help="Dataset path")
    review = commands.add_parser("review")
    review_commands = review.add_subparsers(dest="review_command")
    listing = review_commands.add_parser("list")
    listing.add_argument("--type", choices=("candidate", "mention", "poi"))
    listing.add_argument("--city")
    for action in ("show", "history", "approve", "reject", "resolve", "promote"):
        subcommand = review_commands.add_parser(action)
        subcommand.add_argument("id")
        if action == "reject":
            subcommand.add_argument("--reason", required=True)
        elif action in ("approve", "resolve", "promote"):
            subcommand.add_argument("--reason")
        if action == "resolve":
            subcommand.add_argument("--poi", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.root)
        sessions = MockSessionStore(args.session_file or str(config.root / ".local" / "mock-sessions.json"))
        if args.command in ("auth", "auth-status"):
            if args.source not in config.sources:
                raise ValueError("unknown source: {}".format(args.source))
            if config.sources[args.source].capability.access_mode != "AUTHENTICATED":
                raise ValueError("source does not need authentication")
            if args.source == "threads":
                token = os.environ.get("LAND_THREADS_ACCESS_TOKEN")
                if args.command == "auth":
                    print(json.dumps({"source": "threads", "status": "MANUAL_OAUTH_REQUIRED",
                                      "instructions": "Create a Meta App with Threads use case, authorize via official OAuth, then set LAND_THREADS_ACCESS_TOKEN in your local environment. Run auth-status threads to validate."}))
                    return 0
                if not token:
                    raise AuthRequired("AUTH_REQUIRED: threads; LAND_THREADS_ACCESS_TOKEN is unset")
                probe = ThreadsAdapter(token, post_id="0")
                print(json.dumps(probe.validate_token(), sort_keys=True))
                return 0
            if args.command == "auth":
                sessions.authenticate(args.source)
            print(sessions.status(args.source))
            return 0
        scope = load_scope(args.scope) if getattr(args, "scope", None) else None
        enrichment_plan = None
        if getattr(args, "enrichment_references", None):
            if not scope:
                raise ValueError("enrichment requires --scope")
            references = json.loads(Path(args.enrichment_references).read_text(encoding="utf-8"))
            if not isinstance(references, list):
                raise ValueError("enrichment references must be a JSON list")
            enrichment_plan = EnrichmentPlan(scope, tuple(DirectoryReference(**item) for item in references))
        if args.command in ("collect", "parse", "export", "pipeline"):
            if scope and args.city:
                raise ValueError("use either --scope or --city")
            if not scope and not args.city:
                raise ValueError("--city or --scope is required")
            if not scope and args.city not in config.cities and args.command in ("collect", "pipeline") and args.source != ["threads"]:
                raise ValueError("unknown city: {}".format(args.city))
        run_key = scope.name if scope else getattr(args, "city", None)
        db_path = args.db or str(config.root / ".local" / "land-data.db")
        if args.command == "dashboard":
            return_code = 0
            serve_dashboard(
                db_path, config.root, host=args.host, port=args.port,
                allow_install=args.allow_install,
                land_root=(config.root / args.land_root).resolve(),
                releases_root=(config.root / args.releases_root).resolve(),
                output=sys.stdout)
            return return_code
        # Frozen classifier evaluation is deliberately read-only: it does not
        # open or mutate the application database, persist the evaluation
        # records, or pass human labels into the classifier.
        if (args.command == "classifier" and
                args.classifier_command == "evaluate"):
            result = evaluate_jsonl(args.dataset, args.expected_fingerprint)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "location-verify":
            from .location_verification_replay import run as run_location_verification

            protected = (config.root / ".local" / "baselines").resolve()
            if args.journal.resolve().is_relative_to(protected):
                raise ValueError("journal must be outside frozen baselines")
            if args.journal.resolve() in {args.case.resolve(), args.tool_manifest.resolve()}:
                raise ValueError("journal cannot overwrite replay input")
            result = run_location_verification(
                args.case, args.tool_manifest, args.journal, args.policy, config.root,
                args.handoff_output)
            rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
            if args.output:
                if args.output.exists() or args.output.is_symlink():
                    raise FileExistsError("refusing to overwrite run summary")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered + "\n", encoding="utf-8")
                args.output.chmod(0o600)
            print(rendered)
            return 0
        if args.command == "location-verify-queue":
            from .location_verification import VerificationJournal

            if not args.journal.is_file() or args.journal.is_symlink():
                raise ValueError("verification journal does not exist or is a symlink")
            queue = VerificationJournal(args.journal).handoff_queue()
            if args.with_business_candidates:
                from .location_verification_db import attach_business_review_targets
                queue = attach_business_review_targets(Path(db_path), queue)
            print(json.dumps({"count": len(queue), "cases": queue},
                             ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "location-verify-prepare":
            from .location_verification_prepare import prepare

            result = prepare(args.dossier, args.expected_dossier_sha256,
                             args.case_id, args.output_dir, args.as_of)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "location-verify-prepare-db":
            from .location_verification_db import prepare_db

            result = prepare_db(Path(db_path), args.candidate_id,
                                args.output_dir, args.as_of)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "location-verify-review-hold":
            from .location_verification_review import hold_conflict

            result = hold_conflict(Path(db_path), args.case, args.tool_manifest,
                                   args.journal, apply=args.apply)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        with SQLiteStorage(db_path) as storage:
            if args.command == "classifier":
                if args.classifier_command != "status":
                    raise ValueError("unsupported classifier command: {}".format(
                        args.classifier_command))
                if args.limit < 1 or args.limit > 500:
                    raise ValueError("classifier limit must be between 1 and 500")
                rows = []
                counts = {}
                model_recommended = 0
                for item in current_classifications(storage):
                    content = storage.get(SourceContent, item.source_content_id)
                    if content is None:
                        continue
                    if args.city and content.collection_scope != args.city:
                        continue
                    if args.source and content.platform != args.source:
                        continue
                    counts[item.primary_label] = counts.get(item.primary_label, 0) + 1
                    if item.model_recommended:
                        model_recommended += 1
                    if args.label and item.primary_label != args.label:
                        continue
                    if args.model_recommended and not item.model_recommended:
                        continue
                    rows.append({
                        "id": item.id, "source_content_id": item.source_content_id,
                        "source": content.platform, "city": content.collection_scope,
                        "primary_label": item.primary_label, "route": item.route,
                        "confidence": item.confidence, "needs_review": item.needs_review,
                        "model_recommended": item.model_recommended,
                        "scenario_labels": item.scenario_labels,
                        "signals": item.signals, "reasons": item.reasons,
                        "classifier_version": item.classifier_version,
                        "created_at": item.created_at,
                    })
                rows.sort(key=lambda value: (value["created_at"], value["id"]), reverse=True)
                print(json.dumps({"status": "SUCCEEDED", "llm_calls": 0,
                                  "counts": dict(sorted(counts.items())),
                                  "model_recommended": model_recommended,
                                  "items": rows[:args.limit]},
                                 ensure_ascii=False, sort_keys=True))
                return 0
            if args.command == "social":
                if args.social_command == "list":
                    result = {
                        "status": "SUCCEEDED", "llm_calls": 0,
                        "platforms": [
                            {"provider": "xiaohongshu", "entrypoint": "xhs run",
                             "access": "logged_in_browser", "search": True},
                            {"provider": "threads", "entrypoint": "Dashboard or pipeline --source threads",
                             "access": "official_oauth_api", "search": True,
                             "auth_configured": bool(os.environ.get("LAND_THREADS_ACCESS_TOKEN", "").strip())},
                            {"provider": "other", "entrypoint": "social import",
                             "access": "authorized_browser_export_or_public_feed", "search": False},
                        ],
                    }
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                    return 0
                if args.city not in config.cities:
                    raise ValueError("unknown city: {}; create it first".format(args.city))
                input_path = Path(args.input).expanduser().resolve()
                if not input_path.is_file():
                    raise ValueError("social input JSON does not exist")
                if args.mode == "browser":
                    provider = BrowserVisibleInputProvider.from_json(
                        args.provider, str(input_path), page_size=args.page_size,
                        authenticated=True)
                else:
                    provider = PublicFeedInputProvider.from_json(
                        args.provider, str(input_path), page_size=args.page_size)
                # Reuse the strictest existing community retention policy:
                # export stays disabled, Raw text expires quickly, and no
                # credentials or media files enter the database.
                template = config.sources["threads"]
                dynamic_source = replace(
                    template, name=provider.contract.provider,
                    capability=provider.contract.adapter_capability())
                runtime_config = replace(
                    config, sources={**config.sources,
                                     provider.contract.provider: dynamic_source})
                adapter = SocialConnectorAdapter(
                    provider, storage, page_size=args.page_size)
                counts = Pipeline(storage, runtime_config).run(args.city, [adapter])
                result = {"status": "SUCCEEDED", "provider": provider.contract.provider,
                          "mode": args.mode, "city": args.city,
                          "collection": counts, "llm_calls": 0}
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
            if args.command == "codex-review":
                evidence_metadata = None
                if args.evidence_metadata:
                    metadata_path = (config.root / args.evidence_metadata).resolve()
                    evidence_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if not isinstance(evidence_metadata, dict):
                        raise ValueError("evidence metadata must be a JSON object")
                decision = codex_assisted_review(
                    storage, target_id=args.id, decision=args.decision,
                    reviewer_model=args.reviewer_model, reason=args.reason,
                    evidence_digest=args.evidence_digest,
                    evidence_metadata=evidence_metadata,
                    visibility_threshold=config.visibility_threshold)
                print(json.dumps(asdict(decision), ensure_ascii=False, sort_keys=True))
                return 0
            if args.command == "place-publish":
                releases_root = (config.root / args.releases_root).resolve()
                requested = getattr(args, "release", None)
                release_path = None
                if requested:
                    supplied = Path(requested)
                    release_path = (supplied if supplied.is_absolute() or supplied.exists()
                                    else releases_root / requested).resolve()
                if args.place_publish_command == "prepare":
                    overlay = None
                    if args.overlay:
                        overlay_path = (config.root / args.overlay).resolve()
                        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
                    city, city_profile = resolve_city_id(args.city, config.cities)
                    result = prepare_place_release(
                        storage, city=city, poi_ids=args.poi_id,
                        publication_overlay=overlay, releases_root=str(releases_root),
                        city_profile=city_profile)
                elif args.place_publish_command == "preflight":
                    city, city_profile = resolve_city_id(args.city, config.cities)
                    overlay = None
                    if args.overlay:
                        overlay_path = (config.root / args.overlay).resolve()
                        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
                    result = preflight_place_release(
                        storage, city=city, poi_ids=args.poi_id,
                        publication_overlay=overlay, releases_root=str(releases_root),
                        city_profile=city_profile)
                elif args.place_publish_command == "diff":
                    result = diff_place_release(str(release_path), storage=storage)
                elif args.place_publish_command == "verify":
                    result = verify_place_release(str(release_path), storage=storage,
                                                  reviewer=args.reviewer, reason=args.reason)
                elif args.place_publish_command == "install":
                    result = install_place_release(
                        str(release_path), str((config.root / args.land_root).resolve()),
                        run_app_checks=args.run_app_checks)
                else:
                    result = rollback_place_release(
                        args.release, str((config.root / args.land_root).resolve()),
                        reason=args.reason, run_app_checks=args.run_app_checks)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                failed = ((args.place_publish_command == "verify" and not result.get("ok")) or
                          result.get("status") in ("INSTALL_FAILED_ROLLED_BACK",
                                                   "ROLLBACK_FAILED_RESTORED"))
                return 1 if failed else 0
            if args.command == "publish":
                releases_root = (config.root / getattr(args, "releases_root", ".local/releases")).resolve()
                requested = getattr(args, "release", None)
                release_path = None
                if requested:
                    supplied = Path(requested)
                    release_path = (supplied if supplied.is_absolute() or supplied.exists()
                                    else releases_root / requested).resolve()
                if args.publish_command == "prepare":
                    land_root = (config.root / args.land_root).resolve()
                    base_index = land_root / "LAND" / "Resources" / "question-content-index-v1.json"
                    result = prepare_release(
                        storage, city=args.city, base_index_path=base_index,
                        releases_root=releases_root, city_countries=config.cities,
                        source_policies=config.sources)
                elif args.publish_command == "diff":
                    result = release_diff(release_path)
                elif args.publish_command == "verify":
                    result = verify_release(
                        release_path, storage=storage, reviewer=args.reviewer,
                        reason=args.reason)
                elif args.publish_command == "install":
                    land_root = (config.root / args.land_root).resolve()
                    result = install_release(release_path, land_root)
                else:
                    land_root = (config.root / args.land_root).resolve()
                    result = rollback_release(
                        args.release, land_root, reason=args.reason)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                failed = ((args.publish_command == "verify" and not result.get("ok")) or
                          result.get("status") in ("INSTALL_FAILED_ROLLED_BACK",
                                                   "ROLLBACK_FAILED_RESTORED"))
                return 1 if failed else 0
            if args.command == "xhs":
                if args.xhs_command in ("login", "status", "collect", "run"):
                    browser_options = {"port": args.port}
                    if args.chrome_path:
                        browser_options["chrome_path"] = args.chrome_path
                    browser = ChromeRemoteBrowser(config.root, **browser_options)
                if args.xhs_command == "login":
                    result = browser.launch_login()
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                    return 0
                if args.xhs_command == "status":
                    result = {"browser": browser.status(),
                              "collection": collection_status(storage),
                              "processing": processing_status(storage)}
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                    return 0
                if args.xhs_command == "runs":
                    result = {"runs": collection_runs(storage, args.limit),
                              "processing": processing_status(storage, args.limit)}
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                    return 0
                if args.xhs_command == "knowledge":
                    action = args.knowledge_command
                    if action in (None, "candidates"):
                        result = {"items": knowledge_candidates(
                            storage, city=args.city, scenario=args.scenario,
                            limit=args.limit)}
                    elif action == "extract":
                        client = (qwen_knowledge_from_env(config.root)
                                  if args.llm == "auto" else None)
                        result = process_knowledge_candidates(
                            storage, mode=args.llm, candidate_ids=args.id,
                            limit=args.limit, client=client,
                            retry_errors=args.retry_errors)
                    elif action == "drafts":
                        result = {"items": knowledge_drafts(
                            storage, candidate_id=args.candidate_id,
                            status=args.status, limit=args.limit)}
                    elif action == "history":
                        result = {"id": args.id,
                                  "history": knowledge_review_history(storage, args.id)}
                    elif action == "approve":
                        result = review_knowledge_draft(
                            storage, args.id, action="approve", reviewer=args.reviewer,
                            reason=args.reason, reference_url=args.reference_url,
                            verified_at=args.verified_at,
                            reference_kind=args.reference_kind,
                            reference_title=args.reference_title,
                            valid_until=args.valid_until)
                    else:
                        result = review_knowledge_draft(
                            storage, args.id, action="reject", reviewer=args.reviewer,
                            reason=args.reason)
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                    return 1 if action == "extract" and result["failed"] else 0
                if args.xhs_command == "process":
                    result = process_xhs(storage, config, args.city, llm_mode=args.llm,
                                         limit=args.limit, retry_errors=args.retry_errors)
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                    return 0 if not result["errors"] else 1
                query_file = args.query_file
                setup = None
                if args.city:
                    default_bank_path = config.root / "config" / "xiaohongshu-queries.json"
                    default_bank = (json.loads(default_bank_path.read_text(encoding="utf-8"))
                                    if default_bank_path.is_file() else {})
                    uses_matching_default = (
                        args.city in config.cities and
                        args.query_file == "config/xiaohongshu-queries.json" and
                        default_bank.get("city") == args.city)
                    if not uses_matching_default:
                        setup = ensure_city(config.root, args.city, args.city_name, args.country)
                        if not args.query and args.query_file == "config/xiaohongshu-queries.json":
                            query_file = setup["query_file"]
                        if setup["city_created"]:
                            config = load_config(args.root)
                plan = _xhs_query_plan(config.root, query_file, args.query, args.group,
                                       args.city, args.posts, args.scrolls, args.pause_seconds)
                if plan["city"] not in config.cities:
                    raise ValueError(
                        "unknown xhs city: {}; add it to config/cities.json first".format(
                            plan["city"]))
                browser_state = browser.status()
                if browser_state.get("browser") != "RUNNING" or browser_state.get("auth_required"):
                    opened = browser.launch_login()
                    print(json.dumps({"status": "AUTH_REQUIRED", "browser": opened,
                                      "next_action": "finish login, then rerun the same command"},
                                     ensure_ascii=False, sort_keys=True))
                    return 2
                pipeline = Pipeline(storage, config)

                def persist(item):
                    pipeline.collect(plan["city"], [XHSCaptureAdapter(item)])

                collected = collect_xhs(
                    storage, config.root, plan["city"], plan["queries"],
                    posts=plan["posts"], scrolls=plan["scrolls"],
                    pause_seconds=plan["pause_seconds"], refresh=args.refresh,
                    browser=browser, sink=persist,
                    query_groups=plan["query_groups"])
                if setup and (setup["city_created"] or setup["query_file_created"]):
                    collected["city_setup"] = setup
                if collected["status"] == "AUTH_REQUIRED":
                    opened = browser.launch_login()
                    print(json.dumps({**collected, "browser": opened,
                                      "next_action": "finish login, then rerun the same command"},
                                     ensure_ascii=False, sort_keys=True))
                    return 2
                if collected["status"] != "SUCCEEDED":
                    print(json.dumps(collected, ensure_ascii=False, sort_keys=True))
                    return 1
                deterministic = process_xhs(storage, config, plan["city"], llm_mode="never")
                model = None
                if args.xhs_command == "run" and args.llm == "auto":
                    model = process_xhs(storage, config, plan["city"], llm_mode="auto",
                                        limit=args.llm_limit, retry_errors=True)
                failed = bool(deterministic["errors"] or (model and model["errors"]))
                result = {"status": "PARTIAL_FAILURE" if failed else "SUCCEEDED",
                          "collection": collected,
                          "processing": deterministic, "llm_processing": model}
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 1 if failed else 0
            if args.command == "discover-sources":
                if args.list_candidates and args.list_runs:
                    raise ValueError("choose only one discovery listing")
                if args.list_candidates:
                    print(json.dumps({"candidates": candidate_dicts(storage)},
                                     ensure_ascii=False, sort_keys=True))
                    return 0
                if args.list_runs:
                    print(json.dumps({"runs": run_dicts(storage, args.limit)},
                                     ensure_ascii=False, sort_keys=True))
                    return 0
                discovery_path = str((config.root / args.discovery_config).resolve())
                result = SourceDiscoveryRunner(
                    storage, load_discovery_config(discovery_path)).run()
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0 if result["status"] == "SUCCEEDED" else 1
            if args.command == "auto-collect":
                if args.discovery_only and args.collection_only:
                    raise ValueError("choose only one of --discovery-only or --collection-only")
                result = run_auto_collect(
                    storage, config, sessions, root=config.root, city=args.city,
                    discovery_config_path=str((config.root / args.discovery_config).resolve()),
                    source_config_path=str((config.root / args.source_config).resolve()),
                    sources=args.source, discovery_only=args.discovery_only,
                    collection_only=args.collection_only)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0 if result["status"] in ("SUCCEEDED", "SUCCEEDED_WITH_REVIEW") else 1
            if args.command == "multi-pipeline":
                specs = load_specs(str((config.root / args.source_config).resolve()), config.root,
                                   args.only_source)

                def make_adapter(spec):
                    scope = spec.scope
                    categories = list(spec.categories) if spec.categories else None
                    if spec.borough:
                        categories = [spec.borough]
                    return config.adapter(spec.name, sessions, scope, categories,
                                          spec.limit, spec.timeout, storage=storage,
                                          collection_key=spec.collection_key, max_pages=spec.max_pages,
                                          initial_updated_at=spec.initial_updated_at,
                                          overlap_seconds=spec.overlap_seconds,
                                          safety_rescan_days=spec.safety_rescan_days,
                                          endpoints=spec.endpoints, max_retries=spec.max_retries)

                result = MultiSourceRunner(Pipeline(storage, config), storage, specs,
                                           make_adapter).run()
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0 if result["status"] == "SUCCEEDED" else 1
            if args.command == "process-land-raw":
                print(json.dumps(process_land_raw(storage, config, args.raw_dir,
                                                  use_llm=args.llm_extract, limit=args.limit,
                                                  retry_errors=args.retry_errors,
                                                  allow_historical=args.allow_historical),
                                 ensure_ascii=False, sort_keys=True))
                return 0
            llm_service = (LLMExtractionService(qwen_from_env(config.root))
                           if getattr(args, "llm_extract", False) else None)
            pipeline = Pipeline(storage, config, llm_service)
            if args.command in ("collect", "pipeline"):
                names = args.source or (["osm"] if scope else
                                        [name for name in config.sources if name not in (
                                            "osm", "official-directory", "mock-social-2c", "threads",
                                            "jfk-arrival-guidance", "nyc-open-data-restaurants",
                                            "nyc-parks-events", "xiaohongshu-browser")])
                if ("osm" in names and not scope) or (scope and len(names) != 1):
                    raise ValueError("scoped collection requires one source; OSM requires --scope")
                if "threads" in names:
                    if names != ["threads"] or scope or args.category:
                        raise ValueError("Threads requires a single source, --city, and no --scope/--category")
                    if bool(args.query) == bool(args.post_id):
                        raise ValueError("Threads requires exactly one of --query or --post-id")
                elif args.query or args.post_id or args.pages != 1 or args.search_type != "RECENT":
                    raise ValueError("Threads search options require --source threads")
                for name in names:
                    if name not in config.sources:
                        raise ValueError("unknown source: {}".format(name))
                adapters = [config.adapter(name, sessions, scope,
                                           [args.borough] if name == "nyc-open-data-restaurants" and args.borough else args.category,
                                           args.limit if args.limit is not None else (5 if name == "threads" else 20),
                                           args.timeout, threads_query=args.query,
                                           threads_post_id=args.post_id,
                                           threads_search_type=args.search_type,
                                           threads_pages=args.pages,
                                           threads_token=os.environ.get("LAND_THREADS_ACCESS_TOKEN"))
                            for name in names]
                if args.command == "collect":
                    result = {"collected": pipeline.collect(run_key, adapters, scope)}
                else:
                    result = pipeline.run(run_key, adapters, scope, enrichment_plan)
                    if enrichment_plan:
                        result["enrichment"] = pipeline.last_enrichment_result
                    export_city = scope.city or scope.name if scope else args.city
                    if scope:
                        result["scope"] = run_key
                    result["export"] = args.output or str(config.root / ".local" / "exports" / (run_key + ".json"))
                    write_dataset(storage, config, export_city, result["export"],
                                  scope_id=scope.name if scope else None)
            elif args.command == "parse":
                result = {"parsed": pipeline.parse(run_key)}
            elif args.command == "validate":
                pipeline.validate(enrichment_plan)
                result = pipeline.counts()
                if enrichment_plan:
                    result["enrichment"] = pipeline.last_enrichment_result
            elif args.command == "review":
                review = ReviewService(storage, config.visibility_threshold)
                action = args.review_command or "list"
                if action == "list":
                    result = review.list(getattr(args, "type", None), getattr(args, "city", None))
                elif action == "show":
                    result = review.show(args.id)
                elif action == "history":
                    result = {"id": args.id, "history": review.history(args.id)}
                elif action == "approve":
                    result = asdict(review.approve(args.id, args.reason or "Confirmed by local reviewer"))
                elif action == "reject":
                    result = asdict(review.reject(args.id, args.reason))
                elif action == "resolve":
                    result = asdict(review.resolve(args.id, args.poi, args.reason or "Matched to existing POI"))
                else:
                    result = asdict(review.promote(args.id, args.reason or "Confirmed as a new POI"))
            else:
                path = args.output or str(config.root / ".local" / "exports" / (run_key + ".json"))
                export_city = scope.city or scope.name if scope else args.city
                output = write_dataset(storage, config, export_city, path,
                                       scope_id=scope.name if scope else None)
                result = {"output": path, "pois": len(output["pois"]), "mentions": len(output["mentions"])}
            if llm_service:
                result["llm_extraction"] = {"calls": llm_service.calls,
                                            "cache_hits": llm_service.cache_hits,
                                            "fallbacks": llm_service.fallbacks}
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
    except AuthRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, KeyError, OSError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
