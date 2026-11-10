"""Tests for Takeover's core, the dashboard routes and the agent tool.

Real hermes-agent lease + RfbClientFilter against a throwaway HERMES_HOME, real uvicorn, real WebSocket clients.
Only the screen itself is faked (a unix socket recording the bytes the bridge lets through), so this needs no Xvnc.
Run with the Hermes venv python:   python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = os.path.expanduser(os.environ.get("TAKEOVER_AGENT_DIR", "~/.hermes/hermes-agent"))
sys.path.insert(0, AGENT)
TMP = tempfile.mkdtemp(prefix="takeover-test-")
HOME = os.path.join(TMP, "home")
os.makedirs(HOME)
os.environ["HERMES_HOME"] = HOME
# Never let a test reach the REAL service on the default port (stop_sidecar / ensure_sidecar act on whatever answers there):
# pin the Takeover port to a private, currently free one for this process.
import socket as _s
with _s.socket() as _sk:
    _sk.bind(("127.0.0.1", 0)); os.environ["TAKEOVER_PORT"] = str(_sk.getsockname()[1])

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.sync.client import connect  # noqa: E402


def _load(name, path, **kw):
    spec = importlib.util.spec_from_file_location(name, path, **kw)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("hermes_takeover_core", ROOT / "takeover_core" / "__init__.py", submodule_search_locations=[str(ROOT / "takeover_core")])
api = _load("hermes_dashboard_plugin_takeover", ROOT / "dashboard" / "plugin_api.py")
REAL_ORIGIN_OK = api._origin_ok          # captured once: test classes below swap in a loose stub for the HTTP tests
plugin_init = _load("takeover_plugin_init", ROOT / "__init__.py")

BD = core.compat.load()
core.webui.state_dir = lambda: None        # unit tests must never install into / spawn a service for a real hermes-webui
KEY = bytes([4, 1, 0, 0, 0, 0, 0, 0x61])             # KeyEvent down 'a'
PTR = bytes([5, 1, 0, 10, 0, 10])                    # PointerEvent
FBUR = bytes([3, 1, 0, 0, 0, 0, 0, 10, 0, 10])       # FramebufferUpdateRequest (never input)
HS = b"RFB 003.008\n" + b"\x01" + b"\x01"            # version, security choice, ClientInit


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeScreen:
    def __init__(self, path: str):
        self.path, self.received = path, bytearray()
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(5)

    def _run(self):
        async def handle(r, w):
            w.write(b"RFB 003.008\n")
            await w.drain()
            while True:
                d = await r.read(4096)
                if not d:
                    break
                self.received += d
            w.close()

        async def main():
            await asyncio.start_unix_server(handle, self.path)
            self._ready.set()
            await asyncio.Event().wait()
        asyncio.run(main())

    def after_handshake(self) -> bytes:
        return bytes(self.received[len(HS):])


class Base(unittest.TestCase):
    running = True

    @classmethod
    def setUpClass(cls):
        os.makedirs(os.path.join(HOME, "bot-desktop"), exist_ok=True)
        cls.sock = os.path.join(HOME, "bot-desktop", "rfb.sock")
        if os.path.exists(cls.sock):
            os.unlink(cls.sock)
        cls.screen = FakeScreen(cls.sock)
        rt = BD.runtime

        class St:           # what runtime.status() returns (a dataclass in Hermes)
            pass
        rt.status = lambda profile=None: type("S", (), {"running": cls.running, "geometry": "1440x900", "blocker": None,
                                                         "placement": "gateway", "installed": True})()
        rt.sandbox_screen_running = lambda: False
        cls.real_status = rt.status
        cls.real_origin_ok = staticmethod(REAL_ORIGIN_OK)
        # same-host Origin check (the real dashboard guard is exercised by tests/e2e)
        api._origin_ok = lambda ws: (ws.headers.get("origin") or "").split("//")[-1] == ws.headers.get("host")
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/takeover")
        cls.port = free_port()
        cls.logs = []

        class H(logging.Handler):
            def emit(h, rec):
                cls.logs.append(rec.getMessage())
        for n in ("takeover", "takeover.bridge", "uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(n).addHandler(H())
        logging.getLogger("takeover").setLevel(logging.INFO)
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, access_log=False,
                                                   log_level="warning", ws="websockets", lifespan="off"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        for _ in range(100):
            try:
                cls.post("/pulse", {})
                break
            except Exception:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def setUp(self):
        type(self).running = True
        BD.lease.release()
        self.screen.received.clear()

    @classmethod
    def post(cls, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/plugins/takeover{path}", method="POST",
                                     data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def viewer_ticket(self, viewer=None):
        j = self.post("/ticket", {"viewer_id": viewer} if viewer else {})
        self.assertTrue(j["ok"], j)
        return j["viewer_id"], j["ticket"]

    def ws(self, ticket, origin=None):
        return connect(f"ws://127.0.0.1:{self.port}/api/plugins/takeover/stream?ticket={ticket}",
                       origin=origin or f"http://127.0.0.1:{self.port}", subprotocols=["binary"], open_timeout=5)

    @staticmethod
    def close_code(ws, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            try:
                ws.recv(timeout=0.5)
            except TimeoutError:
                continue
            except ConnectionClosed as e:
                return e.rcvd.code if e.rcvd else 1006
        return None

    @staticmethod
    def settle(t=0.4):
        time.sleep(t)


class TestTicketsAndOrigin(Base):
    def test_ticket_is_single_use(self):
        _, t = self.viewer_ticket()
        ws = self.ws(t)
        ws.recv(timeout=2)
        ws.close()
        with self.ws(t) as ws2:
            self.assertEqual(self.close_code(ws2), core.bridge.CLOSE_BAD_TICKET)

    def test_expired_and_garbage_tickets_refused(self):
        _, t = self.viewer_ticket()
        with core.tickets._lock:
            vid, home, _ = core.tickets._tickets[t]
            core.tickets._tickets[t] = (vid, home, time.monotonic() - 1)
        with self.ws(t) as ws:
            self.assertEqual(self.close_code(ws), core.bridge.CLOSE_BAD_TICKET)
        with self.ws("nope") as ws:
            self.assertEqual(self.close_code(ws), core.bridge.CLOSE_BAD_TICKET)

    def test_foreign_origin_refused_even_with_valid_ticket(self):
        _, t = self.viewer_ticket()
        with self.ws(t, origin="http://evil.example") as ws:
            self.assertEqual(self.close_code(ws), core.bridge.CLOSE_BAD_ORIGIN)

    def test_forged_viewer_id_is_ignored_and_cannot_take_over(self):
        self.assertEqual(self.post("/take-over", {"viewer_id": "aa.bb"})["error"], "unknown_viewer")
        self.assertEqual(self.post("/hand-back", {"viewer_id": "aa.bb"})["error"], "unknown_viewer")
        j = self.post("/ticket", {"viewer_id": "aa.bb"})
        self.assertTrue(j["ok"])
        self.assertNotEqual(j["viewer_id"], "aa.bb")

    def test_viewer_ids_survive_a_restart(self):
        vid = core.tickets.new_viewer_id(HOME)
        self.assertTrue(core.tickets.valid_viewer_id(HOME, vid))
        self.assertFalse(core.tickets.valid_viewer_id(os.path.join(TMP, "other-home"), vid))   # per-home secret

    def test_screen_off(self):
        type(self).running = False
        j = self.post("/ticket", {})
        self.assertEqual((j["ok"], j["error"]), (False, "screen_off"))
        self.assertEqual(self.post("/take-over", {"viewer_id": j["viewer_id"]})["error"], "screen_off")

    def test_origin_guard_is_strict_same_origin_without_private_internals(self):
        class W:
            def __init__(s, h): s.headers = h
        ok = self.real_origin_ok
        self.assertTrue(ok(W({"origin": "https://h.example", "host": "h.example"})))
        self.assertTrue(ok(W({"origin": "http://127.0.0.1:9119", "host": "127.0.0.1:9119"})))
        self.assertTrue(ok(W({"origin": "http://H.Example:80", "host": "h.example:80"})))
        for bad in ({"origin": "https://evil.example", "host": "h.example"}, {"host": "h.example"}, {"origin": "https://h.example"},
                    {"origin": "null", "host": "h.example"}, {"origin": "file://h.example", "host": "h.example"},
                    {"origin": "http://127.0.0.1:9119", "host": "127.0.0.1:9120"}, {"origin": "http://h.example.evil", "host": "h.example"}):
            self.assertFalse(ok(W(bad)), bad)
        import inspect
        src = inspect.getsource(api)
        self.assertNotIn("_ws_request_is_allowed", src)


class TestInputGate(Base):
    def test_input_only_flows_while_this_viewer_holds_the_lease(self):
        vid, t = self.viewer_ticket()
        with self.ws(t) as ws:
            ws.send(HS)
            ws.recv(timeout=2)
            ws.send(FBUR); ws.send(KEY); ws.send(PTR)               # watching: input dropped
            self.settle()
            self.assertEqual(self.screen.after_handshake(), FBUR)
            self.assertTrue(self.post("/take-over", {"viewer_id": vid})["ok"])
            self.settle(0.4)                                        # > the 0.25 s lease re-read
            ws.send(KEY); ws.send(PTR)
            self.settle()
            self.assertEqual(self.screen.after_handshake(), FBUR + KEY + PTR)
            self.post("/hand-back", {"viewer_id": vid})
            self.settle(0.4)
            ws.send(KEY)
            self.settle()
            self.assertEqual(self.screen.after_handshake(), FBUR + KEY + PTR)    # dropped again

    def test_agent_is_fenced_and_lease_does_not_leak_the_viewer_id(self):
        vid, _ = self.viewer_ticket()
        self.post("/take-over", {"viewer_id": vid, "reason": "log in"})
        with self.assertRaises(BD.lease.HumanHasControl):
            BD.lease.assert_agent_may_act()
        st = self.post("/status", {"viewer_id": vid})
        self.assertTrue(st["lease"]["mine"])
        self.assertEqual(st["lease"]["reason"], "log in")
        other = self.post("/status", {})
        self.assertFalse(other["lease"]["mine"])
        self.assertNotIn(vid, json.dumps(other))
        self.post("/hand-back", {"viewer_id": vid})
        BD.lease.assert_agent_may_act()

    def test_stale_tab_cannot_release_newer_holder(self):
        a, _ = self.viewer_ticket(); b, _ = self.viewer_ticket()
        self.post("/take-over", {"viewer_id": a}); self.post("/take-over", {"viewer_id": b})
        self.post("/hand-back", {"viewer_id": a})
        self.assertEqual(BD.lease.get().viewer_id, b)
        self.post("/force-hand-back")
        self.assertEqual(BD.lease.get().holder, BD.lease.AGENT)

    def test_closed_tab_leaves_agent_locked_until_force_hand_back(self):
        vid, t = self.viewer_ticket()
        with self.ws(t) as ws:
            ws.recv(timeout=2)
            self.post("/take-over", {"viewer_id": vid})
        self.settle()
        self.assertEqual(BD.lease.get().holder, BD.lease.HUMAN)
        self.post("/force-hand-back")
        self.assertEqual(BD.lease.get().holder, BD.lease.AGENT)

    def test_second_device_evicts_first(self):
        a, ta = self.viewer_ticket(); b, tb = self.viewer_ticket()
        with self.ws(ta) as wa, self.ws(tb) as wb:
            wa.recv(timeout=2); wb.recv(timeout=2)
            self.post("/take-over", {"viewer_id": a})
            self.settle(0.6)
            self.post("/take-over", {"viewer_id": b})
            self.assertEqual(self.close_code(wa), core.bridge.CLOSE_EVICTED)
            wb.send(HS)
            self.settle(0.4)
            wb.send(KEY)
            self.settle()
            self.assertIn(KEY, self.screen.after_handshake())

    def test_hand_back_to_the_agent_is_not_an_eviction(self):
        a, ta = self.viewer_ticket()
        with self.ws(ta) as wa:
            wa.recv(timeout=2)
            self.post("/take-over", {"viewer_id": a}); self.settle(0.6)
            self.post("/hand-back", {"viewer_id": a})
            self.assertIsNone(self.close_code(wa, timeout=1.5))      # still connected as a watcher

    def test_screen_stop_closes_stream_with_4001(self):
        _, t = self.viewer_ticket()
        try:
            os.rename(self.sock, self.sock + ".gone")
            with self.ws(t) as ws:
                self.assertEqual(self.close_code(ws), core.bridge.CLOSE_SCREEN_OFF)
        finally:
            os.rename(self.sock + ".gone", self.sock)

    def test_bad_rfb_framing_drops_the_viewer(self):
        _, t = self.viewer_ticket()
        with self.ws(t) as ws:
            ws.send(HS + bytes([99]))
            self.assertEqual(self.close_code(ws), 1008)


class TestRequestsAndLogs(Base):
    def test_request_lifecycle_via_routes(self):
        core.state.create_request(HOME, "Enter the code")
        self.assertEqual(self.post("/pulse", {})["request"]["status"], "pending")
        vid, _ = self.viewer_ticket()
        self.post("/take-over", {"viewer_id": vid})
        self.assertEqual(self.post("/pulse", {"viewer_id": vid})["request"]["status"], "taken")
        self.assertIn("Enter the code", BD.lease.get().reason)        # the ask stays on screen while the human acts
        self.post("/hand-back", {"viewer_id": vid})
        self.assertIsNone(self.post("/pulse", {})["request"])
        self.assertEqual(core.state.get_request(HOME)["status"], "done")

    def test_dismiss_and_expiry(self):
        core.state.create_request(HOME, "x")
        self.post("/dismiss")
        self.assertIsNone(self.post("/pulse", {})["request"])
        r = core.state.create_request(HOME, "old")
        core.state.update_request(HOME, r["id"], created=time.time() - core.state.REQUEST_TTL - 5)
        self.assertIsNone(self.post("/pulse", {})["request"])

    def test_no_tickets_or_viewer_ids_in_logs(self):
        self.logs.clear()
        vid, t = self.viewer_ticket()
        with self.ws(t) as ws:
            ws.recv(timeout=2)
            self.post("/take-over", {"viewer_id": vid}); self.post("/hand-back", {"viewer_id": vid})
        self.settle()
        blob = "\n".join(self.logs)
        self.assertIn("take-over", blob)
        for secret in (t, vid, vid.split(".")[0]):
            self.assertNotIn(secret, blob)


class TestTool(Base):
    def setUp(self):
        super().setUp()
        self._ready = core.setup.ensure_ready
        core.setup.ensure_ready = lambda home, timeout=0, **kw: {"ready": True, "headed": True}
        self._start = core.control.start_screen
        core.control.start_screen = lambda home: {"running": True}
        self._bc = core.surfaces.broadcast
        core.surfaces.broadcast = lambda event, payload: False       # no test talks to a real event bridge

    def tearDown(self):
        core.setup.ensure_ready = self._ready
        core.control.start_screen = self._start
        core.surfaces.broadcast = self._bc

    def call(self, **args):
        return json.loads(core.tool.handle(args))

    def human(self, take_after, hand_back_after=None):
        def run():
            time.sleep(take_after)
            BD.lease.acquire("viewer-x", profile_key=HOME)
            if hand_back_after is not None:
                time.sleep(hand_back_after)
                BD.lease.release(viewer_id="viewer-x", profile_key=HOME)
        threading.Thread(target=run, daemon=True).start()

    def test_reason_required(self):
        self.assertEqual(self.call()["status"], "error")

    def test_waits_until_the_human_hands_back(self):
        self.human(1.2, 1.5)
        t0 = time.time()
        out = self.call(reason="SMS code", wait_seconds=30)
        self.assertEqual(out["status"], "completed", out)
        self.assertGreater(time.time() - t0, 2.5)
        self.assertEqual(core.state.get_request(HOME)["status"], "done")

    def test_a_takeover_and_hand_back_between_two_polls_is_not_missed(self):
        def quick():
            time.sleep(1.3)
            BD.lease.acquire("viewer-x", profile_key=HOME)
            BD.lease.release(viewer_id="viewer-x", profile_key=HOME)      # back within a few ms: no poll sees "human"
        threading.Thread(target=quick, daemon=True).start()
        t0 = time.time()
        out = self.call(reason="tick the box", wait_seconds=20)
        self.assertEqual(out["status"], "completed", out)
        self.assertLess(time.time() - t0, 5)

    def test_timeout_when_nobody_comes(self):
        out = self.call(reason="x", wait_seconds=2)
        self.assertEqual(out["status"], "timeout")
        self.assertEqual(core.state.get_request(HOME)["status"], "expired")

    def test_still_with_human_when_they_take_over_but_never_hand_back(self):
        self.human(0.5)
        out = self.call(reason="x", wait_seconds=3)
        self.assertEqual(out["status"], "still_with_human")
        self.assertEqual(BD.lease.get().holder, BD.lease.HUMAN)       # we never take it back from them

    def test_dismissed_from_the_ui(self):
        threading.Timer(1.0, lambda: core.control.dismiss_request(HOME)).start()
        self.assertEqual(self.call(reason="x", wait_seconds=20)["status"], "dismissed")

    def test_zero_wait_just_posts_the_request(self):
        out = self.call(reason="x", wait_seconds=0)
        self.assertEqual(out["status"], "requested")
        self.assertEqual(core.state.get_request(HOME)["status"], "pending")

    def test_returns_still_waiting_before_hermes_tool_deadline_and_keeps_the_card(self):
        saved = core.tool.call_budget
        core.tool.call_budget = lambda: 2.0                         # pretend Hermes's tool deadline is close
        try:
            out = self.call(reason="sign in", wait_seconds=600)
        finally:
            core.tool.call_budget = saved
        self.assertEqual(out["status"], "still_waiting", out)
        self.assertGreater(out["wait_seconds_left"], 500)
        self.assertEqual(core.state.request_view(HOME)["status"], "pending")      # still posted for the human
        rid = core.state.get_request(HOME)["id"]
        self.human(0.5, 0.5)                                       # the agent calls again: same card, then done
        self.assertEqual(self.call(reason="sign in", wait_seconds=20)["status"], "completed")
        self.assertEqual(core.state.get_request(HOME)["id"], rid)

    def test_hermes_deadline_is_read_from_config_with_the_public_reader(self):
        cfg = Path(HOME) / "config.yaml"
        old = cfg.read_text() if cfg.exists() else None
        saved_env = os.environ.pop("HERMES_CONCURRENT_TOOL_TIMEOUT_S", None)
        try:
            for text, want in (("", 420.0), ("timeouts:\n  tools:\n    sequential_call: 120\n", 120.0),
                               ("timeouts:\n  tools:\n    concurrent_batch: 200\n", 200.0),
                               ("timeouts:\n  tools:\n    sequential_call: 90\n    concurrent_batch: 200\n", 90.0),
                               ("timeouts:\n  tools:\n    sequential_call: 0\n", None),
                               ("timeouts:\n  tools:\n    sequential_call: true\n", 420.0),
                               ("timeouts: nonsense\n", 420.0)):
                cfg.write_text(text)
                self.assertEqual(core.tool.tool_deadline(), want, text)
            cfg.write_text("timeouts:\n  tools:\n    sequential_call: 120\n")
            self.assertEqual(core.tool.call_budget(), 75.0)            # returns 45 s before Hermes would cut it off
            cfg.write_text("timeouts:\n  tools:\n    sequential_call: 0\n")
            self.assertIsNone(core.tool.call_budget())
        finally:
            if old is None:
                cfg.unlink(missing_ok=True)
            else:
                cfg.write_text(old)
            if saved_env is not None:
                os.environ["HERMES_CONCURRENT_TOOL_TIMEOUT_S"] = saved_env
        import inspect
        self.assertNotIn("tool_executor", inspect.getsource(core.tool))

    def test_two_agents_share_one_card_and_one_leaving_does_not_remove_it(self):
        res = {}
        t1 = threading.Thread(target=lambda: res.setdefault("a", self.call(reason="sign in to GitHub", wait_seconds=3)))
        t1.start(); time.sleep(0.5)
        t2 = threading.Thread(target=lambda: res.setdefault("b", self.call(reason="approve the app", wait_seconds=30)))
        t2.start(); time.sleep(0.5)
        v = core.state.request_view(HOME)
        self.assertIn("sign in to GitHub", v["reason"]); self.assertIn("approve the app", v["reason"])
        t1.join()
        self.assertEqual(res["a"]["status"], "timeout")
        self.assertIsNotNone(core.state.request_view(HOME), "the card must stay while another agent still waits")
        core.control.dismiss_request(HOME)
        t2.join(10)
        self.assertEqual(res["b"]["status"], "dismissed")

    # ── other Hermes surfaces ─────────────────────────────────────────────────────
    def events(self):
        got = []
        core.surfaces.broadcast = lambda event, payload: got.append((event, payload)) or True     # restored by tearDown
        return got

    def test_desktop_is_told_when_a_request_opens_and_when_it_is_over(self):
        got = self.events()
        self.human(1.2, 1.2)
        self.assertEqual(self.call(reason="Enter the SMS code", wait_seconds=30)["status"], "completed")
        kinds = [e for e, _ in got]
        self.assertEqual(kinds[0], "request"); self.assertEqual(kinds[-1], "ended")
        self.assertEqual(got[0][1]["reason"], "Enter the SMS code")
        self.assertEqual(got[0][1]["id"], got[-1][1]["id"]); self.assertEqual(got[-1][1]["status"], "done")

    def test_dismissal_and_timeout_clear_the_desktop_alert(self):
        got = self.events()
        threading.Timer(1.3, lambda: core.control.dismiss_request(HOME)).start()
        self.assertEqual(self.call(reason="approve", wait_seconds=20)["status"], "dismissed")
        self.assertEqual(got[-1][1]["status"], "dismissed")
        got.clear()
        self.assertEqual(self.call(reason="approve", wait_seconds=1)["status"], "timeout")
        self.assertEqual(got[-1], ("ended", {**got[-1][1], "status": "expired"}))

    def test_still_waiting_keeps_the_desktop_alert_up(self):
        got = self.events()
        saved = core.tool.call_budget
        core.tool.call_budget = lambda: 2.0
        try:
            self.assertEqual(self.call(reason="sign in", wait_seconds=600)["status"], "still_waiting")
        finally:
            core.tool.call_budget = saved
        self.assertEqual([e for e, _ in got], ["request"])               # no "ended": the request is still open

    def test_the_real_event_bridge_names_the_events_for_the_desktop_half(self):
        sent = []
        import types
        core.surfaces.broadcast = self._bc                               # the real one, against a fake gateway module
        fake = types.ModuleType("tui_gateway.server"); fake._broadcast_global_event = lambda n, p=None: sent.append((n, p))
        pkg = types.ModuleType("tui_gateway"); pkg.server = fake
        saved = {k: sys.modules.get(k) for k in ("tui_gateway", "tui_gateway.server")}
        sys.modules.update({"tui_gateway": pkg, "tui_gateway.server": fake})
        try:
            self.assertTrue(core.surfaces.announce(HOME, {"id": "r9", "reason": "Solve the CAPTCHA", "created": 1}))
            self.assertTrue(core.surfaces.ended(HOME, "r9", "done"))
        finally:
            for k, v in saved.items():
                sys.modules.pop(k, None) if v is None else sys.modules.__setitem__(k, v)
        self.assertEqual([n for n, _ in sent], ["plugin.takeover.request", "plugin.takeover.ended"])
        self.assertEqual(sent[0][1]["reason"], "Solve the CAPTCHA")

    def test_no_gateway_in_this_process_means_no_event_and_no_heavy_import(self):
        core.surfaces.broadcast = self._bc
        before = "tui_gateway.server" in sys.modules
        self.assertFalse(core.surfaces.announce(HOME, {"id": "x", "reason": "y"}) and not before)
        self.assertEqual("tui_gateway.server" in sys.modules, before, "must not import the gateway just to find nobody")

    def test_webui_and_local_surfaces_are_not_messaging(self):
        saved = os.environ.get("HERMES_SESSION_PLATFORM")
        try:
            for plat, expect in (("webui", False), ("desktop", False), ("cli", False), ("tui", False), ("dashboard", False),
                                 ("", False), ("telegram", True), ("Discord", True), ("slack", True), ("whatsapp", True)):
                os.environ["HERMES_SESSION_PLATFORM"] = plat
                self.assertEqual(core.surfaces.is_messaging(), expect, plat)
        finally:
            if saved is None:
                os.environ.pop("HERMES_SESSION_PLATFORM", None)
            else:
                os.environ["HERMES_SESSION_PLATFORM"] = saved

    def test_on_a_messaging_platform_the_agent_is_told_to_message_the_user_first(self):
        saved = core.surfaces.is_messaging, core.surfaces.session_key
        core.surfaces.is_messaging, core.surfaces.session_key = (lambda: True), (lambda: "telegram:42")
        try:
            t0 = time.time()
            first = self.call(reason="Enter the code on your bank login", wait_seconds=30)
            self.assertEqual(first["status"], "requested_tell_user", first)
            self.assertLess(time.time() - t0, 5, "must return at once, not block on a request the user cannot see")
            self.assertIn("Enter the code on your bank login", first["message"]); self.assertIn("Hermes Desktop", first["message"])
            self.assertEqual(core.state.request_view(HOME)["status"], "pending")        # the request stays posted
            self.human(0.8, 0.8)                                                        # second call: now it waits
            second = self.call(reason="Enter the code on your bank login", wait_seconds=30)
            self.assertEqual(second["status"], "completed", second)
        finally:
            core.surfaces.is_messaging, core.surfaces.session_key = saved

    def test_a_different_messaging_chat_is_also_told_once(self):
        saved = core.surfaces.is_messaging, core.surfaces.session_key
        core.surfaces.is_messaging = lambda: True
        try:
            core.surfaces.session_key = lambda: "discord:1"
            self.assertEqual(self.call(reason="approve", wait_seconds=1)["status"], "requested_tell_user")
            core.surfaces.session_key = lambda: "telegram:2"
            self.assertEqual(self.call(reason="approve", wait_seconds=1)["status"], "requested_tell_user")
            self.assertEqual(self.call(reason="approve", wait_seconds=1)["status"], "timeout")
        finally:
            core.surfaces.is_messaging, core.surfaces.session_key = saved

    def test_interrupt_cancels(self):
        from tools import interrupt
        tid = threading.get_ident()
        threading.Timer(1.0, lambda: interrupt.set_interrupt(True, tid)).start()
        try:
            self.assertEqual(self.call(reason="x", wait_seconds=30)["status"], "cancelled")
        finally:
            interrupt.set_interrupt(False, tid)

    def test_not_ready_is_an_error_not_a_crash(self):
        def boom(home, timeout=0, **kw):
            raise RuntimeError("needs a password prompt")
        core.setup.ensure_ready = boom
        out = self.call(reason="x")
        self.assertEqual(out["status"], "error")
        self.assertIn("password", out["message"])

    def test_first_use_starts_setup_detached_and_comes_back_instead_of_blocking(self):
        seen = {}

        def installing(home, timeout=0, **kw):
            seen.update(kw, timeout=timeout)
            raise core.setup.SetupRunning("The one-time screen setup is still running.")
        core.setup.ensure_ready = installing
        out = self.call(reason="Sign in", wait_seconds=600)
        self.assertEqual(out["status"], "setting_up", out)
        self.assertIn("again", out["message"])
        self.assertIs(seen.get("detached"), True)                       # survives a short-lived agent process
        self.assertLessEqual(seen["timeout"], 120)                      # well inside Hermes' tool deadline
        self.assertIsNone(core.state.request_view(HOME))                # nothing posted for a screen that is not there yet

    def test_schema_and_registration(self):
        class Ctx:
            def __init__(s): s.tools, s.cli, s.cmds, s.skills, s.hooks = [], [], [], [], []
            def register_hook(s, name, fn): s.hooks.append(name)
            def register_tool(s, **kw): s.tools.append(kw["name"])
            def register_cli_command(s, **kw): s.cli.append(kw["name"])
            def register_command(s, name, handler, **kw): s.cmds.append(name)
            def register_skill(s, name, path): s.skills.append((name, Path(path).exists()))
            def register_system_prompt_section(s, id, content, **kw): s.sections = [(id, kw.get("max_chars"))]
        c = Ctx()
        plugin_init.register(c)
        self.assertEqual((c.tools, c.cli, c.cmds, c.skills, c.hooks), (["request_takeover"], ["takeover"], ["takeover"],
                         [("takeover", True)], ["on_session_start", "pre_tool_call", "transform_tool_result"]))
        self.assertEqual(c.sections, [("takeover.hand-over-to-user", 2000)])
        self.assertEqual(core.tool.SCHEMA["parameters"]["required"], ["reason"])


class TestFence(Base):
    """The pre_tool_call veto that covers what Hermes's own fence does not (browser.cdp_url sessions, browser_cdp)."""
    def setUp(self):
        super().setUp()
        self._cur, self._setting = core.compat.current_home, core.compat.setting
        core.compat.current_home = lambda: HOME

    def tearDown(self):
        core.compat.current_home, core.compat.setting = self._cur, self._setting
        BD.lease.release(profile_key=HOME)

    def test_browser_and_screen_tools_pass_while_the_agent_holds_control(self):
        for name in ("browser_navigate", "browser_cdp", "computer_use"):
            self.assertIsNone(core.fence.check(tool_name=name))

    def test_they_are_refused_while_a_human_holds_control(self):
        BD.lease.acquire("viewer-x", profile_key=HOME)
        for name in ("browser_navigate", "browser_snapshot", "browser_cdp", "browser_vision", "browser_type", "computer_use"):
            v = core.fence.check(tool_name=name, args={})
            self.assertEqual((v or {}).get("action"), "block", name)
            self.assertIn("hand control back", v["message"])

    def test_other_tools_and_request_takeover_are_never_blocked(self):
        BD.lease.acquire("viewer-x", profile_key=HOME)
        for name in ("request_takeover", "terminal", "read_file", "web_search", "clarify", ""):
            self.assertIsNone(core.fence.check(tool_name=name), name)

    def test_opt_out(self):
        BD.lease.acquire("viewer-x", profile_key=HOME)
        core.compat.setting = lambda k, d=None, **kw: False if k == "fence_browser_tools" else d
        self.assertIsNone(core.fence.check(tool_name="browser_cdp"))

    def test_the_registered_hook_is_the_fence(self):
        BD.lease.acquire("viewer-x", profile_key=HOME)
        self.assertEqual(plugin_init._pre_tool_call(tool_name="browser_cdp", args={}, task_id="t")["action"], "block")
        BD.lease.release(profile_key=HOME)
        self.assertIsNone(plugin_init._pre_tool_call(tool_name="browser_cdp", args={}, task_id="t"))


