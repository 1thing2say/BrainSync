"""A classifier trained on the user's own cued trials: phase-invariant filter-bank eTRCA,
fused with FBCCA by a cross-validated gate.

Training (aligned cued trials, per sub-band m of the FBCCA filter bank):

* TRCA spatial filters per target k (Nakanishi et al. 2018): the top ``n_comp``
  generalised eigenvectors of S w = lambda Q w, with S = sum_{i != j} X_i X_j^T (what the
  trials of target k share) and Q = sum_i X_i X_i^T (all their variance, plus a small
  ridge). The ensemble W stacks every target's filters (eTRCA), so one W serves all
  targets.
* A per-channel harmonic model of target k's trial average: the least-squares fit of the
  average onto sin(h theta) and cos(h theta), h = 1..H, theta = 2 pi f t + phase. It keeps
  what an eTRCA template knows (spatial pattern, harmonic amplitudes and their relative
  phases) and drops what a live window cannot know: its absolute phase, i.e. when the
  flicker started. A time shift tau of the window is exactly a rotation of harmonic h by
  h * psi with psi = 2 pi f tau.

Scoring a window of any length with no onset: per band, the largest correlation over a
phase grid psi between the filtered window W^T X and the filtered harmonic template
W^T T_k(psi), computed in closed form from the window's projections on the sine/cosine
references; then sign(r) * r**2, summed over bands with the FBCCA weights m**-a + b.

Scoring with the onset known (phase-locked): the same correlation at one psi per target,
``phase_offsets(t)`` = 2 pi f_k (t - latency) for a window starting t seconds after the
flicker onset, where latency is the offset of the training windows from their stim_on. A
locked score can only be lower than the free one, and the template's own phase is then
part of the evidence: on the 2026-10-24_235732 recording, onset-aligned 2 s windows
decoded at 76 % locked against 62 % free (FBCCA 69 %). The rest baseline for locked
scoring is fitted at random phases (``fit_baseline(..., locked=True)``), which is what
noise scores under a lock.

The fused score is a_trca * eTRCA + a_cca * FBCCA, summed band by band. ``fusion="cv"``
picks (a_trca, a_cca) from FUSION_GRID by an inner cross-validation on the training trials
(held-out random-offset windows of CV_WINDOW_S, as the sliding live decoder sees them), so
a poor recording falls back towards plain FBCCA ((0, 1) is exactly FBCCA). The baseline
and abstain rules are FBCCA's: ``fit`` changes the score scale, so it clears the baseline
and ``fit_baseline`` must run again on still-screen windows (every session).
"""
from __future__ import annotations

import os
import re
import tempfile
import time
import zipfile
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.linalg import eigh

from .classify import CACHE_SIZE, FBCCA, references
from .paths import models_dir
from .signal import Band, filterbank

__all__ = ["TRCA", "ModelInfo", "FUSION_GRID", "load_model", "latest_model",
           "check_compatible", "default_model_path"]

# (a_trca, a_cca) candidates for the CV gate, in tie-break order; (0, 1) is plain FBCCA
FUSION_GRID = ((1.0, 0.0), (1.0, 0.1), (1.0, 0.3), (1.0, 1.0), (1.0, 3.0), (0.0, 1.0))
N_COMP = 2           # TRCA filters kept per target
N_PSI = 48           # phase grid: 7.5 deg at the fundamental, 22.5 deg at h = 3
RIDGE = 1e-6         # Q += RIDGE * trace(Q) / n_ch * I
CV_WINDOW_S = 1.5    # held-out window length in the CV gate (the live window)
CV_OFFSETS = 4       # random-offset windows per held-out trial
CV_SEED = 12345
MODEL_FORMAT = 1
MIN_TRIALS = 2       # per target, to fit (TRCA needs two trials to find what they share)
MIN_CV_TRIALS = 3    # per target, to run the CV gate (each fold keeps >= MIN_TRIALS)

