"""Tests for the server transport, against a real websockets client on a free port."""
from __future__ import annotations

import asyncio
import http.client
import json
import re
import threading
import time

import pytest

pytest.importorskip("websockets")

from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

from brainsync.server import protocol as P  # noqa: E402
from brainsync.server.transport import (Event, Transport, is_loopback,  # noqa: E402
                                        origin_allowed)

TOKEN = "s3cret-token-for-tests"
EXTENSION_ORIGIN = "chrome-extension://" + "abcdefghijklmnop" * 2


def welcome_for(conn_id: int, role: str, hello: P.Hello) -> list[dict]:
    """A catchup like the session's: welcome plus the current phase."""
    return [P.welcome(role, hello.mode, fake=True, waveform="sine", refresh_hz=60.0,
                      refresh_tolerance_hz=1.0, freqs=[15.0, 17.0, 12.0, 20.0], max_slots=4,
                      detect=False),
            P.phase("wait", False, text=f"conn {conn_id}")]


def make(**kwargs) -> Transport:
    kwargs.setdefault("token", TOKEN)
    kwargs.setdefault("catchup", welcome_for)
    return Transport(port=kwargs.pop("port", 0), **kwargs).start()


@pytest.fixture
def transport():
    t = make()
    yield t
    t.stop()


def hello(role: str = "controller", token: str | None = TOKEN, **extra) -> dict:
    msg = {"type": "hello", "protocol": 3, "role": role,
           "client": {"name": "pytest", "kind": "test", "version": "0"}}
    if token is not None:
        msg["token"] = token
    if role == "controller":
        msg.update(mode="slots", slots=["yes", "no"], refresh_hz=60.0, accepted=True)
    msg.update(extra)
    return msg


