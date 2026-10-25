"""CCA / FBCCA classifiers on synthetic data."""
from __future__ import annotations

import numpy as np
import pytest

from brainsync.classify import ABSTAIN, CACHE_SIZE, CCA, FBCCA, make, min_samples, references
from brainsync.signal import bandpass, default_bands, filterbank_weights, preprocess
from brainsync.synthetic import rest_trials, ssvep_trials

FS = 250.0
FREQS = [12.0, 15.0, 17.0]


def _first_corr_eig(x: np.ndarray, y: np.ndarray) -> float:
    """Independent first canonical correlation via the covariance eigenproblem."""
    x = x - x.mean(axis=1, keepdims=True)
    y = y - y.mean(axis=1, keepdims=True)
    cxx, cyy, cxy = x @ x.T, y @ y.T, x @ y.T
    m = np.linalg.solve(cxx, cxy) @ np.linalg.solve(cyy, cxy.T)
    return float(np.sqrt(np.max(np.real(np.linalg.eigvals(m)))))


def _alpha(rng: np.random.Generator, k: int, n: int, freq: float = 12.0) -> np.ndarray:
    t = np.arange(n) / FS
    return np.sin(2 * np.pi * freq * t + rng.uniform(0, 2 * np.pi, (k, 4, 1)))


@pytest.fixture(scope="module")
def task() -> tuple[np.ndarray, np.ndarray]:
    X, y = ssvep_trials(FREQS, FS, n_per_target=15, seconds=2.0, snr_db=-15.0, seed=1)
    return preprocess(X, FS), y


@pytest.fixture(scope="module")
def rest() -> tuple[np.ndarray, np.ndarray]:
    a = preprocess(rest_trials(120, FS, 4, 2.0, seed=2), FS)
    b = preprocess(rest_trials(120, FS, 4, 2.0, seed=3), FS)
    return a, b


def test_references_layout_and_phase() -> None:
    r = references(FREQS, FS, 500, harmonics=3)
    assert r.shape == (3, 6, 500)
    t = np.arange(500) / FS
    # rows 0..2 are the sines of harmonics 1..3, rows 3..5 the cosines
    np.testing.assert_allclose(r[1, 0], np.sin(2 * np.pi * 15.0 * t), atol=1e-9)
    np.testing.assert_allclose(r[1, 4], np.cos(2 * np.pi * 30.0 * t), atol=1e-9)
    np.testing.assert_allclose(r[2, 2], np.sin(2 * np.pi * 51.0 * t), atol=1e-9)
    shifted = references([10.0], FS, 100, harmonics=2, phases=[np.pi / 2])
    u = t[:100]
    np.testing.assert_allclose(shifted[0, 0], np.cos(2 * np.pi * 10.0 * u), atol=1e-9)
    np.testing.assert_allclose(shifted[0, 1], -np.sin(2 * np.pi * 20.0 * u), atol=1e-9)
    np.testing.assert_allclose(shifted[0, 3], -np.cos(2 * np.pi * 20.0 * u), atol=1e-9)


@pytest.mark.parametrize("bad", [dict(freqs=[]), dict(freqs=[0.0]), dict(freqs=[130.0]),
                                 dict(harmonics=0), dict(phases=[0.0, 1.0])])
def test_references_rejects_bad_input(bad: dict) -> None:
    kw = dict(freqs=[10.0], fs=FS, n_samples=100, harmonics=2, phases=None) | bad
    with pytest.raises(ValueError):
        references(**kw)


def test_cca_matches_eigen_solution(rng: np.random.Generator) -> None:
    x = rng.standard_normal((4, 400)) + 3.0
    x[0] += 0.5 * np.sin(2 * np.pi * 15.0 * np.arange(400) / FS)
    clf = CCA(FREQS, FS, harmonics=2)
    got = clf.raw_scores(x)
    assert got.shape == (1, 3)
    refs = references(FREQS, FS, 400, harmonics=2)
    want = [_first_corr_eig(x, r) for r in refs]
    np.testing.assert_allclose(got[0], want, rtol=1e-8)


