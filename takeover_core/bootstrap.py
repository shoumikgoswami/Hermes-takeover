"""First run, right after ``hermes plugins install``: no new chat, no manual command.

Hermes calls ``register(ctx)`` in every process that loads the plugin (the running WebUI / gateway picks a fresh install
up immediately). ``kick`` does a cheap check there and, only when something is missing, starts ONE detached helper
process (``bootstrap_run.py``) that:

1. runs first-run setup once (screen packages through Hermes's Bot Desktop installer, ``browser.headed``), when
   ``auto_install`` allows it and it was never attempted;
2. installs / upgrades the WebUI Screen page and starts the Takeover service, when hermes-webui is on this host.

Detached because the process that loaded the plugin may be a two-second ``hermes ...`` command. Guarded so it never
runs from ``hermes plugins doctor`` / ``validate`` (they import the plugin from its source tree with a temporary home),
from tests, or with ``TAKEOVER_NO_BOOTSTRAP=1``.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import compat, setup, state, webui

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_kicked = False
_lock = threading.Lock()


def installed_copy(home) -> bool:
    """True only for the copy Hermes installed into this home (not a source checkout, not doctor/validate)."""
    try:
        PLUGIN_ROOT.relative_to((Path(str(home)) / "plugins").resolve())
        return True
    except (ValueError, OSError):
        return False


def needs(home) -> Dict[str, bool]:
    """What is missing. Cheap: a couple of file reads and (only before the first setup) a package probe."""
    out = {"setup": False, "webui": False}
    try:
        s = setup.setup_state(home)
        if s["auto_allowed"] and not s["attempted"] and s["state"] != "running":
            r = setup.readiness(home)
            out["setup"] = bool(r["supported"] and (not r["ready"] or r["headed"] is False))
    except Exception:  # noqa: BLE001 - incompatible Hermes / not Linux: nothing to set up
        pass
    try:
        sd = webui.state_dir()
        if sd is not None and webui.enabled():
            ext = webui.installed_version(sd)
            h = webui.health(0.4)
            out["webui"] = (ext is None or webui.older(ext) or h is None or webui.older(h.get("version")))
    except Exception:  # noqa: BLE001
        pass
    return out


def spawn(home) -> Optional[int]:
    env = dict(os.environ, HERMES_HOME=str(home))
    agent_dir = str(Path(compat.load().hc.__file__).resolve().parent)
    env["PYTHONPATH"] = agent_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log = open(state.data_dir(home) / "bootstrap.log", "ab")
    p = subprocess.Popen([sys.executable, "-u", str(PLUGIN_ROOT / "bootstrap_run.py")], env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, close_fds=True)
    return p.pid


def kick(home=None) -> bool:
    """Called from ``register()`` / session start. Returns True when a helper was started. Never raises."""
    global _kicked
    if os.environ.get("TAKEOVER_NO_BOOTSTRAP"):
        return False
    with _lock:
        if _kicked:
            return False
        _kicked = True
    try:
        home = home or compat.current_home()
        if not installed_copy(home):
            return False

        # Inline, not in a thread: the loading process may be a short `hermes ...` command that exits before a thread
        # would get to spawn. The check is a few file reads and one loopback request (milliseconds); the slow part runs
        # in the detached helper.
        n = needs(home)
        if n["setup"] or n["webui"]:
            spawn(home)
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def run(home) -> Dict[str, Any]:
    """The helper process body. One at a time per home (file lock)."""
    fh = open(state.data_dir(home) / "bootstrap.lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"skipped": "another bootstrap is running"}
    try:
        n = needs(home)
        out: Dict[str, Any] = {"needs": n, "at": time.time()}
        if n["setup"]:
            out["setup"] = setup.run_setup(home).get("state")       # also wires hermes-webui at its end
        if webui.state_dir() is not None and webui.enabled():
            out["webui"] = webui.integrate(home)
        print(out, flush=True)
        return out
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
