"""The c-VEP standard every BrainSync app uses: two white squares, one m-sequence.

    dec = CodeVepDecoder(fs=250.0)                      # the standard: 3 targets, 21-frame lags
    for X, t in board_reads:                            # every board read, always
        events = [(t_cycle, "cycle", n), ...]           # each cycle's first flip (Unix time)
        for d in dec.feed(X, t, events):
            show(confidence(d, ("YES", "NO")))          # bars under the squares
            if d.changed and d.committed != ABSTAIN:
                act(d.committed)

The standard (docs/CVEP-STANDARD.md): one 63-bit m-sequence (``codes.m_sequence(63)``, seed
1), frame ``n`` of target ``k`` shows bit ``BITS[(n - k * LAG_FRAMES) mod 63]``, 1 white and
0 black, so the two targets are 31 frames (517 ms at 60 Hz) apart, the widest two allow.
**Never more than two targets** (``MAX_TARGETS``): every app, tool and config in BrainSync
uses at most two squares, as far apart as the screen allows.
A cycle is 63 frames, 1.05 s at 60 Hz. The display reports the Unix time of every cycle's
first flip as a ``(t, "cycle", n)`` event.

``CodeVepDecoder`` trains itself: the first ``train_cycles`` complete cycles (default 30,
about 32 s) are recorded while the user looks at ``train_target`` (default 0) and become
the template (``vep.cvep.fit_template``); ``train_accuracy`` is the leave-one-cycle-out
accuracy (``vep.cvep.cross_validate``), the number to show the user, against
``1 / n_targets`` chance. From then on every complete cycle gives a decision from the mean
of the last ``cycles`` cycles (``vep.sync.SyncDecoder``): per-target Pearson correlations
(the confidence values), the lead over the runner-up, and the committed target after
``votes`` agreeing decisions. ``template=`` skips training.

``confidence``, ``confidence_labels`` and ``confidence_line`` turn a decision into what the
apps show: a bar and score per target, and "lead 0.31   choice: YES".
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from ..metrics import ABSTAIN, accuracy
from ..online import Decision
from ..recordings import MONTAGE, POSTERIOR, Event, channel_indices
from ..signal import Band, preprocess
from .codes import m_sequence
from .cvep import Template, cross_validate, fit_template, lag_samples
from .sync import SyncDecoder, cut_epochs

__all__ = ["BITS", "CONFIG", "MAX_TARGETS", "CYCLE_FRAMES", "CodeVepDecoder", "Confidence", "LAG_FRAMES",
           "LENGTH", "N_TARGETS", "TRAIN_CYCLES", "bar", "code_frame", "confidence",
           "confidence_labels", "confidence_line", "frame_bit", "standard_fake_board"]

LENGTH = 63              # bits in one m-sequence period
SEED = 1                 # LFSR start state; the JS copy (clients/js/cvep.js) uses the same bits
N_TARGETS = 2
MAX_TARGETS = 2          # the project-wide cap: never more than two squares
LAG_FRAMES = 31          # 63 // 2: the widest spacing two targets allow
CYCLE_FRAMES = LENGTH    # one frame per bit
TRAIN_CYCLES = 30
CYCLES = 2               # cycles averaged per decision
VOTES = 2                # agreeing decisions before a commit
CONFIG = "cvep_2"        # the PsychoPy stimulus config with these values
BAND: Band = (1.0, 30.0)
BAR_CELLS = 8

BITS: tuple[int, ...] = tuple(int(b) for b in m_sequence(LENGTH, SEED))


def frame_bit(target: int, frame: int, lag_frames: int = LAG_FRAMES,
              bits: Sequence[int] = BITS) -> int:
    """Bit (1 white, 0 black) target ``target`` shows on flicker frame ``frame``."""
    n = len(bits)
    return int(bits[(int(frame) - int(target) * int(lag_frames)) % n])


def code_frame(frame: int, n_targets: int = N_TARGETS, lag_frames: int = LAG_FRAMES,
               bits: Sequence[int] = BITS) -> tuple[int, ...]:
    """Every target's bit on flicker frame ``frame``."""
    return tuple(frame_bit(k, frame, lag_frames, bits) for k in range(int(n_targets)))


# -- the decoder ----------------------------------------------------------------------------

