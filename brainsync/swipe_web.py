"""BrainSwipe browser mock: the real swipe logic on a simulated EEG board, with no flicker.

    python -m brainsync.swipe_web                 # http://127.0.0.1:8780, decoder on a FakeBoard
    python -m brainsync.swipe_web --open          # same, and open the page in the browser
    python -m brainsync.swipe_web --no-decoder    # keyboard/click swipes only
    python -m brainsync.swipe_web --port 0        # any free port (the real URL is printed)

A localhost server (standard library only: http.server plus Server-Sent Events) serves a
phone-style BrainSwipe page from brainsync/web/ and runs the same pieces as the live demo:
the FBCCA ``Decoder``, BrainSwipe's ``Swiper`` (fresh-window votes, gaze-settle cooldown,
stale windows ignored) and ``Feed`` (endless shuffled deck, made-up matches). Only the
EEG is simulated: a real-time ``FakeBoard`` whose tone follows the presenter's simulated
gaze. Hovering a square (or holding an arrow key) on the page posts that gaze, the server
calls ``FakeBoard.set_target``, and the Swiper decides when a swipe fires, exactly as it
would on the headset. The page never flickers: browser frame timing is not validated for
SSVEP, and a static page is photosensitivity-safe.

Startup: load the deck, deal the feed (--seed, default 0 so demos repeat), render the
cards to a temporary folder with the feed's first card as image 0 (as the swipe CLI does),
fit the rest baseline on --calibrate seconds of a separate FakeBoard in virtual time (about
a second), then decode a real-time FakeBoard in a background thread. Ctrl-C or SIGTERM
stops (exit 0); a busy port exits 1; bad arguments exit 2.

HTTP API (JSON responses carry Cache-Control: no-store):

    GET  /                  the page (brainsync/web/index.html)
    GET  /static/<name>     a file in brainsync/web/ (exact names only)
    GET  /cards/<k>.png     card image k
    GET  /api/state         settings, deck (per image index), counts, gaze, card on screen
    GET  /api/events        text/event-stream: state, window, swipe, gaze; ": ping" every 15 s
    POST /api/gaze          {"gaze": "auto"|"card"|"pass"|"like"}   (409 with --no-decoder)
    POST /api/swipe         {"target": 0|1, "card": k}: instant swipe from the keyboard or a
                            click; "card" (optional) is the image the page shows, and a
                            swipe that would land on another card is a 409

Gaze "auto" lets the board follow its own --switch schedule, "card" means reading the card
(no tone), "pass" and "like" hold the 15 Hz or the 17 Hz tone.
"""
from __future__ import annotations

import argparse
import collections
import errno
import json
import math
import re
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .board import FS, FakeBoard
from .classify import ABSTAIN, make
from .demo import CALIBRATE_S
from .online import TUNED, Decision, Decoder, calibrate
from .profiles import Profile, load_deck, render_deck
from .recordings import MONTAGE, POSTERIOR
from .stimulus import load_config
from .swipe import LIKE, PASS, Feed, Swiper, card_order, voice_line

__all__ = ["GAZES", "CardChanged", "Engine", "NoDecoder", "Subscriber", "SwipeServer",
           "build_engine", "make_server", "main"]

NAME = "BrainSwipe"
DEFAULT_PORT = 8780        # other branches serve on 8765 and 8000
WEB_DIR = Path(__file__).with_name("web")
CONFIG = "swipe"           # targets, labels and flash colours come from the stimulus config
GAZES = ("auto", "card", "pass", "like")
GAZE_TARGET: dict[str, int | None] = {"auto": None, "card": ABSTAIN, "pass": PASS, "like": LIKE}
POLL_S = 0.1               # board reads per second in the decoder thread, as the demo
PING_S = 15.0              # SSE keep-alive comment; proxies and browsers drop idle streams
QUEUE_SIZE = 256           # events held per SSE client (about a minute of windows)
MAX_BODY = 4096            # bytes; the API bodies are a few dozen
# after a stall (laptop asleep, debugger) decode only the newest seconds: catching up on
# minutes of backlog would lag the page, and the Swiper allows one swipe per read anyway
MAX_BACKLOG_S = 2.0
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
                ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8"}
