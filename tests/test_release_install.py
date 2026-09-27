import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from land_data.release_install import install_release, rollback_release
from land_data.cli import build_parser


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Runner:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        failed = self.fail_at == len(self.calls)
        return SimpleNamespace(returncode=1 if failed else 0,
                               stdout="", stderr="failed" if failed else "")


class ReleaseInstallTests(unittest.TestCase):
    def test_publish_cli_requires_explicit_release_review(self):
        parsed = build_parser().parse_args([
            "publish", "verify", "--release", "release-id",
            "--reviewer", "editor", "--reason", "diff reviewed"])
        self.assertEqual(parsed.publish_command, "verify")
        self.assertEqual(parsed.reviewer, "editor")

    def _fixture(self, root, *, status="VERIFIED", blockers=None, questions=True):
        root = Path(root)
        land = root / "LAND"
        target = land / "LAND" / "Resources" / "question-content-index-v1.json"
        target.parent.mkdir(parents=True)
        old = b'{"revision":1}\n'
        new = b'{"revision":2}\n'
        target.write_bytes(old)
        release = root / "release"
        release.mkdir()
        (release / target.name).write_bytes(new)
        manifest = {
            "release_id": "knowledge-new-york-test", "status": status,
            "verification": {"reviewer": "release-editor", "reason": "reviewed"},
            "blockers": blockers or [],
            "release_question_ids": ["q1"] if questions else [],
            "base_index": {"sha256": sha(old)},
            "files": {target.name: sha(new)},
        }
        (release / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return land, release, target, old, new

    def test_install_runs_swift_and_xcode_then_can_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            land, release, target, old, new = self._fixture(temp)
            runner = Runner()
            result = install_release(release, land, runner=runner)
            self.assertEqual(result["status"], "INSTALLED")
            self.assertEqual(target.read_bytes(), new)
            self.assertEqual([call[0] for call in runner.calls], ["swift", "xcodebuild"])
            rolled = rollback_release("knowledge-new-york-test", land,
                                      runner=Runner(), reason="restore previous release")
            self.assertEqual(rolled["status"], "ROLLED_BACK")
            self.assertEqual(target.read_bytes(), old)

    def test_failed_app_check_restores_previous_resource(self):
        with tempfile.TemporaryDirectory() as temp:
            land, release, target, old, _new = self._fixture(temp)
            result = install_release(release, land, runner=Runner(fail_at=2))
            self.assertEqual(result["status"], "INSTALL_FAILED_ROLLED_BACK")
            self.assertEqual(target.read_bytes(), old)
            retried = install_release(release, land, runner=Runner())
            self.assertEqual(retried["status"], "INSTALLED")

    def test_same_release_can_be_reinstalled_after_explicit_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            land, release, target, old, new = self._fixture(temp)
            install_release(release, land, runner=Runner())
            rollback_release("knowledge-new-york-test", land,
                             runner=Runner(), reason="first rollback")
            self.assertEqual(target.read_bytes(), old)
            reinstalled = install_release(release, land, runner=Runner())
            self.assertEqual(reinstalled["status"], "INSTALLED")
            self.assertEqual(target.read_bytes(), new)

    def test_install_rejects_unverified_blocked_empty_or_wrong_base(self):
        cases = [
            {"status": "PREPARED"},
            {"blockers": [{"code": "CONTENT_GAP"}]},
            {"questions": False},
        ]
        for options in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                land, release, _target, _old, _new = self._fixture(temp, **options)
                with self.assertRaises(ValueError):
                    install_release(release, land, runner=Runner())
        with tempfile.TemporaryDirectory() as temp:
            land, release, target, _old, _new = self._fixture(temp)
            target.write_bytes(b"unexpected")
            with self.assertRaisesRegex(ValueError, "differs from the release base"):
                install_release(release, land, runner=Runner())


if __name__ == "__main__":
    unittest.main()
