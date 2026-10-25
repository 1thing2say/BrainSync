"""Tests for the synchronous BrainSync client, against a tiny scripted v3 server on a free port.

The server here is not brainsync.server: each test scripts what the server side says, so the
client is checked against PROTOCOL-v3 alone. Everything the client sends is recorded and
checked with the server's own parsers (parse_hello, validate_client).
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from pathlib import Path

import pytest

pytest.importorskip("websockets")

from websockets.asyncio.server import serve  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from brainsync.server import client as client_mod  # noqa: E402
from brainsync.server.client import URL, Client, ServerError, main  # noqa: E402
from brainsync.server.protocol import (Target, dumps, error, layout, parse_hello,  # noqa: E402
                                       phase, selection, status, validate_client, welcome)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "s3cret-token-do-not-print"
SLOTS = [Target("yes", 15.0, 0.0, "Yes", "slot"), Target("no", 17.0, 0.0, "No", "slot")]


def make_welcome(role: str = "controller", mode: str | None = "slots", *,
                 fake: bool = False) -> dict:
    return welcome(role, mode, fake=fake, waveform="sine", refresh_hz=60.0,
                   refresh_tolerance_hz=1.0, freqs=[15.0, 17.0, 12.0, 20.0], max_slots=4,
                   detect=False)


class FakeServer:
    """A websockets asyncio server on 127.0.0.1:0 that runs ``script(ws, srv)`` per connection.

    After the script returns, the connection is drained (messages recorded) until the client
    closes it. ``received`` holds every client message in arrival order.
    """

    def __init__(self, script, *, process_request=None) -> None:
        self.script = script
        self.process_request = process_request
        self.received: list[dict] = []
        self.hellos: list = []
        self.close_codes: list[int | None] = []
        self.errors: list[BaseException] = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def start(self) -> FakeServer:
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self._thread.start()
        assert self._ready.wait(5.0), "the test server did not start"
        return self

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5.0)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(self._handler, "127.0.0.1", 0,
                         process_request=self.process_request) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    def record(self, raw) -> dict:
        msg = json.loads(raw)
        with self._lock:
            self.received.append(msg)
        return msg

    async def read(self, ws, timeout: float = 5.0) -> dict:
        return self.record(await asyncio.wait_for(ws.recv(), timeout))

    async def handshake(self, ws, *, fake: bool = False, role: str = "controller") -> dict:
        hello = await self.read(ws)
        h = parse_hello(hello, 4)
        self.hellos.append(h)
        await ws.send(dumps(make_welcome(h.role, h.mode, fake=fake)))
        return hello

    async def _handler(self, ws) -> None:
        try:
            await self.script(ws, self)
            async for raw in ws:
                self.record(raw)
        except ConnectionClosed:
            pass
        except BaseException as exc:   # surfaced by the fixture
            self.errors.append(exc)
            raise
        finally:
            self.close_codes.append(None if ws.close_code is None else int(ws.close_code))

    def types(self) -> list[str]:
        with self._lock:
            return [m["type"] for m in self.received]

    def wait_for(self, type_: str, timeout: float = 5.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                for m in self.received:
                    if m["type"] == type_:
                        return m
            time.sleep(0.01)
        raise AssertionError(f"the server got no {type_!r}; it got {self.types()}")


@pytest.fixture
def server_factory():
    servers: list[FakeServer] = []

    def make(script, **kw) -> FakeServer:
        s = FakeServer(script, **kw).start()
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()
    for s in servers:
        assert not s.errors, s.errors


async def just_welcome(ws, srv: FakeServer) -> None:
    await srv.handshake(ws)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- hello and constructor --------------------------------------------------------------------

def test_controller_hello_passes_the_servers_parser():
    c = Client(URL, TOKEN, slots=["yes", "no"])
    msg = c.hello()
    assert msg["type"] == "hello" and msg["protocol"] == 3
    h = parse_hello(json.loads(dumps(msg)), 4)
    assert h.role == "controller" and h.mode == "slots" and h.slots == ("yes", "no")
    assert h.token == TOKEN and h.accepted is True and h.takeover is False
    assert h.refresh_hz == 60.0 and h.viewport is None
    assert h.client == {"name": "brainsync-python", "kind": "python", "version": "0.1.0",
                        "instance": c.instance}
    assert len(c.instance) == 32 and c.hello()["client"]["instance"] == c.instance
    assert Client(URL, TOKEN).instance != c.instance


def test_viewer_and_point_hellos_pass_the_servers_parser():
    v = parse_hello(Client(URL, role="viewer", mode=None, refresh_hz=None).hello(), 4)
    assert v.role == "viewer" and v.mode is None and v.slots is None and v.token is None
    assert v.refresh_hz is None
    v2 = parse_hello(Client(URL, role="viewer").hello(), 4)   # default mode, slots dropped
    assert v2.mode == "slots" and v2.slots is None
    p = parse_hello(Client(URL, "t", mode="point").hello(), 4)
    assert p.mode == "point" and p.slots is None
    t = parse_hello(Client(URL, "t", takeover=True, name="x" * 100).hello(), 4)
    assert t.takeover is True and t.client["name"] == "x" * 64


def test_hello_with_accepted_false_is_passed_through_for_the_server_to_refuse():
    msg = Client(URL, "t", accepted=False).hello()
    assert msg["accepted"] is False


@pytest.mark.parametrize("kwargs, match", [
    ({"url": "http://127.0.0.1:8765/ws"}, "WebSocket URL"),
    ({"url": 8765}, "WebSocket URL"),
    ({"url": "ws://"}, "not a valid WebSocket URL"),
    ({"token": ""}, "token must be a non-empty string"),
    ({"token": 123}, "token must be a non-empty string"),
    ({"role": "admin"}, "role must be one of"),
    ({"mode": None}, "a controller needs mode"),
    ({"mode": "grid"}, "mode must be one of"),
    ({"slots": []}, "at least one slot"),
    ({"slots": None}, "slots must be a list"),
    ({"slots": "yes"}, "slots must be a list"),
    ({"slots": ("a", "a")}, "must not repeat"),
    ({"slots": ("",)}, r"slots\[0\] must be a string of 1-32"),
    ({"slots": ("ok", "x" * 33)}, r"slots\[1\] must be a string of 1-32"),
    ({"slots": (5,)}, r"slots\[0\]"),
    ({"refresh_hz": float("nan")}, "refresh_hz must be a positive"),
    ({"refresh_hz": float("inf")}, "refresh_hz must be a positive"),
    ({"refresh_hz": 0}, "refresh_hz must be a positive"),
    ({"refresh_hz": True}, "refresh_hz must be a positive"),
    ({"refresh_hz": "60"}, "refresh_hz must be a positive"),
    ({"refresh_hz": None}, "a controller must report refresh_hz"),
    ({"name": ""}, "name must be a non-empty string"),
])
def test_constructor_rejects_bad_arguments(kwargs, match):
    kw = {"url": URL, "token": "t", **kwargs}
    with pytest.raises(ValueError, match=match):
        Client(kw.pop("url"), kw.pop("token"), **kw)


def test_point_mode_ignores_slots_and_repr_hides_the_token():
    c = Client(URL, TOKEN, mode="point", slots=None)
    assert c.slots is None
    assert TOKEN not in repr(c) and "not connected" in repr(c)
    assert TOKEN not in str(vars(c).get("welcome"))


def test_calls_before_connect_say_what_to_do():
    c = Client(URL, "t")
    for call in (lambda: c.recv(0.1), lambda: c.send({"type": "bye"}), lambda: c.gaze("yes"),
                 lambda: c.until("phase", 0.1)):
        with pytest.raises(RuntimeError, match="call connect"):
            call()
    c.close()   # a no-op when not connected
    assert c.fake is False and not c.connected


# -- connect ----------------------------------------------------------------------------------

def test_connect_sends_hello_and_returns_the_welcome(server_factory):
    srv = server_factory(just_welcome)
    c = Client(srv.url, TOKEN)
    w = c.connect()
    assert w == make_welcome()
    assert c.welcome is w and c.connected and c.fake is False
    assert srv.hellos[0].token == TOKEN and srv.hellos[0].slots == ("yes", "no")
    assert "connected" in repr(c) and TOKEN not in repr(c)
    with pytest.raises(RuntimeError, match="already connected"):
        c.connect()
    c.close()
    srv.wait_for("bye")
    assert not c.connected
    c.close()   # twice is fine


def test_connect_without_token_sends_null(server_factory):
    srv = server_factory(just_welcome)
    with Client(srv.url) as c:
        assert c.welcome["type"] == "welcome"
    assert srv.received[0]["token"] is None and srv.hellos[0].token is None


def test_context_manager_says_bye_and_closes_normally(server_factory):
    srv = server_factory(just_welcome)
    with Client(srv.url, "t") as c:
        assert c.connected
    srv.wait_for("bye")
    end = time.monotonic() + 5
    while not srv.close_codes and time.monotonic() < end:
        time.sleep(0.01)
    assert srv.close_codes == [1000]
    assert srv.types() == ["hello", "bye"]


def test_fatal_error_before_welcome_raises_with_code_and_close_code(server_factory):
    async def refuse(ws, srv):
        await srv.read(ws)
        await ws.send(dumps(error("bad_token", "the token does not match", fatal=True)))
        await ws.close(4001, "bad token")

    srv = server_factory(refuse)
    c = Client(srv.url, TOKEN)
    with pytest.raises(ServerError) as info:
        c.connect()
    e = info.value
    assert e.code == "bad_token" and e.close_code == 4001 and e.reason == "bad token"
    assert "bad_token" in str(e) and "the token does not match" in str(e) and "4001" in str(e)
    assert TOKEN not in str(e)
    assert not c.connected and c.welcome is None


def test_non_fatal_error_before_welcome_raises_without_waiting(server_factory):
    async def refuse(ws, srv):
        await srv.read(ws)
        await ws.send(dumps(error("too_many_slots", "at most 4 slots", fatal=False)))
        await ws.wait_closed()   # until the client gives up and closes

    srv = server_factory(refuse)
    t0 = time.monotonic()
    with pytest.raises(ServerError) as info:
        Client(srv.url, "t").connect()
    assert time.monotonic() - t0 < 1.0
    assert info.value.code == "too_many_slots" and info.value.close_code is None


@pytest.mark.parametrize("close_code, text", [
    (4002, "another controller is connected"),
    (4003, "expected hello as the first message"),
    (4008, "no hello within 10 s"),
    (4000, "do not reconnect"),
    (4100, "code 4100"),
])
def test_close_before_welcome_raises_with_close_code(server_factory, close_code, text):
    async def slam(ws, srv):
        await srv.read(ws)
        await ws.close(close_code, "")

    srv = server_factory(slam)
    with pytest.raises(ServerError, match=text) as info:
        Client(srv.url, "t").connect()
    assert info.value.close_code == close_code and info.value.code is None
    assert "before the welcome" in str(info.value)


def test_messages_before_the_welcome_are_skipped(server_factory):
    async def chatty(ws, srv):
        await srv.read(ws)
        await ws.send(dumps({"type": "ping"}))
        await ws.send("not json")
        await ws.send(b"\x00\x01")
        await ws.send(dumps(make_welcome()))

    srv = server_factory(chatty)
    with Client(srv.url, "t") as c:
        assert c.welcome["type"] == "welcome"


def test_no_welcome_times_out(server_factory):
    async def silent(ws, srv):
        await srv.read(ws)
        await ws.wait_closed()   # until the client gives up and closes

    srv = server_factory(silent)
    c = Client(srv.url, "t")
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="no welcome"):
        c.connect(timeout=0.3)
    assert 0.25 < time.monotonic() - t0 < 2.0
    assert not c.connected


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), "5"])
def test_connect_rejects_a_bad_timeout(timeout):
    with pytest.raises(ValueError, match="timeout must be a positive"):
        Client(URL, "t").connect(timeout=timeout)


def test_welcome_with_another_protocol_raises(server_factory):
    async def old(ws, srv):
        await srv.read(ws)
        await ws.send(dumps({**make_welcome(), "protocol": 2}))

    srv = server_factory(old)
    with pytest.raises(ServerError, match="protocol 2"):
        Client(srv.url, "t").connect()


def test_refused_http_upgrade_raises_server_error(server_factory):
    def forbid(connection, request):
        return connection.respond(HTTPStatus.FORBIDDEN, "origin not allowed\n")

    srv = server_factory(just_welcome, process_request=forbid)
    with pytest.raises(ServerError, match="refused the WebSocket upgrade") as info:
        Client(srv.url, "t").connect()
    assert "403" in str(info.value)


def test_nothing_listening_raises_oserror():
    with pytest.raises(OSError):
        Client(f"ws://127.0.0.1:{free_port()}/ws", "t").connect(timeout=2.0)


# -- recv, until, send ------------------------------------------------------------------------

def test_recv_returns_none_on_timeout(server_factory):
    srv = server_factory(just_welcome)
    with Client(srv.url, "t") as c:
        t0 = time.monotonic()
        assert c.recv(timeout=0.15) is None
        assert 0.1 < time.monotonic() - t0 < 1.0
        assert c.recv(timeout=0) is None
        assert c.recv(timeout=-1) is None


def test_recv_skips_bad_frames_and_tracks_phase_and_layout(server_factory, capsys):
    async def script(ws, srv):
        await srv.handshake(ws)
        await ws.send("{oops")
        await ws.send(b"\x01\x02\x03")
        await ws.send(json.dumps([1, 2]))
        await ws.send(json.dumps({"type": 5}))
        await ws.send(dumps(phase("calibrate", False, 10.0, "relax")))
        await ws.send(dumps(layout(3, "slots", SLOTS)))
        await ws.send(dumps({"type": "ping"}))

    srv = server_factory(script)
    with Client(srv.url, "t") as c:
        ph = c.recv()
        assert ph["phase"] == "calibrate" and c.phase is ph
        lay = c.recv()
        assert lay["layout_id"] == 3 and c.layout is lay
        assert c.recv() == {"type": "ping"}
    err = capsys.readouterr().err
    assert "not JSON" in err and "binary frame of 3 bytes" in err
    assert err.count("without a string \"type\"") == 2


def test_recv_raises_when_the_server_closes(server_factory):
    async def replace(ws, srv):
        await srv.handshake(ws)
        await ws.send(dumps(phase("run", True)))
        await ws.close(4000, "replaced")

    srv = server_factory(replace)
    with Client(srv.url, "t") as c:
        assert c.recv()["type"] == "phase"
        with pytest.raises(ServerError, match="do not reconnect") as info:
            c.recv()
        assert info.value.close_code == 4000 and info.value.reason == "replaced"
        assert "before the welcome" not in str(info.value)
        with pytest.raises(ServerError):   # still closed
            c.recv(0.1)
        with pytest.raises(ServerError):
            c.send({"type": "pause"})


def test_until_skips_other_messages_and_honours_pred(server_factory):
    async def script(ws, srv):
        await srv.handshake(ws)
        await ws.send(dumps(status("streaming", True, "ok", 12)))
        await ws.send(dumps(phase("calibrate", False, 5.0)))
        await ws.send(dumps(selection(1, 3, "no", 10.0, 0.5, {"yes": 0.1, "no": 0.5})))
        await ws.send(dumps(selection(2, 3, "yes", 11.0, 1.8, {"yes": 1.8, "no": 0.2})))

    srv = server_factory(script)
    with Client(srv.url, "t") as c:
        sel = c.until("selection", pred=lambda m: m["slot"] == "yes")
        assert sel["selection_id"] == 2 and sel["score"] == 1.8
        assert c.phase["phase"] == "calibrate"   # skipped messages still tracked


def test_until_times_out(server_factory):
    async def script(ws, srv):
        await srv.handshake(ws)
        for _ in range(20):
            await ws.send(dumps(status("streaming", True)))
            await asyncio.sleep(0.02)

    srv = server_factory(script)
    with Client(srv.url, "t") as c:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="no 'selection' message within 0.3 s"):
            c.until("selection", timeout=0.3)
        assert 0.25 < time.monotonic() - t0 < 1.5
        with pytest.raises(TimeoutError):
            c.until("phase", timeout=0)


def test_send_rejects_non_messages_and_nan(server_factory):
    srv = server_factory(just_welcome)
    with Client(srv.url, "t") as c:
        for bad in ({"no": "type"}, {"type": 3}, ["type"], "bye", None):
            with pytest.raises(ValueError, match="string \"type\""):
                c.send(bad)
        with pytest.raises(ValueError):
            c.send({"type": "frames", "refresh_hz": float("nan"), "dropped": 0})
        c.send({"type": "frames", "refresh_hz": 59.98, "dropped": 0})
        srv.wait_for("frames")
    assert validate_client(srv.wait_for("frames")) == {"type": "frames", "refresh_hz": 59.98,
                                                        "dropped": 0}


# -- gaze -------------------------------------------------------------------------------------

def test_gaze_message_shape(server_factory):
    async def script(ws, srv):
        await srv.handshake(ws, fake=True)

    srv = server_factory(script)
    with Client(srv.url, "t") as c:
        assert c.fake is True
        c.gaze("yes")
        c.gaze(None)
        for bad in ("", "x" * 33, 3, True):
            with pytest.raises(ValueError, match="gaze slot must be a string of 1-32"):
                c.gaze(bad)
    srv.wait_for("bye")
    gazes = [m for m in srv.received if m["type"] == "gaze"]
    assert gazes == [{"type": "gaze", "slot": "yes"}, {"type": "gaze", "slot": None}]
    assert [validate_client(g) for g in gazes] == gazes


# -- auto onset -------------------------------------------------------------------------------

async def onset_script(ws, srv):
    """Layouts and phases in an order that tests each onset rule; ends with a status."""
    await srv.handshake(ws)
    steps = [
        phase("calibrate", False, 10.0),     # targets drawn still: no onset
        layout(1, "slots", SLOTS),
        phase("run", True),                  # onset 1
        layout(1, "slots", SLOTS),           # same layout again: none
        phase("run", True),                  # run again, same layout: none
        layout(2, "slots", SLOTS),           # onset 2
        phase("pause", False),
        layout(3, "slots", SLOTS),           # paused: none yet
        phase("run", True),                  # onset 3
        layout(4, "idle", []),               # no targets: none
        phase("run", True),                  # still idle: none
        phase("wait", False),
        layout(5, "slots", SLOTS),           # wait: none
        {"type": "phase", "phase": "run", "flicker": False},   # run without flicker: none
        layout(6, "slots", SLOTS),
        phase("run", True),                  # onset 6
        {"type": "layout", "layout_id": "x", "stage": "slots",
         "targets": [t.to_dict() for t in SLOTS]},             # bad id: none, noted
        layout(7, "slots", SLOTS),           # onset 7
        status("streaming", True, "done"),
    ]
    for msg in steps:
        await ws.send(dumps(msg))


def run_onset_script(server_factory, **kw):
    srv = server_factory(onset_script)
    t0 = time.time()
    with Client(srv.url, "t", **kw) as c:
        c.until("status")
        onsets_sent = c.onsets
    t1 = time.time()
    srv.wait_for("bye")
    return srv, [m for m in srv.received if m["type"] == "onset"], onsets_sent, t0, t1


def test_auto_onset_sends_one_onset_per_layout_while_running(server_factory, capsys):
    srv, onsets, sent, t0, t1 = run_onset_script(server_factory)
    assert [m["layout_id"] for m in onsets] == [1, 2, 3, 6, 7]
    assert sent == 5
    for m in onsets:
        assert validate_client(m) == m
        assert t0 <= m["t_client"] <= t1
    types = srv.types()
    assert types[0] == "hello" and types[-1] == "bye"
    assert "no onset for a layout without a valid layout_id: 'x'" in capsys.readouterr().err


def test_auto_onset_off_sends_nothing(server_factory):
    _, onsets, sent, _, _ = run_onset_script(server_factory, auto_onset=False)
    assert onsets == [] and sent == 0


def test_viewers_never_send_onsets(server_factory):
    _, onsets, sent, _, _ = run_onset_script(server_factory, role="viewer", mode=None,
                                             refresh_hz=None)
    assert onsets == [] and sent == 0


def test_reconnect_resets_the_onset_memory(server_factory):
    async def script(ws, srv):
        await srv.handshake(ws)
        await ws.send(dumps(phase("run", True)))
        await ws.send(dumps(layout(1, "slots", SLOTS)))
        await ws.send(dumps(status("streaming", True)))

    srv = server_factory(script)
    c = Client(srv.url, "t")
    for _ in range(2):
        c.connect()
        c.until("status")
        c.close()
    end = time.monotonic() + 5
    while srv.types().count("bye") < 2 and time.monotonic() < end:
        time.sleep(0.01)
    assert [m["layout_id"] for m in srv.received if m["type"] == "onset"] == [1, 1]
    assert c.onsets == 2


# -- command line -----------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["--url", "http://127.0.0.1:8765/ws"],
    ["--url", "ws://"],
    ["--slots", "yes,,no"],
    ["--slots", ""],
    ["--slots", "a,a"],
    ["--slots", "x" * 33],
    ["--refresh-hz", "nan"],
    ["--refresh-hz", "0"],
    ["--refresh-hz", "fast"],
    ["--count", "0"],
    ["--count", "two"],
    ["--seconds", "0"],
    ["--seconds", "inf"],
    ["--timeout", "-1"],
    ["--role", "admin"],
    ["--role", "viewer", "--gaze", "yes"],
    ["--role", "viewer", "--takeover"],
    ["--gaze", "maybe"],
    ["--token", ""],
    ["--token", "a", "--token-file", "b"],
    ["--bogus"],
])
def test_cli_argument_errors_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 2
    assert "error" in capsys.readouterr().err


def test_cli_token_file_problems_exit_2(tmp_path, capsys):
    assert main(["--token-file", str(tmp_path / "missing")]) == 2
    assert "cannot use --token-file" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    assert main(["--token-file", str(empty)]) == 2
    assert "is empty" in capsys.readouterr().err


def selection_script(*, fake: bool = True, expect_gaze: bool = True):
    async def script(ws, srv):
        await srv.handshake(ws, fake=fake)
        if expect_gaze:
            await srv.read(ws)   # the gaze comes right after the welcome
        await ws.send(dumps(phase("run", True, text="go")))
        await ws.send(dumps(layout(1, "slots", SLOTS)))
        await ws.send(dumps(error("refresh", "60 Hz please")))
        await ws.send(dumps(selection(1, 1, "yes", 10.0, 1.8, {"yes": 1.8, "no": 0.2})))
        await ws.send(dumps(selection(2, 1, "no", 11.0, 0.75, {"yes": 0.1, "no": 0.75})))
    return script


def test_cli_prints_selections_until_count(server_factory, tmp_path, capsys):
    srv = server_factory(selection_script())
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    code = main(["--url", srv.url, "--token-file", str(token_file), "--gaze", "yes",
                 "--count", "2"])
    out, err = capsys.readouterr()
    assert code == 0
    assert out.splitlines() == ["yes  score 1.80  layout 1", "no  score 0.75  layout 1"]
    assert "connected to" in err and "fake board" in err and "phase run: go" in err
    assert "error refresh: 60 Hz please" in err
    assert TOKEN not in out and TOKEN not in err
    srv.wait_for("bye")
    assert srv.hellos[0].token == TOKEN
    assert srv.wait_for("gaze") == {"type": "gaze", "slot": "yes"}
    assert srv.wait_for("onset")["layout_id"] == 1


def test_cli_json_output(server_factory, capsys):
    srv = server_factory(selection_script(expect_gaze=False))
    assert main(["--url", srv.url, "--token", "t", "--count", "1", "--json"]) == 0
    line = capsys.readouterr().out.strip()
    assert json.loads(line) == selection(1, 1, "yes", 10.0, 1.8, {"yes": 1.8, "no": 0.2})


def test_cli_gaze_is_ignored_without_a_fake_board(server_factory, capsys):
    srv = server_factory(selection_script(fake=False, expect_gaze=False))
    assert main(["--url", srv.url, "--gaze", "yes", "--count", "1"]) == 0
    assert "--gaze ignored" in capsys.readouterr().err
    srv.wait_for("bye")
    assert "gaze" not in srv.types()


def test_cli_viewer_sends_a_viewer_hello(server_factory, capsys):
    srv = server_factory(selection_script(expect_gaze=False))
    assert main(["--url", srv.url, "--role", "viewer", "--count", "1"]) == 0
    h = srv.hellos[0]
    assert h.role == "viewer" and h.mode is None and h.slots is None
    srv.wait_for("bye")
    assert "onset" not in srv.types()


def test_cli_refused_hello_exits_1_without_printing_the_token(server_factory, capsys):
    async def refuse(ws, srv):
        await srv.read(ws)
        await ws.send(dumps(error("bad_token", "the token does not match", fatal=True)))
        await ws.close(4001, "bad token")

    srv = server_factory(refuse)
    assert main(["--url", srv.url, "--token", TOKEN]) == 1
    out, err = capsys.readouterr()
    assert "bad_token" in err and "4001" in err
    assert TOKEN not in out + err


def test_cli_nothing_listening_exits_1(capsys):
    url = f"ws://127.0.0.1:{free_port()}/ws"
    assert main(["--url", url, "--timeout", "2"]) == 1
    assert f"cannot connect to {url}" in capsys.readouterr().err


def test_cli_unexpected_close_exits_1(server_factory, capsys):
    async def drop(ws, srv):
        await srv.handshake(ws)
        await ws.close(1011, "boom")

    srv = server_factory(drop)
    assert main(["--url", srv.url]) == 1
    assert "code 1011, boom" in capsys.readouterr().err


def test_cli_server_stop_exits_0(server_factory, capsys):
    async def stop(ws, srv):
        await srv.handshake(ws)
        await ws.send(dumps(phase("stop", False, text="Server shutting down")))
        await ws.close(1001, "")

    srv = server_factory(stop)
    assert main(["--url", srv.url]) == 0
    err = capsys.readouterr().err
    assert "phase stop: Server shutting down" in err and "the server stopped" in err


def test_cli_seconds_limit_exits_0(server_factory):
    srv = server_factory(just_welcome)
    t0 = time.monotonic()
    assert main(["--url", srv.url, "--seconds", "0.3"]) == 0
    assert 0.25 < time.monotonic() - t0 < 2.0
    srv.wait_for("bye")


def test_cli_ctrl_c_exits_0(server_factory, monkeypatch, capsys):
    srv = server_factory(just_welcome)

    def interrupt(client, args):
        raise KeyboardInterrupt

    monkeypatch.setattr(client_mod, "_loop", interrupt)
    assert main(["--url", srv.url]) == 0
    assert "stopped" in capsys.readouterr().err
    srv.wait_for("bye")


def test_module_runs_with_python_dash_m():
    run = subprocess.run([sys.executable, "-m", "brainsync.server.client", "--help"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0 and "--slots" in run.stdout and "--token-file" in run.stdout
    run = subprocess.run([sys.executable, "-m", "brainsync.server.client", "--count", "0"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert run.returncode == 2 and "--count must be at least 1" in run.stderr
