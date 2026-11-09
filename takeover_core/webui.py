"""hermes-webui integration: install the Screen page into WebUI and keep its loopback sidecar running.

hermes-webui is a different app from ``hermes dashboard``. It loads *extensions* (front-end files from
``<state>/extensions/<id>/``) and reaches backends only through a loopback sidecar behind its authenticated proxy.
This module does both halves for the user, with no manual steps:

* ``install_extension``  copy the bundled extension into WebUI's extension dir and record it exactly like the
                         WebUI gallery does (loads on the next page render; no WebUI restart)
* ``ensure_sidecar``     start ``takeover_core.sidecar`` detached if it is not already answering on 127.0.0.1:17901
* ``integrate``          detect WebUI; do both; called automatically right after install and by ``hermes takeover setup``

WebUI state dir: ``$HERMES_WEBUI_STATE_DIR`` (set whenever WebUI itself runs the agent), else ``~/.hermes/webui``.
WebUI's sidecar consent (Settings -> Extensions -> Diagnostics) is granted ONCE by the page itself through WebUI's
own endpoint (enabling the plugin is the consent; opt out with ``webui_auto_approve: false``); WebUI still requires
authentication to be on (token-v1). A later revoke sticks (``AUTOCONSENT_RECORD``).
"""
from __future__ import annotations

import json
import os
import re
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
AUTOCONSENT_RECORD = "takeover-autoconsent.json"       # in WebUI's state dir: survives reinstalls / upgrades


