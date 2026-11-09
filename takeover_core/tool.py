"""``request_takeover``: the agent asks the human to take over the screen, then (optionally) waits."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from . import compat, control, setup, state, surfaces

NAME = "request_takeover"
DEFAULT_WAIT, MAX_WAIT = 900, 3600

SCHEMA = {
    "name": NAME,
    "description": (
        "Ask the human to take over the browser/desktop screen for any step only they should do: typing credentials "
        "(password, sign-in, passkey), approving or authorizing something (OAuth consent, 'Allow access', sign-in or device "
        "approval), a 2FA/SMS/email code, a CAPTCHA or bot check, a payment, or an unexpected consent page. The user sees your request on the Screen page of "
        "hermes-webui, the Hermes dashboard or Hermes Desktop (also on their phone), taps the one-tap Take over button, does the step in the SAME browser you are using, then hands "
        "control back. This call WAITS until they have handed back, so you do not need to ask them to say 'done'. "
        "Hermes limits one tool call to a few minutes: if the result is 'still_waiting', call it again with the same "
        "reason to keep waiting (the request stays posted). While they hold control, all your screen/browser tools are refused. Never ask the user "
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
        return _out("error", f"request_takeover failed: {type(exc).__name__}: {exc}")


def call_budget() -> Optional[float]:
    """Seconds this tool call may block. Hermes stops any tool call after its tool deadline (420 s by default,
    ``timeouts.tools.sequential_call``) and interrupts it; waiting longer would lose the answer. None = no deadline."""
    try:
        from agent.tool_executor import _resolve_sequential_tool_timeout
        t = _resolve_sequential_tool_timeout()
    except Exception:  # noqa: BLE001 - older / newer Hermes: assume the documented default
        t = 420.0
    if t is None or t <= 0:
        return None
    return max(20.0, float(t) - 45.0)


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
    budget = call_budget()
    started = time.monotonic()

    try:                                   # never block the whole call on first-run setup
        ready = setup.ensure_ready(home, timeout=min(120.0, (budget or 600.0) / 3))
    except RuntimeError as exc:
        return _out("error", f"The screen is not available: {exc}")
    try:                                   # keep the hermes-webui half current (no-op when WebUI is not on this host)
        from . import webui
        if webui.state_dir() is not None and webui.enabled() and not webui.health(0.5):
            import threading
            threading.Thread(target=webui.integrate, args=(home,), daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
    if not control.screen(home)["running"]:
        try:
            if not control.start_screen(home)["running"]:
                return _out("error", "The screen could not be started (see `hermes takeover status`).")
        except Exception as exc:  # noqa: BLE001
            return _out("error", f"The screen could not be started: {exc}")

    warn = ""
    if ready.get("headed") is False:
        warn = (" Note: browser.headed is off, so your Chrome may be headless and the human may see an empty desktop; "
                "run `hermes config set browser.headed true`.")
    epoch0 = bd.lease.get(profile_key=home).epoch
    req, waiter = state.join_request(home, reason)
    surfaces.announce(home, state.request_view(home) or req)   # Hermes Desktop windows (no-op elsewhere)
    if wait > 0 and surfaces.is_messaging():
        # On a messaging platform the user cannot see a screen, and this call would block silently. The first time,
        # return at once so the agent can message them where to go; calling again (same request) then waits.
        who = surfaces.session_key() or "messaging"
        if not state.told(home, req["id"], who):
            state.mark_told(home, req["id"], who)
            state.leave_request(home, req["id"], waiter)
            return _out("requested_tell_user",
                        "This chat is on a messaging app, where the user cannot see the screen. Your request is posted. "
                        "Tell the user NOW, in this chat: what you need them to do (" + reason + ") and that they must "
                        "open the Screen page in hermes-webui, the Hermes dashboard or Hermes Desktop (the banner has a "
                        "Take over button), do it, and tap Hand back. Then call request_takeover again with the same "
                        "reason to wait for them." + warn)
    if wait == 0:
        state.leave_request(home, req["id"], waiter)          # posted; nobody waits in this call
        return _out("requested", "Request posted. Stop and tell the user to tap Take over in the banner (or open Screen), "
                    "do the step, then Hand back." + warn)

    from tools import interrupt
    # wait for the human, but return before Hermes's tool deadline would cut this call off
    this_call = wait if budget is None else min(wait, max(0.0, budget - (time.monotonic() - started)))
    deadline, taken = time.monotonic() + this_call, False
    last_beat = last_touch = 0.0
    while True:
        if interrupt.is_interrupted():
            state.leave_request(home, req["id"], waiter, status="cancelled")
            surfaces.ended(home, req["id"], "cancelled")
            return _out("cancelled", "Interrupted while waiting for the human.")
        cur = state.get_request(home) or {}
        mine = cur.get("id") == req["id"]
        if mine and cur.get("status") == "dismissed":
            state.leave_request(home, req["id"], waiter)
            surfaces.ended(home, req["id"], "dismissed")
            return _out("dismissed", "The human dismissed the request without doing it. Ask them what they want.")
        lease = bd.lease.get(profile_key=home)
        if lease.holder == bd.lease.HUMAN:
            taken = True
        elif taken or (mine and cur.get("status") == "done") or lease.epoch != epoch0:
            # a take-over + hand-back can both happen between two polls: the hand-back marks the request done and
            # every lease transition bumps the epoch, so neither is missed
            state.update_request(home, req["id"], status="done", done=time.time())
            state.leave_request(home, req["id"], waiter)
            surfaces.ended(home, req["id"], "done")
            return _out("completed", "The human did the step and handed control back. Take a fresh screenshot/snapshot "
                        "before continuing; the page has probably changed.")
        now = time.monotonic()
        if now - last_beat > 5:
            last_beat = now
            state.heartbeat(home, req["id"], waiter)
        if now - last_touch > 20:          # a screen someone is about to use is not idle: keep Bot Desktop's auto-stop away
            last_touch = now
            try:
                with compat.home_scope(home):
                    bd.runtime.touch_activity()
            except Exception:  # noqa: BLE001
                pass
        if now >= deadline:
            break
        time.sleep(1.0)

    left = wait - (time.monotonic() - started)
    if left > 5:
        # out of time for THIS call only: keep the request posted and let the agent call again to keep waiting
        state.leave_request(home, req["id"], waiter)
        what = "has taken over but not handed back yet" if taken else "has not taken over yet"
        return _out("still_waiting", f"The human {what}. The request is still posted. Call request_takeover again with "
                    f"the same reason and wait_seconds={int(left)} to keep waiting (you will not post a second "
                    "request); or tell the user what you need and stop.", wait_seconds_left=int(left))
    if taken:
        state.leave_request(home, req["id"], waiter)
        return _out("still_with_human", "The human took over but has not handed back yet. Tell the user you are waiting, "
                    "and stop; do not use screen tools until they hand back (they would be refused).")
    if state.leave_request(home, req["id"], waiter, status="expired"):
        surfaces.ended(home, req["id"], "expired")
    return _out("timeout", f"Nobody took over within {wait}s. Tell the user what you need and that they can open Screen "
                "in hermes-webui or the Hermes dashboard; then stop." + warn)
