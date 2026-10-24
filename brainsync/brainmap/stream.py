"""From board samples to map frames: a sliding window, one frame per hop, and a polling loop.

    stream = MapStream(board.fs, board.ch_names, source="cerelog")
    server.set_hello(stream.hello())          # what the page needs before the first frame
    with board:
        run(board, stream, server.publish)    # until Ctrl-C (or stop() turns true)

``MapStream.push`` takes whatever the board returned (raw µV, any chunk size), filters it
causally with the filter state carried across chunks, and returns one frame per ``hop_s``
once ``window_s`` seconds are buffered. Frames are due on a fixed sample grid (at 250 Hz a
0.25 s hop alternates 62 and 63 samples), so the output does not depend on how the samples
were chunked, and every frame describes the window ending at the sample whose time it
carries. Windows that start in the first ``FILTER_SETTLE_S`` seconds, while the filters
settle, are shown but kept out of the rest baseline. Band power is smoothed with an
exponential moving average (``smooth`` is the weight of the previous frame) and the SSVEP
SNR comes from the PSD averaged over the last ``SNR_AVG_S`` seconds of hops; both restart
whenever a channel's contact goes bad, so one clipped window does not linger in the next
ones.

``run`` polls the board, pushes, and emits each frame plus a status message whenever the
state changes. ``ReplayBoard`` plays a recording back through the same path, at real time
or ``speed`` times faster, so the page can be tried without the headset.

Messages are plain JSON-ready dicts (see the hello/frame/status shapes in ``hello`` and
``_frame``); NaN never appears, a missing value is None. ``hello["ssvep_keys"]`` are the
exact keys of ``frame["ssvep"]``, in ``ssvep_hz`` order.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ..check import segment_problem
from ..online import RingBuffer
from ..recordings import Recording, load_recording
from . import layout
from .features import (BANDS, SNR_AVG_S, SNR_GUARD_BINS, SNR_SPAN_HZ, Baseline, CausalFilter,
                       SsvepSnr, window_features)

FORMAT = 1
LABEL = "Your EEG · 8 electrodes · scalp map, not a brain scan"
DECIMALS = 2  # dB and z to 0.01: plenty for a colour map, and small frames
LOOP_LEVEL_S = 0.5     # ReplayBoard --loop matches the mean level of this much at each end
FILTER_SETTLE_S = 1.0  # windows starting earlier hold the filters' start-up transient


def _num(v: float) -> float | None:
    return round(float(v), DECIMALS) + 0.0 if math.isfinite(v) else None


def _row(a: np.ndarray) -> list[float | None]:
    return [_num(v) for v in a]


def _hz_key(f: float) -> str:
    return f"{f:g}"


class MapStream:
    """Turns a raw multichannel stream into hello and frame messages for the map page."""

    def __init__(self, fs: float, ch_names: Sequence[str], *, window_s: float = 2.0,
                 hop_s: float = 0.25, baseline_s: float = 30.0, smooth: float = 0.5,
                 ssvep_hz: Sequence[float] = (15.0, 17.0), line_hz: float = 60.0,
                 source: str = "fake") -> None:
        fs = float(fs)
        if not (math.isfinite(fs) and fs > 0):
            raise ValueError(f"fs must be positive, got {fs}")
        self.ch_names = tuple(str(c) for c in ch_names)
        self.positions = layout.positions(self.ch_names)
        self.regions = layout.regions(self.ch_names)
        if not (math.isfinite(window_s) and window_s > 0):
            raise ValueError(f"window_s must be positive, got {window_s}")
        reason = segment_problem(window_s, fs, SNR_SPAN_HZ, SNR_GUARD_BINS)
        if reason:
            raise ValueError(f"window_s {window_s:g} is too short: {reason}")
        if not (math.isfinite(hop_s) and hop_s * fs >= 1):
            raise ValueError(f"hop_s must be at least one sample (1/{fs:g} s), got {hop_s}")
        if not 0 <= smooth < 1:
            raise ValueError(f"smooth must lie in [0, 1), got {smooth}")
        self.ssvep_hz = tuple(float(f) for f in ssvep_hz)
        if any(not 0 < f < fs / 2 for f in self.ssvep_hz):
            raise ValueError(f"ssvep_hz must lie in (0, {fs / 2:g}) Hz, got {list(self.ssvep_hz)}")
        if len({_hz_key(f) for f in self.ssvep_hz}) != len(self.ssvep_hz):
            raise ValueError(f"ssvep_hz must not repeat, got {list(self.ssvep_hz)}")
        self.fs = fs
        self.window_s = float(window_s)
        self.hop_s = float(hop_s)
        self.baseline_s = float(baseline_s)
        self.smooth = float(smooth)
        self.line_hz = float(line_hz)
        self.source = str(source)
        self.bands = dict(BANDS)
        n_ch = len(self.ch_names)
        self.ssvep_keys = [_hz_key(f) for f in self.ssvep_hz]
        self.baseline = Baseline(self.baseline_s, n_ch, len(self.bands), hop_s=self.hop_s,
                                 window_s=self.window_s)
        self._window_n = int(round(self.window_s * fs))
        self._settle_n = int(round(FILTER_SETTLE_S * fs))
        self._filter = CausalFilter(fs, n_ch, line_hz=self.line_hz)
        self._raw = RingBuffer(n_ch, self._window_n)       # rail and DC checks
        self._buf = RingBuffer(n_ch, self._window_n)       # filtered: spectra and RMS
        self._snr: SsvepSnr | None = None                  # needs the first window's bins
        self._snr_n = max(1, int(round(SNR_AVG_S / self.hop_s)))
        self._total = 0          # samples pushed so far
        self._seq = 0            # index of the next frame
        self._due = self._due_at(0)
        self._ema_db: np.ndarray | None = None
        self._ema_ok = np.zeros(n_ch, dtype=bool)
        self.last: Any = None    # the newest frame's WindowFeatures, for tests and tools

    @property
    def samples(self) -> int:
        """Samples pushed so far."""
        return self._raw.total

    def _due_at(self, k: int) -> int:
        """Samples pushed when frame ``k`` is due: the window plus k hops, rounded."""
        return self._window_n + int(math.floor(k * self.hop_s * self.fs + 0.5))

    def hello(self) -> dict:
        return {
            "type": "hello", "format": FORMAT, "source": self.source,
            "channels": list(self.ch_names),
            "positions": self.positions, "regions": self.regions,
            "bands": {name: [float(lo), float(hi)] for name, (lo, hi) in self.bands.items()},
            "ssvep_hz": list(self.ssvep_hz), "ssvep_keys": list(self.ssvep_keys),
            "fs": self.fs, "window_s": self.window_s, "hop_s": self.hop_s,
            "baseline_s": self.baseline_s, "label": LABEL,
        }

    def push(self, X: np.ndarray, t: np.ndarray) -> list[dict]:
        """Feed raw µV ``X`` (n_ch, n) with unix times ``t`` (n,); the frames now due."""
        X = np.asarray(X, dtype=float)
        t = np.asarray(t, dtype=float).reshape(-1)
        if X.ndim != 2 or X.shape[0] != len(self.ch_names):
            raise ValueError(f"expected ({len(self.ch_names)}, n) samples, got shape {X.shape}")
        if t.shape[0] != X.shape[1]:
            raise ValueError(f"{X.shape[1]} samples but {t.shape[0]} times")
        frames: list[dict] = []
        i, n = 0, X.shape[1]
        while i < n:
            take = min(n - i, self._due - self._total)
            chunk = X[:, i:i + take]
            self._raw.append(chunk)
            self._buf.append(self._filter(chunk))
            self._total += take
            i += take
            if self._total == self._due:
                frames.append(self._frame(float(t[i - 1])))
                self._seq += 1
                self._due = self._due_at(self._seq)
        return frames

    def _smoothed(self, prev: np.ndarray | None, new: np.ndarray, ok: np.ndarray) -> np.ndarray:
        if prev is None:
            return new
        carry = ok & self._ema_ok
        return np.where(carry, self.smooth * prev + (1.0 - self.smooth) * new, new)

    def _frame(self, t_last: float) -> dict:
        f = window_features(self._raw.last(self._window_n), self.fs,
                            filtered=self._buf.last(self._window_n), bands=self.bands,
                            ssvep_hz=self.ssvep_hz, line_hz=self.line_hz)
        self.last = f
        ok = f.ok
        if self._snr is None:
            self._snr = SsvepSnr(f.freqs, self.ssvep_hz, len(self.ch_names), self._snr_n)
        snr = self._snr.update(f.power, ok)
        db = self._smoothed(self._ema_db, f.db, ok)
        self._ema_db, self._ema_ok = db, ok
        # the baseline takes the window's own dB, not the smoothed one: a clean window right
        # after a blink would otherwise carry half the blink into the rest statistics
        settled = self._total - self._window_n >= self._settle_n
        self.baseline.add(f.db, ok, self._seq * self.hop_s, ptp=f.ptp, settled=settled)
        z = np.where(ok, self.baseline.z(db), np.nan)
        return {
            "type": "frame", "seq": self._seq, "t": t_last,
            "z": {name: _row(z[i]) for i, name in enumerate(self.bands)},
            "db": {name: _row(db[i]) for i, name in enumerate(self.bands)},
            "ssvep": {key: _row(snr[i]) for i, key in enumerate(self.ssvep_keys)},
            "quality": list(f.quality),
            "baseline": self.baseline.status(),
        }


def status(state: str, text: str) -> dict:
    return {"type": "status", "state": state, "text": text}


def run(board: Any, stream: MapStream, emit: Callable[[dict], None], *,
        stop: Callable[[], bool] = lambda: False, poll_s: float = 0.05, stall_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic) -> str:
    """Poll ``board`` until ``stop()`` or a replay ends; returns "stopped" or "ended".

    Emits a "starting" status, then "streaming" when samples flow, "no-data" after
    ``stall_s`` seconds without any (polling goes on: a live board may come back) and
    "ended" when a board with a ``finished`` flag runs out. A board error is emitted as an
    "error" status and re-raised; so is Ctrl-C, without the status. A FakeBoard without a
    clock runs in virtual time, as in ``board.record``.
    """
    if not (poll_s > 0 and stall_s > 0):
        raise ValueError(f"poll_s and stall_s must be positive, got {poll_s}, {stall_s}")
    if getattr(board, "virtual", False):
        if sleep is time.sleep:
            sleep = board.advance
        if clock is time.monotonic:
            clock = lambda: board.elapsed  # noqa: E731
    state = "starting"
    emit(status(state, f"waiting for samples from the {stream.source} board"))
    heard = clock()

    def change(new: str, text: str) -> None:
        nonlocal state
        if new != state:
            state = new
            emit(status(new, text))

    try:
        while True:
            X, t = board.read()
            if X.shape[1]:
                heard = clock()
                change("streaming", f"{len(stream.ch_names)} channels at {stream.fs:g} Hz")
                for frame in stream.push(X, t):
                    emit(frame)
            elif getattr(board, "finished", False):
                change("ended", "the recording has finished")
                return "ended"
            elif clock() - heard > stall_s:
                change("no-data", f"no samples for over {stall_s:g} s; is the board on "
                                  "and the USB cable in?")
            if stop():
                return "stopped"
            sleep(poll_s)
    except Exception as exc:  # not KeyboardInterrupt: Ctrl-C is the normal way out
        emit(status("error", str(exc) or type(exc).__name__))
        raise


class ReplayBoard:
    """A Board that plays a recording back as if it were streaming live.

    ``recording`` is a Recording, a path to one, or (X µV (n_ch, n), t, fs, ch_names). The
    clock starts at ``open()`` (or the first read); each ``read`` returns the samples due
    since the previous one at ``speed`` times real time. With ``loop`` it starts over when
    it reaches the end, shifting the times so they keep increasing; otherwise ``finished``
    turns true once everything has been read.

    A recording's DC drifts tens of mV from start to end, so looping it as is would put a
    step of that size at every seam, which rings through the high-pass for seconds. With
    ``loop`` each channel's offset therefore changes linearly over the lap, by what brings
    the mean of the lap's last ``LOOP_LEVEL_S`` to that of its first: every lap is the same,
    and the seam is one ordinary sample step. Matching the two end samples instead would
    leave a slow jump the size of the noise on them, which shows as delta power. A
    constant offset per lap would close the seam too, but it piles up, and a few laps
    later the DC trips the "offset" and "clipped" checks; correcting only the laps after
    the first would leave a kink that rings as well. The ramp spans the whole recording,
    far below the 1 Hz high-pass, so the spectra do not change.
    """

    def __init__(self, recording: Recording | str | Path | tuple, *, speed: float = 1.0,
                 clock: Callable[[], float] = time.monotonic, loop: bool = False) -> None:
        if isinstance(recording, (str, Path)):
            recording = load_recording(recording)
        if isinstance(recording, Recording):
            X, t, fs, names = recording.X, recording.t, recording.fs, recording.ch_names
        else:
            X, t, fs, names = recording
        X = np.asarray(X, dtype=float)
        t = np.asarray(t, dtype=float).reshape(-1)
        fs = float(fs)
        if X.ndim != 2 or X.shape[1] != t.shape[0] or X.shape[0] != len(names):
            raise ValueError(f"inconsistent recording: X{X.shape} t{t.shape} "
                             f"{len(names)} channel names")
        if X.shape[1] == 0:
            raise ValueError("the recording holds no samples")
        if not (math.isfinite(fs) and fs > 0):
            raise ValueError(f"fs must be positive, got {fs}")
        if not (math.isfinite(speed) and speed > 0):
            raise ValueError(f"speed must be positive, got {speed}")
        self.fs = fs
        self.ch_names = tuple(str(c) for c in names)
        self.speed = float(speed)
        self.loop = bool(loop)
        self.clock = clock
        self._lap_s = float(t[-1] - t[0]) + 1.0 / fs  # time shift per loop
        n = X.shape[1]
        k = min(int(round(LOOP_LEVEL_S * fs)), n // 2)
        if self.loop and k >= 1:
            drift = (X[:, -k:].mean(axis=1) - X[:, :k].mean(axis=1)) * (n - 1) / (n - k)
            X = X - drift[:, None] * (np.arange(n) / (n - 1))[None, :]
        self._X, self._t = X, t
        self._pos = 0          # samples handed out, counting every loop
        self._start: float | None = None

    @property
    def n_samples(self) -> int:
        return self._X.shape[1]

    @property
    def duration_s(self) -> float:
        return self.n_samples / self.fs

    @property
    def finished(self) -> bool:
        return not self.loop and self._pos >= self.n_samples

    def open(self) -> ReplayBoard:
        if self._start is None:
            self._start = self.clock()
        return self

    def close(self) -> None:
        pass

    def __enter__(self) -> ReplayBoard:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        """Every sample due since the previous read: X in µV (n_ch, n), t unix seconds (n,)."""
        self.open()
        due = int(math.floor((self.clock() - self._start) * self.speed * self.fs + 1e-9))
        if not self.loop:
            due = min(due, self.n_samples)
        if due <= self._pos:
            return np.empty((len(self.ch_names), 0)), np.empty(0)
        idx = np.arange(self._pos, due)
        self._pos = due
        lap, col = np.divmod(idx, self.n_samples)
        return self._X[:, col], self._t[col] + lap * self._lap_s
