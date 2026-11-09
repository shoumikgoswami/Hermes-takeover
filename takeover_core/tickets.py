"""Viewer ids and one-time stream tickets for the dashboard WebSocket.

HTTP routes sit behind the dashboard's auth; a WebSocket upgrade does not (HTTP middleware never runs for
it), so the stream is admitted only with a ticket minted through an authenticated route:
32 random bytes, 30 s TTL, single use, bound to one viewer and one Hermes home, in memory.

viewer_id = <16 random bytes hex>.<hmac(secret, random)[:32]>: stateless to validate, so it survives a
dashboard restart while a human is holding the lease.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

from . import state

TTL = 30.0
MAX_LIVE = 64
_lock = threading.Lock()
_tickets: Dict[str, Tuple[str, str, float]] = {}      # ticket -> (viewer_id, home, expires)


def _sig(home, rnd: str) -> str:
    return hmac.new(state.secret(home), rnd.encode(), hashlib.sha256).hexdigest()[:32]


def new_viewer_id(home) -> str:
    rnd = secrets.token_hex(16)
    return f"{rnd}.{_sig(home, rnd)}"


def valid_viewer_id(home, viewer_id: object) -> bool:
    if not isinstance(viewer_id, str) or viewer_id.count(".") != 1 or len(viewer_id) > 80:
        return False
    rnd, sig = viewer_id.split(".")
    return hmac.compare_digest(sig.encode("utf-8", "replace"), _sig(home, rnd).encode())   # str compare raises on non-ASCII


def short(viewer_id: Optional[str]) -> str:
    """Log-safe handle; a full viewer id is a capability and is never logged."""
    return hashlib.sha256((viewer_id or "").encode()).hexdigest()[:8]


def mint(viewer_id: str, home) -> Optional[str]:
    now = time.monotonic()
    with _lock:
        for t in [t for t, v in _tickets.items() if v[2] <= now]:
            del _tickets[t]
        if len(_tickets) >= MAX_LIVE:
            return None
        t = secrets.token_urlsafe(32)
        _tickets[t] = (viewer_id, str(home), now + TTL)
        return t


def consume(ticket: Optional[str]) -> Optional[Tuple[str, str]]:
    """``(viewer_id, home)`` for a valid unused ticket (burning it), else None."""
    if not ticket:
        return None
    with _lock:
        entry = _tickets.pop(ticket, None)
    if entry is None or entry[2] <= time.monotonic():
        return None
    return entry[0], entry[1]
