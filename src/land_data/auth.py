"""A mock-only session. Real credential handling is intentionally absent."""
import json
from pathlib import Path
from typing import Dict


class AuthRequired(Exception):
    pass


class MockSessionStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def _read(self) -> Dict[str, bool]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def status(self, source: str) -> str:
        return "AUTHENTICATED" if self._read().get(source) is True else "AUTH_REQUIRED"

    def authenticate(self, source: str) -> None:
        states = self._read()
        states[source] = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(states, sort_keys=True), encoding="utf-8")
        self.path.chmod(0o600)

    def revoke(self, source: str) -> None:
        states = self._read()
        states.pop(source, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(states, sort_keys=True), encoding="utf-8")
        self.path.chmod(0o600)
