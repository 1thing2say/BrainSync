"""The server's main loop: phases, calibration, onset-gated selections and the client's mode.

    bank = DecoderBank()
    session = Session(transport, board, bank, (15.0, 17.0, 12.0, 20.0), fake=True,
                      point_factory=None)
    transport = Transport(..., health=session.health, catchup=session.catchup)
    session.run(stop_event)            # blocking; broadcasts phase "stop" when it returns

The session knows nothing about sockets. It reads ``transport.events()`` (objects with
``kind``, ``conn_id``, ``role``, ``msg``, ``hello``, ``t`` and ``close_code``), answers
through ``transport.send`` (broadcast) and ``transport.send_to`` (one connection), and reads
every board sample into the DecoderBank. A Mode (SlotsMode here, PointMode in
brainsync.screen.controller) turns selections into protocol messages.

Phases (PROTOCOL-v3 section 3). The effective phase is "wait" without a ready controller,
"calibrate" while calibrating, "pause" while the user paused or the mode holds (a click in
flight), and "run" otherwise. It is broadcast whenever it changes. The decoder runs only in
"run": entering it activates the bank for the current layout (gate = now + latency), and
leaving it deactivates the bank. A layout keeps its id across a pause, so the client sends no
second onset after one; the gate is then the time the run resumed.

A controller is ready once its refresh rate checks out (``error refresh`` keeps it waiting)
and its mode exists (point mode without a detector gets ``error detect_unavailable``). It is
then calibrated unless the bank holds a calibration under 10 minutes old: ``calibrate_s``
seconds of board samples become the rest baseline, with the fake board's tone held off
meanwhile. Then the mode starts and the phase is "run". A resume after a pause of 120 s or
more recalibrates. A controller that leaves (disconnect, bye or a takeover) stops its mode,
clears the layout and returns the session to "wait"; the board stays open.

Point mode also gets the controller's "rescan" (v3.1: the focused app or window changed), and
a "resume" message after a user pause ends, so it can ask for a fresh scan.

Mode messages. The session intercepts "layout": it stamps a session-wide layout_id (so an
onset from an older layout never matches), activates the bank with the targets' frequencies
and phases, broadcasts it and keeps it for clients that join later. It intercepts "phase":
a mode may ask only for "pause" (hold) or "run" (release). "scan_request" goes to the
controller only; anything else (click, scan_result, error) is broadcast, so viewers see every
click as PROTOCOL-v3 section 1 says. Only the controller acts on a click: a viewer may not
answer "clicked".

Selections. A gated decision that newly commits a target (``changed`` and not ABSTAIN)
during "run" becomes a broadcast "selection" for that target's slot, and then
``mode.on_selection``. In slots mode a 1.5 s cooldown follows, as in brainsync.scroll: the
first decision at or past its end re-arms the voter (bank.rearm) and is dropped, and a commit
counts again only once ``votes`` decisions arrived after the re-arm. A user who keeps looking
gets one selection about every 2.7 s (dwell to repeat).

Streaming gate (v3.1). A commit becomes a selection, and so can lead to a click, only while
the board streams: no board error, and samples delivered within STATUS_S (1.0 s) before the
read that produced it. Otherwise the pick is dropped: a "selection_dropped" log record and a
note say why, and the voter is re-armed, so a gaze that is still held commits again once the
board streams.

Refresh. A controller's display must refresh within ``refresh_tolerance_hz`` of one of
``refresh_rates`` (60, 90, 120 and 144 Hz by default; the first is the nominal rate, the
welcome's ``refresh_hz``). The luminance is frame-indexed and a client off the nominal rate
computes it at its measured refresh, so 15 Hz is 4 frames per cycle at 60 Hz and 8 at 120 Hz.
``ignore_refresh`` accepts any rate; such a client draws still any target it cannot show.

Dropped frames. Each ``frames`` report carries the client's running count of dropped frames.
The difference from the previous report, over the frames expected since it (the report's
refresh_hz times the seconds between the two), above 10 % makes the status ``ok: false`` with
a sentence for the user, sent at once; a later report under the limit clears it. The first
report of a controller only sets the baseline, and so does a report less than 2.5 s or more
than 10 s after the previous one: the count then also holds late frames from a stretch of
flicker before a pause or a new layout, which no gap can be divided by, so that report is not
judged and the status stays as it was. Decoding is not touched.

Onsets. An ``onset`` for the current layout, arriving within 2 s of the activation, moves the
gate to ``t_client + offset`` (offset: the median of the controller's last 8 ping
receipt-time minus t_client), or to its receipt time while no ping has come. The onset is
clamped between the activation and its receipt, since the client cannot flicker before it
got the run phase or after it reported.

Code mode (v3.2, ``CodeBank`` instead of a DecoderBank; docs/CVEP-STANDARD.md). The
targets are three copies of one m-sequence, slot i of a slots layout at lag 21 i frames, and
point mode's K = 3 targets get theirs from the stand-in frequencies of coding.POINT_KEYS.
There is no rest calibration: a controller first goes through the "train" phase (flicker on,
``cue`` the slot at lag 0, text "Look at the <LABEL> square") until the decoder has its
template, unless it holds one under 10 minutes old. Slots mode trains on its own layout;
point mode trains on a "train" layout of the mode's ``train_targets()`` (BrainPoint's b1,
b2 and more in a centred row; the session's t0, t1, t2 for a mode without it) and starts
its mode afterwards. The point factory is called with ``viewport=(w, h)`` in code mode. "train" progress goes out on every new cycle, then "trained"
with the held-out accuracy and chance, and the phase turns "run". A pause during training
restarts the training on resume, and so does a resume after a long pause. The controller's
``cycle`` messages (each cycle's first flip) for the running layout are mapped to the board's
clock with the same offset as ``onset`` (never later than their receipt) and decoded; the
fake board reports its own cycle times instead, and looks at the cue while training. Every
decision of the running layout is broadcast as "decision" (scores per slot, the best slot,
the committed one, the lead). A commit is a selection as in freq mode, with the same
cooldown, which also follows the end of the training so a gaze still on the cue does not
pick it at once. Onsets are only logged.

``catchup`` and ``health`` run on the transport's thread. The phase and layout are updated
and broadcast under one lock, so a joining client gets each change exactly once in order.
Notes go to stderr. ``log(kind, fields)`` receives one record per event of interest
(connect, phase, layout, onset, selection, calibration, error, ...) and never sees the token.
"""
from __future__ import annotations

import copy
import math
import statistics
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import numpy as np

from ..classify import ABSTAIN
from ..online import Decision
from ..recordings import POSTERIOR
from . import protocol as P
from .coding import POINT_KEYS, CodeBank, lag_index
from .decoding import DecoderBank

__all__ = ["Mode", "SlotsMode", "Session", "RECALIBRATE_AFTER_S", "LONG_PAUSE_S",
           "TRAIN_LABELS", "train_text"]

