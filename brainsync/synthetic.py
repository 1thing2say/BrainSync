"""Synthetic SSVEP data for tests and dry runs."""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .recordings import MONTAGE, POSTERIOR

SESSION_T0 = 1_792_700_000.0  # unix seconds; a whole second so sample times print exactly
RESPONSE_LATENCY = 0.14       # seconds from stim_on until the fake cortex follows the flicker


def _background(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    """Unit-variance noise along the last axis: equal parts white and 1/f (pink)."""
    n = shape[-1]
    white = rng.standard_normal(shape)
    spec = np.fft.rfft(rng.standard_normal(shape), axis=-1)
    k = np.arange(spec.shape[-1], dtype=float)
    k[0] = 1.0
    pink = np.fft.irfft(spec / np.sqrt(k), n=n, axis=-1)
    pink -= pink.mean(axis=-1, keepdims=True)
    pink /= pink.std(axis=-1, keepdims=True) + 1e-12
    return (white + pink) / np.sqrt(2.0)


def _check_freqs(freqs: Sequence[float], fs: float) -> np.ndarray:
    f = np.asarray(freqs, dtype=float).reshape(-1)
    if f.size == 0:
        raise ValueError("need at least one frequency, got []")
    if fs <= 0:
        raise ValueError(f"fs must be positive, got {fs}")
    if np.any(f <= 0) or np.any(f >= fs / 2.0):
        raise ValueError(f"frequencies must lie in (0, {fs / 2.0}) Hz, got {f.tolist()}")
    return f


def ssvep_trials(freqs: Sequence[float], fs: float = 250.0, n_per_target: int = 10,
                 n_channels: int = 4, seconds: float = 4.0, snr_db: float = -10.0,
                 harmonics: int = 2, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """SSVEP trials (n_targets * n_per_target, n_channels, n) and labels, grouped by target.

    Each channel sees the target frequency and its harmonics (amplitude 1/h) with a fixed
    per-channel gain and phase, plus small per-trial phase jitter, over unit-variance
    background noise; ``snr_db`` is mean signal power over noise power.
    """
    f = _check_freqs(freqs, fs)
    if n_per_target < 1 or n_channels < 1 or harmonics < 1:
        raise ValueError(f"n_per_target, n_channels and harmonics must be >= 1, got "
                         f"{n_per_target}, {n_channels}, {harmonics}")
    n = int(round(seconds * fs))
    if n < 2:
        raise ValueError(f"seconds * fs must give at least 2 samples, got {seconds} s at {fs} Hz")
    rng = np.random.default_rng(seed)
    tt = np.arange(n) / fs
    gains = rng.uniform(0.5, 1.5, n_channels)
    delays = rng.uniform(0.0, np.pi / 2, (n_channels, harmonics))
    h = np.arange(1, harmonics + 1, dtype=float)
    power = np.mean(gains ** 2) * np.sum(1.0 / h ** 2) / 2.0
    scale = np.sqrt(10.0 ** (snr_db / 10.0) / power)
    y = np.repeat(np.arange(f.size), n_per_target)
    jitter = rng.normal(0.0, 0.2, y.size)
    X = _background(rng, (y.size, n_channels, n))
    for i, (k, dphi) in enumerate(zip(y, jitter)):
        arg = 2 * np.pi * f[k] * h[:, None] * tt + (h * dphi)[:, None]  # (harmonics, n)
        waves = np.sin(arg[None] + delays[:, :, None]) / h[None, :, None]
        X[i] += scale * gains[:, None] * waves.sum(axis=1)
    return X, y


def vep_kernel(fs: float = 250.0, seconds: float = 0.5, latency: float = 0.06,
               freq: float = 6.0, decay: float = 0.1) -> np.ndarray:
    """A made-up flash VEP (impulse response), peak amplitude 1, ``seconds`` long.

    Nothing for ``latency`` seconds, then a damped sine (``freq`` Hz, time constant
    ``decay`` s): a positive peak near 100 ms, a trough near 190 ms, a small second peak
    and silence by 400 ms, the shape of Fig. 2(b) in Bin et al. (2009). A linear model:
    the EEG that a stimulus sequence evokes is the sequence convolved with this kernel
    (``sequence_response``), which is what the t-VEP and c-VEP decoders assume.
    """
    if fs <= 0 or seconds <= 0 or decay <= 0 or freq <= 0:
        raise ValueError(f"fs, seconds, decay and freq must be positive, got {fs}, {seconds}, "
                         f"{decay}, {freq}")
    if latency < 0 or latency >= seconds:
        raise ValueError(f"latency must lie in [0, seconds), got {latency} with {seconds} s")
    t = np.arange(int(round(seconds * fs))) / fs - latency
    k = np.where(t >= 0, np.exp(-np.maximum(t, 0) / decay) * np.sin(2 * np.pi * freq * t), 0.0)
    peak = np.max(np.abs(k))
    if peak <= 0:
        raise ValueError(f"kernel of {seconds} s at {fs} Hz has no samples after the latency")
    return k / peak


def sequence_response(stim: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Causal convolution of a stimulus signal (same sample rate as the kernel) with the
    VEP kernel, trimmed to the stimulus length: the evoked EEG of the linear model."""
    s = np.asarray(stim, dtype=float).reshape(-1)
    k = np.asarray(kernel, dtype=float).reshape(-1)
    if s.size == 0 or k.size == 0:
        raise ValueError("stimulus and kernel must not be empty")
    return np.convolve(s, k)[:s.size]


def rest_trials(n: int, fs: float = 250.0, n_channels: int = 4, seconds: float = 4.0,
                seed: int = 0) -> np.ndarray:
    """Background-only trials (n, n_channels, samples) matching ssvep_trials' noise."""
    if n < 1 or n_channels < 1:
        raise ValueError(f"n and n_channels must be >= 1, got {n}, {n_channels}")
    samples = int(round(seconds * fs))
    if samples < 2:
        raise ValueError(f"seconds * fs must give at least 2 samples, got {seconds} s at {fs} Hz")
    return _background(np.random.default_rng(seed), (n, n_channels, samples))


def fake_session(freqs: Sequence[float], labels: Sequence[int], fs: float = 250.0,
                 cue_s: float = 1.0, stim_s: float = 4.0, rest_s: float = 3.0,
                 amp: float = 2.0, seed: int = 0,
                 noise: float = 5.0) -> tuple[np.ndarray, np.ndarray, str]:
    """A raw 8-channel session laid out like the real recordings.

    Returns X (8, n) in µV with DC offsets and 60 Hz mains, unix times t, and a stimulus
    log text with cue/stim_on/stim_off markers. Every marker falls exactly on a sample.
    Posterior channels carry ``amp`` µV of the cued frequency (plus a half-amplitude second
    harmonic) from stim_on to stim_off, delayed by RESPONSE_LATENCY.
    """
    f = _check_freqs(freqs, fs)
    labels = [int(k) for k in labels]
    if any(not 0 <= k < f.size for k in labels):
        raise ValueError(f"labels must index freqs {f.tolist()}, got {labels}")
    if cue_s < 0 or stim_s <= 0 or rest_s < 0:
        raise ValueError(f"durations must be cue>=0, stim>0, rest>=0, got {cue_s}, {stim_s}, {rest_s}")
    rng = np.random.default_rng(seed)
    lead = tail = 1.0
    period = cue_s + stim_s + rest_s
    n = int(round((lead + period * len(labels) + tail) * fs))
    t = SESSION_T0 + np.arange(n) / fs
    rel = np.arange(n) / fs

    X = noise * _background(rng, (len(MONTAGE), n))
    X += rng.uniform(-80_000.0, -20_000.0, (len(MONTAGE), 1))
    X += 20.0 * np.sin(2 * np.pi * 60.0 * rel + rng.uniform(0, 2 * np.pi, (len(MONTAGE), 1)))
    post = [MONTAGE.index(c) for c in POSTERIOR]
    gains = rng.uniform(0.7, 1.3, len(post))

    def sample(sec: float) -> int:
        return int(round(sec * fs))

    lines = ["measured refresh: 60.00 Hz"]

    def mark(idx: int, name: str) -> None:
        lines.append(f"[marker {t[idx]:.6f}] {name}")

    mark(0, "session_start")
    for i, k in enumerate(labels):
        start = lead + i * period
        i_cue, i_on, i_off = sample(start), sample(start + cue_s), sample(start + cue_s + stim_s)
        mark(i_cue, f"cue:{k}")
        mark(i_on, f"stim_on:{k}")
        mark(i_off, f"stim_off:{k}")
        lag = sample(RESPONSE_LATENCY)
        seg = slice(min(i_on + lag, n), min(i_off + lag, n))
        tau = rel[seg] - rel[i_on]
        wave = np.sin(2 * np.pi * f[k] * tau) + 0.5 * np.sin(4 * np.pi * f[k] * tau)
        X[post, seg] += amp * gains[:, None] * wave
    mark(n - 1, "session_end")
    lines.append(f"finished: {len(labels)} trials, 0 dropped frames")
    return X, t, "\n".join(lines) + "\n"
