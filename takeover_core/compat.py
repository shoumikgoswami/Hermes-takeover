"""The only module that imports hermes-agent internals, with symbol checks.

Takeover builds on Bot Desktop (``tools.bot_desktop``): the control lease, the RFB input filter and the
screen runtime. Those are internals without a frozen API, so every symbol we use is verified once and a
mismatch surfaces as ``Incompatible`` with a message the UI/CLI can show, instead of a stack trace mid-takeover.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

REQUIRED = {
    "lease": ["acquire", "release", "get", "public_view", "assert_agent_may_act", "HumanHasControl", "HUMAN", "AGENT"],
    "rfb_filter": ["RfbClientFilter"],
    "runtime": ["start", "stop", "status", "is_running", "is_supported_host", "missing_binaries", "install_command",
                "state_dir", "touch_activity", "in_sandbox", "sandbox_screen_running", "open_rfb_stream"],
    "install": ["install_packages", "NO_SUDO"],
}


class Incompatible(RuntimeError):
    pass


_lock = threading.Lock()
_bd = None


def load() -> SimpleNamespace:
    """``bd.lease / bd.rfb_filter / bd.runtime / bd.install / bd.hc`` (hermes_constants)."""
    global _bd
    if _bd is not None:
        return _bd
    with _lock:
        if _bd is not None:
            return _bd
        try:
            import hermes_constants as hc
            from tools.bot_desktop import install, lease, rfb_filter, runtime
        except Exception as exc:  # noqa: BLE001 - any import failure means "this Hermes is not compatible"
            raise Incompatible(f"Bot Desktop is not available in this Hermes ({type(exc).__name__}: {exc}). "
                               "Update Hermes: hermes update") from exc
        mods = {"lease": lease, "rfb_filter": rfb_filter, "runtime": runtime, "install": install}
        missing = [f"{m}.{s}" for m, syms in REQUIRED.items() for s in syms if not hasattr(mods[m], s)]
        for s in ("get_hermes_home", "set_hermes_home_override", "reset_hermes_home_override"):
            if not hasattr(hc, s):
                missing.append(f"hermes_constants.{s}")
        if missing:
            raise Incompatible("This Hermes changed under Takeover: missing " + ", ".join(missing)
                               + ". Update the plugin: hermes plugins update takeover")
        _bd = SimpleNamespace(hc=hc, **mods)
        return _bd


def current_home() -> str:
    return str(load().hc.get_hermes_home())


@contextmanager
def home_scope(home):
    """Run a block against ``home`` (the dashboard serves several profiles; the lease and the screen are per home)."""
    hc = load().hc
    token = hc.set_hermes_home_override(Path(str(home)))
    try:
        yield
    finally:
        hc.reset_hermes_home_override(token)


def setting(key: str, default=None):
    """``plugins.entries.takeover.settings.<key>`` from config.yaml (read-only, any process)."""
    try:
        from hermes_cli.config import load_config_readonly
        entry = ((load_config_readonly().get("plugins") or {}).get("entries") or {}).get("takeover") or {}
        return (entry.get("settings") or {}).get(key, default)
    except Exception:  # noqa: BLE001
        return default
