from __future__ import annotations

import numpy as np
import pytest

from brainsync.recordings import MONTAGE, POSTERIOR, parse_markers
from brainsync.synthetic import SESSION_T0, fake_session, rest_trials, ssvep_trials

FREQS = [12.0, 15.0, 17.0]


def test_ssvep_trials_shape_and_labels():
    X, y = ssvep_trials(FREQS, fs=250.0, n_per_target=5, n_channels=4, seconds=2.0)
    assert X.shape == (15, 4, 500)
    np.testing.assert_array_equal(y, np.repeat([0, 1, 2], 5))
    assert y.dtype.kind == "i" and X.dtype == float


def test_ssvep_trials_deterministic_by_seed():
    a, ya = ssvep_trials(FREQS, seed=7)
    b, yb = ssvep_trials(FREQS, seed=7)
    c, _ = ssvep_trials(FREQS, seed=8)
    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(ya, yb)
    assert not np.allclose(a, c)


@pytest.mark.parametrize("snr_db", [-10.0, 0.0])
def test_ssvep_trials_snr(snr_db):
    X, y = ssvep_trials([15.0], n_per_target=20, snr_db=snr_db, seed=2)
    noise = rest_trials(20, seed=99)
    measured = 10 * np.log10(np.mean(X ** 2) / np.mean(noise ** 2) - 1.0)
    assert measured == pytest.approx(snr_db, abs=1.0)


def test_ssvep_trials_peak_at_target_frequency():
    X, y = ssvep_trials(FREQS, n_per_target=4, snr_db=-5.0, seed=1)
    freqs = np.fft.rfftfreq(X.shape[-1], 1 / 250.0)
    spec = np.abs(np.fft.rfft(X, axis=-1)).mean(axis=1)
    band = (freqs > 10) & (freqs < 19)
    peaks = freqs[band][np.argmax(spec[:, band], axis=1)]
    np.testing.assert_allclose(peaks, np.asarray(FREQS)[y], atol=0.3)


def test_ssvep_trials_harmonics():
    X, _ = ssvep_trials([15.0], n_per_target=10, snr_db=0.0, harmonics=2, seed=4)
    freqs = np.fft.rfftfreq(X.shape[-1], 1 / 250.0)
    spec = (np.abs(np.fft.rfft(X, axis=-1)) ** 2).mean(axis=(0, 1))
    floor = np.median(spec[(freqs > 5) & (freqs < 60)])
    assert spec[np.argmin(np.abs(freqs - 30.0))] > 20 * floor
    X1, _ = ssvep_trials([15.0], n_per_target=10, snr_db=0.0, harmonics=1, seed=4)
    spec1 = (np.abs(np.fft.rfft(X1, axis=-1)) ** 2).mean(axis=(0, 1))
    assert spec1[np.argmin(np.abs(freqs - 30.0))] < 5 * floor


@pytest.mark.parametrize("kw", [dict(freqs=[]), dict(freqs=[130.0]), dict(freqs=[15.0], n_per_target=0),
                                dict(freqs=[15.0], harmonics=0), dict(freqs=[15.0], seconds=0.0)])
def test_ssvep_trials_validates(kw):
    with pytest.raises(ValueError):
        ssvep_trials(**kw)


def test_rest_trials_shape_unit_variance_and_pink():
    X = rest_trials(10, fs=250.0, n_channels=3, seconds=4.0, seed=5)
    assert X.shape == (10, 3, 1000)
    assert np.mean(X ** 2) == pytest.approx(1.0, abs=0.05)
    np.testing.assert_array_equal(X, rest_trials(10, fs=250.0, n_channels=3, seconds=4.0, seed=5))
    freqs = np.fft.rfftfreq(1000, 1 / 250.0)
    spec = (np.abs(np.fft.rfft(X, axis=-1)) ** 2).mean(axis=(0, 1))
    low = spec[(freqs > 2) & (freqs < 6)].mean()
    high = spec[(freqs > 60) & (freqs < 100)].mean()
    assert low > 2 * high
    with pytest.raises(ValueError, match="0"):
        rest_trials(0)


def test_fake_session_layout():
    labels = [0, 1, 1, 0]
    X, t, text = fake_session([15.0, 17.0], labels, fs=250.0, cue_s=1.0, stim_s=4.0, rest_s=3.0)
    n = int((1.0 + 8.0 * 4 + 1.0) * 250)
    assert X.shape == (len(MONTAGE), n) and t.shape == (n,)
    assert t[0] == SESSION_T0
    np.testing.assert_allclose(np.diff(t), 1 / 250.0, atol=1e-6)
    assert np.all(X.mean(axis=1) < -10_000)  # raw DC offset, like the board
    m = parse_markers(text)
    ons = [(s, k) for s, kind, k in m if kind == "stim_on"]
    offs = [(s, k) for s, kind, k in m if kind == "stim_off"]
    assert [k for _, k in ons] == labels and [k for _, k in offs] == labels
    for i, ((s_on, _), (s_off, _)) in enumerate(zip(ons, offs)):
        assert s_on == pytest.approx(SESSION_T0 + 1.0 + 8.0 * i + 1.0, abs=1e-6)
        assert s_off - s_on == pytest.approx(4.0, abs=1e-6)
        assert np.min(np.abs(t - s_on)) < 1e-6  # markers land on samples
    assert "cue:0" in text and "session_start" in text


def test_fake_session_ssvep_only_on_posterior_during_stim():
    X, t, text = fake_session([15.0], [0, 0, 0], amp=4.0, noise=0.5, seed=2)
    from brainsync.signal import preprocess
    Y = preprocess(X, 250.0)
    m = parse_markers(text)
    on = int(np.searchsorted(t, m[0][0])) + 50
    off = int(np.searchsorted(t, m[1][0])) + 50
    post = [MONTAGE.index(c) for c in POSTERIOR]
    front = [MONTAGE.index(c) for c in ("Fp1", "Fp2", "C3", "C4")]
    stim = Y[:, on:on + 750]
    still = Y[:, off:off + 500]
    assert np.mean(stim[post] ** 2) > 5 * np.mean(still[post] ** 2)
    assert np.mean(stim[front] ** 2) < 2 * np.mean(still[front] ** 2)


def test_fake_session_deterministic_and_validates():
    a = fake_session([15.0, 17.0], [0, 1], seed=4)
    b = fake_session([15.0, 17.0], [0, 1], seed=4)
    np.testing.assert_array_equal(a[0], b[0])
    assert a[2] == b[2]
    with pytest.raises(ValueError, match="2"):
        fake_session([15.0, 17.0], [0, 2])
    with pytest.raises(ValueError):
        fake_session([15.0], [0], stim_s=0.0)
