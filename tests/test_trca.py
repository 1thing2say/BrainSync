"""Phase-invariant filter-bank eTRCA (brainsync.trca) on synthetic data."""
from __future__ import annotations

from pathlib import Path

import os
import sys

import numpy as np
import pytest

from brainsync import nakanishi as nk
from brainsync import trca
from brainsync.classify import ABSTAIN, CACHE_SIZE, FBCCA, references
from brainsync.signal import filterbank, preprocess
from brainsync.synthetic import rest_trials, ssvep_trials
from brainsync.trca import FUSION_GRID, TRCA, ModelInfo, check_compatible, default_model_path, \
    latest_model, load_model

FS = 250.0
FREQS = [11.0, 15.0, 17.0, 13.0]
N_TRAIN = 8          # training trials per target
N_TEST = 8           # held-out trials per target
INFO = ModelInfo(ch_names=("P7", "P8", "O1", "O2"), band=(1.0, 100.0), notch=60.0,
                 latency=0.14, trial_s=4.0, sources=("a_cued.npz",), created=1_792_700_000.0)


def _trials(n_per_target: int, seconds: float = 4.0, snr_db: float = -10.0,
            seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    # one seed = one simulated head (per-channel gains and delays), whatever the length or SNR
    X, y = ssvep_trials(FREQS, FS, n_per_target=n_per_target, seconds=seconds, snr_db=snr_db,
                        seed=seed)
    return preprocess(X, FS), y


def _offset_windows(X: np.ndarray, y: np.ndarray, window_s: float, per_trial: int,
                    seed: int) -> tuple[np.ndarray, np.ndarray]:
    """``per_trial`` windows per trial at seeded random offsets (the live decoder's view)."""
    n = int(round(window_s * FS))
    off = np.random.default_rng(seed).integers(0, X.shape[-1] - n + 1, size=y.size * per_trial)
    Xr = np.repeat(X, per_trial, axis=0)
    idx = off[:, None] + np.arange(n)[None]
    return np.take_along_axis(Xr, np.repeat(idx[:, None], X.shape[1], axis=1), axis=2), \
        np.repeat(y, per_trial)


@pytest.fixture(scope="module")
def split() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Training and held-out trials from one simulated head, split by per-target rank."""
    X, y = _trials(N_TRAIN + N_TEST)
    rank = np.concatenate([np.arange(N_TRAIN + N_TEST)] * len(FREQS))
    tr = rank < N_TRAIN
    return X[tr], y[tr], X[~tr], y[~tr]


@pytest.fixture(scope="module")
def model(split) -> TRCA:
    Xtr, ytr, _, _ = split
    return TRCA(FREQS, FS).fit(Xtr, ytr, info=INFO)


@pytest.fixture(scope="module")
def rest() -> tuple[np.ndarray, np.ndarray]:
    a = preprocess(rest_trials(80, FS, 4, 1.5, seed=2), FS)
    b = preprocess(rest_trials(80, FS, 4, 1.5, seed=3), FS)
    return a, b


def _fresh(model: TRCA) -> TRCA:
    """A copy of the module-scoped model that a test may change (baseline, margin)."""
    clf = TRCA(FREQS, FS, fusion=model.weights)
    clf.W, clf.A, clf.n_channels, clf.info = model.W, model.A, model.n_channels, model.info
    clf._prepare()
    clf.fitted = True
    return clf


# -- accuracy and phase invariance ------------------------------------------------------

def test_offset_windows_accuracy(model, split) -> None:
    _, _, Xte, yte = split
    Xw, yw = _offset_windows(Xte, yte, 1.5, per_trial=5, seed=3)
    acc = np.mean(model.raw_scores(Xw).argmax(axis=1) == yw)
    fb = np.mean(FBCCA(FREQS, FS).raw_scores(Xw).argmax(axis=1) == yw)
    assert acc >= 0.9 and acc >= fb, (acc, fb)


def test_offset_windows_accuracy_at_low_snr() -> None:
    """At -10 dB both methods score 100 %; at -20 dB they separate (measured: TRCA 0.91,
    FBCCA 0.72 on this head), so a weaker fit shows here."""
    X, y = _trials(N_TRAIN + N_TEST, snr_db=-20.0)
    rank = np.concatenate([np.arange(N_TRAIN + N_TEST)] * len(FREQS))
    tr = rank < N_TRAIN
    clf = TRCA(FREQS, FS).fit(X[tr], y[tr])
    Xw, yw = _offset_windows(X[~tr], y[~tr], 1.5, per_trial=5, seed=3)
    acc = np.mean(clf.raw_scores(Xw).argmax(axis=1) == yw)
    fb = np.mean(FBCCA(FREQS, FS).raw_scores(Xw).argmax(axis=1) == yw)
    assert acc >= 0.85 and acc >= fb + 0.1, (acc, fb)


def test_phase_invariance_on_noiseless_trial(model) -> None:
    """Windows cut 0.37 s and 2.11 s into a flicker trial: no onset needed."""
    Xc, yc = _trials(1, snr_db=60.0)              # same head, practically noiseless
    n = int(round(1.5 * FS))
    early = model.raw_scores(Xc[..., 92:92 + n])  # 0.368 s
    late = model.raw_scores(Xc[..., 528:528 + n])  # 2.112 s
    k = np.arange(len(FREQS))
    np.testing.assert_array_equal(early.argmax(axis=1), yc)
    np.testing.assert_array_equal(late.argmax(axis=1), yc)
    # each window is band-passed on its own, as live, so the filter's edge transient differs
    # between the two cuts: measured 4.0 % on this head (11 Hz, whose band 3 holds almost no
    # signal) and up to 6 % on others. The exact check is the next test, filtered first.
    np.testing.assert_allclose(late[k, k], early[k, k], rtol=0.1)


@pytest.mark.parametrize("n_psi, rtol", [(trca.N_PSI, 0.02), (480, 0.005)])
def test_phase_invariance_is_exact_up_to_the_grid(split, n_psi: int, rtol: float) -> None:
    """Filtered before cutting (no edge transients), a time shift only rotates the phase:
    the per-band score changes by the phase grid's quantisation alone."""
    Xtr, ytr, _, _ = split
    clf = TRCA(FREQS, FS, n_psi=n_psi, fusion=(1.0, 0.0)).fit(Xtr, ytr)
    Xc, _ = _trials(1, snr_db=60.0)
    Xb = filterbank(Xc, FS, clf.bands)
    n = int(round(1.5 * FS))
    k = np.arange(len(FREQS))
    for m in range(len(clf.bands)):
        def part(s0: int) -> np.ndarray:
            w = Xb[m][..., s0:s0 + n]
            return clf._trca_part(w - w.mean(axis=-1, keepdims=True), m)[k, k]
        np.testing.assert_allclose(part(528), part(92), rtol=rtol)


def test_one_fit_scores_any_window_length(model, split) -> None:
    _, _, Xte, yte = split
    for window_s in (1.0, 1.5, 2.0):
        Xw, yw = _offset_windows(Xte, yte, window_s, per_trial=2, seed=4)
        s = model.raw_scores(Xw)
        assert s.shape == (yw.size, len(FREQS)) and np.isfinite(s).all()
        assert np.mean(s.argmax(axis=1) == yw) >= 0.8
    X8, y8 = _trials(2, seconds=8.0)               # longer than the 4 s training trials
    s8 = model.raw_scores(X8)
    np.testing.assert_array_equal(s8.argmax(axis=1), y8)
    assert int(round(8.0 * FS)) in model._gram


# -- the fitting and scoring core ------------------------------------------------------

def test_harmonic_coefficients_fit_the_trial_average(split) -> None:
    """A[m, k] is the least-squares fit of target k's centred, band-passed trial average
    onto the centred sin/cos references (not one trial's, not uncentred)."""
    Xtr, ytr, _, _ = split
    clf = TRCA(FREQS, FS, fusion=(1.0, 0.0)).fit(Xtr, ytr)
    n = Xtr.shape[-1]
    R = references(FREQS, FS, n, clf.harmonics)
    R = R - R.mean(axis=-1, keepdims=True)
    Xb = filterbank(Xtr, FS, clf.bands)
    for m in range(len(clf.bands)):
        for k in range(len(FREQS)):
            avg = Xb[m][ytr == k].mean(axis=0)
            avg = avg - avg.mean(axis=-1, keepdims=True)
            want = np.linalg.lstsq(R[k].T, avg.T, rcond=None)[0].T          # (ch, 2H)
            np.testing.assert_allclose(clf.A[m, k], want, rtol=1e-8, atol=1e-10)
            # the filters come from the same centred trials (A alone cannot tell: the
            # centred references are orthogonal to a constant)
            Xk = Xb[m][ytr == k]
            Wk = trca._trca_filters(Xk - Xk.mean(axis=-1, keepdims=True), clf.n_comp, clf.ridge)
            np.testing.assert_allclose(clf.W[m][:, k * clf.n_comp:(k + 1) * clf.n_comp], Wk,
                                       rtol=1e-10, atol=1e-12)


def test_trca_filters_solve_the_ridged_eigenproblem() -> None:
    """S w = lambda (Q + ridge tr(Q)/C I) w for the top n_comp eigenvalues, with a ridge
    large enough to matter."""
    rng = np.random.default_rng(0)
    Xk = rng.standard_normal((5, 4, 300))
    Xk = Xk - Xk.mean(axis=-1, keepdims=True)
    W = trca._trca_filters(Xk, 2, 0.5)
    s = Xk.sum(axis=0)
    Q = np.einsum("icn,idn->cd", Xk, Xk)
    S = s @ s.T - Q
    Qr = Q + 0.5 * np.trace(Q) / 4 * np.eye(4)
    lam = np.linalg.eigvals(np.linalg.solve(Qr, S)).real
    top = np.sort(lam)[::-1][:2]
    for j in range(2):
        w = W[:, j]
        np.testing.assert_allclose(S @ w, top[j] * (Qr @ w), rtol=1e-8, atol=1e-8)


def test_trca_part_is_the_best_correlation_over_shifted_templates(model, split) -> None:
    """The closed form against the definition: for each phase shift psi, the filtered
    template W^T A_k R_k(theta + psi) built from shifted references, correlated with the
    filtered window; the score is sign(r) r**2 of the best one."""
    _, _, Xte, _ = split
    n = 375
    Xb = filterbank(Xte[:3, :, 100:100 + n], FS, model.bands)
    psi = 2 * np.pi * np.arange(model.n_psi) / model.n_psi
    for m in range(len(model.bands)):
        xm = Xb[m] - Xb[m].mean(axis=-1, keepdims=True)
        got = model._trca_part(xm, m)
        for w in range(xm.shape[0]):
            y = model.W[m].T @ xm[w]
            for k in range(len(FREQS)):
                rs = []
                for g in psi:
                    R = references(FREQS, FS, n, model.harmonics, model.phases + g)[k]
                    T = model.W[m].T @ model.A[m, k] @ (R - R.mean(axis=-1, keepdims=True))
                    rs.append(np.sum(y * T) / (np.linalg.norm(y) * np.linalg.norm(T)))
                r = max(rs)
                np.testing.assert_allclose(got[w, k], np.sign(r) * r ** 2, rtol=1e-9)


def test_parts_centre_each_band_before_the_trca_part(model, split) -> None:
    _, _, Xte, _ = split
    Xw = Xte[:4, :, :375]
    want = np.zeros((4, len(FREQS)))
    for m, (bw, Xb) in enumerate(zip(model.band_weights, filterbank(Xw, FS, model.bands))):
        want += bw * model._trca_part(Xb - Xb.mean(axis=-1, keepdims=True), m)
    got, _ = model._parts(Xw, cca=False)
    np.testing.assert_allclose(got, want, rtol=1e-12)


# -- fusion weights ---------------------------------------------------------------------

def test_fbcca_only_fusion_equals_fbcca(split) -> None:
    Xtr, ytr, Xte, _ = split
    clf = TRCA(FREQS, FS, fusion=(0.0, 1.0)).fit(Xtr, ytr)
    assert clf.weights == (0.0, 1.0)
    want = FBCCA(FREQS, FS).raw_scores(Xte[..., :375])
    np.testing.assert_allclose(clf.raw_scores(Xte[..., :375]), want, rtol=0, atol=1e-12)


def test_trca_only_fusion_makes_no_cca_call(split, monkeypatch: pytest.MonkeyPatch) -> None:
    Xtr, ytr, Xte, _ = split
    clf = TRCA(FREQS, FS, fusion=(1.0, 0.0)).fit(Xtr, ytr)

    def boom(*_a, **_k):
        raise AssertionError("CCA was computed for a zero CCA weight")

    monkeypatch.setattr(clf, "_rho", boom)
    assert clf.raw_scores(Xte[..., :375]).shape == (Xte.shape[0], len(FREQS))


def test_fused_score_is_the_weighted_sum(split) -> None:
    Xtr, ytr, Xte, _ = split
    Xw = Xte[..., :375]
    only_t = TRCA(FREQS, FS, fusion=(1.0, 0.0)).fit(Xtr, ytr).raw_scores(Xw)
    only_c = FBCCA(FREQS, FS).raw_scores(Xw)
    both = TRCA(FREQS, FS, fusion=(1.0, 0.3)).fit(Xtr, ytr).raw_scores(Xw)
    np.testing.assert_allclose(both, only_t + 0.3 * only_c, rtol=1e-10)


@pytest.mark.parametrize("fusion", ["auto", (0.0, 0.0), (1.0, -1.0), (1.0, np.nan), (1.0,),
                                    (1.0, 2.0, 3.0), None])
def test_bad_fusion_is_rejected(fusion) -> None:
    with pytest.raises(ValueError, match="fusion"):
        TRCA(FREQS, FS, fusion=fusion)


@pytest.mark.parametrize("bad, match", [(dict(n_comp=0), "n_comp"), (dict(n_comp=1.5), "n_comp"),
                                        (dict(n_psi=3), "n_psi"), (dict(ridge=-1.0), "ridge"),
                                        (dict(ridge=np.inf), "ridge")])
def test_bad_parameters_are_named(bad: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        TRCA(FREQS, FS, **bad)


@pytest.mark.parametrize("n_ch, n_comp", [(2, 3), (1, 2)])
def test_more_filters_than_channels_is_rejected(split, n_ch: int, n_comp: int) -> None:
    """With 2 channels eigh gives 2 filters, not 3 (a broadcast error); with 1 channel the
    one filter would be copied into both columns."""
    Xtr, ytr, _, _ = split
    with pytest.raises(ValueError, match=f"n_comp {n_comp} exceeds the {n_ch} channel"):
        TRCA(FREQS, FS, n_comp=n_comp).fit(Xtr[:, :n_ch], ytr)
    clf = TRCA(FREQS, FS, n_comp=n_ch, fusion=(1.0, 0.0)).fit(Xtr[:, :n_ch], ytr)
    assert clf.W.shape == (len(clf.bands), n_ch, len(FREQS) * n_ch)


# -- fit validation ---------------------------------------------------------------------

def test_scoring_before_fit_raises(split) -> None:
    with pytest.raises(ValueError, match="not trained"):
        TRCA(FREQS, FS).raw_scores(split[2][:2])


def test_fit_validation(split) -> None:
    Xtr, ytr, Xte, _ = split
    clf = TRCA(FREQS, FS)
    bad_y = ytr.copy()
    bad_y[0] = 4
    with pytest.raises(ValueError, match=r"\[0, 4\).*\[4\]"):
        clf.fit(Xtr, bad_y)
    one = np.r_[np.flatnonzero(ytr != 2), np.flatnonzero(ytr == 2)[:1]]
    with pytest.raises(ValueError, match="17 Hz has 1 training trials"):
        clf.fit(Xtr[one], ytr[one])
    with pytest.raises(ValueError, match="one label per trial"):
        clf.fit(Xtr, ytr[:-1])
    with pytest.raises(ValueError, match="integers"):
        clf.fit(Xtr, ytr + 0.5)
    nan = Xtr.copy()
    nan[3, 1, 7] = np.nan
    with pytest.raises(ValueError, match=r"NaN/inf values, first at index \(3, 1, 7\)"):
        clf.fit(nan, ytr)
    with pytest.raises(ValueError, match="too short"):
        clf.fit(Xtr[..., :10], ytr)
    clf.fit(Xtr, ytr)
    with pytest.raises(ValueError, match="trained on 4 channels, got 3"):
        clf.raw_scores(Xte[:2, :3])


def test_fit_clears_the_baseline(split, rest) -> None:
    Xtr, ytr, _, _ = split
    clf = TRCA(FREQS, FS, fusion=(1.0, 1.0)).fit(Xtr, ytr).fit_baseline(rest[0])
    assert clf.has_baseline
    clf.fit(Xtr, ytr)
    assert not clf.has_baseline and clf.baseline_spread == 1.0
    np.testing.assert_array_equal(clf.baseline_mean, np.zeros(len(FREQS)))


# -- baseline and abstain rules ---------------------------------------------------------

def test_baseline_and_decision_shapes(model, split, rest) -> None:
    _, _, Xte, yte = split
    Xw = Xte[..., :375]
    clf = _fresh(model).fit_baseline(rest[0])
    s = clf.scores(Xw)
    np.testing.assert_allclose(s, clf.raw_scores(Xw) - clf.baseline_mean)
    np.testing.assert_allclose(clf.scores(rest[0]).mean(axis=0), 0.0, atol=1e-12)
    assert s.shape == (yte.size, len(FREQS))
    assert clf.gaps(s).shape == clf.levels(s).shape == clf.predict_scores(s).shape == (yte.size,)
    pred, s2 = clf.decide(Xw)
    np.testing.assert_allclose(s2, s)
    assert np.mean(pred == yte) >= 0.9


def test_margin_and_threshold_rules_in_spread_units(model, split, rest) -> None:
    _, _, Xte, _ = split
    clf = _fresh(model).fit_baseline(rest[0])
    clf.margin, clf.threshold = 1.0, 2.0
    held = np.concatenate([Xte[..., :375], rest[1]])
    pred, s = clf.decide(held)
    top = np.sort(s, axis=1)
    gap = (top[:, -1] - top[:, -2]) / clf.baseline_spread
    level = top[:, -1] / clf.baseline_spread
    np.testing.assert_allclose(clf.gaps(s), gap)
    np.testing.assert_allclose(clf.levels(s), level)
    np.testing.assert_array_equal(pred == ABSTAIN, (gap < 1.0) | (level < 2.0))
    n = Xte.shape[0]
    assert np.mean(pred[n:] == ABSTAIN) > np.mean(pred[:n] == ABSTAIN) + 0.3


def test_abstain_decisions_invariant_to_score_scale(model, split, rest) -> None:
    class Scaled(TRCA):
        def raw_scores(self, X):
            return 10.0 * super().raw_scores(X)

    _, _, Xte, _ = split
    held = np.concatenate([Xte[..., :375], rest[1]])
    a = _fresh(model)
    b = Scaled(FREQS, FS, fusion=model.weights)
    b.W, b.A, b.n_channels = model.W, model.A, model.n_channels
    b._prepare()
    b.fitted = True
    for clf in (a, b):
        clf.margin, clf.threshold = 1.0, 0.5
        clf.fit_baseline(rest[0])
    np.testing.assert_allclose(b.baseline_spread, 10 * a.baseline_spread)
    np.testing.assert_array_equal(a.predict(held), b.predict(held))


# -- the CV gate ------------------------------------------------------------------------

def test_cv_weights_are_a_grid_pair_and_deterministic(split, model) -> None:
    Xtr, ytr, _, _ = split
    again = TRCA(FREQS, FS).fit(Xtr, ytr)
    assert model.weights in FUSION_GRID and again.weights == model.weights
    assert not model.cv_skipped
    assert list(model.cv_table) == list(FUSION_GRID)
    assert all(0.0 <= acc <= 1.0 for acc in model.cv_table.values())
    assert model.cv_table[model.weights] == max(model.cv_table.values())


def test_cv_is_skipped_with_two_trials_per_target(split) -> None:
    Xtr, ytr, _, _ = split
    rank = np.concatenate([np.arange(N_TRAIN)] * len(FREQS))
    two = rank < 2
    clf = TRCA(FREQS, FS).fit(Xtr[two], ytr[two])
    assert clf.cv_skipped and clf.weights == (1.0, 1.0) and clf.cv_table == {}


def test_cv_on_pure_noise_still_returns_a_grid_pair() -> None:
    X = preprocess(rest_trials(4 * 5, FS, 4, 4.0, seed=9), FS)
    y = np.repeat(np.arange(4), 5)
    clf = TRCA(FREQS, FS).fit(X, y)
    assert clf.weights in FUSION_GRID and len(clf.cv_table) == len(FUSION_GRID)


def _locate(X: np.ndarray, w: np.ndarray) -> tuple[int, int]:
    """(trial, offset) of window w (n_ch, n_w) cut from trials X (n_trials, n_ch, n)."""
    n_w = w.shape[-1]
    for i in range(X.shape[0]):
        for o in np.flatnonzero(X[i, 0, :X.shape[-1] - n_w + 1] == w[0, 0]):
            if np.array_equal(X[i][:, o:o + n_w], w):
                return i, int(o)
    raise AssertionError("a CV window that is not cut from any training trial")


@pytest.mark.parametrize("n_short", [N_TRAIN, 3])
def test_cv_gate_holds_out_whole_folds_of_offset_windows(split, monkeypatch, n_short) -> None:
    """Each inner fit sees only the other folds (fold = per-target rank % n_folds), and
    scores CV_OFFSETS windows of CV_WINDOW_S per held-out trial at seeded offsets. With
    15 Hz cut to 3 trials, n_folds is 3 and the other targets' ranks wrap around."""
    Xtr, ytr, _, _ = split
    rank0 = np.array([np.count_nonzero(ytr[:i] == ytr[i]) for i in range(ytr.size)])
    keep = ~((ytr == 1) & (rank0 >= n_short))
    Xtr, ytr = Xtr[keep], ytr[keep]
    rows = {x.tobytes(): i for i, x in enumerate(Xtr)}
    fits: list[tuple[int, set[int]]] = []
    parts: list[tuple[int, np.ndarray]] = []
    real_fit, real_parts = TRCA._fit_templates, TRCA._parts

    def fit_templates(self, X, y):
        fits.append((id(self), {rows[x.tobytes()] for x in X}))
        return real_fit(self, X, y)

    def parts_(self, X, trca=True, cca=True):
        parts.append((id(self), np.array(X)))
        return real_parts(self, X, trca, cca)

    monkeypatch.setattr(TRCA, "_fit_templates", fit_templates)
    monkeypatch.setattr(TRCA, "_parts", parts_)
    outer = TRCA(FREQS, FS).fit(Xtr, ytr)
    n, n_folds = ytr.size, n_short
    assert fits[-1] == (id(outer), set(range(n)))       # the final fit uses every trial
    inner = fits[:-1]
    assert len(inner) == n_folds and len(parts) == n_folds
    rank = np.array([np.count_nonzero(ytr[:i] == ytr[i]) for i in range(n)])
    fold = rank % n_folds
    n_cv = int(round(trca.CV_WINDOW_S * FS))
    offsets = []
    for f, ((fit_id, trained), (part_id, W)) in enumerate(zip(inner, parts)):
        assert fit_id == part_id != id(outer)            # scored by the model fitted for it
        assert trained == set(np.flatnonzero(fold != f).tolist()), f
        assert W.shape[1:] == (Xtr.shape[1], n_cv)
        where = [_locate(Xtr, w) for w in W]
        held = [i for i, _ in where]
        assert set(held) == set(np.flatnonzero(fold == f).tolist())
        assert not set(held) & trained                   # never scored by its own fit
        assert all(held.count(i) == trca.CV_OFFSETS for i in set(held))
        offsets += [o for _, o in where]
    assert max(offsets) > 0 and len(set(offsets)) > n_folds   # random offsets, not the onset
    assert all(0 <= o <= Xtr.shape[-1] - n_cv for o in offsets)


def test_cv_ties_keep_the_earlier_grid_pair(split, monkeypatch) -> None:
    """All-zero parts tie every pair on accuracy and lead: the first pair, (1, 0), wins."""
    Xtr, ytr, _, _ = split

    def zeros(self, X, trca=True, cca=True):
        z = np.zeros((X.shape[0], self.n_targets))
        return z, z.copy()

    monkeypatch.setattr(TRCA, "_parts", zeros)
    clf = TRCA(FREQS, FS).fit(Xtr, ytr)
    assert len(set(clf.cv_table.values())) == 1
    assert clf.weights == FUSION_GRID[0]


def test_cv_fold_rule_tolerates_unequal_counts(split) -> None:
    Xtr, ytr, _, _ = split
    keep = np.flatnonzero(~((ytr == 1) & (np.arange(ytr.size) % N_TRAIN >= 3)))  # 15 Hz: 3
    clf = TRCA(FREQS, FS).fit(Xtr[keep], ytr[keep])
    assert clf.weights in FUSION_GRID and not clf.cv_skipped


# -- repr, cache ------------------------------------------------------------------------

def test_repr_names_the_model(model) -> None:
    text = repr(model)
    assert text.startswith("TRCA(freqs=[11.0, 15.0, 17.0, 13.0]")
    assert "bands=" in text and "n_comp=2" in text and f"weights={model.weights}" in text
    assert "model=['a_cued.npz']" in text
    assert "model=" not in repr(TRCA(FREQS, FS))


def test_reference_cache_is_bounded(model, split) -> None:
    clf = _fresh(model)
    X = split[2][:1]
    for n in range(300, 310):
        clf.raw_scores(X[..., :n])
        assert len(clf._gram) <= CACHE_SIZE
    assert list(clf._gram)[-1] == 309 and 300 not in clf._gram


# -- save / load ------------------------------------------------------------------------

def _rewrite(src: Path, dst: Path, **changes) -> Path:
    with np.load(src, allow_pickle=False) as d:
        payload = {k: d[k] for k in d.files}
    for k, v in changes.items():
        if v is None:
            payload.pop(k)
        else:
            payload[k] = v
    np.savez(dst, **payload)
    return dst


def test_save_load_round_trip(model, split, tmp_path: Path) -> None:
    path = model.save(tmp_path / "deep" / "m_trca.npz")
    assert path.is_file() and list(path.parent.iterdir()) == [path]   # no temp file left
    with np.load(path, allow_pickle=False) as d:
        for key in d.files:
            d[key]                                                     # nothing needs pickle
        assert int(d["format"]) == 1 and str(d["kind"]) == "trca"
    back = load_model(path, margin=0.5, threshold=0.5)
    Xw = split[2][..., :375]
    np.testing.assert_array_equal(back.raw_scores(Xw), model.raw_scores(Xw))
    assert back.weights == model.weights and back.info == model.info
    assert back.cv_table == model.cv_table and back.cv_skipped == model.cv_skipped
    assert back.margin == 0.5 and back.threshold == 0.5 and not back.has_baseline
    assert back.fitted and back.n_channels == 4 and back.bands == model.bands


def test_band_none_round_trips(split, tmp_path: Path) -> None:
    Xtr, ytr, _, _ = split
    info = ModelInfo(("O1", "O2", "P7", "P8"), None, 0.0, 0.14, 4.0, (), 0.0)
    clf = TRCA(FREQS, FS, fusion=(1.0, 0.0)).fit(Xtr, ytr, info=info)
    assert load_model(clf.save(tmp_path / "m.npz")).info == info


def test_load_rejects_bad_files(model, tmp_path: Path) -> None:
    good = model.save(tmp_path / "good.npz")
    with pytest.raises(ValueError, match="format 2"):
        load_model(_rewrite(good, tmp_path / "f2.npz", format=np.int64(2)))
    with pytest.raises(ValueError, match="'cca'"):
        load_model(_rewrite(good, tmp_path / "cca.npz", kind=np.array("cca")))
    with pytest.raises(KeyError, match="'W'"):
        load_model(_rewrite(good, tmp_path / "now.npz", W=None))
    with pytest.raises(FileNotFoundError, match="model not found"):
        load_model(tmp_path / "absent.npz")


def _unreadable_files(good: Path, root: Path) -> dict[str, Path]:
    data = good.read_bytes()
    files = {"empty": b"", "truncated": data[:2000], "text": b"not a model\n",
             "zip header only": data[:4] + b"\0" * 64}
    out = {}
    for label, payload in files.items():
        path = root / f"{label.replace(' ', '_')}_trca.npz"
        path.write_bytes(payload)
        out[label] = path
    npy = root / "array_trca.npz"
    with open(npy, "wb") as fh:                       # a .npy written under an .npz name
        np.save(fh, np.arange(3.0))
    out["npy"] = npy
    return out


def test_load_names_unreadable_files(model, tmp_path: Path) -> None:
    good = model.save(tmp_path / "good.npz")
    for label, path in _unreadable_files(good, tmp_path).items():
        with pytest.raises(ValueError, match=f"{path.name}: not a"):
            load_model(path)


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="file permissions do not stop this user")
def test_load_names_a_file_it_may_not_read(model, tmp_path: Path) -> None:
    path = model.save(tmp_path / "locked_trca.npz")
    path.chmod(0)
    try:
        with pytest.raises(ValueError, match="not a readable TRCA model file"):
            load_model(path)
    finally:
        path.chmod(0o644)


def test_save_refuses_untrained_or_unlabelled(split, tmp_path: Path) -> None:
    Xtr, ytr, _, _ = split
    with pytest.raises(ValueError, match="not trained"):
        TRCA(FREQS, FS).save(tmp_path / "m.npz")
    with pytest.raises(ValueError, match="ModelInfo"):
        TRCA(FREQS, FS, fusion=(1.0, 0.0)).fit(Xtr, ytr).save(tmp_path / "m.npz")
    with pytest.raises(ValueError, match="npz"):
        TRCA(FREQS, FS, fusion=(1.0, 0.0)).fit(Xtr, ytr, info=INFO).save(tmp_path / "m.bin")
    assert not any(tmp_path.iterdir())


def test_latest_and_default_model_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(FileNotFoundError, match="python -m brainsync.train"):
        latest_model(tmp_path)
    for name in ("2026-10-23_090000_trca.npz", "2026-10-24_120000_trca.npz",
                 "2026-10-24_120000_2_trca.npz", "2026-10-22_235959_trca.npz", "notes.npz"):
        (tmp_path / name).write_bytes(b"")
    assert latest_model(tmp_path).name == "2026-10-24_120000_2_trca.npz"
    monkeypatch.setenv("BRAINSYNC_DATA_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="models"):
        latest_model()
    monkeypatch.setattr(trca.time, "strftime", lambda fmt: "2026-10-24_130000")
    first = default_model_path()
    assert first == tmp_path / "models" / "2026-10-24_130000_trca.npz"
    first.parent.mkdir()
    first.write_bytes(b"")
    second = default_model_path()
    assert second.name == "2026-10-24_130000_2_trca.npz"
    second.write_bytes(b"")
    assert default_model_path().name == "2026-10-24_130000_3_trca.npz"
    assert latest_model() == second


def test_latest_model_sorts_by_stamp_then_repeat(tmp_path: Path) -> None:
    """A same-second repeat (_2) of an older stamp must not outrank a newer plain stamp
    from the same day; a name default_model_path did not write never outranks a stamp."""
    for name in ("2026-10-24_120000_trca.npz", "2026-10-24_120000_2_trca.npz",
                 "2026-10-24_150000_trca.npz", "2026-10-24_090000_10_trca.npz",
                 "zz_mine_trca.npz"):
        (tmp_path / name).write_bytes(b"")
    assert latest_model(tmp_path).name == "2026-10-24_150000_trca.npz"
    order = sorted(tmp_path.glob("*_trca.npz"), key=trca._model_key)
    assert [p.name for p in order] == ["zz_mine_trca.npz", "2026-10-24_090000_10_trca.npz",
                                       "2026-10-24_120000_trca.npz",
                                       "2026-10-24_120000_2_trca.npz",
                                       "2026-10-24_150000_trca.npz"]
    (tmp_path / "2026-10-24_150000_2_trca.npz").write_bytes(b"")
    assert latest_model(tmp_path).name == "2026-10-24_150000_2_trca.npz"


# -- compatibility with a live session --------------------------------------------------

def test_check_compatible_names_each_mismatch(model) -> None:
    ok = dict(freqs=FREQS, fs=FS, ch_names=INFO.ch_names, band=(1.0, 100.0), notch=60.0,
              harmonics=3)
    check_compatible(model, **ok)
    cases = [(dict(freqs=[15.0, 11.0, 17.0, 13.0]), "freqs"),
             (dict(freqs=[15.0, 17.0]), "freqs"),
             (dict(fs=256.0), "fs"),
             (dict(ch_names=("O1", "O2", "P7", "P8")), "channels"),
             (dict(band=(3.0, 40.0)), "band"),
             (dict(band=None), "band"),
             (dict(notch=50.0), "notch"),
             (dict(harmonics=2), "harmonics")]
    for change, word in cases:
        with pytest.raises(ValueError, match=word):
            check_compatible(model, **(ok | change))


# -- real data --------------------------------------------------------------------------

@pytest.mark.realdata("nakanishi2015/s1.mat")
@pytest.mark.slow
def test_nakanishi_subject1_beats_fbcca() -> None:
    """4 targets (9.25/11.25/13.25/14.75 Hz), 6 training blocks, 1.5 s offset windows."""
    ds = nk.load(1)
    X = nk.epochs(ds, 4.0)
    targets = [int(np.argmin(np.abs(ds.freqs - f))) for f in (9.25, 11.25, 13.25, 14.75)]
    keep = np.isin(ds.y, targets)
    y = np.searchsorted(targets, ds.y[keep])
    X, blocks = X[keep], ds.blocks[keep]
    tr = blocks < 6
    clf = TRCA(ds.freqs[targets], ds.fs, phases=ds.phases[targets]).fit(X[tr], y[tr])
    fb = FBCCA(ds.freqs[targets], ds.fs, phases=ds.phases[targets])
    n = int(round(1.5 * ds.fs))
    off = np.random.default_rng(5).integers(0, X.shape[-1] - n + 1, size=int((~tr).sum()))
    idx = off[:, None] + np.arange(n)[None]
    Xw = np.take_along_axis(X[~tr], np.repeat(idx[:, None], X.shape[1], axis=1), axis=2)
    acc = np.mean(clf.raw_scores(Xw).argmax(axis=1) == y[~tr])
    acc_fb = np.mean(fb.raw_scores(Xw).argmax(axis=1) == y[~tr])
    assert acc >= acc_fb + 0.05, (acc, acc_fb)
