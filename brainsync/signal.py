"""Zero-phase filters and the single preprocessing chain used offline and online."""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy import signal as sps

Band = tuple[float, float]


def _check_fs(fs: float) -> float:
    fs = float(fs)
    if not np.isfinite(fs) or fs <= 0:
        raise ValueError(f"sampling rate must be positive, got {fs}")
    return fs


def _filtfilt(sos: np.ndarray, x: np.ndarray) -> np.ndarray:
    """sosfiltfilt along the last axis with the pad length clipped for short inputs."""
    n = x.shape[-1]
    if n < 2:
        raise ValueError(f"need at least 2 samples to filter, got {n}")
    pad = 3 * (2 * len(sos) + 1)
    return sps.sosfiltfilt(sos, x, axis=-1, padlen=min(pad, n - 1))


def bandpass(x: np.ndarray, fs: float, low: float, high: float, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth band-pass along the last axis.

    ``high`` at or above Nyquist gives a high-pass at ``low``; ``low`` <= 0 gives a low-pass.
    """
    fs = _check_fs(fs)
    nyq = fs / 2.0
    low, high = float(low), float(high)
    if order < 1:
        raise ValueError(f"filter order must be >= 1, got {order}")
    if high <= low:
        raise ValueError(f"band edges must satisfy low < high, got ({low}, {high})")
    x = np.asarray(x, dtype=float)
    if low <= 0 and high >= nyq:
        return x.copy()
    if high >= nyq:
        sos = sps.butter(order, low, btype="highpass", fs=fs, output="sos")
    elif low <= 0:
        sos = sps.butter(order, high, btype="lowpass", fs=fs, output="sos")
    else:
        sos = sps.butter(order, [low, high], btype="bandpass", fs=fs, output="sos")
    return _filtfilt(sos, x)


def notch(x: np.ndarray, fs: float, freq: float = 60.0, quality: float = 35.0) -> np.ndarray:
    """Zero-phase IIR notch at ``freq`` along the last axis."""
    fs = _check_fs(fs)
    if not 0 < freq < fs / 2.0:
        raise ValueError(f"notch frequency must lie in (0, {fs / 2.0}) Hz, got {freq}")
    if quality <= 0:
        raise ValueError(f"notch quality must be positive, got {quality}")
    b, a = sps.iirnotch(freq, quality, fs=fs)
    return _filtfilt(sps.tf2sos(b, a), np.asarray(x, dtype=float))


def preprocess(x: np.ndarray, fs: float, band: Band | None = (1.0, 100.0),
               notch_hz: float = 60.0) -> np.ndarray:
    """Demean, band-pass and notch along the last axis.

    This is the one preprocessing function: offline analysis and the online decoder both
    call it. ``band`` None skips the band-pass; ``notch_hz`` 0 disables the notch, and a
    notch at or above Nyquist is skipped.
    """
    fs = _check_fs(fs)
    x = np.asarray(x, dtype=float)
    y = x - x.mean(axis=-1, keepdims=True)
    if band is not None:
        y = bandpass(y, fs, band[0], band[1])
    if notch_hz and notch_hz > 0 and notch_hz < fs / 2.0:
        y = notch(y, fs, notch_hz)
    return y


def default_bands(fs: float, upper: float | None = None, n: int | None = None) -> list[Band]:
    """Filter-bank sub-bands (8 m, upper) Hz for m = 1..n (Chen et al. 2015, method M3).

    Defaults: three bands to 45 Hz below 500 Hz sampling, else five to 88 Hz. ``upper`` must
    stay below the Nyquist frequency and above the highest band's lower edge.
    """
    fs = _check_fs(fs)
    if upper is None:
        upper = 45.0 if fs < 500.0 else 88.0
    if n is None:
        n = 3 if fs < 500.0 else 5
    upper = float(upper)
    if int(n) != n or n < 1:
        raise ValueError(f"number of sub-bands must be a positive integer, got {n}")
    if not np.isfinite(upper) or upper >= fs / 2.0 or upper <= 8.0 * n:
        raise ValueError(f"filter-bank upper edge must lie in ({8.0 * n:g}, {fs / 2.0:g}) Hz "
                         f"for {n} sub-bands at {fs:g} Hz, got {upper}")
    return [(8.0 * m, upper) for m in range(1, int(n) + 1)]


def filterbank(x: np.ndarray, fs: float, bands: Sequence[Band]) -> np.ndarray:
    """Band-pass ``x`` into each sub-band; returns (n_bands, *x.shape)."""
    if len(bands) == 0:
        raise ValueError("filterbank needs at least one band, got []")
    x = np.asarray(x, dtype=float)
    return np.stack([bandpass(x, fs, lo, hi) for lo, hi in bands])


def filterbank_weights(n: int, a: float = 1.25, b: float = 0.25) -> np.ndarray:
    """Sub-band weights w_m = m**-a + b for m = 1..n."""
    if n < 1:
        raise ValueError(f"number of bands must be >= 1, got {n}")
    m = np.arange(1, n + 1, dtype=float)
    return m ** -a + b
