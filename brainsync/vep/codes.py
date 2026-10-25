"""Stimulus sequences for the three VEP codings (Bin et al. 2009), as frame-indexed bits.

Everything here is pure arithmetic on display frames; nothing needs a screen. A bit of 1
is a bright square, 0 a dark one (``bit_luminance`` maps bits to the same luminance scale
as ``brainsync.stimulus.flicker``).

- **c-VEP**: one binary m-sequence from a maximal linear feedback shift register (length
  2^n - 1). Its circular autocorrelation is N at lag 0 and -1 everywhere else, so the
  sequence and its own time shifts are nearly orthogonal: target k shows the sequence
  delayed by ``k * lag_frames`` frames (the paper: 63 bits, 16 targets, 4-frame lags).
- **t-VEP**: brief flashes that never overlap. Targets flash in rounds, one slot of
  ``flash_frames + gap_frames`` frames each, in a fixed or a per-round shuffled order, so
  each target flashes well under the paper's 4 Hz and any two flash onsets are at least a
  slot apart. (The paper's bound keeps one target's consecutive responses from overlapping;
  the screen as a whole flashes once a slot, 4 Hz in tvep_4, so an epoch also holds the start
  of another target's response, which the per-target average cancels.)
- **f-VEP** needs no sequence: ``brainsync.stimulus.flicker`` already does it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["PRIMITIVE_TAPS", "ORDERS", "Schedule", "autocorrelation", "bit_luminance",
           "degree_for_length", "flash_rate_hz", "lagged_codes", "m_sequence",
           "min_lag_distance", "time_schedule"]

# One primitive polynomial over GF(2) per degree, as the exponents of its non-constant
# terms (x^6 + x + 1 is (6, 1)). Any of them gives a maximal-length sequence.
PRIMITIVE_TAPS: dict[int, tuple[int, ...]] = {
    3: (3, 1), 4: (4, 1), 5: (5, 2), 6: (6, 1), 7: (7, 1), 8: (8, 6, 5, 4), 9: (9, 4),
    10: (10, 3),
}
ORDERS = ("roundrobin", "random")


def degree_for_length(length: int) -> int:
    """The shift-register degree n with 2^n - 1 == length; refuses other lengths."""
    if int(length) != length or length < 1:
        raise ValueError(f"m-sequence length must be a positive integer, got {length!r}")
    n = int(length).bit_length()
    if 2 ** n - 1 != length:
        ok = ", ".join(str(2 ** d - 1) for d in sorted(PRIMITIVE_TAPS))
        raise ValueError(f"m-sequence length must be 2^n - 1 ({ok}), got {length}")
    if n not in PRIMITIVE_TAPS:
        raise ValueError(f"no primitive polynomial listed for degree {n} (length {length}); "
                         f"known degrees: {sorted(PRIMITIVE_TAPS)}")
    return n


def m_sequence(length: int = 63, seed: int = 1) -> np.ndarray:
    """One period of a binary m-sequence (int8 0/1, shape (length,)).

    A Fibonacci LFSR of degree n (``degree_for_length``) with the listed primitive taps,
    started from the non-zero state ``seed`` (1..2^n - 1); each seed gives a cyclic shift of
    the same sequence.
    """
    n = degree_for_length(length)
    if int(seed) != seed or not 1 <= seed < 2 ** n:
        raise ValueError(f"seed must be an integer in [1, {2 ** n - 1}], got {seed!r}")
    taps = PRIMITIVE_TAPS[n]
    state = [(int(seed) >> i) & 1 for i in range(n)]   # state[i] holds x^(i+1)
    out = np.empty(length, dtype=np.int8)
    for i in range(length):
        out[i] = state[-1]
        feedback = 0
        for e in taps:
            feedback ^= state[e - 1]
        state = [feedback] + state[:-1]
    return out


def _bits(seq: np.ndarray, what: str = "sequence") -> np.ndarray:
    b = np.asarray(seq).reshape(-1)
    if b.size == 0:
        raise ValueError(f"{what} is empty")
    if not np.all((b == 0) | (b == 1)):
        raise ValueError(f"{what} must hold 0/1 bits only, got {np.unique(b).tolist()}")
    return b.astype(np.int8)


def autocorrelation(seq: np.ndarray) -> np.ndarray:
    """Circular autocorrelation of the +/-1 version of a bit sequence, one value per lag.

    For an m-sequence of length N this is N at lag 0 and -1 at every other lag.
    """
    s = 2.0 * _bits(seq) - 1.0
    spec = np.fft.fft(s)
    return np.rint(np.real(np.fft.ifft(spec * np.conj(spec)))).astype(int)


def lagged_codes(seq: np.ndarray, n_targets: int, lag_frames: int,
                 frames_per_bit: int = 1) -> np.ndarray:
    """Per-target frame bits (n_targets, cycle_frames): target k is the sequence delayed
    by ``k * lag_frames`` frames, each bit held for ``frames_per_bit`` frames.

    The lags must be distinct within one cycle: ``(n_targets - 1) * lag_frames`` below
    ``len(seq) * frames_per_bit``.
    """
    bits = _bits(seq)
    for name, value, low in (("n_targets", n_targets, 1), ("lag_frames", lag_frames, 1),
                             ("frames_per_bit", frames_per_bit, 1)):
        if int(value) != value or value < low:
            raise ValueError(f"{name} must be an integer >= {low}, got {value!r}")
    cycle = np.repeat(bits, int(frames_per_bit))
    n_frames = cycle.size
    if (n_targets - 1) * lag_frames >= n_frames:
        raise ValueError(f"{n_targets} targets {lag_frames} frames apart need "
                         f"{(n_targets - 1) * lag_frames + 1} frames, but one cycle has "
                         f"{n_frames}; fewer targets, a shorter lag or a longer sequence")
    return np.stack([np.roll(cycle, k * int(lag_frames)) for k in range(int(n_targets))])


def min_lag_distance(n_targets: int, lag_frames: int, cycle_frames: int) -> int:
    """Smallest circular distance (frames) between any two targets' sequences."""
    if n_targets < 2:
        return int(cycle_frames)
    lags = (np.arange(n_targets) * lag_frames) % cycle_frames
    diff = np.abs(lags[:, None] - lags[None, :])
    d = np.minimum(diff, cycle_frames - diff)
    return int(d[~np.eye(n_targets, dtype=bool)].min())


