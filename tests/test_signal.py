from __future__ import annotations

import numpy as np
import pytest

from brainsync.signal import (bandpass, default_bands, filterbank, filterbank_weights, notch,
                              preprocess)

FS = 250.0


def sine(freq: float, seconds: float = 8.0, fs: float = FS, phase: float = 0.0) -> np.ndarray:
    t = np.arange(int(seconds * fs)) / fs
    return np.sin(2 * np.pi * freq * t + phase)


def gain(y: np.ndarray, x: np.ndarray) -> float:
    """RMS ratio over the middle half, away from filter edges."""
    n = x.shape[-1]
    mid = slice(n // 4, 3 * n // 4)
    return float(np.sqrt(np.mean(y[..., mid] ** 2)) / np.sqrt(np.mean(x[..., mid] ** 2)))


@pytest.mark.parametrize("freq", [12.0, 15.0, 17.0, 30.0])
def test_bandpass_passband_is_unity(freq):
    x = sine(freq)
    assert gain(bandpass(x, FS, 8.0, 45.0), x) == pytest.approx(1.0, abs=0.02)


@pytest.mark.parametrize("freq", [1.0, 2.0, 90.0, 110.0])
def test_bandpass_stopband_attenuates(freq):
    x = sine(freq)
    assert gain(bandpass(x, FS, 8.0, 45.0), x) < 0.05


def test_bandpass_is_zero_phase():
    x = sine(15.0, phase=0.7)
    y = bandpass(x, FS, 8.0, 45.0)
    mid = slice(500, 1500)
    lags = np.arange(-10, 11)
    xc = [np.dot(x[mid], np.roll(y, -lag)[mid]) for lag in lags]
    assert lags[int(np.argmax(xc))] == 0
    np.testing.assert_allclose(y[mid], x[mid], atol=0.02)


def test_bandpass_high_edge_at_nyquist_is_highpass():
    x = sine(110.0) + 5.0 + sine(0.2)
    y = bandpass(x, FS, 1.0, FS / 2)
    assert gain(y, sine(110.0)) == pytest.approx(1.0, abs=0.02)
    assert abs(y[500:1500].mean()) < 0.05


def test_bandpass_low_edge_zero_is_lowpass():
    x = sine(5.0) + sine(80.0)
    y = bandpass(x, FS, 0.0, 30.0)
    np.testing.assert_allclose(y[500:1500], sine(5.0)[500:1500], atol=0.02)


def test_bandpass_filters_last_axis_of_nd_input():
    x = np.stack([sine(15.0), sine(100.0)])[None].repeat(3, axis=0)
    y = bandpass(x, FS, 8.0, 45.0)
    assert y.shape == x.shape
    assert gain(y[:, 0], x[:, 0]) == pytest.approx(1.0, abs=0.02)
    assert gain(y[:, 1], x[:, 1]) < 0.05


def test_bandpass_handles_short_input():
    y = bandpass(sine(15.0, seconds=0.2), FS, 8.0, 45.0)
    assert y.shape == (50,) and np.all(np.isfinite(y))


@pytest.mark.parametrize("low, high", [(10.0, 10.0), (20.0, 8.0)])
def test_bandpass_rejects_bad_edges(low, high):
    with pytest.raises(ValueError, match=str(low)):
        bandpass(sine(10.0), FS, low, high)


def test_notch_depth_and_neighbours():
    x = sine(60.0)
    assert 20 * np.log10(gain(notch(x, FS), x)) < -40.0
    for f in (15.0, 45.0, 75.0):
        assert gain(notch(sine(f), FS), sine(f)) == pytest.approx(1.0, abs=0.02)


def test_notch_is_zero_phase():
    x = sine(50.0, phase=0.3)
    np.testing.assert_allclose(notch(x, FS)[500:1500], x[500:1500], atol=0.05)


def test_notch_rejects_frequency_above_nyquist():
    with pytest.raises(ValueError, match="130"):
        notch(sine(10.0), FS, freq=130.0)


def test_preprocess_removes_dc_drift_and_mains():
    signal = 3.0 * sine(15.0)
    x = signal - 60000.0 + 50.0 * sine(0.1) + 20.0 * sine(60.0)
    y = preprocess(x[None], FS)[0]
    np.testing.assert_allclose(y[500:1500], signal[500:1500], atol=0.15)


def test_preprocess_notch_zero_disables_notch():
    x = sine(60.0)
    assert gain(preprocess(x, FS, notch_hz=0), x) == pytest.approx(1.0, abs=0.02)
    assert gain(preprocess(x, FS), x) < 0.01


def test_preprocess_band_none_only_demeans_and_notches():
    x = sine(3.0) + 7.0
    np.testing.assert_allclose(preprocess(x, FS, band=None, notch_hz=0), x - x.mean())


def test_preprocess_skips_notch_at_or_above_nyquist():
    x = sine(20.0, fs=100.0)
    y = preprocess(x, 100.0, band=(1.0, 45.0), notch_hz=60.0)
    assert gain(y, x) == pytest.approx(1.0, abs=0.02)


def test_default_bands():
    assert default_bands(250.0) == [(8.0, 45.0), (16.0, 45.0), (24.0, 45.0)]
    assert default_bands(256.0) == default_bands(250.0)
    five = default_bands(1000.0)
    assert len(five) == 5
    assert five[0] == (8.0, 88.0) and five[-1] == (40.0, 88.0)


def test_filterbank_shape_and_content():
    x = np.stack([sine(12.0) + sine(35.0)] * 2)
    fb = filterbank(x, FS, default_bands(FS))
    assert fb.shape == (3,) + x.shape
    assert gain(fb[0], x) == pytest.approx(1.0, abs=0.03)
    assert gain(fb[2], sine(35.0)[None].repeat(2, 0)) == pytest.approx(1.0, abs=0.03)


def test_filterbank_rejects_empty_bands():
    with pytest.raises(ValueError):
        filterbank(sine(10.0), FS, [])


def test_filterbank_weights():
    w = filterbank_weights(3)
    np.testing.assert_allclose(w, [1.25, 2 ** -1.25 + 0.25, 3 ** -1.25 + 0.25])
    assert np.all(np.diff(w) < 0)
    np.testing.assert_allclose(filterbank_weights(2, a=0.0, b=0.0), [1.0, 1.0])
    with pytest.raises(ValueError, match="0"):
        filterbank_weights(0)