_TINY = 1e-300
# default_model_path names: <date>_<HHMMSS>_trca.npz, then <date>_<HHMMSS>_<n>_trca.npz
_MODEL_RE = re.compile(r"^(?P<base>\d{4}-\d{2}-\d{2}_\d{6})(?:_(?P<n>\d+))?_trca\.npz$")
# what a damaged, truncated or foreign file raises from np.load or from reading a key
_UNREADABLE = (OSError, EOFError, ValueError, TypeError, zipfile.BadZipFile)
_KEYS = ("format", "kind", "freqs", "phases", "fs", "harmonics", "bands", "a", "b", "n_comp",
         "n_psi", "ridge", "W", "A", "weights", "n_channels", "cv_pairs", "cv_acc",
         "cv_skipped", "ch_names", "band", "notch", "latency", "trial_s", "sources", "created")


@dataclass(frozen=True)
class ModelInfo:
    """Where a model came from: the preprocessing and channels its trials went through."""

    ch_names: tuple[str, ...]
    band: tuple[float, float] | None
    notch: float
    latency: float
    trial_s: float
    sources: tuple[str, ...]
    created: float


def _check_fusion(fusion) -> str | tuple[float, float]:
    if isinstance(fusion, str):
        if fusion != "cv":
            raise ValueError(f'fusion must be "cv" or a pair (a_trca, a_cca), got {fusion!r}')
        return fusion
    try:
        pair = tuple(float(v) for v in fusion)
    except (TypeError, ValueError):
        raise ValueError(f'fusion must be "cv" or a pair (a_trca, a_cca), got {fusion!r}') from None
    if len(pair) != 2 or not all(np.isfinite(v) and v >= 0 for v in pair) or pair == (0.0, 0.0):
        raise ValueError(f"fusion weights must be two finite numbers >= 0, not both 0, "
                         f"got {fusion!r}")
    return pair


def _centred_refs(freqs: np.ndarray, fs: float, n: int, harmonics: int,
                  phases: np.ndarray) -> np.ndarray:
    """(K, 2H, n) sin rows then cos rows (classify.references), centred over time."""
    R = references(freqs, fs, n, harmonics, phases)
    return R - R.mean(axis=-1, keepdims=True)


def _trca_filters(Xk: np.ndarray, n_comp: int, ridge: float) -> np.ndarray:
    """(n_ch, n_comp) TRCA filters from one target's centred trials Xk (n_trials, n_ch, n)."""
    s = Xk.sum(axis=0)
    Q = np.einsum("icn,idn->cd", Xk, Xk)
    S = s @ s.T - Q                                   # sum over pairs i != j of X_i X_j^T
    Q = Q + ridge * np.trace(Q) / Q.shape[0] * np.eye(Q.shape[0])
    _, vecs = eigh(S, Q)                              # ascending eigenvalues
    return vecs[:, ::-1][:, :n_comp]


