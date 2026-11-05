"""Small cross-process state under ``<HERMES_HOME>/plugin-data/takeover/`` (Hermes's per-plugin data root, which
survives ``hermes plugins update``/``remove``; same path ``plugins.plugin_storage.plugin_data_dir`` gives, but for an
explicit home because the dashboard and the WebUI service serve a home that is not necessarily the active one) (the tool runs in the agent process, the
dashboard in another; files are the shared truth, same as the Bot Desktop lease).

* ``request.json``  the agent's "I need you" request (reason, status)
* ``setup.json``    first-run setup progress / outcome
* ``secret``        0600 HMAC key for stream-viewer ids
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

REQUEST_TTL = 3600.0       # a pending request nobody acted on for an hour is stale


def data_dir(home) -> Path:
    d = Path(str(home)) / "plugin-data" / "takeover"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def secret(home) -> bytes:
    p = data_dir(home) / "secret"
    try:
        b = p.read_bytes()
        if len(b) >= 32:
            return b
    except OSError:
        pass
    b = secrets.token_bytes(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b)
    return b


# ── the agent's request ───────────────────────────────────────────────────────
def get_request(home) -> Optional[Dict[str, Any]]:
    r = read_json(data_dir(home) / "request.json")
    if not r:
        return None
    if r.get("status") == "pending" and time.time() - float(r.get("created", 0)) > REQUEST_TTL:
        r["status"] = "expired"
    return r


def create_request(home, reason: str) -> Dict[str, Any]:
    r = {"id": secrets.token_hex(6), "reason": reason[:300], "status": "pending", "created": time.time()}
    write_json(data_dir(home) / "request.json", r)
    return r


def update_request(home, rid: Optional[str], **fields) -> Optional[Dict[str, Any]]:
    """Update the current request (only if ``rid`` still matches, so a stale caller cannot clobber a newer one)."""
    r = get_request(home)
    if not r or (rid is not None and r.get("id") != rid):
        return None
    r.update(fields)
    write_json(data_dir(home) / "request.json", r)
    return r


def request_view(home) -> Optional[Dict[str, Any]]:
    """The request as the UI should show it: only live ones (pending / taken)."""
    r = get_request(home)
    if r and r.get("status") in ("pending", "taken"):
        return {k: r.get(k) for k in ("id", "reason", "status", "created")}
    return None
