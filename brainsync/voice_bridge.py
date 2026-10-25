"""Protocol-2 bridge for BCI Voice: the EEG board, the decoder and the PsychoPy display, driven by
app.server over /ws/decoder (INTERFACE-v2-draft.md).

    python -m brainsync.voice_bridge                          # Cerelog X8 + full-screen display
    python -m brainsync.voice_bridge --fake                   # synthetic board, real display
    python -m brainsync.voice_bridge --fake --fast --no-stimulus   # no hardware, no window
    python -m app.server --bridge fake --bridge-headless      # the server starts it for you
    python -m brainsync.voice_bridge --paradigm code [--template FILE] [--fake]   # c-VEP, phone

Startup: connect the board once (about 16 s for the Cerelog X8), start the display
(``python -m brainsync.stimulus -c four_diamond --free --feedback stdin --hold-still --markers
unix``) and wait for its refresh line; every launch writes ``{"flicker": false}`` as its first
stdin line, so a later integer line (a highlight) can never end --hold-still by itself. Then
connect to the server and send ``hello`` with the slots (s1..s4 in config order), waveform and
refresh rate; on a connection made while the display is down, hello waits until it is back.
Lines to the display go through a writer thread, so a display that stops reading its stdin
never blocks the bridge. Messages from the server:

  hello_ack   ok: its labels go on the display; error: log it and send nothing until a new hello
  baseline    hold the flicker off and record the decoder's rest baseline; baseline_done comes
              no earlier than the seconds asked for ("interrupted" if a pause cuts it short;
              "not_ready" / "display_down" if it cannot start), and only on the connection
              that asked
  display     labels / text / highlight -> the display's stdin (never during a trial)
  stim_start  {"active": [...]} and {"flicker": true} to the display, the decoder starts fresh;
              stim_onset from the display's flicker_on marker; the first commit among the
              active slots -> selection (at most once, never before the onset). A trial it
              cannot carry out (not acked, no baseline yet, a baseline running, after a pause,
              display down) -> no_selection "not_ready"
  stim_stop   flicker off, decoding off
  pause       flicker off at once, decoding off until the next baseline

The flicker also goes off at once when the server connection drops (the websocket keepalive
notices a server that stops answering within about 15 s), and stays off until a new hello_ack
and stim_start; a trial the server has not stopped after --max-flicker-s (default 20 s) turns
it off by itself. Esc on the display closes it (the flicker stops there and then): the bridge
reports display_ok false, relaunches it, and reports display_ok true once its refresh line is
back (the server waits for the partner's Resume); a new hello goes out only if the relaunched
display measured a different refresh (or none went out on this connection yet). Close code
4000 (a newer bridge replaced this one) stops reconnecting and exits 0; otherwise it
reconnects every --reconnect-s seconds. When the bridge stops, the display is closed (EOF on
its stdin, then terminate and kill if it lingers) and never relaunched.

--paradigm code (INTERFACE-v2-draft.md section 9, ``-c cvep_2`` by default) is c-VEP for the
iPhone app, which draws the code itself: no display process at all (--no-stimulus is implied).
The hello carries ``paradigm: code``, the m-sequence's bits and each slot's ``lag_frames``;
``baseline`` needs no recording (still answered after its seconds); ``stim_markers`` (the
phone's cycle onsets, relayed by the server) drive ``brainsync.vep.live.CodeEngine``, whose
first commit among the active slots is the ``selection``, never before the trial's first
marker; no ``stim_onset``. ``train`` fits the template from that trial's cycles and answers
``train_done`` with its leave-one-out accuracy (the template is saved as
``<stamp>_cvep_template.npz``; --template FILE loads one instead). Without a template a trial
that is not a training trial answers ``no_selection`` "not_trained" at its first marker.
``status`` carries ``trained``. --fake there is ``vep.live.MarkerFakeBoard``: its EEG carries
a random active target's code response (a training trial's cued one) from the trial's first
marker on; it hands out samples 2 s late, so markers that come late still line up.

--fake uses FakeBoard; during a trial it carries the tone of a random active target, so trials
end in a selection. --fast runs it in virtual time, FAST_SPEED times faster than real time.
--no-stimulus replaces the display with a headless stand-in: no window, the onset is the time
the flicker would start, the refresh comes from the config.

The board is read on its own thread, which also runs the decoder; the asyncio loop only
handles messages. Exit code 0 when stopped (Ctrl-C, SIGTERM, close code 4000, the server that
started it is gone, or --no-reconnect after a drop), 1 when the board or display fails at
startup or --no-reconnect never reached the server, 2 for a bad argument or config.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import queue
import random
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .board import CerelogBoard, FakeBoard
from .classify import ABSTAIN, make
from .demo import _channels, _names
from .launch import Stimulus, launch_stimulus_live
from .online import TUNED, Decoder, Voter, fit_rest, threshold_arg
from .recordings import MONTAGE, POSTERIOR
from .stimulus import StimulusConfig, load_config
from .vep.live import CodeEngine, MarkerFakeBoard, code_hello, load_template

log = logging.getLogger("voice_bridge")

PROTOCOL = 2
DEFAULT_URL = "ws://127.0.0.1:8000/ws/decoder"
DEFAULT_CONFIG = "four_diamond"
DEFAULT_CONFIGS = {"freq": DEFAULT_CONFIG, "code": "cvep_2"}   # -c per --paradigm
REPLACED = 4000          # close code from app/server.py: a newer bridge took over
FLICKER_ON = '{"flicker": true}'
FLICKER_OFF = '{"flicker": false}'
FAST_SPEED = 10.0        # --fast: virtual seconds of fake data per real second
POLL_S = 0.05            # board read interval
STALL_S = 5.0            # no samples for this long: board_ok false
TICK_S = 0.1             # how often health is checked (status on every change)
RELAUNCH_S = 2.0         # wait between display relaunch attempts
REFRESH_CHANGE_HZ = 0.5  # a relaunched display this far off the hello's refresh -> new hello
MIN_BASELINE_STEPS = 8   # a baseline records at least window + pad + this many steps of data
PARENT_CHECK_S = 1.0     # how often --parent-pid is checked
ONSET_SLACK_S = 0.05     # a flicker_on marker this much older than its stim_start still counts
WRITE_QUEUE_LINES = 512  # display stdin lines queued unwritten before it counts as not reading
CLOSE_EOF_S = 2.0        # closing the display: wait this long after EOF on its stdin,
CLOSE_TERM_S = 1.0       # then this long after terminate, then kill
MAX_FLICKER_S = 20.0     # --max-flicker-s default: 2 x the server's max_trial_s
PING_INTERVAL_S = 5.0    # websocket keepalive: a server that stops answering is dropped
PING_TIMEOUT_S = 10.0    # within PING_INTERVAL_S + PING_TIMEOUT_S (rule 3: flicker off)
BUILD_ERRORS = (ValueError, KeyError, FileNotFoundError, ImportError)

_MARKER = re.compile(r"^\[marker\s+([-+0-9.eE]+)\]\s+(\S+)")
_MEASURED = re.compile(r"measured refresh:\s*([0-9.]+)\s*Hz")
_USING = re.compile(r"using refresh\s+([0-9.]+)\s*Hz")


# ---------------------------------------------------------------------------
# Pure helpers: slot ids, display commands, the display's output
# ---------------------------------------------------------------------------


def slot_ids(n: int) -> list[str]:
    """Slot ids for ``n`` targets in config order: s1 is target 0, s2 target 1, ..."""
    return [f"s{i + 1}" for i in range(n)]


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False)


def active_line(targets: Sequence[int]) -> str:
    """``{"active": [0, 3]}``: which targets flicker on the next flicker-on."""
    return _line({"active": [int(k) for k in targets]})


def display_lines(msg: dict[str, Any], slots: Sequence[str],
                  labels: list[str]) -> tuple[list[str], list[str]]:
    """The display's stdin lines for one ``display`` message, and the labels after it.

    ``labels`` holds the sentence beside each target (by index); the message's labels dict
    updates the slots it names (unknown slots are ignored) and the whole list is sent. ``text``
    becomes one centre line, ``highlight`` the cue box's target index (-1 for null). A field
    left out sends nothing: it stays as it was.
    """
    lines: list[str] = []
    new = list(labels)
    given = msg.get("labels")
    if isinstance(given, dict):
        known = 0
        for slot, text in given.items():
            if slot in slots and isinstance(text, str):
                new[list(slots).index(slot)] = text
                known += 1
            else:
                log.warning("display: ignored label for unknown slot %r", slot)
        if known:
            lines.append(_line({"labels": new}))
    if isinstance(msg.get("text"), str):
        lines.append(_line({"text": msg["text"]}))
    if "highlight" in msg:
        slot = msg["highlight"]
        if slot is None:
            lines.append("-1")
        elif slot in slots:
            lines.append(str(list(slots).index(slot)))
        else:
            log.warning("display: ignored highlight on unknown slot %r", slot)
    return lines, new


def parse_marker(line: str) -> tuple[float, str] | None:
    """``[marker <unix seconds>] <name>`` -> (seconds, name), else None."""
    m = _MARKER.match(line.strip())
    if m is None:
        return None
    try:
        return float(m.group(1)), m.group(2)
    except ValueError:
        return None


def parse_refresh(line: str) -> tuple[str, float] | None:
    """("measured", Hz) for "measured refresh: <x> Hz", ("using", Hz) for "using refresh <x> Hz"."""
    for kind, pattern in (("measured", _MEASURED), ("using", _USING)):
        m = pattern.search(line)
        if m is not None:
            return kind, float(m.group(1))
    return None


def hello_message(cfg: StimulusConfig, refresh_hz: float, name: str,
                  decoder: dict[str, Any] | None, score_kind: str | None) -> dict[str, Any]:
    """The protocol-2 hello for ``cfg``'s targets (s1..sN in config order)."""
    slots = [{"slot": slot, "freq_hz": float(f), "phase_deg": round(math.degrees(float(p)), 6)}
             for slot, f, p in zip(slot_ids(cfg.n_targets), cfg.freqs, cfg.phases)]
    msg: dict[str, Any] = {"type": "hello", "protocol": PROTOCOL, "name": name,
                           "refresh_hz": round(float(refresh_hz), 3), "waveform": cfg.waveform,
                           "slots": slots}
    if decoder is not None:
        msg["decoder"] = decoder
    if score_kind is not None:
        msg["score_kind"] = score_kind
    return msg


