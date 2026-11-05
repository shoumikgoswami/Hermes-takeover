"""Tests for Handoff's core, the dashboard routes and the agent tool.

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
AGENT = os.path.expanduser(os.environ.get("HANDOFF_AGENT_DIR", "~/.hermes/hermes-agent"))
sys.path.insert(0, AGENT)
TMP = tempfile.mkdtemp(prefix="handoff-test-")
HOME = os.path.join(TMP, "home")
os.makedirs(HOME)
os.environ["HERMES_HOME"] = HOME

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


core = _load("hermes_handoff_core", ROOT / "handoff_core" / "__init__.py", submodule_search_locations=[str(ROOT / "handoff_core")])
api = _load("hermes_dashboard_plugin_handoff", ROOT / "dashboard" / "plugin_api.py")
plugin_init = _load("handoff_plugin_init", ROOT / "__init__.py")

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
        cls.real_origin_ok = staticmethod(api._origin_ok)
        # same-host Origin check (the real dashboard guard is exercised by tests/e2e)
        api._origin_ok = lambda ws: (ws.headers.get("origin") or "").split("//")[-1] == ws.headers.get("host")
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/handoff")
        cls.port = free_port()
        cls.logs = []

        class H(logging.Handler):
            def emit(h, rec):
                cls.logs.append(rec.getMessage())
        for n in ("handoff", "handoff.bridge", "uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(n).addHandler(H())
        logging.getLogger("handoff").setLevel(logging.INFO)
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
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/plugins/handoff{path}", method="POST",
                                     data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def viewer_ticket(self, viewer=None):
        j = self.post("/ticket", {"viewer_id": viewer} if viewer else {})
        self.assertTrue(j["ok"], j)
        return j["viewer_id"], j["ticket"]

    def ws(self, ticket, origin=None):
        return connect(f"ws://127.0.0.1:{self.port}/api/plugins/handoff/stream?ticket={ticket}",
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

    def test_fallback_origin_guard_when_dashboard_internals_move(self):
        class W:
            def __init__(s, h): s.headers = h
        saved = sys.modules.get("hermes_cli.web_server_chat")
        sys.modules["hermes_cli.web_server_chat"] = None          # `from hermes_cli.web_server_chat import ...` -> ImportError
        try:
            ok = self.real_origin_ok
            self.assertTrue(ok(W({"origin": "https://h.example", "host": "h.example"})))
            self.assertFalse(ok(W({"origin": "https://evil.example", "host": "h.example"})))
            self.assertFalse(ok(W({"host": "h.example"})))
        finally:
            if saved is None:
                sys.modules.pop("hermes_cli.web_server_chat", None)
            else:
                sys.modules["hermes_cli.web_server_chat"] = saved


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
        core.setup.ensure_ready = lambda home, timeout=0: {"ready": True, "headed": True}
        self._start = core.control.start_screen
        core.control.start_screen = lambda home: {"running": True}

    def tearDown(self):
        core.setup.ensure_ready = self._ready
        core.control.start_screen = self._start

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

    def test_interrupt_cancels(self):
        from tools import interrupt
        tid = threading.get_ident()
        threading.Timer(1.0, lambda: interrupt.set_interrupt(True, tid)).start()
        try:
            self.assertEqual(self.call(reason="x", wait_seconds=30)["status"], "cancelled")
        finally:
            interrupt.set_interrupt(False, tid)

    def test_not_ready_is_an_error_not_a_crash(self):
        def boom(home, timeout=0):
            raise RuntimeError("needs a password prompt")
        core.setup.ensure_ready = boom
        out = self.call(reason="x")
        self.assertEqual(out["status"], "error")
        self.assertIn("password", out["message"])

    def test_schema_and_registration(self):
        class Ctx:
            def __init__(s): s.tools, s.cli, s.cmds, s.skills, s.hooks = [], [], [], [], []
            def register_hook(s, name, fn): s.hooks.append(name)
            def register_tool(s, **kw): s.tools.append(kw["name"])
            def register_cli_command(s, **kw): s.cli.append(kw["name"])
            def register_command(s, name, handler, **kw): s.cmds.append(name)
            def register_skill(s, name, path): s.skills.append((name, Path(path).exists()))
        c = Ctx()
        plugin_init.register(c)
        self.assertEqual((c.tools, c.cli, c.cmds, c.skills, c.hooks), (["request_handoff"], ["handoff"], ["screen"], [("handoff", True)], ["on_session_start", "pre_tool_call"]))
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

    def test_other_tools_and_request_handoff_are_never_blocked(self):
        BD.lease.acquire("viewer-x", profile_key=HOME)
        for name in ("request_handoff", "terminal", "read_file", "web_search", "clarify", ""):
            self.assertIsNone(core.fence.check(tool_name=name), name)

    def test_opt_out(self):
        BD.lease.acquire("viewer-x", profile_key=HOME)
        core.compat.setting = lambda k, d=None: False if k == "fence_browser_tools" else d
        self.assertIsNone(core.fence.check(tool_name="browser_cdp"))

    def test_the_registered_hook_is_the_fence(self):
        BD.lease.acquire("viewer-x", profile_key=HOME)
        self.assertEqual(plugin_init._pre_tool_call(tool_name="browser_cdp", args={}, task_id="t")["action"], "block")
        BD.lease.release(profile_key=HOME)
        self.assertIsNone(plugin_init._pre_tool_call(tool_name="browser_cdp", args={}, task_id="t"))


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
        core.compat.setting = lambda k, d=None: self.auto if k == "auto_install" else d

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
        self.assertIn("hermes handoff setup", str(cm.exception))

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