class TRCA(FBCCA):
    """Phase-invariant filter-bank eTRCA fused with FBCCA; needs ``fit`` on cued trials.

    ``weights`` is the fusion pair (a_trca, a_cca); the FBCCA sub-band weights are
    ``band_weights``.
    """

    # "trca" is brainsync.templates' TRCA (classify.make, brainsync.calibrate); this one is
    # loaded from a brainsync.train model (demo --classifier trca-phase)
    name = "trca-phase"

    def __init__(self, freqs: Sequence[float], fs: float, harmonics: int = 3,
                 phases: Sequence[float] | None = None, margin: float = 0.0,
                 bands: Sequence[Band] | None = None, a: float = 1.25, b: float = 0.25,
                 threshold: float | None = None, n_comp: int = N_COMP, n_psi: int = N_PSI,
                 ridge: float = RIDGE, fusion: str | tuple[float, float] = "cv") -> None:
        super().__init__(freqs, fs, harmonics, phases, margin, bands, a, b, threshold)
        if isinstance(n_comp, bool) or int(n_comp) != n_comp or n_comp < 1:
            raise ValueError(f"n_comp must be an integer >= 1, got {n_comp}")
        if isinstance(n_psi, bool) or int(n_psi) != n_psi or n_psi < 4:
            raise ValueError(f"n_psi must be an integer >= 4, got {n_psi}")
        ridge = float(ridge)
        if not np.isfinite(ridge) or ridge < 0:
            raise ValueError(f"ridge must be finite and >= 0, got {ridge}")
        self.n_comp, self.n_psi, self.ridge = int(n_comp), int(n_psi), ridge
        self.fusion = _check_fusion(fusion)
        self.band_weights = self.weights              # FBCCA's m**-a + b
        self.weights: tuple[float, float] = (1.0, 1.0) if self.fusion == "cv" else self.fusion
        self.fitted = False
        self.n_channels: int | None = None
        self.info: ModelInfo | None = None
        self.cv_table: dict[tuple[float, float], float] = {}
        self.cv_skipped = False
        self._gram: OrderedDict[int, tuple[np.ndarray, np.ndarray]] = OrderedDict()

    def __repr__(self) -> str:
        extra = f", n_comp={self.n_comp}, weights={self.weights}"
        if self.info is not None:
            extra += f", model={list(self.info.sources)}"
        return f"{super().__repr__()[:-1]}{extra})"

    # -- training --------------------------------------------------------------------

    def fit(self, X: np.ndarray, y: Sequence[int], info: ModelInfo | None = None) -> TRCA:
        """Train on cued trials X (n_trials, n_ch, n) with targets y (n_trials,).

        X must be preprocessed like the live windows (on the whole recording, as
        recordings.cut_windows does) and start at stim_on + latency. With fusion "cv" the
        fusion weights come from an inner CV when every target has MIN_CV_TRIALS trials,
        else (1, 1) and ``cv_skipped``. Clears the baseline: call fit_baseline afterwards.
        """
        X = self._windows(X)
        y_arr = np.asarray(y)
        if y_arr.ndim != 1 or y_arr.size != X.shape[0]:
            raise ValueError(f"y must be 1-D with one label per trial ({X.shape[0]}), "
                             f"got shape {y_arr.shape}")
        if y_arr.dtype.kind not in "iuf" or not np.all(np.isfinite(y_arr)) \
                or not np.all(y_arr == np.round(y_arr)):
            raise ValueError(f"labels must be integers, got {y_arr.tolist()}")
        y_arr = y_arr.astype(int)
        bad = (y_arr < 0) | (y_arr >= self.n_targets)
        if bad.any():
            raise ValueError(f"labels must lie in [0, {self.n_targets}), got "
                             f"{sorted(set(y_arr[bad].tolist()))}")
        if self.n_comp > X.shape[1]:
            raise ValueError(f"n_comp {self.n_comp} exceeds the {X.shape[1]} channel(s): TRCA "
                             f"finds at most one filter per channel")
        counts = np.bincount(y_arr, minlength=self.n_targets)
        for k, c in enumerate(counts):
            if c < MIN_TRIALS:
                raise ValueError(f"target {self.freqs[k]:g} Hz has {c} training trials; "
                                 f"need at least {MIN_TRIALS}")
        self.cv_table = {}
        self.cv_skipped = False
        if self.fusion == "cv":
            if counts.min() >= MIN_CV_TRIALS:
                self.weights = self._cv_weights(X, y_arr)
            else:
                self.weights = (1.0, 1.0)
                self.cv_skipped = True
        self._fit_templates(X, y_arr)
        self.n_channels = int(X.shape[1])
        self.fitted = True
        self.info = info
        self.clear_baseline()          # the score scale changed: fit_baseline must follow
        return self

    def _fit_templates(self, X: np.ndarray, y: np.ndarray) -> None:
        """Ensemble filters W and harmonic coefficients A per band from validated trials."""
        n_ch, n = X.shape[1], X.shape[2]
        H = self.harmonics
        R = _centred_refs(self.freqs, self.fs, n, H, self.phases)        # (K, 2H, n)
        Xb = filterbank(X, self.fs, self.bands)                          # (B, trials, ch, n)
        Xb = Xb - Xb.mean(axis=-1, keepdims=True)
        n_bands, K = Xb.shape[0], self.n_targets
        W = np.zeros((n_bands, n_ch, K * self.n_comp))
        A = np.zeros((n_bands, K, n_ch, 2 * H))
        for m in range(n_bands):
            for k in range(K):
                Xk = Xb[m, y == k]
                W[m, :, k * self.n_comp:(k + 1) * self.n_comp] = \
                    _trca_filters(Xk, self.n_comp, self.ridge)
                # least squares of the trial average onto the refs: mean R^T (R R^T)^-1
                Rk = R[k]
                A[m, k] = np.linalg.solve(Rk @ Rk.T, Rk @ Xk.mean(axis=0).T).T
        self.W, self.A = W, A
        self._prepare()

    def _prepare(self) -> None:
        """Filtered, phase-rotated template coefficients _Bpsi (B, K, n_psi, d, 2H)."""
        H = self.harmonics
        psi = 2.0 * np.pi * np.arange(self.n_psi) / self.n_psi
        hpsi = np.outer(psi, np.arange(1, H + 1))                       # (G, H)
        c, s = np.cos(hpsi)[None, None, :, None], np.sin(hpsi)[None, None, :, None]
        a, b = self.A[..., :H][:, :, None], self.A[..., H:][:, :, None]  # (B, K, 1, ch, H)
        # theta -> theta + psi rotates harmonic h by h psi
        Arot = np.concatenate([a * c - b * s, a * s + b * c], axis=-1)  # (B, K, G, ch, 2H)
        self._Bpsi = np.einsum("bcd,bkgcj->bkgdj", self.W, Arot)
        self._gram.clear()

    # -- scoring ---------------------------------------------------------------------

    def _refs(self, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Centred refs (K, 2H, n), template norms (B, K, n_psi) and the refs' Gram
        matrices (K, 2H, 2H), LRU-cached by n."""
        hit = self._gram.get(n)
        if hit is None:
            R = _centred_refs(self.freqs, self.fs, n, self.harmonics, self.phases)
            G = R @ np.swapaxes(R, 1, 2)                                  # (K, 2H, 2H)
            # ||W^T A(psi) R||_F^2 = sum_d B G B^T
            tn = np.einsum("bkgdi,kij,bkgdj->bkg", self._Bpsi, G, self._Bpsi)
            hit = (R, np.sqrt(np.maximum(tn, _TINY)), G)
            self._gram[n] = hit
            while len(self._gram) > CACHE_SIZE:
                self._gram.popitem(last=False)
        else:
            self._gram.move_to_end(n)
        return hit

    def _rotated(self, m: int, psi: np.ndarray) -> np.ndarray:
        """Band m's filtered template coefficients (N, K, d, 2H) with target k's harmonics
        rotated by h * psi[n, k] (psi (N, K), N 1 for one rotation per target)."""
        H = self.harmonics
        hpsi = psi[..., None] * np.arange(1, H + 1)                      # (N, K, H)
        c, s = np.cos(hpsi)[:, :, None], np.sin(hpsi)[:, :, None]        # (N, K, 1, H)
        a, b = self.A[m][None, ..., :H], self.A[m][None, ..., H:]        # (1, K, ch, H)
        Arot = np.concatenate([a * c - b * s, a * s + b * c], axis=-1)   # (N, K, ch, 2H)
        return np.einsum("cd,nkcj->nkdj", self.W[m], Arot)

    def _trca_part(self, Xm: np.ndarray, m: int, psi: np.ndarray | None = None) -> np.ndarray:
        """sign(r) * r**2 (N, K) for band m's centred windows Xm (N, C, n): r maximised over
        the psi grid, or taken at ``psi`` ((K,) or (N, K) radians) when given."""
        R, tnorm, G = self._refs(Xm.shape[-1])
        Y = np.einsum("cd,wcn->wdn", self.W[m], Xm)                     # filtered windows
        P = np.einsum("wdn,kjn->wkdj", Y, R)                            # projections on refs
        ynorm = np.sqrt(np.einsum("wdn,wdn->w", Y, Y)) + _TINY
        if psi is None:
            num = np.einsum("kgdj,wkdj->wkg", self._Bpsi[m], P)
            r = (num / (ynorm[:, None, None] * tnorm[m][None])).max(axis=-1)
        else:
            Bk = self._rotated(m, psi)                                   # (1 or N, K, d, 2H)
            tn = np.sqrt(np.maximum(np.einsum("nkdi,kij,nkdj->nk", Bk, G, Bk), _TINY))
            r = np.einsum("nkdj,wkdj->wk", Bk, P) if Bk.shape[0] == 1 \
                else np.einsum("wkdj,wkdj->wk", Bk, P)
            r = r / (ynorm[:, None] * tn)
        return np.sign(r) * r ** 2

    def _parts(self, X: np.ndarray, trca: bool = True, cca: bool = True,
               psi: np.ndarray | None = None) -> tuple[np.ndarray | None, np.ndarray | None]:
        """Band-weighted eTRCA and FBCCA scores (N, K) of validated X, filtered once."""
        t_sum = np.zeros((X.shape[0], self.n_targets)) if trca else None
        c_sum = np.zeros((X.shape[0], self.n_targets)) if cca else None
        for m, (w, Xb) in enumerate(zip(self.band_weights, filterbank(X, self.fs, self.bands))):
            if trca:
                t_sum += w * self._trca_part(Xb - Xb.mean(axis=-1, keepdims=True), m, psi)
            if cca:
                c_sum += w * self._rho(Xb) ** 2
        return t_sum, c_sum

    def _psi(self, psi: object, n: int) -> np.ndarray | None:
        """``psi`` validated as (1, K) or (n, K) radians; None stays None."""
        if psi is None:
            return None
        p = np.asarray(psi, dtype=float)
        if p.ndim == 1:
            p = p[None]
        if p.ndim != 2 or p.shape[1] != self.n_targets or p.shape[0] not in (1, n) \
                or not np.all(np.isfinite(p)):
            raise ValueError(f"psi must be {self.n_targets} finite phases, or one row of them "
                             f"per window ({n}), got shape {p.shape}")
        return p

    @property
    def latency(self) -> float:
        """Seconds from stim_on to the start of the training windows (0 without info)."""
        return 0.0 if self.info is None else float(self.info.latency)

    def phase_offsets(self, t: float | np.ndarray) -> np.ndarray:
        """The psi (K,) that locks each template to a window starting ``t`` seconds after
        the flicker onset: 2 pi f_k (t - latency), the template's phase advance over a
        training window's. An array of N starts gives (N, K)."""
        starts = np.asarray(t, dtype=float) - self.latency
        return 2.0 * np.pi * np.multiply.outer(starts, self.freqs)

    def raw_scores(self, X: np.ndarray, offset: float = 0.0, psi: object = None) -> np.ndarray:
        """a_trca * eTRCA + a_cca * FBCCA (n_trials, n_targets); a part weighted 0 is
        skipped. ``psi`` ((K,) or (n_trials, K) radians) locks the templates' phase.
        ``offset`` (classify.CCA's API) changes nothing: phase-free scores are
        shift-invariant, and a lock comes only from ``psi`` (phase_offsets)."""
        if not self.fitted:
            raise ValueError("TRCA model is not trained: call fit() or load_model()")
        X = self._windows(X)
        if X.shape[1] != self.n_channels:
            raise ValueError(f"model was trained on {self.n_channels} channels, "
                             f"got {X.shape[1]}")
        a_t, a_c = self.weights
        t_sum, c_sum = self._parts(X, trca=a_t != 0, cca=a_c != 0,
                                   psi=self._psi(psi, X.shape[0]))
        total = np.zeros((X.shape[0], self.n_targets))
        if t_sum is not None:
            total += a_t * t_sum
        if c_sum is not None:
            total += a_c * c_sum
        return total

    def fit_baseline(self, X_rest: np.ndarray, offset: float = 0.0,
                     locked: bool = False) -> TRCA:
        """Learn the rest scores' mean and spread; ``locked`` scores every rest window at
        its own seeded random phase, as the lock does on a screen that does not flicker.
        ``offset`` changes nothing (see raw_scores)."""
        if not locked:
            return super().fit_baseline(X_rest)
        X = self._windows(X_rest)
        psi = np.random.default_rng(CV_SEED).uniform(0.0, 2.0 * np.pi,
                                                     size=(X.shape[0], self.n_targets))
        rest = self.raw_scores(X, psi=psi)
        if rest.shape[0] < 2:
            raise ValueError(f"baseline needs at least 2 rest windows, got {rest.shape[0]}")
        spread = float(np.sqrt(np.mean(np.var(rest, axis=0, ddof=1))))
        if not np.isfinite(spread) or spread <= 0:
            raise ValueError(f"rest scores do not vary (spread {spread}); cannot scale the margin")
        self.baseline_mean = rest.mean(axis=0)
        self.baseline_spread = spread
        self.has_baseline = True
        return self

    def scores(self, X: np.ndarray, offset: float = 0.0, psi: object = None) -> np.ndarray:
        return self.raw_scores(X, offset, psi=psi) - self.baseline_mean

    def decide(self, X: np.ndarray, offset: float = 0.0,
               psi: object = None) -> tuple[np.ndarray, np.ndarray]:
        """Predictions and baseline-corrected scores, phase-locked when ``psi`` is given."""
        s = self.scores(X, offset, psi=psi)
        return self.predict_scores(s), s

    # -- the CV gate -----------------------------------------------------------------

    def _clone(self, fusion: str | tuple[float, float]) -> TRCA:
        return TRCA(self.freqs, self.fs, self.harmonics, self.phases, self.margin, self.bands,
                    self.a, self.b, self.threshold, self.n_comp, self.n_psi, self.ridge,
                    fusion=fusion)

    def _cv_weights(self, X: np.ndarray, y: np.ndarray) -> tuple[float, float]:
        """The FUSION_GRID pair with the best held-out window accuracy (then mean lead).

        Folds go by per-target rank in input order, so each fold holds out about one trial
        per target. Each held-out trial gives CV_OFFSETS windows of CV_WINDOW_S at seeded
        random offsets, scored by a model trained on the other folds.
        """
        K = self.n_targets
        rank = np.zeros(y.size, dtype=int)
        for k in range(K):
            rank[y == k] = np.arange(np.count_nonzero(y == k))
        n_folds = int(np.bincount(y, minlength=K).min())
        fold = rank % n_folds
        n = X.shape[-1]
        n_cv = int(round(min(CV_WINDOW_S, n / self.fs) * self.fs))
        rng = np.random.default_rng(CV_SEED)
        st, sc, yy = [], [], []
        for f in range(n_folds):
            te = fold == f
            inner = self._clone((1.0, 1.0))
            inner._fit_templates(X[~te], y[~te])
            off = rng.integers(0, n - n_cv + 1, size=(int(te.sum()), CV_OFFSETS)).ravel()
            idx = off[:, None] + np.arange(n_cv)[None]                  # (N, n_cv)
            Xw = np.repeat(X[te], CV_OFFSETS, axis=0)
            Xw = np.take_along_axis(Xw, np.repeat(idx[:, None, :], Xw.shape[1], axis=1), axis=2)
            t_part, c_part = inner._parts(self._windows(Xw))
            st.append(t_part)
            sc.append(c_part)
            yy.append(np.repeat(y[te], CV_OFFSETS))
        st, sc, yy = np.concatenate(st), np.concatenate(sc), np.concatenate(yy)
        rows = np.arange(yy.size)
        is_true = np.eye(K, dtype=bool)[yy]
        best, key = FUSION_GRID[-1], None
        for a_t, a_c in FUSION_GRID:
            s = a_t * st + a_c * sc
            acc = float(np.mean(s.argmax(axis=1) == yy))
            other = np.where(is_true, -np.inf, s).max(axis=1)
            lead = float(np.mean((s[rows, yy] - other) / (s.std() or 1.0)))
            self.cv_table[(a_t, a_c)] = acc
            if key is None or (acc, lead) > key:      # strict: ties keep the earlier pair
                best, key = (a_t, a_c), (acc, lead)
        return best

    # -- saving ----------------------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        """Write the trained model to ``path`` (.npz) atomically; the baseline is not saved
        (it is fitted every session on the still screen)."""
        path = Path(path)
        if path.suffix != ".npz":
            raise ValueError(f"model path must end in .npz, got {path}")
        if not self.fitted:
            raise ValueError("TRCA model is not trained: call fit() before save()")
        info = self.info
        if info is None:
            raise ValueError("TRCA model has no ModelInfo: pass info= to fit() before save()")
        pairs = list(self.cv_table)
        payload = {
            "format": np.int64(MODEL_FORMAT), "kind": np.array("trca"),
            "freqs": self.freqs, "phases": self.phases, "fs": np.float64(self.fs),
            "harmonics": np.int64(self.harmonics), "bands": np.array(self.bands, dtype=float),
            "a": np.float64(self.a), "b": np.float64(self.b), "n_comp": np.int64(self.n_comp),
            "n_psi": np.int64(self.n_psi), "ridge": np.float64(self.ridge),
            "W": self.W, "A": self.A, "weights": np.array(self.weights, dtype=float),
            "n_channels": np.int64(self.n_channels),
            "cv_pairs": np.array(pairs, dtype=float).reshape(-1, 2),
            "cv_acc": np.array([self.cv_table[p] for p in pairs], dtype=float),
            "cv_skipped": np.bool_(self.cv_skipped),
            "ch_names": np.array([str(c) for c in info.ch_names], dtype=str),
            "band": np.array(info.band if info.band is not None else (0.0, 0.0), dtype=float),
            "notch": np.float64(info.notch), "latency": np.float64(info.latency),
            "trial_s": np.float64(info.trial_s),
            "sources": np.array([str(s) for s in info.sources], dtype=str),
            "created": np.float64(info.created),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}_", suffix=".npz")
        try:
            with os.fdopen(fd, "wb") as fh:
                np.savez(fh, **payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path


# -- model files ---------------------------------------------------------------------

def load_model(path: str | Path, margin: float = 0.0, threshold: float | None = None) -> TRCA:
    """Load a model written by TRCA.save; the baseline still has to be fitted.

    Raises FileNotFoundError for a missing file, KeyError naming a missing key, and
    ValueError for anything else wrong with it: empty, truncated, unreadable, not an .npz
    archive, or not a format-1 TRCA model.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"model not found: {path}")

    def unreadable(exc: BaseException) -> ValueError:
        return ValueError(f"{path}: not a readable TRCA model file "
                          f"({type(exc).__name__}: {exc})")

    try:
        d = np.load(path, allow_pickle=False)
    except _UNREADABLE as exc:          # empty, truncated, permission denied, pickled
        raise unreadable(exc) from exc
    if not isinstance(d, np.lib.npyio.NpzFile):
        raise ValueError(f"{path}: not a TRCA model file (a single .npy array, not an "
                         f".npz archive)")
    with d:
        for key in _KEYS:
            if key not in d.files:
                raise KeyError(f"{path}: missing key {key!r} (has {d.files})")
        try:
            v = {key: d[key] for key in _KEYS}
        except _UNREADABLE as exc:      # a damaged member inside a valid zip
            raise unreadable(exc) from exc
    try:
        fmt, kind = int(v["format"]), str(v["kind"])
    except (TypeError, ValueError) as exc:
        raise unreadable(exc) from exc
    if fmt != MODEL_FORMAT or kind != "trca":
        raise ValueError(f"{path}: not a format-{MODEL_FORMAT} TRCA model "
                         f"(format {fmt}, kind {kind!r})")
    clf = TRCA(v["freqs"], float(v["fs"]), int(v["harmonics"]), v["phases"], margin,
               [tuple(float(e) for e in row) for row in v["bands"]], float(v["a"]),
               float(v["b"]), threshold, int(v["n_comp"]), int(v["n_psi"]), float(v["ridge"]),
               fusion=tuple(float(w) for w in v["weights"]))
    clf.W = np.asarray(v["W"], dtype=float)
    clf.A = np.asarray(v["A"], dtype=float)
    want_w = (len(clf.bands), int(v["n_channels"]), clf.n_targets * clf.n_comp)
    want_a = (len(clf.bands), clf.n_targets, int(v["n_channels"]), 2 * clf.harmonics)
    if clf.W.shape != want_w or clf.A.shape != want_a:
        raise ValueError(f"{path}: W{clf.W.shape} / A{clf.A.shape} do not match "
                         f"{want_w} / {want_a}")
    clf.n_channels = int(v["n_channels"])
    band = tuple(float(e) for e in v["band"])
    clf.info = ModelInfo(ch_names=tuple(str(c) for c in v["ch_names"]),
                         band=None if band == (0.0, 0.0) else band,
                         notch=float(v["notch"]), latency=float(v["latency"]),
                         trial_s=float(v["trial_s"]),
                         sources=tuple(str(s) for s in v["sources"]),
                         created=float(v["created"]))
    clf.cv_table = {(float(p[0]), float(p[1])): float(acc)
                    for p, acc in zip(v["cv_pairs"], v["cv_acc"])}
    clf.cv_skipped = bool(v["cv_skipped"])
    clf._prepare()
    clf.fitted = True
    return clf