# ---------------------------------------------------------------------------
# Displays: the PsychoPy process, or a headless stand-in
# ---------------------------------------------------------------------------

EventSink = Callable[..., None]   # on_event("marker", t, name) / on_event("exit", code)


class HeadlessDisplay:
    """No window: records every stdin line in ``lines``; a flicker-on is "seen" at once.

    Like PsychoPyDisplay, every start writes ``{"flicker": false}`` first, and ``close`` is
    final. ``auto_onset`` False holds the flicker_on marker until ``fire("flicker_on")``
    (tests). ``simulate_exit()`` behaves like Esc on a real display.
    """

    def __init__(self, refresh_hz: float, auto_onset: bool = True) -> None:
        self.refresh = float(refresh_hz)
        self.auto_onset = auto_onset
        self.lines: list[str] = []
        self.starts = 0
        self._alive = False
        self._closing = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: EventSink | None = None

    def attach(self, loop: asyncio.AbstractEventLoop, on_event: EventSink) -> None:
        self._loop, self._on_event = loop, on_event

    def start(self) -> float:
        """'Launch' (blocking call, run on a thread); returns the refresh rate."""
        if self._closing:
            raise RuntimeError("the display is closing")
        self.starts += 1
        self._alive = True
        self.lines.append(FLICKER_OFF)   # the first line of every launch
        return self.refresh

    @property
    def alive(self) -> bool:
        return self._alive

    def send(self, line: str) -> bool:
        if not self._alive:
            return False
        self.lines.append(line)
        if line == FLICKER_ON and self.auto_onset:
            self.fire("flicker_on")
        elif line == FLICKER_OFF:
            self.fire("flicker_off")
        return True

    def fire(self, name: str) -> None:
        """Report a marker stamped now, as the display would at a flip."""
        self._post("marker", time.time(), name)

    def simulate_exit(self, code: int = 0) -> None:
        self._alive = False
        self._post("exit", code)

    def _post(self, *event: Any) -> None:
        if self._loop is not None and self._on_event is not None:
            with contextlib.suppress(RuntimeError):   # the loop is closing
                self._loop.call_soon_threadsafe(self._on_event, *event)

    def close(self) -> None:
        self._closing = True
        self._alive = False


class PhoneDisplay(HeadlessDisplay):
    """--paradigm code: the phone draws the code, so there is nothing to drive here. Lines
    are dropped (not kept), no marker is ever reported (no stim_onset: the onset is the
    trial's first cycle marker), and it is up from the start at the config's refresh."""

    def send(self, line: str) -> bool:
        return self.alive


class LineWriter:
    """Writes lines to a display's stdin on its own thread, so the event loop never blocks.

    A display that does not read its stdin (stuck, or before its reader started) would
    otherwise fill the pipe and freeze the bridge's loop on a plain write: no status, no
    pause, no reply to the server's pings. ``put`` only queues; it is False once the queue
    holds ``max_lines`` unwritten lines or the pipe broke, which the bridge treats as the
    display not taking commands. ``close`` closes the pipe (EOF ends a free run) after the
    lines already queued.
    """

    _END = object()

    def __init__(self, pipe: Any, max_lines: int = WRITE_QUEUE_LINES) -> None:
        self.pipe = pipe
        self.broken = False
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_lines)
        self._thread = threading.Thread(target=self._run, name="display-stdin", daemon=True)
        self._thread.start()

    def put(self, line: str) -> bool:
        if self.broken:
            return False
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            return False
        return True

    def close(self) -> None:
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(self._END)

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            while (line := self._queue.get()) is not self._END:
                self.pipe.write(f"{line}\n")
                self.pipe.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            self.broken = True
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                self.pipe.close()


