"""SSVEP classifiers: training-free CCA and filter-bank CCA against sine/cosine templates,
and the calibration-based spatial filters TRCA, eTRCA (plus their "-R" variants) and msCCA
fitted on the user's own labelled trials (Wong et al. 2020's unified framework; registered
as "trca-sf", "trca-r-sf", "etrca-sf", "etrca-r-sf" and "mscca", beside the template
classifiers of ``brainsync.templates``).

For CCA a target's score is the largest canonical correlation between the EEG window and
that target's templates. ``fit_baseline`` on still-screen windows learns how much each template
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

import copy
from collections import OrderedDict
from collections.abc import Sequence
from fractions import Fraction
from math import lcm

import numpy as np
from scipy.linalg import eigh

from .metrics import ABSTAIN
from .signal import Band, default_bands, filterbank, filterbank_weights

__all__ = ["ABSTAIN", "CCA", "FBCCA", "Trained", "TRCA", "ETRCA", "MSCCA", "NAMES", "METHODS",
           "TRAINED", "SPATIAL_FILTERS", "TRAINING_FREE", "kinds", "make", "is_trained",
           "references", "min_samples", "period_samples"]

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

    def fit_baseline(self, X_rest: np.ndarray, offset: float = 0.0) -> CCA:
        """Learn each template's mean rest score and the pooled within-template rest std.

        ``offset`` is passed to ``raw_scores``; a trained classifier with raw templates
        needs one inside its calibration epoch (its override supplies it).
        """
        rest = self.raw_scores(X_rest, offset)
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

    def scores(self, X: np.ndarray, offset: float | None = None) -> np.ndarray:
        """Raw scores minus each template's rest mean (unchanged before fit_baseline).

        ``offset`` is passed on only when given, so each subclass keeps its own default
        (0 s for ``brainsync.templates``, the training alignment for ``Trained``).
        """
        raw = self.raw_scores(X) if offset is None else self.raw_scores(X, offset)
        return raw - self.baseline_mean

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

    def decide(self, X: np.ndarray,
               offset: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Predictions (n_trials,) and baseline-corrected scores (n_trials, n_targets)."""
        s = self.scores(X, offset)
        return self.predict_scores(s), s

    def predict(self, X: np.ndarray, offset: float | None = None) -> np.ndarray:
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


# -- trained spatial filters (Wong et al. 2020, IEEE TBME 67(11)) ---------------------

def period_samples(freqs: Sequence[float], fs: float, max_s: float = 10.0) -> int | None:
    """Samples after which every target's steady-state response repeats, or None past ``max_s``.

    A target at f Hz repeats every fs / f samples; the common period is the least common
    multiple of those (as exact fractions). Integer frequencies at 250 Hz give 250 (1 s);
    the Nakanishi set's 9.25 Hz steps give 4 s.
    """
    fs = float(fs)
    k = 1
    for f in np.atleast_1d(np.asarray(freqs, dtype=float)):
        ratio = Fraction(float(f) / fs).limit_denominator(1_000_000)
        k = lcm(k, ratio.denominator)
        if k > max_s * fs:
            return None
    return int(k)


def _fold(T: np.ndarray, period: int) -> np.ndarray:
    """Average (..., n) over its periods: bin j of the result is the mean of samples j mod period."""
    n = T.shape[-1]
    idx = np.arange(n) % period
    out = np.zeros(T.shape[:-1] + (period,))
    np.add.at(np.moveaxis(out, -1, 0), idx, np.moveaxis(T, -1, 0))
    return out / np.bincount(idx, minlength=period)


def _gep_top(A: np.ndarray, B: np.ndarray, shrink: float = 0.0) -> np.ndarray:
    """Top generalised eigenvector of A w = lambda B w, with B shrunk towards a scaled identity.

    The sign is fixed so the largest-magnitude weight is positive, which makes filters
    reproducible; correlations with a filtered template do not depend on it.
    """
    A = 0.5 * (np.asarray(A, dtype=float) + np.asarray(A, dtype=float).T)
    B = 0.5 * (np.asarray(B, dtype=float) + np.asarray(B, dtype=float).T)
    n = B.shape[0]
    mu = float(np.trace(B)) / n
    if not np.isfinite(mu) or mu <= 0:
        raise ValueError("training windows have no variance; cannot fit a spatial filter")
    B = (1.0 - shrink) * B + (shrink + 1e-9) * mu * np.eye(n)
    _, vecs = eigh(A, B)
    w = vecs[:, -1]
    lead = w[np.argmax(np.abs(w))]
    return w if lead >= 0 else -w


