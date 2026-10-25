"""c-VEP on the phone for the BCI Voice bridge (INTERFACE-v2-draft.md section 9): the phone
draws the code, the bridge decodes with the c-VEP standard's ``live.CodeVepDecoder``.

The display that shows the m-sequence (the iPhone app) is not on this machine, so its cycle
onsets arrive over the network as ``stim_markers`` ({cycle, t_unix} on this machine's clock),
usually a fraction of a cycle after the frame was on screen. ``CodeEngine`` reads the board on
its own thread like ``voice_bridge.BoardEngine`` and hands those markers to a
``live.CodeVepDecoder`` as ``(t, "cycle", n)`` events; it cuts each cycle once it and
``pad_s`` of samples after it are in, so a late marker costs nothing. What the protocol needs
on top of the decoder is here: trials (the decoder reset per trial, samples from ``pad_s``
before the trial's stim_start on, so the first cycle is filtered with its lead-in; votes only
among the trial's active targets; one answer per trial) and ``train`` as its own command: a
fresh ``CodeVepDecoder`` (``train_cycles`` = the cycles asked for, ``train_target`` = the cued
slot) is fed that trial's samples and markers, and once it has trained, its template and
leave-one-cycle-out ``train_accuracy`` go to the decoding one (``use_template``), so a failed
retraining keeps the old template. The template is saved as ``<stamp>_cvep_template.npz``
(``cvep.Template.save``), as ``python -m brainsync.vep`` does.

``MarkerFakeBoard`` is the synthetic board for this setup: it hands out its samples
``hold_s`` seconds late (a board with latency), so when the first marker of a trial arrives
the samples after that onset are not generated yet, and the fixated target's code response
starts exactly at the marker's time. ``code_hello`` is the bridge's hello for a [code] config.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import queue
import random
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ..board import FS, N_CHANNELS, POSTERIOR_ROWS
from ..metrics import ABSTAIN
from ..online import Voter
from ..recordings import MONTAGE, POSTERIOR, channel_indices
from ..stimulus import StimulusConfig
from ..synthetic import vep_kernel
from .codes import lagged_codes, m_sequence
from .cvep import Template, lag_samples
from .live import CodeVepDecoder

__all__ = ["CodeEngine", "MarkerFakeBoard", "code_bits", "code_hello", "cycle_samples",
           "load_template"]

log = logging.getLogger("voice_bridge")

POLL_S = 0.05            # board read interval
STALL_S = 5.0            # no samples for this long: board_ok false
HOLD_S = 2.0             # MarkerFakeBoard: samples are handed out this late (> a cycle + slack)
RECENT_S = 2.0           # samples kept between trials, so a trial's first epoch has its pad
MAX_TRIAL_BUFFER_S = 120.0   # a trial keeps at most this much of its samples (for training)
SCORE_KIND = "cvep_correlation"


# ---------------------------------------------------------------------------
# The hello and the cycle geometry
# ---------------------------------------------------------------------------


def _code_of(cfg: StimulusConfig) -> Any:
    if cfg.code is None:
        raise ValueError(f"config {cfg.name!r} has no [code] table: not a c-VEP config")
    return cfg.code


def code_bits(cfg: StimulusConfig) -> str:
    """One period of the config's m-sequence as "0"/"1" (bit 1 bright): the same
    ``m_sequence(length)`` (seed 1) that ``brainsync.stimulus.coded`` draws."""
    return "".join(str(int(b)) for b in m_sequence(_code_of(cfg).length))


def cycle_samples(cfg: StimulusConfig, fs: float) -> int:
    """Samples in one code cycle, as ``python -m brainsync.vep`` cuts them."""
    return int(round(_code_of(cfg).cycle_frames / cfg.refresh * fs))


def cycle_seconds(cfg: StimulusConfig) -> float:
    return _code_of(cfg).cycle_frames / cfg.refresh


def code_hello(cfg: StimulusConfig, name: str, decoder: dict[str, Any] | None = None,
               score_kind: str | None = SCORE_KIND, protocol: int = 2) -> dict[str, Any]:
    """The protocol-2 hello for a [code] config (draft section 9): paradigm code, the bits,
    slots s1..sN lagging ``k * lag_frames`` frames, square (on / off), the config's refresh."""
    code = _code_of(cfg)
    slots = [{"slot": f"s{k + 1}", "lag_frames": int(k * code.lag_frames)}
             for k in range(cfg.n_targets)]
    msg: dict[str, Any] = {"type": "hello", "protocol": protocol, "name": name,
                           "refresh_hz": round(float(cfg.refresh), 3), "waveform": "square",
                           "paradigm": "code",
                           "code": {"length": int(code.length),
                                    "frames_per_bit": int(code.frames_per_bit),
                                    "bits": code_bits(cfg)},
                           "slots": slots}
    if decoder is not None:
        msg["decoder"] = decoder
    if score_kind is not None:
        msg["score_kind"] = score_kind
    return msg


