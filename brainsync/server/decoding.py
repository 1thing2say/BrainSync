"""Decoders for the server: one per active frequency set, over one shared sample history.

    bank = DecoderBank()                       # 250 Hz, the Cerelog montage, 0.14 s latency
    bank.calibrate_from(X_still)               # board rows (8, n) of a still screen
    bank.activate((15.0, 17.0), (0.0, 0.0), now=time.time())   # a new layout
    bank.set_onset(t_onset)                    # the client drew its first flickering frame
    for d in bank.feed(X, t):                  # every board read, always
        if d.changed and d.committed != ABSTAIN:
            ...                                # target d.committed of the active layout

Rules. A layout's targets decide which decoder runs: the bank keeps one decoder per
(frequencies, phases) pair and makes it the first time that pair is activated, fitting its
rest baseline on windows of the calibration data. Every board read goes into one raw
history, whether or not a decoder is active, so a new layout never waits for the buffer to
refill: activate() primes the decoder with the newest history samples, and the first
decision can come one window after the onset instead of window + pad after it.

Onset gating (PROTOCOL-v3 section 4, rule 2). A decision counts for a layout only when its
window started at or after the gate, which is the onset plus ``latency_s``. The window start
is taken as ``Decision.time - window_s``, one sample earlier than the true first sample, so
the rule errs on the late side. activate() sets the gate to ``now + latency_s`` and
set_onset() moves it to the client's onset. A decision before the gate is dropped and its
vote undone (the voter is reset) before the next window is scored: feed() hands the
decoder its samples one decision at a time, so a vote from before the onset never mixes with
one after it, however large the board read was. With the demo settings (1.5 s windows,
0.25 s hop, 4 votes) the first counting decision comes about 1.64 s after the onset and the
first commit about 0.75 s later.

rearm() resets only the voter (dwell to repeat, as brainsync.scroll explains): resetting
the decoder would empty its buffer and blind it for window + pad seconds.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from ..board import FS
from ..classify import make
from ..eog import GazeDecoder
from ..online import TUNED, Calibration, Decision, Decoder, RingBuffer, fit_rest
from ..recordings import MONTAGE, POSTERIOR, channel_indices
from ..templates import load_model
from .protocol import LATENCY_S

__all__ = ["DecoderBank", "build_decoder", "PAD_S", "PROBE_DIVISOR"]

PAD_S = 2.0            # filter start-up seconds dropped before each window (demo setting)
PROBE_DIVISOR = 25.0   # the probe decoder runs at fs / 25 Hz (10 Hz at 250 Hz)

DecoderFactory = Callable[[tuple[float, ...], tuple[float, ...]], Decoder]


def build_decoder(freqs: Sequence[float], phases: Sequence[float], fs: float = FS,
                  ch_names: Sequence[str] = MONTAGE, model: str | Path | None = None,
                  channels: Sequence[str | int] | None = POSTERIOR) -> Decoder:
    """The live demo's decoder: swipe_web.build_decoder's recipe (TUNED FBCCA, 3 harmonics,
    the POSTERIOR channels, a 2 s pad), written here so the server does not import the
    BrainSwipe page and its dependencies.

    With ``model`` (a file from ``brainsync.calibrate``) the classifier is the saved, trained
    one instead; its targets must be these ``freqs`` and ``phases`` (within 0.01 Hz) and its
    channel count must match ``channels``. The saved margin and threshold are replaced by the
    TUNED ones so the abstain rules stay the same as for FBCCA.
    """
    if model is None:
        clf = make(TUNED.classifier, freqs=freqs, fs=fs, harmonics=3, phases=phases,
                   margin=TUNED.margin, threshold=TUNED.threshold)
    else:
        clf = load_model(model)
        f, ph = np.asarray(freqs, float), np.asarray(phases, float)
        if (clf.freqs.size != f.size or np.any(np.abs(clf.freqs - f) > 0.01)
                or np.any(np.abs(np.angle(np.exp(1j * (clf.phases - ph)))) > 1e-6)):
            raise ValueError(f"model {model} was trained for {clf.freqs.tolist()} Hz, phases "
                             f"{clf.phases.tolist()}, not {f.tolist()} Hz, {ph.tolist()}")
        if abs(clf.fs - fs) > 1e-6:
            raise ValueError(f"model {model} was trained at {clf.fs} Hz, the board runs at {fs}")
        names = tuple(str(c) for c in ch_names)
        selected = [names[i] for i in channel_indices(names, channels)]
        if clf.n_ch != len(selected):
            raise ValueError(f"model {model} was trained on {clf.n_ch} channels, the decoder "
                             f"selects {len(selected)}")
        trained_on = clf.meta.get("channels") if isinstance(clf.meta, dict) else None
        if trained_on and list(trained_on) != selected:
            raise ValueError(f"model {model} was trained on channels {list(trained_on)}, the "
                             f"decoder selects {selected}")
        clf.margin, clf.threshold = TUNED.margin, TUNED.threshold
    return Decoder(clf, fs, window_s=TUNED.window_s, step_s=TUNED.step_s, votes=TUNED.votes,
                   channels=channels, pad_s=PAD_S, ch_names=ch_names)


def _key(freqs: Sequence[float], phases: Sequence[float], fs: float) -> tuple[tuple[float, ...],
                                                                             tuple[float, ...]]:
    try:
        f = tuple(float(v) for v in freqs)
        p = tuple(float(v) for v in phases)
    except (TypeError, ValueError):
        raise ValueError(f"freqs and phases must be sequences of numbers, got {freqs!r} and "
                         f"{phases!r}") from None
    if not f:
        raise ValueError("activate() needs at least one frequency; use deactivate() for a "
                         "layout without targets")
    if len(p) != len(f):
        raise ValueError(f"need one phase per frequency, got {len(f)} frequencies and "
                         f"{len(p)} phases")
    if not all(math.isfinite(v) and 0 < v < fs / 2 for v in f):
        raise ValueError(f"frequencies must lie in (0, {fs / 2:g}) Hz, got {list(f)}")
    if len(set(f)) != len(f):
        raise ValueError(f"frequencies must be unique (two targets on one frequency cannot "
                         f"be told apart), got {list(f)}")
    if not all(math.isfinite(v) for v in p):
        raise ValueError(f"phases must be finite numbers of radians, got {list(p)}")
    return f, p


class DecoderBank:
    """One decoder per active frequency set over one shared raw-sample history, with onset gating.

    ``factory(freqs, phases)`` builds a decoder; the default is ``build_decoder`` at ``fs``
    over ``ch_names``. Decoders must read board rows named ``ch_names`` at ``fs``. One probe
    decoder is built at construction (at fs / 25 Hz) to learn the window geometry: how much
    history to keep and how much calibration data is needed. All decoders a factory makes
    are assumed to share that geometry; a bigger one grows the history when it is made.

    ``gaze`` (brainsync.eog.GazeDecoder over the same ``ch_names``) adds an EOG prior: it
    is fed every sample, its centre is fitted on the calibration data, and while a layout
    is active with known target ``positions`` (activate), each decoder's ``prior`` adds the
    gaze scores to the SSVEP scores. A layout without positions runs on SSVEP alone.
    """

    def __init__(self, fs: float = FS, *, ch_names: Sequence[str] = MONTAGE,
                 latency_s: float = LATENCY_S,
                 factory: DecoderFactory | None = None,
                 model: str | Path | None = None,
                 gaze: GazeDecoder | None = None) -> None:
        fs = float(fs)
        if not math.isfinite(fs) or fs <= 0:
            raise ValueError(f"fs must be a positive number of Hz, got {fs}")
        latency_s = float(latency_s)
        if not math.isfinite(latency_s) or latency_s < 0:
            raise ValueError(f"latency_s must be >= 0 seconds, got {latency_s}")
        self.fs = fs
        self.ch_names = tuple(str(c) for c in ch_names)
        if not self.ch_names or len(set(self.ch_names)) != len(self.ch_names):
            raise ValueError(f"ch_names must be unique board channel names, got "
                             f"{list(self.ch_names)}")
        self.latency_s = latency_s
        self.model = None if model is None else Path(model)
        # the probe (a single off-target frequency) only learns the window geometry, so the
        # default factory applies the saved model to real layouts only
        if gaze is not None:
            if not isinstance(gaze, GazeDecoder):
                raise ValueError(f"gaze must be a brainsync.eog.GazeDecoder, got "
                                 f"{type(gaze).__name__}")
            if gaze.ch_names != self.ch_names:
                raise ValueError(f"the gaze decoder reads channels {list(gaze.ch_names)} but "
                                 f"the board has {list(self.ch_names)}")
            if abs(gaze.fs - fs) > 1e-6:
                raise ValueError(f"the gaze decoder runs at {gaze.fs:g} Hz but the bank at {fs:g} Hz")
        self.gaze = gaze
        self._gaze_on = False   # positions known for the active layout
        self._factory: DecoderFactory = factory or (
            lambda f, p: build_decoder(f, p, fs=self.fs, ch_names=self.ch_names,
                                       model=None if len(f) == 1 and f[0] == fs / PROBE_DIVISOR
                                       else self.model))
        self._decoders: dict[tuple[tuple[float, ...], tuple[float, ...]], Decoder] = {}
        self._probe = self._make((fs / PROBE_DIVISOR,), (0.0,))
        n = self._probe.needed
        self._hist = RingBuffer(len(self.ch_names), n)
        self._times = RingBuffer(1, n)
        self._calib: np.ndarray | None = None
        self.calibration: Calibration | None = None   # the probe's fit of the last data
        self._active: tuple[tuple[float, ...], tuple[float, ...]] | None = None
        self._gate = math.inf
        self.dropped = 0        # decisions dropped by the gate since the last activate()

    # -- properties ---------------------------------------------------------------------

    @property
    def calibrated(self) -> bool:
        return self._calib is not None

    @property
    def active(self) -> tuple[float, ...] | None:
        """The active decoder's frequencies, in target order, or None."""
        return None if self._active is None else self._active[0]

    @property
    def decoder(self) -> Decoder | None:
        """The active decoder, or None."""
        return None if self._active is None else self._decoders[self._active]

    @property
    def gate(self) -> float:
        """Earliest window start (unix s) that counts; inf while nothing is active."""
        return self._gate

    @property
    def votes(self) -> int:
        """Agreeing windows a commit takes (the active decoder's, else the probe's)."""
        dec = self.decoder
        return (dec if dec is not None else self._probe).votes

    @property
    def min_calibrate_s(self) -> float:
        """Shortest calibration: two windows plus the pad, as online.calibrate requires."""
        return max(d.needed + d.n_step for d in self._all()) / self.fs

    @property
    def n_history(self) -> int:
        """Raw samples held for priming."""
        return len(self._hist)

    def _all(self) -> list[Decoder]:
        return [self._probe, *self._decoders.values()]

    # -- decoders -----------------------------------------------------------------------

    def _make(self, freqs: tuple[float, ...], phases: tuple[float, ...]) -> Decoder:
        try:
            dec = self._factory(freqs, phases)
        except KeyError as exc:      # recordings.channel_indices: a channel the board lacks
            raise ValueError(f"the decoder's channels do not fit the board's {list(self.ch_names)}"
                             f" ({exc.args[0]}); pass matching ch_names or a factory") from None
        if not isinstance(dec, Decoder):
            raise ValueError(f"the decoder factory must return an online.Decoder, got "
                             f"{type(dec).__name__}")
        if abs(dec.fs - self.fs) > 1e-6:
            raise ValueError(f"the factory made a decoder at {dec.fs:g} Hz but the bank runs at "
                             f"{self.fs:g} Hz; build it with fs={self.fs:g}")
        if dec.ch_names != self.ch_names:
            raise ValueError(f"the factory made a decoder over channels {list(dec.ch_names)} "
                             f"but the board has {list(self.ch_names)}; pass ch_names")
        if dec.classifier.n_targets != len(freqs):
            raise ValueError(f"the factory made a decoder with {dec.classifier.n_targets} "
                             f"targets for {len(freqs)} frequencies")
        if self.gaze is not None:
            dec.prior = self._gaze_prior
        return dec

    def _gaze_prior(self) -> np.ndarray | None:
        """The gaze bonus for the active layout, or None (no positions, not ready, abstain)."""
        if self.gaze is None or not self._gaze_on:
            return None
        bonus = self.gaze.bonus()
        dec = self.decoder
        if bonus is None or dec is None or bonus.shape != (dec.classifier.n_targets,):
            return None
        return bonus

    def _grow(self, n: int) -> None:
        """Keep at least ``n`` history samples, carrying over what is buffered."""
        if n <= self._hist.capacity:
            return
        hist, times = RingBuffer(self._hist.n_rows, n), RingBuffer(1, n)
        k = len(self._hist)
        hist.append(self._hist.last(k))
        times.append(self._times.last(k))
        self._hist, self._times = hist, times

    def _fit(self, dec: Decoder, X: np.ndarray) -> Calibration:
        if X.shape[1] < dec.needed + dec.n_step:
            raise ValueError(f"calibration data holds {X.shape[1] / self.fs:.2f} s but the "
                             f"decoder needs >= {(dec.needed + dec.n_step) / self.fs:g} s "
                             f"(two {dec.window_s:g} s windows plus the {dec.pad_s:g} s pad); "
                             f"calibrate for longer")
        return fit_rest(dec, dec.windows(X))

    def calibrate_from(self, X: np.ndarray) -> None:
        """Fit every decoder's rest baseline on board-row still-screen data (n_ch, n).

        Decoders made later fit theirs on the same data. On a ValueError (too short, too
        noisy) every baseline stays as it was.
        """
        X = np.array(X, dtype=float)    # a copy: the caller may reuse its buffer
        if X.ndim != 2 or X.shape[0] != len(self.ch_names):
            raise ValueError(f"calibration data must be board rows ({len(self.ch_names)}, n), "
                             f"got shape {X.shape}")
        if not np.all(np.isfinite(X)):
            raise ValueError("calibration data contains NaN or infinity; check the board")
        saved = [(d.classifier.baseline_mean.copy(), d.classifier.baseline_spread,
                  d.classifier.has_baseline) for d in self._all()]
        try:
            probe = self._fit(self._probe, X)
            for dec in self._decoders.values():
                self._fit(dec, X)
        except ValueError:
            for dec, (mean, spread, has) in zip(self._all(), saved):
                dec.classifier.baseline_mean = mean
                dec.classifier.baseline_spread = spread
                dec.classifier.has_baseline = has
            raise
        if self.gaze is not None:
            self.gaze.fit_centre(X)     # raises ValueError when too short; baselines stay
        self._calib = X
        self.calibration = probe

    def activate(self, freqs: Sequence[float], phases: Sequence[float], now: float,
                 positions: Sequence[Sequence[float]] | None = None) -> None:
        """Run the decoder for these targets from ``now`` on (gate = now + latency_s).

        The decoder is made on first use (and fit on the calibration data), then reset and
        primed with the newest history; the priming decides nothing and leaves no votes.
        ``positions`` (one (x right, y up) per target, around the screen centre) turn the
        gaze prior on for this layout; without them it stays off.
        """
        key = _key(freqs, phases, self.fs)
        now = float(now)
        if not math.isfinite(now):
            raise ValueError(f"activate() needs a finite time, got {now}")
        if self._calib is None:
            raise ValueError("the bank is not calibrated; call calibrate_from() with "
                             "still-screen data before activate()")
        self._gaze_on = False
        if self.gaze is not None and positions is not None and len(key[0]) >= 2:
            if len(positions) != len(key[0]):
                raise ValueError(f"need one position per target, got {len(positions)} for "
                                 f"{len(key[0])} frequencies")
            self.gaze.set_targets(positions)
            self._gaze_on = True
        elif self.gaze is not None:
            self.gaze.clear_targets()    # one target, or no positions: SSVEP alone
        dec = self._decoders.get(key)
        if dec is None:
            dec = self._make(*key)
            self._fit(dec, self._calib)
            self._decoders[key] = dec
            self._grow(dec.needed)
        dec.reset()
        k = min(len(self._hist), dec.needed - 1)   # one sample short: priming scores nothing
        if k > 0:
            dec.feed(self._hist.last(k), self._times.last(k)[0])
        dec.voter.reset()
        dec.set_onset(now)           # refined by set_onset() when the client reports its frame
        self._active = key
        self._gate = now + self.latency_s
        self.dropped = 0

    def set_onset(self, t_onset: float) -> None:
        """The client's first flickering frame: gate = t_onset + latency_s.

        Moving the gate later also resets the voter, because votes already counted may come
        from windows that started before the new gate.
        """
        t_onset = float(t_onset)
        if not math.isfinite(t_onset):
            raise ValueError(f"onset time must be finite, got {t_onset}")
        dec = self.decoder
        if dec is None:
            raise ValueError("no decoder is active; call activate() for the layout first")
        gate = t_onset + self.latency_s
        if gate > self._gate:
            dec.voter.reset()
        self._gate = gate
        dec.set_onset(t_onset)       # trained classifiers lock their templates to this time

    def deactivate(self) -> None:
        """Stop deciding (a layout without targets, or not running); history keeps filling."""
        self._gaze_on = False
        if self.gaze is not None:
            self.gaze.clear_targets()
        self._active = None
        self._gate = math.inf

    def rearm(self) -> None:
        """Forget the active decoder's votes so a held gaze commits again (dwell repeat)."""
        dec = self.decoder
        if dec is not None:
            dec.voter.reset()

    # -- streaming ----------------------------------------------------------------------

    def feed(self, X: np.ndarray, t: np.ndarray) -> list[Decision]:
        """Add board samples (n_ch, n) with unix times (n,); the gated decisions, oldest first.

        The samples always go into the history. With a decoder active they are also fed to
        it one decision at a time, and a decision whose window starts before the gate is
        dropped with its vote undone.
        """
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[0] != len(self.ch_names):
            raise ValueError(f"expected board rows ({len(self.ch_names)}, n), got shape {X.shape}")
        t = np.asarray(t, dtype=float).reshape(-1)
        n = X.shape[1]
        if t.size != n:
            raise ValueError(f"got {n} samples but {t.size} timestamps")
        if n == 0:
            return []
        self._hist.append(X)
        self._times.append(t)
        dec = self.decoder
        if dec is None:
            if self.gaze is not None:
                self.gaze.feed(X)
            return []
        out: list[Decision] = []
        pos = 0
        while pos < n:
            seen = dec.n_seen
            due = (dec.needed - seen if seen < dec.needed
                   else dec.n_step - (seen - dec.needed) % dec.n_step)
            take = min(n - pos, due)
            if self.gaze is not None:    # the same samples first, so the prior sees this window
                self.gaze.feed(X[:, pos:pos + take])
            for d in dec.feed(X[:, pos:pos + take], t[pos:pos + take]):
                if d.time - dec.window_s < self._gate:
                    dec.voter.reset()
                    self.dropped += 1
                else:
                    out.append(d)
            pos += take
        return out
