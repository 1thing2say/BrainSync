"""Trained classifiers (templates, spatial filters, offsets) on synthetic phase-locked SSVEP."""
from __future__ import annotations

import numpy as np
import pytest

from brainsync.classify import ABSTAIN, CCA, NAMES, TRAINED, make, references
from brainsync.metrics import accuracy
from brainsync.signal import default_bands, preprocess
from brainsync.synthetic import rest_trials, ssvep_trials
from brainsync.templates import (CLASSES, ECCA, ETRCA, ETRCAR, ITCCA, SFCCA, TRCA, TemplateClassifier,
                                 cca_weights, cross_val_predict, load_model, save_model)

FS = 250.0
FREQS = [13.0, 15.0, 17.0, 11.0]
N_PER = 10
T0 = 0.14


@pytest.fixture(scope="module")
def task() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Phase-locked trials (40, 4, 500), labels and blocks (trial j of each target -> block j)."""
    X, y = ssvep_trials(FREQS, FS, n_per_target=N_PER, seconds=2.0, snr_db=-18.0, seed=5)
    blocks = np.tile(np.arange(N_PER), len(FREQS))
    return preprocess(X, FS), y, blocks


@pytest.fixture(scope="module")
def rich() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cleaner and longer: 16 trials per target at -15 dB, for the raw (unprojected) templates."""
    X, y = ssvep_trials(FREQS, FS, n_per_target=16, seconds=2.0, snr_db=-15.0, seed=5)
    return preprocess(X, FS), y, np.tile(np.arange(16), len(FREQS))


@pytest.fixture(scope="module")
def rest() -> np.ndarray:
    return preprocess(rest_trials(40, FS, 4, 2.0, seed=6), FS)


def _split(task, train_blocks: int = 5):
    X, y, blocks = task
    train = blocks < train_blocks
    return (X[train], y[train]), (X[~train], y[~train])


# -- references with a time origin ----------------------------------------------------------

def test_references_t0_shifts_phase() -> None:
    r0 = references([10.0], FS, 50, 2, t0=0.0)[0]
    r1 = references([10.0], FS, 50, 2, t0=0.1)[0]       # one full cycle: identical
    r2 = references([10.0], FS, 50, 2, t0=0.025)[0]     # a quarter cycle: sin -> cos
    np.testing.assert_allclose(r0, r1, atol=1e-9)
    np.testing.assert_allclose(r2[0], r0[2], atol=1e-9)
    with pytest.raises(ValueError, match="t0"):
        references([10.0], FS, 50, 2, t0=np.inf)


def test_cca_ignores_offset(task) -> None:
    X, _, _ = task
    clf = CCA(FREQS, FS)
    np.testing.assert_allclose(clf.raw_scores(X[:5], 0.0), clf.raw_scores(X[:5], 0.37))
    assert not clf.uses_offset and not clf.needs_fit


def test_cca_weights_recover_correlation() -> None:
    rng = np.random.default_rng(0)
    Y = references([12.0], FS, 400, 2)[0]
    X = rng.standard_normal((3, 400))
    X[1] += 2.0 * Y[0]
    wx, wy, rho = cca_weights(X, Y)
    a, b = wx @ (X - X.mean(1, keepdims=True)), wy @ (Y - Y.mean(1, keepdims=True))
    assert rho == pytest.approx(np.corrcoef(a, b)[0, 1], abs=1e-9)
    assert rho > 0.8


# -- fitting and scoring ------------------------------------------------------------------

@pytest.mark.parametrize("name", TRAINED)
def test_trained_methods_decode_held_out_trials(task, name: str) -> None:
    (Xa, ya), (Xb, yb) = _split(task)
    clf = make(name, freqs=FREQS, fs=FS).fit(Xa, ya, T0)
    acc = accuracy(yb, clf.predict(Xb, T0))
    assert acc >= 0.85, f"{name}: {acc:.2f}"      # single-filter TRCA is the weakest at 0.90