def _pearson(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pearson correlation along the last axis, broadcasting the leading ones; 0 where flat."""
    a = a - a.mean(axis=-1, keepdims=True)
    b = b - b.mean(axis=-1, keepdims=True)
    num = np.sum(a * b, axis=-1)
    den = np.sqrt(np.sum(a * a, axis=-1) * np.sum(b * b, axis=-1))
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(den > 0, num / den, 0.0)
    return np.clip(r, -1.0, 1.0)


class Trained(CCA):
    """Base for classifiers fitted on the user's own labelled trials (calibration-based).

    ``fit(X, y, t0)`` learns one template per target (the mean trial) and spatial filters
    from windows that all start ``t0`` seconds after the stimulus onset. A window's raw score for target i
    is the signed Pearson correlation between the filtered window and the filtered
    template, so the subject's own phase counts, which calibration-free CCA cannot use.
    The baseline and abstain rules are inherited unchanged: ``fit_baseline`` learns what
    the correlations do on a still screen.

    Templates are locked to stimulus onset, but a sliding decoder scores windows at any
    offset from it. Once the response is steady it repeats every ``period_samples`` (1 s for
    integer frequencies at 250 Hz), so when the training window holds at least one period
    the template is folded into one steady-state cycle and any window length and offset can
    be scored. ``offset`` is, as for every classifier here, the window's first sample in
    seconds since the onset (None: ``t0``, the training alignment). With a shorter training
    window the raw template is used, the window must match its length, and ``offset`` must
    be ``t0`` (``covers`` says so). ``periodic`` forces either behaviour.

    ``shrink`` in [0, 1) pulls the covariance the filters divide by towards a scaled identity
    (0 = plain, 1 would be the identity), which steadies filters fitted on few trials.
    """

    name = "trained"
    ensemble = False       # score with every target's filter at once (the eTRCA idea)
    min_trials = 2         # per target; inter-trial covariance needs two
    uses_offset = True     # templates are locked to the onset (the decoder needs set_onset)
    needs_fit = True

    def __init__(self, freqs: Sequence[float], fs: float, harmonics: int = 3,
                 phases: Sequence[float] | None = None, margin: float = 0.0,
                 threshold: float | None = None, shrink: float = 0.0,
                 periodic: bool | None = None) -> None:
        super().__init__(freqs, fs, harmonics, phases, margin, threshold)
        shrink = float(shrink)
        if not np.isfinite(shrink) or not 0.0 <= shrink < 1.0:
            raise ValueError(f"shrink must lie in [0, 1), got {shrink}")
        self.shrink = shrink
        self.periodic = None if periodic is None else bool(periodic)
        self.templates: np.ndarray | None = None   # (n_targets, n_ch, n_train)
        self.filters: np.ndarray | None = None     # (n_ch, n_filters)
        self.cycle: np.ndarray | None = None       # (n_targets, n_ch, period) or None
        self.n_train: np.ndarray | None = None     # trials per target
        self.period: int | None = None
        self.t0 = 0.0                               # seconds from onset to the training windows

    def __repr__(self) -> str:
        state = "unfitted" if not self.fitted else (
            f"fitted on {int(self.n_train.sum())} trials, {self.templates.shape[-1]} samples"
            + (", periodic" if self.cycle is not None else ""))
        return f"{super().__repr__()[:-1]}, shrink={self.shrink}; {state})"

    @property
    def fitted(self) -> bool:
        return self.templates is not None

    @property
    def n_ch(self) -> int:
        """Channels the classifier was fitted on (0 before fit)."""
        return 0 if self.templates is None else int(self.templates.shape[1])

    # -- fitting ----------------------------------------------------------------------

    def _fit_filters(self, X: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Spatial filters (n_ch, k) from centred training windows; ``self.templates`` is set."""
        raise NotImplementedError

    def fit(self, X: np.ndarray, y: Sequence[int], t0: float = 0.0) -> Trained:
        """Learn templates and filters from windows X (n_trials, n_ch, n) with labels y that
        start ``t0`` s after the onset (the visual latency, as ``recordings.cut_windows``
        cuts them).

        Every target needs at least ``min_trials`` windows. Fitting forgets the baseline,
        because the rest scores depend on the templates: call ``fit_baseline`` again.
        """
        t0 = float(t0)
        if not np.isfinite(t0):
            raise ValueError(f"t0 must be a finite time in seconds, got {t0}")
        X = self._windows(X)
        y = np.asarray(y).reshape(-1)
        if y.size != X.shape[0]:
            raise ValueError(f"{y.size} labels for {X.shape[0]} windows")
        if y.size and (not np.issubdtype(y.dtype, np.integer) or y.min() < 0
                       or y.max() >= self.n_targets):
            raise ValueError(f"labels must be integers in 0..{self.n_targets - 1}, got "
                             f"{sorted(set(y.tolist()))}")
        y = y.astype(int)
        counts = np.bincount(y, minlength=self.n_targets)
        if counts.min() < self.min_trials:
            raise ValueError(f"{type(self).__name__} needs at least {self.min_trials} trials per "
                             f"target, got {counts.tolist()} for {self.freqs.tolist()} Hz")
        X = X - X.mean(axis=-1, keepdims=True)
        n = X.shape[-1]
        self.templates = np.stack([X[y == i].mean(axis=0) for i in range(self.n_targets)])
        self.filters = self._fit_filters(X, y)
        self.n_train = counts
        self.train_X, self.train_y = X, y          # kept so update() can refit on the union
        self.period = period_samples(self.freqs, self.fs)
        fits = self.period is not None and n >= self.period
        use_cycle = fits if self.periodic is None else self.periodic
        if use_cycle and not fits:
            raise ValueError(f"periodic templates need a training window of at least one "
                             f"common period ({self.period} samples), got {n}")
        self.cycle = _fold(self.templates, self.period) if use_cycle else None
        self.t0 = t0
        self.clear_baseline()
        return self

    def update(self, X: np.ndarray, y: Sequence[int] | int) -> Trained:
        """Add labelled windows to the training set and refit on the union.

        Windows must have the training windows' channels and length; ``y`` may be one label
        for a single window. Like ``fit`` this forgets the baseline.
        """
        if not self.fitted:
            raise ValueError(f"{type(self).__name__} is not fitted; call fit(X, y) first")
        X = self._windows(X)
        y = np.atleast_1d(np.asarray(y)).reshape(-1)
        if X.shape[1:] != self.train_X.shape[1:]:
            raise ValueError(f"new windows must be shaped (n, {self.train_X.shape[1]}, "
                             f"{self.train_X.shape[2]}) like the training windows, got {X.shape}")
        return self.fit(np.concatenate([self.train_X, X]), np.concatenate([self.train_y, y]),
                        self.t0)

    def _shift(self, offset: float | None) -> int:
        """Samples from the training alignment to a window starting ``offset`` s after the onset."""
        if offset is None:
            return 0
        offset = float(offset)
        if not np.isfinite(offset):
            raise ValueError(f"offset must be a finite time in seconds, got {offset}")
        return int(round((offset - self.t0) * self.fs))

    def covers(self, n: int, offset: float | None) -> bool:
        """Can a window of ``n`` samples starting ``offset`` s after the onset be scored?
        Always with periodic templates; a raw template only at its own length and ``t0``."""
        if self.cycle is not None:
            return True
        return (self.templates is not None and n == self.templates.shape[-1]
                and self._shift(offset) == 0)

    def templates_for(self, n: int, offset: float | None = None) -> np.ndarray:
        """Templates (n_targets, n_ch, n) for a window of ``n`` samples starting ``offset`` s
        after the onset (None: at the training alignment ``t0``)."""
        if not self.fitted:
            raise ValueError(f"{type(self).__name__} is not fitted; call fit(X, y) first")
        shift = self._shift(offset)
        if self.cycle is None:
            if shift != 0:
                raise ValueError(f"this classifier has no periodic templates (training window "
                                 f"shorter than one period), so the offset must be its t0 "
                                 f"({self.t0:g} s)")
            if n != self.templates.shape[-1]:
                raise ValueError(f"window of {n} samples does not match the training window of "
                                 f"{self.templates.shape[-1]}; fit on the same length")
            return self.templates
        idx = (shift + np.arange(int(n))) % self.period
        return self.cycle[..., idx]

    # -- scoring ----------------------------------------------------------------------

    def fit_baseline(self, X_rest: np.ndarray, offset: float | None = None) -> Trained:
        """As for CCA; still windows carry no flicker, so they are scored at ``t0`` by default,
        which a raw template always covers."""
        super().fit_baseline(X_rest, self.t0 if offset is None else float(offset))
        return self

    def raw_scores(self, X: np.ndarray, offset: float | None = None) -> np.ndarray:
        """Signed correlations (n_trials, n_targets) of filtered windows with filtered templates."""
        X = self._windows(X)
        if not self.fitted:
            raise ValueError(f"{type(self).__name__} is not fitted; call fit(X, y) first")
        if X.shape[1] != self.templates.shape[1]:
            raise ValueError(f"windows have {X.shape[1]} channels but the classifier was fitted "
                             f"on {self.templates.shape[1]}")
        X = X - X.mean(axis=-1, keepdims=True)
        T = self.templates_for(X.shape[-1], offset)
        fx = np.einsum("ck,tcn->tkn", self.filters, X)     # (trials, k, n)
        ft = np.einsum("ck,icn->ikn", self.filters, T)     # (targets, k, n)
        if self.ensemble:
            a = fx.reshape(fx.shape[0], 1, -1)
            b = ft.reshape(1, ft.shape[0], -1)
        else:
            k = np.arange(self.n_targets)
            a = fx                                          # filter i scores target i
            b = ft[k, k][None]                              # (1, targets, n)
        return _pearson(a, b)

    # -- subsets (the confirm screen flickers two of the four targets) ----------------

    def subset(self, active: Sequence[int]) -> Trained:
        """A copy restricted to the targets ``active`` (indices), with no baseline.

        Per-target filters keep only those targets'; an ensemble keeps every trained filter
        and only restricts which templates compete.
        """
        idx = np.unique(np.asarray(active, dtype=int).reshape(-1))
        if idx.size == 0 or idx.min() < 0 or idx.max() >= self.n_targets:
            raise ValueError(f"active targets must be indices in 0..{self.n_targets - 1}, "
                             f"got {np.asarray(active).tolist()}")
        if not self.fitted:
            raise ValueError(f"{type(self).__name__} is not fitted; call fit(X, y) first")
        new = copy.copy(self)
        new.freqs = self.freqs[idx]
        new.phases = self.phases[idx]
        new._cache = OrderedDict()
        new.templates = self.templates[idx]
        new.cycle = None if self.cycle is None else self.cycle[idx]
        new.n_train = self.n_train[idx]
        if not self.ensemble:
            new.filters = self.filters[:, idx]
        new.clear_baseline()
        return new

    def for_freqs(self, freqs: Sequence[float], tol: float = 0.01) -> Trained:
        """``subset`` by frequency, in the order given (each must match a trained target)."""
        idx = []
        for f in np.atleast_1d(np.asarray(freqs, dtype=float)):
            hit = np.flatnonzero(np.abs(self.freqs - f) <= tol)
            if hit.size != 1:
                raise ValueError(f"{f:g} Hz is not one of the trained targets {self.freqs.tolist()}")
            idx.append(int(hit[0]))
        if len(set(idx)) != len(idx):
            raise ValueError(f"frequencies repeat: {np.asarray(freqs).tolist()}")
        sub = self.subset(idx)
        order = np.argsort(np.argsort(idx))        # subset() sorted them; restore the asked order
        sub.freqs, sub.phases = sub.freqs[order], sub.phases[order]
        sub.templates, sub.n_train = sub.templates[order], sub.n_train[order]
        if sub.cycle is not None:
            sub.cycle = sub.cycle[order]
        if not self.ensemble:
            sub.filters = sub.filters[:, order]
        return sub


class TRCA(Trained):
    """Task-related component analysis (Nakanishi et al. 2018): per target, the filter that
    maximises the covariance between its trials against their summed within-trial covariance.

    ``reference=True`` is TRCA-R (Wong et al. 2020): the summed trials are first projected
    onto the target's sine-cosine subspace, which steadies the filter when trials are few.
    """

    name = "trca-sf"     # "-sf": brainsync.templates already registers "trca" and "etrca"

    def __init__(self, *args, reference: bool = False, **kw) -> None:
        super().__init__(*args, **kw)
        self.reference = bool(reference)
        if self.reference:
            self.name = type(self).name.replace("-sf", "-r-sf")

    def _fit_filters(self, X: np.ndarray, y: np.ndarray) -> np.ndarray:
        n = X.shape[-1]
        W = []
        for i in range(self.n_targets):
            Xi = X[y == i]                                       # (trials, ch, n)
            Q = np.einsum("tcn,tdn->cd", Xi, Xi)                 # sum of within-trial covariances
            S = Xi.sum(axis=0)                                   # (ch, n)
            if self.reference:
                ref = references(self.freqs[i:i + 1], self.fs, n, self.harmonics,
                                 self.phases[i:i + 1])[0]        # (2H, n)
                P = S @ _span(ref.T)                             # project onto the sine-cosine span
                A = P @ P.T
            else:
                A = S @ S.T - Q                                  # between-trial covariance only
            W.append(_gep_top(A, Q, self.shrink))
        return np.stack(W, axis=1)


class ETRCA(TRCA):
    """Ensemble TRCA: every target's filter is applied to the window and to each template,
    and the correlation is taken over all filtered channels at once. ``reference=True`` is
    eTRCA-R."""

    name = "etrca-sf"
    ensemble = True


class MSCCA(Trained):
    """Multi-stimulus CCA (Wong et al. 2020): one spatial filter shared by all targets, fitted
    on every target's template stacked against its phase-aware sine-cosine reference. Works
    from a single trial per target, since the between-target data does the estimating."""

    name = "mscca"
    ensemble = True
    min_trials = 1

    def _fit_filters(self, X: np.ndarray, y: np.ndarray) -> np.ndarray:
        n = X.shape[-1]
        refs = references(self.freqs, self.fs, n, self.harmonics, self.phases)   # (nt, 2H, n)
        Z = np.concatenate([self.templates[i].T for i in range(self.n_targets)])  # (nt*n, ch)
        Y = np.concatenate([refs[i].T for i in range(self.n_targets)])            # (nt*n, 2H)
        ZQ = Z.T @ _span(Y)
        return _gep_top(ZQ @ ZQ.T, Z.T @ Z, self.shrink)[:, None]


TRAINING_FREE = ("cca", "fbcca")
TRAINED = ("itcca", "ecca", "trca", "etrca", "etrcar", "sfcca")   # brainsync.templates (save_model)
SPATIAL_FILTERS = ("trca-sf", "trca-r-sf", "etrca-sf", "etrca-r-sf", "mscca")   # Trained, above
NAMES = TRAINING_FREE + TRAINED + SPATIAL_FILTERS
METHODS = NAMES
# brainsync.trca's TRCA, phase-locked to the flicker with --lock (demo --classifier
# trca-phase): trained and saved by brainsync.train, loaded by brainsync.trca.load_model,
# never built by make(), so it is not in NAMES (what score_cued, nakanishi, calibrate and
# online build and fit themselves)
PHASE_LOCKED = ("trca-phase",)

_SPATIAL: dict[str, tuple[type[Trained], dict]] = {
    "trca-sf": (TRCA, {}), "trca-r-sf": (TRCA, {"reference": True}),
    "etrca-sf": (ETRCA, {}), "etrca-r-sf": (ETRCA, {"reference": True}),
    "mscca": (MSCCA, {}),
}


def _key(name: str) -> str:
    return str(name).strip().lower().replace("_", "-")


def is_trained(name: str) -> bool:
    """True for classifiers that need ``fit`` on labelled trials before scoring: ``TRAINED``
    from brainsync.templates, the ``SPATIAL_FILTERS`` here and ``PHASE_LOCKED``."""
    key = _key(name)
    return key in TRAINED or key in SPATIAL_FILTERS or key in PHASE_LOCKED


def kinds() -> dict[str, type[CCA]]:
    """Every classifier class by name: the training-free ones and the template classifiers
    of ``brainsync.templates`` (the spatial filters here are built by ``make``, since two
    names share a class)."""
    from . import templates  # here, not at the top: templates imports this module

    out = {cls.name: cls for cls in (CCA, FBCCA)}
    out.update({cls.name: cls for cls in templates.CLASSES})
    return out


def make(name: str, **kw) -> CCA:
    """Build a classifier by name (case-insensitive): "cca", "fbcca", a template classifier
    ("itcca", "ecca", "trca", "etrca", "etrcar", "sfcca") or a spatial filter ("trca-sf",
    "trca-r-sf", "etrca-sf", "etrca-r-sf", "mscca"). The trained ones need ``fit`` first."""
    key = _key(name)
    if key in _SPATIAL:
        cls, fixed = _SPATIAL[key]
        return cls(**kw, **fixed)
    if key in PHASE_LOCKED:
        raise ValueError(f"{key} is loaded from a brainsync.train model "
                         f"(brainsync.trca.load_model), not built by make()")
    cls = kinds().get(key)
    if cls is None:
        raise ValueError(f"unknown classifier {name!r}; choose from {list(NAMES)}")
    return cls(**kw)
