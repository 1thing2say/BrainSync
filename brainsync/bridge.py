"""Localhost WebSocket bridge between the decoder and the browser overlay.

    with Bridge(config_message=config_message(cfg)) as bridge:   # ws://127.0.0.1:8765
        bridge.send(phase_message("wait", False, None, "Click Start in the browser"))
        hello = bridge.wait_hello(120.0)
        bridge.send(scroll_message("down", 1))
        for event in bridge.events():          # bye, scrolled, frames
            ...

The server runs an asyncio loop on a daemon thread, so the decoder loop stays a plain
blocking loop. Every message is one JSON text frame holding an object with a "type". Each
client gets the config as soon as it connects, then the current phase (a "wait" phase
until the decoder sends one) and the EEG board's state, so a browser tab that opens or
reloads mid-run catches up. Everything sent is broadcast to all clients. A {"type":
"ping"} goes out every ``ping_s`` (20 s) because Chrome stops an MV3 extension's service
worker after 30 s without events, and the websockets library's protocol-level pings do not
reach the extension's code. Malformed or unknown client messages are noted on stderr and
ignored. websockets (the ``reels`` extra) is imported only by start().

Only the extension and pages served from this machine may connect: a handshake whose
Origin is neither chrome-extension://<id> nor http(s)://127.0.0.1 or localhost (any port)
is refused with 403. Without that check any web page open in the browser could connect,
say bye and end the run, or send a hello at 60 Hz before the real overlay did. Clients
that send no Origin (not a browser: the tests, websocat) are let in.

The run decodes only while some tab shows the squares: every client says whether its
squares are in view (accepted, on Reels, not stopped, and the tab visible: not a background
tab, another Space or a covered window) in its hello and again in a "view" message each
time that changes. ``showing`` is True while any connected client's latest word is "in
view"; a hello without the field (an older extension) counts as in view. A client that
disconnects stops counting.

Server -> client:
    config  {"type": "config", "name", "waveform", "depth", "refresh_hz",
             "refresh_tolerance_hz", "targets": [{"label", "freq", "phase", "x", "y",
             "size"}]}  on connect; x, y are the square's centre and size its side, in
            screen heights from the viewport centre with y up (PsychoPy height units);
            phase in radians; depth in (0, 1] is the modulation depth (luminance
            0.5 +/- depth/2), absent means 1
    phase   {"type": "phase", "phase": wait|calibrate|run|stop, "flicker": bool,
             "seconds": number|null, "text"}  flicker false: squares still at 0.5;
            stop: hide the squares and show the text
    cue     {"type": "cue", "target": -1|0|1}  box around the committed target (-1: none)
    scroll  {"type": "scroll", "dir": up|down, "id": increasing int}
    status  {"type": "status", "text"}  optional HUD text
    eeg     {"type": "eeg", "state": unknown|absent|connecting|connected|streaming|no-data|
             error|unused, "text"}  the EEG board, for the HUD's EEG line
    ping    {"type": "ping"}  every 20 s, ignored by clients
Client -> server:
    hello    {"type": "hello", "agent", "url", "refresh_hz", "dropped", "accepted": true,
              "showing": bool}  once the viewer accepted the photosensitivity warning and
             refresh was measured
    view     {"type": "view", "showing": bool}  the squares came into or went out of view
    frames   {"type": "frames", "refresh_hz", "dropped"}  every 5 s while flickering
    scrolled {"type": "scrolled", "id", "ok": bool, "method"}  one per scroll
    bye      {"type": "bye"}  the viewer pressed Escape
"""
from __future__ import annotations

import asyncio
import json
import math
import queue
import re
import sys
import threading
from typing import TYPE_CHECKING, Any

from .stimulus.config import StimulusConfig

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request, Response

HOST = "127.0.0.1"
PORT = 8765
PING_S = 20.0
EVENT_TYPES = frozenset({"bye", "scrolled", "frames", "view"})
PHASES = ("wait", "calibrate", "run", "stop")
EEG_STATES = ("unknown", "absent", "connecting", "connected", "streaming", "no-data", "error",
              "unused")
# Chrome extension ids are 32 letters a-p; the mock page is served by http.server.
ORIGINS = re.compile(r"chrome-extension://[a-p]{32}|https?://(127\.0\.0\.1|localhost)(:\d+)?")


# -- protocol messages ----------------------------------------------------------------------

