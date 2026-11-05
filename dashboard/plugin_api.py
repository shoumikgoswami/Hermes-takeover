"""Backend routes for the Screen tab, mounted by the dashboard at /api/plugins/handoff/.

HTTP routes are behind the dashboard's own auth (session token / OAuth). The WebSocket stream is not covered by
HTTP middleware, so it is admitted only with a one-time ticket minted by an authenticated route, plus the
dashboard's own Host/Origin/peer guard. Routes run in the dashboard process (plugins.isolation: process, the default).
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, WebSocket

_CORE = "hermes_handoff_core"
log = logging.getLogger("handoff")


def _load_core():
    if _CORE in sys.modules:
        return sys.modules[_CORE]
    root = Path(__file__).resolve().parent.parent / "handoff_core"
    spec = importlib.util.spec_from_file_location(_CORE, root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CORE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_CORE, None)
        raise
    return mod


core = _load_core()
router = APIRouter()
STREAM = "/api/plugins/handoff/stream"


def _home() -> str:
    return core.compat.current_home()


def _fail(code: str, **extra: Any) -> Dict[str, Any]:
    # Expected conditions answer 200 + ok:false so the page never depends on HTTP status codes.
    return {"ok": False, "error": code, **extra}


def _viewer(body: Optional[dict], home: str) -> Optional[str]:
    v = (body or {}).get("viewer_id")
    return v if core.tickets.valid_viewer_id(home, v) else None


@router.post("/status")
def status(body: Optional[dict] = Body(None)) -> Dict[str, Any]:
    home = _home()
    try:
        return core.control.status(home, _viewer(body, home))
    except core.compat.Incompatible as exc:
        return _fail("incompatible", message=str(exc))


@router.post("/pulse")
def pulse(body: Optional[dict] = Body(None)) -> Dict[str, Any]:
    home = _home()
    try:
        return core.control.pulse(home, _viewer(body, home))
    except core.compat.Incompatible as exc:
        return _fail("incompatible", message=str(exc))


@router.post("/setup")
def setup(body: Optional[dict] = Body(None)) -> Dict[str, Any]:
    """``{auto: true}`` = first-run: start once if needed and allowed. Otherwise an explicit (re)run."""
    home = _home()
    try:
        if (body or {}).get("auto"):
            s = core.setup.maybe_auto_setup(home)
        else:
            core.setup.start_async(home)
            s = core.setup.setup_state(home)
        return {"ok": True, "setup": s}
    except core.compat.Incompatible as exc:
        return _fail("incompatible", message=str(exc))


@router.post("/start")
def start() -> Dict[str, Any]:
    home = _home()
    if not core.setup.readiness(home)["ready"]:
        return _fail("not_ready")
    core.control.start_screen_async(home)
    return {"ok": True}


@router.post("/ticket")
def ticket(body: Optional[dict] = Body(None)) -> Dict[str, Any]:
    home = _home()
    vid = _viewer(body, home) or core.tickets.new_viewer_id(home)
    if not core.control.screen(home)["running"]:
        return _fail("screen_off", viewer_id=vid)
    t = core.tickets.mint(vid, home)
    if t is None:
        return _fail("too_many_tickets", viewer_id=vid)
    return {"ok": True, "viewer_id": vid, "ticket": t, "expires_in": int(core.tickets.TTL), "stream_path": STREAM}


@router.post("/take-over")
def take_over(body: Optional[dict] = Body(None)) -> Dict[str, Any]:
    home = _home()
    vid = _viewer(body, home)
    if not vid:
        return _fail("unknown_viewer")
    if not core.control.screen(home)["running"]:
        return _fail("screen_off")
    return {"ok": True, "lease": core.control.take_over(home, vid, str((body or {}).get("reason") or ""))}


@router.post("/hand-back")
def hand_back(body: Optional[dict] = Body(None)) -> Dict[str, Any]:
    home = _home()
    vid = _viewer(body, home)
    if not vid:
        return _fail("unknown_viewer")
    return {"ok": True, "lease": core.control.hand_back(home, vid)}


@router.post("/force-hand-back")
def force_hand_back() -> Dict[str, Any]:
    return {"ok": True, "lease": core.control.force_hand_back(_home())}


@router.post("/dismiss")
def dismiss() -> Dict[str, Any]:
    core.control.dismiss_request(_home())
    return {"ok": True}


# ── the live stream ───────────────────────────────────────────────────────────
def _origin_ok(ws: WebSocket) -> bool:
    """The dashboard's own WebSocket guard (Host header, Origin, peer address) when available, else same-host Origin."""
    try:
        from hermes_cli.web_server_chat import _ws_request_is_allowed
        return bool(_ws_request_is_allowed(ws))
    except Exception:  # noqa: BLE001 - internals moved: fall back to a strict same-host check
        origin, host = ws.headers.get("origin"), ws.headers.get("host")
        return bool(origin and host and urlsplit(origin).netloc.lower() == host.lower())


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    offered = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol="binary" if "binary" in offered else None)
    if not _origin_ok(ws):
        await ws.close(code=core.bridge.CLOSE_BAD_ORIGIN)
        return
    info = core.tickets.consume(ws.query_params.get("ticket"))
    if info is None:
        await ws.close(code=core.bridge.CLOSE_BAD_TICKET)
        return
    viewer_id, home = info
    await core.bridge.pump(ws, home, viewer_id, core.tickets.short(viewer_id))
