"""hermes-webui integration: install the Screen page into WebUI and keep its loopback sidecar running.

hermes-webui is a different app from ``hermes dashboard``. It loads *extensions* (front-end files from
``<state>/extensions/<id>/``) and reaches backends only through a loopback sidecar behind its authenticated proxy.
This module does both halves for the user, with no manual steps beyond WebUI's own one-click consent:

* ``install_extension``  copy the bundled extension into WebUI's extension dir and record it exactly like the
                         WebUI gallery does (loads on the next page render; no WebUI restart)
* ``ensure_sidecar``     start ``handoff_core.sidecar`` detached if it is not already answering on 127.0.0.1:17901
* ``integrate``          detect WebUI; do both; called automatically on first agent session and by ``hermes handoff setup``

WebUI state dir: ``$HERMES_WEBUI_STATE_DIR`` (set whenever WebUI itself runs the agent), else ``~/.hermes/webui``.
Consent (Settings -> Extensions -> Diagnostics -> Loopback sidecars -> Handoff -> Approve) is WebUI's security
decision and is deliberately left to the user; it requires WebUI authentication to be on (token-v1).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from . import PLUGIN_ID, __version__, compat, state
from .sidecar import DEFAULT_PORT, home_id

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SRC = PLUGIN_ROOT / "webui"
NOVNC = PLUGIN_ROOT / "dashboard" / "dist" / "novnc"
INSTALL_MANIFEST = "extension-install-manifest.json"


# ── detection ─────────────────────────────────────────────────────────────────
def state_dir() -> Optional[Path]:
    env = os.environ.get("HERMES_WEBUI_STATE_DIR")
    if env:
        return Path(env).expanduser()
    cand = Path.home() / ".hermes" / "webui"
    return cand if cand.is_dir() else None


def port() -> int:
    try:
        return int(os.environ.get("HANDOFF_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def installed_version(sd: Optional[Path] = None) -> Optional[str]:
    sd = sd or state_dir()
    if not sd:
        return None
    entry = (state.read_json(sd / INSTALL_MANIFEST) or {}).get("installed", {}).get(PLUGIN_ID)
    return entry.get("version") if isinstance(entry, dict) else None


# ── extension files ───────────────────────────────────────────────────────────
def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def install_extension(sd: Optional[Path] = None) -> Dict[str, Any]:
    sd = sd or state_dir()
    if not sd:
        return {"installed": False, "reason": "hermes-webui state directory not found"}
    dest = sd / "extensions" / PLUGIN_ID
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest)
    shutil.copytree(NOVNC, dest / "assets" / "novnc")          # one copy of noVNC in the repo; copied at install
    man_path = dest / "manifest.json"                          # the sidecar port is baked into the manifest (HANDOFF_PORT)
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    for ext in manifest.get("extensions", []):
        if isinstance(ext.get("sidecar"), dict):
            ext["sidecar"]["origin"] = f"http://127.0.0.1:{port()}"
    man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    man = state.read_json(sd / INSTALL_MANIFEST) or {}
    if not isinstance(man.get("installed"), dict):
        man = {"version": 1, "installed": {}}
    man["installed"][PLUGIN_ID] = {"version": __version__, "files": files,
                                   "installed_at": datetime.now(timezone.utc).isoformat()}
    _write_json_atomic(sd / INSTALL_MANIFEST, man)
    return {"installed": True, "dir": str(dest), "files": len(files)}


def uninstall_extension(sd: Optional[Path] = None) -> bool:
    sd = sd or state_dir()
    if not sd:
        return False
    shutil.rmtree(sd / "extensions" / PLUGIN_ID, ignore_errors=True)
    man = state.read_json(sd / INSTALL_MANIFEST) or {}
    if isinstance(man.get("installed"), dict) and man["installed"].pop(PLUGIN_ID, None) is not None:
        _write_json_atomic(sd / INSTALL_MANIFEST, man)
    return True


# ── sidecar process ───────────────────────────────────────────────────────────
def health(timeout: float = 1.5) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port()}/health", timeout=timeout) as r:
            d = json.loads(r.read())
            return d if d.get("service") == PLUGIN_ID else None
    except Exception:  # noqa: BLE001
        return None


def _pidfile(home) -> Path:
    return state.data_dir(home) / "sidecar.json"


def stop_sidecar(home) -> bool:
    info = state.read_json(_pidfile(home)) or {}
    pid = info.get("pid")
    h = health()
    pid = pid or (h or {}).get("pid")
    if not pid or int(pid) == os.getpid():            # never stop ourselves (setup can run inside the sidecar)
        return False
    try:
        os.kill(int(pid), 15)
    except (OSError, ValueError):
        return False
    for _ in range(30):
        if not health(0.3):
            return True
        time.sleep(0.1)
    return False


def ensure_sidecar(home, restart: bool = False) -> Dict[str, Any]:
    """Make sure a sidecar is answering. Returns ``{running, started, version, ...}``. Never raises."""
    try:
        h = health()
        if h and not restart and h.get("version") == __version__:
            other = bool(h.get("home_id")) and h["home_id"] != home_id(home)
            # a sidecar serves ONE Hermes home (the control lease lives there). The first one wins; an explicit
            # `hermes handoff webui` takes over for this profile.
            return {"running": True, "started": False, "version": h.get("version"), "other_profile": other}
        if h:                                                   # stale version, or an explicit restart
            stop_sidecar(home)
        sd = state_dir()
        env = dict(os.environ)
        env.update(HERMES_HOME=str(home), HANDOFF_PORT=str(port()))
        if sd:
            env["HERMES_WEBUI_STATE_DIR"] = str(sd)
        agent_dir = str(Path(compat.load().hc.__file__).resolve().parent)
        env["PYTHONPATH"] = agent_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        log = open(state.data_dir(home) / "sidecar.log", "ab")
        proc = subprocess.Popen([sys.executable, "-u", str(PLUGIN_ROOT / "sidecar_run.py")], env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True, close_fds=True)
        state.write_json(_pidfile(home), {"pid": proc.pid, "port": port(), "started": time.time(), "version": __version__})
        for _ in range(60):
            h = health(0.5)
            if h:
                return {"running": True, "started": True, "version": h.get("version")}
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        return {"running": False, "started": True, "error": "the sidecar did not come up; see handoff/sidecar.log"}
    except Exception as exc:  # noqa: BLE001
        return {"running": False, "started": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


# ── the whole thing ───────────────────────────────────────────────────────────
def status(home) -> Dict[str, Any]:
    sd = state_dir()
    h = health(0.5)
    return {"detected": bool(sd), "state_dir": str(sd) if sd else None, "extension": installed_version(sd),
            "sidecar": bool(h), "sidecar_version": (h or {}).get("version"), "enabled": enabled(),
            "serves_this_profile": bool(h) and (not h.get("home_id") or h["home_id"] == home_id(home))}


def enabled() -> bool:
    """``plugins.entries.handoff.settings.webui_integration`` (default on): set false to never touch hermes-webui."""
    return bool(compat.setting("webui_integration", True))


def integrate(home, force: bool = False) -> Dict[str, Any]:
    """Idempotent. Without WebUI on this host (or when switched off) it does nothing (and says so)."""
    sd = state_dir()
    if not sd or (not force and not enabled()):
        return {"detected": bool(sd), "disabled": bool(sd)}
    out: Dict[str, Any] = {"detected": True, "state_dir": str(sd)}
    if force or installed_version(sd) != __version__ or not (sd / "extensions" / PLUGIN_ID / "manifest.json").exists():
        out["extension"] = install_extension(sd)
    out["sidecar"] = ensure_sidecar(home, restart=force)
    return out


def orphan_cleanup() -> None:
    """Called by the sidecar on its way out after the plugin was removed: take the WebUI extension with it."""
    try:
        uninstall_extension()
    except Exception:  # noqa: BLE001
        pass