def test_rank_deficient_channels_are_dropped(rng: np.random.Generator) -> None:
    x = rng.standard_normal((3, 300))
    dup = np.vstack([x, x[:1], np.zeros((1, 300)), 2 * x[1:2] - x[2:3]])
    clf = CCA(FREQS, FS)
    s = clf.raw_scores(dup)
    np.testing.assert_allclose(s, clf.raw_scores(x), atol=1e-10)
    assert np.all((s >= 0) & (s <= 1))
    assert np.all(clf.raw_scores(np.zeros((2, 300))) == 0)


@pytest.mark.parametrize("name", ["cca", "fbcca"])
def test_picks_the_right_target(name: str, task) -> None:
    X, y = task
    clf = make(name, freqs=FREQS, fs=FS)
    pred, scores = clf.decide(X)
    assert scores.shape == (len(y), 3)
    assert np.mean(pred == y) >= 0.95
    np.testing.assert_array_equal(pred, clf.predict(X))


def test_single_trial_input_gives_one_row(task) -> None:
    X, y = task
    clf = CCA(FREQS, FS)
    assert clf.raw_scores(X[0]).shape == (1, 3)
    assert clf.predict(X[0]).shape == (1,)


def test_baseline_removes_constant_bias(task, rest) -> None:
    class Biased(CCA):
        def raw_scores(self, X, offset=0.0):
            return super().raw_scores(X, offset) + np.array([0.5, 0.0, 0.0])

    X, y = task
    R, _ = rest
    clf = Biased(FREQS, FS)
    assert np.all(clf.predict(X) == 0)  # the bias wins everywhere before the baseline
    clf.fit_baseline(R)
    assert np.mean(clf.predict(X) == y) >= 0.95


@pytest.mark.parametrize("cls", [CCA, FBCCA])
def test_baseline_fixes_resting_rhythm_bias(cls) -> None:
    """A 12 Hz resting rhythm present at rest and during 15 Hz flicker."""
    rng = np.random.default_rng(5)
    n = 500
    R = rest_trials(40, FS, 4, 2.0, seed=1) + _alpha(rng, 40, n)
    X, _ = ssvep_trials([15.0], FS, n_per_target=40, seconds=2.0, snr_db=-12.0, seed=2)
    X = X + _alpha(rng, 40, n)
    clf = cls(FREQS, FS)
    assert np.mean(clf.predict(X) == 0) >= 0.8  # raw argmax: the resting 12 Hz template
    clf.fit_baseline(R)
    assert clf.has_baseline and clf.baseline_mean[0] > clf.baseline_mean[1]
    assert np.mean(clf.predict(X) == 1) >= 0.95


def test_fit_baseline_stats(rest) -> None:
    R, _ = rest
    clf = CCA(FREQS, FS).fit_baseline(R)
    raw = clf.raw_scores(R)
    np.testing.assert_allclose(clf.baseline_mean, raw.mean(axis=0))
    np.testing.assert_allclose(clf.scores(R).mean(axis=0), 0.0, atol=1e-12)
    clf.clear_baseline()
    assert clf.baseline_spread == 1.0 and not clf.has_baseline
    np.testing.assert_array_equal(clf.baseline_mean, np.zeros(3))


def test_baseline_spread_is_pooled_within_template(rest) -> None:
    """Spread = sqrt of the mean per-template rest variance; offsets between templates don't count."""
    class Offset(CCA):
        def raw_scores(self, X, offset=0.0):
            return super().raw_scores(X, offset) + np.array([0.6, 0.0, -0.3])

    R, _ = rest
    plain = CCA(FREQS, FS).fit_baseline(R)
    shifted = Offset(FREQS, FS).fit_baseline(R)
    raw = plain.raw_scores(R)
    n, k = raw.shape
    centred = raw - raw.mean(axis=0)
    want = np.sqrt(np.sum(centred ** 2) / (k * (n - 1)))
    assert plain.baseline_spread == pytest.approx(want, rel=1e-12)
    assert shifted.baseline_spread == pytest.approx(want, rel=1e-12)
    assert shifted.raw_scores(R).std() > 3 * want  # an all-scores std would be far larger