RECALIBRATE_AFTER_S = 600.0   # a calibration older than this is redone for a new controller
LONG_PAUSE_S = 120.0          # a resume after a pause this long recalibrates
STATUS_S = 1.0                # status period, and how recent a sample must be for "streaming"
N_OFFSETS = 8                 # ping offsets in the median
WAIT_TEXT = "Waiting for a controller to connect"
CALIBRATE_TEXT = "Look at the screen and relax"
PAUSE_TEXT = "Paused; press Resume to go on"
HOLD_TEXT = "Working"
STOP_TEXT = "The BrainSync server stopped"
FRAMES_S = 5.0                # the clients' "frames" report period
DROP_LIMIT = 0.10             # dropped frames above this share of the expected ones: ok false
# Point mode's training layout (code mode): three patches in a centred row, the first cued.
TRAIN_LABELS = ("Left", "Middle", "Right")
TRAIN_PATCH = 0.24            # patch height, in viewport heights (square in pixels)


def _note(text: str) -> None:
    print(f"session: {text}", file=sys.stderr, flush=True)


def train_text(label: str) -> str:
    """The train phase's text: "Look at the YES square" (docs/CVEP-STANDARD.md)."""
    return f"Look at the {label.upper()} square"


def train_layout(viewport: Mapping[str, Any] | None, lag_frames: int) -> list[P.Target]:
    """Point mode's training targets: three square patches in a centred row (t0, t1, t2)."""
    aspect = 16 / 9
    if viewport and viewport.get("w") and viewport.get("h"):
        aspect = float(viewport["w"]) / float(viewport["h"])
    h = TRAIN_PATCH
    w = min(h / aspect, 0.25)
    gap = w / 2
    x0 = (1.0 - (3 * w + 2 * gap)) / 2
    y = (1.0 - h) / 2
    return [P.Target(f"t{k}", None, 0.0, TRAIN_LABELS[k], "slot",
                     (P.Rect(x0 + k * (w + gap), y, w, h),), lag=k * lag_frames)
            for k in range(3)]


class Mode(Protocol):
    """What a client mode does; every method returns protocol messages to send."""

    def start(self, now: float) -> list[dict]: ...

    def on_selection(self, slot: str, now: float) -> list[dict]: ...

    def on_message(self, msg: dict, now: float) -> list[dict]: ...   # scan, clicked

    def tick(self, now: float) -> list[dict]: ...

    def stop(self) -> None: ...


class SlotsMode:
    """Slots mode: slot i gets freqs[i] at phase 0, in one "slots" layout without rects.

    The client places its own targets and acts on the selections itself, so a selection
    changes nothing here. ``freqs`` may be longer than ``slots``; the extra ones go unused.
    Code mode passes ``lags`` (frames, slot i at lags[i]) instead of ``freqs``.
    """

    def __init__(self, slots: Sequence[str], freqs: Sequence[float] = (), *,
                 lags: Sequence[int] | None = None) -> None:
        slots = tuple(slots)
        if not slots:
            raise ValueError("slots mode needs at least one slot name, such as (\"yes\", \"no\")")
        if lags is not None:
            lags = tuple(lags)
            if len(lags) < len(slots):
                raise ValueError(f"{len(slots)} slots need {len(slots)} code lags but the "
                                 f"server shows {len(lags)} targets; name fewer slots")
            self.targets = tuple(P.Target(s, None, 0.0, s, "slot", lag=lag)
                                 for s, lag in zip(slots, lags))
            P.layout(0, "slots", self.targets)
            self.slots = slots
            return
        freqs = tuple(float(f) for f in freqs)
        if len(freqs) < len(slots):
            raise ValueError(f"{len(slots)} slots need {len(slots)} frequencies but the server "
                             f"has {len(freqs)}; name fewer slots or start it with more --freqs")
        # Target and protocol.layout check the names, frequencies and uniqueness
        self.targets = tuple(P.Target(s, f, 0.0, s, "slot") for s, f in zip(slots, freqs))
        P.layout(0, "slots", self.targets)
        self.slots = slots

    def start(self, now: float) -> list[dict]:
        return [P.layout(0, "slots", self.targets)]   # the session stamps the layout_id

    def on_selection(self, slot: str, now: float) -> list[dict]:
        return []

    def on_message(self, msg: dict, now: float) -> list[dict]:
        return []

    def tick(self, now: float) -> list[dict]:
        return []

    def stop(self) -> None:
        pass