class TestNudge(Base):
    """The prompt section + the hint on browser results that show a CAPTCHA or a code prompt."""
    def setUp(self):
        super().setUp()
        self._cur = core.compat.current_home
        core.compat.current_home = lambda: HOME
        core.nudge._seen.clear()
        BD.lease.release(profile_key=HOME)
        try: os.unlink(core.state.data_dir(HOME) / "request.json")
        except OSError: pass

    def tearDown(self):
        core.compat.current_home = self._cur
        BD.lease.release(profile_key=HOME)

    def t(self, result, tool="browser_snapshot", sid="s1"):
        return core.nudge.transform(tool_name=tool, result=result, args={}, task_id="t", session_id=sid)

    def test_guidance_names_the_tool_and_the_cases(self):
        g = core.nudge.guidance({})
        for needle in ("request_takeover", "CAPTCHA", "2FA", "Authorize", "password", "never ask the user to paste", "one-tap"):
            self.assertIn(needle.lower(), g.lower())
        self.assertLessEqual(len(g), 2000)

    WALLS = {
        "captcha": ('- iframe "reCAPTCHA" [ref=e12]', "Verify you are human by completing the action below.",
                    "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b", "Our systems have detected unusual traffic from your computer",
                    "- checkbox \"I'm not a robot\"", "Press & Hold to confirm you are a human",
                    '- iframe "Widget containing checkbox for hCaptcha security challenge"'),
        "code": ("Enter the 6-digit code we sent to +1 *** 12", '- textbox "Enter code" [ref=e4]',
                 "We just sent you a verification code", '- textbox "One-time password"'),
        "authorize": ('- button "Authorize hermes-app" [ref=e9]', "Hermes Agent wants to access your Google Account",
                      '- button "Allow" [ref=e3]', "Approve sign-in request", "Check your phone to approve the sign-in. Tap Yes on the notification"),
        "credentials": ('- textbox "Password" [ref=e5]', '<input type="password" name="pw">', "Sign in with a passkey",
                        '- textbox "Username or email address" [ref=e2]'),
        "payment": ('- textbox "Card number"', '- button "Place order" [ref=e2]', '- button "Pay now"', '- textbox "MM / YY"'),
    }

    def test_real_walls_are_detected_raw_and_as_the_browser_tools_return_them(self):
        for kind, pages in self.WALLS.items():
            for page in pages:
                self.assertEqual((core.nudge.detect(page) or ("",))[0], kind, page)
                # browser tools return JSON: quotes inside the snapshot arrive escaped in the raw result
                wrapped = json.dumps({"success": True, "snapshot": "- heading \"Example\"\n" + page + "\n- link \"Help\""})
                self.assertIn('\\"', wrapped) if '"' in page else None
                self.assertEqual((core.nudge.detect(wrapped) or ("",))[0], kind, wrapped)

    def test_ordinary_pages_are_left_alone(self):
        for page in ("r/AskReddit - What is a skill everyone should learn?", '- link "Sign in" [ref=e1]', "Allowances and grants 2026",
                     "The Captchas Of Old - a history", "OTP Bank shares rose 3% on Tuesday",
                     "This site is protected by reCAPTCHA and the Google Privacy Policy and Terms of Service apply.",
                     "We use reCAPTCHA to keep spam out", "Best before / expiration date: 2026-05-01",
                     "How to grant access to a shared drive", "Enable two-factor authentication in Settings > Security",
                     "Set up 2-Step Verification and an authenticator app", "Check your phone for updates",
                     "The company said it would authorize the app store changes", "Forgot your password? Read our guide",
                     '- link "Pricing" - heading "Card number formats explained"', "A verification code is a short number", ""):
            self.assertIsNone(self.t(page), page)
            self.assertIsNone(self.t(json.dumps({"success": True, "snapshot": page})), page)

    def test_hint_is_added_to_text_and_to_json_results(self):
        out = self.t("page text ... I'm not a robot ...")
        self.assertIn("request_takeover", out); self.assertTrue(out.startswith("page text"))
        core.nudge._seen.clear()
        j = json.loads(self.t(json.dumps({"success": True, "snapshot": "- iframe \"reCAPTCHA\""})))
        self.assertTrue(j["success"]); self.assertIn("request_takeover", j["takeover_hint"])

    def test_once_per_conversation_and_kind(self):
        self.assertIsNotNone(self.t("I'm not a robot"))
        self.assertIsNone(self.t("I'm not a robot"))                       # same conversation, same kind: quiet
        self.assertIsNotNone(self.t("Enter the code we sent"))       # a different kind
        self.assertIsNotNone(self.t("I'm not a robot", sid="s2"))          # another conversation

    def test_quiet_when_the_user_already_has_it_or_a_request_is_open(self):
        BD.lease.acquire("viewer-x", profile_key=HOME)
        self.assertIsNone(self.t("I'm not a robot"))
        BD.lease.release(profile_key=HOME)
        core.state.create_request(HOME, "solve it")                 # an agent just asked
        self.assertIsNone(self.t("I'm not a robot", sid="s3"))
        r = core.state.get_request(HOME); r["created"] = time.time() - 3600 + 60; r["waiters"] = {}
        core.state.write_json(core.state.data_dir(HOME) / "request.json", r)
        self.assertIsNotNone(self.t("I'm not a robot", sid="s4"))  # left behind, nobody waiting: hints resume

    def test_only_page_reading_browser_tools_and_opt_out(self):
        for tool in ("terminal", "read_file", "web_extract", "computer_use", "browser_cdp", "request_takeover"):
            self.assertIsNone(self.t("I'm not a robot", tool=tool), tool)
        saved = core.compat.setting
        core.compat.setting = lambda k, d=None, **kw: False if k == "takeover_hints" else d
        try: self.assertIsNone(self.t("I'm not a robot", sid="s9"))
        finally: core.compat.setting = saved

    def test_registered_hook_is_the_nudge_and_never_raises(self):
        self.assertIn("request_takeover", plugin_init._transform_tool_result(tool_name="browser_navigate", result="I'm not a robot", session_id="x"))
        self.assertIsNone(plugin_init._transform_tool_result(tool_name="browser_navigate", result=None))
        self.assertIsNone(plugin_init._transform_tool_result())


