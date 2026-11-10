"""First run, right after ``hermes plugins install``: the hermes-webui Screen page appears by itself.

Hermes calls ``register(ctx)`` in every process that loads the plugin (including cron and the messaging gateway).
``kick`` does a cheap check there and, only when something is missing, starts ONE detached helper process
(``bootstrap_run.py``) that installs / upgrades the WebUI Screen page and starts the Takeover service, when hermes-webui
is on this host. That is unprivileged and reversible (a copy into WebUI's extensions folder plus a loopback process).

Nothing privileged ever starts from a plugin load. The screen packages (root, through Hermes's Bot Desktop installer)
and ``browser.headed`` are set up on the first deliberate use instead: opening the Screen page (WebUI or dashboard), the
agent's first ``request_takeover`` (``spawn_setup``, detached, so the tool call returns at once), or
``hermes takeover setup``.

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
    """What a plugin load may fix: only the unprivileged WebUI half. (``setup`` is always False here: see the module
    docstring; it stays in the result for older callers.)"""
    out = {"setup": False, "webui": False}
    try:
        sd = webui.state_dir()
        if sd is not None and webui.enabled():
            ext = webui.installed_version(sd)
            h = webui.health(0.4)
            out["webui"] = (ext is None or webui.older(ext) or h is None or webui.older(h.get("version")))
    except Exception:  # noqa: BLE001
        pass
    return out


def spawn(home, mode: str = "webui") -> Optional[int]:
    """Start the detached helper. ``mode``: ``webui`` (plugin load) or ``setup`` (a deliberate first use)."""
    env = webui.child_env(home)                    # no credentials in a detached helper; PYTHONPATH reaches Hermes
    with open(state.data_dir(home) / "bootstrap.log", "ab") as log:
        p = subprocess.Popen([sys.executable, "-u", str(PLUGIN_ROOT / "bootstrap_run.py"), mode], env=env,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    return p.pid


def spawn_setup(home) -> bool:
    """First-run setup in a detached process (survives a short-lived agent process). False when it is already running."""
    if setup.setup_state(home)["state"] == "running":
        return False
    spawn(home, "setup")
    return True


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
        if needs(home)["webui"]:
            spawn(home, "webui")
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def run(home, mode: str = "webui") -> Dict[str, Any]:
    """The helper process body. ``webui``: the Screen page + service only. ``setup``: first-run setup (started by a
    deliberate use, never by a plugin load), which also wires hermes-webui at its end."""
    if mode == "setup":
        out: Dict[str, Any] = {"mode": "setup", "at": time.time(), "setup": setup.run_setup(home).get("state")}
        print(out, flush=True)
        return out
    fh = open(state.data_dir(home) / "bootstrap.lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"skipped": "another bootstrap is running"}
    try:
        n = needs(home)
        out = {"needs": n, "at": time.time()}
        if webui.state_dir() is not None and webui.enabled():
            out["webui"] = webui.integrate(home)
        print(out, flush=True)
        return out
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
