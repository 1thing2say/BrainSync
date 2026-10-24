"""Tests for the BrainSwipe browser mock's server, headless: the engine on a virtual
FakeBoard (time moves only when a test advances it), the HTTP server on a free port in a
thread, and the CLI in a subprocess."""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from brainsync import swipe_web
from brainsync.classify import ABSTAIN
from brainsync.profiles import load_deck, render_deck
from brainsync.swipe import LIKE, PASS, card_order
from brainsync.swipe_web import Engine, Subscriber, build_engine

ROOT = Path(__file__).resolve().parents[1]
STATE_KEYS = {"name", "decoder", "labels", "freqs", "colors", "votes", "window_s", "step_s",
              "cooldown_s", "switch_s", "gaze", "card", "n_cards", "deck", "counts", "started"}
COUNT_KEYS = {"swipes", "likes", "passes", "matches", "stale"}
WINDOW_KEYS = {"t", "scores", "raw", "pending", "streak", "votes", "stale", "gaze", "true"}
SWIPE_KEYS = {"target", "label", "source", "swiped", "card", "liked", "match", "voice", "counts"}
POLL = 0.1   # virtual seconds per board read, as the decoder thread


@pytest.fixture(scope="module")
def deck():
    return load_deck()


@pytest.fixture(scope="module")
def cards(deck, tmp_path_factory) -> list[Path]:
    """The deck drawn in the image order of seed 0 (every engine here uses seed 0)."""
    order = build_engine(deck, calibrate_s=None).order
    return render_deck([deck[i] for i in order], tmp_path_factory.mktemp("cards"))


@pytest.fixture(scope="module")
def web_dir(tmp_path_factory) -> Path:
    """A stand-in frontend folder, with a file beside it that must never be served."""
    root = tmp_path_factory.mktemp("site")
    web = root / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>BrainSwipe</title>", "utf-8")
    (web / "app.js").write_text("export const x = 1;\n", "utf-8")
    (web / "style.css").write_text("body { color: #eee; }\n", "utf-8")
    (web / ".hidden.js").write_text("secret\n", "utf-8")
    (web / "notes.py").write_text("secret\n", "utf-8")
    (root / "secret.js").write_text("secret\n", "utf-8")
    return web


def engine_virtual(deck, **kw) -> Engine:
    return build_engine(deck, realtime=False, **kw)


def advance(engine: Engine, seconds: float) -> list[dict]:
    """Run the decoder for ``seconds`` of virtual time; the window events sent."""
    out = []
    for _ in range(int(round(seconds / POLL))):
        engine.board.advance(POLL)
        out.extend(engine.step())
    return out


def drain(sub: Subscriber) -> list[tuple[str, dict]]:
    out = []
    while (item := sub.get(timeout=0)) is not None:
        out.append(item)
    return out


def swipes_of(items: list[tuple[str, dict]]) -> list[dict]:
    return [data for event, data in items if event == "swipe"]


@contextlib.contextmanager
def serving(engine: Engine, cards=(), web_dir: Path | None = None,
            ping_s: float = 15.0) -> Iterator[swipe_web.SwipeServer]:
    kw = {} if web_dir is None else {"web_dir": web_dir}
    server = swipe_web.make_server("127.0.0.1", 0, engine=engine, cards=cards,
                                   ping_s=ping_s, **kw)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield server
    finally:
        engine.close()   # ends the SSE streams first, so their threads exit
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, method: str, path: str, body: object = None,
            headers: dict | None = None) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def get_json(server, path: str) -> tuple[int, dict]:
    status, headers, body = request(server, "GET", path)
    assert headers["content-type"].startswith("application/json")
    assert headers["cache-control"] == "no-store"
    return status, json.loads(body)


def post_json(server, path: str, body) -> tuple[int, dict]:
    status, headers, raw = request(server, "POST", path, body,
                                   {"Content-Type": "application/json"})
    assert headers["cache-control"] == "no-store"
    return status, json.loads(raw)