def test_sfcca_is_phase_free(task) -> None:
    (Xa, ya), (Xb, yb) = _split(task)
    clf = make("sfcca", freqs=FREQS, fs=FS).fit(Xa, ya, T0)
    assert not clf.uses_offset
    np.testing.assert_allclose(clf.raw_scores(Xb[:4], T0), clf.raw_scores(Xb[:4], T0 + 0.0137))
    assert accuracy(yb, clf.predict(Xb)) >= 0.85


def test_registry_and_flags() -> None:
    assert set(NAMES) == {"cca", "fbcca", *TRAINED}
    for cls in CLASSES:
        assert cls.name in TRAINED and cls.needs_fit
        assert cls.uses_offset == (cls is not SFCCA)
    assert TRCA.min_trials == 2 and ITCCA.min_trials == 1 and SFCCA.min_trials == 1
    assert ETRCA.ensemble and not TRCA.ensemble and ETRCAR.regularised


def test_unfitted_raises_and_fit_validates(task) -> None:
    X, y, _ = task
    clf = ECCA(FREQS, FS)
    with pytest.raises(ValueError, match="fit"):
        clf.raw_scores(X[:2])
    with pytest.raises(ValueError, match="trials must be"):
        clf.fit(X[0], y[:1])
    with pytest.raises(ValueError, match="labels"):
        clf.fit(X[:3], y[:2])
    with pytest.raises(ValueError, match="index"):
        clf.fit(X[:3], [0, 1, 9])
    with pytest.raises(ValueError, match="at least 2"):
        TRCA(FREQS, FS).fit(X[::N_PER], y[::N_PER])     # one trial per target
    with pytest.raises(ValueError, match="at least 1"):
        ECCA(FREQS, FS).fit(X[:N_PER], y[:N_PER])        # only target 0
    with pytest.raises(ValueError, match="NaN"):
        ECCA(FREQS, FS).fit(np.full_like(X, np.nan), y)
    with pytest.raises(ValueError, match="template"):
        ECCA(FREQS, FS, template="spline")
    with pytest.raises(ValueError, match="bands"):
        ECCA(FREQS, FS, bands=[])


def test_itcca_with_one_trial_per_target_and_no_trca_filters(task) -> None:
    X, y, _ = task
    clf = ITCCA(FREQS, FS).fit(X[::N_PER], y[::N_PER], T0)
    assert clf.trca is None and clf.trcar is None and clf.n_train == 4
    assert clf.predict(X[:2], T0).shape == (2,)


def test_channel_count_must_match_fit(task) -> None:
    (Xa, ya), (Xb, _) = _split(task)
    clf = ETRCA(FREQS, FS).fit(Xa, ya, T0)
    with pytest.raises(ValueError, match="4 channels"):
        clf.raw_scores(Xb[:2, :3])


def test_harmonic_templates_follow_the_offset(task) -> None:
    """A window later in the trial scores right only when its offset is passed."""
    (Xa, ya), (Xb, yb) = _split(task)
    clf = ETRCA(FREQS, FS).fit(Xa, ya, T0)
    late = Xb[:, :, 250:]                          # starts 1 s into the window
    aligned = accuracy(yb, clf.predict(late, T0 + 1.0))
    wrong = accuracy(yb, clf.predict(late, T0 + 1.0 + 1 / 60))   # a sixtieth of a second off
    assert aligned >= 0.7
    assert aligned > wrong + 0.1


def test_raw_templates_only_inside_the_epoch(rich) -> None:
    (Xa, ya), (Xb, yb) = _split(rich, 8)
    clf = ETRCA(FREQS, FS, template="raw").fit(Xa, ya, T0)
    assert accuracy(yb, clf.predict(Xb, T0)) >= 0.85
    assert clf.templates(100, T0 + 1.0).shape == (4, 4, 100)
    with pytest.raises(ValueError, match="does not fit"):
        clf.templates(500, T0 + 1.0)


