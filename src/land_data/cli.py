"""CLI entry point; business operations live in Pipeline and processing modules."""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .auth import AuthRequired, MockSessionStore
from .config import load_config
from .export import write_dataset
from .geoscope import load_scope
from .pipeline import Pipeline
from .review import ReviewService
from .sqlite_storage import SQLiteStorage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="land-data")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--db", help="SQLite file; default <root>/.local/land-data.db")
    parser.add_argument("--session-file", help="Mock session state; default <root>/.local/mock-sessions.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("auth", "auth-status"):
        command = commands.add_parser(name)
        command.add_argument("source")
    for name in ("collect", "parse", "validate", "export", "pipeline"):
        command = commands.add_parser(name)
        if name in ("collect", "parse", "export", "pipeline"):
            command.add_argument("--city")
            command.add_argument("--scope", help="JSON GeoScope file for bounded OSM collection")
        if name in ("collect", "pipeline"):
            command.add_argument("--source", action="append", help="Configured source; repeatable for mock sources")
            command.add_argument("--limit", type=int, default=20, help="OSM maximum results, at most 30")
            command.add_argument("--category", action="append", help="OSM restaurant/cafe/museum; repeatable")
            command.add_argument("--timeout", type=int, default=15, help="OSM request timeout in seconds")
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
            if args.command == "auth":
                sessions.authenticate(args.source)
            print(sessions.status(args.source))
            return 0
        scope = load_scope(args.scope) if getattr(args, "scope", None) else None
        if args.command in ("collect", "parse", "export", "pipeline"):
            if scope and args.city:
                raise ValueError("use either --scope or --city")
            if not scope and not args.city:
                raise ValueError("--city or --scope is required")
            if not scope and args.city not in config.cities:
                raise ValueError("unknown city: {}".format(args.city))
        run_key = scope.name if scope else getattr(args, "city", None)
        db_path = args.db or str(config.root / ".local" / "land-data.db")
        with SQLiteStorage(db_path) as storage:
            pipeline = Pipeline(storage, config)
            if args.command in ("collect", "pipeline"):
                names = args.source or (["osm"] if scope else
                                        [name for name in config.sources if name != "osm"])
                if ("osm" in names) != (scope is not None) or (scope and names != ["osm"]):
                    raise ValueError("OSM requires --scope and cannot be mixed with mock sources")
                for name in names:
                    if name not in config.sources:
                        raise ValueError("unknown source: {}".format(name))
                adapters = [config.adapter(name, sessions, scope, args.category, args.limit, args.timeout)
                            for name in names]
                if args.command == "collect":
                    result = {"collected": pipeline.collect(run_key, adapters, scope)}
                else:
                    result = pipeline.run(run_key, adapters, scope)
                    export_city = scope.city or scope.name if scope else args.city
                    if scope:
                        result["scope"] = run_key
                    result["export"] = args.output or str(config.root / ".local" / "exports" / (run_key + ".json"))
                    write_dataset(storage, config, export_city, result["export"])
            elif args.command == "parse":
                result = {"parsed": pipeline.parse(run_key)}
            elif args.command == "validate":
                pipeline.validate()
                result = pipeline.counts()
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
                output = write_dataset(storage, config, export_city, path)
                result = {"output": path, "pois": len(output["pois"]), "mentions": len(output["mentions"])}
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