CARD_PATH = re.compile(r"/cards/(\d{1,6})\.png")


class NoDecoder(RuntimeError):
    """Gaze was set on an engine built without a decoder (--no-decoder)."""


class CardChanged(RuntimeError):
    """A key swipe named a card that is no longer on screen; ``card`` is the one that is."""

    def __init__(self, card: int) -> None:
        super().__init__(f"card changed: image {card} is on screen now")
        self.card = card


def _hex(rgb: Sequence[float]) -> str:
    """A PsychoPy rgb colour (-1..1 per channel) as #rrggbb."""
    return "#" + "".join(f"{round((max(-1.0, min(1.0, v)) + 1) * 127.5):02x}" for v in rgb)


def _num(x: float) -> float:
    """A score for JSON: rounded, and 0.0 for NaN or infinity (JSON has neither)."""
    x = float(x)
    return round(x, 4) if math.isfinite(x) else 0.0


# -- event fan-out ------------------------------------------------------------------------

class Subscriber:
    """One SSE client's bounded queue of (event, data) pairs.

    ``put`` never blocks: when the queue is full the oldest window event is dropped (the
    newest scores matter, a stale streak does not), or the oldest event when there is no
    window event to drop. ``get`` waits up to ``timeout`` and returns None on a timeout;
    after ``close`` it returns None at once and ``closed`` is True.
    """

    def __init__(self, maxsize: int = QUEUE_SIZE) -> None:
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        self.maxsize = maxsize
        self.dropped = 0
        self.closed = False
        self._items: collections.deque[tuple[str, dict[str, Any]]] = collections.deque()
        self._cond = threading.Condition()

    def __len__(self) -> int:
        return len(self._items)

    def put(self, event: str, data: dict[str, Any]) -> None:
        with self._cond:
            if self.closed:
                return
            if len(self._items) >= self.maxsize:
                for i, (name, _) in enumerate(self._items):
                    if name == "window":
                        del self._items[i]
                        break
                else:
                    self._items.popleft()
                self.dropped += 1
            self._items.append((event, data))
            self._cond.notify()

    def get(self, timeout: float | None = None) -> tuple[str, dict[str, Any]] | None:
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


# -- engine -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Look:
    """What the page needs to draw the two targets."""

    labels: tuple[str, str]
    freqs: tuple[float, float]
    colors: tuple[str, str]