def load_template(path: str | Path, cfg: StimulusConfig, fs: float,
                  channels: Sequence[str | int] | None = POSTERIOR,
                  ch_names: Sequence[str] = MONTAGE) -> Template:
    """A saved template, checked against the config and the board; ValueError if it does
    not fit (another config, sample rate, channel count or cycle length)."""
    try:
        tpl = Template.load(path)
    except FileNotFoundError:
        raise
    except Exception as exc:   # a damaged or foreign file: np.load / zip / key errors
        raise ValueError(f"cannot read template {path}: {exc}") from exc
    rows = channel_indices(tuple(ch_names), channels)
    lag = lag_samples(_code_of(cfg).lag_frames, fs, cfg.refresh)
    problems = []
    if tpl.n_targets != cfg.n_targets:
        problems.append(f"{tpl.n_targets} targets, config {cfg.name!r} has {cfg.n_targets}")
    if abs(tpl.fs - fs) > 1e-6:
        problems.append(f"{tpl.fs:g} Hz, the board {fs:g} Hz")
    if tpl.n_channels != len(rows):
        problems.append(f"{tpl.n_channels} channels, the decoder uses {len(rows)}")
    if tpl.n_samples != cycle_samples(cfg, fs):
        problems.append(f"{tpl.n_samples}-sample cycles, config {cfg.name!r} has "
                        f"{cycle_samples(cfg, fs)}")
    if abs(tpl.lag_samples - lag) > 0.5:
        problems.append(f"lag {tpl.lag_samples:g} samples, config {cfg.name!r} has {lag:g}")
    if problems:
        raise ValueError(f"template {path} does not fit: " + "; ".join(problems))
    return tpl


# ---------------------------------------------------------------------------
# The synthetic board
# ---------------------------------------------------------------------------