class TestBootstrap(Base):
    def setUp(self):
        super().setUp()
        core.bootstrap._kicked = False

    def test_a_source_checkout_never_bootstraps(self):
        self.assertFalse(core.bootstrap.installed_copy(HOME))         # tests load the repo, not <home>/plugins/takeover
        spawned = []
        saved = core.bootstrap.spawn
        core.bootstrap.spawn = lambda home, mode="webui": spawned.append(home)
        try:
            core.bootstrap.kick(HOME); time.sleep(0.5)
        finally:
            core.bootstrap.spawn = saved
        self.assertEqual(spawned, [])

    def test_kick_runs_at_most_once_per_process_and_honours_the_off_switch(self):
        os.environ["TAKEOVER_NO_BOOTSTRAP"] = "1"
        try:
            self.assertFalse(core.bootstrap.kick(HOME))
        finally:
            del os.environ["TAKEOVER_NO_BOOTSTRAP"]
        saved = core.bootstrap.installed_copy
        core.bootstrap.installed_copy = lambda home: True
        calls = []
        saved_needs, saved_spawn = core.bootstrap.needs, core.bootstrap.spawn
        core.bootstrap.needs = lambda home: {"setup": False, "webui": True}
        core.bootstrap.spawn = lambda home, mode="webui": calls.append((home, mode))
        try:
            self.assertTrue(core.bootstrap.kick(HOME))
            self.assertFalse(core.bootstrap.kick(HOME))
        finally:
            core.bootstrap.installed_copy, core.bootstrap.needs, core.bootstrap.spawn = saved, saved_needs, saved_spawn
        self.assertEqual(calls, [(HOME, "webui")])

    def test_a_plugin_load_never_starts_the_privileged_setup(self):
        # whatever setup state is (never attempted, packages missing, headless browser), load only ever asks for webui
        saved = core.setup.readiness, core.setup.setup_state
        core.setup.readiness = lambda home: {"supported": True, "ready": False, "headed": False}
        core.setup.setup_state = lambda home: {"auto_allowed": True, "attempted": False, "state": "idle"}
        try:
            self.assertFalse(core.bootstrap.needs(HOME)["setup"])
            import inspect
            body = inspect.getsource(core.bootstrap.kick)
            self.assertNotIn("run_setup", body); self.assertNotIn('"setup"', body)
        finally:
            core.setup.readiness, core.setup.setup_state = saved

    def test_the_helper_only_runs_setup_when_asked_for_by_a_deliberate_use(self):
        ran = []
        saved = core.setup.run_setup, core.webui.state_dir
        core.setup.run_setup = lambda home: ran.append(home) or {"state": "done"}
        core.webui.state_dir = lambda: None
        try:
            core.bootstrap.run(HOME)                                   # what a plugin load starts
            self.assertEqual(ran, [])
            core.bootstrap.run(HOME, "setup")                          # what request_takeover / Screen starts
            self.assertEqual(ran, [HOME])
        finally:
            core.setup.run_setup, core.webui.state_dir = saved

    def test_only_upgrades_never_downgrades(self):
        older, vt = core.webui.older, core.webui.vt
        self.assertTrue(older("0.3.9")); self.assertTrue(older(None)); self.assertTrue(older("garbage"))
        self.assertFalse(older(core.__version__)); self.assertFalse(older("99.0.0"))
        self.assertGreater(vt("0.10.0"), vt("0.9.1"))