class Engine:
    """Feed, Swiper, decoder and board behind one lock, with SSE subscribers.

    ``order`` maps image index to deck index (and back: see swipe.card_order). Without a
    decoder (and board) only key swipes work. ``step()`` is one board read, decode and
    broadcast; the background thread from ``start()`` calls it every POLL_S. On a
    clock-less FakeBoard, tests advance the board and call ``step()`` themselves.
    """

    def __init__(self, deck: Sequence[Profile], feed: Feed, look: Look, *,
                 cooldown_s: float = 0.5, switch_s: float = 8.0,
                 decoder: Decoder | None = None, board: FakeBoard | None = None,
                 queue_size: int = QUEUE_SIZE,
                 clock: Callable[[], float] = time.time) -> None:
        if (decoder is None) != (board is None):
            raise ValueError("decoder and board go together: give both or neither")
        if len(deck) != feed.n_cards:
            raise ValueError(f"the feed deals {feed.n_cards} cards but the deck has {len(deck)}")
        self.deck = list(deck)
        self.feed = feed
        self.look = look
        self.order = card_order(len(deck), feed.current)
        self.cooldown_s = float(cooldown_s)
        self.switch_s = float(switch_s)
        self.decoder = decoder
        self.board = board
        self.swiper = Swiper(decoder, cooldown_s=cooldown_s) if decoder is not None else None
        self.gaze = "card"
        self.started = float(clock())
        self.queue_size = queue_size
        self._began = decoder.n_seen if decoder is not None else 0
        self._lock = threading.RLock()
        self._subs: list[Subscriber] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if board is not None:
            board.set_target(GAZE_TARGET[self.gaze])

    @property
    def has_decoder(self) -> bool:
        return self.decoder is not None

    @property
    def card(self) -> int:
        """Image index of the card on screen."""
        return self.order[self.feed.current]

    # -- snapshots -----------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        f = self.feed
        return {"swipes": f.swipes, "likes": f.likes, "passes": f.passes, "matches": f.matches,
                "stale": self.swiper.stale if self.swiper is not None else 0}

    def state(self) -> dict[str, Any]:
        with self._lock:
            d = self.decoder
            return {
                "name": NAME, "decoder": d is not None,
                "labels": list(self.look.labels), "freqs": list(self.look.freqs),
                "colors": list(self.look.colors),
                "votes": self.swiper.votes if self.swiper is not None else TUNED.votes,
                "window_s": d.window_s if d is not None else TUNED.window_s,
                "step_s": d.step_s if d is not None else TUNED.step_s,
                "cooldown_s": self.cooldown_s, "switch_s": self.switch_s,
                "gaze": self.gaze, "card": self.card, "n_cards": len(self.deck),
                "deck": [{"name": p.name, "age": p.age, "detail": p.detail, "color": p.color}
                         for p in (self.deck[i] for i in self.order)],
                "counts": self.counts(), "started": self.started,
            }

    # -- subscribers ---------------------------------------------------------------------

    def subscribe(self) -> Subscriber:
        """A new client queue that starts with the current state, so it misses nothing."""
        sub = Subscriber(self.queue_size)
        with self._lock:
            sub.put("state", self.state())
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        sub.close()

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        # under the lock, so every client sees events in the order they happened;
        # put() never blocks, so a slow client cannot stall the decoder
        for sub in self._subs:
            sub.put(event, data)

    # -- actions -------------------------------------------------------------------------

    def set_gaze(self, gaze: str) -> str:
        """Point the simulated eyes; raises NoDecoder or ValueError (unknown gaze)."""
        if self.board is None:
            raise NoDecoder("no decoder")
        if gaze not in GAZE_TARGET:
            raise ValueError(f"gaze must be one of {list(GAZES)}, got {gaze!r}")
        with self._lock:
            self.board.set_target(GAZE_TARGET[gaze])
            if gaze != self.gaze:
                self.gaze = gaze
                self._broadcast("gaze", {"gaze": gaze})
        return gaze

    def key_swipe(self, target: int, card: int | None = None) -> dict[str, Any]:
        """Swipe the card on screen now (keyboard or click); the swipe event's payload.

        ``card`` is the image the page was showing. When the decoder swiped it a moment
        earlier (the key press raced the swipe event), CardChanged is raised instead of
        swiping the next card, which nobody has seen yet.
        """
        if not isinstance(target, int) or isinstance(target, bool) or target not in (PASS, LIKE):
            raise ValueError(f"target must be {PASS} or {LIKE}, got {target!r}")
        if card is not None and (not isinstance(card, int) or isinstance(card, bool)):
            raise ValueError(f"card must be an image index, got {card!r}")
        with self._lock:
            if card is not None and card != self.card:
                raise CardChanged(self.card)
            if self.swiper is not None:
                # the decoder must not fire again on EEG from before this card
                self.swiper.card_changed()
            return self._swipe(target, "key")

    def _swipe(self, target: int, source: str) -> dict[str, Any]:
        swiped = self.card
        outcome = self.feed.swipe(liked=target == LIKE)
        payload = {"target": target, "label": self.look.labels[target], "source": source,
                   "swiped": swiped, "card": self.card, "liked": outcome.liked,
                   "match": outcome.match,
                   "voice": voice_line(self.deck[outcome.card], outcome),
                   "counts": self.counts()}
        self._broadcast("swipe", payload)
        return payload

    def step(self) -> list[dict[str, Any]]:
        """One board read: decode, vote, swipe, broadcast; the window events it sent."""
        if self.decoder is None or self.board is None or self.swiper is None:
            return []
        out = []
        with self._lock:
            X, t = self.board.read()
            keep = int(MAX_BACKLOG_S * self.decoder.fs)
            if X.shape[1] > keep:
                X, t = X[:, -keep:], t[-keep:]
            for d in self.decoder.feed(X, t):
                out.append(self._decision(d))
        return out

    def _decision(self, d: Decision) -> dict[str, Any]:
        sw = self.swiper
        assert sw is not None and self.decoder is not None and self.board is not None
        stale = sw.is_stale(d)
        fired = sw.push(d)
        event = {"t": round((d.end - self._began) / self.decoder.fs, 3),
                 "scores": [_num(s) for s in d.scores], "raw": int(d.raw),
                 "pending": sw.pending, "streak": sw.streak, "votes": sw.votes,
                 "stale": stale, "gaze": self.gaze, "true": self.board.active_target()}
        self._broadcast("window", event)
        if fired != ABSTAIN:
            # the rest of this read is EEG from the old card and must count as stale,
            # as in swipe._play: the card changes at the end of the read
            sw.card_changed()
            self._swipe(fired, "decoder")
        return event

    # -- background thread ---------------------------------------------------------------

    def start(self, poll_s: float = POLL_S) -> threading.Thread | None:
        """Run step() every ``poll_s`` in a daemon thread (none without a decoder)."""
        if self.decoder is None or self._thread is not None:
            return self._thread
        with self._lock:
            self.board.read()   # type: ignore[union-attr]  # samples due before now are not news

        def loop() -> None:
            while not self._stop.wait(poll_s):
                try:
                    self.step()
                except Exception:   # keep serving the page; key swipes still work
                    print("error: the decoder thread stopped:", file=sys.stderr)
                    traceback.print_exc()
                    return

        self._thread = threading.Thread(target=loop, name="brainswipe-decoder", daemon=True)
        self._thread.start()
        return self._thread

    def close(self) -> None:
        """Stop the decoder thread and end every SSE stream."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            subs, self._subs = self._subs, []
        for sub in subs:
            sub.close()


def build_decoder(freqs: Sequence[float], phases: Sequence[float], fs: float = FS) -> Decoder:
    """The live demo's decoder with its defaults (see demo.build)."""
    clf = make(TUNED.classifier, freqs=freqs, fs=fs, harmonics=3, phases=phases,
               margin=TUNED.margin, threshold=TUNED.threshold)
    return Decoder(clf, fs, window_s=TUNED.window_s, step_s=TUNED.step_s, votes=TUNED.votes,
                   channels=POSTERIOR, pad_s=2.0, ch_names=MONTAGE)