class CodeVepDecoder:
    """Streaming c-VEP: trains a template on the first cycles, then decodes every cycle.

    ``feed(X, t, events)`` takes every board read (all channels, Unix sample times) and the
    display's ``(t, "cycle", n)`` events, and returns decisions (none while training).
    """

    def __init__(self, fs: float, *, n_targets: int = N_TARGETS, lag_frames: int = LAG_FRAMES,
                 cycle_frames: int = CYCLE_FRAMES, refresh: float = 60.0,
                 channels: Sequence[str | int] | None = POSTERIOR,
                 ch_names: Sequence[str] = MONTAGE, band: Band | None = BAND,
                 notch_hz: float = 60.0, votes: int = VOTES, cycles: int = CYCLES,
                 margin: float = 0.0, pad_s: float = 0.5, train_cycles: int = TRAIN_CYCLES,
                 train_target: int = 0, template: Template | None = None) -> None:
        if int(n_targets) != n_targets or n_targets < 2:
            raise ValueError(f"n_targets must be an integer >= 2, got {n_targets!r}")
        if n_targets > MAX_TARGETS:
            raise ValueError(f"never more than {MAX_TARGETS} targets (docs/CVEP-STANDARD.md), "
                             f"got {n_targets}")
        if (n_targets - 1) * lag_frames >= cycle_frames:
            raise ValueError(f"{n_targets} targets {lag_frames} frames apart do not fit in a "
                             f"{cycle_frames}-frame cycle")
        if int(train_cycles) != train_cycles or train_cycles < 2:
            raise ValueError(f"train_cycles must be an integer >= 2, got {train_cycles!r}")
        if not 0 <= train_target < n_targets:
            raise ValueError(f"train_target {train_target} is not among {n_targets} targets")
        self.fs, self.refresh = float(fs), float(refresh)
        self.n_targets, self.lag_frames = int(n_targets), int(lag_frames)
        self.cycle_frames = int(cycle_frames)
        self.ch_names = tuple(str(c) for c in ch_names)
        self.channels = channels
        self.rows = channel_indices(self.ch_names, channels)
        self.band, self.notch_hz = band, float(notch_hz)
        self.votes, self.cycles, self.margin, self.pad_s = int(votes), int(cycles), margin, pad_s
        self.train_cycles, self.train_target = int(train_cycles), int(train_target)
        self.n_samples = int(round(self.cycle_frames / self.refresh * self.fs))
        self.lag_samples = lag_samples(self.lag_frames, self.fs, self.refresh)
        self.train_accuracy: float = math.nan
        self._Xs: list[np.ndarray] = []
        self._ts: list[np.ndarray] = []
        self._marks: list[float] = []
        self.template: Template | None = None
        self.decoder: SyncDecoder | None = None
        if template is not None:
            self.use_template(template)

    # -- state ------------------------------------------------------------------------------

    @property
    def training(self) -> bool:
        return self.decoder is None

    @property
    def progress(self) -> tuple[int, int]:
        """(complete training cycles seen, cycles needed); (needed, needed) once trained."""
        if not self.training:
            return self.train_cycles, self.train_cycles
        return min(len(self._marks), self.train_cycles), self.train_cycles

    @property
    def chance(self) -> float:
        return 1.0 / self.n_targets

    @property
    def committed(self) -> int:
        return ABSTAIN if self.decoder is None else self.decoder.committed

    def use_template(self, template: Template) -> None:
        """Skip (or replace) training: decode with ``template`` from now on."""
        if template.n_targets != self.n_targets:
            raise ValueError(f"template has {template.n_targets} targets, the decoder "
                             f"{self.n_targets}")
        self.template = template
        self.decoder = SyncDecoder(self.fs, template=template, channels=self.channels,
                                   ch_names=self.ch_names, band=self.band,
                                   notch_hz=self.notch_hz, votes=self.votes,
                                   cycles=self.cycles, pad_s=self.pad_s, margin=self.margin)
        self._Xs, self._ts, self._marks = [], [], []

    def retrain(self) -> None:
        """Forget the template and record ``train_cycles`` new cycles (a new session)."""
        self.template, self.decoder = None, None
        self.train_accuracy = math.nan
        self._Xs, self._ts, self._marks = [], [], []

    def reset(self) -> None:
        """A new layout: drop buffered cycles and votes (training keeps what it has)."""
        if self.decoder is not None:
            self.decoder.reset()

    # -- feeding ----------------------------------------------------------------------------

    def feed(self, X: np.ndarray, t: np.ndarray, events: Iterable[Event] = ()) -> list[Decision]:
        """Every board read and the cycle events since the last call; decisions made."""
        X = np.asarray(X, dtype=float)
        t = np.asarray(t, dtype=float).reshape(-1)
        cycle_events = [e for e in events if e[1] == "cycle"]
        if self.decoder is not None:
            return self.decoder.feed(X, t, cycle_events)
        if X.ndim != 2 or X.shape[1] != t.size:
            raise ValueError(f"X must be (n_ch, n) with n == len(t), got {X.shape}, {t.size}")
        if t.size:
            self._Xs.append(X[list(self.rows)])
            self._ts.append(t)
        self._marks += [float(e[0]) for e in cycle_events]
        if len(self._marks) < self.train_cycles or not self._ts:
            return []
        last_needed = self._marks[self.train_cycles - 1] + self.n_samples / self.fs + self.pad_s
        if self._ts[-1][-1] < last_needed:
            return []
        self._train()
        return []

    def _train(self) -> None:
        Xr = np.concatenate(self._Xs, axis=1)
        tr = np.concatenate(self._ts)
        Xf = preprocess(Xr, self.fs, self.band, notch_hz=self.notch_hz)
        cycles, _ = cut_epochs(Xf, tr, self._marks[:self.train_cycles], self.n_samples)
        if cycles.shape[0] < 2:
            raise RuntimeError(f"only {cycles.shape[0]} training cycle(s) could be cut; "
                               "are the cycle times on the board's clock?")
        data = {self.train_target: cycles}
        names = tuple(self.ch_names[i] for i in self.rows)
        tpl = fit_template(data, self.n_targets, self.lag_samples, self.fs,
                           base=self.train_target, ch_names=names)
        y, pred = cross_validate(data, self.n_targets, self.lag_samples, self.fs,
                                 base=self.train_target)
        self.train_accuracy = float(accuracy(y, pred))
        self.use_template(tpl)


