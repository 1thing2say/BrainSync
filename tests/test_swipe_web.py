"""Tests for the BrainSwipe browser mock's server on the c-VEP standard, headless: the
engine on a virtual standard fake board (time moves only when a test advances it), the
HTTP server on a free port in a thread, and the CLI in a subprocess."""
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

import numpy as np
import pytest

from brainsync import swipe_web
from brainsync.classify import ABSTAIN
from brainsync.profiles import load_deck, render_deck
from brainsync.swipe import LIKE, PASS, SUPER_LIKE, card_order
from brainsync.swipe_web import Engine, Subscriber, build_engine
from brainsync.vep import live

ROOT = Path(__file__).resolve().parents[1]
STATE_KEYS = {"name", "decoder", "paradigm", "labels", "colors", "lags", "cycle_s", "votes",
              "margin", "cycles", "training", "cooldown_s", "switch_s", "gaze", "card",
              "n_cards", "deck", "counts", "started"}
COUNT_KEYS = {"swipes", "likes", "super_likes", "passes", "matches", "stale"}
DECISION_KEYS = {"t", "scores", "raw", "committed", "lead", "changed", "fired", "votes",
                 "stale", "gaze", "true"}
SWIPE_KEYS = {"target", "label", "source", "swiped", "card", "liked", "super_like", "match",
              "voice", "counts"}
POLL = 0.1   # virtual seconds per board read, as the decoder thread


@pytest.fixture(scope="module")
def deck():
    return load_deck()


@pytest.fixture(scope="module")
def cards(deck, tmp_path_factory) -> list[Path]:
    """The deck drawn in the image order of seed 0 (every engine here uses seed 0)."""
    order = build_engine(deck, decoder=False).order
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
    """An engine on a virtual board; it trains on its first cycles (30 by default)."""
    return build_engine(deck, realtime=False, **kw)


def trained(deck, **kw) -> Engine:
    """An engine on a virtual board whose decoder was trained at build time."""
    return build_engine(deck, realtime=False, pretrain=True, **kw)


def advance(engine: Engine, seconds: float) -> list[dict]:
    """Run the decoder for ``seconds`` of virtual time; the decision events sent."""
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
    e = build_engine(deck, decoder=False, seed=3)
    s = e.state()
    assert set(s) == STATE_KEYS
    assert s["name"] == "BrainSwipe" and s["decoder"] is False and s["paradigm"] == "code"
    assert s["labels"] == ["Pass", "Super like", "Like"] and s["lags"] == [0, 21, 42]
    assert s["colors"] == ["#d0605a", "#5a9bd0", "#5aae6e"]   # swipe_cvep [card] flash_colors
    assert s["cycle_s"] == 1.05 and s["votes"] == live.VOTES and s["cycles"] == live.CYCLES
    assert s["card"] == 0 and s["gaze"] == "card" and s["n_cards"] == len(deck)
    assert set(s["counts"]) == COUNT_KEYS and not any(s["counts"].values())
    assert s["training"]["active"] is False
    # image k shows deck[order[k]], and the feed's first card is image 0
    order = card_order(len(deck), e.feed.current)
    assert e.order == order
    for k, entry in enumerate(s["deck"]):
        p = deck[order[k]]
        assert entry == {"name": p.name, "age": p.age, "detail": p.detail, "color": p.color}
    assert s["deck"][0]["name"] == deck[e.feed.current].name
    json.dumps(s)   # serialisable as is


def test_key_swipes_pass_super_like_and_like(deck):
    e = build_engine(deck, decoder=False, match_rate=0.0)
    sub = e.subscribe()
    assert drain(sub)[0][0] == "state"
    before = e.card
    name = e.state()["deck"][before]["name"]
    out = e.key_swipe(SUPER_LIKE)
    assert set(out) == SWIPE_KEYS
    assert out["source"] == "key" and out["label"] == "Super like"
    assert out["liked"] and out["super_like"] and out["match"]   # always, even at rate 0
    assert out["swiped"] == before and out["card"] == e.card != before
    assert out["voice"] == f"Super liked {name}. It's a match!"
    assert out["counts"] == {"swipes": 1, "likes": 1, "super_likes": 1, "passes": 0,
                             "matches": 1, "stale": 0}
    assert swipes_of(drain(sub)) == [out]
    like = e.key_swipe(LIKE)
    assert like["label"] == "Like" and like["liked"] and not like["super_like"]
    assert not like["match"] and like["voice"].startswith("Liked ")
    p = e.key_swipe(PASS)
    assert p["voice"] == "Pass" and not p["liked"] and not p["match"]
    for bad in (True, 3, -1, "1", 1.0, None):
        with pytest.raises(ValueError):
            e.key_swipe(bad)