def build_engine(deck: Sequence[Profile], *, seed: int = 0, match_rate: float = 0.3,
                 cooldown_s: float = 0.5, switch_s: float = 8.0,
                 calibrate_s: float | None = CALIBRATE_S, realtime: bool = True,
                 queue_size: int = QUEUE_SIZE) -> Engine:
    """Deal the feed and, unless ``calibrate_s`` is None, calibrate and attach a decoder.

    The rest baseline is fit in virtual time on its own FakeBoard (no tone, like reading
    the card), so startup takes about a second instead of ``calibrate_s``. The board the
    decoder then reads runs in real time (``realtime``) or virtual time (tests: advance
    it and call step()). It is seeded ``seed + 1``: with the calibration board's seed its
    first ``calibrate_s`` seconds of noise would equal the baseline's exactly and make
    reading the card look cleaner than it is. Raises ValueError for a too-short baseline.
    """
    cfg = load_config(CONFIG)
    if cfg.n_targets != 2:
        raise ValueError(f"BrainSwipe needs two targets; config {cfg.name!r} has {cfg.n_targets}")
    look = Look(labels=(cfg.labels[0], cfg.labels[1]),
                freqs=(float(cfg.freqs[0]), float(cfg.freqs[1])),
                colors=(_hex(cfg.flash_color(0)), _hex(cfg.flash_color(1))))
    feed = Feed(len(deck), match_rate=match_rate, seed=seed)
    if calibrate_s is None:
        return Engine(deck, feed, look, cooldown_s=cooldown_s, switch_s=switch_s,
                      queue_size=queue_size)
    decoder = build_decoder(cfg.freqs, cfg.phases)
    still = FakeBoard(cfg.freqs, switch_s=switch_s, seed=seed, ch_names=MONTAGE)
    still.set_target(ABSTAIN)
    calibrate(decoder, still, calibrate_s)
    board = FakeBoard(cfg.freqs, switch_s=switch_s, seed=seed + 1, ch_names=MONTAGE,
                      clock=time.time if realtime else None)
    return Engine(deck, feed, look, cooldown_s=cooldown_s, switch_s=switch_s,
                  decoder=decoder, board=board, queue_size=queue_size)