async def recv(ws, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def until(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def closed(ws, timeout: float = 5.0) -> tuple[int | None, str]:
    """Wait for the server to close ``ws``; returns (code, reason)."""
    await asyncio.wait_for(ws.wait_closed(), timeout)
    return ws.close_code, ws.close_reason


async def join(t: Transport, role: str = "controller", **extra):
    """Connect, send a hello and read the two catchup messages."""
    ws = await connect(t.url)
    await ws.send(json.dumps(hello(role, **extra)))
    first = await recv(ws)
    assert first["type"] == "welcome", first
    assert (await recv(ws))["type"] == "phase"
    return ws


def no_token_leak(events: list[Event]) -> None:
    for e in events:
        assert TOKEN not in repr(e)


# -- pure helpers -----------------------------------------------------------------------------

def test_is_loopback():
    for ok in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"):
        assert is_loopback(ok), ok
    for bad in ("0.0.0.0", "192.168.1.5", "100.64.0.1", "::", "example.com", "localhost.evil"):
        assert not is_loopback(bad), bad


def test_origin_allowed():
    for ok in (None, EXTENSION_ORIGIN, "http://127.0.0.1:8000", "http://localhost",
               "https://localhost:3000", "http://[::1]:8765"):
        assert origin_allowed(ok), ok
    for bad in ("https://evil.example", "null", "", "chrome-extension://xyz",
                "http://127.0.0.1.evil.example", "http://localhost:8000.evil.example",
                "file://", "http://localhost:8000/path"):
        assert not origin_allowed(bad), bad


def test_origin_allowed_own_host():
    # the page served by this server over the network: Origin equals Host, host is an IP
    assert origin_allowed("http://100.64.0.5:8765", "100.64.0.5:8765")
    assert origin_allowed("http://jetson:8765", "jetson:8765", bind_host="jetson")
    # a DNS rebinding page matches Host too, but is a name: refused
    assert not origin_allowed("http://evil.example:8765", "evil.example:8765")
    # so is any other name, a tunnel's or a *.ts.net one: they need --origin
    assert not origin_allowed("https://ripe-banana.trycloudflare.com",
                              "ripe-banana.trycloudflare.com")
    assert not origin_allowed("https://jetson.tail1234.ts.net", "jetson.tail1234.ts.net")
    # port or host differ from Host: refused
    assert not origin_allowed("http://100.64.0.5:9999", "100.64.0.5:8765")
    assert not origin_allowed("http://100.64.0.6:8765", "100.64.0.5:8765")
    assert not origin_allowed("http://100.64.0.5:8765", None)


def test_origin_allowed_patterns():
    pats = [re.compile(r"https://app\.example\.com")]
    assert origin_allowed("https://app.example.com", patterns=pats)
    assert not origin_allowed("https://app.example.com.evil", patterns=pats)


def test_origin_allowed_tunnel_host_only_with_its_pattern():
    # brainsync-tunnel.sh passes --origin https://<host> with the dots escaped
    host = "ripe-banana.trycloudflare.com"
    pats = [re.compile(r"https://ripe-banana\.trycloudflare\.com")]
    assert origin_allowed(f"https://{host}", host, patterns=pats)
    assert origin_allowed(f"https://{host}", patterns=pats)
    assert not origin_allowed("https://other-name.trycloudflare.com",
                              "other-name.trycloudflare.com", patterns=pats)
    # a *.ts.net name is no different now: only a pattern lets it in
    assert origin_allowed("https://jetson.tail1234.ts.net", "jetson.tail1234.ts.net",
                          patterns=[re.compile(r"https://jetson\.tail1234\.ts\.net")])


# -- arguments --------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "jetson.local"])
def test_no_auth_refused_on_a_non_loopback_host(host):
    with pytest.raises(ValueError, match="--no-auth is allowed only with a loopback host"):
        Transport(host, 0, token=None)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_no_auth_allowed_on_loopback(host):
    Transport(host, 0, token=None)            # no error; not started


@pytest.mark.parametrize("kwargs", [
    {"port": -1}, {"port": 70000}, {"port": "8765"}, {"port": True},
    {"token": ""}, {"token": 5},
    {"ping_s": 0.0}, {"ping_s": float("nan")}, {"hello_timeout_s": -1},
    {"max_slots": 0}, {"max_frame": 0}, {"max_slots": True},
    {"origins": ["("]},
    {"health": "nope"}, {"catchup": 3},
    {"host": ""}])
def test_bad_arguments(kwargs):
    kwargs.setdefault("token", TOKEN)
    with pytest.raises(ValueError):
        Transport(**kwargs)


def test_bad_static_files(tmp_path):
    f = tmp_path / "app.js"
    f.write_text("1")
    for name in ("../app.js", "/app.js", "", "a/../b.js", "a\\b.js", ".hidden/../x", "a//b"):
        with pytest.raises(ValueError, match="relative name"):
            Transport(token=TOKEN, static_files={name: f})
    with pytest.raises(ValueError, match="not a file"):
        Transport(token=TOKEN, static_files={"missing.js": tmp_path / "missing.js"})
    with pytest.raises(ValueError, match="not a file"):
        Transport(token=TOKEN, static_files={"dir": tmp_path})


def test_url_and_port(transport):
    assert transport.port > 0 and transport.running
    assert transport.url == f"ws://127.0.0.1:{transport.port}/ws"
    assert Transport("::1", 9000, token=TOKEN).url == "ws://[::1]:9000/ws"


# -- auth and the hello -----------------------------------------------------------------------

def test_good_token_gets_catchup_and_a_connect_event(transport):
    async def main():
        ws = await connect(transport.url)
        await ws.send(json.dumps(hello()))
        welcome = await recv(ws)
        assert welcome["type"] == "welcome" and welcome["role"] == "controller"
        assert welcome["mode"] == "slots"
        assert (await recv(ws)) == P.phase("wait", False, text="conn 1")
        await until(lambda: transport.controller == 1)
        assert transport.n_viewers == 0
        await ws.close()
        await until(lambda: transport.controller is None)

    asyncio.run(main())
    events = transport.events()
    assert [e.kind for e in events] == ["connect", "disconnect"]
    connect_ev, disconnect_ev = events
    assert connect_ev.conn_id == 1 and connect_ev.role == "controller"
    assert connect_ev.hello is not None and connect_ev.hello.slots == ("yes", "no")
    assert connect_ev.hello.token is None            # the token stops at the transport
    assert "token" not in connect_ev.msg and connect_ev.msg["type"] == "hello"
    assert connect_ev.msg == connect_ev.hello.to_dict()
    assert abs(connect_ev.t - time.time()) < 5
    assert disconnect_ev.conn_id == 1 and disconnect_ev.role == "controller"
    assert disconnect_ev.close_code == 1000 and disconnect_ev.msg is None
    no_token_leak(events)
    assert transport.events() == []


@pytest.mark.parametrize("token", ["wrong", None, "", TOKEN + "x", TOKEN[:-1], "ünïcode"])
def test_bad_token_gets_4001(transport, token, capsys):
    async def main():
        async with connect(transport.url) as ws:
            msg = hello(token=token)
            await ws.send(json.dumps(msg))
            err = await recv(ws)
            assert err["type"] == "error" and err["code"] == "bad_token" and err["fatal"]
            assert (await closed(ws)) == (P.CLOSE_BAD_TOKEN, "bad token")

    asyncio.run(main())
    assert transport.events() == [] and transport.controller is None
    err = capsys.readouterr().err
    assert "wrong or missing token" in err and TOKEN not in err


def test_token_of_the_wrong_type_gets_4001(transport):
    async def main():
        async with connect(transport.url) as ws:
            await ws.send(json.dumps(hello(token=None) | {"token": 12345}))
            assert (await recv(ws))["code"] == "bad_token"
            assert (await closed(ws))[0] == P.CLOSE_BAD_TOKEN

    asyncio.run(main())


def test_no_auth_accepts_a_hello_without_token():
    t = make(token=None)
    try:
        async def main():
            ws = await join(t, token=None)
            await ws.close()
        asyncio.run(main())
    finally:
        t.stop()
    assert [e.kind for e in t.events()] == ["connect", "disconnect"]


@pytest.mark.parametrize("first", [
    json.dumps({"type": "ping", "ping_id": 1, "t_client": 1.0}),
    json.dumps({"type": "dance"}),
    "not json",
    json.dumps([1, 2]),
    b"\x00\x01binary"])
def test_message_before_hello_gets_4003(transport, first):
    async def main():
        async with connect(transport.url) as ws:
            await ws.send(first)
            err = await recv(ws)
            assert err["code"] == "protocol" and err["fatal"] is True
            assert (await closed(ws)) == (P.CLOSE_HELLO_FIRST, "hello first")

    asyncio.run(main())
    assert transport.events() == []


def test_hello_timeout_gets_4008():
    t = make(hello_timeout_s=0.2)
    try:
        async def main():
            async with connect(t.url) as ws:
                start = time.monotonic()
                err = await recv(ws)
                assert err["code"] == "protocol" and "no hello within 0.2 s" in err["message"]
                assert (await closed(ws)) == (P.CLOSE_HELLO_TIMEOUT, "hello timeout")
                assert time.monotonic() - start < 3
        asyncio.run(main())
    finally:
        t.stop()
    assert t.events() == []


@pytest.mark.parametrize("bad, code", [
    ({"accepted": False}, "bad_hello"),
    ({"protocol": 2}, "bad_hello"),
    ({"role": "admin"}, "bad_hello"),
    ({"slots": ["a", "b", "c", "d", "e"]}, "too_many_slots"),
    ({"refresh_hz": "60"}, "bad_hello")])
def test_bad_hello_is_fatal(transport, bad, code):
    async def main():
        async with connect(transport.url) as ws:
            await ws.send(json.dumps(hello() | bad))
            err = await recv(ws)
            assert err["type"] == "error" and err["code"] == code and err["fatal"] is True
            assert (await closed(ws)) == (1008, "bad hello")

    asyncio.run(main())
    assert transport.events() == [] and transport.controller is None


def test_max_slots_is_passed_to_parse_hello():
    t = make(max_slots=2)
    try:
        async def main():
            async with connect(t.url) as ws:
                await ws.send(json.dumps(hello(slots=["a", "b", "c"])))
                assert (await recv(ws))["code"] == "too_many_slots"
        asyncio.run(main())
    finally:
        t.stop()


# -- roles ------------------------------------------------------------------------------------

def test_second_controller_is_busy_and_takeover_replaces(transport, capsys):
    async def main():
        a = await join(transport)
        await until(lambda: transport.controller == 1)

        async with connect(transport.url) as b:          # conn 2: busy
            await b.send(json.dumps(hello()))
            err = await recv(b)
            assert err["code"] == "busy" and err["fatal"] is True
            assert "takeover" in err["message"]
            assert (await closed(b)) == (P.CLOSE_BUSY, "busy")
        assert transport.controller == 1

        transport.send({"type": "status", "board": "unknown", "ok": True, "text": "a only"})
        assert (await recv(a))["text"] == "a only"

        c = await join(transport, takeover=True)          # conn 3: takes over
        assert (await closed(a)) == (P.CLOSE_REPLACED, "replaced")
        assert transport.controller == 3
        transport.send({"type": "status", "board": "unknown", "ok": True, "text": "c"})
        assert (await recv(c))["text"] == "c"
        await c.close()

    asyncio.run(main())
    events = transport.events()
    assert [(e.kind, e.conn_id, e.close_code) for e in events] == [
        ("connect", 1, None), ("disconnect", 1, P.CLOSE_REPLACED), ("connect", 3, None),
        ("disconnect", 3, 1000)]
    assert "took over from controller 1" in capsys.readouterr().err


def test_same_client_instance_reconnecting_replaces_its_dead_socket(transport, capsys):
    """A controller whose socket died on its side only reconnects with takeover false; the
    server still holds the old connection, so it must not answer busy (4002), which would
    stop the client for good. The matching client.instance makes it a resume."""
    me = {"name": "pytest", "kind": "test", "version": "0", "instance": "inst-1"}

    async def main():
        a = await join(transport, client=me)                  # conn 1
        await until(lambda: transport.controller == 1)
        async with connect(transport.url) as b:                # conn 2: another instance
            await b.send(json.dumps(hello(client={**me, "instance": "inst-2"})))
            assert (await recv(b))["code"] == "busy"
            assert (await closed(b)) == (P.CLOSE_BUSY, "busy")
        async with connect(transport.url) as b:                # conn 3: no instance at all
            await b.send(json.dumps(hello()))
            assert (await recv(b))["code"] == "busy"
        c = await join(transport, client=me)                  # conn 4: the same client again
        assert (await closed(a)) == (P.CLOSE_REPLACED, "replaced")
        await until(lambda: transport.controller == 4)
        await c.close()

    asyncio.run(main())
    kinds = [(e.kind, e.conn_id) for e in transport.events()]
    assert kinds == [("connect", 1), ("disconnect", 1), ("connect", 4), ("disconnect", 4)]
    assert "controller 1 reconnecting" in capsys.readouterr().err


def test_a_viewer_with_the_controllers_instance_is_still_a_viewer(transport):
    me = {"name": "pytest", "instance": "inst-1"}

    async def main():
        a = await join(transport, client=me)
        v = await join(transport, role="viewer", client=me)
        await until(lambda: transport.controller == 1 and transport.n_viewers == 1)
        await v.close()
        await a.close()

    asyncio.run(main())


def test_websocket_keepalive_finds_a_dead_peer_quickly(monkeypatch):
    from brainsync.server import transport as T
    assert T.WS_PING_INTERVAL_S + T.WS_PING_TIMEOUT_S <= 20.0
    seen = {}
    import websockets.asyncio.server as S
    real = S.serve

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(S, "serve", spy)
    t = make()
    t.stop()
    assert seen["ping_interval"] == T.WS_PING_INTERVAL_S
    assert seen["ping_timeout"] == T.WS_PING_TIMEOUT_S


def test_takeover_with_no_controller_is_a_plain_join(transport):
    async def main():
        ws = await join(transport, takeover=True)
        await until(lambda: transport.controller == 1)
        await ws.close()

    asyncio.run(main())


def test_viewers_join_beside_the_controller_and_get_broadcasts(transport):
    async def main():
        ctl = await join(transport)
        v1 = await join(transport, "viewer")
        v2 = await join(transport, "viewer", mode="point")
        await until(lambda: transport.n_viewers == 2)
        assert transport.controller == 1
        msg = P.phase("run", True)
        transport.send(msg)
        for ws in (ctl, v1, v2):
            assert await recv(ws) == msg
        await v1.close()
        await until(lambda: transport.n_viewers == 1)
        await ctl.close()
        await until(lambda: transport.controller is None)
        assert transport.n_viewers == 1
        await v2.close()
        await until(lambda: transport.n_viewers == 0)

    asyncio.run(main())
    kinds = [(e.kind, e.role) for e in transport.events()]
    assert kinds.count(("connect", "viewer")) == 2
    assert kinds.count(("disconnect", "viewer")) == 2


@pytest.mark.parametrize("kind", ["pause", "resume", "onset", "frames", "gaze", "scan",
                                  "clicked"])
def test_viewer_may_not_control(transport, kind):
    async def main():
        v = await join(transport, "viewer")
        await v.send(json.dumps({"type": kind, "layout_id": 1, "t_client": 1.0}))
        err = await recv(v)
        assert err == {"type": "error", "code": "not_allowed", "fatal": False,
                       "message": err["message"]}
        assert kind in err["message"] and "only ping and bye" in err["message"]
        # still connected and still receiving
        await v.send(json.dumps({"type": "ping", "ping_id": 1, "t_client": 2.0}))
        assert (await recv(v))["type"] == "pong"
        await v.close()

    asyncio.run(main())
    events = transport.events()
    assert [e.msg["type"] for e in events if e.kind == "message"] == ["ping"]


def test_viewer_bye_is_queued_and_closes(transport):
    async def main():
        v = await join(transport, "viewer")
        await v.send(json.dumps({"type": "bye"}))
        assert (await closed(v)) == (1000, "bye")
        await until(lambda: transport.n_viewers == 0)

    asyncio.run(main())
    events = transport.events()
    assert [(e.kind, e.msg and e.msg["type"]) for e in events] == [
        ("connect", "hello"), ("message", "bye"), ("disconnect", None)]
    assert events[2].close_code == 1000


def test_controller_bye_frees_the_controller_slot(transport):
    async def main():
        ws = await join(transport)
        await ws.send(json.dumps({"type": "bye", "extra": 1}))
        assert (await closed(ws))[0] == 1000
        await until(lambda: transport.controller is None)
        again = await join(transport)                   # no busy
        await again.close()

    asyncio.run(main())
    events = transport.events()
    assert [e.kind for e in events] == ["connect", "message", "disconnect", "connect",
                                        "disconnect"]
    assert events[1].msg == {"type": "bye"}             # cleaned: unknown fields dropped


# -- messages ---------------------------------------------------------------------------------

def test_ping_gets_a_pong_and_is_queued(transport):
    async def main():
        ws = await join(transport)
        before = time.time()
        await ws.send(json.dumps({"type": "ping", "ping_id": 7, "t_client": 1792809201.0}))
        p = await recv(ws)
        assert p["type"] == "pong" and p["ping_id"] == 7 and p["t_client"] == 1792809201.0
        assert before - 1 <= p["t_server"] <= time.time() + 1
        await ws.close()

    asyncio.run(main())
    msgs = [e for e in transport.events() if e.kind == "message"]
    assert len(msgs) == 1 and msgs[0].msg == {"type": "ping", "ping_id": 7,
                                              "t_client": 1792809201.0}
    assert msgs[0].role == "controller" and msgs[0].hello is None


def test_controller_messages_are_validated_and_queued(transport):
    async def main():
        ws = await join(transport)
        for msg in ({"type": "onset", "layout_id": 3, "t_client": 5.5, "junk": True},
                    {"type": "frames", "refresh_hz": 59.98, "dropped": 0},
                    {"type": "pause"}, {"type": "resume"}, {"type": "gaze", "slot": None}):
            await ws.send(json.dumps(msg))
        await until(lambda: transport._events.qsize() >= 6)
        await ws.close()

    asyncio.run(main())
    msgs = [e.msg for e in transport.events() if e.kind == "message"]
    assert msgs == [{"type": "onset", "layout_id": 3, "t_client": 5.5},
                    {"type": "frames", "refresh_hz": 59.98, "dropped": 0},
                    {"type": "pause"}, {"type": "resume"}, {"type": "gaze", "slot": None}]


@pytest.mark.parametrize("bad, words", [
    (json.dumps({"type": "dance"}), "unknown message type"),
    ("not json", "not JSON"),
    (json.dumps([1]), "JSON object"),
    (json.dumps({"type": 5}), "string \"type\""),
    (json.dumps({"type": "onset"}), "layout_id"),
    (json.dumps({"type": "ping", "ping_id": 1, "t_client": "x"}), "t_client"),
    ('{"type": "frames", "refresh_hz": NaN, "dropped": 0}', "not JSON"),
    (json.dumps({"type": "hello", "protocol": 3, "role": "viewer", "token": TOKEN}),
     "first message")])
def test_bad_messages_get_a_non_fatal_protocol_error(transport, bad, words):
    async def main():
        ws = await join(transport)
        await ws.send(bad)
        err = await recv(ws)
        assert err["type"] == "error" and err["code"] == "protocol" and err["fatal"] is False
        assert words in err["message"]
        assert TOKEN not in err["message"]
        await ws.send(json.dumps({"type": "ping", "ping_id": 1, "t_client": 1.0}))
        assert (await recv(ws))["type"] == "pong"       # the connection survives
        await ws.close()

    asyncio.run(main())
    msgs = [e.msg["type"] for e in transport.events() if e.kind == "message"]
    assert msgs == ["ping"]


def test_viewer_unknown_type_is_protocol_and_viewer_hello_is_protocol(transport):
    async def main():
        v = await join(transport, "viewer")
        await v.send(json.dumps({"type": "dance"}))
        assert (await recv(v))["code"] == "protocol"
        await v.send(json.dumps(hello("viewer")))
        assert (await recv(v))["code"] == "protocol"
        await v.close()

    asyncio.run(main())


def test_binary_frames_are_ignored_after_hello(transport):
    async def main():
        ws = await join(transport)
        await ws.send(b"\x00\x01\x02")
        await ws.send(json.dumps({"type": "ping", "ping_id": 2, "t_client": 1.0}))
        assert (await recv(ws))["type"] == "pong"       # nothing came before the pong
        await ws.close()

    asyncio.run(main())


def test_oversize_frame_closes_1009():
    t = make(max_frame=1024)
    try:
        async def main():
            ws = await join(t)
            await ws.send(json.dumps({"type": "gaze", "slot": None, "pad": "x" * 2000}))
            assert (await closed(ws))[0] == 1009
            await until(lambda: t.controller is None)
        asyncio.run(main())
    finally:
        t.stop()
    events = t.events()
    assert [e.kind for e in events] == ["connect", "disconnect"]
    assert events[1].close_code == 1009


def test_oversize_frame_before_hello_closes_1009():
    t = make(max_frame=1024)
    try:
        async def main():
            async with connect(t.url) as ws:
                await ws.send(json.dumps(hello() | {"pad": "x" * 2000}))
                assert (await closed(ws))[0] == 1009
        asyncio.run(main())
    finally:
        t.stop()
    assert t.events() == []


# -- catchup ----------------------------------------------------------------------------------

def test_catchup_runs_on_the_loop_thread_with_the_parsed_hello():
    calls = []

    def catchup(conn_id, role, h):
        calls.append((conn_id, role, h, threading.current_thread().name))
        return [{"type": "status", "board": "unknown", "ok": True, "text": f"hi {role}"}]

    t = make(catchup=catchup)
    try:
        async def main():
            async with connect(t.url) as ws:
                await ws.send(json.dumps(hello("viewer")))
                assert (await recv(ws))["text"] == "hi viewer"
        asyncio.run(main())
    finally:
        t.stop()
    [(conn_id, role, h, thread)] = calls
    assert conn_id == 1 and role == "viewer" and isinstance(h, P.Hello)
    assert h.token is None and thread == "brainsync-transport"


def test_catchup_messages_arrive_before_any_broadcast():
    """A client joining during a broadcast storm gets its catchup first, then a gapless run."""
    t = make(catchup=lambda cid, role, h: [{"type": "catchup", "conn": cid}])
    stop = threading.Event()

    def storm():
        n = 0
        while not stop.is_set():
            n += 1
            t.send({"type": "status", "board": "unknown", "ok": True, "text": str(n)})
            if n % 5 == 0:
                time.sleep(0.0005)

    sender = threading.Thread(target=storm, daemon=True)
    sender.start()
    try:
        async def main():
            for _ in range(5):
                async with connect(t.url) as ws:
                    await ws.send(json.dumps(hello("viewer")))
                    assert (await recv(ws))["type"] == "catchup"
                    seen = [int((await recv(ws))["text"]) for _ in range(100)]
                    assert seen == list(range(seen[0], seen[0] + 100))
                    assert seen[0] > 1          # joined while the storm was running
        asyncio.run(main())
    finally:
        stop.set()
        sender.join()
        t.stop()


def test_catchup_failure_closes_with_internal(capsys):
    def catchup(conn_id, role, h):
        raise RuntimeError("session broke")

    t = make(catchup=catchup)
    try:
        async def main():
            async with connect(t.url) as ws:
                await ws.send(json.dumps(hello()))
                err = await recv(ws)
                assert err["code"] == "internal" and err["fatal"] is True
                assert (await closed(ws))[0] == 1011
        asyncio.run(main())
    finally:
        t.stop()
    assert t.events() == [] and t.controller is None
    assert "catchup failed" in capsys.readouterr().err


def test_no_catchup_sends_nothing_but_joins():
    t = make(catchup=None)
    try:
        async def main():
            async with connect(t.url) as ws:
                await ws.send(json.dumps(hello()))
                await until(lambda: t.controller == 1)
                t.send(P.phase("run", True))
                assert await recv(ws) == P.phase("run", True)
        asyncio.run(main())
    finally:
        t.stop()


# -- sending ----------------------------------------------------------------------------------

def test_send_to_reaches_one_connection(transport):
    async def main():
        a = await join(transport)
        b = await join(transport, "viewer")
        await until(lambda: transport.n_viewers == 1)
        transport.send_to(2, {"type": "status", "board": "unknown", "ok": True, "text": "b"})
        transport.send_to(99, {"type": "status", "board": "unknown", "ok": True})  # gone
        transport.send({"type": "status", "board": "unknown", "ok": True, "text": "all"})
        assert (await recv(b))["text"] == "b"
        assert (await recv(b))["text"] == "all"
        assert (await recv(a))["text"] == "all"         # a never got "b"
        await a.close()
        await b.close()

    asyncio.run(main())


def test_pre_hello_connections_get_no_broadcasts(transport):
    async def main():
        async with connect(transport.url) as idle:
            await until(lambda: len(transport._conns) == 1)
            transport.send(P.phase("run", True))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(idle.recv(), 0.2)

    asyncio.run(main())


def test_send_validates_the_message(transport):
    with pytest.raises(ValueError, match="string \"type\""):
        transport.send({"text": "x"})
    with pytest.raises(ValueError):
        transport.send("phase")                          # type: ignore[arg-type]
    with pytest.raises(ValueError):
        transport.send({"type": "status", "score": float("nan")})
    with pytest.raises(TypeError):
        transport.send_to(1, {"type": "status", "text": object()})


def test_close_closes_with_the_code_and_queues_the_disconnect(transport):
    async def main():
        ws = await join(transport)
        await until(lambda: transport.controller == 1)
        transport.close(1, 4005, "go away")
        assert (await closed(ws)) == (4005, "go away")
        assert transport.controller is None

    asyncio.run(main())
    events = transport.events()
    assert [(e.kind, e.close_code) for e in events] == [("connect", None), ("disconnect", 4005)]


def test_close_validates_code(transport):
    for bad in (1001, 2000, 5000, True, "4000"):
        with pytest.raises(ValueError, match="close code"):
            transport.close(1, bad)
    with pytest.raises(ValueError, match="123 bytes"):
        transport.close(1, 4000, "x" * 200)
    transport.close(42, 4000)                             # unknown id: nothing happens


def test_close_a_connection_that_has_not_said_hello(transport):
    async def main():
        async with connect(transport.url) as ws:
            await until(lambda: 1 in transport._conns)
            transport.close(1, 4009, "nope")
            assert (await closed(ws)) == (4009, "nope")

    asyncio.run(main())
    assert transport.events() == []


def test_keepalive_ping_goes_to_joined_clients():
    t = make(ping_s=0.05)
    try:
        async def main():
            ws = await join(t, "viewer")
            assert await recv(ws) == {"type": "ping"}
            await ws.close()
        asyncio.run(main())
    finally:
        t.stop()


def test_send_before_start_and_after_stop_is_a_no_op():
    t = Transport(port=0, token=TOKEN)
    t.send({"type": "status"})
    t.send_to(1, {"type": "status"})
    t.close(1, 4000)
    with t:
        pass
    t.send({"type": "status"})
    assert not t.running


def test_stop_closes_clients_and_is_idempotent():
    t = make()
    msg = P.phase("stop", False, text="bye")

    async def main():
        ws = await join(t)
        await until(lambda: t.controller == 1)
        t.send(msg)                                      # sent before stop: delivered
        await asyncio.to_thread(t.stop)
        assert await recv(ws) == msg
        with pytest.raises(ConnectionClosed):
            await recv(ws)
        assert ws.close_code == 1001

    asyncio.run(main())
    assert not t.running and t.controller is None and t.n_viewers == 0
    assert [e.kind for e in t.events()] == ["connect", "disconnect"]
    t.stop()
    with pytest.raises(RuntimeError, match="already started"):
        t.start()


def test_port_in_use_raises_a_clear_error(transport):
    other = Transport(port=transport.port, token=TOKEN)
    with pytest.raises(OSError, match=rf"cannot listen on ws://127.0.0.1:{transport.port}"
                                      r"/ws.*--port"):
        other.start()
    assert not other.running


# -- origin -----------------------------------------------------------------------------------

def test_refused_origin_gets_403(transport, capsys):
    async def main():
        with pytest.raises(InvalidStatus, match="403"):
            await connect(transport.url, origin="https://evil.example")
        for origin in (EXTENSION_ORIGIN, "http://127.0.0.1:8000", "http://localhost:3000",
                       f"http://127.0.0.1:{transport.port}"):
            async with connect(transport.url, origin=origin) as ws:
                await ws.send(json.dumps(hello("viewer")))
                assert (await recv(ws))["type"] == "welcome"

    asyncio.run(main())
    assert "refused a request from origin 'https://evil.example'" in capsys.readouterr().err


def test_origin_patterns_let_more_origins_in():
    t = make(origins=[r"https://app\.example\.com"])
    try:
        async def main():
            async with connect(t.url, origin="https://app.example.com") as ws:
                await ws.send(json.dumps(hello("viewer")))
                assert (await recv(ws))["type"] == "welcome"
            with pytest.raises(InvalidStatus, match="403"):
                await connect(t.url, origin="https://app.example.com.evil")
        asyncio.run(main())
    finally:
        t.stop()


def test_two_origin_headers_are_refused(transport):
    status, _, _ = http_get(transport.port, "/health",
                            [("Origin", "http://localhost"), ("Origin", "https://evil.example")])
    assert status == 403


# -- HTTP -------------------------------------------------------------------------------------

def http_get(port: int, path: str, headers=()) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_accept_encoding=True)
        for k, v in headers:
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>BrainSync</title>")
    (tmp_path / "app.js").write_text("console.log(1)")
    (tmp_path / "style.css").write_text("body{}")
    (tmp_path / "data.json").write_text("{}")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01")
    (tmp_path / "secret.txt").write_text("do not serve")
    files = {n: tmp_path / n for n in ("index.html", "app.js", "style.css", "data.json",
                                       "blob.bin")}
    files["js/flicker.js"] = tmp_path / "app.js"
    t = make(static_files=files, health=lambda: {"ok": True, "board": "streaming",
                                                 "controller": False, "viewers": 0})
    yield t
    t.stop()


def test_http_serves_the_page_and_static_files(site):
    status, headers, body = http_get(site.port, "/")
    assert status == 200 and body.startswith(b"<!doctype html>")
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["cache-control"] == "no-store"
    assert headers["content-length"] == str(len(body))
    for path, ctype in (("/static/app.js", "text/javascript; charset=utf-8"),
                        ("/static/style.css", "text/css; charset=utf-8"),
                        ("/static/data.json", "application/json"),
                        ("/static/blob.bin", "application/octet-stream"),
                        ("/static/js/flicker.js", "text/javascript; charset=utf-8"),
                        ("/static/app.js?v=3", "text/javascript; charset=utf-8")):
        status, headers, _ = http_get(site.port, path)
        assert status == 200, path
        assert headers["content-type"] == ctype, path
        assert headers["cache-control"] == "no-store"


def test_http_health(site):
    status, headers, body = http_get(site.port, "/health")
    assert status == 200 and headers["content-type"] == "application/json"
    assert json.loads(body) == {"ok": True, "board": "streaming", "controller": False,
                                "viewers": 0}
    assert http_get(site.port, "/health?x=1")[0] == 200


def test_http_default_health_counts_clients(transport):
    async def main():
        ws = await join(transport)
        v = await join(transport, "viewer")
        await until(lambda: transport.n_viewers == 1)
        status, _, body = await asyncio.to_thread(http_get, transport.port, "/health")
        assert status == 200
        assert json.loads(body) == {"ok": True, "board": "unknown", "controller": True,
                                    "viewers": 1}
        await ws.close()
        await v.close()

    asyncio.run(main())


def test_http_health_failure_is_500(capsys):
    def health():
        raise RuntimeError("board gone")

    t = make(health=health)
    try:
        assert http_get(t.port, "/health")[0] == 500
        t2 = make(health=lambda: {"ok": float("nan")})
        try:
            assert http_get(t2.port, "/health")[0] == 500     # NaN never reaches a browser
        finally:
            t2.stop()
        assert http_get(t.port, "/nope")[0] == 404     # the server still runs
    finally:
        t.stop()
    assert "health() failed" in capsys.readouterr().err


@pytest.mark.parametrize("path", [
    "/nope", "/index.html", "/static/", "/static/missing.js", "/static/secret.txt",
    "/static/../secret.txt", "/static/./app.js", "/static//app.js", "/static/%2e%2e/secret.txt",
    "/static/js/../app.js", "/static/APP.JS", "/static/app.js/", "/static", "/healthz",
    "/ws/extra", "//static/app.js", "/static/..%2fsecret.txt"])
def test_http_404_and_no_path_traversal(site, path):
    status, _, body = http_get(site.port, path)
    assert status == 404, path
    assert b"do not serve" not in body


def test_http_root_without_index_is_404(transport):
    assert http_get(transport.port, "/")[0] == 404


def test_http_ws_without_upgrade_is_not_a_page(site):
    status, _, _ = http_get(site.port, "/ws")
    assert status == 426


def test_http_refused_origin_is_403(site):
    status, _, body = http_get(site.port, "/", [("Origin", "https://evil.example")])
    assert status == 403 and b"BrainSync" not in body
    assert http_get(site.port, "/", [("Origin", f"http://127.0.0.1:{site.port}")])[0] == 200


def test_websocket_on_another_path_is_not_upgraded(site):
    async def main():
        base = site.url.removesuffix("/ws")
        with pytest.raises(InvalidStatus, match="404"):
            await connect(base + "/nope")

    asyncio.run(main())


# -- /api/speech (v3.1) ---------------------------------------------------------------------

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(40)
AUTH = [("Authorization", f"Bearer {TOKEN}")]


@pytest.mark.parametrize("headers", [(), [("Authorization", "Bearer wrong")],
                                     [("Authorization", TOKEN)],
                                     [("Authorization", f"Basic {TOKEN}")]])
def test_speech_needs_the_token_when_auth_is_on(headers):
    said: list[str] = []
    t = make(speech=lambda text: said.append(text) or WAV)
    try:
        status, _, body = http_get(t.port, "/api/speech?text=hello", headers)
        assert status == 401 and "Bearer" in json.loads(body)["error"] and said == []
        assert TOKEN not in body.decode()
    finally:
        t.stop()


def test_speech_needs_no_token_when_auth_is_off():
    t = make(token=None, speech=lambda text: WAV)
    try:
        assert http_get(t.port, "/api/speech?text=hello")[0] == 200
    finally:
        t.stop()


def test_speech_serves_wav_from_the_speech_function():
    said: list[str] = []
    t = make(speech=lambda text: said.append(text) or WAV)
    try:
        status, headers, body = http_get(t.port, "/api/speech?text=Headset%20%20streaming%0A", AUTH)
        assert status == 200 and body == WAV
        assert headers["content-type"] == "audio/wav"
        assert headers["cache-control"] == "private, max-age=3600"
        assert said == ["Headset streaming"]                    # whitespace collapsed
        status, _, _ = http_get(t.port, "/api/speech?text=" + "a" * 300, AUTH)
        assert status == 200 and said[-1] == "a" * 300
    finally:
        t.stop()


@pytest.mark.parametrize("query", ["", "?text=", "?text=%20%20", "?other=x",
                                   "?text=" + "a" * 301])
def test_speech_refuses_an_empty_or_long_text(query):
    said: list[str] = []
    t = make(speech=lambda text: said.append(text) or WAV)
    try:
        status, headers, body = http_get(t.port, "/api/speech" + query, AUTH)
        assert status == 400 and headers["content-type"] == "application/json"
        assert "1..300 characters" in json.loads(body)["error"]
        assert said == []
    finally:
        t.stop()


@pytest.mark.parametrize("speech", [None, lambda text: None, lambda text: b"",
                                    lambda text: 1 / 0])
def test_speech_without_a_voice_is_503(speech, capsys):
    t = make(speech=speech)
    try:
        status, _, body = http_get(t.port, "/api/speech?text=hello", AUTH)
        assert status == 503 and "no voice" in json.loads(body)["error"]
    finally:
        t.stop()


def test_slow_speech_does_not_block_the_websocket_loop():
    release = threading.Event()

    def slow(text):
        release.wait(5)
        return WAV

    t = make(speech=slow)
    try:
        got: dict = {}
        thread = threading.Thread(target=lambda: got.update(r=http_get(t.port,
                                                                        "/api/speech?text=hi", AUTH)))
        thread.start()
        time.sleep(0.1)
        t0 = time.monotonic()
        assert http_get(t.port, "/health")[0] == 200            # served meanwhile
        assert time.monotonic() - t0 < 1.0
        release.set()
        thread.join(5)
        assert got["r"][0] == 200
    finally:
        release.set()
        t.stop()


def test_speech_must_be_callable():
    with pytest.raises(ValueError, match="speech must be a function"):
        Transport(token=TOKEN, speech="say")


def test_speech_obeys_the_origin_check():
    t = make(speech=lambda text: WAV)
    try:
        assert http_get(t.port, "/api/speech?text=hi",
                        [("Origin", "https://evil.example")])[0] == 403
    finally:
        t.stop()


def test_a_viewer_may_not_rescan(transport):
    async def main():
        v = await join(transport, role="viewer")
        await v.send(json.dumps({"type": "rescan"}))
        assert (await recv(v))["code"] == "not_allowed"
        await v.close()

    asyncio.run(main())