def test_training_comes_first_with_the_eyes_on_pass(deck):
    e = engine_virtual(deck, train_cycles=10)
    sub = e.subscribe()
    s = drain(sub)[0][1]
    assert s["training"] == {"active": True, "cycle": 0, "of": 10, "accuracy": None,
                             "chance": 0.3333, "target": PASS}
    e.set_gaze("like")                      # ignored until the template is ready
    assert e.board.fixated == PASS
    decisions = advance(e, 8.0)
    assert decisions == [] and e.training and e.board.fixated == PASS
    decisions = advance(e, 8.0)
    assert not e.training and decisions
    items = drain(sub)
    cycles = [d["cycle"] for ev, d in items if ev == "train"]
    assert cycles == sorted(cycles) and cycles[-1] == 10 and len(cycles) >= 10
    [done] = [d for ev, d in items if ev == "trained"]
    assert done["cycles"] == 10 and done["chance"] == 0.3333 and done["accuracy"] >= 0.9
    assert e.board.fixated == LIKE          # the gaze takes over after training
    assert e.state()["training"]["accuracy"] == done["accuracy"]


def test_gaze_needs_a_decoder_and_a_known_value(deck):
    with pytest.raises(swipe_web.NoDecoder):
        build_engine(deck, decoder=False).set_gaze("like")
    e = trained(deck)
    assert e.board.fixated == ABSTAIN       # reading the card: no response
    sub = e.subscribe()
    drain(sub)
    for gaze, target in (("like", LIKE), ("super", SUPER_LIKE), ("pass", PASS),
                         ("card", ABSTAIN)):
        assert e.set_gaze(gaze) == gaze
        assert e.board.fixated == target
    e.set_gaze("auto")                      # Pass, Super like, Like for --switch s each
    seen = []
    for _ in range(int(3 * e.switch_s / POLL) + 1):
        advance(e, POLL)
        seen.append(e.board.fixated)
    assert [k for i, k in enumerate(seen) if i == 0 or seen[i - 1] != k][:3] in (
        [PASS, SUPER_LIKE, LIKE], [SUPER_LIKE, LIKE, PASS], [LIKE, PASS, SUPER_LIKE])
    gazes = [d["gaze"] for ev, d in drain(sub) if ev == "gaze"]
    assert gazes == ["like", "super", "pass", "card", "auto"]
    e.set_gaze("auto")
    assert [ev for ev, _ in drain(sub) if ev == "gaze"] == []   # no news, no event
    with pytest.raises(ValueError):
        e.set_gaze("left")


@pytest.mark.parametrize("gaze, target", [("like", LIKE), ("super", SUPER_LIKE),
                                          ("pass", PASS)])
def test_a_held_gaze_keeps_swiping_that_square(deck, gaze, target):
    e = trained(deck)
    sub = e.subscribe()
    e.set_gaze(gaze)
    t0 = e.board.elapsed
    times = []
    for _ in range(int(20 / POLL)):
        advance(e, POLL)
        times += [(e.board.elapsed, s["target"]) for s in swipes_of(drain(sub))]
    assert len(times) >= 4 and {k for _, k in times} == {target}
    assert times[0][0] - t0 < 4.5
    gaps = [b - a for (a, _), (b, _) in zip(times, times[1:])]
    # settle 0.5 s, two fresh cycles of 1.05 s and the 0.5 s pad: never faster
    assert min(gaps) >= 3.0, gaps