class Sse:
    """A raw SSE client: next() returns (event, data) or ("ping", None) for a comment."""

    def __init__(self, server, timeout: float = 5) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                               timeout=timeout)
        self.conn.request("GET", "/api/events")
        self.resp = self.conn.getresponse()
        assert self.resp.status == 200
        assert self.resp.getheader("Content-Type").startswith("text/event-stream")
        assert self.resp.getheader("Cache-Control") == "no-store"

    def next(self) -> tuple[str, dict | None]:
        event, data = "message", []
        while True:
            line = self.resp.fp.readline().decode("utf-8")
            if not line:
                raise EOFError("stream closed")
            line = line.rstrip("\n")
            if line == "":
                if data:
                    return event, json.loads("\n".join(data))
                continue
            if line.startswith(":"):
                return "ping", None
            key, _, value = line.partition(": ")
            if key == "event":
                event = value
            elif key == "data":
                data.append(value)

    def until(self, event: str, limit: int = 1000) -> dict:
        for _ in range(limit):
            name, data = self.next()
            if name == event:
                return data
        raise AssertionError(f"no {event} event in {limit}")

    def close(self) -> None:
        self.conn.close()


def png_size(body: bytes) -> tuple[int, int]:
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", body[16:24])


# -- engine -------------------------------------------------------------------------------

def test_state_shape_and_image_mapping(deck):
    e = build_engine(deck, calibrate_s=None, seed=3)
    s = e.state()
    assert set(s) == STATE_KEYS
    assert s["name"] == "BrainSwipe" and s["decoder"] is False
    assert s["labels"] == ["Pass", "Like"] and s["freqs"] == [15.0, 17.0]
    assert s["colors"] == ["#d0605a", "#5aae6e"]   # swipe.toml [card] flash_colors
    assert s["card"] == 0 and s["gaze"] == "card" and s["n_cards"] == len(deck)
    assert set(s["counts"]) == COUNT_KEYS and not any(s["counts"].values())
    # image k shows deck[order[k]], and the feed's first card is image 0
    order = card_order(len(deck), e.feed.current)
    assert e.order == order
    for k, entry in enumerate(s["deck"]):
        p = deck[order[k]]
        assert entry == {"name": p.name, "age": p.age, "detail": p.detail, "color": p.color}
    assert s["deck"][0]["name"] == deck[e.feed.current].name
    json.dumps(s)   # serialisable as is


def test_key_swipe_moves_the_feed_and_reports_the_swiped_profile(deck):
    e = build_engine(deck, calibrate_s=None, match_rate=1.0)
    sub = e.subscribe()
    assert drain(sub)[0][0] == "state"
    before = e.card
    name = e.state()["deck"][before]["name"]
    out = e.key_swipe(LIKE)
    assert set(out) == SWIPE_KEYS
    assert out["source"] == "key" and out["label"] == "Like" and out["liked"] and out["match"]
    assert out["swiped"] == before and out["card"] == e.card != before
    assert out["voice"] == f"{name} liked you back. It's a match!"
    assert out["counts"] == {"swipes": 1, "likes": 1, "passes": 0, "matches": 1, "stale": 0}
    assert swipes_of(drain(sub)) == [out]
    p = e.key_swipe(PASS)
    assert p["voice"] == "Pass" and not p["liked"] and not p["match"]
    for bad in (True, 2, -1, "1", 1.0, None):
        with pytest.raises(ValueError):
            e.key_swipe(bad)


def test_gaze_needs_a_decoder_and_a_known_value(deck):
    with pytest.raises(swipe_web.NoDecoder):
        build_engine(deck, calibrate_s=None).set_gaze("like")
    e = engine_virtual(deck)
    assert e.board.active_target() == ABSTAIN   # reading the card: no tone
    sub = e.subscribe()
    drain(sub)
    for gaze, target in (("like", LIKE), ("pass", PASS), ("card", ABSTAIN)):
        assert e.set_gaze(gaze) == gaze
        assert e.board.active_target() == target
    e.set_gaze("auto")
    assert e.board._forced is None   # the board's own switch schedule
    assert [d["gaze"] for ev, d in drain(sub) if ev == "gaze"] == ["like", "pass", "card", "auto"]
    e.set_gaze("auto")
    assert drain(sub) == []   # no news, no event
    with pytest.raises(ValueError):
        e.set_gaze("left")


