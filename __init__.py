"""Takeover: let a human take over the agent's screen, do the step only they can do, and hand control back.

Surfaces registered here: the ``request_takeover`` tool, ``hermes takeover`` and ``/takeover``, a system prompt section and two hooks, and a bundled skill.
The dashboard half (Screen tab, routes, live view) is in ``dashboard/``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CORE = "hermes_takeover_core"


def _load_core():
    """Import ``takeover_core`` by path under one fixed module name, shared with dashboard/plugin_api.py."""
    if _CORE in sys.modules:
        return sys.modules[_CORE]
    root = Path(__file__).resolve().parent / "takeover_core"
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
    lines = [f"Takeover {st['version']}: {'ready' if r['ready'] else 'not set up (run `hermes takeover setup`)'}",
             f"Control: {ls['holder']}" + (f" ({ls['reason']})" if ls.get("reason") else "")]
    if rq:
        lines.append(f"Agent request: {rq['reason']} [{rq['status']}]")
    lines.append("Take over from the Screen page (hermes-webui or the dashboard). `/takeover release` returns control to the agent.")
    return "\n".join(lines)


def _on_session_start(**kwargs) -> None:
    """Belt and braces for the first-run bootstrap (it normally already ran when the plugin was loaded)."""
    try:
        _load_core().bootstrap.kick()
    except Exception:  # noqa: BLE001 - never let a helper break a chat session
        pass


def _pre_tool_call(**kwargs):
    """Pause browser / screen tools while a human holds the screen (see takeover_core/fence.py)."""
    try:
        return _load_core().fence.check(**kwargs)
    except Exception:  # noqa: BLE001
        return None


def _transform_tool_result(**kwargs):
    """Attach a "call request_takeover" hint to browser results that show a CAPTCHA or code prompt (takeover_core/nudge.py)."""
    try:
        return _load_core().nudge.transform(**kwargs)
    except Exception:  # noqa: BLE001
        return None


def register(ctx) -> None:
    core = _load_core()
    core.bootstrap.kick()         # first run right after install: WebUI Screen page + service, screen packages (once)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_tool(name=core.tool.NAME, toolset="takeover", schema=core.tool.SCHEMA, handler=core.tool.handle,
                      check_fn=core.tool.available, emoji="\U0001F590", description=core.tool.SCHEMA["description"][:200])
    ctx.register_cli_command(name="takeover", help="Take over the agent's screen: status, setup, release",
                             setup_fn=core.cli.setup_parser, handler_fn=core.cli.run)
    ctx.register_command("takeover", _slash, description="Who controls the agent's screen; `/takeover release` hands it back")
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    if hasattr(ctx, "register_system_prompt_section"):        # older Hermes: the tool description still says when to call it
        ctx.register_system_prompt_section(core.nudge.SECTION_ID, core.nudge.guidance, max_chars=2000)
    skills = Path(__file__).resolve().parent / "skills"
    if skills.is_dir() and hasattr(ctx, "register_skill"):
        for child in sorted(skills.iterdir()):
            md = child / "SKILL.md"
            if child.is_dir() and md.exists():
                ctx.register_skill(child.name, md)