def test_decision_events_carry_the_decoder_state(deck):
    e = trained(deck)
    sub = e.subscribe()
    e.set_gaze("like")
    decisions = advance(e, 12.0)
    assert len(decisions) == len([d for ev, d in drain(sub) if ev == "decision"]) >= 6
    for d in decisions:
        assert set(d) == DECISION_KEYS
        assert len(d["scores"]) == 3 and all(isinstance(x, float) for x in d["scores"])
        assert d["raw"] in (-1, 0, 1, 2) and d["committed"] in (-1, 0, 1, 2)
        assert d["fired"] in (-1, d["committed"])
        assert d["votes"] == live.VOTES and d["gaze"] == "like" and d["true"] == LIKE
        assert d["lead"] is None or 0 <= d["lead"] <= 2
    fired = [d for d in decisions if d["fired"] != ABSTAIN]
    assert fired and all(d["fired"] == LIKE and d["changed"] for d in fired)
    assert 0 < decisions[-1]["stale"] <= e.swiper.stale
    gaps = np.diff([d["t"] for d in decisions])
    assert min(gaps) >= 1.0                  # at most one decision per 1.05 s cycle


def test_reading_the_card_rarely_swipes(deck):
    """Gaze on the card: the fake board carries no response, so every swipe is a false
    one. At the standard's margin 0 this gave 13 a minute (two noise decisions agree a
    third of the time); the mock's margin 0.2 gave none over 6 seeds x 60 s."""
    seconds, n_false = 60.0, 0
    for seed in range(4):
        e = trained(deck, seed=seed)
        sub = e.subscribe()
        decisions = advance(e, seconds)
        assert decisions and all(d["true"] == ABSTAIN and d["gaze"] == "card" for d in decisions)
        n_false += len(swipes_of(drain(sub)))
    assert n_false * 60 / (4 * seconds) <= 1.0, f"{n_false} false swipes"


def test_a_key_swipe_resets_the_decoder_so_it_cannot_double_fire(deck):
    e = trained(deck)
    sub = e.subscribe()
    e.set_gaze("like")
    for _ in range(100):
        advance(e, POLL)
        if swipes_of(drain(sub)):
            break
    # wait for the first decision on the new card, then swipe it by key
    for _ in range(100):
        if advance(e, POLL):
            break
    e.key_swipe(LIKE)
    assert e.decoder.committed == ABSTAIN
    t_key = e.board.elapsed
    times = []
    for _ in range(60):
        advance(e, POLL)
        times += [e.board.elapsed for s in swipes_of(drain(sub)) if s["source"] == "decoder"]
    # only cycles that began after the key swipe + cooldown count: >= 0.5 + 2 x 1.05 + 0.5
    assert times and times[0] - t_key >= 3.0


def test_a_key_swipe_for_a_card_already_swiped_is_refused(deck):
    """The decoder swipes card A; a Shift+Arrow pressed while A was still on the page
    names A and must not swipe B, which nobody has seen yet."""
    e = trained(deck, match_rate=1.0)
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
    e = trained(deck, queue_size=8)
    slow = e.subscribe()          # never read
    fast = e.subscribe()
    e.set_gaze("like")
    longest = 0.0
    seen = []
    for _ in range(int(60 / POLL)):
        e.board.advance(POLL)
        t = time.perf_counter()
        e.step()
        longest = max(longest, time.perf_counter() - t)
        seen += drain(fast)
    n_decisions = sum(ev == "decision" for ev, _ in seen)
    n_swipes = len(swipes_of(seen))
    assert n_decisions > 20 and n_swipes >= 10
    assert len(slow) == 8 and slow.dropped > 0
    assert longest < 0.5
    # decisions go first: the slow client still has the newest swipes, in order
    kept = [data for _, data in iter(lambda: slow.get(timeout=0), None)]
    swipes = [d for d in kept if "label" in d]
    assert swipes and swipes == swipes_of(seen)[-len(swipes):]


def test_subscriber_drops_old_decisions_first_and_closes():
    sub = Subscriber(3)
    sub.put("swipe", {"n": 1})
    sub.put("decision", {"n": 2})
    sub.put("decision", {"n": 3})
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
    e = build_engine(deck, decoder=False)
    with pytest.raises(ValueError, match="both or neither"):
        Engine(deck, e.feed, e.look, decoder=engine_virtual(deck).decoder)
    with pytest.raises(ValueError, match="deck has"):
        Engine(deck[:3], e.feed, e.look)
    with pytest.raises(ValueError, match="train_cycles"):
        engine_virtual(deck, train_cycles=1)
    with pytest.raises(ValueError, match="margin"):
        engine_virtual(deck, margin=-0.1)


