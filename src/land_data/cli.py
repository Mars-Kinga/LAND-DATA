"""CLI entry point; business operations live in Pipeline and processing modules."""
import argparse
import json
import sys
from pathlib import Path

from .auth import AuthRequired, MockSessionStore
from .config import load_config
from .export import write_dataset
from .pipeline import Pipeline
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
    for name in ("collect", "parse", "validate", "review", "export", "pipeline"):
        command = commands.add_parser(name)
        if name in ("collect", "parse", "export", "pipeline"):
            command.add_argument("--city", required=True)
        if name in ("collect", "pipeline"):
            command.add_argument("--source", action="append", help="Configured mock source; repeatable")
        if name in ("export", "pipeline"):
            command.add_argument("--output", help="Dataset path")
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
        if hasattr(args, "city") and args.city not in config.cities:
            raise ValueError("unknown city: {}".format(args.city))
        db_path = args.db or str(config.root / ".local" / "land-data.db")
        with SQLiteStorage(db_path) as storage:
            pipeline = Pipeline(storage, config)
            if args.command in ("collect", "pipeline"):
                names = args.source or list(config.sources)
                for name in names:
                    if name not in config.sources:
                        raise ValueError("unknown source: {}".format(name))
                adapters = [config.adapter(name, sessions) for name in names]
                if args.command == "collect":
                    result = {"collected": pipeline.collect(args.city, adapters)}
                else:
                    result = pipeline.run(args.city, adapters)
                    result["export"] = args.output or str(config.root / ".local" / "exports" / (args.city + ".json"))
                    write_dataset(storage, config, args.city, result["export"])
            elif args.command == "parse":
                result = {"parsed": pipeline.parse(args.city)}
            elif args.command == "validate":
                pipeline.validate()
                result = pipeline.counts()
            elif args.command == "review":
                result = pipeline.review_queue()
            else:
                path = args.output or str(config.root / ".local" / "exports" / (args.city + ".json"))
                output = write_dataset(storage, config, args.city, path)
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
