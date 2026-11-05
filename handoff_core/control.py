"""Lease operations and status, shared by the dashboard routes, the CLI and the agent tool."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional

from . import __version__, compat, setup, state, tickets

log = logging.getLogger("handoff")
_start: Dict[str, Dict[str, Any]] = {}
_start_lock = threading.Lock()
START_TIMEOUT = 25.0


# ── the screen ────────────────────────────────────────────────────────────────
def screen(home) -> Dict[str, Any]:
    rt = compat.load().runtime
    with compat.home_scope(home):
        try:
            d = rt.status()
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "error": f"{type(exc).__name__}: {exc}"}
    if is_dataclass(d) and not isinstance(d, type):
        d = asdict(d)
    get = (lambda k: d.get(k)) if isinstance(d, dict) else (lambda k: getattr(d, k, None))
    with _start_lock:
        st = dict(_start.get(str(home)) or {})
    running = bool(get("running"))
    return {"running": running, "geometry": get("geometry"), "blocker": get("blocker"),
            "placement": get("placement"), "starting": st.get("state") == "starting" and not running,
            "start_error": st.get("error") if st.get("state") == "error" else None}


def start_screen_async(home) -> bool:
    home = str(home)
    with _start_lock:
        cur = _start.get(home) or {}
        if cur.get("state") == "starting" and time.time() - cur.get("since", 0) < 60:
            return False
        _start[home] = {"state": "starting", "since": time.time(), "error": None}

    def work():
        try:
            with compat.home_scope(home):
                compat.load().runtime.start(wait_seconds=START_TIMEOUT)
            new = {"state": "idle", "since": time.time(), "error": None}
        except Exception as exc:  # noqa: BLE001
            new = {"state": "error", "since": time.time(), "error": f"{type(exc).__name__}: {exc}"[:300]}
            log.warning("screen start failed: %s", type(exc).__name__)
        with _start_lock:
            _start[home] = new
    threading.Thread(target=work, name="handoff-screen-start", daemon=True).start()
    return True


def start_screen(home) -> Dict[str, Any]:
    """Blocking variant for the CLI / agent tool."""
    with compat.home_scope(home):
        s = compat.load().runtime.start(wait_seconds=START_TIMEOUT)
    return {"running": bool(getattr(s, "running", False)), "blocker": getattr(s, "blocker", None)}


# ── the lease ─────────────────────────────────────────────────────────────────
def lease_view(home, viewer_id: Optional[str]) -> Dict[str, Any]:
    bd = compat.load()
    lease = bd.lease.get(profile_key=str(home))
    pv = bd.lease.public_view(lease)         # never exposes the holder's viewer id (it is a capability)
    mine = bool(viewer_id and lease.holder == bd.lease.HUMAN and lease.viewer_id == viewer_id)
    return {"holder": lease.holder, "since": lease.since, "reason": lease.reason, "epoch": lease.epoch,
            "viewer_hash": pv.get("viewer_hash"), "mine": mine}


def take_over(home, viewer_id: str, reason: str = "") -> Dict[str, Any]:
    bd = compat.load()
    req = state.get_request(home)
    if not reason and req and req.get("status") in ("pending", "taken"):
        reason = req.get("reason", "")        # the agent's ask stays on screen while the human acts
    lease = bd.lease.acquire(viewer_id, profile_key=str(home), reason=reason[:200])
    if req and req.get("status") == "pending":
        state.update_request(home, req["id"], status="taken", taken=time.time())
    log.info("take-over viewer=%s epoch=%s", tickets.short(viewer_id), lease.epoch)
    return lease_view(home, viewer_id)


def _close_request_if_handed_back(home) -> None:
    bd = compat.load()
    if bd.lease.get(profile_key=str(home)).holder == bd.lease.AGENT:
        req = state.get_request(home)
        if req and req.get("status") in ("pending", "taken"):
            state.update_request(home, req["id"], status="done", done=time.time())


def hand_back(home, viewer_id: str) -> Dict[str, Any]:
    bd = compat.load()
    lease = bd.lease.release(viewer_id=viewer_id, profile_key=str(home))
    log.info("hand-back viewer=%s epoch=%s holder=%s", tickets.short(viewer_id), lease.epoch, lease.holder)
    _close_request_if_handed_back(home)
    return lease_view(home, viewer_id)


def force_hand_back(home) -> Dict[str, Any]:
    bd = compat.load()
    lease = bd.lease.release(profile_key=str(home))
    log.info("force-hand-back epoch=%s holder=%s", lease.epoch, lease.holder)
    _close_request_if_handed_back(home)
    return lease_view(home, None)


def dismiss_request(home) -> None:
    req = state.get_request(home)
    if req and req.get("status") in ("pending", "taken"):
        state.update_request(home, req["id"], status="dismissed")


# ── status ────────────────────────────────────────────────────────────────────
def pulse(home, viewer_id: Optional[str]) -> Dict[str, Any]:
    """Cheap (no subprocess): enough for the always-on banner."""
    return {"ok": True, "request": state.request_view(home), "lease": lease_view(home, viewer_id)}


def status(home, viewer_id: Optional[str]) -> Dict[str, Any]:
    from . import webui
    r = setup.readiness(home)
    return {"ok": True, "version": __version__, "hermes_home": str(home), "readiness": r, "webui": webui.status(home),
            "setup": setup.setup_state(home), "screen": screen(home) if r["supported"] else {"running": False},
            "request": state.request_view(home), "lease": lease_view(home, viewer_id)}
