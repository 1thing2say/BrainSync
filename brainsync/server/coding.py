"""The c-VEP decoder for the server (``--paradigm code``, docs/CVEP-STANDARD.md).

    bank = CodeBank(fs=250.0)                  # 3 targets, 21-frame lags, 30 training cycles
    bank.activate(now)                         # a layout starts flickering (train or run)
    bank.add_cycle(t_board)                    # each cycle's first flip, on the board's clock
    for d in bank.feed(X, t):                  # every board read, always
        ...                                    # none while training; then one per cycle
    bank.trained, bank.accuracy, bank.progress # the template and its held-out accuracy

The bank wraps one ``brainsync.vep.live.CodeVepDecoder``: the first ``train_cycles``
complete cycles (recorded while the user looks at target 0) become the template, and from
then on every complete cycle gives a decision from the mean of the last two, with the
target committed after two agreeing decisions. Target k is the copy of the m-sequence
delayed by ``k * lag_frames`` frames, so a layout target with ``lag`` 21 is decoder target 1.

Rules.
- Cycle times count only while the bank is active (a layout flickers, phase train or run);
  ``activate`` drops the cycles of the previous layout and the votes, and ``deactivate``
  stops counting. A cycle earlier than the activation (less a small slack for the clock
  offset) belongs to an older layout and is dropped.
- Every board read goes into a short raw history, whether or not the bank is active. A new
  layout restarts the decoder's sample buffer (the decoder forgets old epochs), and the
  history then primes it, so the first cycle of the layout already has the filter's pad of
  samples before it. While training, samples go to the decoder only while active, so a
  long wait never piles up in the training buffer.
- ``retrain`` forgets the template and starts a fresh training (a new session or a pause
  during training). A training whose cycles could not be cut (cycle times off the board's
  clock) is restarted, and ``train_error`` says why.
- ``rearm`` resets the votes only (dwell to repeat), as DecoderBank.rearm does.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from ..board import FS
from ..online import Decision, RingBuffer
from ..recordings import MONTAGE
from ..vep.cvep import Template
from ..vep.live import BITS, CYCLE_FRAMES, LAG_FRAMES, N_TARGETS, TRAIN_CYCLES, CodeVepDecoder

__all__ = ["CodeBank", "CYCLE_SLACK_S", "HISTORY_S", "POINT_KEYS", "lag_index"]

HISTORY_S = 2.0        # raw seconds kept to prime the decoder after a reset
CYCLE_SLACK_S = 0.25   # a cycle this much before the activation still counts (offset error)
# Point mode in code mode: PointMode (brainsync.screen) places its K = 3 targets on "freqs";
# the server hands it these stand-ins and turns freqs POINT_KEYS[k] into lag k * LAG_FRAMES.
POINT_KEYS = (1.0, 2.0, 3.0)


def lag_index(lag: int, lag_frames: int = LAG_FRAMES, n_targets: int = N_TARGETS) -> int | None:
    """The decoder target of a layout target's ``lag``, or None when it is no target's lag."""
    if isinstance(lag, bool) or not isinstance(lag, (int, np.integer)):
        return None
    k, rest = divmod(int(lag), int(lag_frames))
    return k if rest == 0 and 0 <= k < n_targets else None


class CodeBank:
    """The c-VEP decoder, its training and the cycle times of the active layout."""

    paradigm = "code"

    def __init__(self, fs: float = FS, *, ch_names: Sequence[str] = MONTAGE,
                 refresh: float = 60.0, train_cycles: int = TRAIN_CYCLES,
                 template: Template | None = None) -> None:
        fs = float(fs)
        if not math.isfinite(fs) or fs <= 0:
            raise ValueError(f"fs must be a positive number of Hz, got {fs}")
        self.fs = fs
        self.ch_names = tuple(str(c) for c in ch_names)
        if not self.ch_names or len(set(self.ch_names)) != len(self.ch_names):
            raise ValueError(f"ch_names must be unique board channel names, got "
                             f"{list(self.ch_names)}")
        self.refresh = float(refresh)
        self.decoder = CodeVepDecoder(fs, refresh=self.refresh, ch_names=self.ch_names,
                                      train_cycles=train_cycles, template=template)
        self.bits = tuple(int(b) for b in BITS)
        self.n_targets = self.decoder.n_targets
        self.lag_frames = self.decoder.lag_frames
        self.cycle_frames = self.decoder.cycle_frames
        self.train_cycles = self.decoder.train_cycles
        n = max(2, int(round(HISTORY_S * fs)))
        self._hist = RingBuffer(len(self.ch_names), n)
        self._times = RingBuffer(1, n)
        self._pending: list[float] = []
        self._active = False
        self._activated_at = -math.inf
        self._fed_training = False      # the training buffer holds data (primed or fed)
        self.train_error = ""
        self.dropped = 0                # cycles dropped as older than the activation

    # -- state --------------------------------------------------------------------------

    @property
    def training(self) -> bool:
        return self.decoder.training

    @property
    def trained(self) -> bool:
        return not self.decoder.training

    @property
    def progress(self) -> tuple[int, int]:
        """(training cycles recorded, cycles needed)."""
        return self.decoder.progress

    @property
    def accuracy(self) -> float | None:
        """The template's leave-one-cycle-out accuracy, or None (not trained, or a template
        that was given rather than trained)."""
        a = self.decoder.train_accuracy
        return None if not math.isfinite(a) else float(a)

    @property
    def chance(self) -> float:
        return self.decoder.chance

    @property
    def active(self) -> bool:
        return self._active

    @property
    def votes(self) -> int:
        return self.decoder.votes

    @property
    def code(self) -> dict:
        """The fields of the welcome's code object (protocol.code_spec takes them)."""
        return {"bits": list(self.bits), "lag_frames": self.lag_frames,
                "cycle_frames": self.cycle_frames, "train_cycles": self.train_cycles}

    # -- control ------------------------------------------------------------------------

    def activate(self, now: float) -> None:
        """A layout starts flickering at ``now``: count its cycles, with fresh votes."""
        now = float(now)
        if not math.isfinite(now):
            raise ValueError(f"activate() needs a finite time, got {now}")
        self._pending = []
        self._active = True
        self._activated_at = now
        self.dropped = 0
        self.decoder.reset()            # trained: forget epochs, samples and votes
        if self.trained:
            self._prime()
        elif not self._fed_training:
            self._prime()

    def deactivate(self) -> None:
        """Nothing flickers: stop counting cycles (the history keeps filling)."""
        self._active = False
        self._pending = []

    def retrain(self) -> None:
        """Forget the template and any training so far; the next active cycles train again."""
        self.decoder.retrain()
        self._fed_training = False
        self._pending = []
        if self._active:
            self._prime()

    def use_template(self, template: Template) -> None:
        """Decode with ``template`` from now on (a saved one), skipping training."""
        self.decoder.use_template(template)
        self._fed_training = False
        if self._active:
            self._prime()

    def rearm(self) -> None:
        """Forget the votes so a held gaze commits again (dwell repeat)."""
        dec = self.decoder.decoder
        if dec is not None:
            dec.voter.reset()

    def add_cycle(self, t: float) -> bool:
        """A cycle's first flip at board time ``t``; False when it was not counted."""
        t = float(t)
        if not self._active or not math.isfinite(t):
            return False
        if t < self._activated_at - CYCLE_SLACK_S:
            self.dropped += 1
            return False
        self._pending.append(t)
        return True

    def _prime(self) -> None:
        k = len(self._hist)
        if k == 0:
            return
        X, t = self._hist.last(k), self._times.last(k)[0]
        self.decoder.feed(X, t, ())
        if self.training:
            self._fed_training = True

    # -- streaming ----------------------------------------------------------------------

    def feed(self, X: np.ndarray, t: np.ndarray) -> list[Decision]:
        """Add board samples (n_ch, n) with unix times (n,); the decisions made, oldest first.

        While training no decision comes; the read that completes the training returns none
        either, and ``trained`` is true after it.
        """
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[0] != len(self.ch_names):
            raise ValueError(f"expected board rows ({len(self.ch_names)}, n), got shape {X.shape}")
        t = np.asarray(t, dtype=float).reshape(-1)
        if t.size != X.shape[1]:
            raise ValueError(f"got {X.shape[1]} samples but {t.size} timestamps")
        if t.size:
            self._hist.append(X)
            self._times.append(t[None, :])
        if not self._active:
            return []
        events = [(c, "cycle", 0) for c in self._pending]
        self._pending = []
        was_training = self.training
        try:
            out = self.decoder.feed(X, t, events)
        except RuntimeError as exc:     # training cycles could not be cut
            self.train_error = str(exc)
            self.retrain()
            return []
        if was_training:
            self._fed_training = True
            if self.trained:            # this read finished the training
                self.train_error = ""
                self._prime()
        return list(out)
