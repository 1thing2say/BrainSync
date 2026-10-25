"""The c-VEP decoder: one trained template, shifted per target, matched by correlation.

Bin et al. (2009), section 2.3: fixate one target for N stimulation cycles, average the
cycles into a template T(t), build every target's template by shifting it by that
target's lag, and pick the target whose template correlates best with the new cycle.
Because every target shows the same m-sequence at a different lag, the responses differ
only by that time shift, which is why one template is enough.

``fit_template`` accepts cycles from any cued target (each is shifted back to the base
target's alignment before averaging), so a cued recording of every target trains it, and
``cross_validate`` is the paper's training accuracy with each cycle held out in turn.
Correlation is Pearson's r per channel, averaged over the channels given.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..metrics import ABSTAIN

__all__ = ["Template", "cross_validate", "fit_template", "lag_samples"]


def lag_samples(lag_frames: int, fs: float, refresh: float) -> float:
    """Samples between consecutive targets' sequences."""
    if fs <= 0 or refresh <= 0:
        raise ValueError(f"fs and refresh must be positive, got {fs}, {refresh}")
    return float(lag_frames) * float(fs) / float(refresh)


def _cycles(x: np.ndarray, what: str = "cycles") -> np.ndarray:
    c = np.asarray(x, dtype=float)
    if c.ndim == 2:
        c = c[np.newaxis]
    if c.ndim != 3 or c.shape[0] == 0 or c.shape[2] < 2:
        raise ValueError(f"{what} must be (n_cycles, n_ch, n_samples) with n_samples >= 2, "
                         f"got shape {c.shape}")
    if not np.isfinite(c).all():
        raise ValueError(f"{what} hold NaN/inf values")
    return c


