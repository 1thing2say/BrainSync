"""Trained SSVEP classifiers: individual templates and spatial filters from cued trials.

The training-free classifiers in ``brainsync.classify`` compare a window with sine/cosine
references. Every comparison study since 2014 finds that a few cued trials of the user's own
EEG do much better, most of all at short windows and with more targets: individual-template
CCA (Bin 2011, Nakanishi 2015), the CCA + template combination ("eCCA", Nakanishi 2014/2015),
task-related component analysis (TRCA, Nakanishi 2018) and its sine/cosine-regularised form
TRCA-R (Wong 2020). This module implements them behind the ``CCA`` scoring API, so the
decoder, the replay tools and the baseline/margin/threshold rules work unchanged.

Fit on ``fit(trials, labels, t0)``: ``trials`` are preprocessed, channel-picked epochs
(n_trials, n_ch, n) that all start ``t0`` seconds after the stimulus onset (the visual latency
when they are cut as ``recordings.cut_windows`` does). Each target's template is its trial
average projected onto the sine/cosine subspace of its own frequency (the same projection
TRCA-R and TDCA use), so it is defined at any time since the onset: scoring takes
``offset``, the window's first sample in seconds since the onset, and evaluates the template
there. That is what lets a sliding online window be matched against a template built from
4 s calibration trials. The spatial filters (CCA between template and references for eCCA,
TRCA / TRCA-R per target) are learned from the raw trials.

Scores are per sub-band features combined as sum_m w_m sign(f) f^2 with the FBCCA weights,
or a single band when ``bands`` is None. Features: ITCCA the first canonical correlation
with the template; ECCA the four signed squared correlations of Nakanishi 2015 eq. 14-15;
TRCA the correlation after that target's filter; ETRCA / ETRCAR the 2-D correlation after the
ensemble of all targets' filters; SFCCA the standard CCA score of the window after that
target's learned filter, which ignores the phase and so tolerates onset jitter.

    clf = make("etrca", freqs=[13, 15, 17, 11], fs=250)
    clf.fit(epochs, labels, t0=0.14)            # calibration trials
    clf.fit_baseline(still_windows)             # as for CCA
    pred, scores = clf.decide(window, offset=0.14 + 2.0)
    save_model(clf, "cache/templates/x.npz"); clf = load_model("cache/templates/x.npz")
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.linalg import eigh

from .classify import CCA, _span, min_samples, references
from .signal import Band, filterbank, filterbank_weights

__all__ = ["TEMPLATE_KINDS", "TemplateClassifier", "ITCCA", "ECCA", "TRCA", "ETRCA", "ETRCAR", "SFCCA", "CLASSES",
           "CVResult", "cca_weights", "cross_val_predict", "save_model", "load_model"]

_EIG_REG = 1e-6   # ridge added to the TRCA denominator, relative to its mean diagonal
# "harmonic": the trial average projected onto its sine/cosine subspace, defined at any offset
# (the online choice). "raw": the trial average itself, only for windows that lie inside the
# calibration epochs; it keeps the waveform detail the harmonics drop and reproduces the
# published offline results, but cannot score a window that runs past the epoch.
TEMPLATE_KINDS = ("harmonic", "raw")


# -- small linear algebra ---------------------------------------------------------------

def _center(A: np.ndarray) -> np.ndarray:
    return A - A.mean(axis=-1, keepdims=True)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation of two flattened arrays; 0 when either has no variance."""
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    a = a - a.mean()
    b = b - b.mean()
    den = np.sqrt((a @ a) * (b @ b))
    return float(a @ b / den) if den > 0 else 0.0


