"""BrainSwipe browser mock: the real c-VEP swipe logic on a simulated EEG board, no flicker.

    python -m brainsync.swipe_web                 # http://127.0.0.1:8780, decoder on a fake board
    python -m brainsync.swipe_web --open          # same, and open the page in the browser
    python -m brainsync.swipe_web --pretrain      # train the template at startup (instant)
    python -m brainsync.swipe_web --no-decoder    # keyboard/click swipes only
    python -m brainsync.swipe_web --port 0        # any free port (the real URL is printed)

A localhost server (standard library only: http.server plus Server-Sent Events) serves a
phone-style BrainSwipe page from brainsync/web/ and runs the same pieces as the live demo
on the c-VEP standard (docs/CVEP-STANDARD.md): three white squares, Pass, Super like and
Like on targets 0, 1 and 2 (config swipe_cvep), the self-training
``vep.live.CodeVepDecoder``, BrainSwipe's ``CodeSwiper`` (one swipe per commit, cycles
from before a card settled dropped) and ``Feed`` (endless shuffled deck, made-up matches;
a Super like always matches). Only the EEG is simulated: a real-time
``vep.live.standard_fake_board`` whose response follows the presenter's simulated gaze.
Training comes first, as on the headset: the simulated user looks at Pass for
--train-cycles code cycles (30, about 32 s) while the page shows the count, then the
held-out accuracy against chance (33 %). From then on every cycle gives a decision whose
scores (correlations) fill the confidence bar under each square. Hovering a square (or
holding an arrow key: left Pass, up Super like, right Like) posts that gaze, the server
calls ``set_target`` on the fake board, and the CodeSwiper decides when a swipe fires,
exactly as it would on the headset. The page never flickers: the squares are drawn
static white, browser frame timing is not validated here, and a static page is
photosensitivity-safe.

Startup: load the deck, deal the feed (--seed, default 0 so demos repeat), render the
cards to a temporary folder with the feed's first card as image 0 (as the swipe CLI does),
then decode a real-time fake board in a background thread (with --pretrain the template is
first trained on a separate fake board in virtual time, about a second, and decoding
starts at once). Ctrl-C or SIGTERM stops (exit 0); a busy port exits 1; bad arguments
exit 2.

HTTP API (JSON responses carry Cache-Control: no-store):

    GET  /                  the page (brainsync/web/index.html)
    GET  /static/<name>     a file in brainsync/web/ (exact names only)
    GET  /cards/<k>.png     card image k
    GET  /api/state         settings, training, deck (per image index), counts, gaze, card
    GET  /api/events        text/event-stream: state, train, trained, decision, swipe,
                            gaze; ": ping" every 15 s
    POST /api/gaze          {"gaze": "auto"|"card"|"pass"|"super"|"like"} (409 with
                            --no-decoder)
    POST /api/swipe         {"target": 0|1|2, "card": k}: instant swipe from the keyboard or
                            a click; "card" (optional) is the image the page shows, and a
                            swipe that would land on another card is a 409

Gaze "auto" moves the simulated eyes Pass -> Super like -> Like every --switch s, "card"
means reading the card (no response), "pass", "super" and "like" hold that square.
During training the simulated eyes stay on Pass whatever the gaze.
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

from .classify import ABSTAIN
from .demo import is_standard_code
from .online import Decision
from .profiles import Profile, load_deck, render_deck
from .stimulus import load_config
from .swipe import (LIKE, PASS, SUPER_LIKE, CodeSwiper, Feed, card_order, voice_line)
from .vep import live

__all__ = ["GAZES", "CardChanged", "Engine", "NoDecoder", "Subscriber", "SwipeServer",
           "build_engine", "make_server", "main"]

NAME = "BrainSwipe"
DEFAULT_PORT = 8780        # other branches serve on 8765 and 8000
WEB_DIR = Path(__file__).with_name("web")
CONFIG = "swipe_cvep"      # targets, labels and accent colours come from the stimulus config
TARGETS = (PASS, SUPER_LIKE, LIKE)
GAZES = ("auto", "card", "pass", "super", "like")
GAZE_TARGET: dict[str, int | None] = {"auto": None, "card": ABSTAIN, "pass": PASS,
                                      "super": SUPER_LIKE, "like": LIKE}
POLL_S = 0.1               # board reads per second in the decoder thread, as the demo
PING_S = 15.0              # SSE keep-alive comment; proxies and browsers drop idle streams
QUEUE_SIZE = 256           # events held per SSE client (minutes of decisions)
MAX_BODY = 4096            # bytes; the API bodies are a few dozen
# after a stall (laptop asleep, debugger) decode only the newest seconds: catching up on
# minutes of backlog would lag the page, and the CodeSwiper allows one swipe per read anyway
MAX_BACKLOG_S = 4.0
# abstain unless the best square's correlation leads the runner-up by this much. Reading
# the card (no response) on the fake board gave 13 false swipes a minute at the standard's
# margin 0, 0.3 at 0.15 and none at 0.2 (6 seeds x 60 s); a held gaze leads by about 0.9
MARGIN = 0.2
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


def _maybe(x: float) -> float | None:
    """A rounded number, or None (JSON null) for NaN or infinity."""
    x = float(x)
    return round(x, 4) if math.isfinite(x) else None


# -- event fan-out ------------------------------------------------------------------------

class Subscriber:
    """One SSE client's bounded queue of (event, data) pairs.

    ``put`` never blocks: when the queue is full the oldest decision event is dropped (the
    newest scores matter, old ones do not), or the oldest event when there is no
    decision event to drop. ``get`` waits up to ``timeout`` and returns None on a timeout;
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
                    if name == "decision":
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
    """What the page needs to draw the three targets: labels, accent colours, code lags."""

    labels: tuple[str, ...]
    colors: tuple[str, ...]
    lags: tuple[int, ...]       # frames each square's code is delayed (0, 21, 42)
    cycle_s: float              # one code cycle, seconds


