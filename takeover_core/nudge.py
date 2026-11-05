"""Make the agent actually hand over when the page needs the human.

Two documented plugin surfaces, no core patching:

* a cache-safe **system prompt section** (``ctx.register_system_prompt_section``): standing guidance, frozen into each
  new session, that CAPTCHAs, logins, 2FA codes and payments are the user's job and ``request_takeover`` is how to ask;
* a ``transform_tool_result`` hook: when a browser tool's result shows a CAPTCHA / "verify you are human" wall or a
  one-time-code prompt, a short hint is attached to that result telling the agent to call ``request_takeover`` instead
  of trying to solve, bypass or guess it. At most once per conversation and kind every few minutes, and never while the
  user already holds the screen or a request is already open.

Opt out of the hint with ``plugins.entries.takeover.settings.captcha_hints: false``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from . import compat, state

SECTION_ID = "takeover.hand-over-to-user"

GUIDANCE = """You can hand the screen to the user with the `request_takeover` tool.
Use it when the page in your browser needs something only the user should do:
- a CAPTCHA, "verify you are human" / "I'm not a robot" check, or bot wall (Cloudflare, reCAPTCHA, hCaptcha, Turnstile)
- a login password, 2FA / SMS / authenticator code, passkey, or device approval
- a payment, card entry, or an irreversible confirmation the user should own
Never try to solve, bypass or guess these, and never ask the user to paste a password, code or card number into chat.
Call `request_takeover(reason="<exactly what to do, e.g. Solve the CAPTCHA on the Reddit login page>")`. The user opens
the Screen page (hermes-webui or the Hermes dashboard, works on a phone), taps Take over, does the step in YOUR browser
and hands back. The call waits for that; on `completed`, take a fresh snapshot and continue. On `timeout`,
`still_with_human` or `dismissed`, tell the user plainly what you are waiting for and stop the turn."""

# Only browser tools that return page content. computer_use returns pixels, browser_cdp returns raw protocol data.
WATCHED = frozenset({"browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_press",
                     "browser_back", "browser_scroll", "browser_vision", "browser_exec", "browser_console"})

_CAPTCHA = re.compile(
    r"re-?captcha|h-?captcha|cf-turnstile|challenges\.cloudflare\.com|verify (?:that )?you(?:'| a)re (?:a )?human"
    r"|i'?m not a robot|i am not a robot|are you a robot|unusual traffic from your|checking if the site connection is secure"
    r"|press (?:&|and) hold|complete the security check|human verification|prove you(?:'| a)re (?:a )?human"
    r"|\bcaptcha\b", re.I)
_CODE = re.compile(
    r"enter (?:the|your) (?:\d-digit |6-digit |verification |security |one-time )?code|verification code|one-time (?:code|password|passcode)"
    r"|two-factor|2-step verification|two-step verification|authenticator app|we (?:just )?sent (?:you )?a code"
    r"|security code (?:sent|we sent)|enter the otp|\botp\b", re.I)

COOLDOWN = 300.0
MAX_SCAN = 400_000
_seen: Dict[Tuple[str, str], float] = {}
_lock = threading.Lock()


def guidance(_session_info: Any = None) -> str:
    """Prompt section body; empty (Hermes skips it) where the tool is not available."""
    from . import tool
    try:
        return GUIDANCE if tool.available() else ""
    except Exception:  # noqa: BLE001
        return ""


def detect(text: str) -> Optional[Tuple[str, str]]:
    """``(kind, matched)`` for a human-only wall in ``text``, else None."""
    t = text[:MAX_SCAN]
    m = _CAPTCHA.search(t)
    if m:
        return "captcha", m.group(0)
    m = _CODE.search(t)
    if m:
        return "code", m.group(0)
    return None


def _message(kind: str, matched: str) -> str:
    what = ("a CAPTCHA / human-verification check" if kind == "captcha" else "a one-time code / 2-step verification prompt")
    example = "Solve the CAPTCHA on this page" if kind == "captcha" else "Enter the verification code on this page"
    return (f"[Takeover] This page appears to show {what} (matched: {matched!r}). Do not try to solve, bypass or guess it and "
            f"do not ask for the code in chat. Call request_takeover(reason=\"{example} ...\") so the user can do it on the "
            f"Screen page, then continue when it returns 'completed'. Ignore this hint if the page does not actually need the user.")


def _quiet_now(home: str) -> bool:
    """True when a hint would be noise: the user already holds the screen, or a request is already open."""
    bd = compat.load()
    if bd.lease.get(profile_key=home).holder == bd.lease.HUMAN:
        return True
    return state.request_view(home) is not None


def _first_in_window(key: Tuple[str, str]) -> bool:
    now = time.monotonic()
    with _lock:
        for k in [k for k, t in _seen.items() if now - t > COOLDOWN]:
            del _seen[k]
        if key in _seen:
            return False
        _seen[key] = now
        return True


def transform(tool_name: str = "", result: Any = None, task_id: str = "", session_id: str = "", **_: Any) -> Optional[str]:
    """``transform_tool_result`` callback: the result with a hint attached, or None (unchanged). Never raises."""
    try:
        if tool_name not in WATCHED or not isinstance(result, str) or not result:
            return None
        if not compat.setting("captcha_hints", True):
            return None
        hit = detect(result)
        if not hit:
            return None
        home = compat.current_home()
        if _quiet_now(home) or not _first_in_window((session_id or task_id or home, hit[0])):
            return None
        msg = _message(*hit)
        try:
            data = json.loads(result)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data["takeover_hint"] = msg
            return json.dumps(data, ensure_ascii=False)
        return result + "\n\n" + msg
    except Exception:  # noqa: BLE001 - a hint must never break a tool result
        return None