# ── detection ─────────────────────────────────────────────────────────────────
def state_dir() -> Optional[Path]:
    env = os.environ.get("HERMES_WEBUI_STATE_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    cand = Path.home() / ".hermes" / "webui"
    return cand if cand.is_dir() else None


def port() -> int:
    try:
        return int(os.environ.get("TAKEOVER_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def vt(v: Optional[str]) -> tuple:
    """Version tuple for comparisons ('0.10.0' > '0.9.1'); unknown = oldest."""
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except (TypeError, ValueError):
        return (0,)


def older(v: Optional[str]) -> bool:
    """True when ``v`` (an installed / running copy) is older than this plugin. Several profiles may carry different
    versions of Takeover: only ever upgrade, never let an older copy replace a newer one."""
    return vt(v) < vt(__version__)


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
    final = sd / "extensions" / PLUGIN_ID
    dest = sd / "extensions" / f".{PLUGIN_ID}.tmp-{os.getpid()}"     # build aside, then swap: a killed process never
    shutil.rmtree(dest, ignore_errors=True)                           # leaves WebUI with half an extension
    shutil.copytree(SRC, dest)
    shutil.copytree(NOVNC, dest / "assets" / "novnc")          # one copy of noVNC in the repo; copied at install
    man_path = dest / "manifest.json"                          # the sidecar port is baked into the manifest (TAKEOVER_PORT)
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    for ext in manifest.get("extensions", []):
        if isinstance(ext.get("sidecar"), dict):
            ext["sidecar"]["origin"] = f"http://127.0.0.1:{port()}"
    man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (dest / "assets" / "autoconsent.json").write_text(json.dumps(autoconsent_marker(sd)), encoding="utf-8")
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    old = sd / "extensions" / f".{PLUGIN_ID}.old-{os.getpid()}"
    if final.exists():
        os.replace(final, old)
    os.replace(dest, final)
    shutil.rmtree(old, ignore_errors=True)
    dest = final
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
def listener_uid(p: int) -> Optional[int]:
    """UID owning the socket LISTENing on 127.0.0.1:<p> (from /proc/net/tcp), None if nobody / unknown."""
    want = "0100007F:%04X" % p
    try:
        with open("/proc/net/tcp", encoding="ascii") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if len(f) > 7 and f[1] == want and f[3] == "0A":
                    return int(f[7])
    except (OSError, ValueError, StopIteration):
        pass
    return None


def port_owner_ok() -> bool:
    """Is whatever listens on our port running as this user? Another local user squatting the fixed loopback port
    would otherwise receive the keystrokes WebUI's proxy forwards (passwords typed during a takeover)."""
    uid = listener_uid(port())
    return uid is None or uid == os.getuid()


def health(timeout: float = 1.5) -> Optional[Dict[str, Any]]:
    """The running sidecar's /health, but only if it is ours (same UID)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port()}/health", timeout=timeout) as r:
            d = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None
    if d.get("service") != PLUGIN_ID or not port_owner_ok():
        return None
    return d


def _is_our_sidecar(pid: int) -> bool:
    """Only ever signal a process that is ours AND is a Takeover sidecar (pids get reused; a pidfile goes stale)."""
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return False
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"sidecar_run.py" in fh.read()
    except (OSError, ValueError):
        return False


_SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|COOKIE|SESSION|AUTH|PRIVATE", re.I)
_KEEP = {"HERMES_HOME", "HERMES_WEBUI_STATE_DIR", "TAKEOVER_PORT", "XAUTHORITY"}


def child_env(home) -> Dict[str, str]:
    """Environment for the detached helpers: the parent's, minus anything that looks like a credential (they are often
    started from an agent or gateway process that holds provider API keys the service never needs)."""
    env = {k: v for k, v in os.environ.items() if k in _KEEP or not _SECRETISH.search(k)}
    env.update(HERMES_HOME=str(home), TAKEOVER_PORT=str(port()))
    sd = state_dir()
    if sd:
        env["HERMES_WEBUI_STATE_DIR"] = str(sd)
    agent_dir = str(Path(compat.load().hc.__file__).resolve().parent)
    env["PYTHONPATH"] = agent_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _pidfile(home) -> Path:
    return state.data_dir(home) / "sidecar.json"


def stop_sidecar(home) -> bool:
    h = health()
    pid = (h or {}).get("pid") or (state.read_json(_pidfile(home)) or {}).get("pid")   # the live one first
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid() or not _is_our_sidecar(pid):   # never ourselves (setup can run inside it), never a stranger
        with __import__("contextlib").suppress(OSError):
            _pidfile(home).unlink()
        return False
    try:
        os.kill(pid, 15)
    except OSError:
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
        if h and not restart and not older(h.get("version")):
            # one service serves every profile (?profile=), so whoever started it is fine; only an older copy is replaced
            return {"running": True, "started": False, "version": h.get("version")}
        if not port_owner_ok():
            return {"running": False, "started": False,
                    "error": f"127.0.0.1:{port()} is held by another user's process; set TAKEOVER_PORT to a free port"}
        if h:                                                   # older version, or an explicit restart
            stop_sidecar(home)
        with open(state.data_dir(home) / "sidecar.log", "ab") as log:
            proc = subprocess.Popen([sys.executable, "-u", str(PLUGIN_ROOT / "sidecar_run.py")], env=child_env(home),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
        for _ in range(60):
            h = health(0.5)
            if h:
                if h.get("pid") == proc.pid:                   # record it only once it is the one that owns the port
                    state.write_json(_pidfile(home), {"pid": proc.pid, "port": port(), "started": time.time(),
                                                      "version": __version__})
                return {"running": True, "started": True, "version": h.get("version")}
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        return {"running": False, "started": True, "error": "the sidecar did not come up; see takeover/sidecar.log"}
    except Exception as exc:  # noqa: BLE001
        return {"running": False, "started": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


# ── the whole thing ───────────────────────────────────────────────────────────
def status(home) -> Dict[str, Any]:
    sd = state_dir()
    h = health(0.5)
    return {"detected": bool(sd), "state_dir": str(sd) if sd else None, "extension": installed_version(sd),
            "sidecar": bool(h), "sidecar_version": (h or {}).get("version"), "enabled": enabled(),
            "serves_this_profile": bool(h) and (bool(h.get("multi_profile")) or not h.get("home_id")
                                                or h["home_id"] == home_id(home))}


def auto_approve_allowed(home=None) -> bool:
    """``plugins.entries.takeover.settings.webui_auto_approve`` (default on): enabling the plugin is your consent for
    its Screen page to use its own service, so the page grants WebUI's sidecar consent itself, ONCE per WebUI."""
    return bool(compat.setting("webui_auto_approve", True, home=home))


def autoconsent_marker(sd: Path) -> Dict[str, Any]:
    """What the page reads (``assets/autoconsent.json``): approve once, unless already done (so a later revoke sticks)."""
    import secrets
    if not auto_approve_allowed() or state.read_json(sd / AUTOCONSENT_RECORD):
        return {"auto": False}
    return {"auto": True, "nonce": secrets.token_hex(8)}


def _marker(sd: Path) -> Path:
    return sd / "extensions" / PLUGIN_ID / "assets" / "autoconsent.json"


def mark_autoconsent_used(sd: Path, nonce: Optional[str] = None) -> bool:
    """Record that the one automatic approval happened and switch the marker off, so a later revoke sticks.

    The service calls this itself on the first authenticated request it gets through WebUI's proxy (that request
    only exists because consent was granted), so it does not depend on the page's follow-up call; the page's
    ``/api/autoconsent-used`` must present the marker's own nonce."""
    import hmac
    m = state.read_json(_marker(sd)) or {}
    if not m.get("auto"):
        return False
    if nonce is not None and not (isinstance(nonce, str) and hmac.compare_digest(nonce.encode(), str(m.get("nonce", "")).encode())):
        return False
    state.write_json(sd / AUTOCONSENT_RECORD, {"used_nonce": m.get("nonce"), "at": time.time()})
    _marker(sd).write_text(json.dumps({"auto": False}), encoding="utf-8")
    return True


def enabled() -> bool:
    """``plugins.entries.takeover.settings.webui_integration`` (default on): set false to never touch hermes-webui."""
    return bool(compat.setting("webui_integration", True))


def integrate(home, force: bool = False) -> Dict[str, Any]:
    """Idempotent. Without WebUI on this host (or when switched off) it does nothing (and says so)."""
    sd = state_dir()
    if not sd or (not force and not enabled()):
        return {"detected": bool(sd), "disabled": bool(sd)}
    out: Dict[str, Any] = {"detected": True, "state_dir": str(sd)}
    if force or older(installed_version(sd)) or not (sd / "extensions" / PLUGIN_ID / "manifest.json").exists():
        out["extension"] = install_extension(sd)
    out["sidecar"] = ensure_sidecar(home, restart=force)
    return out


def orphan_cleanup() -> None:
    """Called by the sidecar on its way out after the plugin was removed: take the WebUI extension with it."""
    try:
        uninstall_extension()
    except Exception:  # noqa: BLE001
        pass
