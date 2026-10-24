"""The EEG trace strip under the BrainChess panels: live lanes and the decoder windows.

Two halves, one per process. ``TraceFeed`` runs in the game: it turns each board read
into a few display samples per decoder channel (block means down to about 50 Hz, which
also smooths out mains hum, minus a slow running mean so drift does not push a lane off
its strip). ``TraceBuffer`` runs in the board stimulus: the reader thread adds samples
and decoder windows to it and the render loop takes a ``TraceView`` of the last few
seconds, which ``lane_points`` and ``window_marks`` turn into geometry.

Times are stream seconds: decoder samples fed / fs, the clock of ``Decision.end``. The
stimulus has no such clock of its own, so it extrapolates from the newest stream time it
was sent, by at most ``lead_s``, so the strip scrolls smoothly between board reads.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

__all__ = ["TraceFeed", "Window", "TraceView", "TraceBuffer", "lane_points", "window_marks",
           "DISPLAY_RATE", "MAX_LANES"]

DISPLAY_RATE = 50.0     # display samples per second
DRIFT_TAU_S = 0.4       # time constant of the running mean taken off each lane
MAX_LANES = 8
ABSTAIN = -1


class TraceFeed:
    """Board reads -> display samples of the decoder's channels (game side).

    ``rows`` are the rows of the board's X to show (``Decoder.rows``), ``fs`` its rate.
    """

    def __init__(self, rows: Sequence[int], fs: float, rate: float = DISPLAY_RATE,
                 tau_s: float = DRIFT_TAU_S) -> None:
        if not rows:
            raise ValueError("TraceFeed needs at least one channel row")
        if not fs > 0 or not rate > 0 or not tau_s > 0:
            raise ValueError(f"fs, rate and tau_s must be positive, got {fs}, {rate}, {tau_s}")
        self.rows = [int(r) for r in rows]
        self.fs = float(fs)
        self.factor = max(1, int(round(self.fs / rate)))
        self.dt = self.factor / self.fs
        self.alpha = 1.0 - math.exp(-self.dt / tau_s)
        self._carry = np.empty((len(self.rows), 0))
        self._mean: np.ndarray | None = None

    def push(self, X: np.ndarray, end: int) -> tuple[float, np.ndarray] | None:
        """Take the newest board samples X (n_ch, n); ``end`` is the stream position just
        after them (``Decoder.n_seen`` once they are fed). Returns (stream time of the last
        display sample, samples (k, n_rows)), or None when no full block is ready yet."""
        x = np.concatenate([self._carry, np.asarray(X, dtype=float)[self.rows]], axis=1)
        k = x.shape[1] // self.factor
        if k == 0:
            self._carry = x
            return None
        used = k * self.factor
        blocks = x[:, :used].reshape(len(self.rows), k, self.factor).mean(axis=2).T
        self._carry = x[:, used:]
        out = np.empty_like(blocks)
        mean = blocks[0].copy() if self._mean is None else self._mean
        for j, b in enumerate(blocks):
            mean = mean + self.alpha * (b - mean)
            out[j] = b - mean
        self._mean = mean
        return (end - self._carry.shape[1]) / self.fs, out


@dataclass(frozen=True)
class Window:
    """One decoder window: when it ended (stream s), what it named (0, 1 or -1 for no
    answer), whether the gate counted it (False: too early after a screen change, or the
    board was still) and whether it fired a pick."""

    t: float
    raw: int
    fresh: bool
    fired: bool


@dataclass(frozen=True)
class TraceView:
    """The last ``span`` seconds up to ``t_now``: samples (times (m,), values (m, lanes))
    and windows, oldest first. ``t_now`` is None before any data arrived."""

    t_now: float | None
    times: np.ndarray
    values: np.ndarray
    windows: tuple[Window, ...]
    step: float


class TraceBuffer:
    """Samples and decoder windows for the strip (stimulus side; thread-safe)."""

    def __init__(self, n_lanes: int = 4, span_s: float = 6.0, lead_s: float = 0.3,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.span_s = float(span_s)
        self.lead_s = float(lead_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._n_lanes = 0
        self._chunks: deque[tuple[np.ndarray, np.ndarray]] = deque()
        self._windows: deque[Window] = deque()
        self._latest: float | None = None
        self._heard = 0.0
        self.set_lanes(n_lanes)

    @property
    def n_lanes(self) -> int:
        return self._n_lanes

    def set_lanes(self, n: int) -> None:
        """Start over with ``n`` lanes (drops the samples, keeps the windows)."""
        if not 1 <= n <= MAX_LANES:
            raise ValueError(f"need 1..{MAX_LANES} lanes, got {n}")
        with self._lock:
            self._n_lanes = int(n)
            self._chunks.clear()

    def _heard_at(self, t: float) -> None:
        if self._latest is None or t >= self._latest:
            self._latest = t
            self._heard = self._clock()

    def _prune(self) -> None:
        assert self._latest is not None
        oldest = self._latest - self.span_s - 1.0
        while self._chunks and self._chunks[0][0][-1] < oldest:
            self._chunks.popleft()
        while self._windows and self._windows[0].t < oldest:
            self._windows.popleft()

    def add_samples(self, t_end: float, dt: float, values: np.ndarray) -> None:
        """Samples spaced ``dt`` apart, the last at stream time ``t_end``; ``values`` flat
        (row-major) or (k, n_lanes)."""
        if not (math.isfinite(t_end) and dt > 0):
            raise ValueError(f"need a finite end time and dt > 0, got {t_end}, {dt}")
        with self._lock:
            v = np.asarray(values, dtype=float).reshape(-1)
            if v.size == 0 or v.size % self._n_lanes:
                raise ValueError(f"{v.size} values do not fill rows of {self._n_lanes} lanes")
            v = v.reshape(-1, self._n_lanes)
            times = t_end - dt * np.arange(len(v) - 1, -1, -1)
            self._chunks.append((times, v))
            self._heard_at(t_end)
            self._prune()

    def add_window(self, w: Window) -> None:
        with self._lock:
            self._windows.append(w)
            self._heard_at(w.t)
            self._prune()

    def now(self) -> float | None:
        """Stream time to draw up to: the newest time heard, run on by the wall clock for
        at most ``lead_s``."""
        with self._lock:
            return self._now()

    def _now(self) -> float | None:
        if self._latest is None:
            return None
        return self._latest + min(max(self._clock() - self._heard, 0.0), self.lead_s)

    def view(self) -> TraceView:
        with self._lock:
            t_now = self._now()
            if self._chunks:
                times = np.concatenate([c[0] for c in self._chunks])
                values = np.concatenate([c[1] for c in self._chunks])
            else:
                times, values = np.empty(0), np.empty((0, self._n_lanes))
            windows = tuple(self._windows)
        if t_now is not None and times.size:
            keep = times >= t_now - self.span_s
            times, values = times[keep], values[keep]
        gaps = np.diff([w.t for w in windows[-8:]])
        gaps = gaps[gaps > 1e-6]
        step = float(np.min(gaps)) if gaps.size else 0.25
        return TraceView(t_now, times, values, windows, step)


def lane_points(view: TraceView, x0: float, x1: float, top: float, bottom: float,
                span_s: float, fill: float = 0.42) -> list[np.ndarray]:
    """One polyline (m, 2) per lane, in screen units, lanes stacked from ``top`` down to
    ``bottom``. Each lane is centred on its median and scaled to its own spread over the
    view (3 robust standard deviations reach ``fill`` of half a lane), clipped to it."""
    n = view.values.shape[1] if view.values.ndim == 2 else 0
    if view.t_now is None or len(view.times) < 2 or n == 0:
        return [np.empty((0, 2)) for _ in range(n)]
    x = x0 + (view.times - (view.t_now - span_s)) / span_s * (x1 - x0)
    lane = (top - bottom) / n
    out = []
    for i in range(n):
        v = view.values[:, i]
        centre = float(np.median(v))
        spread = 1.4826 * float(np.median(np.abs(v - centre)))
        scale = 3.0 * spread if spread > 1e-9 else 1.0
        mid = top - lane * (i + 0.5)
        y = mid + np.clip((v - centre) / scale, -1.0, 1.0) * lane * fill
        out.append(np.column_stack([x, y]))
    return out


def window_marks(view: TraceView, x0: float, x1: float, span_s: float,
                 gap: float = 0.003) -> list[tuple[float, float, Window]]:
    """(left x, width, window) for each window in view: a tick one step wide that ends at
    the window's end time, clipped to the strip."""
    if view.t_now is None:
        return []
    per_s = (x1 - x0) / span_s
    width = max(view.step * per_s - gap, 0.002)
    t0 = view.t_now - span_s
    out = []
    for w in view.windows:
        right = x0 + (w.t - t0) * per_s
        left = right - width
        if right <= x0 or left >= x1:
            continue
        left, right = max(left, x0), min(right, x1)
        out.append((left, right - left, w))
    return out