# -- HTTP ---------------------------------------------------------------------------------

class SwipeServer(ThreadingHTTPServer):
    """The HTTP server; ``engine`` and ``cards`` (PNG paths by image index) are attached
    once startup is done, after the port is bound."""

    daemon_threads = True          # SSE streams must not hold up Ctrl-C
    allow_reuse_address = True     # restart at once after Ctrl-C (TIME_WAIT)
    allow_reuse_port = False       # but never share the port with another server

    def __init__(self, address: tuple[str, int], engine: Engine | None = None,
                 cards: Sequence[Path] = (), web_dir: Path = WEB_DIR,
                 ping_s: float = PING_S) -> None:
        super().__init__(address, Handler)
        self.engine = engine
        self.cards = list(cards)
        self.web_dir = Path(web_dir)
        self.ping_s = float(ping_s)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


def make_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT, **kw: Any) -> SwipeServer:
    """Bind (raises OSError, errno EADDRINUSE for a busy port); serve_forever() serves."""
    return SwipeServer((host, port), **kw)


def port_taken(host: str, port: int, timeout: float = 0.2) -> bool:
    """Whether something already accepts connections on ``port``.

    The bind alone misses one case: on macOS and the BSDs, SO_REUSEADDR (kept so a
    restart is not blocked by TIME_WAIT) lets 127.0.0.1:P bind while another server
    listens on 0.0.0.0:P, and the presenter would then drive whichever one the browser
    happens to reach. A wildcard host is probed on the loopback address.
    """
    if port == 0:
        return False
    probe = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    try:
        with socket.create_connection((probe, port), timeout=timeout):
            return True
    except OSError:   # refused, unreachable or timed out: nobody is listening there
        return False


