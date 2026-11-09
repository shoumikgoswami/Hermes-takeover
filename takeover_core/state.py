"""Small cross-process state under ``<HERMES_HOME>/plugin-data/takeover/`` (Hermes's per-plugin data root, which
survives ``hermes plugins update``/``remove``; same path ``plugins.plugin_storage.plugin_data_dir`` gives, but for an
explicit home because the dashboard and the WebUI service serve a home that is not necessarily the active one). The
tool runs in the agent process and the dashboard / WebUI service in others, so files are the shared truth (same as the
Bot Desktop lease); writes that read-modify-write go through a file lock.

* ``request.json``  the agent's "I need you" request (reason, status)
* ``setup.json``    first-run setup progress / outcome
* ``secret``        0600 HMAC key for stream-viewer ids
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

REQUEST_TTL = 3600.0       # a pending request nobody acted on for an hour is stale
WAITER_ALIVE = 15.0        # a waiting agent call refreshes its heartbeat more often than this


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
    """Per-home HMAC key. Created once, atomically (write aside, publish with a create-or-fail link), so concurrent
    first users in several processes all end up with the same key instead of overwriting each other's."""
    p = data_dir(home) / "secret"
    for _ in range(3):
        try:
            b = p.read_bytes()
            if len(b) >= 32:
                return b
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".secret.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(32))
            os.chmod(tmp, 0o600)
            try:
                os.link(tmp, p)                  # fails if another process published first: then read theirs
            except FileExistsError:
                pass
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    return p.read_bytes()


@contextlib.contextmanager
def _locked(home):
    fh = open(data_dir(home) / "request.lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


# ── the agent's request ───────────────────────────────────────────────────────
def get_request(home) -> Optional[Dict[str, Any]]:
    r = read_json(data_dir(home) / "request.json")
    if not r:
        return None
    if r.get("status") == "pending" and time.time() - float(r.get("created", 0)) > REQUEST_TTL:
        r["status"] = "expired"
    return r


def create_request(home, reason: str) -> Dict[str, Any]:
    """Post a request (used by ``wait_seconds=0`` and tests). Joins a live one instead of replacing it."""
    return join_request(home, reason)[0]


def join_request(home, reason: str, waiter: Optional[str] = None):
    """``(request, waiter_id)``. One request card per profile: a second agent (or the same agent calling again to keep
    waiting) joins the live request instead of overwriting it, so nobody misses the human's dismissal or hand-back."""
    reason = reason.strip()[:300]
    waiter = waiter or secrets.token_hex(4)
    now = time.time()
    with _locked(home):
        r = get_request(home)
        if r and r.get("status") in ("pending", "taken"):
            if reason and reason != r.get("reason") and reason not in (r.get("also") or []):
                r["also"] = ((r.get("also") or []) + [reason])[-4:]
        else:
            r = {"id": secrets.token_hex(6), "reason": reason, "status": "pending", "created": now}
        r.setdefault("waiters", {})[waiter] = now
        write_json(data_dir(home) / "request.json", r)
    return r, waiter


def told(home, rid: str, who: str) -> bool:
    """Has this conversation already been sent to tell the user about request ``rid``?"""
    r = get_request(home)
    return bool(r and r.get("id") == rid and who in (r.get("told") or []))


def mark_told(home, rid: str, who: str) -> None:
    with _locked(home):
        r = get_request(home)
        if r and r.get("id") == rid and who not in (r.get("told") or []):
            r["told"] = ((r.get("told") or []) + [who])[-8:]
            write_json(data_dir(home) / "request.json", r)


def heartbeat(home, rid: str, waiter: str) -> None:
    with _locked(home):
        r = get_request(home)
        if r and r.get("id") == rid and r.get("status") in ("pending", "taken"):
            r.setdefault("waiters", {})[waiter] = time.time()
            write_json(data_dir(home) / "request.json", r)


def leave_request(home, rid: str, waiter: str, status: Optional[str] = None) -> Optional[str]:
    """This waiter stops waiting. ``status`` (expired / cancelled) is applied only if no other live waiter remains, so
    one agent giving up never takes the card away from another that is still waiting. Returns the status applied."""
    with _locked(home):
        r = get_request(home)
        if not r or r.get("id") != rid:
            return None
        ws = {k: v for k, v in (r.get("waiters") or {}).items() if k != waiter and time.time() - float(v) < WAITER_ALIVE}
        r["waiters"] = ws
        applied = None
        if status and not ws and r.get("status") in ("pending", "taken"):
            r["status"] = applied = status
        write_json(data_dir(home) / "request.json", r)
        return applied


def update_request(home, rid: Optional[str], **fields) -> Optional[Dict[str, Any]]:
    """Update the current request (only if ``rid`` still matches, so a stale caller cannot clobber a newer one)."""
    with _locked(home):
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
        v = {k: r.get(k) for k in ("id", "reason", "status", "created")}
        if r.get("also"):
            v["reason"] = "; ".join([r.get("reason") or ""] + list(r["also"]))[:400]
        return v
    return None