def test_filter_bank_scores_combine_bands(task) -> None:
    (Xa, ya), (Xb, yb) = _split(task)
    bands = default_bands(FS)
    fb = ETRCAR(FREQS, FS, bands=bands).fit(Xa, ya, T0)
    assert fb.n_bands == 3 and fb.coef.shape == (3, 4, 4, 6)
    parts = [ETRCAR(FREQS, FS, bands=[b]).fit(Xa, ya, T0) for b in bands]
    want = sum(w * p.raw_scores(Xb[:3], T0) / p.weights[0] for w, p in zip(fb.weights, parts))
    np.testing.assert_allclose(fb.raw_scores(Xb[:3], T0), want, rtol=1e-6, atol=1e-9)
    assert accuracy(yb, fb.predict(Xb, T0)) >= 0.7


def test_baseline_margin_and_threshold_work_as_for_cca(task, rest) -> None:
    (Xa, ya), (Xb, yb) = _split(task)
    clf = ETRCA(FREQS, FS, margin=0.5, threshold=0.5).fit(Xa, ya, T0).fit_baseline(rest[:20])
    assert clf.has_baseline and clf.baseline_spread > 0
    pred = clf.predict(Xb, T0)
    go = pred != ABSTAIN
    assert go.mean() > 0.5
    assert np.mean(pred[go] == yb[go]) >= 0.7
    assert np.mean(clf.predict(rest[20:], 0.0) == ABSTAIN) > 0.5


# -- cross-validation ---------------------------------------------------------------------

def test_cross_val_predict_scores_every_trial_out_of_fold(task, rest) -> None:
    X, y, blocks = task
    calls: list[int] = []

    def factory():
        calls.append(1)
        return ETRCAR(FREQS, FS)

    cv = cross_val_predict(factory, X, y, blocks, T0, rest=rest)
    assert len(calls) == N_PER == cv.n_folds
    assert cv.pred.shape == (40,) and cv.scores.shape == (40, 4)
    assert cv.rest_scores is not None and cv.rest_scores.shape == (40, 4)
    assert cv.baseline_means.shape == (N_PER, 4) and np.all(cv.baseline_spreads > 0)
    assert accuracy(y, cv.pred) >= 0.7
    plain = cross_val_predict(lambda: CCA(FREQS, FS), X, y, blocks, T0)
    assert plain.rest_scores is None and accuracy(y, plain.pred) > 0.4


def test_cross_val_predict_validation(task) -> None:
    X, y, blocks = task
    with pytest.raises(ValueError, match="at least 2 blocks"):
        cross_val_predict(lambda: ECCA(FREQS, FS), X, y, np.zeros_like(blocks), T0)
    with pytest.raises(ValueError, match="no training trial"):
        cross_val_predict(lambda: ECCA(FREQS, FS), X[:N_PER + 1], y[:N_PER + 1],
                          np.r_[np.zeros(N_PER, int), 1], T0)
    with pytest.raises(ValueError, match="one label"):
        cross_val_predict(lambda: ECCA(FREQS, FS), X, y[:-1], blocks, T0)


# -- saving and loading ---------------------------------------------------------------------