def config_message(cfg: StimulusConfig) -> dict[str, Any]:
    """The "config" message: targets with centre (x, y) and side in screen heights, y up."""
    return {"type": "config", "name": cfg.name, "waveform": cfg.waveform, "depth": cfg.depth,
            "refresh_hz": cfg.display.refresh_hz,
            "refresh_tolerance_hz": cfg.display.refresh_tolerance_hz,
            "targets": [{"label": t.label, "freq": t.freq, "phase": t.phase,
                         "x": t.pos[0], "y": t.pos[1], "size": cfg.size}
                        for t in cfg.targets]}


def phase_message(phase: str, flicker: bool, seconds: float | None = None,
                  text: str = "") -> dict[str, Any]:
    """A "phase" message; ``flicker`` False draws the squares still at luminance 0.5."""
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
    return {"type": "phase", "phase": phase, "flicker": bool(flicker),
            "seconds": None if seconds is None else float(seconds), "text": str(text)}


def cue_message(target: int) -> dict[str, Any]:
    """Cue box around ``target``; -1 clears it."""
    return {"type": "cue", "target": int(target)}


def scroll_message(direction: str, scroll_id: int) -> dict[str, Any]:
    return {"type": "scroll", "dir": direction, "id": int(scroll_id)}


def status_message(text: str) -> dict[str, Any]:
    return {"type": "status", "text": str(text)}


def eeg_message(state: str, text: str = "") -> dict[str, Any]:
    """The EEG board's state for the overlay's EEG line."""
    if state not in EEG_STATES:
        raise ValueError(f"EEG state must be one of {EEG_STATES}, got {state!r}")
    return {"type": "eeg", "state": state, "text": str(text)}


DEFAULT_PHASE = phase_message("wait", False, None, "Waiting for the decoder...")
DEFAULT_EEG = eeg_message("unknown")


def parse_client_message(text: str | bytes) -> dict[str, Any] | None:
    """A client message as a dict, or None (with the reason on stderr) when malformed."""
    if isinstance(text, bytes):
        _note(f"ignored a binary frame of {len(text)} bytes")
        return None
    try:
        msg = json.loads(text)
    except (ValueError, RecursionError):   # RecursionError: arrays nested ~100000 deep
        _note(f"ignored a message that is not JSON: {text[:80]!r}")
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        _note(f"ignored a message without a string \"type\": {text[:80]!r}")
        return None
    return msg


def origin_allowed(origin: str | None) -> bool:
    """True for the extension, a page served from this machine, or no Origin at all."""
    return origin is None or ORIGINS.fullmatch(origin) is not None


def _note(text: str) -> None:
    print(f"bridge: {text}", file=sys.stderr, flush=True)


# -- server ---------------------------------------------------------------------------------

