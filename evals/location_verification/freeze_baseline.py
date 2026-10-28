"""Freeze and verify local pre-agent inputs without contacting external services.

The snapshot is intentionally stored below .local/ because the databases and
historical source material must not be published with evaluation code.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tarfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("src", "config", "tests", "fixtures", "scripts", "overlays", "evals")
SOURCE_FILES = (
    "pyproject.toml",
    "land-data",
    ".env.example",
    "README.md",
    "evals/README.md",
    "evals/classifier_regression.jsonl",
    "evals/location-verification-baseline-2026-10-28.md",
    "docs/location-verification-agent-harness-plan.md",
    "docs/xhs-verification-a.json",
    "docs/xhs-verification-b.json",
    "docs/xhs-verification-c.json",
    "docs/xhs-verification-final.json",
)
DATABASES = (".local/land-data.db", ".local/nyc-incremental-acceptance-v2.db")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def source_paths() -> list[Path]:
    paths: list[Path] = []
    for name in SOURCE_DIRS:
        directory = ROOT / name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and not path.is_symlink() and not any(
                part == "__pycache__" or part.startswith(".")
                for part in path.relative_to(ROOT).parts
            ) and path.suffix != ".pyc":
                paths.append(path)
    for name in SOURCE_FILES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(name)
        paths.append(path)
    return sorted(set(paths), key=lambda path: path.relative_to(ROOT).as_posix())


def git_output(*args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=ROOT, text=True).strip()


def freeze_database(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as source_db:
        source_db.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(destination)) as snapshot_db:
            source_db.backup(snapshot_db)
            # A self-contained frozen copy should not depend on mutable WAL sidecars.
            snapshot_db.execute("PRAGMA journal_mode=DELETE")
    destination.chmod(0o600)
    with closing(sqlite3.connect(destination.as_uri() + "?mode=ro&immutable=1", uri=True)) as check_db:
        result = check_db.execute("PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"SQLite quick_check failed for {destination}: {result}")
    return {
        "source": source.relative_to(ROOT).as_posix(),
        "snapshot": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": file_digest(destination),
        "quick_check": "ok",
    }


def create(output: Path) -> dict[str, Any]:
    output = output.resolve()
    if not output.is_relative_to((ROOT / ".local").resolve()):
        raise ValueError("snapshot output must be under the ignored .local directory")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    output.chmod(0o700)
    source_entries: list[dict[str, Any]] = []
    archive = output / "source.tar.gz"
    with archive.open("wb") as file_handle:
        with gzip.GzipFile(fileobj=file_handle, mode="wb", filename="", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for path in source_paths():
                    content = path.read_bytes()
                    name = path.relative_to(ROOT).as_posix()
                    entry = tarfile.TarInfo(name)
                    entry.size = len(content)
                    entry.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    entry.mtime = 0
                    tar.addfile(entry, io.BytesIO(content))
                    source_entries.append({"path": name, "bytes": len(content),
                                           "sha256": digest(content)})
    archive.chmod(0o600)
    databases = [freeze_database(ROOT / name, output / Path(name).name)
                 for name in DATABASES]
    status = git_output("status", "--porcelain=v1")
    manifest: dict[str, Any] = {
        "schema_version": "location-verification-pre-agent-freeze-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": git_output("rev-parse", "HEAD"),
        "worktree_dirty": bool(status),
        "worktree_status_entry_count": len(status.splitlines()),
        "python_version": platform.python_version(),
        "source_archive": {"path": archive.name, "bytes": archive.stat().st_size,
                           "sha256": file_digest(archive)},
        "source_files": source_entries,
        "databases": databases,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    return manifest


def verify(output: Path) -> dict[str, Any]:
    output = output.resolve()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    archive_info = manifest["source_archive"]
    archive = output / archive_info["path"]
    if file_digest(archive) != archive_info["sha256"]:
        raise ValueError("source archive hash mismatch")
    expected = {row["path"]: row for row in manifest["source_files"]}
    seen: set[str] = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isfile() or member.name not in expected or member.name in seen:
                raise ValueError(f"unexpected archive member: {member.name}")
            content = tar.extractfile(member).read()  # type: ignore[union-attr]
            row = expected[member.name]
            if len(content) != row["bytes"] or digest(content) != row["sha256"]:
                raise ValueError(f"source file hash mismatch: {member.name}")
            seen.add(member.name)
    if seen != expected.keys():
        raise ValueError("source archive is missing files")
    for row in manifest["databases"]:
        path = output / row["snapshot"]
        if path.stat().st_size != row["bytes"] or file_digest(path) != row["sha256"]:
            raise ValueError(f"database hash mismatch: {path.name}")
        # Immutable reads avoid creating -wal/-shm files while validating a freeze.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError(f"database integrity failure: {path.name}")
    return {"status": "VERIFIED", "source_files": len(seen),
            "databases": len(manifest["databases"]),
            "manifest_sha256": file_digest(output / "manifest.json")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        sub = commands.add_parser(command)
        sub.add_argument("output", type=Path)
    arguments = parser.parse_args()
    result = create(arguments.output) if arguments.command == "create" else verify(arguments.output)
    print(json.dumps(result if arguments.command == "verify" else {
        "status": "FROZEN", "source_files": len(result["source_files"]),
        "databases": len(result["databases"]), "output": str(arguments.output.resolve()),
        "git_head": result["git_head"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