def test_save_load_roundtrip(task, rest, tmp_path) -> None:
    (Xa, ya), (Xb, _) = _split(task)
    clf = ETRCAR(FREQS, FS, margin=0.3, bands=default_bands(FS)).fit(Xa, ya, T0)
    clf.fit_baseline(rest[:10])
    path = save_model(clf, tmp_path / "m.npz", recording="x_cued.npz", note=3)
    back = load_model(path)
    assert isinstance(back, ETRCAR) and back.bands == clf.bands and back.margin == 0.3
    assert back.has_baseline and back.t0 == T0 and back.n_ch == 4 and back.n_train == 20
    assert back.meta == {"recording": "x_cued.npz", "note": 3}
    np.testing.assert_allclose(back.raw_scores(Xb[:3], T0), clf.raw_scores(Xb[:3], T0))
    np.testing.assert_array_equal(back.predict(Xb, T0), clf.predict(Xb, T0))
    other = load_model(path, name="ecca")       # same templates, another scorer
    assert isinstance(other, ECCA) and other.fitted
    with pytest.raises(ValueError, match="trained"):
        save_model(CCA(FREQS, FS), tmp_path / "c.npz")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fit"):
        save_model(ECCA(FREQS, FS), tmp_path / "u.npz")
    with pytest.raises(FileNotFoundError):
        load_model(tmp_path / "missing.npz")
    with pytest.raises(ValueError, match="npz"):
        save_model(clf, tmp_path / "m.txt")


def test_template_classifier_is_a_cca(task) -> None:
    (Xa, ya), _ = _split(task)
    clf = make("etrca", freqs=FREQS, fs=FS).fit(Xa, ya, T0)
    assert isinstance(clf, TemplateClassifier) and isinstance(clf, CCA)
    assert "fitted on 20 trials" in repr(clf)


def test_cca_weights_are_rank_aware() -> None:
    """A template inside the sine/cosine span (rank 2H < n_ch) must not score above the span."""
    from brainsync.templates import _first_cc, _span
    rng = np.random.default_rng(3)
    refs = references([12.0], FS, 256, 3)[0]                       # (6, 256)
    T = rng.standard_normal((8, 6)) @ refs                          # 8 channels, rank 6
    x = rng.standard_normal((8, 256))
    _, _, rho_t = cca_weights(x, T)
    _, _, rho_r = cca_weights(x, refs)
    exact = float(_first_cc(_span(x.T), _span(refs.T)))
    assert rho_t <= rho_r + 1e-9 and rho_r == pytest.approx(exact, abs=1e-9)
    flat = x.copy()
    flat[2] = 0.0                                                   # a dead channel
    wx, _, rho_f = cca_weights(flat, refs)
    assert np.isfinite(wx).all() and abs(wx[2]) < 1e-12 and 0.0 <= rho_f <= 1.0


def test_trca_filters_have_unit_output_variance(task) -> None:
    from brainsync.templates import _filters
    X, y, _ = task
    trials = X[y == 1] - X[y == 1].mean(axis=-1, keepdims=True)
    w = _filters(trials, None)
    Q = np.einsum("tcn,tdn->cd", trials, trials)
    assert w @ Q @ w == pytest.approx(1.0, rel=1e-4)


def test_flat_training_data_is_refused(task) -> None:
    X, y, _ = task
    dead = X.copy()
    dead[y == 2] = 0.0
    with pytest.raises(ValueError, match="target 2"):
        ETRCA(FREQS, FS).fit(dead, y, T0)


def test_covers_and_load_model_shape_checks(task, tmp_path) -> None:
    (Xa, ya), _ = _split(task)
    raw = ETRCA(FREQS, FS, template="raw").fit(Xa, ya, T0)
    assert raw.covers(100, T0 + 1.0) and not raw.covers(500, T0 + 1.0) and not raw.covers(10, T0 - 0.1)
    assert ETRCA(FREQS, FS).fit(Xa, ya, T0).covers(5000, 99.0)
    single = ITCCA(FREQS, FS).fit(Xa[::5], ya[::5], T0)             # one trial per target
    path = save_model(single, tmp_path / "s.npz")
    with pytest.raises(ValueError, match="TRCA filters"):
        load_model(path, name="etrca")
    d = dict(np.load(path, allow_pickle=False))
    d["w_tmpl"] = d["w_tmpl"][:, :2]
    np.savez(tmp_path / "bad.npz", **d)
    with pytest.raises(ValueError, match="w_tmpl shape"):
        load_model(tmp_path / "bad.npz")