def test_fit_baseline_needs_two_windows(rest) -> None:
    with pytest.raises(ValueError, match="at least 2"):
        CCA(FREQS, FS).fit_baseline(rest[0][:1])


@pytest.mark.parametrize("cls", [CCA, FBCCA])
def test_margin_extremes(cls, task, rest) -> None:
    X, _ = task
    clf = cls(FREQS, FS, margin=0.0).fit_baseline(rest[0])
    assert not np.any(clf.predict(X) == ABSTAIN)
    assert not np.any(clf.predict(rest[1]) == ABSTAIN)
    clf.margin = 1e9
    assert np.all(clf.predict(X) == ABSTAIN)
    assert np.all(clf.predict(rest[1]) == ABSTAIN)


def test_margin_before_baseline_uses_unit_spread(task) -> None:
    X, _ = task
    clf = CCA(FREQS, FS, margin=0.05)
    pred, s = clf.decide(X)
    top = np.sort(s, axis=1)
    np.testing.assert_array_equal(pred == ABSTAIN, top[:, -1] - top[:, -2] < 0.05)


def test_margin_is_in_baseline_spread_units(rest) -> None:
    """The same margin gives comparable abstain rates although raw score scales differ."""
    fit, held = rest
    cca = CCA(FREQS, FS).fit_baseline(fit)
    fb = FBCCA(FREQS, FS).fit_baseline(fit)
    assert fb.baseline_spread / cca.baseline_spread > 1.3
    for m in (0.5, 1.0, 2.0):
        cca.margin = fb.margin = m
        r_cca = np.mean(cca.predict(held) == ABSTAIN)
        r_fb = np.mean(fb.predict(held) == ABSTAIN)
        assert 0.05 < r_cca < 1.0 and 0.05 < r_fb < 1.0
        assert abs(r_cca - r_fb) <= 0.2, (m, r_cca, r_fb)


def test_abstain_decisions_invariant_to_score_scale(task, rest) -> None:
    class Scaled(CCA):
        def raw_scores(self, X, offset=0.0):
            return 10.0 * super().raw_scores(X, offset)

    X, _ = task
    held = np.concatenate([X, rest[1]])
    a = CCA(FREQS, FS, margin=1.0).fit_baseline(rest[0])
    b = Scaled(FREQS, FS, margin=1.0).fit_baseline(rest[0])
    np.testing.assert_allclose(b.baseline_spread, 10 * a.baseline_spread)
    np.testing.assert_array_equal(a.predict(held), b.predict(held))


def test_single_target_abstains_at_rest_level(task, rest) -> None:
    clf = CCA([15.0], FS, margin=3.0).fit_baseline(rest[0])
    X, y = task
    assert np.mean(clf.predict(X[y == 1]) == 0) >= 0.8
    assert np.mean(clf.predict(rest[1]) == ABSTAIN) > 0.9


def test_fbcca_defaults_at_250_hz() -> None:
    clf = FBCCA(FREQS, FS)
    assert clf.bands == [(8.0, 45.0), (16.0, 45.0), (24.0, 45.0)] == default_bands(FS)
    np.testing.assert_allclose(clf.weights, filterbank_weights(3, 1.25, 0.25))
    assert clf.harmonics == 3 and clf.margin == 0.0


def test_fbcca_score_is_weighted_sum_of_squares(task) -> None:
    X, _ = task
    fb = FBCCA(FREQS, FS, bands=[(8.0, 45.0)], a=1.25, b=0.25)
    rho = CCA(FREQS, FS).raw_scores(bandpass(X[:3], FS, 8.0, 45.0))
    np.testing.assert_allclose(fb.raw_scores(X[:3]), 1.25 * rho ** 2)


