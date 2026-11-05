"""hermes-webui sidecar: real HTTP server, real lease + RfbClientFilter, fake screen (unix socket recording the bytes
the sidecar lets through). Run with the Hermes venv python:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import http.client
import importlib.util
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = os.path.expanduser(os.environ.get("HANDOFF_AGENT_DIR", "~/.hermes/hermes-agent"))
sys.path.insert(0, AGENT)
TMP = tempfile.mkdtemp(prefix="handoff-sc-")
HOME = os.path.join(TMP, "home")
os.makedirs(HOME)
TOKEN_FILE = os.path.join(TMP, "handoff.token")
Path(TOKEN_FILE).write_text("test-token\n")
os.environ.update(HERMES_HOME=HOME, HERMES_EXT_SIDECAR_TOKEN_FILE=TOKEN_FILE)

if "hermes_handoff_core" not in sys.modules:
    spec = importlib.util.spec_from_file_location("hermes_handoff_core", ROOT / "handoff_core" / "__init__.py",
                                                  submodule_search_locations=[str(ROOT / "handoff_core")])
    core = importlib.util.module_from_spec(spec)
    sys.modules["hermes_handoff_core"] = core
    spec.loader.exec_module(core)
core = sys.modules["hermes_handoff_core"]
sc = core.sidecar
BD = core.compat.load()
_real_state_dir = core.webui.state_dir
core.webui.state_dir = lambda: None        # same rule; TestWebuiIntegration passes explicit scratch dirs
KEY = bytes([4, 1, 0, 0, 0, 0, 0, 0x61])
PTR = bytes([5, 1, 0, 10, 0, 10])
FBUR = bytes([3, 1, 0, 0, 0, 0, 0, 10, 0, 10])
HS = b"RFB 003.008\n" + b"\x01" + b"\x01"


class FakeScreen:
    def __init__(self, path):
        self.received, self.push = bytearray(), None
        self._ready = threading.Event()
        threading.Thread(target=self._run, args=(path,), daemon=True).start()
        self._ready.wait(5)

    def _run(self, path):
        async def handle(r, w):
            w.write(b"RFB 003.008\n"); await w.drain()
            while True:
                d = await r.read(65536)
                if not d: break
                self.received += d
                if d == b"BIG":
                    w.write(b"x" * 1_200_000); await w.drain()
            w.close()

        async def main():
            await asyncio.start_unix_server(handle, path)
            self._ready.set()
            await asyncio.Event().wait()
        asyncio.run(main())

    def after_handshake(self): return bytes(self.received[len(HS):])


class Base(unittest.TestCase):
    running = True

    @classmethod
    def setUpClass(cls):
        os.makedirs(os.path.join(HOME, "bot-desktop"), exist_ok=True)
        cls.sock = os.path.join(HOME, "bot-desktop", "rfb.sock")
        if os.path.exists(cls.sock): os.unlink(cls.sock)
        cls.screen = FakeScreen(cls.sock)
        cls.saved = (BD.runtime.status, BD.runtime.sandbox_screen_running, core.setup.readiness)
        BD.runtime.status = lambda profile=None: type("S", (), {"running": cls.running, "geometry": "1440x900", "blocker": None, "placement": "gateway"})()
        BD.runtime.sandbox_screen_running = lambda: False
        core.setup.readiness = lambda home: {"supported": True, "missing": [], "xvnc_tigervnc": True, "ready": True, "headed": True, "running": True, "can_install": True}
        cls.logs = []

        class H(logging.Handler):
            def emit(h, rec): cls.logs.append(rec.getMessage())
        logging.getLogger("handoff").addHandler(H()); logging.getLogger("handoff").setLevel(logging.INFO)
        cls.app = sc.App()
        cls.srv = sc.Server(("127.0.0.1", 0), sc.make_handler(cls.app))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        BD.runtime.status, BD.runtime.sandbox_screen_running, core.setup.readiness = cls.saved

    def setUp(self):
        type(self).running = True
        BD.lease.release()
        self.screen.received.clear()

    def req(self, method, path, body=None, token="test-token", raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Content-Type": "application/json" if isinstance(body, (dict, type(None))) else "application/octet-stream"}
        if token: h["X-Hermes-Sidecar-Token"] = token
        data = json.dumps(body or {}).encode() if isinstance(body, (dict, type(None))) and method == "POST" else body
        c.request(method, path, body=data, headers=h)
        r = c.getresponse(); out = r.read(); hdrs = {k.lower(): v for k, v in r.getheaders()}; c.close()
        if raw: return r.status, hdrs, out
        try: return r.status, json.loads(out or b"{}")
        except ValueError: return r.status, {}

    def viewer(self):
        return self.req("POST", "/api/viewer")[1]["viewer_id"]

    def open(self, vid=None):
        vid = vid or self.viewer()
        st, j = self.req("POST", "/api/rfb/open", {"viewer_id": vid})
        self.assertTrue(j["ok"], j)
        return vid, j["sid"]

    def send(self, sid, data): return self.req("POST", f"/api/rfb/send?sid={sid}", data, raw=True)
    def recv(self, sid, wait=0.3): return self.req("GET", f"/api/rfb/recv?sid={sid}&wait={wait}", raw=True)

    def drain(self, sid, until=None, timeout=3.0):
        got, end = b"", time.time() + timeout
        while time.time() < end:
            st, h, d = self.recv(sid, 0.3)
            got += d
            if h.get("x-handoff-closed"): return got, int(h["x-handoff-closed"])
            if until and until in got: break
        return got, None


class TestAuth(Base):
    def test_health_is_open_everything_else_needs_the_token(self):
        self.assertEqual(self.req("GET", "/health", token=None)[0], 200)
        for m, p in (("GET", "/api/status"), ("POST", "/api/viewer"), ("POST", "/api/rfb/open"), ("POST", "/api/take-over"),
                     ("POST", "/api/force-hand-back"), ("GET", "/api/rfb/recv?sid=x")):
            self.assertEqual(self.req(m, p, token=None)[0], 401, p)
            self.assertEqual(self.req(m, p, token="wrong")[0], 401, p)
        self.assertEqual(self.req("GET", "/api/status")[0], 200)

    def test_missing_token_file_is_503_and_rotation_is_immediate(self):
        os.rename(TOKEN_FILE, TOKEN_FILE + ".bak")
        try: self.assertEqual(self.req("GET", "/api/status")[0], 503)
        finally: os.rename(TOKEN_FILE + ".bak", TOKEN_FILE)
        Path(TOKEN_FILE).write_text("rotated\n")
        try:
            self.assertEqual(self.req("GET", "/api/status")[0], 401)
            self.assertEqual(self.req("GET", "/api/status", token="rotated")[0], 200)
        finally: Path(TOKEN_FILE).write_text("test-token\n")

    def test_forged_viewer_ids_are_refused(self):
        self.assertEqual(self.req("POST", "/api/take-over", {"viewer_id": "aa.bb"})[1]["error"], "unknown_viewer")
        self.assertEqual(self.req("POST", "/api/rfb/open", {"viewer_id": "aa.bb"})[1]["error"], "unknown_viewer")
        self.assertNotEqual(self.req("POST", "/api/viewer", {"viewer_id": "aa.bb"})[1]["viewer_id"], "aa.bb")

    def test_a_bogus_content_length_is_refused_not_hung_on(self):
        import http.client
        for bad, want in (("-1", 400), ("abc", 400), (str(sc.MAX_SEND + 1), 413)):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.putrequest("POST", "/api/status"); c.putheader("Content-Length", bad); c.putheader(sc.HEADER, "test-token"); c.endheaders()
            self.assertEqual(c.getresponse().status, want, bad)
            c.close()

    def test_stream_calls_with_an_unknown_session_say_it_is_closed(self):
        st, h, _ = self.recv("nope")
        self.assertEqual((st, h.get("x-handoff-closed")), (404, str(sc.CLOSE_IDLE)))


class TestStream(Base):
    def test_screen_bytes_arrive_through_long_poll(self):
        _, sid = self.open()
        got, closed = self.drain(sid, until=b"RFB 003.008\n")
        self.assertEqual((got, closed), (b"RFB 003.008\n", None))

    def test_input_only_flows_while_this_viewer_holds_the_lease(self):
        vid, sid = self.open()
        self.send(sid, HS); self.send(sid, FBUR); self.send(sid, KEY); self.send(sid, PTR)       # watching: input dropped
        time.sleep(0.3)
        self.assertEqual(self.screen.after_handshake(), FBUR)
        self.assertTrue(self.req("POST", "/api/take-over", {"viewer_id": vid})[1]["ok"])
        time.sleep(0.4)
        self.send(sid, KEY); self.send(sid, PTR)
        time.sleep(0.3)
        self.assertEqual(self.screen.after_handshake(), FBUR + KEY + PTR)
        self.req("POST", "/api/hand-back", {"viewer_id": vid})
        time.sleep(0.4)
        self.send(sid, KEY)
        time.sleep(0.3)
        self.assertEqual(self.screen.after_handshake(), FBUR + KEY + PTR)                          # dropped again

    def test_another_viewers_session_cannot_type_while_a_different_one_holds(self):
        a, sa = self.open(); b, sb = self.open()
        self.send(sa, HS); self.send(sb, HS); time.sleep(0.3)
        self.screen.received.clear()
        self.req("POST", "/api/take-over", {"viewer_id": a}); time.sleep(0.4)
        self.send(sb, KEY); time.sleep(0.3)
        self.assertEqual(bytes(self.screen.received), b"")                                         # b is a watcher
        self.send(sa, KEY); time.sleep(0.3)
        self.assertEqual(bytes(self.screen.received), KEY)

    def test_second_device_evicts_first_with_4000(self):
        a, sa = self.open(); b, sb = self.open()
        self.send(sa, HS)
        self.req("POST", "/api/take-over", {"viewer_id": a}); time.sleep(0.6)
        self.recv(sa, 0.3)                                                                          # session observes it held control
        self.req("POST", "/api/take-over", {"viewer_id": b})
        _, closed = self.drain(sa, timeout=3)
        self.assertEqual(closed, sc.CLOSE_EVICTED)

    def test_handing_back_to_the_agent_is_not_an_eviction(self):
        a, sa = self.open()
        self.req("POST", "/api/take-over", {"viewer_id": a}); time.sleep(0.6); self.recv(sa, 0.3)
        self.req("POST", "/api/hand-back", {"viewer_id": a})
        _, closed = self.drain(sa, timeout=1.5)
        self.assertIsNone(closed)

    def test_bad_framing_closes_with_1008(self):
        _, sid = self.open()
        st, h, _ = self.send(sid, HS + bytes([99]))
        self.assertEqual((st, h.get("x-handoff-closed")), (410, "1008"))

    def test_closing_a_session_is_clean_and_idempotent(self):
        _, sid = self.open()
        self.assertEqual(self.req("POST", f"/api/rfb/close?sid={sid}", raw=True)[0], 204)
        self.assertEqual(self.recv(sid)[0], 404)

    def test_idle_sessions_are_reaped(self):
        _, sid = self.open()
        self.app.sessions.get(sid).last_poll -= sc.IDLE_CLOSE + 5
        time.sleep(2.6)
        self.assertEqual(self.recv(sid)[0], 404)

    def test_big_frames_are_chunked_under_the_proxy_cap(self):
        _, sid = self.open()
        sess = self.app.sessions.get(sid)
        self.drain(sid, until=b"RFB 003.008\n")
        with sess.cv:
            sess.buf += b"y" * 1_200_000
        sizes, total = [], 0
        while total < 1_200_000:
            st, h, d = self.recv(sid, 0.5)
            if not d: break
            sizes.append(len(d)); total += len(d)
        self.assertEqual(total, 1_200_000)
        self.assertLessEqual(max(sizes), sc.MAX_RECV)
        self.assertLess(sc.MAX_RECV, 512 * 1024)             # WebUI's proxy refuses larger responses

    def test_screen_stop_closes_with_4001(self):
        _, sid = self.open()
        self.drain(sid, until=b"RFB")
        self.app.sessions.get(sid).up.shutdown(2)                                                                         # Xvnc went away
        _, closed = self.drain(sid, timeout=3)
        self.assertEqual(closed, sc.CLOSE_SCREEN_OFF)

    def test_screen_off_is_reported_not_crashed(self):
        os.rename(self.sock, self.sock + ".gone")
        try:
            st, j = self.req("POST", "/api/rfb/open", {"viewer_id": self.viewer()})
            self.assertEqual((j["ok"], j["error"]), (False, "screen_off"))
        finally:
            os.rename(self.sock + ".gone", self.sock)


class TestControl(Base):
    def test_status_pulse_and_request_lifecycle(self):
        core.state.create_request(HOME, "Enter the code")
        vid = self.viewer()
        self.assertEqual(self.req("POST", "/api/pulse", {"viewer_id": vid})[1]["request"]["status"], "pending")
        self.assertTrue(self.req("POST", "/api/take-over", {"viewer_id": vid, "reason": ""})[1]["lease"]["mine"])
        self.assertIn("Enter the code", BD.lease.get().reason)
        with self.assertRaises(BD.lease.HumanHasControl):
            BD.lease.assert_agent_may_act()
        st = self.req("POST", "/api/status", {"viewer_id": vid})[1]
        self.assertEqual((st["lease"]["mine"], st["request"]["status"]), (True, "taken"))
        self.assertNotIn(vid, json.dumps(self.req("GET", "/api/status")[1]))                       # capability never leaks
        self.req("POST", "/api/hand-back", {"viewer_id": vid})
        self.assertIsNone(self.req("POST", "/api/pulse", {})[1]["request"])
        self.assertEqual(core.state.get_request(HOME)["status"], "done")

    def test_stale_tab_cannot_release_newer_holder_and_force_hand_back_works(self):
        a, b = self.viewer(), self.viewer()
        self.req("POST", "/api/take-over", {"viewer_id": a}); self.req("POST", "/api/take-over", {"viewer_id": b})
        self.req("POST", "/api/hand-back", {"viewer_id": a})
        self.assertEqual(BD.lease.get().viewer_id, b)
        self.req("POST", "/api/force-hand-back")
        self.assertEqual(BD.lease.get().holder, BD.lease.AGENT)

    def test_no_session_ids_or_viewer_ids_in_logs(self):
        self.logs.clear()
        vid, sid = self.open()
        self.req("POST", "/api/take-over", {"viewer_id": vid}); time.sleep(0.5); self.recv(sid, 0.3)
        self.req("POST", "/api/take-over", {"viewer_id": self.viewer()}); self.drain(sid, timeout=2)
        blob = "\n".join(self.logs)
        for secret in (sid, vid, vid.split(".")[0]):
            self.assertNotIn(secret, blob)


class TestWebuiIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = sc.Server(("127.0.0.1", 0), sc.make_handler(sc.App()))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.sd = Path(tempfile.mkdtemp(dir=TMP))
        self.webui = core.webui

    def test_install_matches_webui_gallery_format_and_is_idempotent(self):
        r = self.webui.install_extension(self.sd)
        self.assertTrue(r["installed"])
        man = json.loads((self.sd / "extension-install-manifest.json").read_text())
        e = man["installed"]["handoff"]
        self.assertEqual(e["version"], core.__version__)
        self.assertIn("manifest.json", e["files"]); self.assertIn("assets/handoff.js", e["files"])
        self.assertIn("assets/novnc/core/rfb.js", e["files"])
        self.assertEqual(self.webui.installed_version(self.sd), core.__version__)
        self.webui.install_extension(self.sd)
        self.assertEqual(len(json.loads((self.sd / "extension-install-manifest.json").read_text())["installed"]), 1)

    def test_sidecar_port_follows_HANDOFF_PORT(self):
        old = os.environ.get("HANDOFF_PORT")
        os.environ["HANDOFF_PORT"] = "17951"
        try:
            self.webui.install_extension(self.sd)
        finally:
            os.environ.pop("HANDOFF_PORT") if old is None else os.environ.__setitem__("HANDOFF_PORT", old)
        man = json.loads((self.sd / "extensions" / "handoff" / "manifest.json").read_text())
        self.assertEqual(man["extensions"][0]["sidecar"]["origin"], "http://127.0.0.1:17951")

    def test_other_installed_extensions_are_preserved(self):
        (self.sd / "extension-install-manifest.json").write_text(json.dumps({"version": 1, "installed": {"btop": {"version": "1", "files": ["manifest.json"], "installed_at": "x"}}}))
        self.webui.install_extension(self.sd)
        self.assertEqual(set(json.loads((self.sd / "extension-install-manifest.json").read_text())["installed"]), {"btop", "handoff"})

    def test_uninstall_removes_files_and_entry(self):
        self.webui.install_extension(self.sd)
        self.webui.uninstall_extension(self.sd)
        self.assertFalse((self.sd / "extensions" / "handoff").exists())
        self.assertIsNone(self.webui.installed_version(self.sd))

    def test_a_process_never_stops_itself_and_setup_never_forces_a_restart(self):
        state_file = core.state.data_dir(HOME) / "sidecar.json"
        core.state.write_json(state_file, {"pid": os.getpid()})
        self.assertFalse(self.webui.stop_sidecar(HOME))          # would have SIGTERMed this very test process
        import inspect
        src = inspect.getsource(core.setup._run)
        self.assertNotIn("force=True", src)

    def test_nothing_happens_without_webui(self):
        self.assertEqual(self.webui.integrate(HOME), {"detected": False, "disabled": False})

    def test_switch_off_means_hands_off(self):
        saved = (self.webui.state_dir, core.compat.setting)
        self.webui.state_dir = lambda: self.sd
        core.compat.setting = lambda k, d=None: False if k == "webui_integration" else d
        try:
            self.assertEqual(self.webui.integrate(HOME), {"detected": True, "disabled": True})
            self.assertFalse((self.sd / "extensions").exists())          # nothing written, no process started
        finally:
            self.webui.state_dir, core.compat.setting = saved

    def test_health_identifies_the_profile_so_a_foreign_sidecar_is_not_mistaken_for_ours(self):
        h = json.loads(__import__("urllib.request").request.urlopen(f"http://127.0.0.1:{TestWebuiIntegration.port}/health").read())
        self.assertEqual(h["home_id"], sc.home_id(HOME))
        self.assertNotEqual(sc.home_id(HOME), sc.home_id(os.path.join(TMP, "other")))
        self.assertNotIn(HOME, json.dumps(h))


if __name__ == "__main__":
    unittest.main(verbosity=2)