class Bridge:
    """WebSocket server on a background thread; ``send`` is safe from any thread."""

    def __init__(self, host: str = HOST, port: int = PORT,
                 config_message: dict[str, Any] | None = None, ping_s: float = PING_S) -> None:
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError(f"port must be 0..65535, got {port!r}")
        if not math.isfinite(ping_s) or ping_s <= 0:
            raise ValueError(f"ping_s must be positive, got {ping_s}")
        self.host = host
        self.port = port            # the bound port once started (useful with port 0)
        self.config = dict(config_message or {"type": "config"})
        self.ping_s = float(ping_s)
        self.phase: dict[str, Any] = dict(DEFAULT_PHASE)
        self.eeg: dict[str, Any] = dict(DEFAULT_EEG)
        self.ignored = 0            # malformed or unknown client messages
        self._clients: set[ServerConnection] = set()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._hello: dict[str, Any] | None = None
        self._hello_seen = threading.Event()
        self._views: dict[object, bool] = {}   # client -> its squares are in view
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping: asyncio.Event | None = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None

    # -- life cycle ------------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 10.0) -> Bridge:
        """Start listening; returns once the socket is bound. OSError if it cannot bind."""
        if self._thread is not None:
            raise RuntimeError("bridge already started")
        try:
            import websockets.asyncio.server  # noqa: F401  (fail here, not on the thread)
        except ImportError as exc:
            raise ImportError(f"the browser bridge needs websockets: pip install -e "
                              f"\".[reels]\" ({exc})") from exc
        self._thread = threading.Thread(target=self._main, name="brainsync-bridge",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"WebSocket server on {self.url} did not start within "
                               f"{timeout:g} s")
        if self._failure is not None:
            self._thread.join(timeout)
            exc = self._failure
            raise OSError(f"cannot listen on {self.url}: {exc} (is another brainsync.reels "
                          f"running? pick another port with --ws-port)") from exc
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Close every connection and the server, and join the thread (idempotent).

        Messages sent before stop() still go out first: the loop runs its callbacks in
        the order they were scheduled.
        """
        thread, loop, stopping = self._thread, self._loop, self._stopping
        if thread is None or not thread.is_alive() or loop is None or stopping is None:
            return
        try:
            loop.call_soon_threadsafe(stopping.set)
        except RuntimeError:   # loop already closed
            pass
        thread.join(timeout)

    def __enter__(self) -> Bridge:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:   # surfaced by start(); a daemon thread cannot raise
            if not self._ready.is_set():
                self._failure = exc
                self._ready.set()
            else:
                _note(f"server stopped: {exc}")

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        async with serve(self._handle, self.host, self.port,
                         process_request=self._check_origin) as server:
            self.port = server.sockets[0].getsockname()[1] if server.sockets else self.port
            self._ready.set()
            pinger = asyncio.create_task(self._ping())
            await self._stopping.wait()
            pinger.cancel()
        self._clients.clear()

    def _check_origin(self, conn: ServerConnection, request: Request) -> Response | None:
        origins = request.headers.get_all("Origin")
        origin = origins[0] if len(origins) == 1 else None
        if len(origins) <= 1 and origin_allowed(origin):
            return None
        _note(f"refused a connection from origin {', '.join(origins)!r}: only the "
              "BrainScroll extension and pages on this machine may connect")
        return conn.respond(403, "origin not allowed\n")

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(self.ping_s)
            self._broadcast({"type": "ping"})

    # -- outgoing --------------------------------------------------------------------------

    def send(self, msg: dict[str, Any]) -> None:
        """Broadcast one message to every client; "phase" and "eeg" are also kept for late
        joiners."""
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise ValueError(f"a message must be a dict with a string \"type\", got {msg!r}")
        text = json.dumps(msg)          # fail here, in the caller's thread, if unserialisable
        loop = self._loop
        if loop is None or not self.running:
            self._keep(dict(msg))
            return
        try:
            loop.call_soon_threadsafe(self._send_now, dict(msg), text)
        except RuntimeError:            # loop closed during shutdown
            pass

    def _send_now(self, msg: dict[str, Any], text: str) -> None:
        from websockets.asyncio.server import broadcast

        self._keep(msg)
        broadcast(self._clients, text)

    def _keep(self, msg: dict[str, Any]) -> None:
        if msg["type"] == "phase":
            self.phase = msg
        elif msg["type"] == "eeg":
            self.eeg = msg

    def _broadcast(self, msg: dict[str, Any]) -> None:
        from websockets.asyncio.server import broadcast

        broadcast(self._clients, json.dumps(msg))

    # -- incoming --------------------------------------------------------------------------

    async def _handle(self, conn: ServerConnection) -> None:
        from websockets.asyncio.server import broadcast
        from websockets.exceptions import ConnectionClosed

        # config, phase and EEG state go out and the client joins the broadcast set in one
        # step, without yielding to the loop, so no message sent meanwhile is missed
        broadcast([conn], json.dumps(self.config))
        broadcast([conn], json.dumps(self.phase))
        broadcast([conn], json.dumps(self.eeg))
        self._clients.add(conn)
        try:
            async for text in conn:
                self._receive(text, conn)
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(conn)
            with self._lock:
                self._views.pop(conn, None)

    def _receive(self, text: str | bytes, client: object = None) -> None:
        msg = parse_client_message(text)
        if msg is None:
            self.ignored += 1
            return
        kind = msg["type"]
        if kind == "hello":
            with self._lock:
                self._hello = msg
                self._views[client] = msg.get("showing") is not False
            self._events.put(msg)       # queued first: a waiter woken by the event finds it
            self._hello_seen.set()
        elif kind == "view" and not isinstance(msg.get("showing"), bool):
            self.ignored += 1
            _note(f"ignored a view message without a true/false \"showing\": {text[:80]!r}")
        elif kind in EVENT_TYPES:
            if kind == "view":
                with self._lock:
                    self._views[client] = msg["showing"]
            self._events.put(msg)
        else:
            self.ignored += 1
            _note(f"ignored a message of unknown type {kind!r}")

    @property
    def n_clients(self) -> int:
        return len(self._clients)

    @property
    def showing(self) -> bool:
        """True while some connected client has its squares in view."""
        with self._lock:
            return any(self._views.values())

    @property
    def latest_hello(self) -> dict[str, Any] | None:
        with self._lock:
            return self._hello

    def wait_hello(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The latest hello, waiting up to ``timeout`` s for the first; None on timeout."""
        self._hello_seen.wait(timeout)
        return self.latest_hello

    def events(self) -> list[dict[str, Any]]:
        """Client messages received since the last call (hello, bye, scrolled, frames, view)."""
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out
