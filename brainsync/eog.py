"""Gaze direction from the electro-oculogram (EOG), as a prior for the SSVEP decoder.

The eye is a dipole (the cornea positive), so two electrodes at the outer canthi see the
horizontal gaze angle as a near-DC voltage of roughly 15-20 µV per degree, and the
forehead electrodes against a mastoid reference see the vertical angle the same way. On
the Cerelog X8 the C3/C4 pins never reach the scalp, so their leads go to gel pads at
the left and right temple instead: channels ``LO`` and ``RO`` (``EOG_MONTAGE``).

A window's gaze is the mean of the 10 Hz low-passed EOG channels minus the centre
level (fitted on a still screen the user looks at the middle of): a vector (h, v) in µV,
x to the right and y up. Each target is a direction from the centre, either from the
layout's positions (``set_targets``) or measured on labelled trials (``fit``). A target
scores the cosine between the gaze vector and its direction, scaled by how far the gaze
is from the centre, so a clear look scores +1 for the looked-at target and -1 for the
one opposite. The decoder adds ``weight`` x score (in baseline spreads) to the SSVEP
scores, so a clear gaze resolves an abstaining FBCCA while a strong SSVEP can still win.

The gaze abstains on a blink (vertical swing above ``blink_uv`` in the window), on a
gaze too close to the centre, and when the best two targets are not separated by
``margin``. Drift on the pads moves the centre over minutes: ``fit_centre`` or
``recentre`` (fitted mode, after a confirmed pick) puts it back.

    python -m brainsync.eog <cued.npz>...   # score gaze alone and fused with FBCCA, held out
"""
from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .classify import ABSTAIN, make
from .online import RingBuffer
from .recordings import (EOG, EOG_MONTAGE, Recording, channel_indices, cut_windows,
                         load_recording, pair_trials)
from .signal import bandpass

__all__ = ["EOG", "EOG_MONTAGE", "GazeDecoder", "GazeDecision", "raw_windows", "score_gaze"]

LOWPASS_HZ = 10.0     # the gaze level and blinks live well below this; EEG and EMG above
BLINK_UV = 150.0      # a vertical swing above this inside one window is a blink
MIN_UV = 25.0         # closer than this to the centre is "looking at the middle": abstain
MARGIN = 0.3          # the best target's cosine lead over the runner-up, else abstain
WEIGHT = 1.5          # baseline spreads a clear gaze adds to the looked-at target's score:
                      # above the decoder's level threshold (0.5) with room for the SSVEP
                      # score's own swing, so a clear gaze can decide alone when FBCCA sees
                      # nothing, while an SSVEP lead of a few spreads still wins. Tune it on
                      # a recording with ``python -m brainsync.eog --weight``.
HORIZONTAL = ("LO", "RO")
VERTICAL = ("Fp1", "Fp2")


@dataclass
class GazeDecision:
    """One scored window: the gaze vector ``uv`` (h, v) in µV from the centre, per-target
    ``scores`` in [-1, 1] (empty before targets are set), the prediction (ABSTAIN when the
    rule says hold back) and why it abstained ("" when it did not)."""

    uv: np.ndarray
    scores: np.ndarray
    raw: int
    reason: str = ""


