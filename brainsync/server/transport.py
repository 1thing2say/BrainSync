"""The BrainSync server's WebSocket and HTTP front door, on a background thread.

    with Transport(port=8765, token=token, static_files={"index.html": page},
                   health=session.health, catchup=session.catchup) as transport:
        transport.send(phase("run", True))           # broadcast to every joined client
        for event in transport.events():             # connect, message, disconnect
            if event.kind == "message" and event.msg["type"] == "onset":
                ...
            transport.send_to(event.conn_id, scan_request(4))

One port serves both HTTP and WebSocket (PROTOCOL-v3 section 1). ``GET /`` serves
``static_files["index.html"]``, ``GET /static/<name>`` serves the file of that exact name in
``static_files`` (nothing else on disk is reachable), ``GET /health`` serves ``health()`` as
JSON, and ``GET /ws`` upgrades to WebSocket. Every other path gets 404.

The transport handles everything the session should not have to think about: the token, the
hello-first rule, the hello timeout, the Origin check, who is the controller, what a viewer
may send, ``pong`` replies, malformed and unknown messages, and the keepalive ping. It knows
nothing about decoding. The session sees three kinds of Event: "connect" once a hello was
accepted, "message" for each valid message afterwards, and "disconnect" exactly once per
connected client.

The rules, in the order a connection meets them:

- A browser's Origin must be the Chrome extension, a page on localhost or 127.0.0.1, the
  server's own address, or match an ``origins`` pattern. Anything else gets HTTP 403 and a
  note on stderr. A request without Origin (the Python client, native apps) is let in, since
  the token is the real gate.
- The first frame must be a hello. Any other frame closes the connection with 4003, and no
  hello within ``hello_timeout_s`` closes it with 4008.
- A wrong or missing token gets ``error bad_token`` and close 4001. The token is compared in
  constant time and never printed. ``token=None`` turns auth off, which is allowed only on a
  loopback host.
- A hello that ``protocol.parse_hello`` rejects gets that error (``bad_hello`` or
  ``too_many_slots``) as fatal and close 1008.
- There is one controller at a time. Another controller's hello gets ``error busy`` and close
  4002, unless it sends ``takeover: true``: then the old controller is closed with 4000
  ("replaced") and the new one takes its place. A hello whose ``client.instance`` equals the
  current controller's is the same client reconnecting after its old socket died on its side
  only (a Wi-Fi change, a proxy that keeps the upstream open), so it replaces the old
  connection the same way instead of being refused as busy.
- ``catchup(conn_id, role, hello)`` runs on the asyncio thread. Its messages go out and the
  connection joins the broadcast set in the same step, without yielding to the loop, so a
  client never misses a broadcast and never gets one before its catchup.
- A viewer may send only ping and bye; anything else gets ``error not_allowed``. A ping from
  anyone gets a ``pong`` and is also queued, so the session can estimate the clock offset. A
  bye is queued and the connection is then closed (1000). Bad or unknown messages get
  ``error protocol`` (not fatal) and are not queued. Binary frames are ignored.
- A frame over ``max_frame`` bytes closes the connection with 1009.
- Every ``ping_s`` (20 s) each joined client gets ``{"type": "ping"}``, because Chrome stops
  an MV3 service worker after 30 s without events. Separately, WebSocket ping frames go out
  every ``WS_PING_INTERVAL_S`` and a connection that has not answered within
  ``WS_PING_TIMEOUT_S`` is closed, so a dead controller frees its place within about 20 s.

Events never carry the token: the connect Event's ``hello`` has ``token=None`` and its
``msg`` is ``Hello.to_dict()``. websockets is imported only by start().
"""
from __future__ import annotations

import asyncio
import email.utils
import hmac
import http
import ipaddress
import itertools
import math
import queue
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .protocol import (CLIENT_TYPES, CLOSE_BAD_TOKEN, CLOSE_BUSY, CLOSE_HELLO_FIRST,
                       CLOSE_HELLO_TIMEOUT, CLOSE_REPLACED, DEFAULT_PORT, MAX_FRAME, PING,
                       VIEWER_TYPES, Hello, ProtocolError, dumps, error, parse_frame,
                       parse_hello, pong, validate_client)

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request, Response

