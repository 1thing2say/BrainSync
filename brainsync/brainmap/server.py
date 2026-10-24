"""The live brain map's web server: the page plus a Server-Sent Events stream of frames.

    with MapServer(port=0) as server:          # port 0: any free port, see server.url
        server.set_hello(stream.hello())
        for msg in messages:
            server.publish(msg)                # from any thread; never blocks

Standard library only (http.server on a daemon thread, no websockets or web framework), so it
runs the same on the Jetson. Routes:

    GET /           index.html from ``root`` (default extension/brainmap/)
    GET /<file>     a file under ``root`` with a known type: no "..", no hidden names, no
                    directory listings, no symlinks out of ``root``
    GET /events     text/event-stream: the hello, then the latest status, then every
                    published message as "data: <json>"; ": ping" every 15 s when idle

Each SSE client has a bounded queue. A client that falls behind (a sleeping laptop, a
stalled tab) is dropped rather than slowing publish() or the other clients; the browser's
EventSource reconnects by itself and starts again from the hello. A dropped client's socket
is shut down, which also frees a handler thread stuck writing to it.

The stream is someone's EEG, so the server listens on 127.0.0.1 by default, sends no CORS
headers (other sites cannot read /events), and refuses requests whose Host header is not a
loopback name (403): without that check a web page could point its own domain at 127.0.0.1
(DNS rebinding) and read the stream as same-origin. The check stays on when the server is
bound to the network (``--host 0.0.0.0`` for a headless Jetson viewed from a laptop), since
a page could rebind its domain to the LAN address just as well. Exposed, it also accepts any
Host that is an IP literal (``http://192.168.1.20:8140/``: rebinding needs a DNS name, and a
browser only sends an IP literal for a URL that already points at that address), and
``allowed_hosts`` adds names such as ``jetson.local``. Whether the server is exposed is read
from the address actually bound, so ``127.0.0.2``, ``::1`` or a name that resolves to the
loopback all keep the strict loopback check.
"""
from __future__ import annotations

import collections
import ipaddress
import json
import math
import socket
import socketserver
import threading
import urllib.parse
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import paths

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "PAGE_DIR", "MapServer", "encode", "host_name",
           "is_ip_literal", "port_in_use"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8140            # BrainSwipe's mock uses 8780, other branches 8765 and 8000
PAGE_DIR = paths.REPO_ROOT / "extension" / "brainmap"
PING_S = 15.0                  # proxies and browsers drop streams that stay silent too long
QUEUE_SIZE = 64                # messages held per SSE client: 16 s of frames at 4 Hz
READ_TIMEOUT_S = 30.0          # a client that never finishes its request line frees its thread
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
                ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
                ".woff2": "font/woff2"}
TEXT = "text/plain; charset=utf-8"


# -- message encoding ---------------------------------------------------------------------