class Handler(BaseHTTPRequestHandler):
    server: SwipeServer
    server_version = "BrainSwipe"

    def log_message(self, format: str, *args: Any) -> None:
        pass   # a hover posts a gaze; logging each request would bury the startup lines

    # -- responses -----------------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict[str, Any]) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _not_found(self) -> None:
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    # -- GET -----------------------------------------------------------------------------

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        engine = self.server.engine
        if path == "/":
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(urllib.parse.unquote(path[len("/static/"):]))
        elif (m := CARD_PATH.fullmatch(path)) is not None:
            self._card(int(m.group(1)))
        elif path == "/api/state" and engine is not None:
            self._json(200, engine.state())
        elif path == "/api/events" and engine is not None:
            self._events(engine)
        elif path.startswith("/api/"):
            self._error(404, "not found")
        else:
            self._not_found()

    def _static(self, name: str) -> None:
        # whitelist of exact file names in the folder: no path ever reaches open() unchecked
        web = self.server.web_dir
        try:
            allowed = {p.name for p in web.iterdir()
                       if p.is_file() and not p.name.startswith(".")
                       and p.suffix.lower() in STATIC_TYPES}
        except OSError:
            allowed = set()
        if name not in allowed:
            self._not_found()
            return
        try:
            body = (web / name).read_bytes()
        except OSError:
            self._not_found()
            return
        self._send(200, body, STATIC_TYPES[Path(name).suffix.lower()], cache="no-cache")

    def _card(self, k: int) -> None:
        cards = self.server.cards
        if not 0 <= k < len(cards):
            self._not_found()
            return
        try:
            body = cards[k].read_bytes()
        except OSError:
            self._not_found()
            return
        self._send(200, body, "image/png", cache="no-cache")

    def _events(self, engine: Engine) -> None:
        sub = engine.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.flush()
            while True:
                item = sub.get(timeout=self.server.ping_s)
                if sub.closed:
                    break
                if item is None:
                    self.wfile.write(b": ping\n\n")
                else:
                    event, data = item
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n"
                                     .encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass   # the page closed or reloaded
        finally:
            engine.unsubscribe(sub)
            self.close_connection = True

    # -- POST ----------------------------------------------------------------------------

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        engine = self.server.engine
        if engine is None or path not in ("/api/gaze", "/api/swipe"):
            self._error(404, "not found")
            return
        body = self._body()
        if body is None:
            return
        if path == "/api/gaze":
            self._gaze(engine, body)
        else:
            self._swipe(engine, body)

    def _body(self) -> dict[str, Any] | None:
        """The JSON object sent, or None after an error response (400 or 413)."""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            self._error(400, "bad Content-Length")
            return None
        if n > MAX_BODY:
            self.close_connection = True   # the unread body must not be parsed as a request
            self._error(413, f"body over {MAX_BODY} bytes")
            return None
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "body must be JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "body must be a JSON object")
            return None
        return data

    def _gaze(self, engine: Engine, body: dict[str, Any]) -> None:
        if not engine.has_decoder:
            self._error(409, "no decoder")
            return
        gaze = body.get("gaze")
        if not isinstance(gaze, str) or gaze not in GAZE_TARGET:
            self._error(400, f"gaze must be one of {list(GAZES)}")
            return
        self._json(200, {"gaze": engine.set_gaze(gaze)})

    def _swipe(self, engine: Engine, body: dict[str, Any]) -> None:
        target = body.get("target")
        if not isinstance(target, int) or isinstance(target, bool) or target not in (PASS, LIKE):
            self._error(400, f"target must be {PASS} (Pass) or {LIKE} (Like)")
            return
        card = body.get("card")
        if card is not None and (not isinstance(card, int) or isinstance(card, bool)
                                 or card < 0):
            self._error(400, "card must be the image index on screen")
            return
        try:
            self._json(200, engine.key_swipe(target, card))
        except CardChanged as exc:
            self._json(409, {"error": "card changed", "card": exc.card})