def test_make_and_validation() -> None:
    assert isinstance(make("FBCCA", freqs=FREQS, fs=FS), FBCCA)
    assert type(make("cca", freqs=FREQS, fs=FS)) is CCA
    with pytest.raises(ValueError, match="nope"):
        make("nope", freqs=FREQS, fs=FS)
    with pytest.raises(ValueError, match="tdca"):
        make("tdca", freqs=FREQS, fs=FS)
    assert make("trca", freqs=FREQS, fs=FS).needs_fit and not make("cca", freqs=FREQS, fs=FS).needs_fit
    with pytest.raises(ValueError, match="-1"):
        CCA(FREQS, FS, margin=-1.0)
    with pytest.raises(ValueError, match="200"):
        CCA([200.0], FS)
    with pytest.raises(ValueError, match="shape"):
        CCA(FREQS, FS).raw_scores(np.zeros(10))
    with pytest.raises(ValueError):
        FBCCA(FREQS, FS, bands=[(20.0, 10.0)])


def test_fbcca_rejects_empty_band_list() -> None:
    with pytest.raises(ValueError, match=r"bands=\[\]"):
        FBCCA(FREQS, FS, bands=[])
    with pytest.raises(ValueError, match="125"):
        FBCCA(FREQS, FS, bands=[(130.0, 140.0)])
    assert FBCCA(FREQS, FS, bands=[(8.0, 200.0)]).bands == [(8.0, 200.0)]  # high-pass is fine


@pytest.mark.parametrize("cls", [CCA, FBCCA])
def test_short_windows_are_rejected(cls, rng: np.random.Generator) -> None:
    clf = cls(FREQS, FS, harmonics=3)
    need = min_samples(4, 3)
    assert need == 12
    with pytest.raises(ValueError, match=f"need >= {need}"):
        clf.raw_scores(rng.standard_normal((4, need - 1)))
    with pytest.raises(ValueError, match="too short"):
        clf.raw_scores(rng.standard_normal((2, 4, 10)))
    s = clf.raw_scores(rng.standard_normal((4, 3 * need)))
    assert np.ptp(s) > 0  # targets no longer tie at the degenerate maximum


@pytest.mark.parametrize("cls", [CCA, FBCCA])
def test_non_finite_input_is_named(cls, rng: np.random.Generator) -> None:
    X = rng.standard_normal((3, 4, 200))
    X[1, 2, 5] = np.nan
    X[2, 0, 0] = np.inf
    with pytest.raises(ValueError, match=r"2 NaN/inf values, first at index \(1, 2, 5\)"):
        cls(FREQS, FS).raw_scores(X)


def test_template_cache_is_bounded(rng: np.random.Generator) -> None:
    clf = CCA(FREQS, FS)
    first = rng.standard_normal((4, 100))
    want = clf.raw_scores(first)
    for n in range(100, 120):
        clf.raw_scores(rng.standard_normal((4, n)))
        assert len(clf._cache) <= CACHE_SIZE
    assert 100 not in clf._cache
    np.testing.assert_allclose(clf.raw_scores(first), want)  # rebuilt after eviction
    clf.raw_scores(rng.standard_normal((4, 119)))
    assert list(clf._cache)[-1] == 119


@pytest.mark.parametrize("cls", [CCA, FBCCA])
def test_threshold_abstains_below_rest_level(cls, task, rest) -> None:
    X, y = task
    clf = cls(FREQS, FS, margin=0.0, threshold=1.0).fit_baseline(rest[0])
    pred, s = clf.decide(np.concatenate([X, rest[1]]))
    level = s.max(axis=1) / clf.baseline_spread
    np.testing.assert_allclose(clf.levels(s), level)
    np.testing.assert_array_equal(pred == ABSTAIN, level < 1.0)
    # rest windows abstain far more often than flicker windows
    at_rest = np.mean(pred[len(y):] == ABSTAIN)
    on_task = np.mean(pred[:len(y)] == ABSTAIN)
    assert at_rest > 0.5 and on_task < at_rest - 0.2   # 3 iid scores: P(max < 1) ~ 0.59