class PsychoPyDisplay:
    """``python -m brainsync.stimulus <args>`` with feedback on its stdin; its output is read
    live, so flicker_on / flicker_off markers and its exit reach the bridge at once.

    Every launch writes ``{"flicker": false}`` to the new process before ``send`` can reach
    it, so no integer line (a highlight) is ever its first line. ``close`` is final: it ends
    the running display, a launch still in progress, and any later ``start``.
    """

    def __init__(self, stim_args: Sequence[str], fallback_refresh: float,
                 timeout: float = 60.0, command: Sequence[str] | None = None) -> None:
        self.args = list(stim_args)
        self.fallback_refresh = float(fallback_refresh)
        self.timeout = timeout
        self.command = command
        self.stim: Stimulus | None = None
        self._writer: LineWriter | None = None
        self._gen = 0
        self._lock = threading.Lock()
        self._closing = False
        self._spawned: Any = None   # the process of a launch still waiting for its refresh line
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: EventSink | None = None

    def attach(self, loop: asyncio.AbstractEventLoop, on_event: EventSink) -> None:
        self._loop, self._on_event = loop, on_event

    def start(self) -> float:
        """Launch the display (blocking until its refresh check passed); its refresh rate.

        Raises RuntimeError if it exits or does not come up within the timeout, or if
        ``close`` was called (before or during the launch).
        """
        with self._lock:
            if self._closing:
                raise RuntimeError("the display is closing")
            old, self.stim, writer, self._writer = self.stim, None, self._writer, None
            self._gen += 1
            gen = self._gen
        _end_display(old, writer)
        rates: dict[str, float] = {}

        def on_line(line: str) -> None:
            marker = parse_marker(line)
            if marker is not None:
                self._post(gen, "marker", *marker)
                return
            rate = parse_refresh(line)
            if rate is not None:
                rates[rate[0]] = rate[1]
            if line.strip():
                log.info("display: %s", line.strip())

        def on_spawn(proc: Any) -> None:
            with self._lock:
                self._spawned = proc
                closing = self._closing
            if closing:   # close() came first: never let this window come up
                proc.kill()

        try:
            stim = launch_stimulus_live(self.args, on_line, timeout=self.timeout,
                                        command=self.command, on_spawn=on_spawn,
                                        on_end=lambda code: self._post(gen, "exit", code))
        finally:
            with self._lock:
                self._spawned = None
        writer = LineWriter(stim.proc.stdin)
        writer.put(FLICKER_OFF)   # the first line, before send() can reach this process
        with self._lock:
            doomed = self._closing
            if not doomed:
                self.stim, self._writer = stim, writer
        if doomed:
            _end_display(stim, writer)
            raise RuntimeError("the display is closing")
        # The rate its frame counts use: "using refresh" (--refresh auto), else the measured one.
        return rates.get("using", rates.get("measured", self.fallback_refresh))

    @property
    def alive(self) -> bool:
        stim = self.stim
        return stim is not None and stim.proc.poll() is None

    def send(self, line: str) -> bool:
        """Queue one stdin line; False when the display is gone or not taking lines."""
        writer = self._writer
        return self.alive and writer is not None and writer.put(line)

    def _post(self, gen: int, *event: Any) -> None:
        def deliver() -> None:
            if gen == self._gen and self._on_event is not None:   # not an older process
                self._on_event(*event)

        if self._loop is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(deliver)

    def close(self) -> None:
        """End the display for good: EOF on its stdin, then terminate and kill if it lingers
        (on its start screen, or hung). Its exit is not reported as the display going down."""
        with self._lock:
            self._closing = True
            self._gen += 1
            stim, self.stim, writer, self._writer = self.stim, None, self._writer, None
            spawned = self._spawned
        if spawned is not None and spawned.poll() is None:
            spawned.kill()   # a launch in progress: start() then raises
        _end_display(stim, writer)


