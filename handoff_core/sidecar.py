"""Loopback sidecar for hermes-webui (stdlib only).

hermes-webui extensions cannot add backend routes; their backend is a loopback process reached through WebUI's
authenticated proxy at ``/api/extensions/handoff/sidecar/...``. That proxy buffers whole responses (<= 512 KiB,
~10 s) and cannot carry WebSockets, so the live screen travels as **long-polling over plain HTTP**:

    POST /api/rfb/open   {viewer_id}            -> {sid}
    POST /api/rfb/send?sid=...   <raw RFB bytes> -> {ok}            (client -> Xvnc, through RfbClientFilter)
    GET  /api/rfb/recv?sid=...&wait=4            -> raw RFB bytes   (Xvnc -> client; header X-Handoff-Closed: <code>)
    POST /api/rfb/close?sid=...

Because every byte goes through the proxy, no extra port, ticket, tunnel or CSP change is needed, and the stream is
exactly as authenticated as the rest of WebUI. The same lease gate as the dashboard bridge applies: key / pointer /
clipboard / resize messages are dropped unless the lease says THIS viewer is the human holder.

Auth (token-v1): every route except /health requires ``X-Hermes-Sidecar-Token`` equal to the token WebUI minted in
``<state>/sidecar-auth/handoff.token`` (re-read per request): 401 on mismatch, 503 if the file is missing.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import PLUGIN_ID, __version__, compat, control, setup, state, tickets

log = logging.getLogger("handoff.sidecar")
PLUGIN_FILE = Path(__file__).resolve().parent.parent / "plugin.yaml"

DEFAULT_PORT = 17901
HEADER = "X-Hermes-Sidecar-Token"
MAX_SEND = 1 << 20          # request body ceiling per send
MAX_RECV = 400 * 1024       # < the proxy's 512 KiB response cap
MAX_WAIT = 5.0              # < the proxy's ~10 s upstream timeout
IDLE_CLOSE = 30.0           # a session nobody polls for this long is closed
MAX_SESSIONS = 8
BUFFER_CAP = 8 << 20        # stop reading Xvnc when the client is this far behind (backpressure)
CLOSE_EVICTED, CLOSE_SCREEN_OFF, CLOSE_BAD_FRAMING, CLOSE_IDLE, CLOSE_CLIENT = 4000, 4001, 1008, 4408, 1000


# ── token ─────────────────────────────────────────────────────────────────────
def token_path() -> Path:
    explicit = os.environ.get("HERMES_EXT_SIDECAR_TOKEN_FILE")
    if explicit:
        return Path(explicit).expanduser()
    st = os.environ.get("HERMES_WEBUI_STATE_DIR")
    if st:
        return Path(st).expanduser() / "sidecar-auth" / f"{PLUGIN_ID}.token"
    home = os.environ.get("HERMES_HOME")
    if home and (Path(home) / "webui" / "sidecar-auth" / f"{PLUGIN_ID}.token").exists():
        return Path(home) / "webui" / "sidecar-auth" / f"{PLUGIN_ID}.token"
    return Path("~/.hermes/webui/sidecar-auth").expanduser() / f"{PLUGIN_ID}.token"


def check_token(presented: Optional[str]) -> int:
    try:
        expected = token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return 503
    if not expected:
        return 503
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        return 401
    return 200


def home_id(home) -> str:
    """Short hash that identifies which Hermes home a sidecar serves (no paths on an unauthenticated route)."""
    import hashlib
    return hashlib.sha256(str(Path(str(home)).resolve()).encode()).hexdigest()[:10]


# ── RFB sessions ──────────────────────────────────────────────────────────────
class Session:
    def __init__(self, home: str, viewer_id: str, upstream, relay=None):
        self.sid = tickets.secrets.token_urlsafe(18)
        self.home, self.viewer_id = home, viewer_id
        self.short = tickets.short(viewer_id)
        self.up, self.relay = upstream, relay
        bd = compat.load()
        self.bd = bd
        self.cv = threading.Condition()
        self.buf = bytearray()
        self.close_code: Optional[int] = None
        self.last_poll = time.monotonic()
        self.had_control = False
        self._lease, self._lease_at = None, 0.0
        self.filter = bd.rfb_filter.RfbClientFilter(self.allow_input)
        self.send_lock = threading.Lock()
        threading.Thread(target=self._reader, name=f"handoff-rfb-{self.short}", daemon=True).start()

    # lease -----------------------------------------------------------------
    def _read_lease(self):
        now = time.monotonic()
        if self._lease is None or now - self._lease_at >= 0.25:     # at most every 0.25 s, like core's bridge
            self._lease = self.bd.lease.get(profile_key=self.home)
            self._lease_at = now
            if self._holds(self._lease):
                self.had_control = True
        return self._lease

    def _holds(self, lease) -> bool:
        return lease.holder == self.bd.lease.HUMAN and lease.viewer_id == self.viewer_id

    def allow_input(self) -> bool:
        return self._holds(self._read_lease())

    def evicted(self) -> bool:
        lease = self._read_lease()
        if lease.holder != self.bd.lease.HUMAN:
            self.had_control = False           # a plain hand-back to the agent is not an eviction
            return False
        return self.had_control and lease.viewer_id != self.viewer_id

    # io --------------------------------------------------------------------
    def _reader(self) -> None:
        try:
            while True:
                with self.cv:
                    while len(self.buf) > BUFFER_CAP and self.close_code is None:
                        self.cv.wait(0.5)
                    if self.close_code is not None:
                        return
                data = self._read_upstream()
                if not data:
                    self.close(CLOSE_SCREEN_OFF)
                    return
                with self.cv:
                    self.buf += data
                    self.cv.notify_all()
        except Exception:  # noqa: BLE001
            self.close(CLOSE_SCREEN_OFF)

    def _read_upstream(self) -> bytes:
        if self.relay is not None:
            return self.relay.stdout.read1(65536) if hasattr(self.relay.stdout, "read1") else self.relay.stdout.read(65536)
        return self.up.recv(65536)

    def write(self, data: bytes) -> None:
        out = self.filter.feed(data)           # ValueError on unframeable bytes
        log.debug("viewer %s: send in=%d forwarded=%d input_allowed=%s", self.short, len(data), len(out), self.allow_input())
        if not out:
            return
        with self.send_lock:
            if self.relay is not None:
                self.relay.stdin.write(out)
                self.relay.stdin.flush()
            else:
                self.up.sendall(out)

    def recv(self, wait: float) -> Tuple[bytes, Optional[int]]:
        self.last_poll = time.monotonic()
        end = time.monotonic() + wait
        with self.cv:
            # Checked on EVERY poll (not only while idle): a busy stream must still notice that control moved, and
            # this is also what records that this viewer ever held it (the eviction test needs that history).
            if self.close_code is None and self.evicted():
                self.close_locked(CLOSE_EVICTED)
            while not self.buf and self.close_code is None:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cv.wait(min(left, 0.25))
                if self.evicted():
                    self.close_locked(CLOSE_EVICTED)
            chunk = bytes(self.buf[:MAX_RECV])
            del self.buf[:MAX_RECV]
            self.cv.notify_all()
            return chunk, (self.close_code if not self.buf else None)

    def close(self, code: int) -> None:
        with self.cv:
            self.close_locked(code)

    def close_locked(self, code: int) -> None:
        if self.close_code is not None:
            return
        self.close_code = code
        self.cv.notify_all()
        try:
            if self.relay is not None:
                self.relay.terminate()
            else:
                self.up.shutdown(socket.SHUT_RDWR)
                self.up.close()
        except Exception:  # noqa: BLE001
            pass


class Sessions:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: Dict[str, Session] = {}
        threading.Thread(target=self._reap, name="handoff-reaper", daemon=True).start()

    def open(self, home: str, viewer_id: str) -> Session:
        bd = compat.load()
        relay, sock = None, None
        with compat.home_scope(home):
            if bd.runtime.sandbox_screen_running():
                relay = bd.runtime.open_rfb_stream()
            else:
                path = Path(home) / "bot-desktop" / "rfb.sock"
                if not path.exists():
                    raise FileNotFoundError("screen is not running")
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(None)
                sock.connect(str(path))
        with self.lock:
            if len(self.items) >= MAX_SESSIONS:
                for s in sorted(self.items.values(), key=lambda s: s.last_poll)[:1]:
                    s.close(CLOSE_IDLE)
                    self.items.pop(s.sid, None)
            s = Session(home, viewer_id, sock, relay)
            self.items[s.sid] = s
            return s

    def get(self, sid: str) -> Optional[Session]:
        with self.lock:
            return self.items.get(sid)

    def drop(self, sid: str, code: int = CLOSE_CLIENT) -> None:
        with self.lock:
            s = self.items.pop(sid, None)
        if s:
            s.close(code)

    def _reap(self) -> None:
        touched = 0.0
        while True:
            time.sleep(2.0)
            if not PLUGIN_FILE.exists():       # `hermes plugins remove handoff` deleted us: do not linger as an orphan
                log.info("plugin removed; removing the WebUI extension and exiting")
                try:
                    sys.modules[__package__ + ".webui"].orphan_cleanup()
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not remove the WebUI extension: %s", type(exc).__name__)
                for s in list(self.items.values()):
                    s.close(CLOSE_SCREEN_OFF)
                os._exit(0)
            now = time.monotonic()
            with self.lock:
                dead = [s for s in self.items.values() if now - s.last_poll > IDLE_CLOSE]
                live = [s for s in self.items.values() if s not in dead]
            for s in dead:
                self.drop(s.sid, CLOSE_IDLE)
            if live and now - touched > 30:          # a watched screen is not idle: keep Bot Desktop's auto-stop away
                touched = now
                for h in {s.home for s in live}:
                    try:
                        (Path(h) / "bot-desktop" / "activity").touch()
                    except OSError:
                        pass


# ── http ──────────────────────────────────────────────────────────────────────
class App:
    def __init__(self) -> None:
        self.home = compat.current_home()
        self.home_id = home_id(self.home)
        self.sessions = Sessions()
        compat.load()

    def viewer(self, body: dict) -> Optional[str]:
        v = (body or {}).get("viewer_id")
        return v if tickets.valid_viewer_id(self.home, v) else None

    def route(self, method: str, path: str, query: Dict[str, list], body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        def jr(obj: Any, status: int = 200):
            return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()

        def payload() -> dict:
            try:
                d = json.loads(body.decode() or "{}")
                return d if isinstance(d, dict) else {}
            except ValueError:
                return {}

        home = self.home
        if path == "/api/status" and method in ("GET", "POST"):
            return jr(control.status(home, self.viewer(payload())))
        if path == "/api/pulse" and method in ("GET", "POST"):
            return jr(control.pulse(home, self.viewer(payload())))
        if method != "POST" and not (path == "/api/rfb/recv" and method == "GET"):
            return jr({"ok": False, "error": "not_found"}, 404)
        if path == "/api/setup":
            if payload().get("auto"):
                return jr({"ok": True, "setup": setup.maybe_auto_setup(home)})
            setup.start_async(home)
            return jr({"ok": True, "setup": setup.setup_state(home)})
        if path == "/api/start":
            if not setup.readiness(home)["ready"]:
                return jr({"ok": False, "error": "not_ready"})
            control.start_screen_async(home)
            return jr({"ok": True})
        if path == "/api/viewer":
            return jr({"ok": True, "viewer_id": self.viewer(payload()) or tickets.new_viewer_id(home)})
        if path == "/api/take-over":
            b = payload()
            vid = self.viewer(b)
            if not vid:
                return jr({"ok": False, "error": "unknown_viewer"})
            if not control.screen(home)["running"]:
                return jr({"ok": False, "error": "screen_off"})
            return jr({"ok": True, "lease": control.take_over(home, vid, str(b.get("reason") or ""))})
        if path == "/api/hand-back":
            vid = self.viewer(payload())
            if not vid:
                return jr({"ok": False, "error": "unknown_viewer"})
            return jr({"ok": True, "lease": control.hand_back(home, vid)})
        if path == "/api/force-hand-back":
            return jr({"ok": True, "lease": control.force_hand_back(home)})
        if path == "/api/dismiss":
            control.dismiss_request(home)
            return jr({"ok": True})

        # the stream ----------------------------------------------------------
        if path == "/api/rfb/open":
            vid = self.viewer(payload())
            if not vid:
                return jr({"ok": False, "error": "unknown_viewer"})
            try:
                s = self.sessions.open(home, vid)
            except (FileNotFoundError, OSError):
                return jr({"ok": False, "error": "screen_off"})
            return jr({"ok": True, "sid": s.sid})
        sid = (query.get("sid") or [""])[0]
        s = self.sessions.get(sid)
        if path in ("/api/rfb/send", "/api/rfb/recv", "/api/rfb/close"):
            if s is None:
                return 404, {"X-Handoff-Closed": str(CLOSE_IDLE)}, b""
        if path == "/api/rfb/send":
            if s.close_code is not None:
                return 410, {"X-Handoff-Closed": str(s.close_code)}, b""
            try:
                s.write(body)
            except ValueError:
                log.info("viewer %s: closed on bad RFB framing", s.short)
                self.sessions.drop(s.sid, CLOSE_BAD_FRAMING)
                return 410, {"X-Handoff-Closed": str(CLOSE_BAD_FRAMING)}, b""
            except OSError:
                self.sessions.drop(s.sid, CLOSE_SCREEN_OFF)
                return 410, {"X-Handoff-Closed": str(CLOSE_SCREEN_OFF)}, b""
            return 204, {}, b""
        if path == "/api/rfb/recv":
            try:
                wait = max(0.0, min(float((query.get("wait") or ["4"])[0]), MAX_WAIT))
            except ValueError:
                wait = 4.0
            data, closed = s.recv(wait)
            hdr = {"Content-Type": "application/octet-stream"}
            if closed is not None:
                hdr["X-Handoff-Closed"] = str(closed)
                if closed == CLOSE_EVICTED:
                    log.info("viewer %s: evicted (control taken)", s.short)
                self.sessions.drop(s.sid, closed)
            return 200, hdr, data
        if path == "/api/rfb/close":
            self.sessions.drop(s.sid, CLOSE_CLIENT)
            return 204, {}, b""
        return jr({"ok": False, "error": "not_found"}, 404)


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "handoff"

        def log_message(self, fmt, *args):          # never log URLs: they carry session ids
            pass

        def _run(self, method: str) -> None:
            u = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_SEND:
                self.send_error(400 if length < 0 else 413)
                self.close_connection = True
                return
            body = self.rfile.read(length) if length else b""
            try:
                if u.path == "/health":
                    st, hdr, out = 200, {"Content-Type": "application/json"}, json.dumps(
                        {"ok": True, "service": PLUGIN_ID, "version": __version__, "pid": os.getpid(), "home_id": app.home_id}).encode()
                else:
                    code = check_token(self.headers.get(HEADER))
                    if code != 200:
                        st, hdr, out = code, {"Content-Type": "application/json"}, json.dumps(
                            {"ok": False, "error": "unauthorized" if code == 401 else "token_unavailable"}).encode()
                    else:
                        st, hdr, out = app.route(method, u.path, parse_qs(u.query), body)
            except compat.Incompatible as exc:
                st, hdr, out = 200, {"Content-Type": "application/json"}, json.dumps(
                    {"ok": False, "error": "incompatible", "message": str(exc)}).encode()
            except Exception as exc:  # noqa: BLE001
                log.warning("route %s failed: %s", u.path, type(exc).__name__)
                st, hdr, out = 500, {"Content-Type": "application/json"}, json.dumps(
                    {"ok": False, "error": "internal"}).encode()
            self.send_response(st)
            for k, v in hdr.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(out)

        def do_GET(self): self._run("GET")
        def do_POST(self): self._run("POST")
        def do_DELETE(self): self._run("DELETE")
        def do_HEAD(self): self._run("HEAD")

    return H


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def serve(port: int = DEFAULT_PORT) -> Server:
    from . import webui  # noqa: F401 - import now: after `plugins remove` the file is gone and the exit cleanup needs it
    app = App()
    srv = Server(("127.0.0.1", port), make_handler(app))
    srv.app = app
    return srv


def main() -> int:
    logging.basicConfig(level=getattr(logging, os.environ.get("HANDOFF_LOG", "INFO").upper(), logging.INFO), format="%(asctime)s %(name)s %(message)s")
    port = int(os.environ.get("HANDOFF_PORT") or DEFAULT_PORT)
    try:
        srv = serve(port)
    except OSError as exc:
        print(f"handoff sidecar: cannot bind 127.0.0.1:{port}: {exc}", file=sys.stderr)
        return 1
    log.info("sidecar %s on 127.0.0.1:%s home=%s", __version__, port, srv.app.home)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