def test_pretrain_is_quick_and_decodes_at_once(deck):
    t = time.perf_counter()
    e = trained(deck)
    assert time.perf_counter() - t < 5.0
    s = e.state()["training"]
    assert s["active"] is False and s["cycle"] == s["of"] == live.TRAIN_CYCLES
    assert s["accuracy"] >= 0.9
    e.set_gaze("super")
    assert advance(e, 4.0)                   # decisions within a few cycles


# -- HTTP ---------------------------------------------------------------------------------

def test_api_state_over_http(deck, cards, web_dir):
    e = engine_virtual(deck)
    with serving(e, cards, web_dir) as server:
        status, s = get_json(server, "/api/state")
    assert status == 200
    assert set(s) == STATE_KEYS and s["decoder"] is True
    assert s["votes"] == 2 and s["cycles"] == 2 and s["margin"] == 0.2
    assert s["training"]["active"] is True and s["training"]["of"] == 30
    assert s["cooldown_s"] == 0.5 and s["switch_s"] == 8.0 and len(s["deck"]) == 20


def test_static_files_are_whitelisted(deck, cards, web_dir):
    with serving(build_engine(deck, decoder=False), cards, web_dir) as server:
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
                     "/../secret.js", "/index.html", "/api/nothing", "/static/cvep.js"):
            status, _, body = request(server, "GET", path)
            assert status == 404, path
            assert b"secret" not in body


def test_the_real_page_is_served(deck, cards):
    with serving(build_engine(deck, decoder=False), cards) as server:
        status, _, body = request(server, "GET", "/")
        assert status == 200 and b'data-side="super"' in body
        for name in ("app.js", "logic.js", "style.css"):
            assert request(server, "GET", f"/static/{name}")[0] == 200, name


def test_missing_frontend_is_a_404_not_a_crash(deck, tmp_path):
    with serving(build_engine(deck, decoder=False), web_dir=tmp_path / "absent") as server:
        assert request(server, "GET", "/")[0] == 404
        assert get_json(server, "/api/state")[0] == 200


def test_card_images(deck, cards, web_dir):
    with serving(build_engine(deck, decoder=False), cards, web_dir) as server:
        status, headers, body = request(server, "GET", "/cards/0.png")
        assert status == 200 and headers["content-type"] == "image/png"
        assert png_size(body) == (600, 900)
        assert body == cards[0].read_bytes()
        assert request(server, "GET", "/cards/19.png")[0] == 200
        for path in ("/cards/20.png", "/cards/-1.png", "/cards/x.png", "/cards/1.jpg",
                     "/cards/1.png/x", "/cards/9999999.png", "/cards/"):
            assert request(server, "GET", path)[0] == 404, path


def test_post_gaze(deck, cards):
    e = trained(deck)
    with serving(e, cards) as server:
        assert post_json(server, "/api/gaze", {"gaze": "super"}) == (200, {"gaze": "super"})
        assert e.board.fixated == SUPER_LIKE
        assert post_json(server, "/api/gaze", {"gaze": "auto"}) == (200, {"gaze": "auto"})
        assert get_json(server, "/api/state")[1]["gaze"] == "auto"
        for body in ({"gaze": "left"}, {"gaze": 1}, {}, [1], b"not json", b"\xff\xfe",
                     b'"like"'):
            status, data = post_json(server, "/api/gaze", body)
            assert status == 400 and "error" in data, body
        assert e.gaze == "auto"
        assert post_json(server, "/api/nothing", {})[0] == 404


def test_post_gaze_without_decoder_is_409(deck, cards):
    with serving(build_engine(deck, decoder=False), cards) as server:
        assert post_json(server, "/api/gaze", {"gaze": "like"}) == (409, {"error": "no decoder"})
        assert get_json(server, "/api/state")[1]["decoder"] is False
        for target in (0, 1, 2):
            assert post_json(server, "/api/swipe", {"target": target})[0] == 200


