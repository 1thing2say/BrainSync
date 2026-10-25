"""Frame-indexed flicker luminance and frame-timing arithmetic (no display needed).

Luminance is a function of the frame count since flicker onset, never of the wall
clock: frame ``n`` of a target at ``f`` Hz on a ``refresh`` Hz display shows
``0.5 * (1 + depth * sin(2 pi f n / refresh + phase))`` for the sine waveform. The square
waveform is high for the first half of every cycle of that same phase:
``0.5 * (1 + depth * (2 on - 1))`` with ``on`` 1 then 0. The modulation ``depth`` in
(0, 1] swings the luminance around mid-grey 0.5 by ``depth / 2``; the default 1 is the full
black-to-white flicker, bit for bit the same as without a depth.
"""
from __future__ import annotations

import math

import numpy as np

WAVEFORMS = ("sine", "square")
DROP_FACTOR = 1.5


def luminance(freq: float, phase: float, frame: int | np.ndarray, refresh: float,
              waveform: str = "sine", depth: float = 1.0) -> float | np.ndarray:
    """Luminance in [0.5 - depth/2, 0.5 + depth/2] of one target on frame index ``frame``
    (scalar or array)."""
    if refresh <= 0:
        raise ValueError(f"refresh must be positive, got {refresh}")
    if waveform not in WAVEFORMS:
        raise ValueError(f"unknown waveform {waveform!r}; expected one of {WAVEFORMS}")
    if not 0.0 < depth <= 1.0:   # also refuses nan
        raise ValueError(f"depth must be in (0, 1], got {depth}")
    n = np.asarray(frame, dtype=float)
    if waveform == "sine":
        out = 0.5 * (1.0 + depth * np.sin(2.0 * np.pi * freq * n / refresh + phase))
    else:
        cycles = (freq * n) / refresh + phase / (2.0 * np.pi)
        on = (np.mod(cycles, 1.0) < 0.5).astype(float)
        out = 0.5 * (1.0 + depth * (2.0 * on - 1.0))
    return float(out) if out.ndim == 0 else out


def frame_luminances(freqs: np.ndarray, phases: np.ndarray, frame: int, refresh: float,
                     waveform: str = "sine", depth: float = 1.0) -> np.ndarray:
    """Luminance of every target on one frame, shape (n_targets,)."""
    freqs = np.asarray(freqs, dtype=float)
    phases = np.asarray(phases, dtype=float)
    out = (luminance(freqs, phases, frame, refresh, waveform, depth) if freqs.size
           else np.zeros(0))
    return np.atleast_1d(np.asarray(out, dtype=float))


def frames_per_cycle(freq: float, refresh: float) -> float:
    """Display frames in one flicker cycle (need not be an integer for sine flicker)."""
    if freq <= 0:
        raise ValueError(f"frequency must be positive, got {freq}")
    return refresh / freq


def square_duty_even(freq: float, refresh: float, tol: float = 1e-9) -> bool:
    """True when square flicker at ``freq`` has an exact 50 % duty cycle (even frames/cycle)."""
    fpc = frames_per_cycle(freq, refresh)
    k = round(fpc)
    return abs(fpc - k) < tol and k % 2 == 0


def n_frames(seconds: float, refresh: float) -> int:
    """Whole frames closest to ``seconds`` at ``refresh`` Hz."""
    if seconds < 0:
        raise ValueError(f"duration must be >= 0, got {seconds}")
    return int(round(seconds * refresh))


def to_psychopy_rgb(lum: float) -> tuple[float, float, float]:
    """Grey level in PsychoPy's -1..1 rgb space for luminance in [0, 1]."""
    v = 2.0 * float(lum) - 1.0
    return (v, v, v)


def to_psychopy_fill(lum: float, fill: tuple[float, float, float] = (1.0, 1.0, 1.0)
                     ) -> tuple[float, float, float]:
    """``lum`` scaled per channel by ``fill`` (each in [0, 1]; (1, 1, 1) is grey) in
    PsychoPy's -1..1 rgb space: (1, 0, 0) flickers a red square on black."""
    return tuple(2.0 * float(lum) * float(f) - 1.0 for f in fill)   # type: ignore[return-value]


def dropped_frames(flip_times: np.ndarray | list[float], refresh: float,
                   factor: float = DROP_FACTOR) -> int:
    """Count flip intervals longer than ``factor`` refresh periods."""
    t = np.asarray(flip_times, dtype=float)
    if t.size < 2:
        return 0
    return int(np.sum(np.diff(t) > factor / refresh))


def photosensitive(freq: float) -> bool:
    """15-25 Hz flicker is the band most likely to trigger photosensitive seizures."""
    return 15.0 <= freq <= 25.0


def phase_pi(phase: float) -> float:
    """Phase in multiples of pi, for printing."""
    return phase / math.pi