class GazeDecoder:
    """Gaze direction from the EOG rows of board data (rows named by ``ch_names``).

    ``horizontal`` names the (left, right) canthus channels, ``vertical`` the forehead
    pair; ``gain`` scales (h, v) before the directions are compared, for an anisotropic
    electrode gain (fitted mode learns it instead). Feed board samples with ``feed`` and
    ask ``bonus`` for the newest window's prior, or score windows with ``decide``.
    """

    def __init__(self, fs: float, ch_names: Sequence[str] = EOG_MONTAGE, *,
                 horizontal: Sequence[str] = HORIZONTAL, vertical: Sequence[str] = VERTICAL,
                 window_s: float = 1.5, lowpass_hz: float = LOWPASS_HZ,
                 blink_uv: float = BLINK_UV, min_uv: float = MIN_UV, margin: float = MARGIN,
                 weight: float = WEIGHT, gain: tuple[float, float] = (1.0, 1.0)) -> None:
        self.fs = float(fs)
        if not math.isfinite(self.fs) or self.fs <= 0:
            raise ValueError(f"fs must be positive, got {fs}")
        self.ch_names = tuple(str(c) for c in ch_names)
        if len(horizontal) != 2 or len(vertical) != 2:
            raise ValueError(f"horizontal and vertical each name two channels, got "
                             f"{list(horizontal)} and {list(vertical)}")
        self.horizontal = tuple(str(c) for c in horizontal)
        self.vertical = tuple(str(c) for c in vertical)
        self._h_rows = channel_indices(self.ch_names, self.horizontal)
        self._v_rows = channel_indices(self.ch_names, self.vertical)
        for name, value in (("window_s", window_s), ("lowpass_hz", lowpass_hz),
                            ("blink_uv", blink_uv), ("min_uv", min_uv)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if lowpass_hz >= self.fs / 2:
            raise ValueError(f"lowpass_hz must be below Nyquist ({self.fs / 2:g}), got {lowpass_hz}")
        for name, value in (("margin", margin), ("weight", weight)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")
        g = tuple(float(v) for v in gain)
        if len(g) != 2 or not all(math.isfinite(v) and v > 0 for v in g):
            raise ValueError(f"gain must be two positive numbers (h, v), got {gain}")
        self.window_s = float(window_s)
        self.n_window = max(2, int(round(self.window_s * self.fs)))
        self.lowpass_hz = float(lowpass_hz)
        self.blink_uv = float(blink_uv)
        self.min_uv = float(min_uv)
        self.margin = float(margin)
        self.weight = float(weight)
        self._gain0 = np.array(g)
        self.gain = self._gain0.copy()                   # fitted mode learns its own
        self.centre = np.zeros(2)
        self.has_centre = False
        self.directions: np.ndarray = np.empty((0, 2))   # unit vectors (scaled by gain), per target
        self.radii: np.ndarray | None = None             # fitted: each target's scaled distance
        self.means: np.ndarray | None = None             # fitted: each target's mean (h, v) in µV
        self.mode = "none"                               # none | layout | fitted
        self._buf = RingBuffer(len(self.ch_names), self.n_window)

    # -- geometry ------------------------------------------------------------------------

    @property
    def n_targets(self) -> int:
        return int(self.directions.shape[0])

    @property
    def ready(self) -> bool:
        """Centre fitted, targets known and a full window buffered."""
        return self.has_centre and self.n_targets > 0 and len(self._buf) >= self.n_window

    def set_targets(self, positions: Sequence[Sequence[float]]) -> None:
        """Targets from their screen positions (x right, y up, any unit) around the centre.

        Radii are unknown in this mode: the gaze must only be ``min_uv`` from the centre.
        """
        pos = np.asarray(positions, dtype=float)
        if pos.ndim != 2 or pos.shape[1] != 2 or pos.shape[0] < 2:
            raise ValueError(f"positions must be (n_targets >= 2, 2) of (x, y), got shape {pos.shape}")
        if not np.all(np.isfinite(pos)):
            raise ValueError("positions must be finite")
        norms = np.linalg.norm(pos, axis=1)
        if np.any(norms <= 0):
            raise ValueError(f"a target at the centre has no direction: {pos.tolist()}")
        self.directions = pos / norms[:, None]
        self.radii = None
        self.means = None
        self.gain = self._gain0.copy()
        self.mode = "layout"

    def clear_targets(self) -> None:
        self.directions = np.empty((0, 2))
        self.radii = None
        self.means = None
        self.gain = self._gain0.copy()
        self.mode = "none"

    # -- features ------------------------------------------------------------------------

    def _lowpassed(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(h, v) traces of one raw window (n_ch, n): h = right - left, v = mean forehead."""
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[0] != len(self.ch_names):
            raise ValueError(f"expected board rows ({len(self.ch_names)}, n), got shape {X.shape}")
        if X.shape[1] < 2:
            raise ValueError(f"need at least 2 samples, got {X.shape[1]}")
        left, right = X[self._h_rows]
        h = right - left
        v = X[self._v_rows].mean(axis=0)
        hv = bandpass(np.stack([h, v]), self.fs, 0.0, self.lowpass_hz)
        return hv[0], hv[1]

    def features(self, X: np.ndarray) -> np.ndarray:
        """The window's gaze vector (h, v) in µV from the centre (uncorrected before fit_centre)."""
        h, v = self._lowpassed(X)
        return np.array([h.mean(), v.mean()]) - self.centre

    def blink(self, X: np.ndarray) -> bool:
        """True when the vertical trace swings more than ``blink_uv`` inside the window."""
        _, v = self._lowpassed(X)
        return bool(np.ptp(v) > self.blink_uv)

    # -- calibration ---------------------------------------------------------------------

    def fit_centre(self, X_rest: np.ndarray) -> np.ndarray:
        """The centre level from raw board rows (n_ch, n) recorded while the user looks at
        the middle of a still screen; blink-sized swings are left out of the mean."""
        X_rest = np.asarray(X_rest, dtype=float)
        if X_rest.ndim != 2 or X_rest.shape[1] < self.n_window:
            raise ValueError(f"the centre needs >= {self.window_s:g} s of board rows, got shape "
                             f"{X_rest.shape}")
        if not np.all(np.isfinite(X_rest)):
            raise ValueError("centre data contains NaN or infinity; check the board")
        h, v = self._lowpassed(X_rest)
        # Windows hop by half a window; keep those without a blink, else use everything.
        hop = max(1, self.n_window // 2)
        starts = np.arange(0, h.size - self.n_window + 1, hop)
        keep = [s for s in starts if np.ptp(v[s:s + self.n_window]) <= self.blink_uv]
        if keep:
            idx = np.concatenate([np.arange(s, s + self.n_window) for s in keep])
            h, v = h[idx], v[idx]
        self.centre = np.array([h.mean(), v.mean()])
        self.has_centre = True
        return self.centre.copy()

    def fit(self, windows: np.ndarray, labels: Sequence[int]) -> np.ndarray:
        """Learn each target's direction and radius from raw labelled windows
        (n_trials, n_ch, n); needs the centre first. Returns the (n_targets, 2) mean vectors."""
        if not self.has_centre:
            raise ValueError("fit the centre first (fit_centre) so the vectors are relative to it")
        W = np.asarray(windows, dtype=float)
        y = np.asarray(labels, dtype=int).reshape(-1)
        if W.ndim != 3 or W.shape[0] != y.size or W.shape[0] < 2:
            raise ValueError(f"windows must be (n_trials >= 2, n_ch, n) with one label each, got "
                             f"{W.shape} and {y.size} labels")
        if y.min() < 0:
            raise ValueError(f"labels must be target indices >= 0, got {y.tolist()}")
        n_targets = int(y.max()) + 1
        if n_targets < 2:
            raise ValueError("fit needs trials of at least 2 different targets")
        feats = np.array([self.features(w) for w in W])
        means = np.empty((n_targets, 2))
        for k in range(n_targets):
            if not np.any(y == k):
                raise ValueError(f"no trial labelled {k} among targets 0..{n_targets - 1}")
            means[k] = feats[y == k].mean(axis=0)
        # The two axes have different gains (the forehead pair sees less of the vertical
        # dipole than the canthi see of the horizontal one): scale each so the targets'
        # spread is the same on both, else corners differ by too small an angle.
        spread = means.std(axis=0)
        if np.all(spread > 0):
            gain = spread.max() / spread
        else:
            gain = np.ones(2)          # targets on one line: no second axis to scale
        scaled = means * gain
        radii = np.linalg.norm(scaled, axis=1)
        if np.any(radii <= 0):
            raise ValueError("a target's mean gaze sits on the centre; it cannot give a direction")
        self.gain = gain
        self.means = means
        self.directions = scaled / radii[:, None]
        self.radii = radii
        self.mode = "fitted"
        return means

    def recentre(self, X: np.ndarray, target: int) -> np.ndarray:
        """Move the centre so this raw window (n_ch, n), known to be a look at ``target``,
        lands on that target's fitted vector; corrects drift after a confirmed pick."""
        if self.mode != "fitted" or self.radii is None:
            raise ValueError("recentre needs fitted targets (fit), not a layout")
        if not 0 <= int(target) < self.n_targets:
            raise ValueError(f"target {target} is not one of 0..{self.n_targets - 1}")
        h, v = self._lowpassed(X)
        level = np.array([h.mean(), v.mean()])
        self.centre = level - self.means[int(target)]
        return self.centre.copy()

    # -- decisions -----------------------------------------------------------------------

    def _confidence(self, uv: np.ndarray) -> float:
        """0 at the centre, 1 at min_uv (layout) or at half the nearest radius (fitted)."""
        r = float(np.linalg.norm(uv * self.gain))
        if self.radii is None:
            scale = self.min_uv
        else:
            scale = max(self.min_uv, 0.5 * float(self.radii.min()))
        return min(1.0, r / scale)

    def score_uv(self, uv: np.ndarray) -> np.ndarray:
        """Per-target scores in [-1, 1] for a gaze vector: cosine x confidence."""
        if self.n_targets == 0:
            raise ValueError("no targets: call set_targets or fit first")
        g = np.asarray(uv, dtype=float) * self.gain
        r = float(np.linalg.norm(g))
        if r == 0:
            return np.zeros(self.n_targets)
        return self.directions @ (g / r) * self._confidence(uv)

    def decide_window(self, X: np.ndarray) -> GazeDecision:
        """Score one raw window (n_ch, n)."""
        if self.blink(X):
            uv = self.features(X)
            return GazeDecision(uv, np.zeros(self.n_targets), ABSTAIN, "blink")
        uv = self.features(X)
        if not np.all(np.isfinite(uv)):
            return GazeDecision(uv, np.zeros(self.n_targets), ABSTAIN, "not_finite")
        if self.n_targets == 0:
            return GazeDecision(uv, np.empty(0), ABSTAIN, "no_targets")
        if float(np.linalg.norm(uv * self.gain)) < self.min_uv:
            return GazeDecision(uv, np.zeros(self.n_targets), ABSTAIN, "centre")
        scores = self.score_uv(uv)
        order = np.argsort(scores)
        best = int(order[-1])
        gap = float(scores[order[-1]] - scores[order[-2]])
        if gap < self.margin:
            return GazeDecision(uv, scores, ABSTAIN, "margin")
        return GazeDecision(uv, scores, best)

    def decide(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Predictions (n,) and scores (n, n_targets) for raw windows (n, n_ch, samples)."""
        W = np.asarray(windows, dtype=float)
        if W.ndim != 3:
            raise ValueError(f"windows must be (n, n_ch, samples), got shape {W.shape}")
        out = [self.decide_window(w) for w in W]
        return (np.array([d.raw for d in out], dtype=int),
                np.array([d.scores for d in out]).reshape(len(out), self.n_targets))

    # -- streaming -----------------------------------------------------------------------

    def reset(self) -> None:
        """Forget buffered samples (the centre and targets stay)."""
        self._buf.clear()

    def feed(self, X: np.ndarray) -> None:
        """Append board samples (n_ch, n); only the newest window is kept."""
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[0] != len(self.ch_names):
            raise ValueError(f"expected board rows ({len(self.ch_names)}, n), got shape {X.shape}")
        if X.shape[1]:
            self._buf.append(X)

    def latest(self) -> GazeDecision | None:
        """The newest window's decision, or None until the centre, targets and a window exist."""
        if not self.ready:
            return None
        return self.decide_window(self._buf.last(self.n_window))

    def bonus(self) -> np.ndarray | None:
        """``weight`` x the newest window's scores for the SSVEP decoder's prior; None when
        the gaze is not ready or abstains, so the SSVEP scores stay untouched."""
        d = self.latest()
        if d is None or d.raw == ABSTAIN:
            return None
        return self.weight * d.scores


# -- offline scoring -----------------------------------------------------------------------

@dataclass
class RawWindows:
    """Unfiltered flicker (``on``) and still (``off``) windows cut like recordings.cut_windows."""

    on: np.ndarray
    off: np.ndarray
    labels: np.ndarray


def raw_windows(rec: Recording, window_s: float | None = None,
                latency: float | None = None) -> RawWindows:
    """Cut raw (unfiltered, not demeaned) windows per trial, the same trials and sample
    starts as ``cut_windows`` for the same arguments."""
    window_s = rec.window_s if window_s is None else float(window_s)
    latency = rec.latency if latency is None else float(latency)
    if window_s <= 0:
        raise ValueError(f"window_s must be positive, got {window_s}")
    n_win = int(round(window_s * rec.fs))
    n_total = rec.X.shape[1]
    tol = 0.01 / rec.fs
    ons, offs, labels = [], [], []
    for t_on, t_off, target in pair_trials(rec.markers):
        if t_on + latency < rec.t[0] - tol:
            continue
        s_on = int(np.searchsorted(rec.t, t_on + latency - tol))
        s_off = int(np.searchsorted(rec.t, t_off + latency - tol))
        if s_on + n_win > n_total or s_off + n_win > n_total:
            continue
        ons.append(s_on)
        offs.append(s_off)
        labels.append(target)
    if not ons:
        n_ch = rec.X.shape[0]
        return RawWindows(np.empty((0, n_ch, n_win)), np.empty((0, n_ch, n_win)),
                          np.empty(0, dtype=int))
    span = np.arange(n_win)
    on = rec.X[:, np.asarray(ons)[:, None] + span].transpose(1, 0, 2)
    off = rec.X[:, np.asarray(offs)[:, None] + span].transpose(1, 0, 2)
    return RawWindows(on, off, np.asarray(labels, dtype=int))


@dataclass
class GazeScore:
    """Held-out accuracy of the gaze alone, FBCCA alone and the two fused, on one recording."""

    path: Path | None
    window_s: float
    n: int
    gaze_hits: int
    gaze_abstain: int
    ssvep_hits: int
    ssvep_abstain: int
    fused_hits: int
    fused_abstain: int
    notes: list[str] = field(default_factory=list)

    def rate(self, hits: int) -> float:
        return hits / self.n if self.n else math.nan


def score_gaze(rec: Recording, positions: Sequence[Sequence[float]] | None = None,
               window_s: float | None = None, weight: float = WEIGHT,
               ssvep_margin: float = 0.5, ssvep_threshold: float | None = 0.5,
               make_gaze: Callable[[float, Sequence[str]], GazeDecoder] | None = None) -> GazeScore:
    """Leave-one-trial-out scoring of a cued recording with EOG channels.

    The gaze centre comes from the still windows; with ``positions`` the gaze runs in
    layout mode, otherwise each trial is scored by targets fitted on the other trials.
    FBCCA runs on the posterior channels with the still windows as its baseline; "fused"
    adds ``weight`` x the gaze scores (in baseline spreads) before FBCCA's abstain rules.
    """
    missing = [c for c in EOG if c not in rec.ch_names]
    if missing:
        raise ValueError(f"{rec.path}: no EOG channels {missing} in {list(rec.ch_names)}; "
                         f"record with --names {','.join(EOG_MONTAGE)}")
    raw = raw_windows(rec, window_s)
    n = int(raw.labels.size)
    w_s = rec.window_s if window_s is None else float(window_s)
    if n < 2:
        return GazeScore(rec.path, w_s, n, 0, 0, 0, 0, 0, 0, ["fewer than 2 trials"])
    factory = make_gaze or (lambda fs, names: GazeDecoder(fs, names, window_s=w_s))
    gaze = factory(rec.fs, rec.ch_names)
    gaze.fit_centre(np.concatenate(list(raw.off), axis=1))
    if positions is not None:
        gaze.set_targets(positions)

    # SSVEP: the usual preprocessed windows on the posterior channels.
    win = cut_windows(rec, w_s, band=rec.band, notch_hz=rec.notch).pick(
        [c for c in ("P7", "P8", "O1", "O2") if c in rec.ch_names])
    if len(win) != n:
        raise ValueError(f"{rec.path}: {len(win)} filtered but {n} raw trial windows")
    clf = make("fbcca", freqs=rec.freqs, fs=rec.fs, phases=rec.phases, margin=ssvep_margin,
               threshold=ssvep_threshold)
    clf.fit_baseline(win.off)
    s_scores = clf.scores(win.on)
    s_pred = clf.predict_scores(s_scores)

    gaze_pred = np.full(n, ABSTAIN)
    gaze_scores = np.zeros((n, rec.n_targets))
    for i in range(n):
        if positions is None:
            others = np.array([j for j in range(n) if j != i])
            if np.unique(raw.labels[others]).size < rec.n_targets:
                continue
            gaze.fit(raw.on[others], raw.labels[others])
        d = gaze.decide_window(raw.on[i])
        gaze_pred[i] = d.raw
        if d.raw != ABSTAIN:
            gaze_scores[i] = d.scores
    fused = clf.predict_scores(s_scores + weight * gaze_scores * clf.baseline_spread)
    y = raw.labels
    return GazeScore(rec.path, w_s, n,
                     int(np.sum(gaze_pred == y)), int(np.sum(gaze_pred == ABSTAIN)),
                     int(np.sum(s_pred == y)), int(np.sum(s_pred == ABSTAIN)),
                     int(np.sum(fused == y)), int(np.sum(fused == ABSTAIN)))


def format_score(s: GazeScore) -> str:
    def cell(hits: int, abstain: int) -> str:
        return f"{100 * s.rate(hits):5.1f} % correct, {abstain:3d} abstained"
    lines = [f"{s.path}: {s.n} trials, {s.window_s:g} s windows",
             f"  gaze  : {cell(s.gaze_hits, s.gaze_abstain)}",
             f"  fbcca : {cell(s.ssvep_hits, s.ssvep_abstain)}",
             f"  fused : {cell(s.fused_hits, s.fused_abstain)}"]
    lines += [f"  note: {t}" for t in s.notes]
    return "\n".join(lines)


def _parse_positions(text: str | None) -> list[tuple[float, float]] | None:
    """"x,y;x,y;..." -> positions; None for None."""
    if text is None:
        return None
    out = []
    for part in text.split(";"):
        xy = part.split(",")
        if len(xy) != 2:
            raise ValueError(f"positions are x,y pairs separated by ';', got {text!r}")
        out.append((float(xy[0]), float(xy[1])))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m brainsync.eog",
                                 description="Score the EOG gaze prior on cued recordings")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--windows", type=float, nargs="+", default=[1.5, 3.0])
    ap.add_argument("--positions", default=None,
                    help="target positions 'x,y;x,y;...' (x right, y up) for layout mode; "
                         "default: targets fitted on the other trials (held out)")
    ap.add_argument("--weight", type=float, default=WEIGHT)
    args = ap.parse_args(argv)
    try:
        positions = _parse_positions(args.positions)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    status = 0
    for path in args.files:
        try:
            rec = load_recording(path)
            for w in args.windows:
                print(format_score(score_gaze(rec, positions, w, weight=args.weight)))
        except (OSError, KeyError, ValueError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