def test_post_swipe_advances_the_card_and_reaches_sse_clients(deck, cards):
    e = trained(deck)
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
            # decisions and gaze changes stream too
            post_json(server, "/api/gaze", {"gaze": "like"})
            assert sse.until("gaze") == {"gaze": "like"}
            advance(e, 4.0)
            d = sse.until("decision")
            assert set(d) == DECISION_KEYS and d["gaze"] == "like"
        finally:
            sse.close()
        for body in ({"target": 3}, {"target": True}, {"target": "1"}, {"target": 1.0}, {},
                     b"{", {"target": 0, "card": "1"}, {"target": 0, "card": -1},
                     {"target": 0, "card": True}):
            assert post_json(server, "/api/swipe", body)[0] == 400, body
        swipes = e.feed.swipes
        # the page names the card it shows: a stale one is a 409 that says which is on
        assert post_json(server, "/api/swipe", {"target": 1, "card": 0}) == (
            409, {"error": "card changed", "card": e.card})
        assert e.feed.swipes == swipes
        status, out = post_json(server, "/api/swipe", {"target": 1, "card": e.card})
        assert status == 200 and out["super_like"] and out["match"]
        assert e.feed.swipes == swipes + 1 and e.feed.super_likes == 1


def test_training_streams_to_sse_clients(deck, cards):
    e = engine_virtual(deck, train_cycles=4)
    with serving(e, cards) as server:
        sse = Sse(server)
        try:
            assert sse.next()[1]["training"]["active"] is True
            advance(e, 10.0)
            assert sse.until("train")["of"] == 4
            done = sse.until("trained")
            assert done["cycles"] == 4 and done["chance"] == 0.3333
        finally:
            sse.close()


def test_sse_pings_and_ends_on_close(deck):
    e = build_engine(deck, decoder=False)
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
    ["--match-rate", "nan"], ["--cooldown", "-0.1"], ["--switch", "0"],
    ["--train-cycles", "1"], ["--margin", "-0.1"], ["--margin", "nan"], ["--calibrate", "30"],
    ["--port", "x"], ["--bogus"],
])
def test_bad_arguments_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        swipe_web.main(argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_bad_deck_exits_2(tmp_path, capsys):
    assert swipe_web.main(["--port", "0", "--deck", str(tmp_path / "none.toml")]) == 2
    assert capsys.readouterr().err.startswith("error: ")


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
    """End to end in real time: --pretrain trains in virtual time, then decision events
    stream within a few seconds (two cycles of 1.05 s and the filter pad)."""
    t = time.monotonic()
    proc, line, port = _launch("--pretrain", "--seed", "2")
    try:
        assert "c-VEP on a simulated EEG board, no flicker" in line
        assert time.monotonic() - t < 10
        server = _Server(port)
        assert post_json(server, "/api/gaze", {"gaze": "like"}) == (200, {"gaze": "like"})
        sse = Sse(server, timeout=10)
        try:
            assert sse.next()[0] == "state"
            d = sse.until("decision")
            assert d["gaze"] == "like" and d["true"] == LIKE
        finally:
            sse.close()
    finally:
        code = _stop(proc)
    assert code == 0


class _FakeSpeaker:
    def __init__(self, folder: Path, ok: bool = True) -> None:
        self.folder, self.ok, self.asked = folder, ok, []

    def clip(self, text, timeout=None):
        self.asked.append(text)
        if not self.ok:
            return None
        path = self.folder / "clip.wav"
        path.write_bytes(b"RIFF" + text.encode())
        return path


def test_speech_is_cartesia_through_the_server(deck, cards, tmp_path):
    fake = _FakeSpeaker(tmp_path)
    with serving(build_engine(deck, decoder=False), cards) as server:
        server._speaker = fake
        status, headers, body = request(server, "GET", "/api/speech?text=Nadia%20liked%20you%20back")
        assert status == 200 and headers["content-type"] == "audio/wav"
        assert body == b"RIFFNadia liked you back" and fake.asked == ["Nadia liked you back"]
        assert request(server, "GET", "/api/speech")[0] == 400
        assert request(server, "GET", "/api/speech?text=" + "a" * 301)[0] == 400
        fake.ok = False
        status, _, body = request(server, "GET", "/api/speech?text=hello")
        assert status == 503 and b"no voice" in body


def test_speech_without_a_voice_is_503(deck, cards):
    # the suite runs with BRAINSYNC_TTS=off, so the process-wide speaker is None
    with serving(build_engine(deck, decoder=False), cards) as server:
        assert server.speaker is None
        assert request(server, "GET", "/api/speech?text=hello")[0] == 503
