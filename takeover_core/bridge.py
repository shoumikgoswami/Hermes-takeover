"""RFB <-> WebSocket bridge with lease-gated input.

Client->server bytes go through ``RfbClientFilter(allow_input)``: key/pointer/clipboard/resize messages are dropped
unless the lease says THIS viewer is the human holder (re-read at most every 0.25 s, so a takeover from another
process lands quickly). noVNC's view-only flag is only UX; this is the gate. Payload bytes, tickets and full viewer
ids are never logged.

Close codes (the page maps them): 4000 control taken by another viewer, 4001 screen not running / stopped,
4401 bad ticket, 4403 origin not allowed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Tuple

from . import compat

log = logging.getLogger("takeover.bridge")

CLOSE_EVICTED, CLOSE_SCREEN_OFF, CLOSE_BAD_TICKET, CLOSE_BAD_ORIGIN = 4000, 4001, 4401, 4403
LEASE_POLL = 0.25
ACTIVITY_EVERY = 30.0
CHUNK = 64 * 1024


class ScreenOff(Exception):
    pass


async def open_upstream(home) -> Tuple[Any, Any, Any]:
    """``(reader, writer, relay)`` for this home's Xvnc: its unix socket, or a docker/ssh relay when the screen
    lives inside the terminal sandbox (``relay`` is that Popen, else None)."""
    rt = compat.load().runtime
    try:
        with compat.home_scope(home):
            if rt.sandbox_screen_running():
                relay = rt.open_rfb_stream()
            else:
                sock = Path(str(home)) / "bot-desktop" / "rfb.sock"
                if not sock.exists():
                    raise ScreenOff("rfb.sock missing")
                r, w = await asyncio.open_unix_connection(str(sock))
                return r, w, None
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), relay.stdout)
        wt, wp = await loop.connect_write_pipe(lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()), relay.stdin)
        return reader, asyncio.StreamWriter(wt, wp, None, loop), relay
    except ScreenOff:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ScreenOff(type(exc).__name__) from exc


class LeaseWatch:
    def __init__(self, bd, home: str, viewer_id: str):
        self.bd, self.home, self.viewer_id = bd, str(home), viewer_id
        self._at, self._lease, self.had_control = 0.0, None, False

    def lease(self):
        now = time.monotonic()
        if self._lease is None or now - self._at >= LEASE_POLL:
            self._lease = self.bd.lease.get(profile_key=self.home)
            self._at = now
            if self._holds(self._lease):
                self.had_control = True
        return self._lease

    def _holds(self, lease) -> bool:
        return lease.holder == self.bd.lease.HUMAN and lease.viewer_id == self.viewer_id

    def allow_input(self) -> bool:
        return self._holds(self.lease())

    def evicted(self) -> bool:
        """Held control during this connection and a DIFFERENT human holds it now (a plain hand-back to the
        agent is not an eviction)."""
        lease = self.lease()
        if lease.holder != self.bd.lease.HUMAN:
            self.had_control = False
            return False
        return self.had_control and lease.viewer_id != self.viewer_id


async def pump(ws, home, viewer_id: str, short: str) -> None:
    """``ws`` is an accepted Starlette WebSocket."""
    bd = compat.load()
    try:
        reader, writer, relay = await open_upstream(home)
    except ScreenOff:
        await ws.close(code=CLOSE_SCREEN_OFF)
        return
    watch = LeaseWatch(bd, str(home), viewer_id)
    filt = bd.rfb_filter.RfbClientFilter(watch.allow_input)
    activity = Path(str(home)) / "bot-desktop" / "activity"
    code = {"v": 1000}

    async def c2s():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is None:
                code["v"] = 1003          # RFB is binary
                return
            out = filt.feed(data)         # ValueError on unframeable bytes -> the viewer reconnects
            if out:
                writer.write(out)
                await writer.drain()

    async def s2c():
        while True:
            chunk = await reader.read(CHUNK)
            if not chunk:
                code["v"] = CLOSE_SCREEN_OFF
                return
            await ws.send_bytes(chunk)    # awaiting the send is the backpressure toward Xvnc

    async def watcher():
        last = 0.0
        while True:
            await asyncio.sleep(LEASE_POLL)
            if watch.evicted():
                code["v"] = CLOSE_EVICTED
                return
            now = time.monotonic()
            if now - last >= ACTIVITY_EVERY:      # a watched screen is not idle: don't let auto-stop take it
                last = now
                try:
                    activity.touch()
                except OSError:
                    pass

    tasks = [asyncio.create_task(f()) for f in (c2s, s2c, watcher)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if isinstance(exc, ValueError):
                code["v"] = 1008
                log.info("viewer %s: closed on bad RFB framing", short)
            elif exc is not None and type(exc).__name__ not in ("WebSocketDisconnect", "ConnectionResetError",
                                                                "BrokenPipeError", "RuntimeError"):
                code["v"] = 1011
                log.info("viewer %s: bridge error %s", short, type(exc).__name__)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
        if relay is not None:
            try:
                relay.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            await ws.close(code=code["v"])
        except Exception:  # noqa: BLE001
            pass
