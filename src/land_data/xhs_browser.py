"""Bounded Xiaohongshu collection through a dedicated, user-controlled Chrome profile.

The module uses the public Chrome DevTools Protocol over localhost.  It never asks
Chrome for cookies or storage and never issues fetch/XHR from the page.
"""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import time
from typing import Callable, Iterable, Optional
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .adapters import SourceItem
from .auth import AuthRequired
from .util import stable_id, utc_now


SOURCE = "xiaohongshu-browser"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
POST_ID = re.compile(r"/(?:explore|search_result|discovery/item)/([0-9a-f]{24})(?:/|$)", re.I)
BLOCKER_WORDS = ("验证码", "账号验证", "访问过于频繁", "访问受限", "安全验证")


class BrowserUnavailable(ValueError):
    pass


class PageStructureError(ValueError):
    pass


def safe_post_url(value):
    parts = urlsplit(value)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in (
            "xiaohongshu.com", "www.xiaohongshu.com"):
        raise ValueError("unexpected Xiaohongshu post URL")
    if not POST_ID.search(parts.path):
        raise ValueError("Xiaohongshu detail URL has no stable post ID")
    return urlunsplit(("https", "www.xiaohongshu.com", parts.path, "", ""))


def _json_request(url, method="GET", timeout=3):
    request = Request(url, method=method)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class _WebSocket:
    """Small RFC 6455 client sufficient for local Chrome CDP."""
    def __init__(self, url, timeout=20):
        parts = urlsplit(url)
        if parts.scheme != "ws" or parts.hostname not in ("127.0.0.1", "localhost"):
            raise BrowserUnavailable("Chrome debugging endpoint must be local")
        self.socket = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parts.path + (("?" + parts.query) if parts.query else "")
        request = ("GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n").format(
                       path, parts.hostname, parts.port or 80, key).encode("ascii")
        self.socket.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.socket.recv(4096)
            if len(response) > 65536:
                raise BrowserUnavailable("invalid Chrome WebSocket handshake")
        if not response.startswith(b"HTTP/1.1 101"):
            raise BrowserUnavailable("Chrome rejected WebSocket connection")

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass

    def _read_exact(self, size):
        value = b""
        while len(value) < size:
            chunk = self.socket.recv(size - len(value))
            if not chunk:
                raise BrowserUnavailable("Chrome debugging connection closed")
            value += chunk
        return value

    def send_text(self, value):
        payload = value.encode("utf-8")
        first = bytes([0x81])
        length = len(payload)
        if length < 126:
            header = first + bytes([0x80 | length])
        elif length < 65536:
            header = first + bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header = first + bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def receive_text(self):
        parts = []
        while True:
            first, second = self._read_exact(2)
            final, opcode = bool(first & 0x80), first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else None
            payload = self._read_exact(length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise BrowserUnavailable("Chrome debugging connection closed")
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode in (0x1, 0x0):
                parts.append(payload)
                if final:
                    return b"".join(parts).decode("utf-8")

    def _send_control(self, opcode, payload):
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)


class CDPPage:
    def __init__(self, websocket_url, timeout=20):
        self.ws = _WebSocket(websocket_url, timeout)
        self.sequence = 0

    def close(self):
        self.ws.close()

    def call(self, method, params=None):
        self.sequence += 1
        ident = self.sequence
        self.ws.send_text(json.dumps({"id": ident, "method": method,
                                      "params": params or {}}, separators=(",", ":")))
        while True:
            message = json.loads(self.ws.receive_text())
            if message.get("id") != ident:
                continue
            if "error" in message:
                raise PageStructureError(message["error"].get("message", "CDP error"))
            return message.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression,
                           "returnByValue": True, "awaitPromise": True})
        value = result.get("result", {})
        if value.get("subtype") == "error":
            raise PageStructureError(value.get("description", "page evaluation failed"))
        return value.get("value")

    def navigate(self, url):
        self.call("Page.navigate", {"url": url})


