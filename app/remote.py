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

``RemoteGate(app, key)`` is ASGI middleware; ``key=None`` turns it off (tests, and a server that
only listens on 127.0.0.1 with no tunnel, where nothing remote can arrive).
"""

from __future__ import annotations

import hmac
import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

COOKIE = "bs_key"
COOKIE_MAX_AGE_S = 12 * 3600  # a demo day
FORBIDDEN = 4403  # websocket close code: remote without the key, or a remote decoder
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
# Set by a proxy or tunnel in front of the server; a browser on this computer never sends them.
PROXY_HEADERS = (b"cf-connecting-ip", b"cf-ray", b"x-forwarded-for", b"forwarded")
LOCAL_ONLY = ("/ws/decoder",)


def new_key() -> str:
    return secrets.token_urlsafe(18)


def _headers(scope: dict) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in scope.get("headers", [])}


def is_remote(scope: dict) -> bool:
    client = scope.get("client")
    host = client[0] if client else ""
    return host not in LOCAL_HOSTS or any(h in _headers(scope) for h in PROXY_HEADERS)


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


class RemoteGate:
    """ASGI middleware: remote requests need the key; the decoder socket is local only."""

    def __init__(self, app, key: str | None) -> None:
        self.app = app
        self.key = key

    async def __call__(self, scope, receive, send) -> None:
        if self.key is None or scope["type"] not in ("http", "websocket") or not is_remote(scope):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        given, from_link = presented_key(scope)
        allowed = not path.startswith(LOCAL_ONLY) and hmac.compare_digest(given.encode(), self.key.encode())
        if not allowed:
            return await self._refuse(scope, receive, send)
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

    async def _refuse(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": FORBIDDEN})
            return
        body = (b"This BrainSync server needs the phone link with its access key "
                b"(printed by python -m app.server --tunnel).\n")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