def _corr(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pearson r along the last axis, broadcast over the leading ones; 0 where flat."""
    a = a - a.mean(axis=-1, keepdims=True)
    b = b - b.mean(axis=-1, keepdims=True)
    num = np.sum(a * b, axis=-1)
    den = np.sqrt(np.sum(a * a, axis=-1) * np.sum(b * b, axis=-1))
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(den > 0, num / den, 0.0)
    return r


@dataclass
class Template:
    """The averaged response to target ``base`` over one cycle, (n_ch, n_samples)."""

    mean: np.ndarray
    base: int
    n_targets: int
    lag_samples: float
    fs: float
    n_cycles: int = 0
    ch_names: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.mean = np.asarray(self.mean, dtype=float)
        if self.mean.ndim != 2 or self.mean.shape[1] < 2:
            raise ValueError(f"template must be (n_ch, n_samples), got {self.mean.shape}")
        if not 0 <= self.base < self.n_targets:
            raise ValueError(f"base target {self.base} is not among {self.n_targets} targets")
        if self.lag_samples <= 0 and self.n_targets > 1:
            raise ValueError(f"lag_samples must be positive, got {self.lag_samples}")
        if (self.n_targets - 1) * self.lag_samples >= self.mean.shape[1] + 0.5:
            raise ValueError(f"{self.n_targets} targets {self.lag_samples:g} samples apart do "
                             f"not fit in a {self.mean.shape[1]}-sample cycle")
        if self.ch_names and len(self.ch_names) != self.mean.shape[0]:
            raise ValueError(f"{len(self.ch_names)} channel names for {self.mean.shape[0]} rows")

    @property
    def n_samples(self) -> int:
        return int(self.mean.shape[1])

    @property
    def n_channels(self) -> int:
        return int(self.mean.shape[0])

    def shift(self, target: int) -> int:
        """Samples target ``target``'s response lags the base target's (negative: leads)."""
        return int(round((target - self.base) * self.lag_samples))

    def templates(self) -> np.ndarray:
        """Every target's template (n_targets, n_ch, n_samples): the base shifted by its lag."""
        return np.stack([np.roll(self.mean, self.shift(k), axis=-1)
                         for k in range(self.n_targets)])

    def scores(self, cycles: np.ndarray) -> np.ndarray:
        """Channel-mean correlation with each target's template, (n_cycles, n_targets)."""
        c = _cycles(cycles)
        if c.shape[1:] != self.mean.shape:
            raise ValueError(f"cycles are {c.shape[1:]}, the template {self.mean.shape}")
        r = _corr(c[:, None, :, :], self.templates()[None])   # (n_cycles, n_targets, n_ch)
        return r.mean(axis=-1)

    def decide(self, cycles: np.ndarray, margin: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Best target per cycle (ABSTAIN when its lead over the runner-up is below
        ``margin``) and the scores."""
        if margin < 0:
            raise ValueError(f"margin must be >= 0, got {margin}")
        s = self.scores(cycles)
        pred = s.argmax(axis=1)
        if s.shape[1] > 1 and margin > 0:
            ranked = np.sort(s, axis=1)
            pred = np.where(ranked[:, -1] - ranked[:, -2] >= margin, pred, ABSTAIN)
        return pred, s

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        np.savez(p, mean=self.mean, base=self.base, n_targets=self.n_targets,
                 lag_samples=self.lag_samples, fs=self.fs, n_cycles=self.n_cycles,
                 ch_names=np.array(self.ch_names, dtype=str))
        return p

    @classmethod
    def load(cls, path: str | Path) -> Template:
        with np.load(Path(path), allow_pickle=False) as d:
            return cls(mean=d["mean"], base=int(d["base"]), n_targets=int(d["n_targets"]),
                       lag_samples=float(d["lag_samples"]), fs=float(d["fs"]),
                       n_cycles=int(d["n_cycles"]),
                       ch_names=tuple(str(c) for c in d["ch_names"]))


def _aligned(cycles_by_target: Mapping[int, np.ndarray], base: int, n_targets: int,
             lag: float) -> tuple[np.ndarray, np.ndarray]:
    """Every cycle shifted into the base target's alignment, and its target label."""
    parts, labels = [], []
    for k, cycles in sorted(cycles_by_target.items()):
        if not 0 <= int(k) < n_targets:
            raise ValueError(f"target {k} is not among {n_targets} targets")
        c = _cycles(cycles, f"cycles of target {k}")
        shift = int(round((int(k) - base) * lag))
        parts.append(np.roll(c, -shift, axis=-1))
        labels.append(np.full(c.shape[0], int(k)))
    if not parts:
        raise ValueError("no cycles to fit a template on")
    shapes = {p.shape[1:] for p in parts}
    if len(shapes) > 1:
        raise ValueError(f"cycles differ in shape across targets: {sorted(shapes)}")
    return np.concatenate(parts), np.concatenate(labels)


def fit_template(cycles_by_target: Mapping[int, np.ndarray], n_targets: int, lag_samples: float,
                 fs: float, base: int = 0, ch_names: tuple[str, ...] = ()) -> Template:
    """Average every target's cycles, each shifted back to the base target's alignment."""
    aligned, _ = _aligned(cycles_by_target, base, n_targets, lag_samples)
    return Template(mean=aligned.mean(axis=0), base=base, n_targets=n_targets,
                    lag_samples=lag_samples, fs=fs, n_cycles=int(aligned.shape[0]),
                    ch_names=tuple(ch_names))


def cross_validate(cycles_by_target: Mapping[int, np.ndarray], n_targets: int,
                   lag_samples: float, fs: float, base: int = 0,
                   margin: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Leave-one-cycle-out: each cycle is classified by the template of all the others.

    Returns (labels, predictions) over every cycle; ``metrics.accuracy`` scores them.
    """
    aligned, labels = _aligned(cycles_by_target, base, n_targets, lag_samples)
    if aligned.shape[0] < 2:
        raise ValueError("cross-validation needs at least 2 cycles")
    total = aligned.sum(axis=0)
    preds = np.empty(labels.size, dtype=int)
    for i in range(labels.size):
        held = Template(mean=(total - aligned[i]) / (labels.size - 1), base=base,
                        n_targets=n_targets, lag_samples=lag_samples, fs=fs)
        # undo the alignment: the held-out cycle as it was recorded
        cycle = np.roll(aligned[i], held.shift(int(labels[i])), axis=-1)
        preds[i] = held.decide(cycle, margin)[0][0]
    return labels, preds