def test_lead_alone_rarely_abstains_at_rest(rest) -> None:
    # the finding behind the threshold: two independent rest scores differ by ~1.4
    # spreads, so a 0.5-spread lead abstains on well under half of the rest windows
    two = [15.0, 17.0]
    lead = CCA(two, FS, margin=0.5).fit_baseline(rest[0])
    both = CCA(two, FS, margin=0.5, threshold=0.5).fit_baseline(rest[0])
    r_lead = np.mean(lead.predict(rest[1]) == ABSTAIN)
    r_both = np.mean(both.predict(rest[1]) == ABSTAIN)
    assert r_lead < 0.45 and r_both > r_lead + 0.2


def test_threshold_and_margin_combine(task, rest) -> None:
    X, _ = task
    m = CCA(FREQS, FS, margin=0.5).fit_baseline(rest[0])
    t = CCA(FREQS, FS, threshold=0.5).fit_baseline(rest[0])
    mt = CCA(FREQS, FS, margin=0.5, threshold=0.5).fit_baseline(rest[0])
    a, b, c = (clf.predict(X) == ABSTAIN for clf in (m, t, mt))
    np.testing.assert_array_equal(c, a | b)


def test_threshold_validation_and_repr() -> None:
    assert CCA(FREQS, FS).threshold is None
    assert "threshold=0.5" in repr(FBCCA(FREQS, FS, threshold=0.5))
    assert make("fbcca", freqs=FREQS, fs=FS, threshold=-1.0).threshold == -1.0
    with pytest.raises(ValueError, match="nan"):
        CCA(FREQS, FS, threshold=float("nan"))


# -- trained spatial filters (TRCA, eTRCA, -R variants, msCCA) ---------------------------

from brainsync.classify import (ETRCA, METHODS, MSCCA, NAMES, SPATIAL_FILTERS, TRAINED,  # noqa: E402
                                TRCA, Trained, is_trained, period_samples)

TRAINED_NAMES = list(SPATIAL_FILTERS)