def test_held_like_gaze_swipes_like_within_4_s_then_every_3_s(deck):
    e = engine_virtual(deck)
    sub = e.subscribe()
    e.set_gaze("like")
    advance(e, 4.5)   # from an empty buffer: 3.5 s fills window + pad, then 4 fresh votes
    first = swipes_of(drain(sub))
    assert [s["target"] for s in first] == [LIKE]
    assert first[0]["source"] == "decoder"
    # after a settled stretch of reading, the change of gaze is what starts the clock
    e.set_gaze("card")
    advance(e, 6.0)
    drain(sub)
    e.set_gaze("like")
    t0 = e.board.elapsed
    times = []
    for _ in range(int(12 / POLL)):
        advance(e, POLL)
        times += [e.board.elapsed for _ in swipes_of(drain(sub))]
    assert times and times[0] - t0 < 4.0
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(gaps) >= 2
    # cooldown + window + 3 hops = 744 samples = 2.976 s, read every 0.1 s
    assert all(2.85 <= g <= 3.15 for g in gaps), gaps


def test_window_events_carry_the_decoder_state(deck):
    e = engine_virtual(deck)
    sub = e.subscribe()
    e.set_gaze("like")
    windows = advance(e, 8.0)
    assert len(windows) == len([d for ev, d in drain(sub) if ev == "window"]) > 10
    for w in windows:
        assert set(w) == WINDOW_KEYS
        assert len(w["scores"]) == 2 and all(isinstance(x, float) for x in w["scores"])
        assert w["raw"] in (-1, 0, 1) and w["pending"] in (-1, 0, 1)
        assert 0 <= w["streak"] < w["votes"] == 4
        assert w["gaze"] == "like" and w["true"] == LIKE
    ts = [w["t"] for w in windows]
    assert ts[0] == pytest.approx(3.5) and all(b - a == pytest.approx(0.248) for a, b in
                                               zip(ts, ts[1:]))
    # right after a swipe the overlapping windows still hold EEG from the old card
    assert any(w["stale"] for w in windows)
    assert e.swiper.stale == sum(w["stale"] for w in windows)


def test_reading_the_card_rarely_swipes(deck):
    """Gaze on the card: the board carries no tone, so every swipe here is a false one.

    The decoder does fire on pure noise now and then. Pooled over seeds 0..7 (30 s of
    reading each, after 10 s to settle) it gave 26 false swipes in 240 s, 6.5 a minute.
    The bound, 8 a minute, holds that rate rather than one noise draw, and still fails a
    real regression: without the rest threshold (online.TUNED.threshold=None) the same
    run gives 50, 12.5 a minute."""
    seconds, n_false = 30.0, 0
    for seed in range(8):
        e = engine_virtual(deck, seed=seed)
        sub = e.subscribe()
        advance(e, 10.0)
        drain(sub)
        windows = advance(e, seconds)
        assert all(w["true"] == ABSTAIN and w["gaze"] == "card" for w in windows)
        n_false += len(swipes_of(drain(sub)))
    per_minute = n_false * 60 / (8 * seconds)
    assert per_minute <= 8.0, f"{n_false} false swipes in {8 * seconds:g} s"


def test_key_swipe_resets_the_streak_so_the_decoder_cannot_double_fire(deck):
    e = engine_virtual(deck)
    sub = e.subscribe()
    e.set_gaze("like")
    advance(e, 4.5)
    assert swipes_of(drain(sub))
    # wait until a fresh run is building towards the next Like
    for _ in range(100):
        advance(e, POLL)
        assert not swipes_of(drain(sub))
        if e.swiper.streak >= 2:
            break
    assert e.swiper.pending == LIKE and e.swiper.streak >= 2
    e.key_swipe(LIKE)
    assert e.swiper.pending == ABSTAIN and e.swiper.streak == 0
    t_key = e.board.elapsed
    times = []
    for _ in range(50):
        advance(e, POLL)
        times += [e.board.elapsed for s in swipes_of(drain(sub)) if s["source"] == "decoder"]
    # only windows wholly after the key swipe + cooldown vote: >= 0.5 + 1.5 + 3 x 0.248
    assert times and times[0] - t_key >= 2.7


