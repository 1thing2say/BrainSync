"""A synthetic board that follows a coded stimulus: the fixated target's sequence,
convolved with a made-up VEP, on the posterior channels.

``board.FakeBoard`` plays a tone at a frequency, which is all an f-VEP decoder needs. The
c-VEP and t-VEP decoders need the response to line up with the display's cycle and flash
markers, so this board runs its own frame grid: ``start_flicker`` begins the sequence at
the next sample, frame i starts ``i / refresh`` seconds later (not snapped to a sample,
like a real flip), ``read_events`` hands out the markers of every frame shown so far
(``stim_on:<k>`` with the fixated target, then the sequence's ``cycle:<n>`` or
``flash:<k>``), and ``read`` returns the EEG: Gaussian noise on all 8 channels plus
``amp`` times the kernel-filtered stimulus of the fixated target on P7, P8, O1, O2. It is a
check of the pipeline, not of anyone's visual cortex, and an easy one: only the fixated
target evokes anything, while the paper's peripheral, non-fixated targets evoke smaller
responses too, which the real decoders have to reject. Time moves like FakeBoard's: through
``advance`` (virtual) or a ``clock``.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence

import numpy as np

from ..board import FS, N_CHANNELS, POSTERIOR_ROWS
from ..recordings import MONTAGE, Event
from ..stimulus.coded import Sequence as StimulusSequence
from ..synthetic import vep_kernel

__all__ = ["SequenceFakeBoard", "markers_text"]


def markers_text(events: Sequence[Event], refresh: float = 60.0) -> str:
    """The stimulus log text ``recordings.parse_events`` reads, from (t, kind, k) events."""
    lines = [f"measured refresh: {refresh:.2f} Hz"]
    lines += [f"[marker {t:.6f}] {kind}:{k}" for t, kind, k in events]
    return "\n".join(lines) + "\n"


class SequenceFakeBoard:
    """Synthetic 8-channel board driven by a CodeSequence or TimeSequence."""

    def __init__(self, seq: StimulusSequence, fs: float = FS, refresh: float = 60.0,
                 fixated: int = 0, amp: float = 2.0, noise: float = 8.0, seed: int = 0,
                 clock: Callable[[], float] | None = None,
                 ch_names: Sequence[str] | None = None, t0: float | None = None,
                 kernel: np.ndarray | None = None) -> None:
        if fs <= 0 or refresh <= 0:
            raise ValueError(f"fs and refresh must be positive, got {fs}, {refresh}")
        if amp < 0 or noise < 0:
            raise ValueError(f"amp and noise must be >= 0, got {amp}, {noise}")
        self.seq = seq
        self.n_targets = int(np.asarray(seq.still).size)
        self.fs, self.refresh = float(fs), float(refresh)
        self.amp, self.noise = float(amp), float(noise)
        names = tuple(MONTAGE) if ch_names is None else tuple(str(c) for c in ch_names)
        if len(names) != N_CHANNELS:
            raise ValueError(f"need {N_CHANNELS} channel names, got {len(names)}")
        self.ch_names = names
        self.kernel = vep_kernel(self.fs) if kernel is None else np.asarray(kernel, float)
        if self.kernel.ndim != 1 or self.kernel.size == 0:
            raise ValueError("kernel must be a non-empty 1-d array")
        self.clock = clock
        self._rng = np.random.default_rng(seed)
        self.gains = self._rng.uniform(0.7, 1.3, len(POSTERIOR_ROWS))
        start = clock() if clock is not None else time.time()
        self.t0 = float(start if t0 is None else t0)
        self._clock0 = start
        self._n = 0
        self._due_s = 0.0
        self._pending: list[tuple[np.ndarray, np.ndarray]] = []
        self._events: list[Event] = []
        self._hist = np.zeros(self.kernel.size - 1)
        self._start: int | None = None     # sample index of flicker frame 0, None: still
        self._frames_shown = 0
        self._fixated = 0
        self.set_target(fixated)

    # -- the FakeBoard surface -------------------------------------------------------------

    def __enter__(self) -> SequenceFakeBoard:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def open(self) -> SequenceFakeBoard:
        return self

    def close(self) -> None:
        pass

    @property
    def virtual(self) -> bool:
        return self.clock is None

    @property
    def elapsed(self) -> float:
        return self._n / self.fs

    @property
    def now(self) -> float:
        """Unix time of the next sample."""
        return self.t0 + self._n / self.fs

    # -- what the fake user does -------------------------------------------------------------

    @property
    def fixated(self) -> int:
        return self._fixated

    def set_target(self, target: int) -> None:
        """Look at ``target`` (-1: at none of them; no response)."""
        if int(target) != target or not -1 <= int(target) < self.n_targets:
            raise ValueError(f"target must be -1..{self.n_targets - 1}, got {target}")
        self._fixated = int(target)

    @property
    def flickering(self) -> bool:
        return self._start is not None

    def start_flicker(self) -> None:
        """Start the sequence (from its frame 0) at the next sample."""
        self.seq.restart()
        self._start = self._n
        self._frames_shown = 0

    def stop_flicker(self) -> None:
        self._start = None

    def frame_time(self, frame: int) -> float:
        """Unix time of a flicker frame's onset in the current run."""
        if self._start is None:
            raise ValueError("the flicker is not running")
        return self.t0 + (self._start + frame * self.fs / self.refresh) / self.fs

    def read_events(self) -> list[Event]:
        """Markers of the frames shown since the previous call: (unix time, kind, k)."""
        out, self._events = self._events, []
        return out

    # -- samples -------------------------------------------------------------------------------

    def _frames_of(self, idx: np.ndarray) -> np.ndarray:
        assert self._start is not None
        return np.floor((idx - self._start) * self.refresh / self.fs + 1e-9).astype(int)

    def _generate(self, n: int) -> None:
        if n <= 0:
            return
        idx = self._n + np.arange(n)
        stim = np.zeros(n)
        if self._start is not None:
            frames = self._frames_of(idx)
            last = int(frames[-1])
            if self._fixated >= 0:
                k = self._fixated
                still = float(np.asarray(self.seq.still)[k])
                lum = {f: float(self.seq.luminance(f)[k]) - still
                       for f in range(int(frames[0]), last + 1)}
                stim = np.array([lum[int(f)] for f in frames])
            for f in range(self._frames_shown, last + 1):
                t = self.frame_time(f)
                if f == 0:
                    self._events.append((t, "stim_on", self._fixated))
                for ev in self.seq.events(f):
                    kind, _, num = ev.partition(":")
                    self._events.append((t, kind, int(num)))
            self._frames_shown = last + 1
        both = np.concatenate([self._hist, stim])
        response = np.convolve(both, self.kernel)[self._hist.size:self._hist.size + n]
        if self.kernel.size > 1:
            self._hist = both[-(self.kernel.size - 1):]
        X = self.noise * self._rng.standard_normal((N_CHANNELS, n))
        X[list(POSTERIOR_ROWS)] += self.amp * self.gains[:, None] * response[None, :]
        self._pending.append((X, self.t0 + idx / self.fs))
        self._n += n

    def advance(self, seconds: float) -> None:
        """Generate ``seconds`` more data (virtual time; not with a clock)."""
        if self.clock is not None:
            raise ValueError("advance() is for a board without a clock; this one has one")
        if seconds < 0:
            raise ValueError(f"cannot advance by a negative time, got {seconds}")
        self._due_s += float(seconds)
        self._generate(int(math.floor(self._due_s * self.fs + 1e-9)) - self._n)

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        """Every sample since the previous read: X in µV (8, n), t unix seconds (n,)."""
        if self.clock is not None:
            due = int(math.floor((self.clock() - self._clock0) * self.fs + 1e-9))
            self._generate(due - self._n)
        if not self._pending:
            return np.empty((N_CHANNELS, 0)), np.empty(0)
        X = np.concatenate([x for x, _ in self._pending], axis=1)
        t = np.concatenate([s for _, s in self._pending])
        self._pending = []
        return X, t