@pytest.fixture(scope="module")
def split() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Synthetic 2 s trials split into a training and a held-out half (same per-channel phases)."""
    X, y = ssvep_trials(FREQS, FS, n_per_target=20, seconds=2.0, snr_db=-15.0, seed=5)
    X = preprocess(X, FS)
    train = np.arange(y.size) % 2 == 0
    return X[train], y[train], X[~train], y[~train]


def test_period_samples() -> None:
    assert period_samples([13.0, 15.0, 17.0, 11.0], 250.0) == 250      # 1 s
    assert period_samples([15.0, 17.0], 250.0) == 250
    assert period_samples([9.25, 9.75, 14.75], 256.0) == 1024          # 4 s
    assert period_samples([15.0], 250.0) == 50                          # one 15 Hz cycle is 3 frames
    assert period_samples([9.25], 256.0, max_s=2.0) is None


def test_make_trained_names_and_is_trained() -> None:
    assert METHODS == NAMES == ("cca", "fbcca") + TRAINED + SPATIAL_FILTERS
    assert make("trca-sf", freqs=FREQS, fs=FS).name == "trca-sf"
    assert make("TRCA-R-SF", freqs=FREQS, fs=FS).name == "trca-r-sf"
    assert make("etrca_r_sf", freqs=FREQS, fs=FS).name == "etrca-r-sf"
    assert isinstance(make("etrca-sf", freqs=FREQS, fs=FS), ETRCA)
    assert isinstance(make("mscca", freqs=FREQS, fs=FS), MSCCA)
    assert not isinstance(make("etrca", freqs=FREQS, fs=FS), Trained)    # brainsync.templates'
    assert all(is_trained(n) for n in SPATIAL_FILTERS + TRAINED)
    assert not is_trained("cca") and not is_trained("fbcca")
    assert all(make(n, freqs=FREQS, fs=FS).needs_fit for n in SPATIAL_FILTERS)
    with pytest.raises(ValueError, match="unknown classifier"):
        make("trca-x", freqs=FREQS, fs=FS)
    with pytest.raises(ValueError, match="shrink"):
        TRCA(FREQS, FS, shrink=1.0)
    assert "unfitted" in repr(TRCA(FREQS, FS))


def test_trained_fit_validation(split) -> None:
    Xtr, ytr, _, _ = split
    clf = TRCA(FREQS, FS)
    with pytest.raises(ValueError, match="not fitted"):
        clf.raw_scores(Xtr[0])
    with pytest.raises(ValueError, match="not fitted"):
        clf.subset([0])
    with pytest.raises(ValueError, match="labels for"):
        clf.fit(Xtr, ytr[:-1])
    with pytest.raises(ValueError, match="labels must be integers"):
        clf.fit(Xtr, ytr + 1)
    with pytest.raises(ValueError, match="at least 2 trials"):
        clf.fit(Xtr[[0, 10, 20]], ytr[[0, 10, 20]])        # one trial per target
    one_each = MSCCA(FREQS, FS).fit(Xtr[[0, 10, 20]], ytr[[0, 10, 20]])
    assert one_each.fitted and one_each.filters.shape == (4, 1)
    fitted = clf.fit(Xtr, ytr)
    assert fitted is clf and "fitted on 30 trials" in repr(clf) and "periodic" in repr(clf)
    with pytest.raises(ValueError, match="channels"):
        clf.raw_scores(Xtr[0, :3])
    with pytest.raises(ValueError, match="active targets"):
        clf.subset([3])
    with pytest.raises(ValueError, match="not one of the trained"):
        clf.for_freqs([15.0, 16.0])
    with pytest.raises(ValueError, match="repeat"):
        clf.for_freqs([15.0, 15.0])


def test_trca_matches_explicit_eigen_solution(split) -> None:
    """The TRCA filter solves S w = lambda Q w with S the sum over trial pairs j != k."""
    Xtr, ytr, _, _ = split
    clf = TRCA(FREQS, FS).fit(Xtr, ytr)
    Xi = Xtr[ytr == 1] - Xtr[ytr == 1].mean(axis=-1, keepdims=True)
    S = sum(Xi[j] @ Xi[k].T for j in range(len(Xi)) for k in range(len(Xi)) if j != k)
    Q = sum(x @ x.T for x in Xi)
    vals, vecs = np.linalg.eig(np.linalg.solve(Q, S))
    w = np.real(vecs[:, np.argmax(np.real(vals))])
    got = clf.filters[:, 1]
    cos = abs(w @ got) / (np.linalg.norm(w) * np.linalg.norm(got))
    assert cos > 1 - 1e-6
    assert got[np.argmax(np.abs(got))] > 0           # sign convention


@pytest.mark.parametrize("name", TRAINED_NAMES)
def test_trained_held_out_accuracy(name: str, split) -> None:
    Xtr, ytr, Xte, yte = split
    clf = make(name, freqs=FREQS, fs=FS).fit(Xtr, ytr)
    pred, scores = clf.decide(Xte)
    assert scores.shape == (yte.size, 3)
    assert np.all((scores >= -1) & (scores <= 1))
    assert np.mean(pred == yte) >= 0.95
    np.testing.assert_array_equal(pred, clf.predict(Xte))
    assert clf.raw_scores(Xte[0]).shape == (1, 3)


@pytest.mark.parametrize("name", ["trca-sf", "etrca-sf", "mscca"])
def test_subset_matches_full_scores(name: str, split) -> None:
    Xtr, ytr, Xte, _ = split
    clf = make(name, freqs=FREQS, fs=FS).fit(Xtr, ytr)
    full = clf.raw_scores(Xte)
    sub = clf.subset([0, 2])
    assert sub.n_targets == 2 and sub.freqs.tolist() == [12.0, 17.0]
    np.testing.assert_allclose(sub.raw_scores(Xte), full[:, [0, 2]], atol=1e-12)
    assert not sub.has_baseline
    if clf.ensemble:
        assert sub.filters.shape == clf.filters.shape       # the ensemble keeps every filter
    else:
        assert sub.filters.shape == (4, 2)
    rev = clf.for_freqs([17.0, 12.0])
    assert rev.freqs.tolist() == [17.0, 12.0]
    np.testing.assert_allclose(rev.raw_scores(Xte), full[:, [2, 0]], atol=1e-12)
    # the original is untouched
    assert clf.n_targets == 3 and clf.freqs.tolist() == FREQS


def test_periodic_templates_follow_the_window_offset() -> None:
    """A window cut later into the flicker scores like the aligned one when ``offset`` says so."""
    freqs = [15.0, 17.0]
    X, y = ssvep_trials(freqs, FS, n_per_target=6, seconds=3.0, snr_db=0.0, seed=7)
    X = preprocess(X, FS)
    n = 500                                                   # 2 s windows, period 250 samples
    clf = ETRCA(freqs, FS).fit(X[..., :n], y)
    assert clf.period == 250 and clf.cycle.shape == (2, 4, 250)
    aligned = clf.raw_scores(X[..., :n])
    shift = 137
    later = X[..., shift:shift + n]
    with_offset = clf.raw_scores(later, offset=shift / FS)             # seconds since the onset
    np.testing.assert_allclose(with_offset, aligned, atol=0.1)      # off-target cells are noise
    assert np.all(with_offset.argmax(axis=1) == y)
    without = clf.raw_scores(later)
    assert np.mean(without[np.arange(y.size), y]) < np.mean(with_offset[np.arange(y.size), y]) - 0.2
    # any window length works with periodic templates
    assert clf.raw_scores(X[..., :300]).shape == (y.size, 2)
    assert clf.templates_for(600, offset=1.0).shape == (2, 4, 600)
    np.testing.assert_allclose(clf.templates_for(300, offset=1.0), clf.templates_for(300, offset=0))
    # fitted with t0 (windows cut at onset + t0), offsets count from the onset
    late = ETRCA(freqs, FS).fit(X[..., :n], y, t0=0.14)
    np.testing.assert_allclose(late.raw_scores(later, offset=0.14 + shift / FS), with_offset)
    np.testing.assert_allclose(late.raw_scores(X[..., :n]), aligned)  # None: at t0
    assert late.covers(n, 5.0) and late.uses_offset and late.n_ch == 4


def test_raw_templates_need_the_training_length_and_no_offset() -> None:
    freqs = [9.25, 9.75]                                      # period 4 s: longer than the window
    X, y = ssvep_trials(freqs, 256.0, n_per_target=4, seconds=1.0, snr_db=0.0, seed=8)
    clf = TRCA(freqs, 256.0).fit(X, y)
    assert clf.cycle is None and clf.period == 1024
    assert clf.raw_scores(X).shape == (y.size, 2)
    with pytest.raises(ValueError, match="offset must be its t0"):
        clf.raw_scores(X, offset=3)
    assert clf.covers(X.shape[-1], 0.0) and not clf.covers(X.shape[-1], 3.0)
    assert not clf.covers(200, 0.0)
    with pytest.raises(ValueError, match="does not match the training window"):
        clf.raw_scores(X[..., :200])
    with pytest.raises(ValueError, match="at least one common period"):
        TRCA(freqs, 256.0, periodic=True).fit(X, y)
    raw = TRCA([15.0, 17.0], FS, periodic=False).fit(X[..., :256], y)
    assert raw.cycle is None
    assert "periodic" not in repr(raw)


def test_trained_baseline_and_abstain(split, rest) -> None:
    Xtr, ytr, Xte, yte = split
    R, _ = rest
    clf = ETRCA(FREQS, FS, margin=1e6).fit(Xtr, ytr)
    assert not clf.has_baseline
    clf.fit_baseline(R)
    assert clf.has_baseline and clf.baseline_mean.shape == (3,) and clf.baseline_spread > 0
    pred, s = clf.decide(Xte)
    assert np.all(pred == ABSTAIN)
    assert np.all(s.argmax(axis=1) == yte)
    clf.margin = 0.0
    assert np.mean(clf.predict(Xte) == yte) >= 0.95
    clf.fit(Xtr, ytr)                                          # refitting forgets the baseline
    assert not clf.has_baseline


def test_shrink_moves_the_filter_towards_the_plain_eigenvector(split) -> None:
    """At shrink -> 1 the within-trial covariance is replaced by the identity, so the filter
    becomes the principal eigenvector of the between-trial covariance alone."""
    Xtr, ytr, _, _ = split
    heavy = TRCA(FREQS, FS, shrink=0.999).fit(Xtr, ytr).filters[:, 0]
    Xi = Xtr[ytr == 0] - Xtr[ytr == 0].mean(axis=-1, keepdims=True)
    S = Xi.sum(axis=0) @ Xi.sum(axis=0).T - sum(x @ x.T for x in Xi)
    vals, vecs = np.linalg.eigh(S)
    principal = vecs[:, -1]
    cos = abs(principal @ heavy) / (np.linalg.norm(principal) * np.linalg.norm(heavy))
    assert cos > 0.99


@pytest.mark.realdata("nakanishi2015/s1.mat")
@pytest.mark.slow
def test_etrca_beats_cca_on_the_hardest_nakanishi_subject() -> None:
    """Wong et al. 2020 / Nakanishi 2018: calibration lifts subject 1 from under 30 % (CCA, 1 s)
    to over 90 %. Leave-one-block-out over the 15 blocks, 4 s periodic templates not used."""
    from brainsync import nakanishi as nk

    ds = nk.load(1)
    X = nk.epochs(ds, 1.0)
    cca = np.mean(make("cca", freqs=ds.freqs, fs=ds.fs, phases=ds.phases).predict(X) == ds.y)
    hits = 0
    for b in range(ds.n_blocks):
        te = ds.blocks == b
        clf = make("etrca-sf", freqs=ds.freqs, fs=ds.fs, phases=ds.phases).fit(X[~te], ds.y[~te])
        hits += int(np.sum(clf.predict(X[te]) == ds.y[te]))
    assert cca < 0.5
    assert hits / ds.y.size > 0.9


def test_update_adds_trials_and_refits(split) -> None:
    Xtr, ytr, Xte, yte = split
    clf = ETRCA(FREQS, FS).fit(Xtr, ytr)
    before = clf.n_train.copy()
    other = clf.templates[1].copy()
    new = Xte[yte == 0][:3]
    assert clf.update(new, [0, 0, 0]) is clf
    assert clf.n_train.tolist() == [before[0] + 3, before[1], before[2]]
    np.testing.assert_allclose(clf.templates[1], other)        # untouched target
    assert clf.train_X.shape[0] == ytr.size + 3 and not clf.has_baseline
    assert np.mean(clf.predict(Xte) == yte) >= 0.95
    clf.update(Xte[0], int(yte[0]))                            # one window, one label
    assert int(clf.n_train.sum()) == ytr.size + 4
    with pytest.raises(ValueError, match="like the training windows"):
        clf.update(Xte[:1, :, :-10], [0])
    with pytest.raises(ValueError, match="not fitted"):
        ETRCA(FREQS, FS).update(Xte[:1], [0])
