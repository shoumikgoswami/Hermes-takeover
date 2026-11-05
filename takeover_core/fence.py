"""Close the gaps in Hermes's own lease fence while a human holds the screen.

Hermes fences its LOCAL browser sessions, computer_use and browser_vault. It does not fence a browser reached through
``browser.cdp_url`` (it treats that as "someone else's browser") nor the raw ``browser_cdp`` tool, yet on a Takeover host
that is very often the very Chrome on the Bot Desktop the human is typing a password into. This ``pre_tool_call`` hook
refuses every browser / screen tool for the profile while the human holds the lease. It cannot fence ``terminal`` or
code execution (README: the lease is a tool-level fence, not an OS boundary).

Opt out with ``plugins.entries.takeover.settings.fence_browser_tools: false``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import compat

SCREEN_TOOLS = frozenset({"computer_use"})
PREFIXES = ("browser_",)
EXEMPT = frozenset({"request_takeover"})

MESSAGE = ("A human has taken over the agent's screen (they may be typing a password or a code), so browser and "
           "screen tools are paused until they hand control back. If you called request_takeover it will return when they "
           "do; otherwise tell the user what you need and stop.")


def fenced_tool(name: str) -> bool:
    return bool(name) and name not in EXEMPT and (name in SCREEN_TOOLS or name.startswith(PREFIXES))


def check(tool_name: str = "", **_: Any) -> Optional[Dict[str, Any]]:
    """``{"action": "block", ...}`` while a human holds this profile's lease, else None. Never raises."""
    if not fenced_tool(tool_name):
        return None
    try:
        if not compat.setting("fence_browser_tools", True):
            return None
        bd = compat.load()
        if bd.lease.get(profile_key=compat.current_home()).holder == bd.lease.HUMAN:
            return {"action": "block", "message": MESSAGE}
    except Exception:  # noqa: BLE001 - no lease / incompatible Hermes: Hermes's own fence still applies
        return None
    return None