class MarkerFakeBoard:
    """Synthetic 8-channel board whose flicker starts at a Unix time the markers give.

    ``read`` returns every sample up to ``clock() - hold_s`` (Unix times on the samples), so a
    flicker started at a marker's time, up to ``hold_s`` after that time, still shapes every
    sample from the onset on. Between ``start_flicker`` and ``stop_flicker`` the fixated
    target's lagged code (frame n = floor((t - onset) * refresh)) is convolved with
    ``synthetic.vep_kernel`` and added, ``amp`` µV times a per-channel gain, to P7, P8, O1
    and O2 over Gaussian noise; only the fixated target evokes anything (an easy check of
    the pipeline, not of anyone's visual cortex).
    """

    def __init__(self, cfg: StimulusConfig, fs: float = FS, amp: float = 3.0,
                 noise: float = 8.0, seed: int = 0, hold_s: float = HOLD_S,
                 clock: Callable[[], float] = time.time,
                 ch_names: Sequence[str] | None = None) -> None:
        code = _code_of(cfg)
        if fs <= 0 or amp < 0 or noise < 0 or hold_s < 0:
            raise ValueError(f"need fs > 0 and amp, noise, hold_s >= 0, got {fs}, {amp}, "
                             f"{noise}, {hold_s}")
        self.fs, self.refresh = float(fs), float(cfg.refresh)
        self.amp, self.noise, self.hold_s = float(amp), float(noise), float(hold_s)
        names = tuple(MONTAGE) if ch_names is None else tuple(str(c) for c in ch_names)
        if len(names) != N_CHANNELS:
            raise ValueError(f"need {N_CHANNELS} channel names, got {len(names)}")
        self.ch_names = names
        self.n_targets = cfg.n_targets
        self.codes = lagged_codes(m_sequence(code.length), cfg.n_targets, code.lag_frames,
                                  code.frames_per_bit).astype(float)
        self.kernel = vep_kernel(self.fs)
        self.clock = clock
        self._rng = np.random.default_rng(seed)
        self.gains = self._rng.uniform(0.7, 1.3, len(POSTERIOR_ROWS))
        self._t0: float | None = None   # Unix time of sample 0
        self._n = 0
        self._hist = np.zeros(self.kernel.size - 1)
        self._target = ABSTAIN
        self._onset: float | None = None
        self._stop: float | None = None

    def __enter__(self) -> MarkerFakeBoard:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> MarkerFakeBoard:
        if self._t0 is None:
            self._t0 = self.clock() - self.hold_s
        return self

    def close(self) -> None:
        pass

    @property
    def fixated(self) -> int:
        return self._target

    def set_target(self, target: int) -> None:
        """Look at ``target`` (-1: at none of them)."""
        if int(target) != target or not -1 <= int(target) < self.n_targets:
            raise ValueError(f"target must be -1..{self.n_targets - 1}, got {target}")
        self._target = int(target)

    def start_flicker(self, onset: float) -> None:
        """The code's frame 0 was on screen at Unix time ``onset``."""
        self._onset, self._stop = float(onset), None

    def stop_flicker(self, at: float | None = None) -> None:
        if self._onset is not None and self._stop is None:
            self._stop = float(self.clock() if at is None else at)

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        """Every sample since the previous read: X in µV (8, n), t Unix seconds (n,)."""
        self.open()
        assert self._t0 is not None
        due = int(math.floor((self.clock() - self.hold_s - self._t0) * self.fs + 1e-9))
        n = due - self._n
        if n <= 0:
            return np.empty((N_CHANNELS, 0)), np.empty(0)
        t = self._t0 + (self._n + np.arange(n)) / self.fs
        stim = np.zeros(n)
        if self._onset is not None and self._target >= 0:
            frames = np.floor((t - self._onset) * self.refresh + 1e-9).astype(int)
            on = frames >= 0
            if self._stop is not None:
                on &= t < self._stop
            row = self.codes[self._target]
            stim[on] = row[frames[on] % row.size] - 0.5
        both = np.concatenate([self._hist, stim])
        response = np.convolve(both, self.kernel)[self._hist.size:self._hist.size + n]
        if self.kernel.size > 1:
            self._hist = both[-(self.kernel.size - 1):]
        X = self.noise * self._rng.standard_normal((N_CHANNELS, n))
        X[list(POSTERIOR_ROWS)] += self.amp * self.gains[:, None] * response[None, :]
        self._n = due
        return X, t




# ---------------------------------------------------------------------------
# The engine: board thread, marker-locked decoding and training
# ---------------------------------------------------------------------------