def test_a_key_swipe_for_a_card_already_swiped_is_refused(deck):
    """The decoder swipes card A; a Shift+Arrow pressed while A was still on the page
    names A and must not swipe B, which nobody has seen yet."""
    e = engine_virtual(deck, match_rate=1.0)
    sub = e.subscribe()
    seen = e.card
    e.set_gaze("like")
    for _ in range(100):
        advance(e, POLL)
        if e.feed.swipes:
            break
    [fired] = swipes_of(drain(sub))
    assert fired["source"] == "decoder" and fired["swiped"] == seen
    with pytest.raises(swipe_web.CardChanged) as exc:
        e.key_swipe(LIKE, card=seen)
    assert exc.value.card == e.card == fired["card"]
    assert e.feed.swipes == 1 and e.feed.likes == 1 and swipes_of(drain(sub)) == []
    # naming the card on screen (or no card at all, as before) swipes it
    out = e.key_swipe(PASS, card=e.card)
    assert out["swiped"] == fired["card"] and out["source"] == "key"
    assert e.key_swipe(PASS)["swiped"] == out["card"]
    for bad in (True, 1.0, "0"):
        with pytest.raises(ValueError):
            e.key_swipe(PASS, card=bad)


def test_a_slow_client_never_blocks_the_decoder(deck):
    e = engine_virtual(deck, queue_size=16)
    slow = e.subscribe()          # never read
    fast = e.subscribe()
    e.set_gaze("like")
    longest = 0.0
    seen = []
    for _ in range(int(40 / POLL)):
        e.board.advance(POLL)
        t = time.perf_counter()
        e.step()
        longest = max(longest, time.perf_counter() - t)
        seen += drain(fast)
    n_windows = sum(ev == "window" for ev, _ in seen)
    n_swipes = len(swipes_of(seen))
    assert n_windows > 100 and n_swipes >= 10
    assert len(slow) == 16 and slow.dropped > 0
    assert longest < 0.5
    # windows go first: the slow client still has the newest swipes, in order
    kept = [data for _, data in iter(lambda: slow.get(timeout=0), None)]
    swipes = [d for d in kept if "label" in d]
    assert swipes == swipes_of(seen)[-len(swipes):]


def test_subscriber_drops_old_windows_first_and_closes():
    sub = Subscriber(3)
    sub.put("swipe", {"n": 1})
    sub.put("window", {"n": 2})
    sub.put("window", {"n": 3})
    sub.put("gaze", {"n": 4})
    assert [d["n"] for _, d in iter(lambda: sub.get(0), None)] == [1, 3, 4]
    for n in range(5):
        sub.put("swipe", {"n": n})
    assert [d["n"] for _, d in iter(lambda: sub.get(0), None)] == [2, 3, 4]
    t = time.perf_counter()
    assert sub.get(timeout=0.05) is None and time.perf_counter() - t >= 0.04
    threading.Timer(0.05, sub.close).start()
    assert sub.get(timeout=5) is None and sub.closed
    sub.put("swipe", {})
    assert len(sub) == 0
    with pytest.raises(ValueError):
        Subscriber(0)


def test_engine_rejects_mismatched_parts(deck):
    e = build_engine(deck, calibrate_s=None)
    with pytest.raises(ValueError, match="both or neither"):
        Engine(deck, e.feed, e.look, decoder=engine_virtual(deck).decoder)
    with pytest.raises(ValueError, match="deck has"):
        Engine(deck[:3], e.feed, e.look)
    with pytest.raises(ValueError, match="calibration needs"):
        engine_virtual(deck, calibrate_s=2.0)