def _plain(obj: Any) -> Any:
    """json.dumps fallback: numpy arrays and scalars (anything with tolist) become lists."""
    if hasattr(obj, "tolist"):
        return obj.tolist()
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def _finite(obj: Any) -> Any:
    """A copy with every NaN or infinity replaced by None (JSON has neither)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if hasattr(obj, "tolist"):
        return _finite(obj.tolist())
    return obj


def encode(msg: dict[str, Any]) -> bytes:
    """One SSE event, b'data: <json>\\n\\n', with NaN and infinities sent as null."""
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ValueError(f"a message must be a dict with a string 'type', got {msg!r:.80}")
    try:
        text = json.dumps(msg, allow_nan=False, separators=(",", ":"), default=_plain)
    except ValueError:   # a NaN somewhere: the stream should null it, but never send "NaN"
        text = json.dumps(_finite(msg), allow_nan=False, separators=(",", ":"), default=_plain)
    return b"data: " + text.encode("utf-8") + b"\n\n"


# -- hosts --------------------------------------------------------------------------------

def host_name(header: str | None) -> str:
    """The name part of a Host header, lower-case and without the port ('[::1]' kept whole)."""
    h = (header or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[:end + 1] if end > 0 else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def is_ip_literal(name: str) -> bool:
    """Whether a Host name (as ``host_name`` returns it) is an IPv4 or bracketed IPv6 address."""
    if name.startswith("[") and name.endswith("]"):
        inner = name[1:-1].split("%", 1)[0]   # a zone id ("%25en0") is not part of the check
        try:
            return ipaddress.ip_address(inner).version == 6
        except ValueError:
            return False
    try:
        return ipaddress.ip_address(name).version == 4
    except ValueError:   # a DNS name, "" or a bare IPv6 address (invalid in a Host header)
        return False


def _bound_loopback(address: str) -> bool:
    """Whether a bound socket address is a loopback one ("0.0.0.0" and "::" are not)."""
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _url_name(address: str) -> str:
    """How a bound address appears in a URL or Host header: IPv6 inside brackets."""
    return f"[{address}]" if ":" in address else address


def port_in_use(host: str, port: int, timeout: float = 0.2) -> bool:
    """Whether something already accepts connections on ``port``.

    Binding alone misses one case: on macOS and the BSDs, SO_REUSEADDR (kept so a restart is
    not blocked by TIME_WAIT) lets 127.0.0.1:P bind while another server listens on
    0.0.0.0:P, and the browser would reach whichever one it happens to. A wildcard host is
    probed on the loopback address.
    """
    if port == 0:
        return False
    probe = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    try:
        with socket.create_connection((probe, port), timeout=timeout):
            return True
    except OSError:   # refused, unreachable or timed out: nobody is listening there
        return False


# -- SSE clients --------------------------------------------------------------------------

class _Client:
    """One /events connection: a bounded queue of encoded events and its socket."""

    def __init__(self, maxsize: int, sock: socket.socket | None) -> None:
        self.maxsize = maxsize
        self.sock = sock
        self.closed = False
        self._items: collections.deque[bytes] = collections.deque()
        self._cond = threading.Condition()

    def put(self, data: bytes) -> bool:
        """Queue ``data`` without blocking; False when the queue is full or closed."""
        with self._cond:
            if self.closed or len(self._items) >= self.maxsize:
                return False
            self._items.append(data)
            self._cond.notify()
            return True

    def get(self, timeout: float) -> bytes | None:
        """The next event, or None after ``timeout`` s or once closed (see ``closed``)."""
        with self._cond:
            if not self._items and not self.closed:
                self._cond.wait(timeout)
            if self.closed or not self._items:
                return None
            return self._items.popleft()

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._items.clear()
            self._cond.notify_all()
        if self.sock is not None:
            try:   # wakes a handler blocked writing to a client that stopped reading
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# -- server -------------------------------------------------------------------------------

class MapServer:
    """Serves ``root`` and streams published messages to every /events client.

    The port is bound here (OSError, errno EADDRINUSE, for a busy one); ``start`` serves on
    a daemon thread and ``stop`` (or leaving the ``with`` block) ends it, disconnecting the
    clients. ``set_hello`` stores the message every new client gets first (and sends it to
    those already connected); the newest status message is kept and sent second.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 root: str | Path = PAGE_DIR, *, allowed_hosts: Iterable[str] | None = None,
                 ping_s: float = PING_S, queue_size: int = QUEUE_SIZE) -> None:
        if not (math.isfinite(ping_s) and ping_s > 0):
            raise ValueError(f"ping_s must be positive, got {ping_s}")
        if queue_size < 1:
            raise ValueError(f"queue_size must be >= 1, got {queue_size}")
        self.root = Path(root)
        self.ping_s = float(ping_s)
        self.queue_size = int(queue_size)
        self.dropped = 0           # clients dropped for falling behind
        self._lock = threading.Lock()
        self._clients: list[_Client] = []
        self._hello: bytes | None = None
        self._status: bytes | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False
        extra = {host_name(h) for h in (allowed_hosts or ())} - {""}
        self._httpd = _HTTPServer((host, port), self)
        # decided from the bound address, not the --host text: "localhost", "127.1" or a
        # name in /etc/hosts all bind to a loopback address and keep the strict check
        bound = str(self._httpd.server_address[0])
        self._exposed = not _bound_loopback(bound)
        self._allowed = frozenset(LOOPBACK_NAMES | extra
                                  | (set() if self._exposed else {_url_name(bound)}))

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> MapServer:
        if self._stopped:
            raise RuntimeError("this MapServer was stopped; make a new one")
        if self._thread is None:
            self._thread = threading.Thread(target=self._httpd.serve_forever,
                                            kwargs={"poll_interval": 0.1},
                                            name="brainmap-http", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        """Disconnect every client and close the port; safe to call twice."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            clients, self._clients = self._clients, []
        for c in clients:
            c.close()
        if self._thread is not None:
            self._httpd.shutdown()      # waits for serve_forever only, not for the clients
            self._thread.join(timeout=2.0)
        self._httpd.server_close()

    def __enter__(self) -> MapServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        """Where a browser on this machine finds the page, e.g. http://127.0.0.1:8140/."""
        host = str(self._httpd.server_address[0])
        host = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        return f"http://{_url_name(host)}:{self.port}/"

    @property
    def exposed(self) -> bool:
        """True when bound to a non-loopback address, so other machines can connect."""
        return self._exposed

    @property
    def accepted_hosts(self) -> tuple[str, ...]:
        """The Host names requests may carry; exposed, any IP literal is accepted as well."""
        return tuple(sorted(self._allowed))

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    # -- messages ---------------------------------------------------------------------------

    def set_hello(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict) or msg.get("type") != "hello":
            raise ValueError(f"set_hello needs a message with type 'hello', got {msg!r:.80}")
        self.publish(msg)

    def publish(self, msg: dict[str, Any]) -> None:
        """Send ``msg`` to every connected client, from any thread, without blocking."""
        data = encode(msg)
        with self._lock:
            if msg["type"] == "hello":
                self._hello = data
            elif msg["type"] == "status":
                self._status = data
            behind = [c for c in self._clients if not c.put(data)]
            if behind:
                self._clients = [c for c in self._clients if c not in behind]
                self.dropped += len(behind)
        for c in behind:
            c.close()

    def _subscribe(self, sock: socket.socket | None) -> _Client:
        # under the lock, so a message published meanwhile lands after the hello and status
        client = _Client(self.queue_size, sock)
        with self._lock:
            if self._stopped:
                client.close()
                return client
            for data in (self._hello, self._status):
                if data is not None:
                    client.put(data)
            self._clients.append(client)
        return client

    def _unsubscribe(self, client: _Client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        client.close()

    # -- requests ---------------------------------------------------------------------------

    def host_allowed(self, header: str | None) -> bool:
        name = host_name(header)
        return name in self._allowed or (self._exposed and is_ip_literal(name))

    def static_path(self, url_path: str) -> Path | None:
        """The file a GET of ``url_path`` serves, or None (unknown, hidden or outside root)."""
        if not url_path.startswith("/"):
            return None
        name = urllib.parse.unquote(url_path[1:]) or "index.html"
        if "\x00" in name or "\\" in name:
            return None
        parts = name.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            return None
        if Path(parts[-1]).suffix.lower() not in STATIC_TYPES:
            return None
        try:
            root = self.root.resolve(strict=True)
            found = root.joinpath(*parts).resolve(strict=True)
        except (OSError, RuntimeError):   # missing, unreadable, or a symlink loop
            return None
        if root not in found.parents or not found.is_file():
            return None   # a symlink pointing out of root, or a directory
        return found


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True          # SSE streams must not hold up Ctrl-C or stop()
    block_on_close = False
    allow_reuse_address = True     # restart at once after Ctrl-C (TIME_WAIT)
    allow_reuse_port = False       # but never share the port with another server

    def __init__(self, address: tuple[str, int], owner: MapServer) -> None:
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.owner = owner
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up the host's FQDN, which can stall for seconds on
        # macOS; the name is only used for CGI variables
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer
    server_version = "BrainSyncMap"
    sys_version = ""
    timeout = READ_TIMEOUT_S

    def log_message(self, format: str, *args: Any) -> None:
        pass   # four frames a second; request logs would bury the startup lines

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        owner = self.server.owner
        if not owner.host_allowed(self.headers.get("Host")):
            body = (b"forbidden: unknown Host name; open the page by IP address or restart the "
                    b"server with --allow-host NAME\n" if owner.exposed else
                    b"forbidden: this server only answers to localhost\n")
            self._send(403, body, TEXT)
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/events":
            self._events(owner)
            return
        found = owner.static_path(path)
        if found is None:
            self._send(404, b"not found\n", TEXT)
            return
        try:
            body = found.read_bytes()
        except OSError:
            self._send(404, b"not found\n", TEXT)
            return
        self._send(200, body, STATIC_TYPES[found.suffix.lower()], cache="no-cache")

    def _events(self, owner: MapServer) -> None:
        client = owner._subscribe(self.connection)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.flush()
            while True:
                data = client.get(owner.ping_s)
                if data is None:
                    if client.closed:
                        break
                    data = b": ping\n\n"
                self.wfile.write(data)
                self.wfile.flush()
        except OSError:
            pass   # the page closed or reloaded, or the client was dropped
        finally:
            owner._unsubscribe(client)
            self.close_connection = True