class TestSetup(unittest.TestCase):
    """First-run setup policy, with the package installer mocked (it needs root)."""

    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)
        self.rt, self.inst = BD.runtime, BD.install
        self.saved = (self.rt.missing_binaries, self.inst.install_packages, core.setup.xvnc_is_tigervnc, core.compat.setting)
        self.missing = ["Xvnc", "xfwm4"]
        self.tiger = False
        self.rt.missing_binaries = lambda: list(self.missing)
        core.setup.xvnc_is_tigervnc = lambda: self.tiger
        self.config_sets = []
        import hermes_cli.config as cfg
        self._set = cfg.set_config_value
        cfg.set_config_value = lambda k, v, force=False: self.config_sets.append((k, v))
        self._headed = core.setup.browser_headed
        core.setup.browser_headed = lambda: False
        self.auto = True
        core.compat.setting = lambda k, d=None, **kw: self.auto if k == "auto_install" else d

    def tearDown(self):
        import hermes_cli.config as cfg
        self.rt.missing_binaries, self.inst.install_packages, core.setup.xvnc_is_tigervnc, core.compat.setting = self.saved
        cfg.set_config_value = self._set
        core.setup.browser_headed = self._headed

    def installer(self, code, fixes=True):
        calls = []

        def fake(*, ask_password, on_line, timeout_seconds=0, claimed=False):
            calls.append(1)
            on_line("$ apt-get install ...")
            if fixes and code == 0:
                self.missing, self.tiger = [], True
            return code
        self.inst.install_packages = fake
        return calls

    def test_installs_packages_and_turns_on_headed_browser(self):
        calls = self.installer(0)
        res = core.setup.run_setup(self.home)
        self.assertEqual(res["state"], "done", res)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.config_sets, [("browser.headed", "true")])
        self.assertTrue(core.setup.readiness(self.home)["ready"])

    def test_tightvnc_counts_as_missing_even_when_the_binary_exists(self):
        self.missing, self.tiger = [], False
        self.assertFalse(core.setup.readiness(self.home)["ready"])
        self.installer(0)
        self.assertEqual(core.setup.run_setup(self.home)["state"], "done")

    def test_no_root_means_a_clear_manual_command_not_a_crash(self):
        self.installer(-1)
        res = core.setup.run_setup(self.home)
        self.assertEqual(res["state"], "needs_manual")
        self.assertTrue(res["manual_command"])
        self.assertIn(res["manual_command"], res["error"])
        self.assertEqual(self.config_sets, [])

    def test_installer_succeeds_but_xvnc_still_tightvnc_is_reported(self):
        self.installer(0, fixes=False)
        res = core.setup.run_setup(self.home)
        self.assertEqual(res["state"], "needs_manual")
        self.assertIn("TightVNC", res["error"])

    def test_auto_setup_runs_once_per_first_use_then_only_on_request(self):
        calls = self.installer(-1)
        core.setup.maybe_auto_setup(self.home)
        for _ in range(50):
            if core.setup.setup_state(self.home)["state"] != "running" and calls:
                break
            time.sleep(0.1)
        self.assertEqual(len(calls), 1)
        core.setup.maybe_auto_setup(self.home)
        time.sleep(0.5)
        self.assertEqual(len(calls), 1)                      # never a retry loop
        core.setup.start_async(self.home)                    # an explicit request does run again
        time.sleep(1.0)
        self.assertEqual(len(calls), 2)

    def test_packages_fine_but_headless_browser_is_fixed_automatically_once(self):
        self.missing, self.tiger = [], True
        calls = self.installer(0)
        core.setup.maybe_auto_setup(self.home)
        for _ in range(60):
            if core.setup.setup_state(self.home)["state"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(self.config_sets, [("browser.headed", "true")])
        self.assertEqual(calls, [])                          # nothing to install
        n = len(self.config_sets)
        core.setup.maybe_auto_setup(self.home)
        time.sleep(0.4)
        self.assertEqual(len(self.config_sets), n)           # once

    def test_auto_install_can_be_turned_off(self):
        calls = self.installer(0)
        self.auto = False
        core.setup.maybe_auto_setup(self.home)
        time.sleep(0.5)
        self.assertEqual(calls, [])
        with self.assertRaises(RuntimeError) as cm:
            core.setup.ensure_ready(self.home, timeout=1)
        self.assertIn("hermes takeover setup", str(cm.exception))

    def test_ensure_ready_runs_setup_and_returns(self):
        self.installer(0)
        self.assertTrue(core.setup.ensure_ready(self.home, timeout=20)["ready"])

    def test_nothing_to_do_when_already_installed(self):
        self.missing, self.tiger = [], True
        calls = self.installer(0)
        res = core.setup.run_setup(self.home)
        self.assertEqual((res["state"], calls), ("done", []))

    def test_incompatible_hermes_is_reported_not_raised_as_a_stack_trace(self):
        old_bd, old_req = core.compat._bd, dict(core.compat.REQUIRED)
        core.compat._bd = None
        core.compat.REQUIRED["lease"] = core.compat.REQUIRED["lease"] + ["a_symbol_hermes_removed"]
        try:
            with self.assertRaises(core.compat.Incompatible) as cm:
                core.compat.load()
            self.assertIn("a_symbol_hermes_removed", str(cm.exception))
            self.assertFalse(core.tool.available())            # the tool hides itself instead of failing mid-call
            out = json.loads(core.tool.handle({"reason": "x"}))
            self.assertEqual(out["status"], "error")
        finally:
            core.compat._bd = old_bd
            core.compat.REQUIRED.clear()
            core.compat.REQUIRED.update(old_req)


if __name__ == "__main__":
    unittest.main(verbosity=2)
