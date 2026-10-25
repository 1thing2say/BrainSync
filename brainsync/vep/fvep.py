"""The paper's f-VEP decoder: power-spectrum SNR at each flicker frequency.

Bin et al. (2009), section 2.2: take the FFT of the window, define each target's SNR as
the power at its frequency over the mean power of the neighbouring frequency points, and
pick the largest. ``SNR`` does that in the ``classify.CCA`` interface (``decide``,
``gaps``, ``levels``, ``fit_baseline``), so it drops into ``online.Decoder`` and the
scoring tools beside CCA and FBCCA. Scores are in dB, averaged over the channels and, with
``harmonics`` above 1, over the harmonics of each frequency.
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ..classify import CCA

__all__ = ["SNR"]


class SNR(CCA):
    """FFT peak SNR per target (dB): the paper's f-VEP rule, channel-averaged."""

    name = "snr"

    def __init__(self, freqs: Sequence[float], fs: float, harmonics: int = 1,
                 phases: Sequence[float] | None = None, margin: float = 0.0,
                 threshold: float | None = None, neighbour_hz: float = 1.0,
                 exclude_hz: float = 0.2, resolution_hz: float = 0.1) -> None:
        super().__init__(freqs, fs, harmonics, phases, margin, threshold)
        for name, value in (("neighbour_hz", neighbour_hz), ("exclude_hz", exclude_hz),
                            ("resolution_hz", resolution_hz)):
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if exclude_hz >= neighbour_hz:
            raise ValueError(f"exclude_hz {exclude_hz} must be below neighbour_hz {neighbour_hz}")
        self.neighbour_hz = float(neighbour_hz)
        self.exclude_hz = float(exclude_hz)
        self.resolution_hz = float(resolution_hz)

    def __repr__(self) -> str:
        return (f"SNR(freqs={self.freqs.tolist()}, fs={self.fs}, harmonics={self.harmonics}, "
                f"margin={self.margin}, threshold={self.threshold}, "
                f"neighbour_hz={self.neighbour_hz}, exclude_hz={self.exclude_hz})")

    def spectrum(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Channel-mean power spectrum (n_trials, n_bins) and the bin frequencies, from a
        Hann-windowed FFT zero-padded to at least ``resolution_hz`` bins."""
        X = self._windows(X)
        n = X.shape[-1]
        n_fft = int(max(n, 2 ** np.ceil(np.log2(self.fs / self.resolution_hz))))
        centred = (X - X.mean(axis=-1, keepdims=True)) * np.hanning(n)
        power = np.abs(np.fft.rfft(centred, n=n_fft, axis=-1)) ** 2
        return power.mean(axis=1), np.fft.rfftfreq(n_fft, 1.0 / self.fs)

    def raw_scores(self, X: np.ndarray) -> np.ndarray:
        """SNR in dB (n_trials, n_targets): peak power within exclude_hz of the frequency
        over the mean power of the bins between exclude_hz and neighbour_hz away."""
        power, bins = self.spectrum(X)
        out = np.zeros((power.shape[0], self.n_targets))
        for k, f in enumerate(self.freqs):
            terms = []
            for h in range(1, self.harmonics + 1):
                fh = h * f
                if fh >= self.fs / 2.0:
                    continue
                dist = np.abs(bins - fh)
                peak = power[:, dist <= self.exclude_hz].max(axis=1)
                ring = power[:, (dist > self.exclude_hz) & (dist <= self.neighbour_hz)]
                if ring.shape[1] == 0:
                    raise ValueError(f"no neighbour bins around {fh:g} Hz; lower resolution_hz")
                floor = ring.mean(axis=1)
                terms.append(10.0 * np.log10(np.maximum(peak, 1e-300) / np.maximum(floor, 1e-300)))
            out[:, k] = np.mean(terms, axis=0)   # the fundamental is always below Nyquist
        return out