HOST = "127.0.0.1"
PING_S = 20.0
HELLO_TIMEOUT_S = 10.0
WS_PING_INTERVAL_S = 10.0    # WebSocket-level keepalive: a half-open socket is found within
WS_PING_TIMEOUT_S = 10.0     # interval + timeout (the websockets default is 20 + 20 s)
CLOSE_BAD_HELLO = 1008       # policy violation: the hello was refused for another reason
CLOSE_INTERNAL = 1011
CLOSE_BYE = 1000

# Chrome extension ids are 32 letters a-p; pages on this machine may use any port.
LOCAL_ORIGINS = re.compile(r"chrome-extension://[a-p]{32}"
                           r"|https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?")
_ORIGIN = re.compile(r"(?P<scheme>https?)://(?P<host>\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)"
                     r"(:(?P<port>\d{1,5}))?")
_STATIC_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8"}


def _note(text: str) -> None:
    print(f"transport: {text}", file=sys.stderr, flush=True)


def _short(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[:n - 3] + "..."


def is_loopback(host: str) -> bool:
    """True for "localhost" and loopback IP addresses (127.0.0.0/8, ::1)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def origin_allowed(origin: str | None, host_header: str | None = None,
                   patterns: Sequence[re.Pattern[str]] = (), bind_host: str | None = None) -> bool:
    """True when a request with this Origin may connect (PROTOCOL-v3 section 1).

    Allowed: no Origin at all, the Chrome extension, http(s) pages on localhost, 127.0.0.1 or
    [::1], any origin a pattern fully matches, and the server's own address. "Own address"
    means the Origin's host and port equal the request's Host header, and that host is an IP
    address or the bind host. Other names, a cloudflared tunnel's among them, are refused even
    when they match Host, because a DNS rebinding page would match too; add them with --origin
    (deploy/jetson/cloudflare/brainsync-tunnel.sh passes its tunnel's address that way).
    """
    if origin is None:
        return True
    if LOCAL_ORIGINS.fullmatch(origin) or any(p.fullmatch(origin) for p in patterns):
        return True
    m = _ORIGIN.fullmatch(origin)
    if m is None or not host_header:
        return False
    host = m["host"].lower()
    netloc = host if m["port"] is None else f"{host}:{m['port']}"
    if netloc != host_header.strip().lower():
        return False
    return _is_ip(host) or (bind_host is not None and host == bind_host.lower())


@dataclass(frozen=True)
class Event:
    """Something that happened on a connection, for the session's main loop.

    kind is "connect" (a hello was accepted; ``msg`` is the hello as a dict without the
    token, ``hello`` the parsed hello with ``token=None``), "message" (``msg`` is the
    validated message) or "disconnect" (``close_code`` is the close code, None if unknown).
    ``t`` is the server's time.time() when the frame arrived or the connection ended.
    """

    kind: str
    conn_id: int
    role: str
    msg: dict | None
    hello: Hello | None
    t: float
    close_code: int | None = None


class Transport:
    """WebSocket + HTTP server on a daemon thread; send, send_to and close are thread-safe."""

    def __init__(self, host: str = HOST, port: int = DEFAULT_PORT, *, token: str | None,
                 origins: Sequence[str] = (),
                 static_files: Mapping[str, Path | str] = MappingProxyType({}),
                 health: Callable[[], dict] | None = None,
                 catchup: Callable[[int, str, Hello], list[dict]] | None = None,
                 max_slots: int = 4, ping_s: float = PING_S,
                 hello_timeout_s: float = HELLO_TIMEOUT_S, max_frame: int = MAX_FRAME) -> None:
        if not isinstance(host, str) or not host:
            raise ValueError(f"host must be an address such as 127.0.0.1, got {host!r}")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError(f"port must be 0..65535, got {port!r}")
        if token is None:
            if not is_loopback(host):
                raise ValueError(f"--no-auth is allowed only with a loopback host (127.0.0.1, "
                                 f"::1 or localhost), not {host!r}; drop --no-auth so clients "
                                 f"need the token from --token-file")
        elif not isinstance(token, str) or not token:
            raise ValueError("the token must be a non-empty string; pass token=None (and a "
                             "loopback host) to turn auth off")
        for name, value in (("ping_s", ping_s), ("hello_timeout_s", hello_timeout_s)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be a positive number of seconds, got {value!r}")
        for name, value in (("max_slots", max_slots), ("max_frame", max_frame)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        if health is not None and not callable(health):
            raise ValueError("health must be a function returning a dict, or None")
        if catchup is not None and not callable(catchup):
            raise ValueError("catchup must be a function (conn_id, role, hello) -> list of "
                             "messages, or None")
        patterns = []
        for pattern in origins:
            try:
                patterns.append(re.compile(pattern))
            except (re.error, TypeError) as exc:
                raise ValueError(f"--origin {pattern!r} is not a valid regular expression "
                                 f"({exc}); it must fully match an Origin such as "
                                 f"https://example\\.com") from None
        files: dict[str, Path] = {}
        for name, path in static_files.items():
            if (not isinstance(name, str) or _STATIC_NAME.fullmatch(name) is None
                    or ".." in name.split("/")):
                raise ValueError(f"static file name {name!r} must be a relative name such as "
                                 f"\"app.js\" (letters, digits, . _ - and /, no ..)")
            path = Path(path)
            if not path.is_file():
                raise ValueError(f"static file {name!r} points to {str(path)!r}, which is not "
                                 f"a file; check the path")
            files[name] = path

        self.host = host
        self.port = port               # the bound port once started (useful with port 0)
        self.token = token
        self.max_slots = max_slots
        self.ping_s = float(ping_s)
        self.hello_timeout_s = float(hello_timeout_s)
        self.max_frame = max_frame
        self._patterns = tuple(patterns)
        self._files = files
        self._health = health
        self._catchup = catchup
        self._ids = itertools.count(1)
        self._conns: dict[int, ServerConnection] = {}               # every open connection
        self._joined: dict[int, tuple[ServerConnection, str]] = {}  # after hello: (conn, role)
        self._controller: int | None = None
        self._instances: dict[int, str] = {}    # controller conn_id -> its hello.client.instance
        self._n_viewers = 0
        self._events: queue.Queue[Event] = queue.Queue()
        # Guards _controller and _n_viewers for readers. Each change to them queues its
        # connect or disconnect Event under the same lock, so a reader that sees the new
        # controller or viewer count always finds that Event in events() too.
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping: asyncio.Event | None = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None

    # -- life cycle --------------------------------------------------------------------------

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"ws://{host}:{self.port}/ws"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 10.0) -> Transport:
        """Start listening; returns once the socket is bound. OSError if it cannot bind."""
        if self._thread is not None:
            raise RuntimeError("transport already started")
        try:
            import websockets.asyncio.server  # noqa: F401  (fail here, not on the thread)
        except ImportError as exc:
            raise ImportError(f"the server needs websockets: pip install websockets ({exc})"
                              ) from exc
        self._thread = threading.Thread(target=self._main, name="brainsync-transport",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"the server on {self.url} did not start within {timeout:g} s")
        if self._failure is not None:
            self._thread.join(timeout)
            exc = self._failure
            raise OSError(f"cannot listen on {self.url}: {exc} (is another brainsync.server "
                          f"running? pick another port with --port)") from exc
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Close every connection (1001) and the server, and join the thread (idempotent).

        Messages sent before stop() still go out first: the loop runs its callbacks in the
        order they were scheduled.
        """
        thread, loop, stopping = self._thread, self._loop, self._stopping
        if thread is None or not thread.is_alive() or loop is None or stopping is None:
            return
        try:
            loop.call_soon_threadsafe(stopping.set)
        except RuntimeError:   # loop already closed
            pass
        thread.join(timeout)

    def __enter__(self) -> Transport:
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
        async with serve(self._handle, self.host, self.port, process_request=self._http,
                         max_size=self.max_frame, ping_interval=WS_PING_INTERVAL_S,
                         ping_timeout=WS_PING_TIMEOUT_S) as server:
            if server.sockets:
                self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            pinger = asyncio.create_task(self._ping())
            await self._stopping.wait()
            pinger.cancel()
        self._joined.clear()
        self._conns.clear()
        self._instances.clear()
        with self._lock:
            self._controller, self._n_viewers = None, 0

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(self.ping_s)
            self._broadcast_now(dumps(PING))

    # -- HTTP --------------------------------------------------------------------------------

    def _http(self, conn: ServerConnection, request: Request) -> Response | None:
        """Route a request: 403 for a refused Origin, files, health, the /ws upgrade, or 404."""
        origins = request.headers.get_all("Origin")
        origin = origins[0] if len(origins) == 1 else None
        if len(origins) > 1 or not origin_allowed(origin, request.headers.get("Host"),
                                                  self._patterns, self.host):
            shown = _short(", ".join(origins))
            _note(f"refused a request from origin {shown!r}: only the BrainPoint extension, "
                  f"pages on this machine and --origin patterns may connect")
            return self._response(403, b"origin not allowed\n")
        path = request.path.split("?", 1)[0]
        if path == "/ws":
            return None                      # websockets does the upgrade (426 without one)
        if path == "/health":
            try:
                body = dumps(self._health() if self._health is not None
                             else self._default_health()).encode()
            except Exception as exc:         # a broken health() must not kill the server
                _note(f"health() failed: {type(exc).__name__}: {exc}")
                return self._response(500, b"health check failed\n")
            return self._response(200, body, "application/json")
        if path == "/":
            name: str | None = "index.html"
        elif path.startswith("/static/"):
            name = path[len("/static/"):]
        else:
            name = None
        file = self._files.get(name) if name else None   # exact names only: no traversal
        if file is None:
            return self._response(404, b"not found\n")
        try:
            body = file.read_bytes()
        except OSError as exc:
            _note(f"cannot read {str(file)!r} for {path}: {exc}")
            return self._response(404, b"not found\n")
        ctype = CONTENT_TYPES.get(file.suffix.lower(), "application/octet-stream")
        return self._response(200, body, ctype)

    def _default_health(self) -> dict[str, Any]:
        return {"ok": True, "board": "unknown", "controller": self.controller is not None,
                "viewers": self.n_viewers}

    @staticmethod
    def _response(status: int, body: bytes,
                  ctype: str = "text/plain; charset=utf-8") -> Response:
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        headers = Headers([("Date", email.utils.formatdate(usegmt=True)),
                           ("Connection", "close"), ("Content-Length", str(len(body))),
                           ("Content-Type", ctype), ("Cache-Control", "no-store"),
                           ("X-Content-Type-Options", "nosniff")])
        return Response(status, http.HTTPStatus(status).phrase, headers, body)

    # -- outgoing ----------------------------------------------------------------------------

    @staticmethod
    def _encode(msg: dict[str, Any]) -> str:
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise ValueError(f"a message must be a dict with a string \"type\", got "
                             f"{_short(repr(msg))}")
        return dumps(msg)     # fail in the caller's thread: TypeError, or ValueError for NaN

    def _call(self, fn: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is None or not self.running:
            return                        # not started or stopped: nobody to send to
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:              # loop closed during shutdown
            pass

    def send(self, msg: dict[str, Any]) -> None:
        """Broadcast one message to every joined connection (controller and viewers)."""
        self._call(self._broadcast_now, self._encode(msg))

    def send_to(self, conn_id: int, msg: dict[str, Any]) -> None:
        """Send one message to one joined connection; dropped if it has gone."""
        self._call(self._send_to_now, conn_id, self._encode(msg))

    def close(self, conn_id: int, code: int, reason: str = "") -> None:
        """Close one connection; a joined one gets its disconnect Event with ``code`` at once."""
        if (isinstance(code, bool) or not isinstance(code, int)
                or not (code == 1000 or 3000 <= code <= 4999)):
            raise ValueError(f"close code must be 1000 or 3000..4999, got {code!r}")
        if len(str(reason).encode()) > 123:
            raise ValueError("the close reason must be at most 123 bytes of UTF-8")
        self._call(self._retire, conn_id, code, str(reason))

    def _broadcast_now(self, text: str) -> None:
        from websockets.asyncio.server import broadcast

        broadcast([conn for conn, _ in self._joined.values()], text)

    def _send_to_now(self, conn_id: int, text: str) -> None:
        from websockets.asyncio.server import broadcast

        entry = self._joined.get(conn_id)
        if entry is not None:
            broadcast([entry[0]], text)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _retire(self, conn_id: int, code: int, reason: str) -> None:
        """On the loop: leave the broadcast set, queue the disconnect Event, start the close."""
        entry = self._joined.pop(conn_id, None)
        if entry is not None:
            conn, role = entry
            self._recount(Event("disconnect", conn_id, role, None, None, time.time(), code))
        else:
            conn = self._conns.get(conn_id)
        if conn is not None:
            self._spawn(conn.close(code, reason))

    def _recount(self, event: Event | None = None, *, controller: int | None = None,
                 instance: str | None = None) -> None:
        """Bring the controller and viewer count in line with ``_joined`` and queue ``event``,
        all under one lock. ``controller`` names a newly joined controller (with its
        ``instance``, if it sent one)."""
        with self._lock:
            if controller is not None:
                self._controller = controller
                if instance is not None:
                    self._instances[controller] = instance
            if self._controller is not None and self._controller not in self._joined:
                self._controller = None
            for gone in [c for c in self._instances if c not in self._joined]:
                del self._instances[gone]
            self._n_viewers = sum(1 for _, role in self._joined.values() if role == "viewer")
            if event is not None:
                self._events.put(event)

    # -- incoming ----------------------------------------------------------------------------

    async def _handle(self, conn: ServerConnection) -> None:
        from websockets.exceptions import ConnectionClosed

        conn_id = next(self._ids)
        self._conns[conn_id] = conn
        try:
            if await self._join(conn, conn_id):
                async for text in conn:
                    if conn_id not in self._joined:      # retired: replaced, closed or bye
                        await conn.wait_closed()         # let the retiring close code win
                        break
                    await self._receive(conn, conn_id, text)
        except ConnectionClosed:
            pass
        finally:
            self._conns.pop(conn_id, None)
            entry = self._joined.pop(conn_id, None)
            if entry is not None:
                self._recount(Event("disconnect", conn_id, entry[1], None, None,
                                    time.time(), _close_code(conn)))

    async def _refuse(self, conn: ServerConnection, code: str | None, message: str,
                      close_code: int, reason: str) -> bool:
        """Send a fatal error (when ``code`` is given), close, and return False."""
        from websockets.exceptions import ConnectionClosed

        try:
            if code is not None:
                await conn.send(dumps(error(code, message, fatal=True)))
            await conn.close(close_code, reason)
        except ConnectionClosed:
            pass
        return False

    async def _join(self, conn: ServerConnection, conn_id: int) -> bool:
        """Wait for the hello and admit the connection; False when it was refused."""
        try:
            text = await asyncio.wait_for(conn.recv(), self.hello_timeout_s)
        except TimeoutError:
            return await self._refuse(conn, "protocol", f"no hello within "
                                      f"{self.hello_timeout_s:g} s; send hello as the first "
                                      f"message", CLOSE_HELLO_TIMEOUT, "hello timeout")
        t = time.time()
        try:
            msg = parse_frame(text)
        except ProtocolError:
            msg = None
        if msg is None or msg["type"] != "hello":
            return await self._refuse(conn, "protocol", "the first message must be a hello "
                                      "with the token", CLOSE_HELLO_FIRST, "hello first")
        if self.token is not None:
            sent = msg.get("token")
            if not (isinstance(sent, str)
                    and hmac.compare_digest(sent.encode(), self.token.encode())):
                _note(f"refused a hello with a wrong or missing token from "
                      f"{_peer(conn)}")
                return await self._refuse(conn, "bad_token", "wrong token; paste the token "
                                          "the server printed at startup",
                                          CLOSE_BAD_TOKEN, "bad token")
        try:
            hello = parse_hello(msg, self.max_slots)
        except ProtocolError as exc:
            return await self._refuse(conn, exc.code, exc.message, CLOSE_BAD_HELLO,
                                      "bad hello")
        hello = replace(hello, token=None)       # the token stops here
        role = hello.role
        old = self._controller if role == "controller" else None
        instance = hello.client.get("instance") or None
        resume = (old is not None and not hello.takeover and instance is not None
                  and self._instances.get(old) == instance)
        if old is not None and not hello.takeover and not resume:
            return await self._refuse(conn, "busy", "another controller is connected; "
                                      "reconnect with \"takeover\": true to replace it, or "
                                      "connect as a viewer", CLOSE_BUSY, "busy")
        # From here to the join nothing awaits, so no broadcast can slip in between.
        try:
            texts = [dumps(m) for m in (self._catchup(conn_id, role, hello)
                                        if self._catchup is not None else [])]
        except Exception as exc:
            _note(f"catchup failed for connection {conn_id}: {type(exc).__name__}: {exc}")
            return await self._refuse(conn, "internal", "the server failed to set up this "
                                      "connection; see its log", CLOSE_INTERNAL,
                                      "internal error")
        if old is not None:
            if resume:
                _note(f"connection {conn_id} is controller {old} reconnecting; the old "
                      f"connection is closed")
            else:
                _note(f"connection {conn_id} took over from controller {old}")
            self._retire(old, CLOSE_REPLACED, "replaced")
        from websockets.asyncio.server import broadcast

        for out in texts:
            broadcast([conn], out)
        self._joined[conn_id] = (conn, role)
        self._recount(Event("connect", conn_id, role, hello.to_dict(), hello, t),
                      controller=conn_id if role == "controller" else None, instance=instance)
        return True

    async def _receive(self, conn: ServerConnection, conn_id: int, text: str | bytes) -> None:
        t = time.time()
        role = self._joined[conn_id][1]
        if isinstance(text, (bytes, bytearray, memoryview)):
            return                               # binary frames are ignored
        try:
            msg = parse_frame(text)
            kind = msg["type"]
            if (role == "viewer" and kind in CLIENT_TYPES and kind != "hello"
                    and kind not in VIEWER_TYPES):
                await conn.send(dumps(error(
                    "not_allowed", f"a viewer may send only ping and bye, not {kind!r}; "
                                   f"connect with role \"controller\" to control the session")))
                return
            msg = validate_client(msg)
        except ProtocolError as exc:
            await conn.send(dumps(error(exc.code, exc.message)))
            return
        self._events.put(Event("message", conn_id, role, msg, None, t))
        if msg["type"] == "ping":
            await conn.send(dumps(pong(msg["ping_id"], msg["t_client"], time.time())))
        elif msg["type"] == "bye":
            self._retire(conn_id, CLOSE_BYE, "bye")

    # -- state -------------------------------------------------------------------------------

    @property
    def controller(self) -> int | None:
        """The controller's connection id, or None."""
        with self._lock:
            return self._controller

    @property
    def n_viewers(self) -> int:
        with self._lock:
            return self._n_viewers

    def events(self) -> list[Event]:
        """Events since the last call, oldest first."""
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out


def _close_code(conn: ServerConnection) -> int | None:
    """The code the client closed with, else the one the server sent (1009 for a big frame)."""
    proto = conn.protocol
    frame = proto.close_rcvd or proto.close_sent
    if frame is not None:
        return int(frame.code)
    code = conn.close_code
    return None if code is None else int(code)


def _peer(conn: ServerConnection) -> str:
    try:
        addr = conn.remote_address
    except Exception:
        return "an unknown address"
    return str(addr[0]) if isinstance(addr, tuple) and addr else str(addr)
