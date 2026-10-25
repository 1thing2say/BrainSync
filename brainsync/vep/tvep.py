"""The t-VEP decoder: flash-locked averages, largest peak-to-valley response wins.

Bin et al. (2009), section 2.1: the flashes of different targets never overlap, and the
flash VEP is time-locked to its own flash, so averaging the EEG epochs that follow one
target's flashes builds up that target's response while the other targets' responses,
falling at unrelated times, average out. The fixated (foveal) target gives the largest
response, so the target with the largest peak-to-valley amplitude of its average is the
one looked at. It needs many flashes per target, which is why t-VEP is the slow paradigm.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from ..metrics import ABSTAIN

__all__ = ["FlashAverager"]


class FlashAverager:
    """Per-target flash epochs (n_flashes, n_ch, n_samples) to a decision."""

    def __init__(self, n_targets: int, fs: float, epoch_s: float = 0.5, min_flashes: int = 2,
                 margin: float = 0.0) -> None:
        if int(n_targets) != n_targets or n_targets < 1:
            raise ValueError(f"n_targets must be a positive integer, got {n_targets!r}")
        if fs <= 0 or epoch_s <= 0:
            raise ValueError(f"fs and epoch_s must be positive, got {fs}, {epoch_s}")
        if int(min_flashes) != min_flashes or min_flashes < 1:
            raise ValueError(f"min_flashes must be a positive integer, got {min_flashes!r}")
        if margin < 0:
            raise ValueError(f"margin must be >= 0, got {margin}")
        self.n_targets = int(n_targets)
        self.fs = float(fs)
        self.epoch_s = float(epoch_s)
        self.n_samples = int(round(epoch_s * fs))
        if self.n_samples < 2:
            raise ValueError(f"epoch of {epoch_s} s at {fs} Hz has fewer than 2 samples")
        self.min_flashes = int(min_flashes)
        self.margin = float(margin)

    def _epochs(self, epochs: np.ndarray, k: int) -> np.ndarray:
        if not 0 <= k < self.n_targets:
            raise ValueError(f"target {k} is not among {self.n_targets} targets")
        e = np.asarray(epochs, dtype=float)
        if e.ndim == 2:
            e = e[np.newaxis]
        if e.ndim != 3 or e.shape[2] != self.n_samples:
            raise ValueError(f"target {k}: epochs must be (n_flashes, n_ch, {self.n_samples}), "
                             f"got shape {e.shape}")
        if not np.isfinite(e).all():
            raise ValueError(f"target {k}: epochs hold NaN/inf values")
        return e

    def counts(self, epochs_by_target: Mapping[int, np.ndarray]) -> np.ndarray:
        """Flashes seen per target."""
        n = np.zeros(self.n_targets, dtype=int)
        for k, e in epochs_by_target.items():
            n[int(k)] = self._epochs(e, int(k)).shape[0]
        return n

    def average(self, epochs_by_target: Mapping[int, np.ndarray]) -> np.ndarray:
        """Mean response per target (n_targets, n_ch, n_samples); NaN rows where a target
        has fewer than ``min_flashes`` epochs."""
        items = {int(k): self._epochs(e, int(k)) for k, e in epochs_by_target.items()}
        if not items:
            raise ValueError("no flash epochs at all")
        n_ch = {e.shape[1] for e in items.values()}
        if len(n_ch) > 1:
            raise ValueError(f"epochs differ in channel count: {sorted(n_ch)}")
        out = np.full((self.n_targets, n_ch.pop(), self.n_samples), np.nan)
        for k, e in items.items():
            if e.shape[0] >= self.min_flashes:
                out[k] = e.mean(axis=0)
        return out

    def scores(self, epochs_by_target: Mapping[int, np.ndarray]) -> np.ndarray:
        """Peak-to-valley amplitude of each target's average, mean over channels (n_targets,);
        NaN for targets with too few flashes."""
        avg = self.average(epochs_by_target)
        return (avg.max(axis=-1) - avg.min(axis=-1)).mean(axis=-1)

    def decide(self, epochs_by_target: Mapping[int, np.ndarray]) -> tuple[int, np.ndarray]:
        """The target with the largest response, or ABSTAIN while any target still lacks
        ``min_flashes`` epochs or the best does not beat the runner-up by ``margin``
        (a fraction of the runner-up's amplitude)."""
        s = self.scores(epochs_by_target)
        if np.isnan(s).any():
            return ABSTAIN, s
        order = np.argsort(s)
        best = int(order[-1])
        if self.n_targets > 1 and self.margin > 0:
            runner = s[order[-2]]
            if s[best] < (1.0 + self.margin) * runner:
                return ABSTAIN, s
        return best, s
