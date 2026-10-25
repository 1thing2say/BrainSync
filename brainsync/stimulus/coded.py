"""Frames for the coded stimuli, c-VEP ([code]) and t-VEP ([time]), beside the frequency
flicker in ``display``.

A sequence object gives the luminance of every target on flicker frame ``i`` and the
markers that frame carries: ``cycle:<n>`` on the first frame of every m-sequence cycle,
``flash:<k>`` on the first frame of every flash of target k. The frame generators mirror
``display.cued_frames`` and ``display.free_frames`` (cue, flicker from frame 0, rest; or
continuous with a decoder's cue box), and ``display.play`` draws and time-stamps them
unchanged, so the onset of every cycle and flash lands in the marker log with the flip
time, which the c-VEP and t-VEP decoders need.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

import numpy as np

from ..vep.codes import bit_luminance, lagged_codes, m_sequence, time_schedule
from .config import StimulusConfig, Trial
from .display import END_PROMPT, Frame, free_frames, still_luminances
from .feedback import Control
from .flicker import n_frames

__all__ = ["CodeSequence", "TimeSequence", "coded_cued_frames", "coded_free_frames",
           "sequence_for"]

_SEED_RANGE = 2 ** 31


class CodeSequence:
    """c-VEP: every target shows the same m-sequence, target k delayed by k lags.

    Flicker frame ``i`` shows column ``i mod cycle_frames`` of the lagged code matrix;
    ``events(i)`` is ``cycle:<n>`` on each cycle's first frame. A code has no randomness,
    so ``restart`` changes nothing.
    """

    def __init__(self, cfg: StimulusConfig) -> None:
        if cfg.code is None:
            raise ValueError(f"config {cfg.name!r} has no [code] table")
        c = cfg.code
        self.codes = lagged_codes(m_sequence(c.length), cfg.n_targets, c.lag_frames,
                                  c.frames_per_bit)
        self.cycle_frames = c.cycle_frames
        self.depth = cfg.depth
        self.still = still_luminances(cfg)

    def restart(self) -> None:
        return None

    def luminance(self, i: int) -> np.ndarray:
        return bit_luminance(self.codes[:, i % self.cycle_frames], self.depth)

    def events(self, i: int) -> tuple[str, ...]:
        return (f"cycle:{i // self.cycle_frames}",) if i % self.cycle_frames == 0 else ()


class TimeSequence:
    """t-VEP: non-overlapping flashes, generated one round ahead of the frames asked for.

    Each round flashes every target once (``[time] order``); ``restart`` begins a fresh
    schedule from frame 0 with a new order drawn from the master seed, so every cued
    trial has its own flash order while the whole session stays reproducible.
    """

    def __init__(self, cfg: StimulusConfig, seed: int | None = None) -> None:
        if cfg.time is None:
            raise ValueError(f"config {cfg.name!r} has no [time] table")
        self.spec = cfg.time
        self.n_targets = cfg.n_targets
        self.depth = cfg.depth
        self.still = still_luminances(cfg)
        self._master = np.random.default_rng(seed)
        self.restart()

    def restart(self) -> None:
        self._seed = int(self._master.integers(_SEED_RANGE))
        self._onsets: dict[int, int] = {}
        self._bright: dict[int, int] = {}
        self._frames = 0
        self._rounds = 0

    def _extend(self, upto: int) -> None:
        while self._frames <= upto:
            sched = time_schedule(self.n_targets, self.spec.flash_frames, self.spec.gap_frames,
                                  rounds=1, order=self.spec.order,
                                  seed=(self._seed + self._rounds) % _SEED_RANGE)
            for frame, k in sched.onsets:
                f = frame + self._frames
                self._onsets[f] = k
                for j in range(self.spec.flash_frames):
                    self._bright[f + j] = k
            self._frames += sched.n_frames
            self._rounds += 1

    def luminance(self, i: int) -> np.ndarray:
        self._extend(i)
        bits = np.zeros(self.n_targets)
        k = self._bright.get(i)
        if k is not None:
            bits[k] = 1.0
        return bit_luminance(bits, self.depth)

    def events(self, i: int) -> tuple[str, ...]:
        self._extend(i)
        k = self._onsets.get(i)
        return (f"flash:{k}",) if k is not None else ()


Sequence = CodeSequence | TimeSequence


def sequence_for(cfg: StimulusConfig, seed: int | None = None) -> Sequence:
    """The config's sequence: CodeSequence for [code], TimeSequence for [time]."""
    if cfg.code is not None:
        return CodeSequence(cfg)
    if cfg.time is not None:
        return TimeSequence(cfg, seed)
    raise ValueError(f"config {cfg.name!r} has no [code] or [time] table; use "
                     "display.cued_frames / free_frames for a frequency flicker")


def coded_cued_frames(cfg: StimulusConfig, trials: list[Trial], seq: Sequence,
                      status: Callable[[], str] | None = None,
                      finish: Callable[[], bool] | None = None) -> Iterator[Frame]:
    """Frames of a cued session: per trial cue, the sequence from its frame 0, then rest.

    Same markers as ``display.cued_frames`` (cue, stim_on, stim_off) plus the sequence's
    own (cycle / flash). ``status()`` is polled on still frames for the text under the
    targets; with ``finish``, still frames follow the last trial until it turns true.
    """
    r = cfg.refresh
    still = seq.still
    n_cue = n_frames(cfg.session.cue_s, r)
    n_stim = max(1, n_frames(cfg.session.stim_s, r))
    n_rest = max(1, n_frames(cfg.session.rest_s, r))

    def note() -> str:
        return status() if status is not None else ""

    for tr in trials:
        k = tr.target
        seq.restart()
        for i in range(n_cue):
            yield Frame(still, cue=k, events=(f"cue:{k}",) if i == 0 else (), text=note())
        stim_cue = k if cfg.hold_cue else -1
        onset = ((f"cue:{k}",) if n_cue == 0 and cfg.hold_cue else ()) + (f"stim_on:{k}",)
        for i in range(n_stim):
            events = (onset if i == 0 else ()) + seq.events(i)
            yield Frame(seq.luminance(i), cue=stim_cue, events=events, trial=tr.index)
        for i in range(n_rest):
            yield Frame(still, events=(f"stim_off:{k}",) if i == 0 else (), text=note())
    if finish is not None:
        while not finish():
            yield Frame(still, text=note(), prompt=END_PROMPT)


def coded_free_frames(cfg: StimulusConfig, seq: Sequence, seconds: float | None = None,
                      cue: Callable[[], int] | None = None,
                      stop: Callable[[], bool] | None = None,
                      hold: Callable[[], bool] | None = None,
                      control: Callable[[], Control] | None = None,
                      card: Callable[[], int] | None = None,
                      flash: Callable[[], tuple[int, int]] | None = None) -> Iterator[Frame]:
    """The sequence running continuously from its frame 0: ``display.free_frames`` with
    ``seq`` in place of the frequency flicker, so the cue box, ``hold``, the JSON
    ``control`` lines (labels, centre text, active targets, flicker on / off) and the
    profile ``card`` / ``flash`` (BrainSwipe) work as there; ``seconds`` counts sequence
    frames only."""
    return free_frames(cfg, seconds, cue=cue, stop=stop, hold=hold, card=card, flash=flash,
                       control=control, seq=seq)