def _model_key(p: Path) -> tuple[int, str, int]:
    """Sort key: the date_time stamp, then its "_N" repeat counter (1 if none). Names that
    default_model_path did not write (a hand-picked --out) sort before every stamped one."""
    m = _MODEL_RE.match(p.name)
    return (1, m.group("base"), int(m.group("n") or 1)) if m else (0, p.name, 1)


def latest_model(directory: str | Path | None = None) -> Path:
    """The newest ``*_trca.npz`` in ``directory`` (default models_dir()), by its name's
    date_time stamp and repeat counter."""
    root = Path(directory) if directory is not None else models_dir()
    found = sorted(root.glob("*_trca.npz"), key=_model_key) if root.is_dir() else []
    if not found:
        raise FileNotFoundError(f"no trained model in {root}; "
                                f"run python -m brainsync.train <cued.npz>")
    return found[-1]


def default_model_path(directory: str | Path | None = None) -> Path:
    """<models_dir>/<date>_<time>_trca.npz, with _2, _3, ... before _trca if it exists."""
    root = Path(directory) if directory is not None else models_dir()
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    path, n = root / f"{stamp}_trca.npz", 1
    while path.exists():
        n += 1
        path = root / f"{stamp}_{n}_trca.npz"
    return path


def check_compatible(clf: TRCA, freqs: Sequence[float], fs: float, ch_names: Sequence[str],
                     band: tuple[float, float] | None, notch: float, harmonics: int) -> None:
    """Raise ValueError naming the first way a loaded model does not fit this session."""
    want = np.asarray(freqs, dtype=float).reshape(-1)
    if want.shape != clf.freqs.shape or not np.allclose(want, clf.freqs, rtol=0, atol=1e-6):
        raise ValueError(f"model freqs {clf.freqs.tolist()} do not match the targets "
                         f"{want.tolist()} (same order needed)")
    if abs(float(fs) - clf.fs) > 1e-6:
        raise ValueError(f"model fs {clf.fs} Hz does not match the board's {float(fs)} Hz")
    info = clf.info
    if info is None:
        raise ValueError("model has no ModelInfo (channels, band, notch); retrain it")
    names = tuple(str(c) for c in ch_names)
    if names != info.ch_names:
        raise ValueError(f"model channels {list(info.ch_names)} do not match {list(names)}")
    band_t = None if band is None else (float(band[0]), float(band[1]))
    if band_t != info.band:
        raise ValueError(f"model band {info.band} does not match the decoder's band {band_t}")
    if float(notch) != info.notch:
        raise ValueError(f"model notch {info.notch} Hz does not match the decoder's "
                         f"{float(notch)} Hz")
    if int(harmonics) != clf.harmonics:
        raise ValueError(f"model harmonics {clf.harmonics} do not match --harmonics {harmonics}")