# -- HTTP ---------------------------------------------------------------------------------

def test_api_state_over_http(deck, cards, web_dir):
    e = engine_virtual(deck)
    with serving(e, cards, web_dir) as server:
        status, s = get_json(server, "/api/state")
    assert status == 200
    assert set(s) == STATE_KEYS and s["decoder"] is True
    assert s["votes"] == 4 and s["window_s"] == 1.5 and s["step_s"] == 0.25
    assert s["cooldown_s"] == 0.5 and s["switch_s"] == 8.0 and len(s["deck"]) == 20


def test_static_files_are_whitelisted(deck, cards, web_dir):
    with serving(build_engine(deck, calibrate_s=None), cards, web_dir) as server:
        status, headers, body = request(server, "GET", "/")
        assert status == 200 and body.startswith(b"<!doctype html>")
        assert headers["content-type"] == "text/html; charset=utf-8"
        status, headers, body = request(server, "GET", "/static/app.js")
        assert status == 200 and headers["content-type"] == "text/javascript; charset=utf-8"
        assert body == b"export const x = 1;\n"
        status, headers, _ = request(server, "GET", "/static/style.css?v=2")
        assert status == 200 and headers["content-type"] == "text/css; charset=utf-8"
        for path in ("/static/missing.js", "/static/", "/static/.hidden.js", "/static/notes.py",
                     "/static/../secret.js", "/static/%2e%2e/secret.js",
                     "/static/%2e%2e%2fsecret.js", "/static/..%2fsecret.js",
                     "/static/web/../app.js", "/static//etc/passwd", "/static/%2Fetc%2Fpasswd",
                     f"/static/{web_dir / 'app.js'}", "/static/./app.js", "/secret.js",
                     "/../secret.js", "/index.html", "/api/nothing"):
            status, _, body = request(server, "GET", path)
            assert status == 404, path
            assert b"secret" not in body


def test_missing_frontend_is_a_404_not_a_crash(deck, tmp_path):
    with serving(build_engine(deck, calibrate_s=None), web_dir=tmp_path / "absent") as server:
        assert request(server, "GET", "/")[0] == 404
        assert get_json(server, "/api/state")[0] == 200


def test_card_images(deck, cards, web_dir):
    with serving(build_engine(deck, calibrate_s=None), cards, web_dir) as server:
        status, headers, body = request(server, "GET", "/cards/0.png")
        assert status == 200 and headers["content-type"] == "image/png"
        assert png_size(body) == (600, 900)
        assert body == cards[0].read_bytes()
        assert request(server, "GET", "/cards/19.png")[0] == 200
        for path in ("/cards/20.png", "/cards/-1.png", "/cards/x.png", "/cards/1.jpg",
                     "/cards/1.png/x", "/cards/9999999.png", "/cards/"):
            assert request(server, "GET", path)[0] == 404, path


def test_post_gaze(deck, cards):
    e = engine_virtual(deck)
    with serving(e, cards) as server:
        assert post_json(server, "/api/gaze", {"gaze": "like"}) == (200, {"gaze": "like"})
        assert e.board.active_target() == LIKE
        assert post_json(server, "/api/gaze", {"gaze": "auto"}) == (200, {"gaze": "auto"})
        assert get_json(server, "/api/state")[1]["gaze"] == "auto"
        for body in ({"gaze": "left"}, {"gaze": 1}, {}, [1], b"not json", b"\xff\xfe",
                     b'"like"'):
            status, data = post_json(server, "/api/gaze", body)
            assert status == 400 and "error" in data, body
        assert e.gaze == "auto"
        assert post_json(server, "/api/nothing", {})[0] == 404


def test_post_gaze_without_decoder_is_409(deck, cards):
    with serving(build_engine(deck, calibrate_s=None), cards) as server:
        assert post_json(server, "/api/gaze", {"gaze": "like"}) == (409, {"error": "no decoder"})
        assert get_json(server, "/api/state")[1]["decoder"] is False
        assert post_json(server, "/api/swipe", {"target": 1})[0] == 200


