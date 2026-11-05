"""``request_handoff``: the agent asks the human to take over the screen, then (optionally) waits."""
from __future__ import annotations

import json
import time
from typing import Any, Dict

from . import compat, control, setup, state

NAME = "request_handoff"
DEFAULT_WAIT, MAX_WAIT = 900, 3600

SCHEMA = {
    "name": NAME,
    "description": (
        "Ask the human to take over the browser/desktop screen for a step only they can do: a login, password, 2FA/SMS "
        "code, CAPTCHA, payment, or an unexpected consent page. The user sees your request in the Hermes dashboard's "
        "Screen tab (and on their phone), taps Take over, does the step in the SAME browser you are using, then hands "
        "control back. This call WAITS until they have handed back (default up to 15 minutes), so you do not need to "
        "ask them to say 'done'. While they hold control, all your screen/browser tools are refused. Never ask the user "
        "to type secrets into chat; use this instead. After it returns 'completed', take a fresh screenshot/snapshot "
        "before acting: the page has changed."),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string",
                       "description": "One short sentence telling the human exactly what to do, e.g. 'Enter the SMS code "
                                      "for your bank login'. Shown to them on the Screen tab."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT,
                             "description": f"How long to wait for the human to finish (default {DEFAULT_WAIT}). 0 = just "
                                            "post the request and return immediately."},
        },
        "required": ["reason"],
    },
}


def available() -> bool:
    try:
        return bool(compat.load().runtime.is_supported_host())
    except compat.Incompatible:
        return False


def _out(status: str, message: str, **extra: Any) -> str:
    return json.dumps({"status": status, "message": message, **extra})


def handle(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        return _handle(args or {})
    except compat.Incompatible as exc:
        return _out("error", str(exc))
    except Exception as exc:  # noqa: BLE001 - a tool must return an error, never raise
        return _out("error", f"request_handoff failed: {type(exc).__name__}: {exc}")


def _handle(args: Dict[str, Any]) -> str:
    bd = compat.load()
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return _out("error", "reason is required: tell the human what to do.")
    try:
        wait = max(0, min(int(args.get("wait_seconds", DEFAULT_WAIT)), MAX_WAIT))
    except (TypeError, ValueError):
        wait = DEFAULT_WAIT
    home = compat.current_home()

    try:
        ready = setup.ensure_ready(home)
    except RuntimeError as exc:
        return _out("error", f"The screen is not available: {exc}")
    try:                                   # keep the hermes-webui half current (no-op when WebUI is not on this host)
        from . import webui
        if webui.state_dir() is not None and not webui.health(0.5):
            import threading
            threading.Thread(target=webui.integrate, args=(home,), daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
    if not control.screen(home)["running"]:
        try:
            if not control.start_screen(home)["running"]:
                return _out("error", "The screen could not be started (see `hermes handoff status`).")
        except Exception as exc:  # noqa: BLE001
            return _out("error", f"The screen could not be started: {exc}")

    warn = ""
    if ready.get("headed") is False:
        warn = (" Note: browser.headed is off, so your Chrome may be headless and the human may see an empty desktop; "
                "run `hermes config set browser.headed true`.")
    req = state.create_request(home, reason)
    if wait == 0:
        return _out("requested", "Request posted on the Screen tab. Stop and tell the user to open Screen -> Take over, "
                    "do the step, then Hand back." + warn)

    from tools import interrupt
    deadline, taken = time.monotonic() + wait, False
    while True:
        if interrupt.is_interrupted():
            state.update_request(home, req["id"], status="cancelled")
            return _out("cancelled", "Interrupted while waiting for the human.")
        cur = state.get_request(home) or {}
        if cur.get("id") == req["id"] and cur.get("status") == "dismissed":
            return _out("dismissed", "The human dismissed the request without doing it. Ask them what they want.")
        holder = bd.lease.get(profile_key=home).holder
        if holder == bd.lease.HUMAN:
            taken = True
        elif taken:
            state.update_request(home, req["id"], status="done", done=time.time())
            return _out("completed", "The human did the step and handed control back. Take a fresh screenshot/snapshot "
                        "before continuing; the page has probably changed.")
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)

    if taken:
        return _out("still_with_human", "The human took over but has not handed back yet. Tell the user you are waiting, "
                    "and stop; do not use screen tools until they hand back (they would be refused).")
    state.update_request(home, req["id"], status="expired")
    return _out("timeout", f"Nobody took over within {wait}s. Tell the user what you need and that they can open the Hermes "
                "dashboard -> Screen; then stop." + warn)
