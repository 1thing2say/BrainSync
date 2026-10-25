"""Training-free SSVEP classifiers: CCA and filter-bank CCA against sine/cosine templates.

A target's score is the largest canonical correlation between the EEG window and that
target's templates. ``fit_baseline`` on still-screen windows learns how much each template
scores at rest (the 1/f background favours low frequencies) and subtracts it; the pooled
rest spread is then the unit of the abstain rules, so they mean the same thing for CCA
and FBCCA even though their raw scores live on different scales. Two rules can make a
classifier abstain: ``margin`` (the best target must lead the runner-up by this many
spreads: is the choice clear?) and ``threshold`` (the best target must sit this many
spreads above its own rest level: is there an SSVEP at all?). At rest two independent
scores differ by about 1.4 spreads, so the lead alone rarely abstains on a still screen
or with eyes closed; the threshold is what catches those.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence

import numpy as np

from .metrics import ABSTAIN
from .signal import Band, default_bands, filterbank, filterbank_weights

__all__ = ["ABSTAIN", "CCA", "FBCCA", "NAMES", "TRAINED", "TRAINING_FREE", "kinds", "make",
           "references", "min_samples"]

CACHE_SIZE = 4   # template bases kept per classifier (one per window length)


def _target_freqs(freqs: Sequence[float], fs: float) -> np.ndarray:
    fs = float(fs)
    if not np.isfinite(fs) or fs <= 0:
        raise ValueError(f"sampling rate must be positive, got {fs}")
    f = np.atleast_1d(np.asarray(freqs, dtype=float)).ravel()
    if f.size == 0:
        raise ValueError("need at least one target frequency, got []")
    bad = ~np.isfinite(f) | (f <= 0) | (f >= fs / 2.0)
    if bad.any():
        raise ValueError(f"target frequencies must lie in (0, {fs / 2.0}) Hz, got {f.tolist()}")
    return f


def _target_phases(phases: Sequence[float] | None, n_targets: int) -> np.ndarray:
    if phases is None:
        return np.zeros(n_targets)
    p = np.atleast_1d(np.asarray(phases, dtype=float)).ravel()
    if p.size != n_targets:
        raise ValueError(f"need {n_targets} phases (one per frequency), got {p.size}: {p.tolist()}")
    return p


def _check_harmonics(harmonics: int) -> int:
    if int(harmonics) != harmonics or harmonics < 1:
        raise ValueError(f"harmonics must be a positive integer, got {harmonics}")
    return int(harmonics)


def references(freqs: Sequence[float], fs: float, n_samples: int, harmonics: int = 3,
               phases: Sequence[float] | None = None, t0: float = 0.0) -> np.ndarray:
    """Templates (n_targets, 2 * harmonics, n_samples).

    For each target the first ``harmonics`` rows are sin(h * theta) for h = 1..H and the
    next H rows cos(h * theta), where theta = 2 pi f (t0 + k / fs) + phase for sample k.
    ``t0`` is the first sample's time since the stimulus onset, so templates for windows
    cut at different offsets stay phase-locked to the same flicker.
    """
    f = _target_freqs(freqs, fs)
    H = _check_harmonics(harmonics)
    if n_samples < 2:
        raise ValueError(f"templates need at least 2 samples, got {n_samples}")
    t0 = float(t0)
    if not np.isfinite(t0):
        raise ValueError(f"t0 must be a finite time in seconds, got {t0}")
    p = _target_phases(phases, f.size)
    times = t0 + np.arange(int(n_samples)) / float(fs)
    theta = 2.0 * np.pi * np.outer(f, times) + p[:, None]
    orders = np.arange(1, H + 1, dtype=float)
    phasor = np.exp(1j * orders[None, :, None] * theta[:, None, :])  # (n_targets, H, n)
    return np.concatenate([phasor.imag, phasor.real], axis=1)


def min_samples(n_channels: int, harmonics: int) -> int:
    """Shortest window that leaves CCA a residual dimension (else every rho is ~1)."""
    return int(n_channels) + 2 * int(harmonics) + 2


def _span(A: np.ndarray) -> np.ndarray:
    """Orthonormal columns spanning the centred columns of A (..., n, k), via SVD.

    Directions below numerical rank are zeroed rather than dropped, so a stack of
    matrices keeps one shape; zero columns add nothing to the canonical correlations.
    """
    A = A - A.mean(axis=-2, keepdims=True)
    u, s, _ = np.linalg.svd(A, full_matrices=False)
    tol = s[..., :1] * max(A.shape[-2:]) * np.finfo(float).eps
    return u * (s > tol)[..., None, :]


class CCA:
    """Standard CCA (Lin et al. 2006): first canonical correlation with each target's templates.

    ``offset`` (seconds from the stimulus onset to the window's first sample) is accepted by
    every scoring method for API compatibility with the trained classifiers in
    ``brainsync.templates``; the sine/cosine span is shift-invariant, so it changes nothing
    here (``uses_offset`` says whether a classifier needs it).
    """

    name = "cca"
    uses_offset = False   # scores do not depend on where the window sits in the trial
    needs_fit = False     # training-free: no calibration trials required

    def __init__(self, freqs: Sequence[float], fs: float, harmonics: int = 3,
                 phases: Sequence[float] | None = None, margin: float = 0.0,
                 threshold: float | None = None) -> None:
        self.fs = float(fs)
        self.freqs = _target_freqs(freqs, self.fs)
        self.phases = _target_phases(phases, self.freqs.size)
        self.harmonics = _check_harmonics(harmonics)
        margin = float(margin)
        if not np.isfinite(margin) or margin < 0:
            raise ValueError(f"margin must be finite and >= 0, got {margin}")
        self.margin = margin
        if threshold is not None:
            threshold = float(threshold)
            if not np.isfinite(threshold):
                raise ValueError(f"threshold must be finite or None, got {threshold}")
        self.threshold = threshold
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self.clear_baseline()

    @property
    def n_targets(self) -> int:
        return int(self.freqs.size)

    def __repr__(self) -> str:
        return (f"{type(self).__name__}(freqs={self.freqs.tolist()}, fs={self.fs}, "
                f"harmonics={self.harmonics}, margin={self.margin}, threshold={self.threshold})")

    # -- scoring ---------------------------------------------------------------------

    def _windows(self, X: np.ndarray) -> np.ndarray:
        """Validate X and return it as (n_trials, n_ch, n)."""
        X = np.asarray(X, dtype=float)
        if X.ndim == 2:
            X = X[np.newaxis]
        if X.ndim != 3:
            raise ValueError(f"X must be (n_ch, n) or (n_trials, n_ch, n), got shape {X.shape}")
        need = min_samples(X.shape[1], self.harmonics)
        if X.shape[2] < need:
            raise ValueError(f"window of {X.shape[2]} samples is too short for {X.shape[1]} "
                             f"channels and {self.harmonics} harmonics (need >= {need})")
        finite = np.isfinite(X)
        if not finite.all():
            first = tuple(int(i) for i in np.argwhere(~finite)[0])
            raise ValueError(f"X has {int((~finite).sum())} NaN/inf values, first at index {first}")
        return X

    def _template_span(self, n: int) -> np.ndarray:
        """(n_targets, n, 2H) orthonormal template bases, LRU-cached by window length."""
        hit = self._cache.get(n)
        if hit is None:
            refs = references(self.freqs, self.fs, n, self.harmonics, self.phases)
            hit = _span(np.swapaxes(refs, 1, 2))
            self._cache[n] = hit
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(n)
        return hit

    def _rho(self, X: np.ndarray) -> np.ndarray:
        """First canonical correlation (n_trials, n_targets) for validated X (n_trials, n_ch, n)."""
        qy = self._template_span(X.shape[-1])                  # (targets, n, 2H)
        qx = _span(np.swapaxes(X, 1, 2))                       # (trials, n, ch)
        cross = np.swapaxes(qx, 1, 2)[:, None] @ qy[None]      # (trials, targets, ch, 2H)
        top = np.linalg.svd(cross, compute_uv=False)[..., 0]
        return np.clip(top, 0.0, 1.0)

    def raw_scores(self, X: np.ndarray, offset: float = 0.0) -> np.ndarray:
        """Uncorrected scores (n_trials, n_targets) for X (n_trials, n_ch, n) or (n_ch, n)."""
        return self._rho(self._windows(X))

    # -- resting baseline ------------------------------------------------------------

    def fit_baseline(self, X_rest: np.ndarray) -> CCA:
        """Learn each template's mean rest score and the pooled within-template rest std."""
        rest = self.raw_scores(X_rest)
        if rest.shape[0] < 2:
            raise ValueError(f"baseline needs at least 2 rest windows, got {rest.shape[0]}")
        spread = float(np.sqrt(np.mean(np.var(rest, axis=0, ddof=1))))
        if not np.isfinite(spread) or spread <= 0:
            raise ValueError(f"rest scores do not vary (spread {spread}); cannot scale the margin")
        self.baseline_mean = rest.mean(axis=0)
        self.baseline_spread = spread
        self.has_baseline = True
        return self

    def clear_baseline(self) -> None:
        """Forget the baseline: zero offsets and unit spread."""
        self.baseline_mean = np.zeros(self.n_targets)
        self.baseline_spread = 1.0
        self.has_baseline = False

    def scores(self, X: np.ndarray, offset: float = 0.0) -> np.ndarray:
        """Raw scores minus each template's rest mean (unchanged before fit_baseline)."""
        return self.raw_scores(X, offset) - self.baseline_mean

    # -- decisions -------------------------------------------------------------------

    def gaps(self, scores: np.ndarray) -> np.ndarray:
        """Lead of the best target, in baseline spreads, per trial.

        The lead is over the runner-up; with a single target it is over the rest level.
        """
        s = np.atleast_2d(np.asarray(scores, dtype=float))
        ranked = np.sort(s, axis=1)
        runner_up = ranked[:, -2] if s.shape[1] > 1 else 0.0
        return (ranked[:, -1] - runner_up) / self.baseline_spread

    def levels(self, scores: np.ndarray) -> np.ndarray:
        """Best target's score above its rest mean, in baseline spreads, per trial."""
        s = np.atleast_2d(np.asarray(scores, dtype=float))
        return s.max(axis=1) / self.baseline_spread

    def predict_scores(self, scores: np.ndarray) -> np.ndarray:
        """Best target per trial, or ABSTAIN where its lead is below ``margin`` or its
        level above rest is below ``threshold`` (None: no level rule)."""
        s = np.atleast_2d(np.asarray(scores, dtype=float))
        best = s.argmax(axis=1)
        keep = np.ones(best.shape, dtype=bool)
        if self.margin > 0:
            keep &= self.gaps(s) >= self.margin
        if self.threshold is not None:
            keep &= self.levels(s) >= self.threshold
        return np.where(keep, best, ABSTAIN)

    def decide(self, X: np.ndarray, offset: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Predictions (n_trials,) and baseline-corrected scores (n_trials, n_targets)."""
        s = self.scores(X, offset)
        return self.predict_scores(s), s

    def predict(self, X: np.ndarray, offset: float = 0.0) -> np.ndarray:
        return self.decide(X, offset)[0]


class FBCCA(CCA):
    """Filter-bank CCA (Chen et al. 2015): score = sum over sub-bands m of w_m * rho_m**2."""

    name = "fbcca"

    def __init__(self, freqs: Sequence[float], fs: float, harmonics: int = 3,
                 phases: Sequence[float] | None = None, margin: float = 0.0,
                 bands: Sequence[Band] | None = None, a: float = 1.25, b: float = 0.25,
                 threshold: float | None = None) -> None:
        super().__init__(freqs, fs, harmonics, phases, margin, threshold)
        chosen = default_bands(self.fs) if bands is None else list(bands)
        if len(chosen) == 0:
            raise ValueError("FBCCA needs at least one sub-band, got bands=[]")
        self.bands: list[Band] = []
        for lo, hi in chosen:
            lo, hi = float(lo), float(hi)
            if not (0.0 <= lo < hi and lo < self.fs / 2.0):
                raise ValueError(f"sub-band must satisfy 0 <= low < high and low < "
                                 f"{self.fs / 2.0} Hz, got ({lo}, {hi})")
            self.bands.append((lo, hi))
        self.a, self.b = float(a), float(b)
        self.weights = filterbank_weights(len(self.bands), self.a, self.b)

    def __repr__(self) -> str:
        return f"{super().__repr__()[:-1]}, bands={self.bands})"

    def raw_scores(self, X: np.ndarray, offset: float = 0.0) -> np.ndarray:
        X = self._windows(X)
        total = np.zeros((X.shape[0], self.n_targets))
        for w, Xb in zip(self.weights, filterbank(X, self.fs, self.bands)):
            total += w * self._rho(Xb) ** 2
        return total


TRAINING_FREE = ("cca", "fbcca")
TRAINED = ("itcca", "ecca", "trca", "etrca", "etrcar", "sfcca")
NAMES = TRAINING_FREE + TRAINED


def kinds() -> dict[str, type[CCA]]:
    """Every classifier class by name (the trained ones live in ``brainsync.templates``)."""
    from . import templates  # here, not at the top: templates imports this module

    out = {cls.name: cls for cls in (CCA, FBCCA)}
    out.update({cls.name: cls for cls in templates.CLASSES})
    return out


def make(name: str, **kw) -> CCA:
    """Build a classifier by name (case-insensitive): "cca", "fbcca", or a trained one
    ("itcca", "ecca", "trca", "etrca", "etrcar", "sfcca"), which needs ``fit`` before scoring."""
    cls = kinds().get(str(name).strip().lower())
    if cls is None:
        raise ValueError(f"unknown classifier {name!r}; choose from {list(NAMES)}")
    return cls(**kw)
