"""Make the agent actually hand over when the page needs the human.

Two documented plugin surfaces, no core patching:

* a cache-safe **system prompt section** (``ctx.register_system_prompt_section``): standing guidance, frozen into each
  new session, that CAPTCHAs, logins, 2FA codes and payments are the user's job and ``request_takeover`` is how to ask;
* a ``transform_tool_result`` hook: when a browser tool's result shows a step only the user should do (a password /
  sign-in form, an "Authorize app" / "Allow access" consent, a 2FA / device approval prompt, a payment form, or a
  CAPTCHA wall), a short hint is attached to that result telling the agent to call ``request_takeover`` instead of
  doing it, bypassing it or guessing. At most once per conversation and kind every 5 minutes, and never while the
  user already holds the screen or an agent is already waiting on a request.

Opt out of the hint with ``plugins.entries.takeover.settings.takeover_hints: false``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from . import compat, state

SECTION_ID = "takeover.hand-over-to-user"

GUIDANCE = """You can hand your browser screen to the user with the `request_takeover` tool; they get a one-tap
"Take over" button (hermes-webui or the Hermes dashboard, also on their phone).
Call it whenever the next step on screen is one only the user should do, for example:
- typing credentials: a password, sign-in form, passkey, or account recovery
- approving or authorizing: an "Authorize app" / "Allow access" / OAuth consent screen, "Approve sign-in", device approval
- a 2FA / SMS / authenticator / email code
- a CAPTCHA, "verify you are human" check or bot wall
- a payment, card entry, or another irreversible confirmation the user should own
Never type, guess, solve or bypass these yourself, and never ask the user to paste a password, code or card number into
chat. Call `request_takeover(reason="<exactly what to do, e.g. Sign in to GitHub and approve the Hermes app>")`. The
call waits until they hand back; on `completed`, take a fresh snapshot and continue. On `timeout`, `still_with_human`
or `dismissed`, tell the user plainly what you are waiting for and stop the turn. On `still_waiting`, call it again with
the same reason to keep waiting."""

# Only browser tools that return page content. computer_use returns pixels, browser_cdp returns raw protocol data.
WATCHED = frozenset({"browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_press",
                     "browser_back", "browser_scroll", "browser_vision", "browser_exec", "browser_console"})

# Patterns run on the page text the agent sees: the decoded string values of the tool's JSON result (snapshots read
# `textbox "Password"`, `button "Authorize app"`). They need *structural* evidence (a field, a button, a challenge
# widget, an instruction addressed to the visitor), not a topic word: a news story about "OTP Bank", a help article on
# two-factor settings, "protected by reCAPTCHA" footers (invisible v3) or a "best before / expiration date" label must
# not interrupt the user. Most specific first: a sign-in page that also carries a CAPTCHA is reported as the CAPTCHA.
_Q = r"[\"']"
_CAPTCHA = re.compile(
    r"i'?m not a robot|i am not a robot|are you a robot|verify (?:that )?you(?:'| a)re (?:a )?human|prove you(?:'| a)re (?:a )?human"
    r"|unusual traffic from your|checking if the site connection is secure|press (?:&|and) hold|complete the security check"
    r"|cf-turnstile|challenges\.cloudflare\.com/cdn-cgi/challenge|recaptcha/api2/(?:anchor|bframe)|hcaptcha\.com/captcha"
    r"|select all (?:images|squares) with|type the (?:characters|text) (?:you see|in the image)"
    r"|(?:textbox|input) " + _Q + r"[^\"'\n]{0,40}captcha"
    # a challenge widget's frame (v2 / hCaptcha / Turnstile render inside one the snapshot cannot see into)
    r"|iframe " + _Q + r"(?:re)?captcha|iframe " + _Q + r"[^\"'\n]{0,80}(?:hcaptcha|security challenge|cloudflare)", re.I)
_CODE = re.compile(
    r"enter (?:the|your) (?:\d-digit |six-digit |verification |security |one-time |sign-in |login )?code"
    r"|we (?:just )?(?:sent|texted|emailed) (?:you )?(?:a|an|the) (?:\d-digit |verification |security |one-time |sign-in |login )?code"
    r"|enter the otp|code from your authenticator"
    r"|(?:textbox|input|spinbutton) " + _Q + r"[^\"'\n]{0,50}(?:\bcode\b|one-time|\botp\b|2fa|passcode)",
    re.I)
_AUTHORIZE = re.compile(
    r"button " + _Q + r"(?:authorize|allow|approve|grant access|accept and continue|yes, it'?s me)\b"
    r"|wants to access your|is requesting (?:access|permission)|would like (?:to access|permission to)|requesting permission to"
    r"|approve (?:this |the )?sign-?in|is this you trying to sign in|tap yes on|check your phone to (?:approve|confirm|sign in|continue)"
    r"|(?:open|use) (?:the |your )?\S+ app (?:on your phone )?to (?:approve|confirm)", re.I)
_CREDENTIALS = re.compile(
    r"(?:textbox|input) " + _Q + r"[^\"'\n]{0,40}(?:password|passwort|mot de passe|contraseña)|type=[\"']?password"
    r"|(?:textbox|input) " + _Q + r"[^\"'\n]{0,40}(?:username|email or phone|email address or phone|phone or email)"
    r"|enter your password|sign in with (?:a |your )?passkey|use (?:a |your )?passkey to sign in", re.I)
_PAYMENT = re.compile(
    r"(?:textbox|input|combobox) " + _Q + r"[^\"'\n]{0,40}(?:card number|name on card|expiry|expiration|\bmm ?/ ?yy|cvv|cvc|security code)"
    r"|autocomplete=[\"']?cc-(?:number|csc|exp)"
    r"|button " + _Q + r"(?:pay(?: now| \S{0,12}\d)?|place (?:your )?order|buy now|complete (?:the )?purchase|confirm (?:and pay|payment|purchase))\b",
    re.I)
KINDS = (("captcha", _CAPTCHA), ("code", _CODE), ("authorize", _AUTHORIZE), ("payment", _PAYMENT), ("credentials", _CREDENTIALS))
WHAT = {
    "captcha": ("a CAPTCHA / human-verification check", "Solve the CAPTCHA on this page"),
    "code": ("a one-time code / 2-step verification prompt", "Enter the verification code on this page"),
    "authorize": ("an approval / authorization screen (consent, \"Allow access\", sign-in approval)", "Review and approve the access request on this page"),
    "payment": ("a payment / card form", "Enter the payment details and confirm"),
    "credentials": ("a sign-in form asking for credentials", "Sign in on this page"),
}

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


def page_text(result: str) -> str:
    """What the agent actually reads: the decoded string values of a JSON tool result (so `\\"Password\\"` in the raw
    JSON reads as `"Password"`), else the raw text."""
    try:
        data = json.loads(result)
    except ValueError:
        return result.replace('\\"', '"')         # truncated / not JSON: still undo the escaping
    out = []

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k != "takeover_hint":
                    walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)
    walk(data)
    return "\n".join(out)


def detect(text: str) -> Optional[Tuple[str, str]]:
    """``(kind, matched)`` for a human-only wall in ``text`` (a tool result, JSON or plain), else None."""
    t = page_text(text[:MAX_SCAN])
    for kind, rx in KINDS:
        m = rx.search(t)
        if m:
            return kind, m.group(0)
    return None


def _message(kind: str, matched: str) -> str:
    what, example = WHAT[kind]
    return (f"[Takeover] This page appears to show {what} (matched: {matched!r}). That is the user's step: do not fill it in, "
            f"solve, approve, bypass or guess it, and do not ask for passwords or codes in chat. Call request_takeover(reason=\"{example} ...\") so the user can do it on the "
            f"Screen page, then continue when it returns 'completed'. Ignore this hint if the page does not actually need the user.")


def _quiet_now(home: str) -> bool:
    """True when a hint would be noise: the user already holds the screen, or an agent is waiting on a request right now
    (a request left behind with nobody waiting on it, e.g. ``wait_seconds=0`` long ago, does not mute hints)."""
    bd = compat.load()
    if bd.lease.get(profile_key=home).holder == bd.lease.HUMAN:
        return True
    r = state.get_request(home)
    if not r or r.get("status") not in ("pending", "taken"):
        return False
    now = time.time()
    if now - float(r.get("created", 0)) < 120:
        return True
    return any(now - float(t) < state.WAITER_ALIVE for t in (r.get("waiters") or {}).values())


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
        if not compat.setting("takeover_hints", True):
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
