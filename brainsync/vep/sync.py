"""Marker-synchronous decoding: epochs cut at the display's cycle and flash onsets.

The c-VEP and t-VEP decoders are onset-locked: a cycle epoch starts at the display's
``cycle:<n>`` marker, a flash epoch at ``flash:<k>``, both Unix times on the same clock as
the board's sample times (``recordings.parse_events`` reads them from the stimulus log,
``vep.fake.SequenceFakeBoard.read_events`` hands them out live). ``cut_epochs`` does that
on arrays; ``SyncDecoder`` does it on a live stream: it buffers samples and marker events,
and once an epoch and ``pad_s`` of samples after it have arrived it filters the stretch
around the epoch (the same ``signal.preprocess`` as the other decoders, with the pad
absorbing the filter's edges), cuts the epoch and decides: a c-VEP decision from the mean
of the last ``cycles`` cycles against the template, a t-VEP decision from the flash
epochs of the last ``flash_window_s`` seconds. Decisions and votes are the same
``online.Decision`` / ``Voter`` as the sliding f-VEP decoder, so demos treat them alike.
A marker whose epoch is not in the buffer (before the stream began, or lost) gives no
decision: nothing is ever guessed without its onset.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from ..metrics import ABSTAIN
from ..online import Decision, Voter
from ..recordings import MONTAGE, POSTERIOR, Event, channel_indices
from ..signal import Band, preprocess
from .cvep import Template
from .tvep import FlashAverager

__all__ = ["SyncDecoder", "cut_epochs", "epochs_by_target"]


def cut_epochs(X: np.ndarray, t: np.ndarray, onsets: Sequence[float],
               n_samples: int) -> tuple[np.ndarray, list[int]]:
    """Epochs (m, n_ch, n_samples) starting at the first sample at or after each onset.

    Onsets before the first sample or whose epoch runs past the last are skipped; the
    second value lists the indices into ``onsets`` that were kept, in order.
    """
    X = np.asarray(X, dtype=float)
    t = np.asarray(t, dtype=float).reshape(-1)
    if X.ndim != 2 or X.shape[1] != t.size:
        raise ValueError(f"X must be (n_ch, n) with n == len(t), got {X.shape} and {t.size}")
    if int(n_samples) != n_samples or n_samples < 1:
        raise ValueError(f"n_samples must be a positive integer, got {n_samples!r}")
    out, kept = [], []
    for i, onset in enumerate(onsets):
        if t.size == 0 or onset < t[0]:
            continue
        i0 = int(np.searchsorted(t, onset))
        if i0 + n_samples <= t.size:
            out.append(X[:, i0:i0 + int(n_samples)])
            kept.append(i)
    epochs = np.stack(out) if out else np.empty((0, X.shape[0], int(n_samples)))
    return epochs, kept


def epochs_by_target(X: np.ndarray, t: np.ndarray, events: Iterable[Event], kind: str,
                     n_samples: int) -> dict[int, np.ndarray]:
    """``cut_epochs`` per marker index: {k: epochs} for every ``kind:k`` event."""
    groups: dict[int, list[float]] = {}
    for tt, name, k in events:
        if name == kind:
            groups.setdefault(int(k), []).append(float(tt))
    return {k: cut_epochs(X, t, onsets, n_samples)[0] for k, onsets in sorted(groups.items())}


class SyncDecoder:
    """Live c-VEP (``template``) or t-VEP (``averager``) decoding from samples and markers."""

    def __init__(self, fs: float, template: Template | None = None,
                 averager: FlashAverager | None = None, *,
                 channels: Sequence[str | int] | None = POSTERIOR,
                 ch_names: Sequence[str] = MONTAGE, band: Band | None = (1.0, 30.0),
                 notch_hz: float = 60.0, votes: int = 2, cycles: int = 2,
                 flash_window_s: float = 6.0, pad_s: float = 0.5, margin: float = 0.0,
                 keep_s: float | None = None) -> None:
        if (template is None) == (averager is None):
            raise ValueError("give exactly one of template (c-VEP) or averager (t-VEP)")
        self.fs = float(fs)
        if not np.isfinite(self.fs) or self.fs <= 0:
            raise ValueError(f"fs must be positive, got {fs}")
        for name, value in (("pad_s", pad_s), ("flash_window_s", flash_window_s)):
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")
        if int(cycles) != cycles or cycles < 1:
            raise ValueError(f"cycles must be a positive integer, got {cycles!r}")
        if margin < 0:
            raise ValueError(f"margin must be >= 0, got {margin}")
        self.template, self.averager = template, averager
        if template is not None and abs(template.fs - self.fs) > 1e-6:
            raise ValueError(f"template is at {template.fs} Hz, the decoder at {self.fs} Hz")
        if averager is not None and abs(averager.fs - self.fs) > 1e-6:
            raise ValueError(f"averager is at {averager.fs} Hz, the decoder at {self.fs} Hz")
        self.ch_names = tuple(str(c) for c in ch_names)
        self.rows = channel_indices(self.ch_names, channels)
        if template is not None and template.n_channels != len(self.rows):
            raise ValueError(f"template has {template.n_channels} channels, the decoder "
                             f"{len(self.rows)}")
        self.band, self.notch_hz = band, float(notch_hz)
        self.cycles, self.flash_window_s = int(cycles), float(flash_window_s)
        self.pad_s, self.margin = float(pad_s), float(margin)
        self.voter = Voter(votes)
        self.n_samples = template.n_samples if template is not None else averager.n_samples
        self.keep_s = (float(keep_s) if keep_s is not None else
                       max(self.flash_window_s, self.cycles * self.n_samples / self.fs)
                       + 2.0 * self.pad_s + 2.0)
        self.reset()

    @property
    def paradigm(self) -> str:
        return "code" if self.template is not None else "time"

    @property
    def kind(self) -> str:
        """The marker kind this decoder epochs on."""
        return "cycle" if self.template is not None else "flash"

    @property
    def n_targets(self) -> int:
        return self.template.n_targets if self.template is not None else self.averager.n_targets

    @property
    def votes(self) -> int:
        return self.voter.votes

    @property
    def committed(self) -> int:
        return self.voter.committed

    @property
    def n_seen(self) -> int:
        return self._n_seen

    def reset(self) -> None:
        """Forget samples, markers, epochs and votes (a new trial)."""
        self._X = np.empty((len(self.rows), 0))
        self._t = np.empty(0)
        self._pending: list[Event] = []
        self._cycles: list[np.ndarray] = []
        self._flashes: list[tuple[float, int, np.ndarray]] = []
        self._n_seen = 0
        self._dropped = 0
        self.voter.reset()

    @property
    def dropped(self) -> int:
        """Markers whose epoch was not in the buffer (no decision was made for them)."""
        return self._dropped

    # -- stream ------------------------------------------------------------------------------

    def feed(self, X: np.ndarray, t: np.ndarray, events: Iterable[Event] = ()) -> list[Decision]:
        """Add samples (all board rows) and markers; decide for every epoch now complete."""
        X = np.asarray(X, dtype=float)
        t = np.asarray(t, dtype=float).reshape(-1)
        if X.ndim != 2 or X.shape[1] != t.size:
            raise ValueError(f"X must be (n_ch, n) with n == len(t), got {X.shape} and {t.size}")
        if t.size:
            if X.shape[0] < max(self.rows) + 1:
                raise ValueError(f"X has {X.shape[0]} rows, the montage needs "
                                 f"{max(self.rows) + 1}")
            self._X = np.concatenate([self._X, X[self.rows]], axis=1)
            self._t = np.concatenate([self._t, t])
            self._n_seen += t.size
        for ev in events:
            if ev[1] == self.kind:
                self._pending.append((float(ev[0]), ev[1], int(ev[2])))
        self._pending.sort(key=lambda e: e[0])
        out: list[Decision] = []
        while self._pending and self._ready(self._pending[0][0]):
            onset, _, k = self._pending.pop(0)
            epoch = self._epoch(onset)
            if epoch is None:
                self._dropped += 1
                continue
            end = onset + self.n_samples / self.fs
            if self.template is not None:
                self._cycles.append(epoch)
                del self._cycles[:-self.cycles]
            else:
                self._flashes.append((onset, k, epoch))
                self._flashes = [f for f in self._flashes if f[0] >= end - self.flash_window_s]
            out.append(self._decide(end))
        self._trim()
        return out

    def _ready(self, onset: float) -> bool:
        return self._t.size > 0 and self._t[-1] >= onset + self.n_samples / self.fs + self.pad_s

    def _epoch(self, onset: float) -> np.ndarray | None:
        t0, t1 = onset - self.pad_s, onset + self.n_samples / self.fs + self.pad_s
        lo = int(np.searchsorted(self._t, t0))
        hi = int(np.searchsorted(self._t, t1, side="right"))
        if lo >= hi or onset < self._t[0]:
            return None
        seg = preprocess(self._X[:, lo:hi], self.fs, self.band, self.notch_hz)
        epochs, kept = cut_epochs(seg, self._t[lo:hi], [onset], self.n_samples)
        return epochs[0] if kept else None

    def _trim(self) -> None:
        if self._t.size == 0:
            return
        oldest_needed = self._t[-1] - self.keep_s
        if self._pending:
            oldest_needed = min(oldest_needed, self._pending[0][0] - self.pad_s)
        lo = int(np.searchsorted(self._t, oldest_needed))
        if lo > 0:
            self._X = self._X[:, lo:]
            self._t = self._t[lo:]

    def _decide(self, time: float) -> Decision:
        if self.template is not None:
            mean = np.mean(np.stack(self._cycles), axis=0)
            pred, scores = self.template.decide(mean, self.margin)
            raw, s = int(pred[0]), scores[0]
        else:
            groups: dict[int, list[np.ndarray]] = {}
            for _, k, epoch in self._flashes:
                groups.setdefault(k, []).append(epoch)
            raw, s = self.averager.decide({k: np.stack(v) for k, v in groups.items()})
        s = np.asarray(s, dtype=float)
        if np.isfinite(s).all():
            ranked = np.sort(s)
            gap = float(ranked[-1] - ranked[-2]) if ranked.size > 1 else float(ranked[-1])
            level = float(ranked[-1])
        else:                       # a target without enough flashes yet: no lead to report
            gap = level = float("nan")
        changed = self.voter.push(raw)
        return Decision(time=float(time), scores=s, raw=raw, committed=self.committed,
                        changed=changed, end=self._n_seen, gap=gap, level=level)


def first_commit(decisions: Sequence[Decision]) -> Decision | None:
    """The first decision that committed to a target, or None."""
    for d in decisions:
        if d.committed != ABSTAIN:
            return d
    return None