class _Refused(Exception):
    """A controller that cannot be made ready: (error code, message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Session:
    """Ties the transport, the board, the decoder bank and the controller's mode together.

    ``fake`` means the board is a FakeBoard: ``gaze`` messages then move its tone. The
    tone is held off (-1) until a gaze names a slot, so an unattended fake board commits
    nothing. ``point_factory`` makes a point-mode Mode per controller; None means point
    mode is unavailable (no API key, --no-detect). ``clock`` must be the board's clock
    (time.time for real boards and real-time fake boards).

    A CodeBank as ``bank`` runs code mode (v3.2): ``freqs`` and ``calibrate_s`` are then
    unused, and a fake board is a SequenceFakeBoard (vep.live.standard_fake_board).
    """

    def __init__(self, transport: Any, board: Any, bank: DecoderBank | CodeBank,
                 freqs: Sequence[float] = (), *,
                 fake: bool, point_factory: Callable[[], Mode] | None, calibrate_s: float = 10.0,
                 refresh_hz: float = P.REFRESH_RATES[0],
                 refresh_tolerance_hz: float = P.REFRESH_TOLERANCE_HZ,
                 refresh_rates: Sequence[float] | None = None,
                 ignore_refresh: bool = False, waveform: str = "sine",
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str, dict], None] | None = None) -> None:
        self.paradigm = "code" if isinstance(bank, CodeBank) else "freq"
        code = self.paradigm == "code"
        try:
            self.freqs = () if code else tuple(float(f) for f in freqs)
        except (TypeError, ValueError):
            raise ValueError(f"freqs must be numbers of Hz, got {freqs!r}") from None
        if not code and not self.freqs:
            raise ValueError("the session needs at least one frequency, such as 15,17")
        if not all(math.isfinite(f) and 0 < f < bank.fs / 2 for f in self.freqs):
            raise ValueError(f"frequencies must lie in (0, {bank.fs / 2:g}) Hz, got "
                             f"{list(self.freqs)}")
        if len(set(self.freqs)) != len(self.freqs):
            raise ValueError(f"frequencies must be unique, got {list(self.freqs)}")
        calibrate_s = float(calibrate_s)
        if not code and (not math.isfinite(calibrate_s) or calibrate_s < bank.min_calibrate_s):
            raise ValueError(f"calibrate_s must be at least {bank.min_calibrate_s:.2f} s (two "
                             f"decoder windows plus the pad), got {calibrate_s}")
        refresh_hz, refresh_tolerance_hz = float(refresh_hz), float(refresh_tolerance_hz)
        if not math.isfinite(refresh_hz) or refresh_hz <= 0:
            raise ValueError(f"refresh_hz must be a positive number, got {refresh_hz}")
        if not math.isfinite(refresh_tolerance_hz) or refresh_tolerance_hz < 0:
            raise ValueError(f"refresh_tolerance_hz must be >= 0, got {refresh_tolerance_hz}")
        if refresh_rates is None:      # code mode: one bit a frame, so the nominal rate only
            refresh_rates = (refresh_hz,) if code else P.REFRESH_RATES
        try:
            rates = tuple(float(r) for r in refresh_rates)
        except (TypeError, ValueError):
            raise ValueError(f"refresh_rates must be numbers of Hz, got "
                             f"{refresh_rates!r}") from None
        if not all(math.isfinite(r) and r > 0 for r in rates):
            raise ValueError(f"refresh_rates must be positive numbers of Hz, got {list(rates)}")
        # the nominal rate is always accepted, and comes first
        rates = (refresh_hz,) + tuple(r for r in dict.fromkeys(rates) if r != refresh_hz)
        lowest, top = min(rates), max(self.freqs, default=0.0)
        if self.freqs and lowest - refresh_tolerance_hz <= 2 * top:
            raise ValueError(f"a {lowest:g} Hz display cannot show {top:g} Hz flicker (a display "
                             f"shows less than half its refresh); drop that refresh rate or the "
                             f"frequency")
        if not isinstance(waveform, str) or not waveform:
            raise ValueError(f"waveform must be a name such as \"sine\", got {waveform!r}")
        if abs(float(board.fs) - bank.fs) > 1e-6:
            raise ValueError(f"the board samples at {board.fs:g} Hz but the bank decodes at "
                             f"{bank.fs:g} Hz; build the bank with the board's fs")
        if fake and not callable(getattr(board, "set_target", None)):
            raise ValueError("fake=True needs a board with set_target() (a FakeBoard)")
        if fake and code and not all(callable(getattr(board, name, None)) for name in
                                     ("start_flicker", "stop_flicker", "read_events")):
            raise ValueError("code mode with fake=True needs a coded fake board "
                             "(vep.live.standard_fake_board)")
        self._transport = transport
        self._board = board
        self._bank = bank
        self.fake = bool(fake)
        self._point_factory = point_factory
        self.calibrate_s = calibrate_s
        self.refresh_hz = refresh_hz
        self.refresh_tolerance_hz = refresh_tolerance_hz
        self.refresh_rates = rates
        self.ignore_refresh = bool(ignore_refresh)
        self.waveform = waveform
        self._clock = clock
        self._log_fn = log
        self._lock = threading.RLock()
        names = tuple(getattr(board, "ch_names", ()))
        self._posterior = [names.index(c) for c in POSTERIOR if c in names]
        # controller
        self._ctrl: int | None = None
        self._hello: P.Hello | None = None
        self._ready = False
        self._wait_text = WAIT_TEXT
        self._mode: Mode | None = None
        self._started = False
        self._offsets: deque[float] = deque(maxlen=N_OFFSETS)
        # calibration and pauses
        self._cal_start: float | None = None
        self._cal_chunks: list[np.ndarray] = []
        self._cal_n = 0
        self._calibrated_at: float | None = (
            clock() if (bank.trained if code else bank.calibrated) else None)
        # code mode: training (the train phase) and its last messages, for late joiners
        self._training = False
        self._train_cycle = -1
        self._train_msg: dict | None = None
        self._trained_msg: dict | None = None
        self._user_paused = False
        self._paused_at = 0.0
        self._hold = False
        self._hold_text = HOLD_TEXT
        # layout and selections
        self._layout_seq = 0
        self._layout: dict | None = None
        self._targets: list[dict] = []
        self._last_pick: tuple[float, float, float, int] | None = None  # (freq, gate, commit, layout)
        self._activated_at = -math.inf
        self._selection_seq = 0
        self._cool_until: float | None = None
        self._since_rearm: int | None = None
        self._gaze: str | None = None
        # board status
        self._board_state = "unknown"
        self._board_error = ""
        self._last_sample = -math.inf
        self._gap_s = math.inf        # seconds without samples before the latest delivery
        self._status_at = -math.inf
        self._acc_n = 0
        self._acc_sum = np.zeros(len(self._posterior))
        self._acc_sq = np.zeros(len(self._posterior))
        self._status_msg: dict | None = None
        self._frames_last: tuple[float, int] | None = None   # last report: (received, dropped)
        self._frames_text = ""        # the dropped-frames problem shown in status, or ""
        self._status_text = ""        # the problem the last status carried
        self._last_crash = ""
        self._closed = False
        self._phase_msg = P.phase("wait", False, text=WAIT_TEXT)
        if self.fake:
            board.set_target(-1)

    # -- read-only views ----------------------------------------------------------------

    @property
    def phase(self) -> str:
        return self._phase_msg["phase"]

    @property
    def controller(self) -> int | None:
        return self._ctrl

    @property
    def layout(self) -> dict | None:
        """The last layout broadcast (None after the controller left)."""
        return self._layout

    @property
    def calibrating(self) -> bool:
        return self._cal_start is not None

    @property
    def training(self) -> bool:
        """Code mode: the controller is in the train phase's training."""
        return self._training

    @property
    def offset(self) -> float | None:
        """Median of t_server - t_client over the controller's recent pings, or None."""
        return statistics.median(self._offsets) if self._offsets else None

    @property
    def max_slots(self) -> int:
        return P.CODE_MAX_SLOTS if self.paradigm == "code" else len(self.freqs)

    # -- transport-thread entry points --------------------------------------------------

    def catchup(self, conn_id: int, role: str, hello: P.Hello) -> list[dict]:
        """What a joining client gets first: welcome, phase, then the layout and the status.

        In code mode the training's last "train" progress (while training) or its "trained"
        result (once the template exists) follows.
        """
        code = None
        if self.paradigm == "code":
            code = P.code_spec(**self._bank.code)
        with self._lock:
            out = [P.welcome(role, hello.mode, fake=self.fake, waveform=self.waveform,
                             refresh_hz=self.refresh_hz,
                             refresh_tolerance_hz=self.refresh_tolerance_hz,
                             freqs=self.freqs, max_slots=self.max_slots,
                             detect=self._point_factory is not None,
                             refresh_rates=self.refresh_rates, paradigm=self.paradigm,
                             code=code),
                   copy.deepcopy(self._phase_msg)]
            if self._layout is not None:
                out.append(copy.deepcopy(self._layout))
            if self._status_msg is not None:
                out.append(copy.deepcopy(self._status_msg))
            if self._training and self._train_msg is not None:
                out.append(copy.deepcopy(self._train_msg))
            elif self._trained_msg is not None:
                out.append(copy.deepcopy(self._trained_msg))
            return out

    def health(self) -> dict:
        """The /health answer."""
        with self._lock:
            return {"ok": True, "board": self._board_state, "controller": self._ctrl is not None,
                    "viewers": int(getattr(self._transport, "n_viewers", 0) or 0)}

    # -- main loop ----------------------------------------------------------------------

    def step(self) -> None:
        """Drain events, read the board, feed the bank, select, tick the mode, send status."""
        now = float(self._clock())
        for ev in self._transport.events():
            try:
                self._on_event(ev, now)
            except Exception as exc:   # a bad event must not stop the headset loop
                self._crash(f"handling a {getattr(ev, 'kind', '?')} event", exc)
        X, t = self._read(now)
        if self.paradigm == "code":
            self._fake_cycles()
        if X.shape[1]:
            decisions = self._bank.feed(X, t)
            if self.paradigm == "code":
                self._train_step(now)
            self._decide(decisions, now)
            if self._cal_start is not None:
                self._collect(X, t, now)
        if self._started and self._mode is not None:
            self._mode_call("tick", now)
        self._maybe_status(now)

    def run(self, stop: threading.Event, poll_s: float = 0.05) -> None:
        """step() every ``poll_s`` until ``stop`` is set, then close() (phase "stop")."""
        if not math.isfinite(poll_s) or poll_s <= 0:
            raise ValueError(f"poll_s must be positive, got {poll_s}")
        try:
            while not stop.is_set():
                try:
                    self.step()
                except Exception as exc:
                    self._crash("in step()", exc)
                stop.wait(poll_s)
        finally:
            self.close()

    def close(self, text: str = STOP_TEXT) -> None:
        """Stop the mode and broadcast phase "stop"; safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._stop_mode()
        self._deactivate()
        msg = P.phase("stop", False, text=text)
        with self._lock:
            self._phase_msg = msg
            self._transport.send(msg)
        self._log("phase", {"phase": "stop"})

    # -- events -------------------------------------------------------------------------

    def _on_event(self, ev: Any, now: float) -> None:
        kind = ev.kind
        if kind == "connect":
            self._log("connect", {"conn_id": ev.conn_id, "role": ev.role,
                                  "client": dict(getattr(ev.hello, "client", {}) or {})})
            if ev.role == "controller":
                self._controller_joined(ev, now)
        elif kind == "disconnect":
            self._log("disconnect", {"conn_id": ev.conn_id, "role": ev.role,
                                     "close_code": ev.close_code})
            if ev.conn_id == self._ctrl:
                self._controller_left(now, "disconnected")
        elif kind == "message":
            if ev.conn_id == self._ctrl and isinstance(ev.msg, Mapping):
                self._on_message(ev.msg, float(ev.t), now)
        else:
            _note(f"ignored an event of unknown kind {kind!r}")

    def _controller_joined(self, ev: Any, now: float) -> None:
        if self._ctrl is not None:          # takeover: the transport closed the old one
            self._controller_left(now, "replaced")
        hello = ev.hello
        self._ctrl = ev.conn_id
        self._hello = hello
        self._ready = False
        self._offsets.clear()
        self._frames_last = None
        self._frames_text = ""
        try:
            self._mode = self._make_mode(hello)
        except _Refused as exc:
            self._wait_text = exc.message
            self._transport.send_to(ev.conn_id, P.error(exc.code, exc.message))
            self._log("error", {"code": exc.code, "message": exc.message})
            self._update_phase(now)
            return
        self._ready = True
        self._wait_text = WAIT_TEXT
        ready = self._bank.trained if self.paradigm == "code" else self._bank.calibrated
        stale = (self._calibrated_at is None or not ready
                 or now - self._calibrated_at > RECALIBRATE_AFTER_S)
        if stale and self.paradigm == "code":
            self._start_training(now)
        elif stale:
            self._start_calibration(now)
        else:
            self._start_mode(now)
        self._update_phase(now)

    def _make_mode(self, hello: P.Hello) -> Mode:
        problem = P.check_refresh(hello.refresh_hz, self.refresh_rates, self.refresh_tolerance_hz)
        if problem is not None and not self.ignore_refresh:
            raise _Refused("refresh", problem)
        if problem is not None:
            hz = hello.refresh_hz
            _note(f"accepted a {hz} Hz display (--ignore-refresh); it computes the flicker at "
                  f"its own refresh, so the frequencies stay {list(self.freqs)} Hz"
                  + ("" if hz is None else
                     f", except that any at or above {hz / 2:g} Hz is drawn still"))
            self._log("refresh_ignored", {"refresh_hz": hz})
        if hello.mode == "point":
            if self._point_factory is None:
                raise _Refused("detect_unavailable",
                               "Point mode needs element detection, which this server has "
                               "turned off (no ANTHROPIC_API_KEY, or --no-detect); set the key "
                               "and restart the server, or use slots mode.")
            try:
                if self.paradigm == "code":   # the c-VEP PointMode sizes its training patches
                    vp = hello.viewport
                    return self._point_factory(
                        viewport=None if not vp else (float(vp["w"]), float(vp["h"])))
                return self._point_factory()
            except Exception as exc:
                raise _Refused("detect_unavailable",
                               f"Point mode could not start: {exc}") from exc
        slots = tuple(hello.slots or ())
        if len(slots) > self.max_slots:
            what = "targets" if self.paradigm == "code" else "frequencies"
            raise _Refused("too_many_slots",
                           f"this server has {self.max_slots} {what}, so name at most "
                           f"{self.max_slots} slots, not {len(slots)}")
        try:
            if self.paradigm == "code":
                lag = self._bank.lag_frames
                return SlotsMode(slots, lags=[i * lag for i in range(self.max_slots)])
            return SlotsMode(slots, self.freqs)
        except ValueError as exc:
            raise _Refused("bad_hello", str(exc)) from exc

    def _controller_left(self, now: float, why: str) -> None:
        self._stop_mode()
        self._ctrl = None
        self._hello = None
        self._ready = False
        self._wait_text = WAIT_TEXT
        self._cal_start = None
        self._cal_chunks = []
        if self._training:               # an unfinished training is not kept
            self._training = False
            self._bank.retrain()
        self._user_paused = False
        self._hold = False
        self._offsets.clear()
        self._frames_last = None
        self._frames_text = ""
        self._gaze = None
        self._update_phase(now)          # flicker off first
        if self._layout is not None:
            self._layout_seq += 1
            idle = P.layout(self._layout_seq, "idle", [])
            with self._lock:
                self._layout = None
                self._transport.send(idle)
        self._targets = []
        self._deactivate()
        self._apply_gaze()
        self._log("controller_left", {"why": why})

    def _stop_mode(self) -> None:
        mode, self._mode, self._started = self._mode, None, False
        if mode is not None:
            try:
                mode.stop()
            except Exception as exc:
                self._crash("stopping the mode", exc)

    def _on_message(self, msg: Mapping[str, Any], t_recv: float, now: float) -> None:
        kind = msg.get("type")
        if kind == "onset":
            self._onset(msg, t_recv)
        elif kind == "cycle":
            self._cycle(msg, t_recv)
        elif kind == "ping":
            t_client = msg.get("t_client")
            if isinstance(t_client, (int, float)) and math.isfinite(t_client):
                self._offsets.append(t_recv - float(t_client))
        elif kind == "frames":
            self._log("frames", {"refresh_hz": msg.get("refresh_hz"),
                                 "dropped": msg.get("dropped")})
            self._frames(msg, t_recv)
        elif kind == "pause":
            if not self._user_paused:
                self._user_paused = True
                self._paused_at = now
                self._update_phase(now)
        elif kind == "resume":
            if self._user_paused:
                self._user_paused = False
                long_pause = now - self._paused_at >= LONG_PAUSE_S
                if self.paradigm == "code":
                    # a training paused part way, or a template from before a long pause
                    # (the headset may have moved), is recorded again
                    if self._training or long_pause:
                        self._start_training(now)
                elif long_pause and self._cal_start is None:
                    self._start_calibration(now)
                self._update_phase(now)
                # the screen may have changed while paused: point mode scans again
                if self._started and self._hello is not None and self._hello.mode == "point":
                    self._mode_call("on_message", {"type": "resume"}, now)
        elif kind == "bye":
            self._controller_left(now, "bye")
        elif kind == "gaze":
            if not self.fake:
                self._refuse("not_allowed", "gaze works only when the server runs --board "
                                            "fake; a real headset reads your eyes itself")
                return
            slot = msg.get("slot")
            self._gaze = slot if isinstance(slot, str) else None
            self._apply_gaze()
        elif kind in ("scan", "clicked", "rescan"):
            if self._hello is None or self._hello.mode != "point":
                self._refuse("not_allowed", f"{kind} is for point mode only; this controller "
                                            f"said mode \"slots\" in its hello")
                return
            if self._started:
                self._mode_call("on_message", dict(msg), now)

    def _frames(self, msg: Mapping[str, Any], t_recv: float) -> None:
        """Judge one frames report against the previous one (see the module docstring)."""
        hz, dropped = float(msg["refresh_hz"]), int(msg["dropped"])
        last, self._frames_last = self._frames_last, (t_recv, dropped)
        if last is None or dropped < last[1]:
            return                         # the baseline, or the client's count restarted
        gap = t_recv - last[0]
        if not FRAMES_S / 2 <= gap <= 2 * FRAMES_S:
            return                         # a pause in between, or reports bunched up in transit
        share = (dropped - last[1]) / (hz * gap)
        text = (f"the display dropped {round(100 * share)}% of frames; close other apps or "
                f"plug in") if share > DROP_LIMIT else ""
        if text != self._frames_text:
            self._frames_text = text
            self._log("dropped_frames", {"share": share, "refresh_hz": hz, "ok": not text})

    def _refuse(self, code: str, message: str) -> None:
        if self._ctrl is not None:
            self._transport.send_to(self._ctrl, P.error(code, message))
        self._log("error", {"code": code, "message": message})

    def _client_time(self, t_client: float, t_recv: float) -> float:
        """A controller time on the board's clock: t_client + offset (the receipt time while
        no ping has come), never later than the receipt."""
        offset = self.offset
        t = t_recv if offset is None else float(t_client) + offset
        return min(t, t_recv)

    def _cycle(self, msg: Mapping[str, Any], t_recv: float) -> None:
        """A cycle's first flip (code mode): decoded when it is of the running layout."""
        fields = {"layout_id": msg.get("layout_id"), "n": msg.get("n"),
                  "t_client": msg.get("t_client")}
        if self.paradigm != "code":
            self._log("cycle_ignored", {**fields, "why": "the server runs --paradigm freq"})
            return
        if self.fake:
            return        # the fake board reports its own cycle times (_fake_cycles)
        layout = self._layout
        if (layout is None or msg.get("layout_id") != layout["layout_id"]
                or not self._bank.active):
            self._log("cycle_ignored", {**fields, "why": "not the running layout"})
            return
        t = self._client_time(msg["t_client"], t_recv)
        if self._bank.add_cycle(t):
            self._log("cycle", {**fields, "t_cycle": t})
        else:
            self._log("cycle_ignored", {**fields, "t_cycle": t,
                                        "why": "before the layout started flickering"})

    def _onset(self, msg: Mapping[str, Any], t_recv: float) -> None:
        layout = self._layout
        fields = {"layout_id": msg.get("layout_id"), "t_client": msg.get("t_client")}
        if self.paradigm == "code":      # the cycles carry the timing; the onset is logged
            if layout is not None and msg.get("layout_id") == layout["layout_id"]:
                self._log("onset", {**fields,
                                    "t_onset": self._client_time(msg["t_client"], t_recv)})
            else:
                self._log("onset_ignored", {**fields, "why": "not the running layout"})
            return
        if (layout is None or msg.get("layout_id") != layout["layout_id"]
                or self._bank.active is None):
            self._log("onset_ignored", {**fields, "why": "not the running layout"})
            return
        if t_recv - self._activated_at > P.ONSET_TIMEOUT_S:
            self._log("onset_ignored", {**fields, "why": "late"})
            return
        offset = self.offset
        t = t_recv if offset is None else float(msg["t_client"]) + offset
        t = min(max(t, self._activated_at), t_recv)
        self._bank.set_onset(t)
        self._log("onset", {**fields, "t_onset": t, "gate": self._bank.gate})

    # -- calibration --------------------------------------------------------------------

    def _start_calibration(self, now: float) -> None:
        self._cal_start = now
        self._cal_chunks = []
        self._cal_n = 0
        self._apply_gaze()
        self._log("calibrate_start", {"seconds": self.calibrate_s})

    def _collect(self, X: np.ndarray, t: np.ndarray, now: float) -> None:
        assert self._cal_start is not None
        keep = t >= self._cal_start
        if keep.any():
            self._cal_chunks.append(X[:, keep])
            self._cal_n += int(keep.sum())
        need = int(round(self.calibrate_s * self._bank.fs))
        if self._cal_n < need:
            return
        data = np.concatenate(self._cal_chunks, axis=1)[:, :need]
        self._cal_chunks = []
        try:
            self._bank.calibrate_from(data)
        except ValueError as exc:
            _note(f"calibration failed, trying again: {exc}")
            self._refuse("internal", f"Calibration failed ({exc}); keep still, it restarts now.")
            self._start_calibration(now)
            return
        cal = self._bank.calibration
        self._log("calibrated", {"seconds": self.calibrate_s,
                                 "n_windows": None if cal is None else cal.n_windows,
                                 "n_rejected": None if cal is None else cal.n_rejected})
        self._cal_start = None
        self._calibrated_at = now
        self._apply_gaze()
        if not self._started:
            self._start_mode(now)
        self._update_phase(now)

    def _start_mode(self, now: float) -> None:
        self._started = True
        self._mode_call("start", now)

    # -- training (code mode) -----------------------------------------------------------

    def _point(self) -> bool:
        return self._hello is not None and self._hello.mode == "point"

    def _start_training(self, now: float) -> None:
        """Record a new template: slots mode on its own layout, point mode on the session's
        "train" layout (the mode waits until the template is ready)."""
        self._training = True
        self._train_cycle = -1
        self._bank.retrain()
        self._log("train_start", {"cycles": self._bank.train_cycles})
        if self._point():
            # the mode's own training patches (BrainPoint: b1, b2 and more in a centred
            # row), else the session's t0, t1, t2
            make = getattr(self._mode, "train_targets", None)
            if callable(make):
                targets = list(make())
            else:
                viewport = None if self._hello is None else self._hello.viewport
                targets = train_layout(viewport, self._bank.lag_frames)
            self._set_layout(P.layout(0, "train", targets), now)
        elif not self._started:
            self._start_mode(now)
        self._update_phase(now)
        self._train_progress(0)
        self._apply_gaze()

    def _train_progress(self, cycle: int) -> None:
        of = self._bank.train_cycles
        msg = P.train_progress(min(max(0, cycle), of), of)
        self._train_cycle = cycle
        with self._lock:
            self._train_msg = msg
            self._transport.send(msg)

    def _train_step(self, now: float) -> None:
        """After a board read in code mode: progress, a failed training, or the template."""
        if not self._training:
            return
        if self._bank.train_error:
            why, self._bank.train_error = self._bank.train_error, ""
            _note(f"training failed, starting again: {why}")
            self._log("train_failed", {"error": why})
            self._refuse("internal", f"Training failed ({why}); keep looking at the cue, it "
                                     f"starts again now.")
            self._train_progress(0)
            return
        if self._bank.trained:
            self._training_done(now)
            return
        cycle, _ = self._bank.progress
        if cycle != self._train_cycle:
            self._train_progress(cycle)

    def _training_done(self, now: float) -> None:
        self._training = False
        self._calibrated_at = now
        msg = P.trained(self._bank.accuracy, self._bank.chance, self._bank.train_cycles)
        with self._lock:
            self._trained_msg = msg
            self._train_msg = None
            self._transport.send(msg)
        self._log("trained", {"accuracy": msg["accuracy"], "chance": msg["chance"],
                              "cycles": msg["cycles"]})
        self._apply_gaze()               # the fake board stops following the cue
        if self._point():
            if self._started:            # retrained after a long pause: scan afresh
                self._mode_call("on_message", {"type": "resume"}, now)
            else:
                self._start_mode(now)
        elif not self._started:
            self._start_mode(now)
        self._update_phase(now)
        # the user still looks at the cue: no pick until the cooldown is over
        self._cool_until = now + P.COOLDOWN_S
        self._since_rearm = None

    def _fake_cycles(self) -> None:
        """The fake board's own cycle times (its EEG follows its own frame grid)."""
        if not self.fake:
            return
        events = self._board.read_events()
        if not self._bank.active:
            return
        for t, kind, _ in events:
            if kind == "cycle":
                self._bank.add_cycle(t)

    def _cue_target(self) -> dict | None:
        """The training cue: the target at lag 0 (else the first one)."""
        if not self._targets:
            return None
        return next((t for t in self._targets if t.get("lag") == 0), self._targets[0])

    # -- mode output --------------------------------------------------------------------

    def _mode_call(self, name: str, *args: Any) -> None:
        mode, now = self._mode, float(args[-1])
        if mode is None:
            return
        try:
            msgs = list(getattr(mode, name)(*args) or [])
            for m in msgs:
                if self._mode is not mode:   # the mode was stopped by an earlier message
                    break
                self._apply(m, now)
        except Exception as exc:
            self._crash(f"in mode.{name}()", exc)
            self._refuse("internal", f"The server's {type(mode).__name__} failed: {exc}")

    def _apply(self, m: Mapping[str, Any], now: float) -> None:
        kind = m.get("type") if isinstance(m, Mapping) else None
        if kind == "layout":
            self._set_layout(m, now)
        elif kind == "phase":
            name = m.get("phase")
            if name == "run":
                self._hold = False
            elif name == "pause":
                self._hold = True
                self._hold_text = str(m.get("text") or HOLD_TEXT)
            else:
                raise ValueError(f"a mode may ask only for phase \"run\" or \"pause\", "
                                 f"not {name!r}")
            self._update_phase(now)
        elif kind == "scan_request":
            if self._ctrl is not None:
                self._transport.send_to(self._ctrl, dict(m))
            self._log(kind, {"scan_id": m.get("scan_id")})
        elif kind == "click":
            self._transport.send(dict(m))
            self._log(kind, dict(m))
        elif isinstance(kind, str):
            self._transport.send(dict(m))
            if kind == "scan_result":
                self._log(kind, {k: m.get(k) for k in ("scan_id", "ok", "ms", "n_elements",
                                                       "n_candidates")})
            elif kind == "error":
                self._log(kind, {"code": m.get("code"), "message": m.get("message")})
        else:
            raise ValueError(f"a mode returned {m!r}, which is not a protocol message")

    def _coded(self, targets: list[dict]) -> list[dict]:
        """Code mode: a target placed on stand-in frequency POINT_KEYS[k] (point mode) gets
        lag k * lag_frames and freq None; one with neither gets its position's lag."""
        lag = self._bank.lag_frames
        out = []
        for i, t in enumerate(targets):
            if t.get("lag") is None:
                f = t.get("freq")
                k = POINT_KEYS.index(f) if f in POINT_KEYS else i
                t = {**t, "freq": None, "lag": k * lag}
            out.append(t)
        return out

    def _set_layout(self, m: Mapping[str, Any], now: float) -> None:
        targets = [dict(t) for t in m.get("targets") or []]
        if self.paradigm == "code":
            targets = self._coded(targets)
        self._forget_moved_gaze(targets)
        self._layout_seq += 1
        msg = {**copy.deepcopy(dict(m)), "targets": copy.deepcopy(targets),
               "layout_id": self._layout_seq}
        with self._lock:
            self._layout = msg
            self._transport.send(msg)
        self._targets = targets
        self._log("layout", {"layout_id": self._layout_seq, "stage": m.get("stage"),
                             "slots": [t.get("slot") for t in targets]})
        if self.phase in P.FLICKER_PHASES:
            self._activate(now)
        else:
            self._deactivate()
        self._apply_gaze()

    # -- phase --------------------------------------------------------------------------

    def _effective(self) -> dict:
        if self._ctrl is None or not self._ready:
            return P.phase("wait", False, text=self._wait_text)
        if self._cal_start is not None:
            return P.phase("calibrate", False, seconds=self.calibrate_s, text=CALIBRATE_TEXT)
        if self._user_paused:
            return P.phase("pause", False, text=PAUSE_TEXT)
        if self._training:
            cue = self._cue_target()
            if cue is None:
                return P.phase("pause", False, text=HOLD_TEXT)
            label = str(cue.get("label") or cue.get("slot"))
            return P.phase("train", True, text=train_text(label), cue=str(cue.get("slot")),
                           cycles=self._bank.train_cycles)
        if self._hold:
            return P.phase("pause", False, text=self._hold_text)
        return P.phase("run", True)

    def _update_phase(self, now: float) -> None:
        if self._closed:
            return
        msg = self._effective()
        with self._lock:
            if msg == self._phase_msg:
                return
            was = self._phase_msg["phase"] in P.FLICKER_PHASES
            self._phase_msg = msg
            self._transport.send(msg)
        self._log("phase", {"phase": msg["phase"], "text": msg["text"]})
        flicker = msg["phase"] in P.FLICKER_PHASES
        if flicker and not was:
            self._activate(now)
        elif was and not flicker:
            self._deactivate()

    def _activate(self, now: float) -> None:
        self._cool_until = None
        self._since_rearm = None
        if not self._targets:
            self._deactivate()
            return
        if self.paradigm == "code":
            self._bank.activate(now)
            if self.fake:
                self._board.start_flicker()
        else:
            try:
                self._bank.activate([float(t["freq"]) for t in self._targets],
                                    [float(t["phase"]) for t in self._targets], now,
                                    positions=self._positions())
            except ValueError as exc:   # a saved decoder model that was not trained for this layout
                self._bank.deactivate()
                self._refuse("layout", f"This layout cannot be decoded: {exc}")
                return
        self._activated_at = now

    def _deactivate(self) -> None:
        self._bank.deactivate()
        if self.paradigm == "code" and self.fake:
            self._board.stop_flicker()

    def _positions(self) -> list[tuple[float, float]] | None:
        """Each target's centre from its rects, as (x right, y up) CSS pixels from the middle
        of the viewport, for the EOG gaze prior; None unless every target has a rect
        (slots mode has none) or the viewport size is unknown."""
        hello = self._hello
        if hello is None or not hello.viewport:
            return None
        w, h = float(hello.viewport["w"]), float(hello.viewport["h"])
        out: list[tuple[float, float]] = []
        for t in self._targets:
            rects = t.get("rects") or []
            if not rects:
                return None
            cx = float(np.mean([float(r["x"]) + float(r["w"]) / 2 for r in rects]))
            cy = float(np.mean([float(r["y"]) + float(r["h"]) / 2 for r in rects]))
            out.append(((cx - 0.5) * w, (0.5 - cy) * h))
        if any(x == 0 and y == 0 for x, y in out):
            return None       # a target at the centre has no gaze direction
        return out

    # -- selections ---------------------------------------------------------------------

    def _decide(self, decisions: list[Decision], now: float) -> None:
        if not decisions or self.phase != "run" or self._layout is None:
            return
        layout_id = self._layout["layout_id"]
        dwell = self._hello is not None and self._hello.mode == "slots"
        for d in decisions:
            if (self._layout is None or self._layout["layout_id"] != layout_id
                    or self.phase != "run"):
                break                     # a selection changed the layout: the rest are stale
            if self.paradigm == "code":
                self._send_decision(d, layout_id)
            fires = d.changed and d.committed != ABSTAIN
            if self._cool_until is not None:
                if d.time < self._cool_until:
                    continue
                self._cool_until = None   # this decision re-arms and is dropped
                self._since_rearm = 0
                self._bank.rearm()
                continue
            if self._since_rearm is not None:
                self._since_rearm += 1
                if self._since_rearm >= self._bank.votes:
                    self._since_rearm = None
                elif fires:
                    continue              # voted before the re-arm
            if not fires:
                continue
            if d.committed not in self._target_slots():
                _note(f"decision named target {d.committed} but the layout has "
                      f"{len(self._targets)}; ignored")
                continue
            why = self._not_streaming()
            if why:
                self._drop_pick(d, layout_id, why)
                continue
            self._select(d, layout_id, now)
            if dwell:
                self._cool_until = d.time + P.COOLDOWN_S

    def _not_streaming(self) -> str:
        """Why the board is not streaming well enough to act on a pick, or "" when it is.

        The board must have delivered samples within STATUS_S (1.0 s) before the read that
        produced the decision, so a backlog that arrives after a stall (a loose USB cable, a
        stalled driver) never selects or clicks anything.
        """
        if self._board_error:
            return self._board_error
        if self._gap_s > STATUS_S:
            return (f"the board delivered no samples for {self._gap_s:.1f} s before this "
                    f"decision")
        return ""

    def _drop_pick(self, d: Decision, layout_id: int, why: str) -> None:
        """Log a commit that was not acted on, and re-arm the voter so a held gaze commits
        again once the board streams."""
        slot = self._target_slots()[d.committed]
        _note(f"dropped a pick of {slot!r}: {why}")
        self._log("selection_dropped", {"layout_id": layout_id, "slot": slot,
                                        "t_decision": d.time, "why": why})
        self._bank.rearm()

    def _target_slots(self) -> dict[int, str]:
        """Decoder target -> slot of the current layout: its position in freq mode, its lag's
        target (lag / lag_frames) in code mode."""
        if self.paradigm != "code":
            return {i: str(t["slot"]) for i, t in enumerate(self._targets)}
        out = {}
        for t in self._targets:
            k = lag_index(t.get("lag"), self._bank.lag_frames, self._bank.n_targets)
            if k is not None:
                out[k] = str(t["slot"])
        return out

    def _scores(self, d: Decision) -> dict[str, float]:
        """The decision's score per slot of the layout (non-finite ones as 0)."""
        values = [float(s) for s in np.asarray(d.scores, dtype=float).reshape(-1)]
        slots = self._target_slots()
        if self.paradigm != "code" and (len(values) != len(slots)
                                        or not all(math.isfinite(v) for v in values)):
            _note(f"decoder scores {values} do not fit the layout; sent as 0")
        out = {}
        for k, slot in sorted(slots.items()):
            v = values[k] if k < len(values) else 0.0
            out[slot] = v if math.isfinite(v) else 0.0
        return out

    def _send_decision(self, d: Decision, layout_id: int) -> None:
        """Code mode: one decision for the confidence bars, whether or not it selects."""
        slots = self._target_slots()
        lead = float(d.gap) if math.isfinite(float(d.gap)) else None
        msg = P.decision(layout_id, self._scores(d), slots.get(int(d.raw)),
                         slots.get(int(d.committed)), lead, bool(d.changed))
        self._transport.send(msg)

    def _select(self, d: Decision, layout_id: int, now: float) -> None:
        scores = self._scores(d)
        slot = self._target_slots()[d.committed]
        self._selection_seq += 1
        msg = P.selection(self._selection_seq, layout_id, slot, now, scores[slot], scores)
        self._transport.send(msg)
        self._log("selection", {"selection_id": self._selection_seq, "layout_id": layout_id,
                                "slot": slot, "t_decision": d.time, "score": scores[slot],
                                "gate": getattr(self._bank, "gate", None)})
        # before the mode answers: on_selection may replace the layout and the gate at once.
        # Code mode has no frequencies or trained classifier to teach (its own template).
        if self.paradigm != "code":
            self._learn_from(self._targets[d.committed], self._bank.gate, d.time, layout_id)
        self._mode_call("on_selection", slot, now)

    def _learn_from(self, target: Mapping[str, Any], gate: float, t_commit: float,
                    layout_id: int) -> None:
        """A confirmed pick is a labelled trial: teach it to the trained classifier.

        A selection of a ``button`` is remembered with its gate (the moment its templates
        start) and its commit time (the user was certainly looking at it until then); the
        following ``confirm`` selection says the pick was right, so the bank learns that
        window (``DecoderBank.learn``). ``back`` forgets it. Does nothing without a trained
        classifier. Called with the layout the selection was made in, before the mode acts.
        """
        kind = target.get("kind")
        if kind == "button":
            self._last_pick = (float(target["freq"]), float(gate), float(t_commit), layout_id)
            return
        if kind == "confirm" and self._last_pick is not None and self._bank.trained is not None:
            freq, start, end, _ = self._last_pick
            try:
                learned = self._bank.learn(freq, start, end)
            except ValueError as exc:
                _note(f"learn from the confirmed pick: {exc}")
                learned = False
            self._log("learned", {"freq": freq, "t_start": start, "t_end": end, "ok": learned,
                                  "n_train": [int(n) for n in self._bank.trained.n_train]})
        if kind in ("confirm", "back"):
            self._last_pick = None

    # -- fake gaze ----------------------------------------------------------------------

    def _forget_moved_gaze(self, targets: Sequence[Mapping[str, Any]]) -> None:
        """Drop the fake gaze when the new layout no longer shows its target in the same place.

        A gaze names a slot, and point mode reuses the slot names ("b1" is always the top
        candidate of the page shown), so a gaze kept across layouts would make the fake board
        pick whatever element now holds that slot, wherever it is. The gaze survives only
        when the slot keeps its kind, label and rects (slots mode, or "more" while paging),
        or when the old layout did not show the slot at all (a gaze sent before the layout).
        The client sends a fresh gaze when its pointer is over a patch of the new layout.
        """
        if self._gaze is None:
            return
        old = next((t for t in self._targets if t.get("slot") == self._gaze), None)
        if old is None:
            return
        new = next((t for t in targets if t.get("slot") == self._gaze), None)
        keys = ("kind", "label", "rects")
        if new is None or any(old.get(k) != new.get(k) for k in keys):
            self._gaze = None

    def _apply_gaze(self) -> None:
        if not self.fake:
            return
        target = -1
        if self.paradigm == "code":
            if self._training:
                target = 0               # the fake user follows the cue (lag 0)
            elif self._gaze is not None:
                lag = next((t.get("lag") for t in self._targets
                            if t.get("slot") == self._gaze), None)
                k = None if lag is None else lag_index(lag, self._bank.lag_frames,
                                                       self._bank.n_targets)
                target = -1 if k is None else k
            self._board.set_target(target)
            return
        if self._cal_start is None and self._gaze is not None:
            freq = next((float(t["freq"]) for t in self._targets
                         if t.get("slot") == self._gaze), None)
            if freq is not None:
                hits = np.flatnonzero(np.isclose(np.asarray(self._board.freqs, dtype=float),
                                                 freq))
                target = int(hits[0]) if hits.size else -1
        self._board.set_target(target)

    # -- board and status ---------------------------------------------------------------

    def _read(self, now: float) -> tuple[np.ndarray, np.ndarray]:
        n_ch = len(self._bank.ch_names)
        try:
            X, t = self._board.read()
        except Exception as exc:
            text = f"the board failed: {exc}"
            if text != self._board_error:
                _note(text)
                self._board_error = text
            self._board_state = "error"
            return np.empty((n_ch, 0)), np.empty(0)
        X = np.asarray(X, dtype=float)
        t = np.asarray(t, dtype=float).reshape(-1)
        self._board_error = ""
        if X.ndim == 2 and X.shape[1]:
            self._gap_s = now - self._last_sample
            self._last_sample = now
            if self._posterior:
                rows = X[self._posterior]
                self._acc_n += rows.shape[1]
                self._acc_sum += rows.sum(axis=1)
                self._acc_sq += (rows ** 2).sum(axis=1)
        return X, t

    def _maybe_status(self, now: float) -> None:
        if self._board_error:
            state, text = "error", self._board_error
        else:
            state = "streaming" if now - self._last_sample <= STATUS_S else "no-data"
            text = ""
            if state == "streaming" and self._acc_n > 1:
                mean = self._acc_sum / self._acc_n
                var = np.maximum(self._acc_sq / self._acc_n - mean ** 2, 0.0)
                text = f"posterior RMS {float(np.sqrt(var.mean())):.0f} uV"
        changed = state != self._board_state
        if self._frames_text and state != "error":
            text = self._frames_text
        if not changed and self._frames_text == self._status_text and \
                now - self._status_at < STATUS_S:
            return
        self._status_text = self._frames_text
        self._board_state = state
        self._status_at = now
        self._acc_n = 0
        self._acc_sum[:] = 0.0
        self._acc_sq[:] = 0.0
        msg = P.status(state, state == "streaming" and not self._frames_text, text)
        with self._lock:
            self._status_msg = msg
            self._transport.send(msg)
        if changed:
            self._log("board", {"state": state, "text": text})

    # -- notes and logs -----------------------------------------------------------------

    def _crash(self, where: str, exc: BaseException) -> None:
        text = f"{where}: {type(exc).__name__}: {exc}"
        if text != self._last_crash:
            _note(text)
            self._last_crash = text
        self._log("internal", {"where": where, "error": f"{type(exc).__name__}: {exc}"})

    def _log(self, kind: str, fields: dict) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(kind, {"t": float(self._clock()), **fields})
        except Exception as exc:   # logging must never stop the session
            _note(f"the log hook failed: {exc}")