def test_post_swipe_advances_the_card_and_reaches_sse_clients(deck, cards):
    e = engine_virtual(deck)
    with serving(e, cards) as server:
        sse = Sse(server)
        try:
            name, state = sse.next()
            assert name == "state" and set(state) == STATE_KEYS and state["card"] == 0
            status, out = post_json(server, "/api/swipe", {"target": 0})
            assert status == 200 and set(out) == SWIPE_KEYS
            assert out["source"] == "key" and out["swiped"] == 0 and out["card"] != 0
            assert out["counts"]["passes"] == 1
            assert sse.until("swipe") == out
            _, s = get_json(server, "/api/state")
            assert s["card"] == out["card"] and s["counts"] == out["counts"]
            # decoder windows and gaze changes stream too
            post_json(server, "/api/gaze", {"gaze": "like"})
            assert sse.until("gaze") == {"gaze": "like"}
            advance(e, 4.0)
            w = sse.until("window")
            assert set(w) == WINDOW_KEYS and w["gaze"] == "like"
        finally:
            sse.close()
        for body in ({"target": 2}, {"target": True}, {"target": "1"}, {"target": 1.0}, {},
                     b"{", {"target": 0, "card": "1"}, {"target": 0, "card": -1},
                     {"target": 0, "card": True}):
            assert post_json(server, "/api/swipe", body)[0] == 400, body
        assert e.feed.swipes == 1
        # the page names the card it shows: a stale one is a 409 that says which is on
        assert post_json(server, "/api/swipe", {"target": 1, "card": 0}) == (
            409, {"error": "card changed", "card": e.card})
        assert e.feed.swipes == 1
        status, out = post_json(server, "/api/swipe", {"target": 1, "card": e.card})
        assert status == 200 and out["swiped"] == s["card"] and e.feed.swipes == 2


def test_sse_pings_and_ends_on_close(deck):
    e = build_engine(deck, calibrate_s=None)
    with serving(e, ping_s=0.1) as server:
        sse = Sse(server)
        assert sse.next()[0] == "state"
        assert sse.next() == ("ping", None)
        e.close()
        with pytest.raises(EOFError):
            for _ in range(100):
                sse.next()
        sse.close()


def test_large_body_is_413(deck):
    e = engine_virtual(deck)
    with serving(e) as server:
        big = json.dumps({"gaze": "like", "pad": "x" * 5000}).encode()
        status, _, body = request(server, "POST", "/api/gaze", big,
                                  {"Content-Type": "application/json"})
        assert status == 413 and "error" in json.loads(body)
        assert e.gaze == "card"
        status, _, _ = request(server, "POST", "/api/swipe", b"{}",
                               {"Content-Length": "nope"})
        assert status == 400


