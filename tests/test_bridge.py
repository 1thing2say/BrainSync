"""Tests for the WebSocket bridge, against a real websockets client on a free port."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

pytest.importorskip("websockets")

from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

from brainsync.bridge import (DEFAULT_EEG, DEFAULT_PHASE, Bridge,  # noqa: E402
                              config_message, cue_message, eeg_message, origin_allowed,
                              parse_client_message, phase_message, scroll_message,
                              status_message)
from brainsync.stimulus import load_config, override  # noqa: E402

CONFIG = config_message(load_config("reels"))


@pytest.fixture
def bridge():
    b = Bridge(port=0, config_message=CONFIG).start()
    yield b
    b.stop()


async def recv(ws, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def joined(ws) -> list[dict]:
    """The three messages every client gets on connect: config, phase, EEG state."""
    return [await recv(ws), await recv(ws), await recv(ws)]


async def until(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_config_message_follows_the_protocol():
    assert CONFIG == {
        "type": "config", "name": "reels", "waveform": "sine", "depth": 1.0,
        "refresh_hz": 60.0, "refresh_tolerance_hz": 1.0,
        "targets": [{"label": "up", "freq": 15.0, "phase": 0.0, "x": 0.55, "y": 0.325,
                     "size": 0.3},
                    {"label": "down", "freq": 17.0, "phase": 0.0, "x": 0.55, "y": -0.325,
                     "size": 0.3}]}
    json.dumps(CONFIG)   # plain JSON types only


def test_config_message_carries_the_depth():
    cfg = override(load_config("reels"), depth=0.5)
    msg = config_message(cfg)
    assert msg["depth"] == 0.5 and isinstance(msg["depth"], float)
    assert {k: v for k, v in msg.items() if k != "depth"} == {
        k: v for k, v in CONFIG.items() if k != "depth"}


def test_message_builders():
    assert phase_message("run", True, 30, "go") == {"type": "phase", "phase": "run",
                                                     "flicker": True, "seconds": 30.0,
                                                     "text": "go"}
    assert phase_message("wait", False)["seconds"] is None
    with pytest.raises(ValueError, match="phase must be"):
        phase_message("dance", True)
    assert cue_message(-1) == {"type": "cue", "target": -1}
    assert scroll_message("up", 3) == {"type": "scroll", "dir": "up", "id": 3}
    assert status_message("hi") == {"type": "status", "text": "hi"}
    assert DEFAULT_PHASE["phase"] == "wait" and DEFAULT_PHASE["flicker"] is False
    assert eeg_message("streaming", "COM3") == {"type": "eeg", "state": "streaming",
                                                "text": "COM3"}
    assert DEFAULT_EEG == {"type": "eeg", "state": "unknown", "text": ""}
    with pytest.raises(ValueError, match="EEG state must be"):
        eeg_message("happy")


def test_parse_client_message(capsys):
    assert parse_client_message('{"type": "bye"}') == {"type": "bye"}
    for bad in ("nope", "[1]", '{"type": 3}', '{"kind": "bye"}', b"\x00\x01"):
        assert parse_client_message(bad) is None
    assert capsys.readouterr().err.count("bridge: ignored") == 5
    # json.loads raises RecursionError, not ValueError, on deep nesting
    assert parse_client_message("[" * 100000 + "]" * 100000) is None
    assert "not JSON" in capsys.readouterr().err


EXTENSION_ORIGIN = "chrome-extension://" + "abcdefghijklmnop" * 2


def test_origin_allowed():
    for ok in (None, EXTENSION_ORIGIN, "http://127.0.0.1:8000", "http://localhost:8000",
               "http://localhost", "https://127.0.0.1:9"):
        assert origin_allowed(ok), ok
    for bad in ("https://www.instagram.com", "https://evil.example", "null",
                "chrome-extension://xyz", "http://127.0.0.1.evil.example",
                "http://localhost:8000.evil.example", ""):
        assert not origin_allowed(bad), bad


def test_web_pages_cannot_connect(bridge, capsys):
    async def main():
        with pytest.raises(InvalidStatus, match="403"):
            await connect(bridge.url, origin="https://evil.example")
        for origin in (EXTENSION_ORIGIN, "http://127.0.0.1:8000"):
            async with connect(bridge.url, origin=origin) as ws:
                assert await recv(ws) == CONFIG

    asyncio.run(main())
    assert "refused a connection from origin 'https://evil.example'" in capsys.readouterr().err


def test_config_then_wait_phase_on_connect(bridge):
    assert bridge.port > 0 and bridge.url == f"ws://127.0.0.1:{bridge.port}"

    async def main():
        async with connect(bridge.url + "/any/path") as ws:
            assert await joined(ws) == [CONFIG, DEFAULT_PHASE, DEFAULT_EEG]
            await until(lambda: bridge.n_clients == 1)
        await until(lambda: bridge.n_clients == 0)

    asyncio.run(main())


def test_late_joiner_gets_the_current_phase(bridge):
    calibrate = phase_message("calibrate", True, 30, "Watch the reel")
    bridge.send(calibrate)
    bridge.send(cue_message(1))       # not replayed: only the phase is
    run = phase_message("run", True, None, "Look at a square")

    async def main():
        async with connect(bridge.url) as ws:
            assert await joined(ws) == [CONFIG, calibrate, DEFAULT_EEG]
            bridge.send(run)
            assert await recv(ws) == run
        async with connect(bridge.url) as ws:
            assert await joined(ws) == [CONFIG, run, DEFAULT_EEG]

    asyncio.run(main())
    assert bridge.phase == run


def test_late_joiner_gets_the_current_eeg_state(bridge):
    bridge.send(eeg_message("absent", "plug in the receiver"))
    streaming = eeg_message("streaming", "/dev/cu.usbserial-1, 250 Hz")

    async def main():
        async with connect(bridge.url) as ws:
            assert (await joined(ws))[2] == eeg_message("absent", "plug in the receiver")
            bridge.send(streaming)
            assert await recv(ws) == streaming
        async with connect(bridge.url) as ws:
            assert (await joined(ws))[2] == streaming

    asyncio.run(main())
    assert bridge.eeg == streaming


def test_eeg_state_sent_before_start_is_kept():
    b = Bridge(port=0, config_message=CONFIG)
    absent = eeg_message("absent")
    b.send(absent)
    with b:
        async def main():
            async with connect(b.url) as ws:
                assert (await joined(ws))[2] == absent

        asyncio.run(main())


def test_phase_sent_before_start_is_kept():
    b = Bridge(port=0, config_message=CONFIG)
    stop = phase_message("stop", False, None, "bye")
    b.send(stop)
    with b:
        async def main():
            async with connect(b.url) as ws:
                assert await joined(ws) == [CONFIG, stop, DEFAULT_EEG]
        asyncio.run(main())


def test_broadcast_reaches_every_client_in_order(bridge):
    async def main():
        async with connect(bridge.url) as a, connect(bridge.url) as b:
            for ws in (a, b):
                await joined(ws)
            await until(lambda: bridge.n_clients == 2)
            sent = [cue_message(0), scroll_message("up", 1), cue_message(-1),
                    status_message("ok")]
            for msg in sent:
                bridge.send(msg)
            for ws in (a, b):
                assert [await recv(ws) for _ in sent] == sent

    asyncio.run(main())


def test_send_validates_the_message(bridge):
    with pytest.raises(ValueError, match="string \"type\""):
        bridge.send({"dir": "up"})
    with pytest.raises(ValueError):
        bridge.send("scroll")              # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bridge.send({"type": "status", "text": object()})


def test_wait_hello_and_latest_hello(bridge):
    hello = {"type": "hello", "agent": "extension", "url": "https://www.instagram.com/reels/",
             "refresh_hz": 59.97, "dropped": 0, "accepted": True}
    assert bridge.wait_hello(0.05) is None and bridge.latest_hello is None

    async def main():
        async with connect(bridge.url) as ws:
            await recv(ws)
            await ws.send(json.dumps(hello))
            got = await asyncio.to_thread(bridge.wait_hello, 5.0)
            assert got == hello

    asyncio.run(main())
    assert bridge.latest_hello == hello
    assert bridge.events() == [hello]
    assert bridge.events() == []


def test_showing_follows_each_clients_hello_and_view(bridge, capsys):
    hello = {"type": "hello", "agent": "extension", "refresh_hz": 60.0, "accepted": True}

    async def main():
        assert not bridge.showing                           # nobody connected
        async with connect(bridge.url) as a, connect(bridge.url) as b:
            await joined(a)
            await joined(b)
            await a.send(json.dumps(hello))                 # older extension: no "showing"
            await until(lambda: bridge.showing)
            await a.send(json.dumps({"type": "view", "showing": False}))
            await until(lambda: not bridge.showing)
            await b.send(json.dumps({**hello, "showing": False}))
            await b.send(json.dumps({"type": "view", "showing": "yes"}))   # ignored
            await b.send(json.dumps({"type": "view", "showing": True}))
            await until(lambda: bridge.showing)
        await until(lambda: bridge.n_clients == 0)
        assert not bridge.showing                           # gone clients stop counting

    asyncio.run(main())
    views = [e["showing"] for e in bridge.events() if e["type"] == "view"]
    assert views == [False, True]
    assert "without a true/false \"showing\"" in capsys.readouterr().err and bridge.ignored == 1


def test_malformed_messages_are_ignored_and_the_connection_survives(bridge, capsys):
    async def main():
        async with connect(bridge.url) as ws:
            await joined(ws)
            for bad in ("not json", "[1, 2]", '{"type": 5}', b"\x00binary", '{"type": "dance"}'):
                await ws.send(bad)
            await ws.send(json.dumps({"type": "scrolled", "id": 1, "ok": True,
                                      "method": "container"}))
            await ws.send(json.dumps({"type": "frames", "refresh_hz": 59.98, "dropped": 2}))
            await ws.send(json.dumps({"type": "bye"}))
            await until(lambda: bridge._events.qsize() == 3)
            bridge.send(cue_message(0))
            assert await recv(ws) == cue_message(0)

    asyncio.run(main())
    assert [e["type"] for e in bridge.events()] == ["scrolled", "frames", "bye"]
    assert bridge.ignored == 5
    err = capsys.readouterr().err
    assert "unknown type 'dance'" in err and "not JSON" in err and "binary" in err


def test_ping_keepalive():
    with Bridge(port=0, config_message=CONFIG, ping_s=0.05) as b:
        async def main():
            async with connect(b.url) as ws:
                await joined(ws)
                assert await recv(ws) == {"type": "ping"}
        asyncio.run(main())


def test_stop_closes_clients_and_is_idempotent():
    b = Bridge(port=0, config_message=CONFIG).start()
    phase = phase_message("stop", False, None, "done")

    async def main():
        async with connect(b.url) as ws:
            await joined(ws)
            await until(lambda: b.n_clients == 1)
            b.send(phase)                       # sent before stop: still delivered
            await asyncio.to_thread(b.stop)
            assert await recv(ws) == phase
            with pytest.raises(ConnectionClosed):
                await recv(ws)

    asyncio.run(main())
    assert not b.running
    b.stop()
    b.send(cue_message(0))                      # after stop: no error, nobody to send to
    with pytest.raises(RuntimeError, match="already started"):
        b.start()


def test_port_in_use_raises_a_clear_error(bridge):
    other = Bridge(port=bridge.port, config_message=CONFIG)
    with pytest.raises(OSError, match=rf"cannot listen on ws://127.0.0.1:{bridge.port}.*--ws-port"):
        other.start()
    assert not other.running


@pytest.mark.parametrize("kwargs", [{"port": -1}, {"port": 70000}, {"port": "8765"},
                                    {"ping_s": 0.0}])
def test_bad_arguments(kwargs):
    with pytest.raises(ValueError):
        Bridge(**kwargs)