# -- command line -------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.swipe_web", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1: this machine only)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"port (default {DEFAULT_PORT}; 0 picks a free one)")
    p.add_argument("--switch", type=float, default=8.0,
                   help="seconds between target changes while the gaze is on Auto (default 8)")
    p.add_argument("--cooldown", type=float, default=0.5,
                   help="seconds after a card change before a window may vote (default 0.5)")
    p.add_argument("--deck", metavar="PATH", default=None,
                   help="profiles TOML (default: the bundled deck of invented profiles)")
    p.add_argument("--match-rate", type=float, default=0.3,
                   help="chance that a Like is a match (default 0.3)")
    p.add_argument("--seed", type=int, default=0,
                   help="card order, matches and board noise (default 0, so demos repeat)")
    p.add_argument("--calibrate", type=float, default=CALIBRATE_S,
                   help=f"rest-baseline seconds, run in virtual time (default {CALIBRATE_S:g})")
    p.add_argument("--no-decoder", action="store_true",
                   help="no simulated board or decoder: keyboard and click swipes only")
    p.add_argument("--open", action="store_true", help="open the page in the web browser")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject bad values through ``p.error`` (exit code 2)."""
    if not 0 <= args.port <= 65535:
        p.error(f"--port must lie in 0..65535, got {args.port}")
    if not (math.isfinite(args.switch) and args.switch > 0):
        p.error(f"--switch must be positive, got {args.switch}")
    if not (math.isfinite(args.cooldown) and args.cooldown >= 0):
        p.error(f"--cooldown must be >= 0, got {args.cooldown}")
    if not 0.0 <= args.match_rate <= 1.0:   # also rejects NaN
        p.error(f"--match-rate must lie in [0, 1], got {args.match_rate}")
    if args.seed < 0:   # numpy refuses negative seeds
        p.error(f"--seed must be >= 0, got {args.seed}")
    if not (math.isfinite(args.calibrate) and args.calibrate > 0):
        p.error(f"--calibrate must be positive, got {args.calibrate}")


def _serve(args: argparse.Namespace, server: SwipeServer, deck: list[Profile], tmp: str) -> int:
    try:
        engine = build_engine(deck, seed=args.seed, match_rate=args.match_rate,
                              cooldown_s=args.cooldown, switch_s=args.switch,
                              calibrate_s=None if args.no_decoder else args.calibrate)
    except ValueError as exc:   # e.g. a baseline too short for two windows
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        cards = render_deck([deck[i] for i in engine.order], tmp)
    except ImportError as exc:
        print(f"error: drawing the cards needs Pillow ({exc}); pip install pillow",
              file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot write the cards: {exc}", file=sys.stderr)
        return 2
    server.engine, server.cards = engine, cards
    try:
        engine.start()
        mode = ("simulated EEG board, no flicker" if engine.has_decoder
                else "no decoder: keyboard and click swipes only")
        print(f"{NAME} mock at {server.url} ({mode}); Ctrl-C stops", flush=True)
        if args.open:
            webbrowser.open(server.url)
        server.serve_forever(poll_interval=0.2)
    finally:
        engine.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    try:
        deck = load_deck(args.deck)
    except (ValueError, OSError) as exc:   # OSError: missing, a folder, unreadable
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # bind first: a busy port should fail before a second of rendering and calibration
    busy = f"error: port {args.port} is in use; try --port 0"
    if port_taken(args.host, args.port):
        print(busy, file=sys.stderr)
        return 1
    try:
        server = make_server(args.host, args.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(busy, file=sys.stderr)
        else:
            print(f"error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    code = 0
    # `kill` (SIGTERM) stops like Ctrl-C, so the cards folder is removed either way. SIGINT
    # is set too: a shell starts a background job (`&`, CI) with SIGINT ignored, and
    # Python keeps an inherited SIG_IGN, so Ctrl-C or `kill -INT` would do nothing.
    previous = {sig: _on_signal(sig, h) for sig, h in
                ((signal.SIGTERM, _interrupt), (signal.SIGINT, signal.default_int_handler))}
    try:
        with tempfile.TemporaryDirectory(prefix="brainswipe_web_") as tmp:
            code = _serve(args, server, deck, tmp)
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    finally:
        server.server_close()
        for sig, handler in previous.items():
            _on_signal(sig, handler)
    return code


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _on_signal(sig: int, handler: Any) -> Any:
    """Install a handler for ``sig``; the previous one (None off the main thread, where
    Python allows no signal handlers, e.g. main() called from a test thread)."""
    if handler is None:
        return None
    try:
        return signal.signal(sig, handler)
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())
