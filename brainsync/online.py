"""Real-time SSVEP decoding: a sliding-window decoder over a live sample stream.

Every ``step_s`` the decoder takes the newest ``window_s + pad_s`` seconds, runs
``brainsync.signal.preprocess`` on them (the same chain the offline tools use), throws
away the first ``pad_s`` seconds (the filters' start-up transient at the old end) and
scores the rest with a CCA/FBCCA classifier. The zero-phase filter's transient at the
newest end stays inside the window, so online windows differ somewhat from windows cut
out of a whole preprocessed recording (``consistency`` measures by how much). The
classifier abstains on a small lead (margin) or a low level above rest (threshold). A
target is committed once ``votes`` consecutive windows name it; ``votes`` consecutive
abstentions withdraw the commitment.

    python -m brainsync.online <cued.npz>...          # replay recordings with the demo defaults
    python -m brainsync.online --grid <cued.npz>...   # small tuning grid
"""
from __future__ import annotations

import argparse
import itertools
import math
import sys
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .board import Board, record
from .classify import ABSTAIN, CCA, make, min_samples
from .recordings import (MONTAGE, POSTERIOR, Recording, channel_indices, load_recording,
                         pair_trials)
from .signal import Band, preprocess

__all__ = ["ABSTAIN", "TUNED", "Settings", "RingBuffer", "Decision", "Voter", "Decoder", "Calibration",
           "calibrate", "fit_rest", "replay", "replay_grid", "ReplayResult", "TrialOutcome", "consistency",
           "Idle", "idle"]

# Demo defaults from ``python -m brainsync.online --grid`` on 2026-10-22_162029 and
# 2026-10-22_172130. Window 1.5 s at a 0.25 s hop was the best region for FBCCA, and 4 votes
# at margin 0.5 made the fewest wrong and still-screen commits (2026-10-22_161131, cue not
# held, broke the tie). The margin alone abstained on only ~1/3 of still and eyes-closed
# windows, so the threshold (best target 0.5 spreads above rest) was added: on all three
# cued files it cut still-screen commits from 34 to 17 of 60, wrong commits from 10 to 2,
# and commits during the eyes-closed run 2026-10-22_151304 from ~27 to ~5 in 166 s, at the
# cost of correct commits (42 -> 30 of 60). Chosen on these same files, not held out.
@dataclass(frozen=True)
class Settings:
    classifier: str = "fbcca"
    window_s: float = 1.5
    step_s: float = 0.25
    votes: int = 4
    margin: float = 0.5
    threshold: float | None = 0.5


TUNED = Settings()

REJECT_FACTOR = 3.0   # calibration windows louder than this x median RMS are left out


class RingBuffer:
    """Fixed-capacity buffer of the newest samples of a (n_rows, n) stream."""

    def __init__(self, n_rows: int, capacity: int) -> None:
        if int(n_rows) < 1 or int(capacity) < 1:
            raise ValueError(f"need n_rows >= 1 and capacity >= 1, got {n_rows}, {capacity}")
        self.capacity = int(capacity)
        self._data = np.zeros((int(n_rows), self.capacity))
        self._head = 0       # next column to write
        self._filled = 0
        self.total = 0       # samples ever appended

    @property
    def n_rows(self) -> int:
        return self._data.shape[0]

    def __len__(self) -> int:
        return self._filled

    def clear(self) -> None:
        self._head = 0
        self._filled = 0
        self.total = 0

    def append(self, X: np.ndarray) -> None:
        """Add columns; a 1-D array is one row's samples."""
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X[np.newaxis]
        if X.ndim != 2 or X.shape[0] != self.n_rows:
            raise ValueError(f"expected ({self.n_rows}, n) samples, got shape {X.shape}")
        n = X.shape[1]
        if n == 0:
            return
        cap = self.capacity
        if n >= cap:
            self._data[:] = X[:, n - cap:]
            self._head = 0
        else:
            first = min(n, cap - self._head)
            self._data[:, self._head:self._head + first] = X[:, :first]
            self._data[:, :n - first] = X[:, first:]
            self._head = (self._head + n) % cap
        self._filled = min(cap, self._filled + n)
        self.total += n

    def last(self, n: int) -> np.ndarray:
        """Copy of the newest ``n`` samples, oldest first, shape (n_rows, n)."""
        n = int(n)
        if not 0 <= n <= self._filled:
            raise ValueError(f"asked for the last {n} samples but {self._filled} are buffered")
        start = (self._head - n) % self.capacity
        if start + n <= self.capacity:
            return self._data[:, start:start + n].copy()
        return np.concatenate([self._data[:, start:], self._data[:, :start + n - self.capacity]],
                              axis=1)


