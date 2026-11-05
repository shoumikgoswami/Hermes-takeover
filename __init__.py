"""Handoff: let a human take over the agent's screen, do the step only they can do, and hand control back.

Surfaces registered here: the ``request_handoff`` tool, ``hermes handoff`` and ``/handoff``, and a bundled skill.
The dashboard half (Screen tab, routes, live view) is in ``dashboard/``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CORE = "hermes_handoff_core"


def _load_core():
    """Import ``handoff_core`` by path under one fixed module name, shared with dashboard/plugin_api.py."""
    if _CORE in sys.modules:
        return sys.modules[_CORE]
    root = Path(__file__).resolve().parent / "handoff_core"
    spec = importlib.util.spec_from_file_location(_CORE, root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CORE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_CORE, None)
        raise
    return mod


def _slash(raw: str) -> str:
    core = _load_core()
    try:
        home = core.compat.current_home()
        arg = (raw or "").strip().lower()
        if arg in ("release", "force"):
            core.control.force_hand_back(home)
            return "Control returned to the agent."
        st = core.control.status(home, None)
    except core.compat.Incompatible as exc:
        return str(exc)
    r, ls, rq = st["readiness"], st["lease"], st["request"]
    lines = [f"Handoff {st['version']}: {'ready' if r['ready'] else 'not set up (run `hermes handoff setup`)'}",
             f"Control: {ls['holder']}" + (f" ({ls['reason']})" if ls.get("reason") else "")]
    if rq:
        lines.append(f"Agent request: {rq['reason']} [{rq['status']}]")
    lines.append("Take over from the dashboard: Screen tab. `/handoff release` returns control to the agent.")
    return "\n".join(lines)


def _on_session_start(**kwargs) -> None:
    """First agent session after install: wire the plugin into hermes-webui if it is on this host (idempotent, quiet)."""
    import threading

    def work() -> None:
        try:
            core = _load_core()
            if core.webui.state_dir() is None or not core.webui.enabled():
                return
            core.webui.integrate(core.compat.current_home())
        except Exception:  # noqa: BLE001 - never let a helper break a chat session
            pass
    threading.Thread(target=work, name="handoff-webui", daemon=True).start()


def register(ctx) -> None:
    core = _load_core()
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_tool(name=core.tool.NAME, toolset="handoff", schema=core.tool.SCHEMA, handler=core.tool.handle,
                      check_fn=core.tool.available, emoji="\U0001F590", description=core.tool.SCHEMA["description"][:200])
    ctx.register_cli_command(name="handoff", help="Take over the agent's screen: status, setup, release",
                             setup_fn=core.cli.setup_parser, handler_fn=core.cli.run)
    ctx.register_command("handoff", _slash, description="Show who controls the agent's screen; `/handoff release` hands it back")
    skills = Path(__file__).resolve().parent / "skills"
    if skills.is_dir() and hasattr(ctx, "register_skill"):
        for child in sorted(skills.iterdir()):
            md = child / "SKILL.md"
            if child.is_dir() and md.exists():
                ctx.register_skill(child.name, md)