class Engine:
    """Feed, CodeSwiper, decoder and board behind one lock, with SSE subscribers.

    ``order`` maps image index to deck index (and back: see swipe.card_order). Without a
    decoder (and board) only key swipes work. ``step()`` is one board read, decode and
    broadcast; the background thread from ``start()`` calls it every POLL_S. On a
    clock-less fake board, tests advance the board and call ``step()`` themselves. The
    board flickers from the start; while the decoder trains its simulated eyes stay on
    Pass, then they follow the gaze.
    """

    def __init__(self, deck: Sequence[Profile], feed: Feed, look: Look, *,
                 cooldown_s: float = 0.5, switch_s: float = 8.0,
                 decoder: live.CodeVepDecoder | None = None, board: Any = None,
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
        self.swiper = CodeSwiper(decoder, cooldown_s=cooldown_s) if decoder is not None else None
        self.gaze = "card"
        self.started = float(clock())
        self.queue_size = queue_size
        self._lock = threading.RLock()
        self._subs: list[Subscriber] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._shown = -1               # training cycles last broadcast
        self._decoding_from = 0.0      # board seconds when decoding began
        if board is not None and decoder is not None:
            board.start_flicker()
            if decoder.training:
                board.set_target(decoder.train_target)
            else:
                self._begin_decoding()

    @property
    def has_decoder(self) -> bool:
        return self.decoder is not None

    @property
    def training(self) -> bool:
        return self.decoder is not None and self.decoder.training

    @property
    def card(self) -> int:
        """Image index of the card on screen."""
        return self.order[self.feed.current]

    # -- snapshots -----------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        f = self.feed
        return {"swipes": f.swipes, "likes": f.likes, "super_likes": f.super_likes,
                "passes": f.passes, "matches": f.matches,
                "stale": self.swiper.stale if self.swiper is not None else 0}

    def training_state(self) -> dict[str, Any]:
        """Where training is: cycles seen / needed, and once done the held-out accuracy."""
        d = self.decoder
        if d is None:
            return {"active": False, "cycle": 0, "of": live.TRAIN_CYCLES, "accuracy": None,
                    "chance": round(1 / len(TARGETS), 4), "target": PASS}
        k, n = d.progress
        return {"active": d.training, "cycle": k, "of": n, "accuracy": _maybe(d.train_accuracy),
                "chance": round(d.chance, 4), "target": d.train_target}

    def state(self) -> dict[str, Any]:
        with self._lock:
            d = self.decoder
            return {
                "name": NAME, "decoder": d is not None, "paradigm": "code",
                "labels": list(self.look.labels), "colors": list(self.look.colors),
                "lags": list(self.look.lags), "cycle_s": self.look.cycle_s,
                "votes": d.votes if d is not None else live.VOTES,
                "margin": d.margin if d is not None else MARGIN,
                "cycles": d.cycles if d is not None else live.CYCLES,
                "training": self.training_state(),
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

    # -- the simulated eyes --------------------------------------------------------------

    def _target(self) -> int:
        """Where the simulated eyes are now: Pass while training, else the gaze (auto:
        Pass, Super like, Like for --switch s each, from when decoding began)."""
        assert self.board is not None
        if self.training:
            return self.decoder.train_target   # type: ignore[union-attr]
        want = GAZE_TARGET[self.gaze]
        if want is None:
            elapsed = max(0.0, self.board.elapsed - self._decoding_from)
            return TARGETS[int(elapsed // self.switch_s) % len(TARGETS)]
        return want

    def _look(self) -> None:
        target = self._target()
        if target != self.board.fixated:
            self.board.set_target(target)

    def _begin_decoding(self) -> None:
        assert self.board is not None and self.swiper is not None
        self._decoding_from = self.board.elapsed
        self.swiper.card_changed()      # the eyes leave the Pass square for the card
        self._look()

    # -- actions -------------------------------------------------------------------------

    def set_gaze(self, gaze: str) -> str:
        """Point the simulated eyes; raises NoDecoder or ValueError (unknown gaze)."""
        if self.board is None:
            raise NoDecoder("no decoder")
        if gaze not in GAZE_TARGET:
            raise ValueError(f"gaze must be one of {list(GAZES)}, got {gaze!r}")
        with self._lock:
            if gaze != self.gaze:
                self.gaze = gaze
                self._broadcast("gaze", {"gaze": gaze})
            self._look()
        return gaze

    def key_swipe(self, target: int, card: int | None = None) -> dict[str, Any]:
        """Swipe the card on screen now (keyboard or click); the swipe event's payload.

        ``card`` is the image the page was showing. When the decoder swiped it a moment
        earlier (the key press raced the swipe event), CardChanged is raised instead of
        swiping the next card, which nobody has seen yet.
        """
        if not isinstance(target, int) or isinstance(target, bool) or target not in TARGETS:
            raise ValueError(f"target must be one of {list(TARGETS)}, got {target!r}")
        if card is not None and (not isinstance(card, int) or isinstance(card, bool)):
            raise ValueError(f"card must be an image index, got {card!r}")
        with self._lock:
            if card is not None and card != self.card:
                raise CardChanged(self.card)
            if self.swiper is not None and not self.training:
                # the decoder must not fire again on EEG from before this card
                self.swiper.card_changed()
            return self._swipe(target, "key")

    def _swipe(self, target: int, source: str) -> dict[str, Any]:
        swiped = self.card
        outcome = self.feed.swipe(liked=target != PASS, super_like=target == SUPER_LIKE)
        payload = {"target": target, "label": self.look.labels[target], "source": source,
                   "swiped": swiped, "card": self.card, "liked": outcome.liked,
                   "super_like": outcome.super_like, "match": outcome.match,
                   "voice": voice_line(self.deck[outcome.card], outcome),
                   "counts": self.counts()}
        self._broadcast("swipe", payload)
        return payload

    def step(self) -> list[dict[str, Any]]:
        """One board read: train or decode, swipe, broadcast; the decision events sent."""
        if self.decoder is None or self.board is None or self.swiper is None:
            return []
        out = []
        with self._lock:
            self._look()
            X, t = self.board.read()
            keep = int(MAX_BACKLOG_S * self.decoder.fs)
            if X.shape[1] > keep:
                X, t = X[:, -keep:], t[-keep:]
            was_training = self.decoder.training
            decisions = self.swiper.feed(X, t, self.board.read_events())
            if was_training:
                self._training_news()
                return out
            for d in decisions:
                out.append(self._decision(d))
        return out

    def _training_news(self) -> None:
        d = self.decoder
        assert d is not None
        k, n = d.progress
        if d.training:
            if k != self._shown:
                self._shown = k
                self._broadcast("train", {"cycle": k, "of": n})
            return
        self._broadcast("train", {"cycle": n, "of": n})
        self._broadcast("trained", {"accuracy": _maybe(d.train_accuracy),
                                    "chance": round(d.chance, 4), "cycles": n})
        self._begin_decoding()

    def _decision(self, d: Decision) -> dict[str, Any]:
        sw = self.swiper
        assert sw is not None and self.board is not None
        fired = sw.fires(d)
        event = {"t": round(self.board.elapsed - self._decoding_from, 3),
                 "scores": [_num(s) for s in d.scores], "raw": int(d.raw),
                 "committed": int(d.committed), "lead": _maybe(d.gap),
                 "changed": bool(d.changed), "fired": fired, "votes": sw.votes,
                 "stale": sw.stale, "gaze": self.gaze, "true": self.board.fixated}
        self._broadcast("decision", event)
        if fired != ABSTAIN:
            # CodeSwiper.feed has already reset the decoder for the next card
            self._swipe(fired, "decoder")
        return event

    # -- background thread ---------------------------------------------------------------

    def start(self, poll_s: float = POLL_S) -> threading.Thread | None:
        """Run step() every ``poll_s`` in a daemon thread (none without a decoder)."""
        if self.decoder is None or self._thread is not None:
            return self._thread

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


def pretrained_decoder(train_cycles: int = live.TRAIN_CYCLES, seed: int = 0,
                       fs: float = 250.0, margin: float = MARGIN) -> live.CodeVepDecoder:
    """A CodeVepDecoder trained on its own standard fake board in virtual time (a fraction
    of a second of CPU): the simulated user looked at Pass for ``train_cycles`` cycles."""
    board = live.standard_fake_board(PASS, seed=seed, fs=fs)
    decoder = live.CodeVepDecoder(board.fs, train_cycles=train_cycles, margin=margin)
    board.start_flicker()
    limit = (train_cycles + 5) * 63 / 60.0 + 5.0
    while decoder.training:
        if board.elapsed > limit:
            raise RuntimeError(f"training saw {decoder.progress[0]} of {train_cycles} cycles")
        board.advance(POLL_S)
        X, t = board.read()
        decoder.feed(X, t, board.read_events())
    decoder.reset()
    return decoder


def build_engine(deck: Sequence[Profile], *, seed: int = 0, match_rate: float = 0.3,
                 cooldown_s: float = 0.5, switch_s: float = 8.0, decoder: bool = True,
                 train_cycles: int = live.TRAIN_CYCLES, pretrain: bool = False,
                 margin: float = MARGIN, realtime: bool = True,
                 queue_size: int = QUEUE_SIZE) -> Engine:
    """Deal the feed and, with ``decoder``, attach a c-VEP decoder and its fake board.

    The decoder trains on the board's first ``train_cycles`` cycles (the eyes on Pass),
    unless ``pretrain`` trains it first on a separate fake board in virtual time (seeded
    ``seed``; the decoding board is seeded ``seed + 1`` so its noise is new). The board
    runs in real time (``realtime``) or virtual time (tests: advance it and call step()).
    ``margin`` is the decoder's abstain margin (see MARGIN). Raises ValueError for a config
    that is not the standard's three-target code, or a bad setting.
    """
    cfg = load_config(CONFIG)
    if not is_standard_code(cfg):
        raise ValueError(f"config {cfg.name!r} is not the c-VEP standard's three-target code")
    look = Look(labels=tuple(cfg.labels),
                colors=tuple(_hex(cfg.flash_color(k)) for k in range(cfg.n_targets)),
                lags=tuple(k * cfg.code.lag_frames for k in range(cfg.n_targets)),
                cycle_s=round(cfg.code.cycle_frames / cfg.refresh, 4))
    feed = Feed(len(deck), match_rate=match_rate, seed=seed)
    if not decoder:
        return Engine(deck, feed, look, cooldown_s=cooldown_s, switch_s=switch_s,
                      queue_size=queue_size)
    if int(train_cycles) != train_cycles or train_cycles < 2:
        raise ValueError(f"train_cycles must be an integer >= 2, got {train_cycles!r}")
    board = live.standard_fake_board(PASS, seed=seed + 1,
                                     clock=time.time if realtime else None)
    if not (math.isfinite(margin) and margin >= 0):
        raise ValueError(f"margin must be >= 0, got {margin}")
    dec = (pretrained_decoder(train_cycles, seed=seed, fs=board.fs, margin=margin) if pretrain
           else live.CodeVepDecoder(board.fs, train_cycles=train_cycles, margin=margin))
    return Engine(deck, feed, look, cooldown_s=cooldown_s, switch_s=switch_s,
                  decoder=dec, board=board, queue_size=queue_size)


# -- HTTP ---------------------------------------------------------------------------------

SPEECH_MAX_CHARS = 300      # a voice line is one short sentence
SPEECH_TIMEOUT_S = 10.0     # Cartesia's own timeout is 8 s a try


class SwipeServer(ThreadingHTTPServer):
    """The HTTP server; ``engine`` and ``cards`` (PNG paths by image index) are attached
    once startup is done, after the port is bound."""

    daemon_threads = True          # SSE streams must not hold up Ctrl-C
    allow_reuse_address = True     # restart at once after Ctrl-C (TIME_WAIT)
    allow_reuse_port = False       # but never share the port with another server

    def __init__(self, address: tuple[str, int], engine: Engine | None = None,
                 cards: Sequence[Path] = (), web_dir: Path = WEB_DIR,
                 ping_s: float = PING_S, speaker: Any = None) -> None:
        super().__init__(address, Handler)
        self.engine = engine
        self.cards = list(cards)
        self.web_dir = Path(web_dir)
        self.ping_s = float(ping_s)
        self._speaker = speaker

    @property
    def speaker(self) -> Any:
        """What /api/speech synthesises with: the given speaker, else the process-wide
        Cartesia one (None when BRAINSYNC_TTS=off)."""
        if self._speaker is None:
            from . import voice
            self._speaker = voice.default_speaker()
        return self._speaker

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
        elif path == "/api/speech":
            self._speech(urllib.parse.urlsplit(self.path).query)
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

    def _speech(self, query: str) -> None:
        """A swipe's voice line as WAV from Cartesia (the key stays on the server); 503
        when there is no voice, and the page then shows the line without speaking it."""
        text = " ".join(urllib.parse.parse_qs(query).get("text", [""])[0].split())
        if not text or len(text) > SPEECH_MAX_CHARS:
            self._error(400, f"text must be 1..{SPEECH_MAX_CHARS} characters")
            return
        speaker = self.server.speaker
        try:
            clip = speaker.clip(text, timeout=SPEECH_TIMEOUT_S) if speaker is not None else None
            body = clip.read_bytes() if clip is not None else None
        except Exception:  # noqa: BLE001 - timeout or a vanished cache file: no voice
            body = None
        if body is None:
            self._error(503, "no voice (Cartesia unavailable)")
            return
        self._send(200, body, "audio/wav", cache="private, max-age=3600")

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
        if not isinstance(target, int) or isinstance(target, bool) or target not in TARGETS:
            self._error(400, f"target must be {PASS} (Pass), {SUPER_LIKE} (Super like) or "
                             f"{LIKE} (Like)")
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
                   help="seconds on each square while the gaze is on Auto (default 8)")
    p.add_argument("--cooldown", type=float, default=0.5,
                   help="seconds after a card change before a cycle may vote (default 0.5)")
    p.add_argument("--deck", metavar="PATH", default=None,
                   help="profiles TOML (default: the bundled deck of invented profiles)")
    p.add_argument("--match-rate", type=float, default=0.3,
                   help="chance that a Like is a match (default 0.3; a Super like always is)")
    p.add_argument("--seed", type=int, default=0,
                   help="card order, matches and board noise (default 0, so demos repeat)")
    p.add_argument("--train-cycles", type=int, default=live.TRAIN_CYCLES,
                   help="code cycles of simulated training on Pass before decoding (default "
                        f"{live.TRAIN_CYCLES}, about {live.TRAIN_CYCLES * 1.05:.0f} s)")
    p.add_argument("--margin", type=float, default=MARGIN,
                   help="abstain unless the best square leads by this correlation (default "
                        f"{MARGIN:g}: reading the card then hardly ever swipes)")
    p.add_argument("--pretrain", action="store_true",
                   help="train the template at startup in virtual time: decoding starts at once")
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
    if args.train_cycles < 2:
        p.error(f"--train-cycles must be >= 2, got {args.train_cycles}")
    if not (math.isfinite(args.margin) and args.margin >= 0):
        p.error(f"--margin must be >= 0, got {args.margin}")


def _serve(args: argparse.Namespace, server: SwipeServer, deck: list[Profile], tmp: str) -> int:
    try:
        engine = build_engine(deck, seed=args.seed, match_rate=args.match_rate,
                              cooldown_s=args.cooldown, switch_s=args.switch,
                              decoder=not args.no_decoder, train_cycles=args.train_cycles,
                              pretrain=args.pretrain, margin=args.margin)
    except (ValueError, RuntimeError) as exc:
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
        mode = ("c-VEP on a simulated EEG board, no flicker" if engine.has_decoder
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
    # bind first: a busy port should fail before a second of rendering and training
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
