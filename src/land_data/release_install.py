"""Atomic installation and rollback for verified bundled LAND QAU releases."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from typing import Callable, Optional

from .util import utc_now


RESOURCE_RELATIVE = Path("LAND/Resources/question-content-index-v1.json")
INSTALL_STATE_RELATIVE = Path(".local/land-data-publish")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    temporary.write_bytes(raw)
    os.replace(str(temporary), str(path))


def _append_jsonl(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _run_land_checks(land_root: Path, release_id: str,
                     runner: Optional[Callable] = None):
    safe = "".join(char for char in release_id if char.isalnum() or char in "-_")[:80]
    environment = dict(os.environ)
    environment.update({
        "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer",
        "CLANG_MODULE_CACHE_PATH": "/tmp/LAND-clang-module-cache",
        "SWIFTPM_MODULECACHE_OVERRIDE": "/tmp/LAND-swift-module-cache",
    })
    commands = [
        ["swift", "test", "--disable-sandbox", "--scratch-path",
         "/tmp/LAND-{}-tests".format(safe), "-q"],
        ["xcodebuild", "-quiet", "-project", "LAND.xcodeproj", "-scheme", "LAND",
         "-configuration", "Debug", "-sdk", "iphonesimulator",
         "-destination", "generic/platform=iOS Simulator", "-derivedDataPath",
         "/tmp/LAND-{}-xcode".format(safe), "CODE_SIGNING_ALLOWED=NO", "build"],
    ]
    execute = runner or subprocess.run
    records = []
    for command in commands:
        try:
            completed = execute(command, cwd=str(land_root), env=environment,
                                capture_output=True, text=True, timeout=1800)
            returncode = int(completed.returncode)
            output = ((completed.stdout or "") + (completed.stderr or ""))[-4000:]
        except Exception as exc:
            returncode = 1
            output = type(exc).__name__ + ": " + str(exc)[:500]
        records.append({"command": command[0], "returncode": returncode,
                        "output_tail": output})
        if returncode:
            return False, records
    return True, records


def _release_input(release_dir):
    release_dir = Path(release_dir).resolve()
    manifest_path = release_dir / "manifest.json"
    index_path = release_dir / "question-content-index-v1.json"
    if not manifest_path.is_file() or not index_path.is_file():
        raise ValueError("release manifest or QAU index is missing")
    manifest = _read_json(manifest_path)
    if manifest.get("status") != "VERIFIED" or not manifest.get("verification", {}).get("reviewer"):
        raise ValueError("only an explicitly reviewed VERIFIED release can be installed")
    if manifest.get("blockers"):
        raise ValueError("release still contains unresolved blockers")
    if not manifest.get("release_question_ids"):
        raise ValueError("empty release cannot be installed")
    raw = index_path.read_bytes()
    expected = manifest.get("files", {}).get(index_path.name)
    expected = expected.get("sha256") if isinstance(expected, dict) else expected
    if not isinstance(expected, str) or _sha(raw) != expected:
        raise ValueError("release QAU index hash does not match manifest")
    return release_dir, manifest, raw


def install_release(release_dir, land_root, *, runner=None):
    """Install one verified bundled index and restore the previous bytes on failure."""
    release_dir, manifest, release_raw = _release_input(release_dir)
    land_root = Path(land_root).resolve()
    target = land_root / RESOURCE_RELATIVE
    if not target.is_file():
        raise ValueError("LAND bundled QAU resource is missing")
    previous = target.read_bytes()
    release_sha = _sha(release_raw)
    if _sha(previous) == release_sha:
        return {"release_id": manifest["release_id"], "status": "INSTALLED",
                "idempotent": True, "target": str(target), "checks": []}
    base_sha = manifest.get("base_index", {}).get("sha256")
    if _sha(previous) != base_sha:
        raise ValueError("LAND QAU resource differs from the release base; prepare a new release")
    state_root = land_root / INSTALL_STATE_RELATIVE
    backup_dir = state_root / "backups" / manifest["release_id"]
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / RESOURCE_RELATIVE.name
    if backup_path.is_file():
        if _sha(backup_path.read_bytes()) != _sha(previous):
            raise ValueError("existing release backup differs from the current base")
    else:
        backup_path.write_bytes(previous)
    install_record = {
        "release_id": manifest["release_id"], "release_dir": str(release_dir),
        "target": str(target), "previous_sha256": _sha(previous),
        "installed_sha256": release_sha, "started_at": utc_now(),
    }
    _atomic_bytes(target, release_raw)
    ok, checks = _run_land_checks(land_root, manifest["release_id"], runner)
    install_record["checks"] = checks
    install_record["ended_at"] = utc_now()
    if not ok:
        _atomic_bytes(target, previous)
        install_record["status"] = "INSTALL_FAILED_ROLLED_BACK"
        _append_jsonl(state_root / "history.jsonl", install_record)
        return {"release_id": manifest["release_id"], "status": install_record["status"],
                "idempotent": False, "target": str(target), "checks": checks}
    install_record["status"] = "INSTALLED"
    (backup_dir / "install.json").write_text(
        json.dumps(install_record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    _append_jsonl(state_root / "history.jsonl", install_record)
    return {"release_id": manifest["release_id"], "status": "INSTALLED",
            "idempotent": False, "target": str(target), "checks": checks}


def rollback_release(release_id: str, land_root, *, runner=None, reason: str):
    """Restore the exact bytes saved immediately before a successful installation."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("rollback reason is required")
    land_root = Path(land_root).resolve()
    state_root = land_root / INSTALL_STATE_RELATIVE
    backup_dir = state_root / "backups" / release_id
    record_path = backup_dir / "install.json"
    backup_path = backup_dir / RESOURCE_RELATIVE.name
    target = land_root / RESOURCE_RELATIVE
    if not record_path.is_file() or not backup_path.is_file() or not target.is_file():
        raise ValueError("verified installation backup was not found")
    installed = _read_json(record_path)
    current = target.read_bytes()
    if _sha(current) != installed.get("installed_sha256"):
        raise ValueError("LAND QAU resource changed after this install; refusing unsafe rollback")
    previous = backup_path.read_bytes()
    if _sha(previous) != installed.get("previous_sha256"):
        raise ValueError("rollback backup hash mismatch")
    _atomic_bytes(target, previous)
    ok, checks = _run_land_checks(land_root, release_id + "-rollback", runner)
    entry = {"release_id": release_id, "status": "ROLLED_BACK" if ok else "ROLLBACK_FAILED_RESTORED",
             "reason": reason.strip(), "at": utc_now(), "checks": checks}
    if not ok:
        _atomic_bytes(target, current)
    _append_jsonl(state_root / "history.jsonl", entry)
    return entry