# -- confidence -----------------------------------------------------------------------------

def bar(value: float, cells: int = BAR_CELLS) -> str:
    """``cells`` characters, filled in proportion to ``value`` clipped to 0..1."""
    v = 0.0 if not np.isfinite(value) else min(1.0, max(0.0, float(value)))
    n = int(round(v * cells))
    return "█" * n + "░" * (cells - n)


@dataclass(frozen=True)
class Confidence:
    """What an app shows for one decision: a score per label, the best label this
    decision, the committed label, the lead over the runner-up and whether this decision
    moved the commitment. Scores are Pearson correlations, not probabilities."""

    scores: dict[str, float]
    raw: str | None
    committed: str | None
    lead: float | None
    changed: bool

    def message(self) -> dict:
        """The JSON-ready form the protocols send (docs/CVEP-STANDARD.md, "decision")."""
        return {"scores": {k: round(v, 4) for k, v in self.scores.items()},
                "raw": self.raw, "committed": self.committed,
                "lead": None if self.lead is None else round(self.lead, 4),
                "changed": self.changed}


def _label(labels: Sequence[str], k: int | None) -> str | None:
    return None if k is None or k == ABSTAIN or not 0 <= k < len(labels) else labels[k]


def confidence(d: Decision, labels: Sequence[str]) -> Confidence:
    """The decision as labelled scores (a non-finite score shows as 0)."""
    scores = {}
    for k, label in enumerate(labels):
        s = float(d.scores[k]) if k < len(d.scores) else math.nan
        scores[label] = s if np.isfinite(s) else 0.0
    lead = float(d.gap) if np.isfinite(d.gap) else None
    return Confidence(scores, _label(labels, d.raw), _label(labels, d.committed), lead,
                      bool(d.changed))


def confidence_labels(d: Decision, labels: Sequence[str], scale: float = 1.0) -> list[str]:
    """One line per target for under its square: the label, a bar and the score, "<" after
    this decision's best."""
    width = max(len(s) for s in labels)
    lines = []
    for k, label in enumerate(labels):
        s = float(d.scores[k]) if k < len(d.scores) else math.nan
        best = " <" if k == d.raw else ""
        shown = "  -  " if not np.isfinite(s) else f"{s:+.2f}"
        lines.append(f"{label:<{width}} {bar(s / scale)} {shown}{best}")
    return lines


def confidence_line(d: Decision, labels: Sequence[str]) -> str:
    """"lead 0.31   choice: YES *" ("*" when this decision moved the commitment)."""
    gap = "lead  -  " if not np.isfinite(d.gap) else f"lead {d.gap:.2f}"
    choice = ("no choice yet" if d.committed == ABSTAIN
              else f"choice: {labels[d.committed]}" + (" *" if d.changed else ""))
    return f"{gap}   {choice}"


# -- fake board -----------------------------------------------------------------------------

def standard_fake_board(fixated: int = 0, *, amp: float = 3.0, noise: float = 8.0,
                        seed: int = 0, clock=None, refresh: float = 60.0, fs: float = 250.0):
    """A synthetic board showing the standard's three codes (``vep.fake.SequenceFakeBoard``
    over ``cvep_3``): ``set_target(k)`` moves the fake gaze, ``start_flicker()`` starts the
    sequence, ``read_events()`` hands out its cycle events. ``clock=None`` is virtual time
    (``advance(s)``); ``time.time`` is real time."""
    from ..stimulus import load_config
    from ..stimulus.coded import CodeSequence
    from .fake import SequenceFakeBoard
    cfg = load_config(CONFIG)
    return SequenceFakeBoard(CodeSequence(cfg), fs=fs, refresh=refresh, fixated=fixated,
                             amp=amp, noise=noise, seed=seed, clock=clock)