# -- command line -------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["--port", "70000"], ["--port", "-1"], ["--seed", "-1"], ["--match-rate", "1.5"],
    ["--match-rate", "nan"], ["--cooldown", "-0.1"], ["--switch", "0"], ["--calibrate", "0"],
    ["--port", "x"], ["--bogus"],
])
def test_bad_arguments_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        swipe_web.main(argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_bad_deck_and_short_calibration_exit_2(tmp_path, capsys):
    assert swipe_web.main(["--port", "0", "--deck", str(tmp_path / "none.toml")]) == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert swipe_web.main(["--port", "0", "--calibrate", "2"]) == 2
    assert "calibration needs" in capsys.readouterr().err


def test_busy_port_exits_1(capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert swipe_web.main(["--port", str(port), "--no-decoder"]) == 1
    assert capsys.readouterr().err.strip() == f"error: port {port} is in use; try --port 0"


def test_port_held_on_the_wildcard_address_is_busy(capsys):
    """macOS lets SO_REUSEADDR bind 127.0.0.1:P beside a 0.0.0.0:P listener; the server
    must still report the port as busy instead of starting a second instance."""
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen()
        port = s.getsockname()[1]
        assert swipe_web.port_taken("127.0.0.1", port)
        assert swipe_web.port_taken("0.0.0.0", port)
        assert swipe_web.main(["--port", str(port), "--no-decoder"]) == 1
    assert capsys.readouterr().err.strip() == f"error: port {port} is in use; try --port 0"
    assert not swipe_web.port_taken("127.0.0.1", 0)


def _launch(*argv: str, tmpdir: Path | None = None,
            ignore_sigint: bool = False) -> tuple[subprocess.Popen, str, int]:
    env = {k: v for k, v in os.environ.items() if k != "BRAINSYNC_DATA_DIR"}
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)   # where tempfile puts the rendered cards
    # SIGINT is reset either way, so the tests do not depend on how pytest was launched
    sigint = signal.SIG_IGN if ignore_sigint else signal.SIG_DFL
    proc = subprocess.Popen([sys.executable, "-m", "brainsync.swipe_web", "--port", "0", *argv],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, preexec_fn=lambda: signal.signal(signal.SIGINT, sigint))
    line = proc.stdout.readline()
    m = re.fullmatch(r"BrainSwipe mock at http://127\.0\.0\.1:(\d+) \((.+)\); Ctrl-C stops\n",
                     line)
    if m is None:
        proc.kill()
        raise AssertionError(f"unexpected first line {line!r}; stderr: {proc.stderr.read()}")
    return proc, line, int(m.group(1))


def _stop(proc: subprocess.Popen, sig: int = signal.SIGINT) -> int:
    proc.send_signal(sig)
    try:
        return proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


class _Server:
    def __init__(self, port: int) -> None:
        self.server_address = ("127.0.0.1", port)


def test_cli_prints_the_real_url_and_stops_cleanly_on_ctrl_c():
    proc, line, port = _launch("--no-decoder")
    try:
        assert port not in (0, 8780) and "no decoder" in line
        status, headers, body = request(_Server(port), "GET", "/api/state")
        s = json.loads(body)
        assert status == 200 and s["decoder"] is False and s["n_cards"] == 20
        status, headers, body = request(_Server(port), "GET", f"/cards/{s['n_cards'] - 1}.png")
        assert status == 200 and png_size(body) == (600, 900)
    finally:
        code = _stop(proc)
    assert code == 0


def test_cli_stops_on_sigint_even_when_launched_with_sigint_ignored():
    """A shell starts background jobs (`cmd &`, CI) with SIGINT ignored, and Python keeps
    that: `kill -INT` did nothing until main() installed its own SIGINT handler."""
    proc, line, port = _launch("--no-decoder", ignore_sigint=True)
    try:
        assert request(_Server(port), "GET", "/api/state")[0] == 200
    finally:
        code = _stop(proc)
    assert code == 0


def test_cli_stops_cleanly_on_sigterm_and_removes_the_cards(tmp_path):
    """`kill` must not leak the temporary cards folder (it did before SIGTERM was handled)."""
    proc, line, port = _launch("--no-decoder", tmpdir=tmp_path)
    try:
        assert [p.name[:15] for p in tmp_path.iterdir()] == ["brainswipe_web_"]
    finally:
        code = _stop(proc, signal.SIGTERM)
    assert code == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.slow
def test_cli_decodes_a_real_time_board():
    """End to end in real time: calibration in virtual time, then window events stream
    within a few seconds (3.5 s fills the first window and its filter pad)."""
    t = time.monotonic()
    proc, line, port = _launch("--calibrate", "8", "--seed", "2")
    try:
        assert "simulated EEG board, no flicker" in line
        assert time.monotonic() - t < 10
        server = _Server(port)
        assert post_json(server, "/api/gaze", {"gaze": "like"}) == (200, {"gaze": "like"})
        sse = Sse(server, timeout=10)
        try:
            assert sse.next()[0] == "state"
            w = sse.until("window")
            assert w["gaze"] == "like" and w["true"] == LIKE
        finally:
            sse.close()
    finally:
        code = _stop(proc)
    assert code == 0