@dataclass
class Decision:
    """One scored window.

    ``time`` is the unix time of its newest sample, ``end`` the number of samples fed
    when it was scored (the window is samples end - n_window .. end - 1), ``scores`` the
    baseline-corrected target scores, ``raw`` this window's prediction (-1 abstains),
    ``committed`` the decoder's target after this window and ``changed`` whether this
    window moved it. ``gap`` is the best target's lead over the runner-up and ``level``
    its score above its rest mean, both in baseline spreads.
    """

    time: float
    scores: np.ndarray
    raw: int
    committed: int
    changed: bool
    end: int = 0
    gap: float = 0.0
    level: float = 0.0
    locked: bool = False   # scored with the templates locked to the flicker onset's phase


class Voter:
    """Commit a value once ``votes`` consecutive inputs agree on it (including ABSTAIN)."""

    def __init__(self, votes: int) -> None:
        if int(votes) != votes or votes < 1:
            raise ValueError(f"votes must be a positive integer, got {votes}")
        self.votes = int(votes)
        self._recent: deque[int] = deque(maxlen=self.votes)
        self.committed = ABSTAIN

    def reset(self) -> None:
        self._recent.clear()
        self.committed = ABSTAIN

    def push(self, raw: int) -> bool:
        """Record one prediction; True when it changed ``committed``."""
        self._recent.append(int(raw))
        if len(self._recent) < self.votes or len(set(self._recent)) != 1:
            return False
        if self._recent[0] == self.committed:
            return False
        self.committed = self._recent[0]
        return True