class ChromeRemoteBrowser:
    def __init__(self, root, port=9222, chrome_path=DEFAULT_CHROME, timeout=20):
        self.root = Path(root)
        self.port = int(port)
        self.chrome_path = chrome_path
        self.timeout = timeout
        self.profile = self.root / ".local" / "xiaohongshu" / "chrome-profile"

    @property
    def endpoint(self):
        return "http://127.0.0.1:{}".format(self.port)

    def launch_login(self):
        if not Path(self.chrome_path).is_file():
            raise BrowserUnavailable("Google Chrome is not installed at " + self.chrome_path)
        self.profile.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.profile), 0o700)
        if self._owns_port():
            page = self.page()
            try:
                page.navigate("https://www.xiaohongshu.com/explore")
                page.call("Page.bringToFront")
            finally:
                page.close()
            return {"status": "LOGIN_WINDOW_OPENED", "profile": str(self.profile),
                    "port": self.port, "existing_process": True}
        command = [self.chrome_path, "--remote-debugging-port={}".format(self.port),
                   "--user-data-dir={}".format(self.profile), "--no-first-run",
                   "--no-default-browser-check", "https://www.xiaohongshu.com/explore"]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BrowserUnavailable("LAND-DATA Chrome exited before its debugging port was ready")
            if self._owns_port():
                try:
                    _json_request(self.endpoint + "/json/version", timeout=1)
                except Exception:
                    pass
                else:
                    break
            time.sleep(0.2)
        else:
            raise BrowserUnavailable("LAND-DATA Chrome did not open its local debugging port")
        return {"status": "LOGIN_WINDOW_OPENED", "profile": str(self.profile),
                "port": self.port, "existing_process": False}

    def tabs(self):
        if not self._owns_port():
            raise BrowserUnavailable(
                "Chrome debugging port is not owned by the LAND-DATA profile; run xhs login")
        try:
            values = _json_request(self.endpoint + "/json/list")
        except Exception as exc:
            raise BrowserUnavailable("LAND-DATA Chrome is not running; run xhs login") from exc
        return [item for item in values if item.get("type") == "page"]

    def _owns_port(self):
        try:
            processes = subprocess.check_output(
                ["ps", "-ax", "-o", "command="], text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return False
        return ("--remote-debugging-port={}".format(self.port) in processes and
                "--user-data-dir={}".format(self.profile) in processes)

    def page(self):
        tabs = self.tabs()
        tab = next((item for item in tabs
                    if (urlsplit(item.get("url", "")).hostname or "").lower() in
                    ("xiaohongshu.com", "www.xiaohongshu.com")), None)
        if tab is None:
            encoded = quote("https://www.xiaohongshu.com/explore", safe="")
            tab = _json_request(self.endpoint + "/json/new?" + encoded, method="PUT")
        return CDPPage(tab["webSocketDebuggerUrl"], self.timeout)

    def status(self):
        try:
            page = self.page()
        except BrowserUnavailable:
            return {"browser": "STOPPED", "authenticated": False,
                    "next_action": "run xhs login"}
        try:
            state = page.evaluate(_STATUS_EXPRESSION)
        finally:
            page.close()
        return {"browser": "RUNNING", **(state or {})}

    def discover(self, query, posts, scrolls, pause_seconds):
        page = self.page()
        try:
            search_url = ("https://www.xiaohongshu.com/search_result?keyword=" +
                          quote(query, safe="") + "&source=web_explore_feed")
            page.navigate(search_url)
            self._wait(page, "location.pathname==='/search_result'")
            state = page.evaluate(_STATUS_EXPRESSION)
            if state and state.get("auth_required"):
                raise AuthRequired("AUTH_REQUIRED: xiaohongshu-browser")
            self._wait(page, _CARD_READY_EXPRESSION)
            found = {}
            for index in range(scrolls + 1):
                result = page.evaluate(_DISCOVERY_EXPRESSION) or {}
                if result.get("auth_required"):
                    raise AuthRequired("AUTH_REQUIRED: xiaohongshu-browser")
                for card in result.get("cards", []):
                    match = POST_ID.search(urlsplit(card.get("url", "")).path)
                    if match and match.group(1) not in found:
                        found[match.group(1)] = {"post_id": match.group(1),
                                                "url": safe_post_url(card["url"]),
                                                "_navigation_url": card["url"],
                                                "title": card.get("title")}
                        if len(found) >= posts:
                            break
                if len(found) >= posts or index == scrolls:
                    break
                page.evaluate("window.scrollBy(0, Math.max(700, window.innerHeight)); true")
                time.sleep(pause_seconds)
            return list(found.values())[:posts]
        finally:
            page.close()

    def capture(self, card, query, city, pause_seconds):
        page = self.page()
        try:
            page.navigate(card.get("_navigation_url") or card["url"])
            self._wait(page, _DETAIL_READY_EXPRESSION)
            previous = None
            stable = None
            for attempt in range(4):
                current = page.evaluate(_CAPTURE_EXPRESSION)
                if current and current.get("auth_required"):
                    raise AuthRequired("AUTH_REQUIRED: xiaohongshu-browser")
                if not current or current.get("post_id") != card["post_id"]:
                    raise PageStructureError("detail page did not expose the expected post")
                if previous and current.get("text") == previous.get("text"):
                    stable = current
                    break
                previous = current
                if attempt < 3:
                    time.sleep(pause_seconds)
            if stable is None:
                raise PageStructureError("description was not stable after four reads")
            if stable.get("truncated"):
                raise PageStructureError("description is still collapsed or truncated")
            stable.update(query=query, city=city, captured_at=utc_now(),
                          completeness="stable-rendered-description")
            stable["url"] = safe_post_url(stable["url"])
            return stable
        finally:
            page.close()

    def _wait(self, page, expression):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            value = page.evaluate(expression)
            if value:
                if isinstance(value, dict) and value.get("auth_required"):
                    raise AuthRequired("AUTH_REQUIRED: xiaohongshu-browser")
                return value
            time.sleep(0.4)
        state = page.evaluate(_STATUS_EXPRESSION)
        if state and state.get("auth_required"):
            raise AuthRequired("AUTH_REQUIRED: xiaohongshu-browser")
        raise PageStructureError("timed out waiting for Xiaohongshu page DOM")


_STATUS_EXPRESSION = """(()=>{const modal=document.querySelector('[class*=captcha],[class*=verify],[class*=risk],[class*=login-container],[class*=login-modal]');
 const text=(modal?.innerText||'').slice(0,2000);
 const blocker=Boolean(modal)&&['验证码','账号验证','访问过于频繁','访问受限','安全验证'].some(x=>text.includes(x));
 const login=Boolean(document.querySelector('[class*=login-container],[class*=login-modal]'));
 return {url:location.origin+location.pathname,authenticated:!(blocker||login),
 auth_required:blocker||login};})()"""

_CARD_READY_EXPRESSION = """(()=>{const m=document.querySelector('[class*=captcha],[class*=verify],[class*=risk],[class*=login-container],[class*=login-modal]');const t=(m?.innerText||'').slice(0,2000);
 if(['验证码','账号验证','访问过于频繁','访问受限','安全验证'].some(x=>t.includes(x)))
   return {auth_required:true};
 if(location.pathname!=='/search_result')return false;
 return document.querySelectorAll('a.cover[href*="/search_result/"],a.cover[href*="/explore/"]').length>0;
})()"""

_DISCOVERY_EXPRESSION = """(()=>{const m=document.querySelector('[class*=captcha],[class*=verify],[class*=risk],[class*=login-container],[class*=login-modal]');const text=(m?.innerText||'').slice(0,2000);
 const auth_required=['验证码','账号验证','访问过于频繁','访问受限','安全验证'].some(x=>text.includes(x));
 const cards=Array.from(document.querySelectorAll('a.cover[href*="/search_result/"],a.cover[href*="/explore/"]'))
 .map(a=>({url:a.href,title:a.parentElement?.querySelector('a.title')?.textContent?.trim()||null}));
 return {auth_required,cards};})()"""

_DETAIL_READY_EXPRESSION = """(()=>{const m=document.querySelector('[class*=captcha],[class*=verify],[class*=risk],[class*=login-container],[class*=login-modal]');const t=(m?.innerText||'').slice(0,2000);
 if(['验证码','账号验证','访问过于频繁','访问受限','安全验证'].some(x=>t.includes(x)))
   return {auth_required:true};
 return Boolean(document.querySelector('#noteContainer #detail-desc'));
})()"""

_CAPTURE_EXPRESSION = r"""(()=>{const root=document.querySelector('#noteContainer');
 const body=root?.querySelector('#detail-desc');const title=root?.querySelector('#detail-title');
 const author=root?.querySelector(':scope > .author .author-wrapper a.name,:scope > .author a.name');
 const modal=document.querySelector('[class*=captcha],[class*=verify],[class*=risk],[class*=login-container],[class*=login-modal]');const text=(modal?.innerText||'').slice(0,2000);
 const auth_required=['验证码','账号验证','访问过于频繁','访问受限','安全验证'].some(x=>text.includes(x));
 if(auth_required)return {auth_required:true};if(!root||!body||!title||!author)return null;
 const match=location.pathname.match(/\/(?:explore|search_result|discovery\/item)\/([0-9a-f]{24})(?:\/|$)/i);
 const style=getComputedStyle(body);const expansion=Array.from(body.parentElement.querySelectorAll('button,a,span'))
   .some(e=>/展开|全文|more/i.test((e.textContent||'').trim())&&e.getBoundingClientRect().height>0);
 const media=Array.from(root.querySelectorAll(':scope > .media-container img,:scope > .media-container video'))
   .map(e=>({type:e.tagName==='VIDEO'?'video':'image'}));
 return {post_id:match?.[1]||null,url:location.href,title:title.textContent?.trim()||'',
 text:body.innerText??'',author:author.textContent?.trim()||null,
 publish_label:body.parentElement.querySelector('.date')?.textContent?.trim()||null,
 hashtags:Array.from(body.querySelectorAll('a.tag')).map(x=>x.textContent?.trim()).filter(Boolean),
 media_metadata:media,truncated:expansion||(!['none','0',''].includes(style.webkitLineClamp||''))};})()"""


def _ensure_schema(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS xhs_collection_runs (
      id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
      city TEXT NOT NULL, query_count INTEGER NOT NULL,
      new_count INTEGER NOT NULL, updated_count INTEGER NOT NULL,
      duplicate_count INTEGER NOT NULL, status TEXT NOT NULL,
      next_run_status TEXT NOT NULL, failure_reason TEXT, data TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS xhs_query_checkpoints (
      id TEXT PRIMARY KEY, city TEXT NOT NULL, query TEXT NOT NULL,
      cards_json TEXT NOT NULL, next_index INTEGER NOT NULL,
      complete INTEGER NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(city,query)
    );
    CREATE TABLE IF NOT EXISTS xhs_captures (
      id TEXT PRIMARY KEY, post_id TEXT NOT NULL, content_hash TEXT NOT NULL,
      source_url TEXT NOT NULL, capture_path TEXT NOT NULL,
      captured_at TEXT NOT NULL, data TEXT NOT NULL,
      UNIQUE(post_id,content_hash)
    );
    CREATE INDEX IF NOT EXISTS idx_xhs_captures_post ON xhs_captures(post_id,captured_at);
    CREATE TABLE IF NOT EXISTS xhs_capture_attempts (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, query TEXT NOT NULL,
      post_id TEXT, status TEXT NOT NULL, reason TEXT,
      created_at TEXT NOT NULL, data TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS xhs_query_posts (
      id TEXT PRIMARY KEY, city TEXT NOT NULL, query TEXT NOT NULL,
      post_id TEXT NOT NULL, source_url TEXT NOT NULL, discovered_at TEXT NOT NULL,
      UNIQUE(city,query,post_id)
    );
    CREATE TRIGGER IF NOT EXISTS xhs_capture_attempts_no_update
      BEFORE UPDATE ON xhs_capture_attempts
      BEGIN SELECT RAISE(ABORT, 'xhs capture attempts are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS xhs_capture_attempts_no_delete
      BEFORE DELETE ON xhs_capture_attempts
      BEGIN SELECT RAISE(ABORT, 'xhs capture attempts are append-only'); END;
    """)
    connection.commit()


class XHSCaptureAdapter:
    """One captured item with stable Raw identity independent of run metadata."""
    name = SOURCE

    def __init__(self, item):
        self.item = item

    def collect(self, _city):
        yield self.item

    def raw_identity_payload(self, payload):
        value = dict(payload)
        metadata = dict(value.get("source_metadata", {}))
        metadata.pop("captured_at", None)
        metadata.pop("query", None)
        metadata.pop("query_group", None)
        # The site often renders relative labels such as “昨天” or “2天前”.
        # Keep the label as provenance, but do not treat its daily change as a
        # new version of the post.
        metadata.pop("publish_label", None)
        value["source_metadata"] = metadata
        return value


def capture_to_item(capture):
    return SourceItem(SOURCE, capture["post_id"], safe_post_url(capture["url"]), {
        "content_type": "POST", "title": capture.get("title", ""),
        "text": capture.get("text", ""), "author_identifier": capture.get("author"),
        "published_at": None, "hashtags": capture.get("hashtags", []),
        "media_metadata": capture.get("media_metadata", []), "language": "zh",
        "source_metadata": {"query": capture["query"],
                            "query_group": capture.get("query_group"),
                            "publish_label": capture.get("publish_label"),
                            "captured_at": capture["captured_at"],
                            "completeness": capture["completeness"]},
        "content_status": "ACTIVE", "match_known_pois": True,
    })


def _capture_hash(capture):
    material = {key: capture.get(key) for key in (
        "post_id", "title", "text", "author", "hashtags", "media_metadata")}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _save_capture(storage, root, capture):
    digest = _capture_hash(capture)
    existing = storage.connection.execute(
        "SELECT id FROM xhs_captures WHERE post_id=? AND content_hash=?",
        (capture["post_id"], digest)).fetchone()
    previous = storage.connection.execute(
        "SELECT content_hash FROM xhs_captures WHERE post_id=? ORDER BY captured_at DESC LIMIT 1",
        (capture["post_id"],)).fetchone()
    directory = Path(root) / ".local" / "xiaohongshu" / "captures" / capture["post_id"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    safe_capture = {key: capture.get(key) for key in (
        "post_id", "url", "title", "text", "author", "publish_label", "hashtags",
        "media_metadata", "query", "query_group", "city", "captured_at", "completeness")}
    safe_capture["url"] = safe_post_url(safe_capture["url"])
    data = json.dumps(safe_capture, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8") + b"\n"
    if not path.exists():
        temporary = path.with_suffix(".tmp-" + secrets.token_hex(6))
        temporary.write_bytes(data)
        os.replace(str(temporary), str(path))
    if not existing:
        storage.connection.execute("""INSERT INTO xhs_captures
          (id,post_id,content_hash,source_url,capture_path,captured_at,data)
          VALUES (?,?,?,?,?,?,?)""", (stable_id("xhs-capture", capture["post_id"], digest),
          capture["post_id"], digest, safe_post_url(capture["url"]), str(path),
          capture["captured_at"], json.dumps({"query": capture["query"]}, ensure_ascii=False)))
        storage.connection.commit()
    state = "duplicate" if existing else "updated" if previous else "new"
    return state, str(path)


@contextmanager
def _collector_lock(root):
    path = Path(root) / ".local" / "xiaohongshu" / "collector.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another xhs collection is already running") from exc
        yield
    finally:
        stream.close()


def _purge_expired_capture_files(root, retention_days=7):
    directory = Path(root) / ".local" / "xiaohongshu" / "captures"
    if not directory.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in directory.glob("*/*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            pass
    return removed


def collect_xhs(storage, root, city, queries, *, posts=20, scrolls=3,
                pause_seconds=1.5, refresh=False, browser=None,
                sink: Optional[Callable[[SourceItem], None]] = None,
                query_groups=None):
    if not queries or any(not isinstance(value, str) or not value.strip() for value in queries):
        raise ValueError("at least one non-empty Xiaohongshu query is required")
    if len(queries) > 50 or any(len(value) > 100 for value in queries):
        raise ValueError("xhs query bank is limited to 50 queries of at most 100 characters")
    if posts < 1 or posts > 50 or scrolls < 0 or scrolls > 5:
        raise ValueError("xhs limits are posts 1..50 and scrolls 0..5")
    if pause_seconds < 0.5 or pause_seconds > 10:
        raise ValueError("xhs pause-seconds must be between 0.5 and 10")
    _ensure_schema(storage.connection)
    browser = browser or ChromeRemoteBrowser(root)
    query_groups = query_groups or {}
    with _collector_lock(root):
        return _collect_xhs_locked(storage, root, city, queries, posts=posts, scrolls=scrolls,
                                   pause_seconds=pause_seconds, refresh=refresh,
                                   browser=browser, sink=sink, query_groups=query_groups)


def _collect_xhs_locked(storage, root, city, queries, *, posts, scrolls,
                        pause_seconds, refresh, browser, sink, query_groups):
    purged_capture_files = _purge_expired_capture_files(root)
    started = utc_now()
    stale_rows = storage.connection.execute(
        "SELECT id,data FROM xhs_collection_runs WHERE status='RUNNING'").fetchall()
    for stale in stale_rows:
        try:
            stale_data = json.loads(stale["data"])
        except (TypeError, ValueError):
            stale_data = {}
        stale_data.update(status="FAILED", next_run_status="RETRY_REQUIRED",
                          failure_reason="interrupted_before_completion")
        storage.connection.execute("""UPDATE xhs_collection_runs
          SET ended_at=?,status='FAILED',next_run_status='RETRY_REQUIRED',
              failure_reason='interrupted_before_completion',data=? WHERE id=?""",
          (started, json.dumps(stale_data, ensure_ascii=False, sort_keys=True), stale["id"]))
    storage.connection.commit()
    run_id = stable_id("xhs-run", started, city, list(queries))
    counts = {"new_count": 0, "updated_count": 0, "duplicate_count": 0}
    errors = []
    if sink is None:
        sink = lambda _item: None
    storage.connection.execute("""INSERT INTO xhs_collection_runs
      (id,started_at,ended_at,city,query_count,new_count,updated_count,duplicate_count,
       status,next_run_status,failure_reason,data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
      (run_id, started, None, city, len(queries), 0, 0, 0, "RUNNING",
       "CONTINUE_REQUIRED", None, "{}"))
    storage.connection.commit()
    status, next_status, failure = "SUCCEEDED", "COMPLETE", None
    try:
        for query in queries:
            key = stable_id("xhs-query", city, query)
            row = storage.connection.execute(
                "SELECT * FROM xhs_query_checkpoints WHERE id=?", (key,)).fetchone()
            if row and not row["complete"] and not refresh:
                snapshot = json.loads(row["cards_json"])
                if isinstance(snapshot, list):
                    cards, parameters = snapshot, None
                else:
                    cards, parameters = snapshot.get("cards", []), snapshot.get("parameters")
                wanted = {"posts": posts, "scrolls": scrolls}
                if parameters is not None and parameters != wanted:
                    raise ValueError(
                        "unfinished xhs query uses different limits; rerun with the old limits or --refresh")
                next_index = row["next_index"]
                fresh_cards = browser.discover(query, posts, scrolls, pause_seconds)
                navigation = {item["post_id"]: item.get("_navigation_url")
                              for item in fresh_cards if item.get("_navigation_url")}
                for card in cards[next_index:]:
                    if card.get("post_id") in navigation:
                        card["_navigation_url"] = navigation[card["post_id"]]
            else:
                cards = browser.discover(query, posts, scrolls, pause_seconds)
                for card in cards:
                    navigation_url = card.get("_navigation_url") or card["url"]
                    card["url"] = safe_post_url(card["url"])
                    card["_navigation_url"] = navigation_url
                next_index = 0
                discovered_at = utc_now()
                for card in cards:
                    storage.connection.execute("""INSERT INTO xhs_query_posts
                      (id,city,query,post_id,source_url,discovered_at) VALUES (?,?,?,?,?,?)
                      ON CONFLICT(city,query,post_id) DO UPDATE SET
                      source_url=excluded.source_url,discovered_at=excluded.discovered_at""",
                      (stable_id("xhs-query-post", city, query, card["post_id"]), city, query,
                       card["post_id"], safe_post_url(card["url"]), discovered_at))
                storage.connection.execute("""INSERT INTO xhs_query_checkpoints
                  (id,city,query,cards_json,next_index,complete,updated_at)
                  VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                  cards_json=excluded.cards_json,next_index=0,complete=0,updated_at=excluded.updated_at""",
                  (key, city, query, json.dumps({"cards": [
                      {k: v for k, v in card.items() if k != "_navigation_url"}
                      for card in cards], "parameters": {
                      "posts": posts, "scrolls": scrolls}}, ensure_ascii=False), 0, 0, utc_now()))
                storage.connection.commit()
            for index in range(next_index, len(cards)):
                card = cards[index]
                attempt_at = utc_now()
                try:
                    capture = browser.capture(card, query, city, pause_seconds)
                    capture["query_group"] = query_groups.get(query, "custom")
                    change, path = _save_capture(storage, root, capture)
                    sink(capture_to_item(capture))
                    counts[change + "_count"] += 1
                    attempt_status, reason = "SUCCEEDED", change
                except AuthRequired:
                    attempt_status, reason = "AUTH_REQUIRED", "normal_user_action_required"
                    _save_capture_attempt(storage, run_id, query, card.get("post_id"),
                                          attempt_status, reason, attempt_at)
                    raise
                except Exception as exc:
                    attempt_status = "FAILED"
                    reason = type(exc).__name__ + ": " + str(exc)[:180]
                    _save_capture_attempt(storage, run_id, query, card.get("post_id"),
                                          attempt_status, reason, attempt_at)
                    raise
                _save_capture_attempt(storage, run_id, query, card.get("post_id"),
                                      attempt_status, reason, attempt_at)
                storage.connection.execute("""UPDATE xhs_query_checkpoints
                  SET next_index=?,updated_at=? WHERE id=?""", (index + 1, utc_now(), key))
                storage.connection.commit()
            storage.connection.execute("""UPDATE xhs_query_checkpoints
              SET complete=1,updated_at=? WHERE id=?""", (utc_now(), key))
            storage.connection.commit()
    except AuthRequired as exc:
        status, next_status, failure = "AUTH_REQUIRED", "RETRY_REQUIRED", str(exc)
    except Exception as exc:
        status, next_status = "FAILED", "RETRY_REQUIRED"
        failure = type(exc).__name__ + ": " + str(exc)[:240]
        errors.append(failure)
    ended = utc_now()
    payload = {"run_id": run_id, "status": status, "next_run_status": next_status,
               "city": city, "query_count": len(queries), **counts,
               "failure_reason": failure, "errors": errors, "llm_calls": 0,
               "purged_capture_files": purged_capture_files}
    storage.connection.execute("""UPDATE xhs_collection_runs SET ended_at=?,new_count=?,
      updated_count=?,duplicate_count=?,status=?,next_run_status=?,failure_reason=?,data=?
      WHERE id=?""", (ended, counts["new_count"], counts["updated_count"],
      counts["duplicate_count"], status, next_status, failure,
      json.dumps(payload, ensure_ascii=False, sort_keys=True), run_id))
    storage.connection.commit()
    return payload


def _save_capture_attempt(storage, run_id, query, post_id, status, reason, created_at):
    ident = stable_id("xhs-capture-attempt", run_id, query, post_id, created_at, status)
    storage.connection.execute("""INSERT INTO xhs_capture_attempts
      (id,run_id,query,post_id,status,reason,created_at,data)
      VALUES (?,?,?,?,?,?,?,?)""", (ident, run_id, query, post_id, status, reason,
      created_at, "{}"))
    storage.connection.commit()


def collection_runs(storage, limit=20):
    _ensure_schema(storage.connection)
    rows = storage.connection.execute(
        "SELECT data FROM xhs_collection_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [json.loads(row["data"]) for row in rows]


def collection_status(storage):
    _ensure_schema(storage.connection)
    checkpoints = storage.connection.execute("""SELECT city,query,next_index,complete,updated_at
      FROM xhs_query_checkpoints ORDER BY updated_at DESC""").fetchall()
    capture_rows = storage.connection.execute("SELECT capture_path FROM xhs_captures").fetchall()
    relationships = storage.connection.execute("SELECT COUNT(*) FROM xhs_query_posts").fetchone()[0]
    return {"capture_versions_seen": len(capture_rows),
            "capture_files_retained": sum(Path(row["capture_path"]).is_file()
                                          for row in capture_rows),
            "query_post_relationships": relationships,
            "queries": [dict(row) for row in checkpoints]}
