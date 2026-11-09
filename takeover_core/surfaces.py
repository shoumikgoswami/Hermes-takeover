"""Where a request reaches the human, beyond the files every surface already shares.

* **hermes-webui / the Hermes dashboard** read ``request.json`` themselves (banner, Screen page).
* **Hermes Desktop** runs this plugin's backend inside ``hermes serve`` and listens on the app's event stream: the
  tool emits ``plugin.takeover.request`` / ``plugin.takeover.ended`` (``hermes_cli.plugin_events``, the sanctioned
  door) and the ``desktop/plugin.js`` half turns them into an in-app alert, a status-bar chip and a native
  notification. Outside ``hermes serve`` (``hermes chat``, a messaging gateway, cron) the call is a quiet no-op.
* **Messaging platforms (Telegram, Discord, ...)** cannot show a screen: the tool tells the agent to message the user
  first, instead of blocking on a request the user cannot see.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Optional

from . import PLUGIN_ID

log = logging.getLogger(__name__)


def broadcast(event: str, payload: Dict[str, Any]) -> bool:
    """Fire-and-forget event to every connected Desktop window. Never raises, never blocks the tool.

    Only when this process already hosts the gateway (``hermes serve`` for Desktop, the stdio TUI): that is the only
    place a client can be attached, and importing the gateway into a plain ``hermes chat`` just to find nobody
    listening would be slow and pointless."""
    if "tui_gateway.server" not in sys.modules:
        return False
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
    except Exception:  # noqa: BLE001 - older Hermes without the bridge
        return False
    try:
        broadcast_plugin_event(PLUGIN_ID, event, payload)
        return True
    except Exception:  # noqa: BLE001 - no gateway in this process, wedged client, bad payload
        log.debug("takeover: could not broadcast %s", event, exc_info=True)
        return False


def announce(home, req: Optional[Dict[str, Any]], profile: Optional[str] = None) -> bool:
    """A request is open (or still open): tell Desktop windows. Idempotent on the client (keyed by ``id``)."""
    if not req:
        return False
    return broadcast("request", {"id": req.get("id"), "reason": req.get("reason"), "created": req.get("created"),
                                 "profile": profile or profile_name(home)})


def ended(home, rid: Optional[str], status: str) -> bool:
    """The request is over (done / dismissed / expired / cancelled): clear the alert."""
    return broadcast("ended", {"id": rid, "status": status, "profile": profile_name(home)})


def profile_name(home) -> str:
    try:
        from pathlib import Path
        p = Path(str(home))
        return p.name if p.parent.name == "profiles" else "default"
    except Exception:  # noqa: BLE001
        return "default"


# Chat apps where the user has no screen view. A positive list on purpose: Hermes' own check treats every platform it
# does not know ("webui", a future local surface) as messaging, which would make hermes-webui sessions skip the wait.
# Wrongly missing a messaging platform only means the old behaviour (the request is posted and the call waits).
MESSAGING = frozenset({
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal", "mattermost", "matrix", "email", "sms",
    "dingtalk", "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles", "qqbot", "yuanbao", "relay",
    "imessage", "line", "teams", "googlechat", "irc", "twitch", "rocketchat", "zulip", "messenger", "viber",
})


def is_messaging() -> bool:
    """Is this turn delivered over a messaging platform (the user cannot see a screen there)?"""
    try:
        from gateway.session_context import get_session_env
        idents = (os.getenv("HERMES_PLATFORM") or "", get_session_env("HERMES_SESSION_PLATFORM", ""),
                  get_session_env("HERMES_SESSION_SOURCE", ""))
        return any(str(i or "").strip().lower() in MESSAGING for i in idents)
    except Exception:  # noqa: BLE001
        return False


def session_key() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_KEY", "") or get_session_env("HERMES_SESSION_ID", "") or ""
    except Exception:  # noqa: BLE001
        return ""