class Decoder:
    """Sliding-window decoder over board data (rows named by ``ch_names``).

    With ``lock`` the classifier (a trained brainsync.trca.TRCA) scores each window with
    its templates locked to the flicker's phase, once ``onset`` holds the unix time of
    the first flickering frame (the display's ``free_on`` marker); until then windows are
    scored phase-free and ``Decision.locked`` is False. The rest baseline of a locking
    decoder is fitted at random phases (fit_rest), what noise scores under a lock.
    """

    def __init__(self, classifier: CCA, fs: float, window_s: float = 4.0, step_s: float = 0.5,
                 votes: int = 3, channels: Sequence[str | int] | None = POSTERIOR,
                 pad_s: float = 2.0, band: Band | None = (1.0, 100.0), notch_hz: float = 60.0,
                 ch_names: Sequence[str] = MONTAGE, lock: bool = False) -> None:
        self.fs = float(fs)
        if not np.isfinite(self.fs) or self.fs <= 0:
            raise ValueError(f"fs must be positive, got {fs}")
        if abs(classifier.fs - self.fs) > 1e-6:
            raise ValueError(f"classifier runs at {classifier.fs} Hz but the decoder at {self.fs} Hz")
        if lock and not callable(getattr(classifier, "phase_offsets", None)):
            raise ValueError(f"lock needs a trained TRCA model, not {classifier.name}: "
                             f"only its templates have a phase to lock")
        self.lock = bool(lock)
        self.onset: float | None = None
        for name, value in (("window_s", window_s), ("step_s", step_s)):
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if not np.isfinite(pad_s) or pad_s < 0:
            raise ValueError(f"pad_s must be >= 0, got {pad_s}")
        self.classifier = classifier
        self.ch_names = tuple(str(c) for c in ch_names)
        self.rows = channel_indices(self.ch_names, channels)
        self.window_s, self.step_s, self.pad_s = float(window_s), float(step_s), float(pad_s)
        self.n_window = int(round(window_s * self.fs))
        self.n_pad = int(round(pad_s * self.fs))
        self.n_step = max(1, int(round(step_s * self.fs)))
        need = min_samples(len(self.rows), classifier.harmonics)
        if self.n_window < need:
            raise ValueError(f"window of {window_s} s ({self.n_window} samples) is too short for "
                             f"{len(self.rows)} channels (need >= {need} samples)")
        self.band = band
        self.notch_hz = float(notch_hz)
        self.voter = Voter(votes)
        self._data = RingBuffer(len(self.rows), self.needed)
        self._times = RingBuffer(1, self.needed)
        self.reset()

    @property
    def needed(self) -> int:
        """Samples that must be buffered before the first decision (window + pad)."""
        return self.n_window + self.n_pad

    @property
    def votes(self) -> int:
        return self.voter.votes

    @property
    def committed(self) -> int:
        return self.voter.committed

    @property
    def n_seen(self) -> int:
        """Samples fed since the last reset."""
        return self._data.total

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(self.ch_names[i] for i in self.rows)

    def reset(self) -> None:
        """Empty the buffer and forget votes; the classifier's baseline is kept."""
        self._data.clear()
        self._times.clear()
        self.voter.reset()
        self._next_due = self.needed

    # -- windows -----------------------------------------------------------------------

    def prepare(self, segment: np.ndarray) -> np.ndarray:
        """Preprocess one (n_sel_ch, window + pad) raw segment and drop the pad."""
        segment = np.asarray(segment, dtype=float)
        if segment.shape != (len(self.rows), self.needed):
            raise ValueError(f"segment must be ({len(self.rows)}, {self.needed}), got {segment.shape}")
        return preprocess(segment, self.fs, band=self.band, notch_hz=self.notch_hz)[:, self.n_pad:]

    def segment_starts(self, n: int, first: int = 0) -> np.ndarray:
        """Starts of the overlapping (hop n_step) window+pad segments within ``first..n``."""
        if n - first < self.needed:
            return np.empty(0, dtype=int)
        return np.arange(first, n - self.needed + 1, self.n_step)

    def windows(self, X: np.ndarray, starts: Iterable[int] | None = None) -> np.ndarray:
        """Overlapping windows (n_windows, n_sel_ch, n_window) from contiguous board data.

        Each is prepared exactly like an online window. ``starts`` gives segment starts
        (window + pad) in samples; default every n_step from 0.
        """
        X = self._board_rows(X)
        idx = self.segment_starts(X.shape[1]) if starts is None else np.asarray(list(starts), int)
        out = np.empty((idx.size, len(self.rows), self.n_window))
        for k, s in enumerate(idx):
            if s < 0 or s + self.needed > X.shape[1]:
                raise ValueError(f"segment start {s} leaves no room for {self.needed} samples "
                                 f"in {X.shape[1]}")
            out[k] = self.prepare(X[:, s:s + self.needed])
        return out

    def _board_rows(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[0] != len(self.ch_names):
            raise ValueError(f"expected board data ({len(self.ch_names)}, n), got shape {X.shape}")
        return X[self.rows]

    # -- streaming ---------------------------------------------------------------------

    def feed(self, X: np.ndarray, t: np.ndarray | None = None) -> list[Decision]:
        """Append board samples; returns the decisions that fell due, oldest first.

        A decision falls due once ``needed`` samples are in and then every ``n_step``
        samples, at the same stream positions however the data is chunked. ``t`` (unix
        seconds) defaults to sample count / fs.
        """
        X = self._board_rows(X)
        n = X.shape[1]
        if t is None:
            t = (self.n_seen + np.arange(n)) / self.fs
        t = np.asarray(t, dtype=float).reshape(-1)
        if t.size != n:
            raise ValueError(f"got {n} samples but {t.size} timestamps")
        out: list[Decision] = []
        pos = 0
        while pos < n:
            take = min(n - pos, self._next_due - self.n_seen)
            self._data.append(X[:, pos:pos + take])
            self._times.append(t[pos:pos + take])
            pos += take
            if self.n_seen == self._next_due:
                out.append(self._score())
                self._next_due += self.n_step
        return out

    def step(self, source: Board) -> list[Decision]:
        """Read whatever ``source`` has and feed it."""
        X, t = source.read()
        return self.feed(X, t)

    def decide(self) -> Decision | None:
        """Score the newest window now (outside the step schedule); None until ``needed``."""
        if len(self._data) < self.needed:
            return None
        return self._score()

    def window_start(self) -> float:
        """Unix time of the newest window's first sample (after the pad)."""
        return float(self._times.last(self.n_window)[0, 0])

    def _score(self) -> Decision:
        window = self.prepare(self._data.last(self.needed))
        locked = self.lock and self.onset is not None
        if locked:
            psi = self.classifier.phase_offsets(self.window_start() - self.onset)
            pred, scores = self.classifier.decide(window, psi)
        else:
            pred, scores = self.classifier.decide(window)
        gap = float(self.classifier.gaps(scores)[0])
        level = float(self.classifier.levels(scores)[0])
        raw = int(pred[0])
        changed = self.voter.push(raw)
        return Decision(time=float(self._times.last(1)[0, 0]), scores=scores[0], raw=raw,
                        committed=self.committed, changed=changed, end=self.n_seen, gap=gap,
                        level=level, locked=locked)


# -- calibration -----------------------------------------------------------------------

@dataclass
class Calibration:
    n_windows: int
    n_rejected: int
    mean: np.ndarray
    spread: float


def fit_rest(decoder: Decoder, windows: np.ndarray, reject: float = REJECT_FACTOR) -> Calibration:
    """Fit the classifier's baseline on prepared still-screen windows.

    Windows whose RMS exceeds ``reject`` x the median RMS (movement, blinks) are left
    out; ``reject`` 0 keeps all.
    """
    windows = np.asarray(windows, dtype=float)
    if windows.ndim != 3 or windows.shape[0] < 2:
        raise ValueError(f"need at least 2 still windows (n, ch, samples), got shape {windows.shape}")
    keep = np.ones(windows.shape[0], dtype=bool)
    if reject > 0:
        rms = np.sqrt(np.mean(windows ** 2, axis=(1, 2)))
        keep = rms <= reject * np.median(rms)
    if keep.sum() < 2:
        raise ValueError(f"only {int(keep.sum())} of {windows.shape[0]} still windows passed "
                         f"artifact rejection (factor {reject})")
    clf = decoder.classifier
    if decoder.lock:
        clf.fit_baseline(windows[keep], locked=True)
    else:
        clf.fit_baseline(windows[keep])
    return Calibration(int(keep.sum()), int((~keep).sum()), clf.baseline_mean.copy(),
                       float(clf.baseline_spread))


def calibrate(decoder: Decoder, source: Board, seconds: float = 10.0,
              sleep: Callable[[float], None] | None = None, stall_s: float = 5.0,
              reject: float = REJECT_FACTOR) -> Calibration:
    """Record ``seconds`` of still-screen data and fit the baseline on overlapping windows.

    Windows hop by the decoder's step and are prepared exactly like online windows, so
    the baseline matches what the decoder will score. A clock-less FakeBoard runs in
    virtual time. The decoder's buffer is left untouched.
    """
    shortest = (decoder.needed + decoder.n_step) / decoder.fs
    if seconds < shortest:
        raise ValueError(f"calibration needs >= {shortest:g} s for two {decoder.window_s:g} s "
                         f"windows plus {decoder.pad_s:g} s pad, got {seconds}")
    X, _ = record(source, seconds, sleep=sleep, stall_s=stall_s)
    if X.shape[1] < decoder.needed + decoder.n_step:
        raise ValueError(f"calibration got only {X.shape[1] / decoder.fs:.1f} s of data, "
                         f"need >= {shortest:g} s")
    return fit_rest(decoder, decoder.windows(X), reject)


# -- replay of a recorded cued session -------------------------------------------------

@dataclass
class TrialOutcome:
    """A trial as the decoder saw it; ``committed`` is the first target committed from
    windows ending inside the flicker (-1: none), ``delay_s`` its time after stim_on."""

    target: int
    committed: int
    delay_s: float
    shown: int            # decoder's committed target at the trial's last window
    n_windows: int
    n_abstain: int
    window_hits: int      # windows fully inside the flicker that named the target
    n_inside: int


@dataclass
class ReplayResult:
    path: Path | None
    params: dict[str, object]
    trials: list[TrialOutcome]
    decisions: list[Decision] = field(repr=False)
    calibration: Calibration
    still_windows: int
    still_abstain: int
    still_commits: int    # still screens on which a target got committed
    n_still: int
    decoder: Decoder = field(repr=False)
    recording: Recording = field(repr=False)
    spans: list[tuple[int, int, int]] = field(repr=False, default_factory=list)

    @property
    def n_trials(self) -> int:
        return len(self.trials)

    @property
    def correct(self) -> int:
        return sum(o.committed == o.target for o in self.trials)

    @property
    def wrong(self) -> int:
        return sum(o.committed not in (ABSTAIN, o.target) for o in self.trials)

    @property
    def commit_accuracy(self) -> float:
        return self.correct / self.n_trials if self.trials else math.nan

    @property
    def wrong_rate(self) -> float:
        return self.wrong / self.n_trials if self.trials else math.nan

    @property
    def median_delay_s(self) -> float:
        d = [o.delay_s for o in self.trials if o.committed == o.target]
        return float(np.median(d)) if d else math.nan

    @property
    def abstain_rate(self) -> float:
        n = sum(o.n_windows for o in self.trials)
        return sum(o.n_abstain for o in self.trials) / n if n else math.nan

    @property
    def window_accuracy(self) -> float:
        n = sum(o.n_inside for o in self.trials)
        return sum(o.window_hits for o in self.trials) / n if n else math.nan

    def summary(self) -> str:
        p = self.params
        return (f"{p['classifier']} window {p['window_s']:g} s step {p['step_s']:g} s "
                f"votes {p['votes']} margin {p['margin']:g} threshold "
                f"{_thr(p.get('threshold'))}: correct {self.correct}/"
                f"{self.n_trials} wrong {self.wrong} none {self.n_trials - self.correct - self.wrong}"
                f", median {self.median_delay_s:.2f} s to a correct commit, window abstain "
                f"{_pct(self.abstain_rate)}, still commits {self.still_commits}/{self.n_still}")

    def report(self) -> list[str]:
        name = self.path.name if self.path else "recording"
        rec = self.recording
        labels = [f"{f:g}" for f in rec.freqs]
        lines = [f"{name}: {self.n_trials} trials, targets {', '.join(labels)} Hz",
                 f"  {self.summary()}",
                 f"  baseline from {self.calibration.n_windows} still windows "
                 f"({self.calibration.n_rejected} rejected): mean "
                 + " ".join(f"{m:.4f}" for m in self.calibration.mean)
                 + f", spread {self.calibration.spread:.4f}",
                 f"  windows inside flicker naming the target: {_pct(self.window_accuracy)}; "
                 f"still windows abstaining: {self.still_abstain}/{self.still_windows}"]
        for i, o in enumerate(self.trials):
            got = "none" if o.committed == ABSTAIN else labels[o.committed]
            mark = "ok" if o.committed == o.target else ("--" if o.committed == ABSTAIN else "XX")
            when = f"{o.delay_s:4.2f} s" if o.committed != ABSTAIN else "   -  "
            shown = "none" if o.shown == ABSTAIN else labels[o.shown]
            lines.append(f"  trial {i:2d}  target {labels[o.target]:>4}  committed {got:>4} {mark}"
                         f"  at {when}  shown at end {shown:>4}  abstained {o.n_abstain}/"
                         f"{o.n_windows}")
        return lines


def _pct(x: float) -> str:
    return "n/a" if not np.isfinite(x) else f"{100 * x:.0f}%"


def _thr(threshold: object) -> str:
    return "none" if threshold is None else f"{threshold:g}"


def _trial_spans(rec: Recording, latency: float) -> list[tuple[int, int, int]]:
    """(first, end, target) sample spans of each flicker response, stim_on/off + latency.

    Uses the same pairing and the same completeness rule as recordings.cut_windows (the
    flicker and still windows of rec.window_s must both lie inside the data), so replay
    counts the same trials as score_cued.
    """
    tol = 0.01 / rec.fs
    n_total = rec.X.shape[1]
    n_win = int(round(rec.window_s * rec.fs))
    spans = []
    for on, off, target in pair_trials(rec.markers):
        if on + latency < rec.t[0] - tol:
            continue
        a = int(np.searchsorted(rec.t, on + latency - tol))
        b = int(np.searchsorted(rec.t, off + latency - tol))
        if b <= a or a + n_win > n_total or b + n_win > n_total:
            continue
        if not 0 <= target < rec.n_targets:
            raise ValueError(f"{rec.path}: marker target {target} but freqs {rec.freqs.tolist()}")
        spans.append((a, b, target))
    return spans


def _still_spans(spans: list[tuple[int, int, int]], n: int) -> list[tuple[int, int]]:
    """Sample spans from each flicker response's end to the next one's start."""
    out = []
    for k, (_, end, _) in enumerate(spans):
        nxt = spans[k + 1][0] if k + 1 < len(spans) else n
        if nxt > end:
            out.append((end, nxt))
    return out


def default_decoder(rec: Recording, classifier: str = TUNED.classifier,
                    window_s: float = TUNED.window_s, step_s: float = TUNED.step_s,
                    votes: int = TUNED.votes, margin: float = TUNED.margin,
                    channels: Sequence[str | int] | None = POSTERIOR, pad_s: float = 2.0,
                    harmonics: int = 3, threshold: float | None = TUNED.threshold) -> Decoder:
    """Decoder for a recording's targets, sampling rate, montage and preprocessing."""
    clf = make(classifier, freqs=rec.freqs, fs=rec.fs, harmonics=harmonics, phases=rec.phases,
               margin=margin, threshold=threshold)
    return Decoder(clf, rec.fs, window_s=window_s, step_s=step_s, votes=votes, channels=channels,
                   pad_s=pad_s, band=rec.band, notch_hz=rec.notch, ch_names=rec.ch_names)


def _stream(rec: Recording, decoder: Decoder, chunk_s: float, lat: float,
            reject: float) -> tuple[list, list, Calibration, list[Decision]]:
    """Calibrate on the still screens, then feed the recording in chunks."""
    if chunk_s <= 0:
        raise ValueError(f"chunk_s must be positive, got {chunk_s}")
    spans = _trial_spans(rec, lat)
    if not spans:
        raise ValueError(f"{rec.path}: no complete stim_on/stim_off trials")
    n_total = rec.X.shape[1]
    still = _still_spans(spans, n_total)
    # baseline: windows (after the pad) wholly on a still screen
    starts = [s for a, b in still for s in decoder.segment_starts(b, max(0, a - decoder.n_pad))
              if s + decoder.n_pad >= a]
    calib = fit_rest(decoder, decoder.windows(rec.X, starts), reject)
    decoder.reset()
    chunk = max(1, int(round(chunk_s * rec.fs)))
    decisions: list[Decision] = []
    for a in range(0, n_total, chunk):
        decisions.extend(decoder.feed(rec.X[:, a:a + chunk], rec.t[a:a + chunk]))
    return spans, still, calib, decisions


def _relabel(decisions: list[Decision], margin: float, votes: int,
             threshold: float | None = None) -> list[Decision]:
    """The decisions a decoder with this margin, threshold and vote count would have made."""
    voter = Voter(votes)
    out = []
    for d in decisions:
        keep = (margin <= 0 or d.gap >= margin) and (threshold is None or d.level >= threshold)
        raw = int(np.argmax(d.scores)) if keep else ABSTAIN
        changed = voter.push(raw)
        out.append(Decision(d.time, d.scores, raw, voter.committed, changed, d.end, d.gap,
                            d.level, d.locked))
    return out


def _first_commit(decisions: Iterable[Decision], votes: int) -> Decision | None:
    """First window at which ``votes`` consecutive windows (from scratch) agree on a target."""
    voter = Voter(votes)
    for d in decisions:
        if voter.push(d.raw) and voter.committed != ABSTAIN:
            return d
    return None


def _evaluate(rec: Recording, decoder: Decoder, lat: float, spans: list, still: list,
              calib: Calibration, decisions: list[Decision], votes: int,
              margin: float, threshold: float | None) -> ReplayResult:
    ends = np.array([d.end for d in decisions])
    n_win = decoder.n_window
    trials = []
    for a, b, target in spans:
        inside = [decisions[k] for k in np.flatnonzero((ends > a) & (ends <= b))]
        first = _first_commit(inside, votes)
        full = [d for d in inside if d.end - n_win >= a]
        trials.append(TrialOutcome(
            target=target, committed=ABSTAIN if first is None else first.raw,
            delay_s=math.nan if first is None else first.time - (rec.t[a] - lat),
            shown=inside[-1].committed if inside else ABSTAIN, n_windows=len(inside),
            n_abstain=sum(d.raw == ABSTAIN for d in inside),
            window_hits=sum(d.raw == target for d in full), n_inside=len(full)))
    still_windows = still_abstain = still_commits = 0
    for a, b in still:
        on_still = [decisions[k] for k in np.flatnonzero((ends - n_win >= a) & (ends <= b))]
        still_windows += len(on_still)
        still_abstain += sum(d.raw == ABSTAIN for d in on_still)
        still_commits += _first_commit(on_still, votes) is not None
    used = {"classifier": decoder.classifier.name, "window_s": decoder.window_s,
            "step_s": decoder.step_s, "votes": votes, "margin": margin,
            "threshold": threshold, "pad_s": decoder.pad_s}
    return ReplayResult(path=rec.path, params=used, trials=trials, decisions=decisions,
                        calibration=calib, still_windows=still_windows,
                        still_abstain=still_abstain, still_commits=still_commits,
                        n_still=len(still), decoder=decoder, recording=rec, spans=spans)


def replay(path: str | Path | Recording,
           decoder_factory: Callable[[Recording], Decoder] | None = None,
           chunk_s: float = 0.1, latency: float | None = None, reject: float = REJECT_FACTOR,
           **params: object) -> ReplayResult:
    """Run a saved cued recording through the decoder as if it were live.

    The baseline comes from overlapping windows lying on the recording's still screens
    (from stim_off + latency to the next stim_on + latency). The data is then fed in
    ``chunk_s`` chunks. A trial's windows are those ending inside its flicker response;
    its commit is the first target ``votes`` consecutive such windows agree on, so a
    commitment left over from the previous trial never counts. ``params`` go to
    ``default_decoder`` when no factory is given.
    """
    rec = path if isinstance(path, Recording) else load_recording(path)
    decoder = (decoder_factory(rec) if decoder_factory is not None
               else default_decoder(rec, **params))  # type: ignore[arg-type]
    lat = rec.latency if latency is None else float(latency)
    spans, still, calib, decisions = _stream(rec, decoder, chunk_s, lat, reject)
    return _evaluate(rec, decoder, lat, spans, still, calib, decisions, decoder.votes,
                     decoder.classifier.margin, decoder.classifier.threshold)


def replay_grid(path: str | Path | Recording, votes: Sequence[int], margins: Sequence[float],
                thresholds: Sequence[float | None] = (None,), chunk_s: float = 0.1,
                latency: float | None = None, reject: float = REJECT_FACTOR,
                **params: object) -> dict[tuple[int, float, float | None], ReplayResult]:
    """replay() for every (votes, margin, threshold), streaming the recording only once.

    Scores, leads and levels do not depend on these settings, so the decisions are
    relabelled instead of recomputed. ``consistency`` needs a real replay(), not these.
    """
    rec = path if isinstance(path, Recording) else load_recording(path)
    decoder = default_decoder(rec, **{**params, "margin": 0.0,  # type: ignore[arg-type]
                                      "threshold": None})
    lat = rec.latency if latency is None else float(latency)
    spans, still, calib, decisions = _stream(rec, decoder, chunk_s, lat, reject)
    return {(v, m, t): _evaluate(rec, decoder, lat, spans, still, calib,
                                 _relabel(decisions, m, v, t), v, m, t)
            for v in votes for m in margins for t in thresholds}


@dataclass
class Idle:
    """How a calibrated decoder behaves on data with no attended target."""

    n_windows: int
    n_abstain: int
    commits: int          # changes of the committed target to a target (not to none)
    seconds: float

    @property
    def abstain_rate(self) -> float:
        return self.n_abstain / self.n_windows if self.n_windows else math.nan

    def summary(self) -> str:
        return (f"{self.n_windows} windows over {self.seconds:.0f} s: abstained on "
                f"{_pct(self.abstain_rate)}, committed a target {self.commits} times")


def idle(decoder: Decoder, X: np.ndarray, t: np.ndarray | None = None) -> Idle:
    """Stream target-free board data (still screen, eyes closed) through ``decoder``.

    The decoder keeps its baseline and is reset before and after.
    """
    decoder.reset()
    try:
        ds = decoder.feed(X, t)
    finally:
        decoder.reset()
    commits = sum(d.changed and d.committed != ABSTAIN for d in ds)
    return Idle(len(ds), sum(d.raw == ABSTAIN for d in ds), commits,
                len(ds) * decoder.n_step / decoder.fs)


def consistency(result: ReplayResult) -> dict[str, tuple[int, float]]:
    """Agreement of the decoder's window predictions with offline classification.

    Only windows lying wholly inside a flicker response count. "same" re-cuts each
    window+pad segment from the raw recording and prepares it the decoder's way, so it
    only checks that streaming and chunking change nothing (it is 100% by construction
    and says nothing about prepare() itself). "whole" preprocesses the whole recording
    first (as score_cued and cut_windows do) and cuts the window from that: the real
    online-vs-offline comparison. They differ because zero-phase filtering of a live
    segment has an edge transient at its newest end, which no pad can remove.
    Returns {mode: (n_windows, fraction agreeing)}.
    """
    dec, rec = result.decoder, result.recording
    picked = [d for d in result.decisions
              if any(a <= d.end - dec.n_window and d.end <= b for a, b, _ in result.spans)]
    if not picked:
        return {"same": (0, math.nan), "whole": (0, math.nan)}
    raw = np.array([d.raw for d in picked])
    same = dec.windows(rec.X, [d.end - dec.needed for d in picked])
    Y = preprocess(rec.X[dec.rows], rec.fs, band=dec.band, notch_hz=dec.notch_hz)
    whole = np.stack([Y[:, d.end - dec.n_window:d.end] for d in picked])
    clf = dec.classifier
    return {"same": (len(picked), float(np.mean(clf.predict(same) == raw))),
            "whole": (len(picked), float(np.mean(clf.predict(whole) == raw)))}


# -- CLI -------------------------------------------------------------------------------

GRID = {"classifier": ("cca", "fbcca"), "window_s": (1.0, 1.5, 2.0, 3.0),
        "step_s": (0.25, 0.5), "votes": (2, 3, 4), "margin": (0.0, 0.5, 1.0, 1.5),
        "threshold": (None, 0.5, 1.0)}


def _floats(text: str) -> list[float]:
    try:
        return [float(v) for v in text.split(",") if v.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated numbers, got {text!r}") from None


def threshold_arg(text: str) -> float | None:
    """A --threshold value: a number of rest spreads, or 'none' for no level rule."""
    if text.strip().lower() == "none":
        return None
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number or 'none', got {text!r}") from None
    if not np.isfinite(value):
        raise argparse.ArgumentTypeError(f"threshold must be finite, got {text!r}")
    return value


def channels_arg(text: str) -> list[str | int]:
    """Comma-separated channel names or board row indices."""
    out: list[str | int] = [int(c) if c.isdigit() else c
                            for c in (part.strip() for part in text.split(",")) if c]
    if not out:
        raise argparse.ArgumentTypeError(f"expected comma-separated channels, got {text!r}")
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.online", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="+", type=Path, help="*_cued.npz recordings")
    p.add_argument("--classifier", default=TUNED.classifier, choices=("cca", "fbcca"))
    p.add_argument("--window", type=float, default=TUNED.window_s, help="window seconds")
    p.add_argument("--step", type=float, default=TUNED.step_s, help="hop seconds")
    p.add_argument("--votes", type=int, default=TUNED.votes, help="agreeing windows to commit")
    p.add_argument("--margin", type=float, default=TUNED.margin,
                   help="abstain unless the best target leads by this many rest spreads")
    p.add_argument("--threshold", type=threshold_arg, default=TUNED.threshold,
                   help="abstain unless the best target is this many rest spreads above its "
                        f"rest level, or 'none' (default {_thr(TUNED.threshold)})")
    p.add_argument("--channels", type=channels_arg, default=list(POSTERIOR),
                   help="analysis channels, names or board indices (default "
                        + ",".join(POSTERIOR) + ")")
    p.add_argument("--trials", action="store_true", help="print every trial")
    p.add_argument("--grid", action="store_true",
                   help="score the tuning grid (classifier x window x step x votes x margin)")
    p.add_argument("--windows", type=_floats, help="grid window lengths, comma-separated")
    p.add_argument("--idle", type=Path, action="append", default=[],
                   help="target-free recording (still screen, eyes closed) to run each "
                        "replayed decoder on; repeatable")
    return p


def _grid(paths: list[Path], channels: list[str | int], windows: Sequence[float]) -> int:
    """Print one line per setting: per recording correct/wrong commits, median delay and
    still-screen commits, then the totals."""
    rows: dict[tuple, list[ReplayResult]] = {}
    outer = list(itertools.product(GRID["classifier"], windows, GRID["step_s"]))
    for k, (clf, win, step) in enumerate(outer):
        print(f"  {k + 1}/{len(outer)}: {clf} window {win:g} s step {step:g} s", file=sys.stderr)
        for p in paths:
            found = replay_grid(p, GRID["votes"], GRID["margin"], GRID["threshold"],
                                classifier=clf, window_s=win, step_s=step, channels=channels)
            for (votes, margin, thr), res in found.items():
                rows.setdefault((clf, win, step, votes, margin, thr), []).append(res)
    head = "  ".join(f"{p.stem[:17]:>29}" for p in paths)
    print(f"{'method':6} {'win':>4} {'step':>5} {'votes':>5} {'margin':>6} {'thresh':>6}  "
          f"{head}  {'total':>9}")
    print(f"{'':38}" + "  ".join(f"{'ok/n wrong delay still':>29}" for _ in paths))
    for (clf, win, step, votes, margin, thr), res in rows.items():
        cells = "  ".join(f"{r.correct:2d}/{r.n_trials:<2d} {r.wrong:5d} {r.median_delay_s:5.2f}s "
                          f"{r.still_commits:2d}/{r.n_still:<2d}" for r in res)
        good = sum(r.correct for r in res)
        bad = sum(r.wrong for r in res)
        print(f"{clf:6} {win:4g} {step:5g} {votes:5d} {margin:6g} {_thr(thr):>6}  {cells}  "
              f"{good:3d} ok {bad:2d} xx")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    channels = args.channels
    missing = [p for p in [*args.paths, *args.idle] if not p.is_file()]
    if missing:
        print(f"error: recording not found: {missing[0]}", file=sys.stderr)
        return 1
    try:
        if args.grid:
            return _grid(list(args.paths), channels, args.windows or GRID["window_s"])
        for p in args.paths:
            print(f"replaying {p}", file=sys.stderr)
            res = replay(p, classifier=args.classifier, window_s=args.window, step_s=args.step,
                         votes=args.votes, margin=args.margin, threshold=args.threshold,
                         channels=channels)
            lines = res.report()
            print("\n".join(lines if args.trials else lines[:4]))
            agree = consistency(res)
            print(f"  offline agreement on {agree['same'][0]} windows inside flicker: "
                  f"whole-recording preprocessing (as score_cued) {_pct(agree['whole'][1])}, "
                  f"self-check {_pct(agree['same'][1])}")
            for q in args.idle:
                rest = load_recording(q)
                if tuple(rest.ch_names) != res.decoder.ch_names or rest.fs != res.decoder.fs:
                    raise ValueError(f"{q.name}: montage or rate differs from {p.name}")
                print(f"  idle {q.name}: {idle(res.decoder, rest.X, rest.t).summary()}")
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