def bit_luminance(bits: np.ndarray, depth: float = 1.0) -> np.ndarray:
    """Luminance in [0.5 - depth/2, 0.5 + depth/2] for 0/1 bits (bit 1 bright); the same
    scale as the square flicker in ``brainsync.stimulus.flicker``."""
    if not 0.0 < depth <= 1.0:   # also refuses nan
        raise ValueError(f"depth must be in (0, 1], got {depth}")
    return 0.5 * (1.0 + depth * (2.0 * np.asarray(bits, dtype=float) - 1.0))


# -- t-VEP ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Schedule:
    """A t-VEP flash schedule: ``onsets`` (frame, target) pairs in time order over
    ``n_frames`` frames; ``flash_frames`` bright frames per flash."""

    n_targets: int
    flash_frames: int
    gap_frames: int
    onsets: tuple[tuple[int, int], ...]
    n_frames: int

    @property
    def slot_frames(self) -> int:
        return self.flash_frames + self.gap_frames

    def matrix(self) -> np.ndarray:
        """Per-target frame bits (n_targets, n_frames)."""
        m = np.zeros((self.n_targets, self.n_frames), dtype=np.int8)
        for frame, k in self.onsets:
            m[k, frame:frame + self.flash_frames] = 1
        return m

    def onsets_of(self, target: int) -> np.ndarray:
        """Onset frames of one target, ascending."""
        return np.array([f for f, k in self.onsets if k == target], dtype=int)


def flash_rate_hz(n_targets: int, flash_frames: int, gap_frames: int, refresh: float) -> float:
    """How often each target flashes: one flash per round of ``n_targets`` slots."""
    if refresh <= 0:
        raise ValueError(f"refresh must be positive, got {refresh}")
    return float(refresh) / (n_targets * (flash_frames + gap_frames))


def time_schedule(n_targets: int, flash_frames: int, gap_frames: int, rounds: int,
                  order: str = "random", seed: int | None = None) -> Schedule:
    """Non-overlapping flashes: every round flashes each target once, one slot each.

    ``roundrobin`` keeps the order 0, 1, ..., n-1 every round; ``random`` shuffles it per
    round (seeded), so a target's flash times are irregular but no two flashes ever share
    a frame and consecutive onsets are a whole slot apart.
    """
    for name, value, low in (("n_targets", n_targets, 1), ("flash_frames", flash_frames, 1),
                             ("gap_frames", gap_frames, 0), ("rounds", rounds, 1)):
        if int(value) != value or value < low:
            raise ValueError(f"{name} must be an integer >= {low}, got {value!r}")
    if order not in ORDERS:
        raise ValueError(f"unknown order {order!r}; expected one of {ORDERS}")
    slot = int(flash_frames) + int(gap_frames)
    rng = np.random.default_rng(seed)
    onsets: list[tuple[int, int]] = []
    frame = 0
    for _ in range(int(rounds)):
        targets = rng.permutation(int(n_targets)) if order == "random" else np.arange(n_targets)
        for k in targets:
            onsets.append((frame, int(k)))
            frame += slot
    return Schedule(n_targets=int(n_targets), flash_frames=int(flash_frames),
                    gap_frames=int(gap_frames), onsets=tuple(onsets), n_frames=frame)