def _end_display(stim: Stimulus | None, writer: LineWriter | None) -> None:
    """EOF on the display's stdin (a free run ends), then terminate, then kill: at most about
    CLOSE_EOF_S + CLOSE_TERM_S seconds."""
    if stim is None:
        return
    if writer is not None:
        writer.close()
    else:
        with contextlib.suppress(BrokenPipeError, OSError, ValueError, AttributeError):
            stim.proc.stdin.close()
    proc = stim.proc
    try:
        code = proc.wait(timeout=CLOSE_EOF_S)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            code = proc.wait(timeout=CLOSE_TERM_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
    log.info("display closed (exit code %s; log %s)", code, stim.log)


# ---------------------------------------------------------------------------
# The board thread: reads the board and runs the decoder
# ---------------------------------------------------------------------------


class BoardEngine:
    """Reads ``board`` on its own thread and feeds ``decoder`` while a trial runs.

    The asyncio side only queues commands (start_baseline, start_trial, stop), applied at the
    thread's next read; results come back through ``on_baseline(baseline_id, ok, message)`` and
    ``on_commit(trial_id, target, scores)`` on the event loop. A trial votes only among its
    active targets (the classifier's abstain rules applied to their scores alone) and stops
    decoding after its first commit. With a FakeBoard the tone follows a random active target
    during a trial and is off otherwise.
    """

    def __init__(self, board: Any, decoder: Decoder, *, fake: bool = False, fast: bool = False,
                 speed: float = FAST_SPEED, seed: int | None = None, poll_s: float = POLL_S,
                 stall_s: float = STALL_S) -> None:
        self.board = board
        self.decoder = decoder
        self.fake = fake
        self.fast = fast
        self.speed = speed
        self.poll_s = poll_s
        self.stall_s = stall_s
        self._rng = random.Random(seed)
        self._commands: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._heard = time.monotonic()
        self._error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_commit: Callable[..., None] | None = None
        self._on_baseline: Callable[..., None] | None = None
        # thread-side state
        self._mode = "idle"
        self._trial_id: int | None = None
        self._targets: list[int] = []
        self._t_start = 0.0
        self._voter: Voter | None = None
        self._baseline_id: int | None = None
        self._need = 0
        self._chunks: list[np.ndarray] = []
        self._have = 0
        if self.fake:
            board.set_target(ABSTAIN)

    @property
    def decoder_params(self) -> dict[str, Any]:
        d = self.decoder
        return {"window_s": d.window_s, "step_s": d.step_s, "votes": d.votes}

    @property
    def score_kind(self) -> str:
        return f"{self.decoder.classifier.name}_minus_baseline"

    def attach(self, loop: asyncio.AbstractEventLoop, on_commit: Callable[..., None],
               on_baseline: Callable[..., None]) -> None:
        self._loop, self._on_commit, self._on_baseline = loop, on_commit, on_baseline

    def start(self) -> None:
        if self._thread is None:
            self._heard = time.monotonic()
            self._thread = threading.Thread(target=self._run, name="board", daemon=True)
            self._thread.start()

    def min_baseline_s(self) -> float:
        d = self.decoder
        return (d.needed + MIN_BASELINE_STEPS * d.n_step) / d.fs

    # -- commands from the event loop ---------------------------------------

    def start_baseline(self, baseline_id: int, seconds: float) -> None:
        self._commands.put(("baseline", baseline_id, seconds))

    def start_trial(self, trial_id: int, targets: list[int], t_start: float) -> None:
        self._commands.put(("trial", trial_id, list(targets), t_start))

    def stop(self) -> None:
        self._commands.put(("stop",))

    def board_ok(self) -> bool:
        alive = self._thread is not None and self._thread.is_alive()
        return alive and self._error is None and time.monotonic() - self._heard < self.stall_s

    @property
    def message(self) -> str:
        if self._error is not None:
            return f"board error: {self._error}"
        silent = time.monotonic() - self._heard
        if silent >= self.stall_s:
            return f"board sent nothing for {silent:.0f} s"
        return f"{'fake board' if self.fake else 'Cerelog X8'} at {self.board.fs:g} Hz"

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # -- the thread ------------------------------------------------------------

    def _post(self, fn: Callable[..., None] | None, *data: Any) -> None:
        if self._loop is not None and fn is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(fn, *data)

    def _apply(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            if cmd[0] == "trial":
                _, self._trial_id, self._targets, self._t_start = cmd
                self.decoder.reset()
                self._voter = Voter(self.decoder.votes)
                self._mode = "trial"
                self._tone(self._rng.choice(self._targets))
            elif cmd[0] == "baseline":
                _, self._baseline_id, seconds = cmd
                self._need = int(round(max(seconds, self.min_baseline_s()) * self.decoder.fs))
                self._chunks, self._have = [], 0
                self._mode = "baseline"
                self._tone(ABSTAIN)
            else:
                self._mode = "idle"
                self._tone(ABSTAIN)

    def _tone(self, target: int) -> None:
        if self.fake:
            self.board.set_target(target)

    def _run(self) -> None:
        while not self._closed.is_set():
            self._apply()
            try:
                if self.fast:
                    self.board.advance(self.poll_s * self.speed)
                time.sleep(self.poll_s)
                X, t = self.board.read()
                self._error = None
            except Exception as exc:   # a driver error: report it, keep trying
                self._error = str(exc) or type(exc).__name__
                time.sleep(self.poll_s)
                continue
            if X.shape[1] == 0:
                continue
            self._heard = time.monotonic()
            self._apply()   # a command that came during the read applies to these samples
            try:
                if self._mode == "baseline":
                    self._baseline_data(X)
                elif self._mode == "trial":
                    self._trial_data(X, t)
            except Exception as exc:   # never let a decoder error kill the board thread
                log.exception("decoder error")
                self._error = f"decoder: {exc}"
                self._mode = "idle"

    def _baseline_data(self, X: np.ndarray) -> None:
        self._chunks.append(X)
        self._have += X.shape[1]
        if self._have < self._need:
            return
        data = np.concatenate(self._chunks, axis=1)[:, :self._need]
        self._chunks, self._mode = [], "idle"
        try:
            cal = fit_rest(self.decoder, self.decoder.windows(data))
        except ValueError as exc:
            self._post(self._on_baseline, self._baseline_id, False, str(exc))
            return
        log.info("baseline from %d still windows (%d rejected), spread %.4f",
                 cal.n_windows, cal.n_rejected, cal.spread)
        self._post(self._on_baseline, self._baseline_id, True, "")

    def _trial_data(self, X: np.ndarray, t: np.ndarray) -> None:
        keep = t >= self._t_start   # drop samples read before stim_start
        if not keep.all():
            X, t = X[:, keep], t[keep]
        if X.shape[1] == 0:
            return
        clf = self.decoder.classifier
        idx = self._targets
        for d in self.decoder.feed(X, t):
            sub = np.asarray(d.scores, dtype=float)[idx]
            pick = int(clf.predict_scores(sub[None])[0])
            raw = idx[pick] if pick != ABSTAIN else ABSTAIN
            assert self._voter is not None
            if self._voter.push(raw) and self._voter.committed != ABSTAIN:
                self._post(self._on_commit, self._trial_id, self._voter.committed,
                           np.asarray(d.scores, dtype=float).copy())
                self._mode = "idle"   # one answer per trial: stop decoding
                return


# ---------------------------------------------------------------------------
# The bridge: protocol 2 over the websocket
# ---------------------------------------------------------------------------


@dataclass
class Trial:
    trial_id: int
    active: list[str]
    sent_at: float
    onset: float | None = None
    answered: bool = False
    pending: dict[str, Any] | None = None   # a selection waiting for its onset
    training: int | None = None   # paradigm code: the train_id this trial collects for


@dataclass
class BaselineRun:
    baseline_id: int
    seconds: float
    result: asyncio.Future
    outbox: Any = None   # the connection that asked: its answer goes there or nowhere
    task: asyncio.Task | None = None
    interrupted: bool = False


class Bridge:
    """One bridge: create it with parsed args, the stimulus config, an engine and a display,
    then ``await run()`` (returns the exit code)."""

    def __init__(self, args: argparse.Namespace, cfg: StimulusConfig, engine: Any,
                 display: Any) -> None:
        self.args = args
        self.cfg = cfg
        self.engine = engine
        self.display = display
        self.slots = slot_ids(cfg.n_targets)
        self.code = getattr(args, "paradigm", "freq") == "code"
        self.trains: set[int] = set()   # paradigm code: train_ids still to answer here
        self.name = bridge_name(args)
        self.labels = [""] * cfg.n_targets
        self.text = ""
        self.highlight = -1
        self.refresh: float | None = None    # the running display's, None while it is down
        self.hello_refresh: float | None = None
        self.display_ok = False
        self.flickering = False
        self.finished = False
        self.replaced = False
        self.ws: Any = None
        self.outbox: asyncio.Queue[str] | None = None
        self.acked = False
        self.hello_sent = False    # on this connection (rule 9: not while the display is down)
        self.connected_once = False
        self.rejected = False      # the server answered hello with error
        self.ready = False         # a baseline finished since the last hello / pause
        self.trial: Trial | None = None
        self.baseline: BaselineRun | None = None
        self.relaunching: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._answers: set[asyncio.Task] = set()   # baseline_done replies still to come
        self._flicker_timer: asyncio.TimerHandle | None = None
        self._last_status: dict[str, Any] = {}
        self._last_status_at = 0.0

    # -- startup and connection --------------------------------------------------

    async def run(self) -> int:
        """The exit code: 0 when stopped, 1 when --no-reconnect ends a run that never reached
        the server. A display that does not come up at startup raises RuntimeError."""
        self.loop = asyncio.get_running_loop()
        if self.code:
            self.engine.attach(self.loop, self._on_commit, self._on_baseline, self._on_trained)
        else:
            self.engine.attach(self.loop, self._on_commit, self._on_baseline)
        self.display.attach(self.loop, self._on_display_event)
        self.engine.start()
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        code = 0
        watcher: asyncio.Task | None = None
        ticker: asyncio.Task | None = None
        try:
            # Rule 9: hello only once the board streams and the display's refresh check passed.
            self._display_up(await asyncio.to_thread(self.display.start))
            watcher = (asyncio.create_task(self._watch_parent(self.args.parent_pid))
                       if self.args.parent_pid else None)
            ticker = asyncio.create_task(self._tick())
            while not self.finished:
                try:
                    async with connect(self.args.url, ping_interval=PING_INTERVAL_S,
                                       ping_timeout=PING_TIMEOUT_S) as ws:
                        log.info("connected to %s", self.args.url)
                        await self._session(ws)
                except (OSError, ConnectionClosed, InvalidHandshake, TimeoutError) as exc:
                    log.warning("connection problem: %s", exc)
                if self.replaced:
                    log.warning("replaced by a newer bridge (close code %d): stopping", REPLACED)
                    break
                if self.finished or not self.args.reconnect:
                    break
                await asyncio.sleep(self.args.reconnect_s)
            if not (self.connected_once or self.finished or self.replaced):
                log.error("never reached the server at %s (--no-reconnect)", self.args.url)
                code = 1
        finally:
            # Stopping (also on Ctrl-C, which cancels this task): from here on a display that
            # exits is not relaunched, and a relaunch in progress closes what it launched.
            self.finished = True
            for task in (watcher, ticker, self.relaunching):
                if task is not None:
                    task.cancel()
            self._lost_server()
        return code

    def stop(self) -> None:
        """End the run: close the connection and do not reconnect."""
        self.finished = True
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close())

    async def _watch_parent(self, pid: int) -> None:
        """Stop once the process that started this one is gone (its children are re-parented)."""
        while os.getppid() == pid:
            await asyncio.sleep(PARENT_CHECK_S)
        log.info("parent process %d is gone: stopping", pid)
        self.finished = True
        if self.ws is not None:
            await self.ws.close()

    async def _session(self, ws: Any) -> None:
        self.ws = ws
        self.outbox = asyncio.Queue()
        self.hello_sent = False
        self.connected_once = True
        writer = asyncio.create_task(self._write(ws, self.outbox))
        if self.display_ok:
            self._send_hello()
        else:   # rule 9: no hello while the display is down; _relaunch sends it once it is up
            log.warning("connected while the display is down: hello once it is back")
        self._send_status()
        try:
            async for raw in ws:
                self._handle(raw)
        finally:
            self.replaced = ws.close_code == REPLACED
            self.ws = None
            self.outbox = None
            self._lost_server()
            writer.cancel()

    async def _write(self, ws: Any, outbox: asyncio.Queue[str]) -> None:
        from websockets.exceptions import ConnectionClosed

        with contextlib.suppress(ConnectionClosed):
            while True:
                await ws.send(await outbox.get())

    def _emit(self, msg: dict[str, Any]) -> None:
        """Queue one message for the server; after an error only a new hello goes out."""
        if self.outbox is None or (self.rejected and msg["type"] != "hello"):
            return
        self.outbox.put_nowait(json.dumps(msg))

    def _send_hello(self) -> None:
        self.acked = self.rejected = self.ready = False
        self.hello_sent = self.outbox is not None
        self.hello_refresh = self.refresh if self.refresh is not None else self.cfg.refresh
        if self.code:
            self._emit(code_hello(self.cfg, self.name, self.engine.decoder_params,
                                  self.engine.score_kind))
            return
        self._emit(hello_message(self.cfg, self.hello_refresh, self.name,
                                 self.engine.decoder_params, self.engine.score_kind))

    def _lost_server(self) -> None:
        """The connection is gone (or the bridge stops): flicker off at once (rule 3)."""
        self._flicker_off(force=True)
        self._end_trial(send_off=False)
        self._interrupt_baseline(cancel=True)
        self._drop_answers()
        self.trains.clear()   # a train_done never goes to a later connection
        self.acked = self.ready = self.rejected = self.hello_sent = False

    def _drop_answers(self) -> None:
        """Cancel every baseline_done still to come, interrupted ones included: they belong to
        a connection (or a handshake) that is over (rule 1)."""
        for task in list(self._answers):
            task.cancel()
        self._answers.clear()

    def _track(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._answers.add(task)
        task.add_done_callback(self._answers.discard)
        return task

    # -- health --------------------------------------------------------------------

    def _status(self) -> dict[str, Any]:
        board_ok = bool(self.engine.board_ok())
        message = self.engine.message
        if not self.display_ok:
            message = "display down, relaunching" + (f"; {message}" if message else "")
        elif isinstance(self.display, PhoneDisplay):
            message += "; the phone draws the flicker"
        elif isinstance(self.display, HeadlessDisplay):
            message += "; headless display"
        status = {"type": "status", "ok": board_ok and self.display_ok, "board_ok": board_ok,
                  "display_ok": self.display_ok, "message": message}
        if self.code:
            status["trained"] = bool(self.engine.trained)
        return status

    def _send_status(self) -> None:
        self._last_status = self._status()
        self._last_status_at = time.monotonic()
        self._emit(self._last_status)

    async def _tick(self) -> None:
        """Health every TICK_S: status on every change and every --status-interval seconds."""
        self._send_status()
        while True:
            await asyncio.sleep(TICK_S)
            if self.display_ok and not self.display.alive:
                self._display_down("the display process is gone")
            now = self._status()
            changed = {k: v for k, v in now.items() if k != "message"} != {
                k: v for k, v in self._last_status.items() if k != "message"}
            if changed or time.monotonic() - self._last_status_at >= self.args.status_interval:
                self._send_status()
            t = self.trial
            if t is not None and t.onset is not None and not t.answered and not now["board_ok"]:
                t.answered = True
                self._emit({"type": "no_selection", "trial_id": t.trial_id,
                            "reason": "board_stalled"})

    # -- the display -----------------------------------------------------------------

    def _display_up(self, refresh: float) -> None:
        # The display's start() has already written {"flicker": false} as the new process's
        # first line: until a display has seen a JSON line, an integer line (a highlight)
        # would end --hold-still and flicker every target without a stim_start
        # (brainsync.stimulus.feedback, Control.used). From then on only {"flicker": ...} counts.
        self.refresh = float(refresh)
        self.display_ok = True
        self.flickering = False
        log.info("display up at %.2f Hz", self.refresh)

    def _display_down(self, why: str) -> None:
        if not self.display_ok:
            return
        log.warning("display down: %s", why)
        self.display_ok = False
        self.flickering = False   # the process is gone, and with it the flicker
        self.refresh = None
        self._end_trial(send_off=False)
        self._send_status()
        if self.relaunching is None or self.relaunching.done():
            self.relaunching = asyncio.create_task(self._relaunch())

    async def _relaunch(self) -> None:
        while not self.finished:
            await asyncio.sleep(0)   # let the down status go out first
            try:
                refresh = await asyncio.to_thread(self.display.start)
            except Exception as exc:   # any launch failure: keep trying every RELAUNCH_S
                if self.finished:
                    return
                log.warning("display relaunch failed: %s; retrying in %g s", exc, RELAUNCH_S,
                            exc_info=not isinstance(exc, RuntimeError))
                await asyncio.sleep(RELAUNCH_S)
                continue
            if self.finished:   # stopping while it launched: never leave a window behind
                await asyncio.to_thread(self.display.close)
                return
            self._display_up(refresh)
            self._restore_display()
            if self.outbox is not None and not self.hello_sent:
                log.info("display back: hello")
                self._send_hello()
            elif (self.hello_refresh is not None and self.outbox is not None
                    and abs(refresh - self.hello_refresh) > REFRESH_CHANGE_HZ):
                log.warning("the display now runs at %.2f Hz, hello said %.2f Hz: new hello",
                            refresh, self.hello_refresh)
                self._send_hello()
            self._send_status()
            return

    def _restore_display(self) -> None:
        """The relaunched display shows the last screen again, held still (_display_up has
        already sent it {"flicker": false})."""
        self.display.send(_line({"labels": self.labels}))
        self.display.send(_line({"text": self.text}))
        self.display.send(str(self.highlight))

    def _on_display_event(self, kind: str, *data: Any) -> None:
        if self.finished:
            return   # stopping: a display that exits now is not relaunched
        if kind == "exit":
            self._display_down(f"it exited with code {data[0]} (Esc or a crash)")
        elif kind == "marker":
            t_unix, name = data
            if name == "flicker_on":
                self._on_onset(float(t_unix))

    def _flicker_off(self, force: bool = False) -> None:
        if self.flickering or force:
            self.display.send(FLICKER_OFF)
        self.flickering = False

    # -- trials --------------------------------------------------------------------

    def _not_ready(self) -> str | None:
        if not self.acked:
            return "no hello_ack on this connection"
        if not self.display_ok:
            return "the display is down"
        if self.baseline is not None:
            return "a baseline is running"
        if not self.ready:
            return "no baseline since the last hello or pause"
        return None

    def _on_stim_start(self, msg: dict[str, Any]) -> None:
        trial_id = msg.get("trial_id")
        active = msg.get("active_slots")
        if not isinstance(trial_id, int) or not isinstance(active, list) or not active:
            log.warning("ignored invalid stim_start: %.200s", msg)
            return
        why = self._not_ready()
        if why is not None:
            log.warning("trial %d: not ready (%s)", trial_id, why)
            self._emit({"type": "no_selection", "trial_id": trial_id, "reason": "not_ready"})
            return
        unknown = [s for s in active if s not in self.slots]
        if unknown:
            log.warning("trial %d: unknown slots %s", trial_id, unknown)
            self._emit({"type": "no_selection", "trial_id": trial_id, "reason": "unknown_slot"})
            return
        if self.trial is not None:
            log.info("trial %d superseded by trial %d", self.trial.trial_id, trial_id)
            self._end_trial()
        targets = [self.slots.index(s) for s in dict.fromkeys(active)]
        self.display.send(active_line(targets))
        now = time.time()
        self.trial = Trial(trial_id, [self.slots[k] for k in targets], now)
        self.engine.start_trial(trial_id, targets, now)
        self.flickering = True
        if not self.code:   # code: the phone draws (and stops) the flicker, not this process
            self._flicker_timer = self.loop.call_later(   # type: ignore[union-attr]
                self.args.max_flicker_s, self._flicker_limit, trial_id)
        if not self.display.send(FLICKER_ON):
            self._display_down("it does not take stdin lines")
            return
        log.info("trial %d started (active %s)", trial_id, ",".join(self.trial.active))

    def _flicker_limit(self, trial_id: int) -> None:
        """A local backstop (photosensitive safety): the server never stopped this trial within
        --max-flicker-s, so the flicker goes off here, whatever the server does next."""
        t = self.trial
        if t is None or t.trial_id != trial_id:
            return
        log.warning("trial %d: no stim_stop within %g s: flicker off", trial_id,
                    self.args.max_flicker_s)
        self._end_trial()
        if t.onset is not None and not t.answered:
            self._emit({"type": "no_selection", "trial_id": trial_id, "reason": "flicker_limit"})

    def _end_trial(self, send_off: bool = True) -> None:
        if self._flicker_timer is not None:
            self._flicker_timer.cancel()
            self._flicker_timer = None
        if send_off:
            self._flicker_off()
        if self.trial is not None:
            self.engine.stop()
            self.trial = None

    def _on_onset(self, t_unix: float) -> None:
        t = self.trial
        if t is None or t.onset is not None or t_unix < t.sent_at - ONSET_SLACK_S:
            return   # no trial, already stamped, or a marker of an earlier flicker
        self._set_onset(t, t_unix, emit=True)

    def _set_onset(self, t: Trial, t_unix: float, emit: bool) -> None:
        """The trial's onset: from the display (freq, sent as stim_onset) or its first cycle
        marker (code, not sent); a selection held for it goes out now."""
        t.onset = t_unix
        if emit:
            self._emit({"type": "stim_onset", "trial_id": t.trial_id, "t_unix": t_unix})
        if t.pending is not None:
            sel, t.pending = t.pending, None
            sel["t_unix"] = max(sel["t_unix"], t_unix)
            self._emit(sel)

    def _on_commit(self, trial_id: int, target: int, scores: np.ndarray) -> None:
        t = self.trial
        if t is None or t.trial_id != trial_id or t.answered:
            return   # late: the trial ended, or it was answered already (rule 1)
        slot = self.slots[target]
        if slot not in t.active:
            return
        values = {s: round(float(v), 5) for s, v in zip(self.slots, scores)}
        sel = {"type": "selection", "trial_id": trial_id, "slot": slot, "t_unix": time.time(),
               "score": values[slot], "scores": values}
        t.answered = True
        log.info("trial %d -> %s", trial_id, slot)
        if t.onset is None:
            t.pending = sel   # never before the trial's stim_onset
            log.info("trial %d: selection held until the display reports its onset", trial_id)
        else:
            sel["t_unix"] = max(sel["t_unix"], t.onset)
            self._emit(sel)

    # -- paradigm code: cycle markers and training ---------------------------------

    def _on_markers(self, msg: dict[str, Any]) -> None:
        trial_id, raw = msg.get("trial_id"), msg.get("markers")
        if not isinstance(trial_id, int) or not isinstance(raw, list):
            log.warning("ignored invalid stim_markers: %.200s", msg)
            return
        t = self.trial
        if t is None or t.trial_id != trial_id:
            log.debug("ignored stim_markers of trial %s (not the current trial)", trial_id)
            return
        marks = [(int(m["cycle"]), float(m["t_unix"])) for m in raw
                 if isinstance(m, dict) and _is_int(m.get("cycle")) and m["cycle"] >= 0
                 and _is_number(m.get("t_unix"))]
        if not marks:
            log.warning("ignored stim_markers without a valid marker: %.200s", msg)
            return
        self.engine.add_markers(trial_id, marks)
        if t.onset is not None:
            return
        # The trial's first marker (normally cycle 0) is its onset: nothing is answered before.
        self._set_onset(t, min(marks)[1], emit=False)
        if not self.engine.trained and t.training is None and not t.answered:
            t.answered = True
            log.warning("trial %d: no c-VEP template yet (train first)", trial_id)
            self._emit({"type": "no_selection", "trial_id": trial_id, "reason": "not_trained"})

    def _on_train(self, msg: dict[str, Any]) -> None:
        train_id, trial_id = msg.get("train_id"), msg.get("trial_id")
        slot, cycles = msg.get("slot"), msg.get("cycles")
        if not _is_int(train_id) or not _is_int(trial_id):
            log.warning("ignored invalid train: %.200s", msg)
            return

        def refuse(why: str) -> None:
            log.warning("train %d: %s", train_id, why)
            self._emit({"type": "train_done", "train_id": train_id, "ok": False,
                        "accuracy": None, "cycles": 0, "message": why})

        t = self.trial
        if not _is_int(cycles) or cycles < 2:
            refuse(f"cycles must be an integer >= 2, got {cycles!r}")
        elif t is None or t.trial_id != trial_id:
            refuse(f"trial {trial_id} is not running")
        elif slot not in t.active:
            refuse(f"slot {slot!r} is not active in trial {trial_id}")
        else:
            t.training = train_id
            t.answered = True   # a training trial never answers with a selection
            self.trains.add(train_id)
            self.engine.start_training(train_id, trial_id, self.slots.index(slot), cycles)
            log.info("train %d: %d cycles of trial %d looking at %s", train_id, cycles,
                     trial_id, slot)

    def _on_trained(self, train_id: int, ok: bool, accuracy: float | None, cycles: int,
                    message: str) -> None:
        if train_id not in self.trains:
            return   # its connection (or handshake) is over
        self.trains.discard(train_id)
        log.info("train %d done: %s", train_id, message or ("ok" if ok else "failed"))
        self._emit({"type": "train_done", "train_id": train_id, "ok": bool(ok),
                    "accuracy": None if accuracy is None else round(float(accuracy), 4),
                    "cycles": int(cycles), "message": message})
        self._send_status()   # trained may have changed

    # -- baselines -----------------------------------------------------------------

    def _on_baseline_msg(self, msg: dict[str, Any]) -> None:
        bid, seconds = msg.get("baseline_id"), msg.get("seconds")
        if not isinstance(bid, int) or not isinstance(seconds, (int, float)) or seconds <= 0:
            log.warning("ignored invalid baseline: %.200s", msg)
            return
        if not self.acked or not self.display_ok:
            # It cannot record this one; the answer still waits the seconds asked for (rule 12).
            why = "not_ready" if not self.acked else "display_down"
            log.warning("baseline %d: cannot record it (%s)", bid, why)
            self._track(self._refuse_baseline(bid, float(seconds), why, self.outbox))
            return
        self._end_trial()
        self._flicker_off()
        self._interrupt_baseline()
        self.ready = False
        future = self.loop.create_future()   # type: ignore[union-attr]
        run = BaselineRun(bid, float(seconds), future, outbox=self.outbox)
        self.baseline = run
        self.engine.start_baseline(bid, float(seconds))
        run.task = self._track(self._baseline_task(run))
        log.info("baseline %d: recording %.1f s of still screen", bid, seconds)

    async def _refuse_baseline(self, bid: int, seconds: float, why: str, outbox: Any) -> None:
        await asyncio.sleep(seconds)
        if self.outbox is outbox:
            self._emit({"type": "baseline_done", "baseline_id": bid, "ok": False,
                        "message": why})

    async def _baseline_task(self, run: BaselineRun) -> None:
        await asyncio.sleep(run.seconds)   # never before the seconds asked for
        ok, message = await run.result
        if run.interrupted:
            ok, message = False, "interrupted"
        if self.baseline is run:
            self.baseline = None
            self.ready = ok
        log.info("baseline %d done: %s", run.baseline_id, "ok" if ok else message)
        if self.outbox is run.outbox:   # only to the connection that asked (rule 1)
            self._emit({"type": "baseline_done", "baseline_id": run.baseline_id, "ok": ok,
                        "message": message})

    def _on_baseline(self, baseline_id: int, ok: bool, message: str) -> None:
        run = self.baseline
        if run is not None and run.baseline_id == baseline_id and not run.result.done():
            run.result.set_result((ok, message))

    def _interrupt_baseline(self, cancel: bool = False) -> None:
        """Cut the running baseline short: it answers "interrupted" (``cancel``: never answers,
        the connection is gone)."""
        run, self.baseline = self.baseline, None
        if run is None:
            return
        run.interrupted = True
        if not run.result.done():
            run.result.set_result((False, "interrupted"))
        if cancel and run.task is not None:
            run.task.cancel()
        self.engine.stop()

    # -- incoming messages -------------------------------------------------------------

    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("ignored a message that is not JSON: %.200s", raw)
            return
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello_ack":
            self.acked, self.rejected = True, False
            log.info("hello_ack: roles %s", msg.get("roles"))
            labels = msg.get("labels")
            if isinstance(labels, dict) and self.trial is None:
                self._apply_display({"labels": labels})
        elif kind == "error":
            log.error("the server rejected hello: %s", msg.get("message"))
            self._flicker_off(force=True)
            self._end_trial(send_off=False)
            self._interrupt_baseline(cancel=True)
            self._drop_answers()
            self.trains.clear()
            self.acked = self.ready = False
            self.rejected = True
        elif self.rejected:
            log.info("ignored %s: the server rejected hello", kind)
        elif kind == "baseline":
            self._on_baseline_msg(msg)
        elif kind == "display":
            if self.trial is not None:
                log.warning("ignored display during trial %d (rule 5)", self.trial.trial_id)
            else:
                self._apply_display(msg)
        elif kind == "stim_start":
            self._on_stim_start(msg)
        elif kind == "stim_stop":
            if self.trial is not None and msg.get("trial_id") == self.trial.trial_id:
                log.info("trial %d stopped (%s)", self.trial.trial_id, msg.get("reason"))
                self._end_trial()
        elif kind in ("stim_markers", "train") and self.code:
            (self._on_markers if kind == "stim_markers" else self._on_train)(msg)
        elif kind in ("stim_markers", "train"):
            log.warning("ignored %s: this bridge runs paradigm freq", kind)
        elif kind == "pause":
            log.info("pause (%s): flicker and decoding off", msg.get("reason"))
            self._flicker_off(force=True)
            self._end_trial(send_off=False)
            self._interrupt_baseline()
            self.ready = False
        else:
            log.warning("ignored unknown message type %r", kind)

    def _apply_display(self, msg: dict[str, Any]) -> None:
        lines, self.labels = display_lines(msg, self.slots, self.labels)
        if isinstance(msg.get("text"), str):
            self.text = msg["text"]
        if "highlight" in msg:
            slot = msg["highlight"]
            self.highlight = self.slots.index(slot) if slot in self.slots else -1
        if not self.display_ok:
            return   # kept: _restore_display draws it on the relaunched display
        for line in lines:
            self.display.send(line)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def bridge_name(args: argparse.Namespace) -> str:
    """The hello name: "fake-..." with the fake board, so the server shows FAKE DECODER."""
    if getattr(args, "paradigm", "freq") == "code":
        return f"{'fake-' if args.fake else ''}cvep-template"
    return f"{'fake-' if args.fake else ''}brainsync-{args.classifier}"


def config_name(args: argparse.Namespace) -> str:
    """-c, or the paradigm's default config (four_diamond, or cvep_2 for code)."""
    if args.config is not None:
        return str(args.config)
    return DEFAULT_CONFIGS[getattr(args, "paradigm", "freq")]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.voice_bridge", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=DEFAULT_URL, help=f"server websocket (default {DEFAULT_URL})")
    p.add_argument("-c", "--config", default=None,
                   help=f"stimulus config name or .toml path (default {DEFAULT_CONFIG}; "
                        f"{DEFAULT_CONFIGS['code']} with --paradigm code)")
    p.add_argument("--paradigm", choices=("freq", "code"), default="freq",
                   help="freq: SSVEP with the PsychoPy display (default); code: c-VEP drawn "
                        "by the phone (INTERFACE-v2-draft.md section 9), no display process")
    dec = p.add_argument_group("decoder (defaults as brainsync.demo)")
    dec.add_argument("--classifier", default=TUNED.classifier, choices=("cca", "fbcca"),
                     help=f"default {TUNED.classifier}")
    dec.add_argument("--window", type=float, default=TUNED.window_s,
                     help=f"window seconds (default {TUNED.window_s:g})")
    dec.add_argument("--step", type=float, default=TUNED.step_s,
                     help=f"seconds between decisions (default {TUNED.step_s:g})")
    dec.add_argument("--votes", type=int, default=TUNED.votes,
                     help=f"consecutive agreeing windows needed to commit (default {TUNED.votes})")
    dec.add_argument("--margin", type=float, default=TUNED.margin,
                     help=f"abstain unless the best target leads by this many rest spreads "
                          f"(default {TUNED.margin:g})")
    dec.add_argument("--threshold", type=threshold_arg, default=TUNED.threshold,
                     help="abstain unless the best target is this many rest spreads above its "
                          f"rest level, or 'none' (default {TUNED.threshold})")
    dec.add_argument("--pad", type=float, default=2.0,
                     help="seconds filtered before each window and dropped (default 2)")
    dec.add_argument("--harmonics", type=int, default=3, help="template harmonics (default 3)")
    code = p.add_argument_group("c-VEP (--paradigm code; defaults as python -m brainsync.vep)")
    code.add_argument("--template", type=Path, default=None, metavar="FILE",
                      help="a saved <stamp>_cvep_template.npz (default: none, the server's "
                           "train makes one)")
    code.add_argument("--cycles", type=int, default=2,
                      help="cycles averaged before each decision (default 2)")
    code.add_argument("--code-votes", type=int, default=2,
                      help="consecutive agreeing decisions needed to commit (default 2)")
    code.add_argument("--code-margin", type=float, default=0.0,
                      help="abstain unless the best correlation leads by this much (default 0)")
    code.add_argument("--code-pad", type=float, default=0.5,
                      help="seconds filtered around each cycle and dropped (default 0.5)")
    board = p.add_argument_group("board")
    board.add_argument("--names", type=_names, default=list(MONTAGE),
                       help="comma-separated names of the 8 board channels, in order")
    board.add_argument("--channels", type=_channels, default=list(POSTERIOR),
                       help="analysis channels (default " + ",".join(POSTERIOR) + ")")
    board.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    board.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
    board.add_argument("--fake", action="store_true", help="FakeBoard instead of the Cerelog X8")
    board.add_argument("--fast", action="store_true",
                       help=f"with --fake: virtual time, {FAST_SPEED:g}x faster than real time")
    board.add_argument("--seed", type=int, default=None,
                       help="with --fake: noise and target seed")
    disp = p.add_argument_group("display")
    disp.add_argument("--no-stimulus", action="store_true",
                      help="no PsychoPy window: a headless stand-in (onset = time.time())")
    disp.add_argument("--no-wait", action="store_true",
                      help="skip the display's start screen (pass --no-wait to it)")
    disp.add_argument("--ignore-refresh", action="store_true",
                      help="let the display run when its measured refresh is off (the server "
                           "still rejects anything more than 1 Hz from 60 Hz)")
    disp.add_argument("--display-timeout", type=float, default=60.0,
                      help="seconds to wait for the display's refresh line (default 60)")
    disp.add_argument("--max-flicker-s", type=float, default=MAX_FLICKER_S,
                      help="turn the flicker off by itself if the server has not stopped the "
                           f"trial after this many seconds (default {MAX_FLICKER_S:g})")
    conn = p.add_argument_group("connection")
    conn.add_argument("--reconnect", action=argparse.BooleanOptionalAction, default=True,
                      help="retry if the connection drops (default on)")
    conn.add_argument("--reconnect-s", type=float, default=1.0,
                      help="seconds between reconnect attempts (default 1)")
    conn.add_argument("--status-interval", type=float, default=1.0,
                      help="seconds between status messages (default 1)")
    conn.add_argument("--parent-pid", type=int, default=None, metavar="PID",
                      help="exit once process PID is gone (python -m app.server --bridge passes "
                           "its own)")
    conn.add_argument("--log-level", default="INFO",
                      choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="default INFO")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject inconsistent flags through ``p.error`` (exit code 2)."""
    if args.fast and not args.fake:
        p.error("--fast needs --fake")
    if args.paradigm == "code" and args.fast:
        p.error("--fast is for --paradigm freq: in code mode the cycle markers come in real time")
    if args.template is not None and args.paradigm != "code":
        p.error("--template is for --paradigm code")
    for flag, value in (("--cycles", args.cycles), ("--code-votes", args.code_votes)):
        if value < 1:
            p.error(f"{flag} must be >= 1, got {value}")
    if args.code_margin < 0 or args.code_pad < 0:
        p.error(f"--code-margin and --code-pad must be >= 0, got {args.code_margin}, "
                f"{args.code_pad}")
    for flag, value in (("--window", args.window), ("--step", args.step),
                        ("--reconnect-s", args.reconnect_s),
                        ("--status-interval", args.status_interval),
                        ("--display-timeout", args.display_timeout),
                        ("--max-flicker-s", args.max_flicker_s)):
        if not value > 0:
            p.error(f"{flag} must be positive, got {value}")
    if args.votes < 1:
        p.error(f"--votes must be >= 1, got {args.votes}")
    if args.margin < 0 or args.pad < 0:
        p.error(f"--margin and --pad must be >= 0, got {args.margin}, {args.pad}")
    if len(args.names) != len(MONTAGE):
        p.error(f"--names needs {len(MONTAGE)} names, got {len(args.names)}: {args.names}")


def stimulus_args(args: argparse.Namespace) -> list[str]:
    """The display's command line: free run, feedback on stdin, still until {"flicker": true},
    flip-time markers; no --seconds, so it runs until its stdin closes or Esc."""
    out = ["-c", config_name(args), "--free", "--feedback", "stdin", "--hold-still",
           "--markers", "unix"]
    if args.no_wait:
        out.append("--no-wait")
    if args.ignore_refresh:
        out.append("--ignore-refresh")
    return out


def build(args: argparse.Namespace) -> tuple[StimulusConfig, Any, Decoder]:
    """The stimulus config, the board (not yet connected) and the decoder ``args`` ask for.

    Raises one of BUILD_ERRORS on a bad setting.
    """
    cfg = load_config(config_name(args))
    if cfg.paradigm != "freq":
        raise ValueError(f"config {cfg.name!r} is a [{cfg.paradigm}] config; c-VEP runs with "
                         "--paradigm code")
    if args.fake:
        board: Any = FakeBoard(cfg.freqs, switch_s=math.inf, seed=args.seed or 0,
                               clock=None if args.fast else time.time, ch_names=args.names)
    else:
        board = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
    clf = make(args.classifier, freqs=cfg.freqs, fs=board.fs, harmonics=args.harmonics,
               phases=cfg.phases, margin=args.margin, threshold=args.threshold)
    decoder = Decoder(clf, board.fs, window_s=args.window, step_s=args.step,
                      votes=args.votes, channels=args.channels, pad_s=args.pad,
                      ch_names=args.names)
    return cfg, board, decoder


def build_code(args: argparse.Namespace) -> tuple[StimulusConfig, Any, Any]:
    """--paradigm code: the [code] config, the board (not yet connected; MarkerFakeBoard with
    --fake) and the template from --template (None: train one).

    Raises one of BUILD_ERRORS on a bad setting.
    """
    cfg = load_config(config_name(args))
    if cfg.paradigm != "code":
        raise ValueError(f"config {cfg.name!r} is a [{cfg.paradigm}] config, not code; the "
                         f"default for --paradigm code is {DEFAULT_CONFIGS['code']}")
    if args.fake:
        board: Any = MarkerFakeBoard(cfg, seed=args.seed or 0, ch_names=args.names)
    else:
        board = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
    template = None
    if args.template is not None:
        template = load_template(args.template, cfg, board.fs, args.channels, args.names)
    return cfg, board, template


def code_engine(args: argparse.Namespace, cfg: StimulusConfig, board: Any,
                template: Any) -> CodeEngine:
    """The c-VEP engine for ``args``; a fake run saves its templates in a temp folder (as
    python -m brainsync.vep does), a real one in the recordings folder."""
    save_dir = Path(tempfile.gettempdir()) / "brainsync_fake" if args.fake else None
    return CodeEngine(board, cfg, template=template, channels=args.channels,
                      ch_names=args.names, votes=args.code_votes, cycles=args.cycles,
                      margin=args.code_margin, pad_s=args.code_pad, fake=args.fake,
                      seed=args.seed, save_dir=save_dir)


async def serve_code(args: argparse.Namespace) -> int:
    """--paradigm code: no display process, the c-VEP engine on the board thread."""
    try:
        cfg, board, template = build_code(args)
    except BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    display = PhoneDisplay(cfg.refresh)
    if not args.fake:
        print("connecting to the Cerelog X8 (can take 15 s)...", file=sys.stderr)
    try:
        await asyncio.to_thread(board.open)   # once: kept across reconnects
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        engine = code_engine(args, cfg, board, template)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            board.close()
        return 2
    print(f"{engine.message}; c-VEP {cfg.name}: {cfg.n_targets} targets "
          f"{cfg.code.lag_frames} frames apart, {cfg.code.cycle_frames}-frame cycles; "
          f"template {args.template or 'none (the server trains one)'}; {args.cycles} cycles "
          f"per decision, {args.code_votes} votes; server {args.url}", file=sys.stderr)
    return await _run_bridge(args, cfg, engine, display, board)


async def serve(args: argparse.Namespace) -> int:
    """Build everything, connect the board once, run the bridge; the exit code."""
    if getattr(args, "paradigm", "freq") == "code":
        return await serve_code(args)
    try:
        cfg, board, decoder = build(args)
    except BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.no_stimulus:
        display: Any = HeadlessDisplay(cfg.refresh)
    else:
        display = PsychoPyDisplay(stimulus_args(args), cfg.refresh, timeout=args.display_timeout)
    if not args.fake:
        print("connecting to the Cerelog X8 (can take 15 s)...", file=sys.stderr)
    try:
        await asyncio.to_thread(board.open)   # once: kept across reconnects
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    engine = BoardEngine(board, decoder, fake=args.fake, fast=args.fast, seed=args.seed)
    print(f"{engine.message}; {decoder.classifier!r}; channels "
          f"{','.join(decoder.channel_names)}; window {args.window:g} s, step {args.step:g} s, "
          f"{args.votes} votes; server {args.url}", file=sys.stderr)
    return await _run_bridge(args, cfg, engine, display, board)


async def _run_bridge(args: argparse.Namespace, cfg: StimulusConfig, engine: Any, display: Any,
                      board: Any) -> int:
    """Run the bridge until it stops; then close the display, the engine and the board."""
    bridge = Bridge(args, cfg, engine, display)
    loop = asyncio.get_running_loop()
    # app.server stops its children with SIGTERM: end like Ctrl-C, so the display's stdin is
    # closed and the board released (not available on Windows event loops).
    stops = False
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError, AttributeError):
        loop.add_signal_handler(signal.SIGTERM, bridge.stop)
        stops = True
    try:
        return await bridge.run()
    except RuntimeError as exc:   # the display did not come up at startup
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if stops:
            loop.remove_signal_handler(signal.SIGTERM)
        await asyncio.to_thread(display.close)
        engine.close()
        with contextlib.suppress(Exception):
            board.close()


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
