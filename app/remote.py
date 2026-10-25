"""Who may use the server from outside this computer (the partner page on a phone, PLAN.md section 10).

The phone reaches the server through a tunnel (``python -m app.server --tunnel``: a Cloudflare quick
tunnel to 127.0.0.1) or over the local network (``server.host: 0.0.0.0``). Such a request is
*remote*: it comes from another address, or it carries a proxy's forwarding headers (the tunnel
connects from 127.0.0.1 itself, so its requests are told apart by those headers).

A public address would let anyone press Resume, and Resume starts the flicker. So:

- A remote request needs the access key: ``?key=...`` in the phone link once, then a cookie the
  server sets on that answer (HttpOnly, SameSite=Strict). Without it: 403 (HTTP) or close code
  4403 (websocket), and nothing reaches the orchestrator.
- ``/ws/decoder`` is never remote: only the bridge on this computer selects (PLAN rule 1).
- Requests from this computer itself need nothing, so the laptop works as before.

``RemoteGate(app, key)`` is ASGI middleware. ``key=None`` is a server that listens on this computer
only with no tunnel (and tests): it lets no remote request in at all, so a tunnel or proxy left
running from an earlier ``--tunnel`` start reaches nothing. There a client that is no address
(Starlette's TestClient calls itself "testclient") counts as local: only a proxy's headers or a
real address that is not this computer are refused. With a key, such a client is remote.

The key goes into a cookie, so it may only hold cookie characters (``key_problem``); the links
carry it percent-encoded.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

COOKIE = "bs_key"
COOKIE_MAX_AGE_S = 12 * 3600  # a demo day
FORBIDDEN = 4403  # websocket close code: remote without the key, or a remote decoder
LOCAL_NAMES = {"localhost"}  # besides any loopback address: 127.x.y.z, ::1, ::ffff:127.x.y.z
# Set by a proxy or tunnel in front of the server; a browser on this computer never sends them.
PROXY_HEADERS = (b"cf-connecting-ip", b"cf-ray", b"x-forwarded-for", b"forwarded")
LOCAL_ONLY = ("/ws/decoder",)
# RFC 6265 cookie-octet: printable ASCII except space, double quote, comma, semicolon and backslash.
COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]+")


def new_key() -> str:
    return secrets.token_urlsafe(18)


def key_problem(key: str) -> str | None:
    """Why ``key`` cannot be the access key, or None. It is sent back as a cookie value, so only
    cookie characters are allowed (never shows the key itself)."""
    if not COOKIE_VALUE.fullmatch(key):
        return ("may only hold printable ASCII without spaces, quotes, commas, semicolons or "
                "backslashes (letters, digits, - and _ are always fine)")
    return None


def _headers(scope: dict) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in scope.get("headers", [])}


def _client_host(scope: dict) -> str:
    client = scope.get("client")
    return client[0] if client else ""


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_local_host(host: str) -> bool:
    """This computer: "localhost" or a loopback address, IPv4-mapped ones included (a server on
    "::" sees 127.0.0.1 as ::ffff:127.0.0.1)."""
    if host in LOCAL_NAMES:
        return True
    ip = _address(host)
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip is not None and (ip.is_loopback or (mapped is not None and mapped.is_loopback))


def via_proxy(scope: dict) -> bool:
    return any(h in _headers(scope) for h in PROXY_HEADERS)


def is_remote(scope: dict) -> bool:
    """Not provably this computer: another address, no address, or through a proxy."""
    return not is_local_host(_client_host(scope)) or via_proxy(scope)


def is_plainly_remote(scope: dict) -> bool:
    """Visibly from outside: through a proxy, or from an address that is not this computer. What
    a server without a key refuses; a client that is no address at all is not counted."""
    host = _client_host(scope)
    return via_proxy(scope) or (_address(host) is not None and not is_local_host(host))


def presented_key(scope: dict) -> tuple[str, bool]:
    """The key this request carries, and whether it came in the query string (the phone link)."""
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    if query.get("key"):
        return query["key"][0], True
    cookie = SimpleCookie()
    raw = _headers(scope).get(b"cookie")
    if raw:
        try:
            cookie.load(raw.decode("latin-1"))
        except Exception:  # a malformed Cookie header is just no key
            return "", False
    morsel = cookie.get(COOKIE)
    return (morsel.value if morsel else ""), False


def _https(scope: dict) -> bool:
    headers = _headers(scope)
    return scope.get("scheme") in ("https", "wss") or headers.get(b"x-forwarded-proto") == b"https" \
        or b'"scheme":"https"' in headers.get(b"cf-visitor", b"")


NEEDS_KEY = (b"This BrainSync server needs the phone link with its access key "
             b"(printed by python -m app.server --tunnel).\n")
NO_KEY = (b"This BrainSync server only answers this computer. For a phone, start it with "
          b"python -m app.server --tunnel and open the phone link it prints.\n")


def same_origin(scope: dict) -> bool:
    """A browser's websocket must come from this server's own page: any web page open in the
    laptop's browser could otherwise connect to ws://127.0.0.1 and press Resume (websockets are
    not covered by the same-origin rules). No Origin header (the bridge, the fake decoder) is fine.
    The tunnel keeps the visitor's Host (the trycloudflare name), so the phone's page matches too."""
    headers = _headers(scope)
    origin = headers.get(b"origin")
    if origin is None:
        return True
    netloc = urlsplit(origin.decode("latin-1")).netloc.lower()
    hosts = {headers.get(b"host", b"").decode("latin-1").lower(),
             headers.get(b"x-forwarded-host", b"").decode("latin-1").lower()}
    return bool(netloc) and netloc in hosts


CROSS_ORIGIN = b"Refused: this socket only answers the server's own page.\n"


class RemoteGate:
    """ASGI middleware: remote requests need the key; the decoder socket is local only."""

    def __init__(self, app, key: str | None) -> None:
        self.app = app
        self.key = key

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket" and not same_origin(scope):
            return await self._refuse(scope, receive, send, CROSS_ORIGIN)
        if self.key is None:  # this computer only: no key can let a remote request in
            if is_plainly_remote(scope):
                return await self._refuse(scope, receive, send, NO_KEY)
            return await self.app(scope, receive, send)
        if not is_remote(scope):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        given, from_link = presented_key(scope)
        allowed = not path.startswith(LOCAL_ONLY) and hmac.compare_digest(given.encode(), self.key.encode())
        if not allowed:
            return await self._refuse(scope, receive, send, NEEDS_KEY)
        if scope["type"] == "http" and from_link:
            send = self._with_cookie(send, secure=_https(scope))
        return await self.app(scope, receive, send)

    def _with_cookie(self, send, secure: bool):
        cookie = (f"{COOKIE}={self.key}; Path=/; Max-Age={COOKIE_MAX_AGE_S}; HttpOnly; SameSite=Strict"
                  + ("; Secure" if secure else ""))

        async def sender(message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                                                  (b"set-cookie", cookie.encode("latin-1"))]}
            await send(message)

        return sender

    async def _refuse(self, scope, receive, send, body: bytes) -> None:
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": FORBIDDEN})
            return
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