def _basis(A: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Rank-truncated orthonormal basis Q (n, k) of the centred columns of A (n, p), and the
    map M (p, k) with A_c M = Q, so weights on A are M @ (weights on Q)."""
    A = A - A.mean(axis=0, keepdims=True)
    u, sv, vt = np.linalg.svd(A, full_matrices=False)
    keep = sv > (sv[0] * max(A.shape) * np.finfo(float).eps if sv.size else 0.0)
    if not keep.any():
        return np.zeros((A.shape[0], 1)), np.zeros((A.shape[1], 1))
    return u[:, keep], vt[keep].T / sv[keep]


def cca_weights(X: np.ndarray, Y: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """CCA between X (p, n) and Y (q, n): x-weights (p,), y-weights (q,), first correlation.

    Both sides are reduced to their rank-truncated bases first, so a flat or duplicated
    channel, or a template of lower rank than its channel count (the harmonic templates
    with n_ch > 2H), neither fails nor inflates the correlation with numerically arbitrary
    null-space directions; the weights are the minimum-norm ones.
    """
    qx, mx = _basis(np.asarray(X, float).T)
    qy, my = _basis(np.asarray(Y, float).T)
    u, s, vt = np.linalg.svd(qx.T @ qy)
    return mx @ u[:, 0], my @ vt[0], float(np.clip(s[0], 0.0, 1.0))


def _first_cc(qx: np.ndarray, qy: np.ndarray) -> np.ndarray:
    """First canonical correlation between orthonormal spans qx (..., n, p) and qy (..., n, q)."""
    cross = np.swapaxes(qx, -1, -2) @ qy
    return np.clip(np.linalg.svd(cross, compute_uv=False)[..., 0], 0.0, 1.0)


def _top_eigvec(S: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """Eigenvector of the largest generalised eigenvalue of S w = lambda Q w, scaled so that
    w^T Q w = 1 (Nakanishi 2018 eq. 10): every filter in an ensemble then has unit output
    variance, so no single filter dominates the 2-D correlation."""
    tr = float(np.trace(Q))
    if not np.isfinite(tr) or tr <= 0:
        raise ValueError("the training trials have no variance; check the channels and the board")
    reg = _EIG_REG * tr / Q.shape[0]
    Qr = Q + reg * np.eye(Q.shape[0])
    vals, vecs = eigh((S + S.T) / 2.0, Qr)
    return vecs[:, -1]


def _filters(trials: np.ndarray, proj: np.ndarray | None) -> np.ndarray:
    """TRCA (proj None) or TRCA-R spatial filter from centred trials (Nt, n_ch, n).

    TRCA maximises the inter-trial covariance (Nakanishi 2018 eq. 8-11); TRCA-R first
    projects the summed trials onto the sine/cosine subspace ``proj`` (Wong 2020 eq. 26b).
    """
    total = trials.sum(axis=0)                                      # (n_ch, n)
    Q = np.einsum("tcn,tdn->cd", trials, trials)                    # sum_j X_j X_j^T
    if proj is None:
        S = total @ total.T - Q                                     # sum_{j != k} X_j X_k^T
    else:
        tp = total @ proj                                           # (n_ch, 2H)
        S = tp @ tp.T                                               # sum_{j,k} X_j P X_k^T
    return _top_eigvec(S, Q)


# -- classifiers ------------------------------------------------------------------------

class TemplateClassifier(CCA):
    """Base class: calibration-trained templates and filters behind the CCA API."""

    name = "template"
    uses_offset = True
    needs_fit = True
    min_trials = 1        # per target, for fit()

    def __init__(self, freqs: Sequence[float], fs: float, harmonics: int = 3,
                 phases: Sequence[float] | None = None, margin: float = 0.0,
                 threshold: float | None = None, bands: Sequence[Band] | None = None,
                 a: float = 1.25, b: float = 0.25, template: str = "harmonic") -> None:
        super().__init__(freqs, fs, harmonics, phases, margin, threshold)
        if template not in TEMPLATE_KINDS:
            raise ValueError(f"template must be one of {TEMPLATE_KINDS}, got {template!r}")
        self.template = template
        self.bands: list[Band] | None = None
        if bands is not None:
            chosen = [(float(lo), float(hi)) for lo, hi in bands]
            if not chosen:
                raise ValueError("bands=[] has no sub-band; pass None for a single band")
            for lo, hi in chosen:
                if not (0.0 <= lo < hi and lo < self.fs / 2.0):
                    raise ValueError(f"sub-band must satisfy 0 <= low < high and low < "
                                     f"{self.fs / 2.0} Hz, got ({lo}, {hi})")
            self.bands = chosen
        self.a, self.b = float(a), float(b)
        self.weights = filterbank_weights(len(self.bands), self.a, self.b) if self.bands \
            else np.ones(1)
        self.fitted = False
        self.n_ch = 0
        self.t0 = 0.0
        self.n_train = 0
        self.n_fit_samples = 0
        self.coef = np.empty(0)      # (n_bands, K, n_ch, 2H) harmonic template coefficients
        self.means = np.empty(0)     # (n_bands, K, n_ch, n_fit) raw trial averages (centred)
        self.w_tmpl = np.empty(0)    # (n_bands, K, n_ch) CCA filter: raw template vs references
        self.trca = None             # (n_bands, K, n_ch) TRCA filters, None with < 2 trials/target
        self.trcar = None            # (n_bands, K, n_ch) TRCA-R filters
        self.meta: dict = {}

    def __repr__(self) -> str:
        state = f"fitted on {self.n_train} trials" if self.fitted else "unfitted"
        return f"{super().__repr__()[:-1]}, bands={self.bands}, {state})"

    @property
    def n_bands(self) -> int:
        return len(self.bands) if self.bands else 1

    # -- fitting ---------------------------------------------------------------------

    def _check_training(self, trials: np.ndarray,
                        labels: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
        X = np.asarray(trials, dtype=float)
        y = np.asarray(labels).reshape(-1)
        if X.ndim != 3:
            raise ValueError(f"trials must be (n_trials, n_ch, n), got shape {X.shape}")
        if y.size != X.shape[0]:
            raise ValueError(f"{y.size} labels for {X.shape[0]} trials")
        if y.size and not np.issubdtype(y.dtype, np.integer) and not np.all(y == np.round(y)):
            raise ValueError(f"labels must be target indices, got {y.tolist()[:8]}")
        y = y.astype(int)
        if y.size and (y.min() < 0 or y.max() >= self.n_targets):
            raise ValueError(f"labels must index the {self.n_targets} targets, got range "
                             f"{int(y.min())}..{int(y.max())}")
        counts = np.bincount(y, minlength=self.n_targets) if y.size \
            else np.zeros(self.n_targets, int)
        short = [k for k, c in enumerate(counts) if c < self.min_trials]
        if short:
            raise ValueError(f"{self.name} needs at least {self.min_trials} trial(s) per target; "
                             f"targets {short} have {[int(counts[k]) for k in short]}")
        need = min_samples(X.shape[1], self.harmonics)
        if X.shape[2] < need:
            raise ValueError(f"trials of {X.shape[2]} samples are too short for {X.shape[1]} "
                             f"channels and {self.harmonics} harmonics (need >= {need})")
        if not np.isfinite(X).all():
            raise ValueError("trials contain NaN or inf")
        return X, y

    def fit(self, trials: np.ndarray, labels: Sequence[int],
            t0: float = 0.0) -> TemplateClassifier:
        """Learn templates and filters from epochs that start ``t0`` s after the onset."""
        X, y = self._check_training(trials, labels)
        t0 = float(t0)
        if not np.isfinite(t0):
            raise ValueError(f"t0 must be a finite time in seconds, got {t0}")
        n_trials, n_ch, n = X.shape
        K, H2 = self.n_targets, 2 * self.harmonics
        refs = references(self.freqs, self.fs, n, self.harmonics, self.phases, t0)  # (K, 2H, n)
        spans = _span(np.swapaxes(refs, 1, 2))                                        # (K, n, 2H)
        banded = filterbank(X, self.fs, self.bands) if self.bands else X[None]
        coef = np.zeros((self.n_bands, K, n_ch, H2))
        means = np.zeros((self.n_bands, K, n_ch, n))
        w_tmpl = np.zeros((self.n_bands, K, n_ch))
        enough = bool(np.all(np.bincount(y, minlength=K) >= 2))
        trca = np.zeros((self.n_bands, K, n_ch)) if enough else None
        trcar = np.zeros((self.n_bands, K, n_ch)) if enough else None
        for m, Xb in enumerate(banded):
            Xb = _center(Xb)
            for k in range(K):
                Xk = Xb[y == k]                                             # (Nt, n_ch, n)
                if not np.any(Xk):
                    raise ValueError(f"target {k}'s training trials are flat (zero variance); "
                                     "check the channels and the board")
                mean = Xk.mean(axis=0)                                      # (n_ch, n)
                means[m, k] = mean
                coef[m, k] = np.linalg.lstsq(refs[k].T, mean.T, rcond=None)[0].T
                w_tmpl[m, k] = cca_weights(mean, refs[k])[0]
                if enough:
                    trca[m, k] = _filters(Xk, None)              # type: ignore[index]
                    trcar[m, k] = _filters(Xk, spans[k])         # type: ignore[index]
        self.coef, self.means, self.w_tmpl, self.trca, self.trcar = coef, means, w_tmpl, trca, trcar
        self.n_ch, self.t0, self.n_train, self.n_fit_samples = n_ch, t0, n_trials, n
        self.fitted = True
        return self

    # -- templates -------------------------------------------------------------------

    def templates(self, n: int, offset: float = 0.0, band: int = 0) -> np.ndarray:
        """Templates (K, n_ch, n) for a window of ``n`` samples starting ``offset`` s after the onset."""
        self._require_fit()
        if self.template == "raw":
            start = int(round((offset - self.t0) * self.fs))
            if start < 0 or start + n > self.n_fit_samples:
                raise ValueError(f"raw templates cover {self.t0:g}..{self.t0 + self.n_fit_samples / self.fs:g} s "
                                 f"after the onset; a {n / self.fs:g} s window at {offset:g} s does "
                                 f"not fit (use template='harmonic' for any offset)")
            return self.means[band][:, :, start:start + n]
        refs = references(self.freqs, self.fs, n, self.harmonics, self.phases, offset)
        return np.einsum("kcj,kjn->kcn", self.coef[band], refs)

    def _require_fit(self) -> None:
        if not self.fitted:
            raise ValueError(f"{self.name} has no templates yet: call fit(trials, labels, t0) first")

    def fit_baseline(self, X_rest: np.ndarray, offset: float | None = None) -> TemplateClassifier:
        """As for CCA; still windows carry no flicker, so their offset is a convention: the
        epoch start ``t0`` by default, which raw templates always cover."""
        super().fit_baseline(X_rest, self.t0 if offset is None else float(offset))
        return self

    def covers(self, n: int, offset: float) -> bool:
        """Can a window of ``n`` samples starting ``offset`` s after the onset be scored?
        Always with harmonic templates; raw templates only inside the calibration epoch."""
        if self.template != "raw":
            return True
        start = int(round((offset - self.t0) * self.fs))
        return 0 <= start and start + n <= self.n_fit_samples

    def _windows(self, X: np.ndarray) -> np.ndarray:
        X = super()._windows(X)
        if self.fitted and X.shape[1] != self.n_ch:
            raise ValueError(f"{self.name} was fitted on {self.n_ch} channels, got {X.shape[1]}")
        return X

    # -- scoring ---------------------------------------------------------------------

    def raw_scores(self, X: np.ndarray, offset: float = 0.0) -> np.ndarray:
        self._require_fit()
        X = self._windows(X)
        offset = float(offset)
        if not np.isfinite(offset):
            raise ValueError(f"offset must be a finite time in seconds, got {offset}")
        banded = filterbank(X, self.fs, self.bands) if self.bands else X[None]
        total = np.zeros((X.shape[0], self.n_targets))
        for m, (w, Xb) in enumerate(zip(self.weights, banded)):
            f = self._band_scores(_center(Xb), m, offset)
            total += w * np.sign(f) * f ** 2
        return total

    def _band_scores(self, X: np.ndarray, band: int, offset: float) -> np.ndarray:
        """Features (n_trials, K) for centred windows X (n_trials, n_ch, n) of one sub-band."""
        raise NotImplementedError


class ITCCA(TemplateClassifier):
    """Individual-template CCA (Bin 2011; Nakanishi 2015): canonical correlation with the template."""

    name = "itcca"

    def _band_scores(self, X: np.ndarray, band: int, offset: float) -> np.ndarray:
        T = self.templates(X.shape[-1], offset, band)                 # (K, n_ch, n)
        qx = _span(np.swapaxes(X, 1, 2))                               # (trials, n, n_ch)
        qt = _span(np.swapaxes(T, 1, 2))                               # (K, n, n_ch)
        return _first_cc(qx[:, None], qt[None])                        # (trials, K)


class ECCA(TemplateClassifier):
    """CCA + individual-template combination (Nakanishi 2014/2015, eq. 14-15).

    Four correlations per target, summed as sign(r) r^2: the standard CCA with the sine/cosine
    references; test window vs template through the filter CCA(test, template); through the
    filter CCA(test, references); and through the filter CCA(template, references) learned
    at fit time from the raw trial average.

    With harmonic templates the template lies inside the sine/cosine span, so when the window
    has at least 2H channels the second and third correlations coincide (the score becomes
    r1^2 + 2 r3^2 + r4^2) and ITCCA equals standard CCA; the published four-term behaviour
    needs ``template="raw"``, which the Nakanishi 2015 reproduction uses.
    """

    name = "ecca"

    def _band_scores(self, X: np.ndarray, band: int, offset: float) -> np.ndarray:
        n = X.shape[-1]
        T = self.templates(n, offset, band)
        refs = references(self.freqs, self.fs, n, self.harmonics, self.phases, offset)
        out = np.zeros((X.shape[0], self.n_targets))
        for i, x in enumerate(X):
            for k in range(self.n_targets):
                wx1, _, r1 = cca_weights(x, refs[k])
                wx2, _, _ = cca_weights(x, T[k])
                w4 = self.w_tmpl[band, k]
                r2 = _corr(wx2 @ x, wx2 @ T[k])
                r3 = _corr(wx1 @ x, wx1 @ T[k])
                r4 = _corr(w4 @ x, w4 @ T[k])
                rs = np.array([r1, r2, r3, r4])
                out[i, k] = float(np.sum(np.sign(rs) * rs ** 2))
        return out


class TRCA(TemplateClassifier):
    """Task-related component analysis (Nakanishi 2018): one spatial filter per target,
    correlation between the filtered window and the filtered template."""

    name = "trca"
    min_trials = 2
    ensemble = False
    regularised = False

    def _bank(self, band: int) -> np.ndarray:
        W = self.trcar if self.regularised else self.trca
        if W is None:
            raise ValueError(f"{self.name} needs at least 2 trials per target to learn filters")
        return W[band].T                                               # (n_ch, K)

    def _band_scores(self, X: np.ndarray, band: int, offset: float) -> np.ndarray:
        T = self.templates(X.shape[-1], offset, band)                 # (K, n_ch, n)
        W = self._bank(band)
        Xp = np.einsum("tcn,ck->tkn", X, W)                            # (trials, K filters, n)
        Tp = np.einsum("kcn,cj->kjn", T, W)                            # (K, K filters, n)
        out = np.zeros((X.shape[0], self.n_targets))
        for i in range(X.shape[0]):
            for k in range(self.n_targets):
                if self.ensemble:
                    out[i, k] = _corr(Xp[i], Tp[k])
                else:
                    out[i, k] = _corr(Xp[i, k], Tp[k, k])
        return out


class ETRCA(TRCA):
    """Ensemble TRCA (Nakanishi 2018): every target's filter applied to each comparison."""

    name = "etrca"
    ensemble = True


class ETRCAR(TRCA):
    """Ensemble TRCA-R (Wong 2020): filters learned from trials projected onto the
    sine/cosine subspace, which holds up with few calibration trials."""

    name = "etrcar"
    ensemble = True
    regularised = True


class SFCCA(TemplateClassifier):
    """Phase-free trained scoring: each target's learned spatial filter (TRCA-R, or the
    template-vs-references CCA filter with a single training trial) reduces the window to one
    channel, which is then scored like standard CCA against the sine/cosine references.

    The filter removes background EEG the way TRCA does, but the score ignores the phase, so
    it survives an uncertain stimulus onset (jitter between the display's marker and the
    board's clock), which the template correlations in the other classes do not.
    """

    name = "sfcca"
    uses_offset = False     # nothing here depends on the window's position in the trial

    def _band_scores(self, X: np.ndarray, band: int, offset: float) -> np.ndarray:
        W = (self.trcar if self.trcar is not None else self.w_tmpl)[band]   # (K, n_ch)
        n = X.shape[-1]
        refs = references(self.freqs, self.fs, n, self.harmonics, self.phases)
        qy = _span(np.swapaxes(refs, 1, 2))                             # (K, n, 2H)
        out = np.zeros((X.shape[0], self.n_targets))
        for k in range(self.n_targets):
            s = np.einsum("c,tcn->tn", W[k], X)                            # (trials, n)
            qx = _span(s[..., None])                                       # (trials, n, 1)
            out[:, k] = _first_cc(qx, qy[k][None])
        return out


CLASSES: tuple[type[TemplateClassifier], ...] = (ITCCA, ECCA, TRCA, ETRCA, ETRCAR, SFCCA)


# -- cross-validation ---------------------------------------------------------------------

@dataclass
class CVResult:
    """Leave-one-block-out output: per-trial predictions (ABSTAIN where the margin or
    threshold held back), baseline-corrected scores, the same for the rest windows when
    given, and each fold's baseline mean and spread."""

    pred: np.ndarray
    scores: np.ndarray
    rest_scores: np.ndarray | None
    baseline_means: np.ndarray     # (n_folds, K)
    baseline_spreads: np.ndarray   # (n_folds,)

    @property
    def n_folds(self) -> int:
        return int(self.baseline_spreads.size)


def cross_val_predict(factory: Callable[[], CCA], X: np.ndarray, y: Sequence[int],
                      blocks: Sequence[int], t0: float, rest: np.ndarray | None = None,
                      offset: float | None = None) -> CVResult:
    """Leave-one-block-out predictions and scores for every trial.

    For each block, a fresh classifier from ``factory`` is fitted on the other blocks' trials
    (and, when ``rest`` windows are given, its baseline on their rest windows) and scores
    this block's trials at ``offset`` (default ``t0``) and, with ``rest``, this block's rest
    windows at offset 0. Training-free classifiers are fitted on nothing and simply scored,
    so the same call compares both kinds.
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y).reshape(-1).astype(int)
    blocks = np.asarray(blocks).reshape(-1)
    if X.ndim != 3 or y.size != X.shape[0] or blocks.size != X.shape[0]:
        raise ValueError(f"need X (n_trials, n_ch, n) with one label and one block per trial, "
                         f"got X{X.shape}, {y.size} labels, {blocks.size} blocks")
    if rest is not None:
        rest = np.asarray(rest, dtype=float)
        if rest.shape[0] != X.shape[0]:
            raise ValueError(f"rest windows ({rest.shape[0]}) must match trials ({X.shape[0]})")
    uniq = np.unique(blocks)
    if uniq.size < 2:
        raise ValueError(f"cross-validation needs at least 2 blocks, got {uniq.tolist()}")
    off = t0 if offset is None else float(offset)
    pred = np.zeros(y.size, dtype=int)
    scores: np.ndarray | None = None
    rest_scores: np.ndarray | None = None
    means, spreads = [], []
    for b in uniq:
        test = blocks == b
        train = ~test
        clf = factory()
        if getattr(clf, "needs_fit", False):
            missing = sorted(set(range(clf.n_targets)) - set(y[train].tolist()))
            if missing:
                raise ValueError(f"leaving block {b} out leaves targets {missing} with no "
                                 f"training trial")
            clf.fit(X[train], y[train], t0)   # type: ignore[attr-defined]
        if rest is not None:
            clf.fit_baseline(rest[train])
        p, s = clf.decide(X[test], off)
        if scores is None:
            scores = np.zeros((y.size, s.shape[1]))
            rest_scores = np.zeros_like(scores) if rest is not None else None
        pred[test] = p
        scores[test] = s
        if rest is not None and rest_scores is not None:
            rest_scores[test] = clf.scores(rest[test], getattr(clf, "t0", 0.0))
        means.append(clf.baseline_mean.copy())
        spreads.append(clf.baseline_spread)
    assert scores is not None
    return CVResult(pred, scores, rest_scores, np.array(means), np.array(spreads))


# -- saving and loading ----------------------------------------------------------------------

def save_model(clf: TemplateClassifier, path: str | Path, **meta: object) -> Path:
    """Save a fitted classifier (templates, filters, baseline, settings) as .npz.

    ``meta`` (strings, numbers, lists) is kept as JSON, for the recording path and the like.
    """
    if not isinstance(clf, TemplateClassifier):
        raise ValueError(f"only trained classifiers are saved, got {type(clf).__name__}")
    clf._require_fit()
    path = Path(path)
    if path.suffix != ".npz":
        raise ValueError(f"model path must end in .npz, got {path}")
    nan = np.full_like(clf.w_tmpl, np.nan)
    payload = {
        "name": np.array(clf.name), "fs": np.float64(clf.fs), "freqs": clf.freqs,
        "phases": clf.phases, "harmonics": np.int64(clf.harmonics),
        "margin": np.float64(clf.margin),
        "threshold": np.float64(np.nan if clf.threshold is None else clf.threshold),
        "bands": np.asarray(clf.bands if clf.bands else np.empty((0, 2)), dtype=float),
        "a": np.float64(clf.a), "b": np.float64(clf.b), "t0": np.float64(clf.t0),
        "n_ch": np.int64(clf.n_ch), "n_train": np.int64(clf.n_train),
        "n_fit_samples": np.int64(clf.n_fit_samples),
        "coef": clf.coef, "means": clf.means, "w_tmpl": clf.w_tmpl,
        "template": np.array(clf.template),
        "trca": nan if clf.trca is None else clf.trca,
        "trcar": nan if clf.trcar is None else clf.trcar,
        "has_baseline": np.bool_(clf.has_baseline), "baseline_mean": clf.baseline_mean,
        "baseline_spread": np.float64(clf.baseline_spread),
        "meta": np.array(json.dumps(meta, default=str)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, **payload)
    return path


def load_model(path: str | Path, name: str | None = None) -> TemplateClassifier:
    """Load a saved model; ``name`` picks another trained classifier over the same templates
    (any of them can be scored from one fit, as long as it has the filters it needs)."""
    from .classify import make

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"model not found: {path}")
    with np.load(path, allow_pickle=False) as d:
        for key in ("name", "fs", "freqs", "coef", "w_tmpl", "t0", "n_ch"):
            if key not in d.files:
                raise KeyError(f"{path}: missing key {key!r} (has {d.files})")
        thr = float(d["threshold"])
        bands = d["bands"]
        clf = make(name or str(d["name"]), freqs=d["freqs"], fs=float(d["fs"]),
                   harmonics=int(d["harmonics"]), phases=d["phases"], margin=float(d["margin"]),
                   threshold=None if np.isnan(thr) else thr,
                   bands=[tuple(b) for b in bands] if bands.size else None,
                   a=float(d["a"]), b=float(d["b"]),
                   template=str(d["template"]) if "template" in d.files else "harmonic")
        if not isinstance(clf, TemplateClassifier):
            raise ValueError(f"{path}: {clf.name} is not a trained classifier")
        clf.coef, clf.w_tmpl = d["coef"], d["w_tmpl"]
        clf.means = d["means"] if "means" in d.files else np.empty(0)
        trca, trcar = d["trca"], d["trcar"]
        clf.trca = None if np.isnan(trca).all() else trca
        clf.trcar = None if np.isnan(trcar).all() else trcar
        clf.t0, clf.n_ch = float(d["t0"]), int(d["n_ch"])
        clf.n_train, clf.n_fit_samples = int(d["n_train"]), int(d["n_fit_samples"])
        want = (clf.n_bands, clf.n_targets, clf.n_ch)
        if clf.coef.shape != (*want, 2 * clf.harmonics):
            raise ValueError(f"{path}: coef shape {clf.coef.shape} does not match {clf.n_bands} "
                             f"band(s), {clf.n_targets} targets, {clf.n_ch} channels and "
                             f"{clf.harmonics} harmonics")
        if clf.w_tmpl.shape != want:
            raise ValueError(f"{path}: w_tmpl shape {clf.w_tmpl.shape}, expected {want}")
        for label, arr in (("trca", clf.trca), ("trcar", clf.trcar)):
            if arr is not None and arr.shape != want:
                raise ValueError(f"{path}: {label} shape {arr.shape}, expected {want}")
        if clf.template == "raw" and clf.means.shape != (*want, clf.n_fit_samples):
            raise ValueError(f"{path}: raw templates need means shaped {(*want, clf.n_fit_samples)}, "
                             f"got {clf.means.shape}")
        if isinstance(clf, TRCA) and (clf.trcar if clf.regularised else clf.trca) is None:
            raise ValueError(f"{path}: {clf.name} needs TRCA filters, which this model lacks "
                             "(it was fitted with a single trial per target)")
        clf.fitted = True
        if bool(d["has_baseline"]):
            clf.baseline_mean = d["baseline_mean"]
            clf.baseline_spread = float(d["baseline_spread"])
            clf.has_baseline = True
        clf.meta = json.loads(str(d["meta"])) if "meta" in d.files else {}
    return clf