class CodeEngine:
    """Reads ``board`` on its own thread; decodes c-VEP trials from relayed cycle markers.

    The asyncio side only queues commands (start_baseline, start_trial, add_markers,
    start_training, stop), applied at the thread's next read; results come back on the event
    loop through ``on_commit(trial_id, target, scores)``, ``on_baseline(baseline_id, ok,
    message)`` and ``on_train(train_id, ok, accuracy, cycles, message)``. c-VEP needs no rest
    baseline, so a baseline succeeds at once (the bridge still answers after the seconds
    asked for). Markers of another trial are ignored. With a MarkerFakeBoard the fake user
    looks at a random active target per trial (the cued one in a training trial) from the
    trial's first marker on.
    """

    def __init__(self, board: Any, cfg: StimulusConfig, *, template: Template | None = None,
                 channels: Sequence[str | int] | None = POSTERIOR,
                 ch_names: Sequence[str] = MONTAGE, votes: int = 2, cycles: int = 2,
                 margin: float = 0.0, pad_s: float = 0.5, fake: bool = False,
                 seed: int | None = None, save_dir: Path | None = None,
                 poll_s: float = POLL_S, stall_s: float = STALL_S) -> None:
        if margin < 0 or pad_s < 0:
            raise ValueError(f"margin and pad_s must be >= 0, got {margin}, {pad_s}")
        self.board = board
        self.cfg = cfg
        self.fs = float(board.fs)
        code = _code_of(cfg)
        self.cycle_s = cycle_seconds(cfg)
        self.ch_names = tuple(ch_names)
        self.rows = channel_indices(self.ch_names, channels)
        self._kw = dict(n_targets=cfg.n_targets, lag_frames=code.lag_frames,
                        cycle_frames=code.cycle_frames, refresh=cfg.refresh, channels=channels,
                        ch_names=self.ch_names, votes=votes, cycles=cycles, margin=margin,
                        pad_s=pad_s)
        # the decoding decoder; untrained (decoder.training) until a template is set
        self.decoder = CodeVepDecoder(self.fs, template=template, **self._kw)
        self.votes, self.margin, self.pad_s = int(votes), float(margin), float(pad_s)
        self.fake = fake
        self.save_dir = save_dir
        self.poll_s, self.stall_s = poll_s, stall_s
        self.template_path: Path | None = None
        self._rng = random.Random(seed)
        self._commands: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._heard = time.monotonic()
        self._error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_commit: Callable[..., None] | None = None
        self._on_baseline: Callable[..., None] | None = None
        self._on_train: Callable[..., None] | None = None
        # thread-side state
        self._mode = "idle"          # idle, trial (decoding, or buffering untrained), train
        self._trial_id: int | None = None
        self._targets: list[int] = []
        self._t_start = 0.0
        self._voter: Voter | None = None
        self._recent: list[tuple[np.ndarray, np.ndarray]] = []
        self._X = np.empty((N_CHANNELS, 0))
        self._t = np.empty(0)
        self._marks: dict[int, float] = {}          # this trial's cycle -> Unix time
        self._events: list[tuple[float, str, int]] = []   # markers not yet fed
        self._trainer: CodeVepDecoder | None = None
        self._train_id: int | None = None
        if self.fake:
            board.set_target(ABSTAIN)

    # -- what the bridge reports --------------------------------------------------

    @property
    def trained(self) -> bool:
        return not self.decoder.training

    @property
    def template(self) -> Template | None:
        return self.decoder.template

    @property
    def decoder_params(self) -> dict[str, Any]:
        """For the hello: a decision averages ``cycles`` cycles, one decision per cycle."""
        return {"window_s": round(self.decoder.cycles * self.cycle_s, 4),
                "step_s": round(self.cycle_s, 4), "votes": self.votes}

    @property
    def score_kind(self) -> str:
        return SCORE_KIND

    def min_baseline_s(self) -> float:
        return 0.0

    def attach(self, loop: asyncio.AbstractEventLoop, on_commit: Callable[..., None],
               on_baseline: Callable[..., None],
               on_train: Callable[..., None] | None = None) -> None:
        self._loop, self._on_commit, self._on_baseline = loop, on_commit, on_baseline
        self._on_train = on_train

    def start(self) -> None:
        if self._thread is None:
            self._heard = time.monotonic()
            self._thread = threading.Thread(target=self._run, name="board", daemon=True)
            self._thread.start()

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
        board = "fake c-VEP board" if self.fake else "Cerelog X8"
        return f"{board} at {self.fs:g} Hz; {'trained' if self.trained else 'no template yet'}"

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # -- commands from the event loop ---------------------------------------------

    def start_baseline(self, baseline_id: int, seconds: float) -> None:
        self._commands.put(("baseline", baseline_id, seconds))

    def start_trial(self, trial_id: int, targets: list[int], t_start: float) -> None:
        self._commands.put(("trial", trial_id, list(targets), t_start))

    def add_markers(self, trial_id: int, markers: list[tuple[int, float]]) -> None:
        self._commands.put(("markers", trial_id, list(markers)))

    def start_training(self, train_id: int, trial_id: int, target: int, cycles: int) -> None:
        self._commands.put(("train", train_id, trial_id, target, cycles))

    def stop(self) -> None:
        self._commands.put(("stop",))

    # -- the thread ----------------------------------------------------------------

    def _post(self, fn: Callable[..., None] | None, *data: Any) -> None:
        if self._loop is not None and fn is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(fn, *data)

    def _run(self) -> None:
        while not self._closed.is_set():
            self.step()
            time.sleep(self.poll_s)

    def step(self) -> None:
        """One read: apply commands, read the board, decode or train. The thread calls it
        every ``poll_s``; tests may call it directly instead of ``start``."""
        self._apply()
        try:
            X, t = self.board.read()
            self._error = None
        except Exception as exc:   # a driver error: report it, keep trying
            self._error = str(exc) or type(exc).__name__
            return
        if X.shape[1]:
            self._heard = time.monotonic()
        self._apply()   # a command that came during the read applies to these samples
        try:
            self._data(np.asarray(X, dtype=float), np.asarray(t, dtype=float).reshape(-1))
        except Exception as exc:   # never let a decoder error kill the board thread
            log.exception("c-VEP decoder error")
            self._error = f"decoder: {exc}"
            self._fail_training(f"decoder error: {exc}")
            self._mode = "idle"

    def _apply(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            kind = cmd[0]
            if kind == "baseline":
                # c-VEP decides from the template alone: no rest baseline to record
                self._idle()
                self._post(self._on_baseline, cmd[1], True, "")
            elif kind == "trial":
                self._begin_trial(*cmd[1:])
            elif kind == "markers":
                self._add_markers(cmd[1], cmd[2])
            elif kind == "train":
                self._begin_training(*cmd[1:])
            else:
                self._idle()

    def _idle(self) -> None:
        if self._trainer is not None:
            done, need = self._trainer.progress
            self._fail_training(f"the trial stopped after {done} of {need} cycles")
        self._mode = "idle"
        self._trial_id = None
        self._X, self._t = np.empty((N_CHANNELS, 0)), np.empty(0)
        self._marks, self._events = {}, []
        if self.fake:
            self.board.stop_flicker()

    def _begin_trial(self, trial_id: int, targets: list[int], t_start: float) -> None:
        self._idle()
        self._trial_id, self._targets, self._t_start = trial_id, list(targets), t_start
        self._voter = Voter(self.votes)
        self._mode = "trial"
        # samples from pad_s before the stim_start on: the first cycle's filter lead-in
        keep_from = t_start - self.pad_s
        for X, t in self._recent:
            sel = t >= keep_from
            if sel.any():
                self._take(X[:, sel], t[sel])
        self.decoder.reset()
        if self.trained and self._t.size:
            self.decoder.feed(self._X, self._t)
        if self.fake:
            self.board.set_target(self._rng.choice(self._targets))

    def _add_markers(self, trial_id: int, markers: list[tuple[int, float]]) -> None:
        if trial_id != self._trial_id or self._mode == "idle":
            return   # another trial's, or this one is over
        first = not self._marks
        for cycle, t in sorted(markers, key=lambda m: m[1]):
            if int(cycle) in self._marks or not math.isfinite(t):
                continue
            self._marks[int(cycle)] = float(t)
            self._events.append((float(t), "cycle", int(cycle)))
        if first and self._marks and self.fake:
            c0 = min(self._marks)
            self.board.start_flicker(self._marks[c0] - c0 * self.cycle_s)

    def _begin_training(self, train_id: int, trial_id: int, target: int, cycles: int) -> None:
        if trial_id != self._trial_id or self._mode == "idle":
            self._post(self._on_train, train_id, False, None, 0,
                       f"trial {trial_id} is not running")
            return
        if self._trainer is not None:
            self._fail_training(f"replaced by train {train_id}")
        try:
            trainer = CodeVepDecoder(self.fs, train_cycles=int(cycles), train_target=int(target),
                                     **self._kw)
        except ValueError as exc:
            self._post(self._on_train, train_id, False, None, 0, str(exc))
            return
        self._trainer, self._train_id = trainer, train_id
        self._mode = "train"
        # this trial so far: its samples (with the lead-in) and every marker
        events = sorted((t, "cycle", c) for c, t in self._marks.items())
        self._events = []
        trainer.feed(self._X, self._t, events)
        if self.fake:
            self.board.set_target(int(target))
        self._train_check()

    def _fail_training(self, why: str) -> None:
        trainer, train_id = self._trainer, self._train_id
        self._trainer = self._train_id = None
        if trainer is not None:
            log.warning("training %s failed: %s", train_id, why)
            self._post(self._on_train, train_id, False, None, trainer.progress[0], why)

    # -- samples -------------------------------------------------------------------

    def _take(self, X: np.ndarray, t: np.ndarray) -> None:
        """Add samples to the trial's buffer (a training that starts later reads it)."""
        self._X = np.concatenate([self._X, X], axis=1)
        self._t = np.concatenate([self._t, t])
        if self._t.size and self._t[-1] - self._t[0] > MAX_TRIAL_BUFFER_S:
            lo = int(np.searchsorted(self._t, self._t[-1] - MAX_TRIAL_BUFFER_S))
            self._X, self._t = self._X[:, lo:], self._t[lo:]

    def _data(self, X: np.ndarray, t: np.ndarray) -> None:
        if t.size:
            self._recent.append((X, t))
            cutoff = t[-1] - RECENT_S
            self._recent = [(x, s) for x, s in self._recent if s[-1] >= cutoff]
        if self._mode == "idle":
            return
        if t.size:
            # drop samples before the trial's lead-in, or already in the buffer
            start = self._t_start - self.pad_s
            if self._t.size:
                start = max(start, self._t[-1] + 1e-9)
            keep = t >= start
            if not keep.all():
                X, t = X[:, keep], t[keep]
        if t.size:
            self._take(X, t)
        events, self._events = self._events, []
        if self._mode == "train" and self._trainer is not None:
            self._trainer.feed(X, t, events)
            self._train_check()
        elif self.trained:
            self._decode(X, t, events)

    def _decode(self, X: np.ndarray, t: np.ndarray, events: list[tuple[float, str, int]]) -> None:
        assert self._voter is not None
        idx = self._targets
        for d in self.decoder.feed(X, t, events):
            scores = np.asarray(d.scores, dtype=float)
            sub = scores[idx]
            raw = idx[int(np.argmax(sub))]
            if len(idx) > 1 and self.margin > 0:
                ranked = np.sort(sub)
                if ranked[-1] - ranked[-2] < self.margin:
                    raw = ABSTAIN
            if self._voter.push(raw) and self._voter.committed != ABSTAIN:
                self._post(self._on_commit, self._trial_id, self._voter.committed, scores.copy())
                self._mode = "idle"   # one answer per trial: stop decoding (buffer kept)
                return

    # -- training ------------------------------------------------------------------

    def _train_check(self) -> None:
        trainer, train_id = self._trainer, self._train_id
        if trainer is None or trainer.training:
            return
        self._trainer = self._train_id = None
        self._mode = "idle"
        tpl = trainer.template
        assert tpl is not None
        acc = float(trainer.train_accuracy)
        self.decoder.use_template(tpl)
        self.decoder.train_accuracy = acc
        message = (f"template from {tpl.n_cycles} cycles of s{trainer.train_target + 1}, "
                   f"leave-one-out {100 * acc:.0f} %")
        try:
            self.template_path = self._save(tpl)
            message += f", saved to {self.template_path}"
        except OSError as exc:   # the template works without its file
            log.warning("could not save the c-VEP template: %s", exc)
            message += f"; not saved: {exc}"
        log.info("training %d: %s", train_id, message)
        self._post(self._on_train, train_id, True, acc, int(tpl.n_cycles), message)

    def _save(self, tpl: Template) -> Path:
        from ..paths import recordings_dir
        from ..record import reserve_path

        folder = Path(self.save_dir if self.save_dir is not None else recordings_dir())
        folder.mkdir(parents=True, exist_ok=True)
        return tpl.save(reserve_path(folder, time.time(), "cvep_template"))
