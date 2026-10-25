"""Score CCA and FBCCA on the cued multi-target recordings (``*_cued.npz``).

    python -m brainsync.score_cued                        # every *_cued.npz in recordings_dir()
    python -m brainsync.score_cued path/a_cued.npz --methods fbcca --windows 1 2 --margin 1.5

Every trial is cut into a flicker window (stim_on + latency) and a "still" window
(stim_off + latency) of the recorded length; shorter analysis windows take the first
seconds of each. The still window contains no flicker, but at the full recorded length
it can run into the next trial's cue screen (with 3 s rest, a 1 s cue and a 3.86 s window
it covers the whole 1 s cue and ends at the next stim_on); the report states this overlap
and warns if a still window ever reaches the next flicker.

Each classifier is given that recording's still windows (same length) as its rest
baseline. Accuracy is forced choice (argmax); the abstain rate is how often the decoder
would hold back at ``--margin`` baseline spreads. The still-window control classifies
every trial's still window (baseline-corrected) against its cued label and should sit
near chance. The best cell and the highest control cell are each the maximum over all
(method x window) cells, so besides the cell's own binomial p each gets a label-
permutation p for that maximum, which accounts for picking it.

Exit status: 0 when every recording was scored, 1 when some recording could not be,
2 for invalid command-line options.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .classify import ABSTAIN, NAMES, TRAINED, make
from .metrics import abstain_rate, accuracy, binom_p, confusion_matrix, itr_bpm
from .recordings import POSTERIOR, Recording, Windows, cut_windows, \
    list_recordings, load_recording, reject_artifacts
from .signal import Band, default_bands
from .templates import cross_val_predict

METHODS = ("cca", "fbcca")
WINDOWS = (1.0, 2.0, 3.0)
DEFAULT_MARGIN = 1.0     # baseline spreads the winner must lead by before the decoder commits
GAZE_SHIFT_S = 0.5       # added to the window for the ITR selection time
N_PERMUTATIONS = 5000
_EPS = 1e-9
_CUE_RE = re.compile(r"^\s*\[marker\s+([-+0-9.eE]+)\]\s+cue:\d+\s*$", re.MULTILINE)


@dataclass
class Row:
    """One (method, window) result."""

    method: str
    window_s: float
    n: int
    hits: int
    accuracy: float
    p: float
    itr_bpm: float
    abstain: float              # fraction of flicker windows below the margin
    committed_accuracy: float   # accuracy over the windows that were not abstained (nan if none)
    control_hits: int           # still windows decoded as the cued target (forced choice)
    control_p: float
    control_abstain: float      # fraction of still windows below the margin
    baseline_mean: np.ndarray
    baseline_spread: float
    confusion: np.ndarray = field(repr=False)   # forced choice, rows cued, columns decoded
    pred: np.ndarray = field(repr=False)
    pred_margin: np.ndarray = field(repr=False)
    scores: np.ndarray = field(repr=False)
    control_pred: np.ndarray = field(repr=False)
    folds: int = 0              # > 0: a trained method, scored leave-one-block-out over this many blocks

    @property
    def control_accuracy(self) -> float:
        return self.control_hits / self.n


@dataclass
class StillTiming:
    """Where the still windows sit relative to the next trial (seconds; nan if unknown)."""

    offset_s: float        # still window start after stim_off
    length_s: float
    into_cue_s: float      # most a still window runs past the next cue marker
    lead_s: float          # least time from a still window's end to the next stim_on
    sample_s: float = 0.0  # one sample; overlaps up to this are marker rounding

    @property
    def overlaps_flicker(self) -> bool:
        return bool(np.isfinite(self.lead_s) and self.lead_s < -(self.sample_s + _EPS))


@dataclass
class Report:
    """Scores for one recording."""

    path: Path | None
    freqs: np.ndarray
    channels: tuple[str, ...]
    n_total: int
    kept: np.ndarray
    labels: np.ndarray
    margin: float
    rows: list[Row]
    skipped_windows: tuple[float, ...] = ()
    still: StillTiming | None = None

    @property
    def chance(self) -> float:
        return 1.0 / len(self.freqs)

    @property
    def dropped(self) -> np.ndarray:
        """Indices (among the complete trials) removed by artifact rejection."""
        return np.setdiff1d(np.arange(self.n_total), self.kept)

    @property
    def best(self) -> Row:
        """Most accurate cell; the shorter window wins a tie."""
        return max(self.rows, key=lambda r: (r.accuracy, -r.window_s))

    @property
    def worst_control(self) -> Row:
        return max(self.rows, key=lambda r: (r.control_hits, -r.window_s))

    def row(self, method: str, window_s: float) -> Row:
        for r in self.rows:
            if r.method == method and abs(r.window_s - window_s) < 1e-6:
                return r
        raise KeyError(f"no result for method {method!r} at window {window_s} s")

    def _max_cell_p(self, preds: np.ndarray, n_perm: int, seed: int) -> float:
        """Permutation p of the highest hit count over cells, preds (cells, trials).

        The cued labels are shuffled ``n_perm`` times against the fixed predictions; p is
        the fraction of shuffles whose best cell does at least as well.
        """
        if n_perm < 1:
            raise ValueError(f"n_perm must be >= 1, got {n_perm}")
        observed = int((preds == self.labels).sum(axis=1).max())
        rng = np.random.default_rng(seed)
        shuffled = np.stack([rng.permutation(self.labels) for _ in range(n_perm)])
        best = (preds[None] == shuffled[:, None]).sum(axis=2).max(axis=1)
        return float((1 + np.count_nonzero(best >= observed)) / (1 + n_perm))

    def control_max_p(self, n_perm: int = N_PERMUTATIONS, seed: int = 0) -> float:
        """Permutation p-value of the highest still-control hit count across all cells."""
        return self._max_cell_p(np.stack([r.control_pred for r in self.rows]), n_perm, seed)

    def best_max_p(self, n_perm: int = N_PERMUTATIONS, seed: int = 0) -> float:
        """Permutation p-value of the best flicker cell, corrected for picking it among all
        cells (forced-choice predictions; the same shuffle as control_max_p)."""
        return self._max_cell_p(np.stack([r.pred for r in self.rows]), n_perm, seed)


def split_windows(windows: Sequence[float], recorded_s: float) -> tuple[list[float], list[float]]:
    """(usable windows ascending, plus the recorded one; requested windows that are too long)."""
    if not np.isfinite(recorded_s) or recorded_s <= 0:
        raise ValueError(f"recorded window must be positive, got {recorded_s}")
    used: list[float] = []
    too_long: list[float] = []
    for w in windows:
        w = float(w)
        if not np.isfinite(w) or w <= 0:
            raise ValueError(f"window lengths must be positive, got {w}")
        if w > recorded_s + _EPS:
            if w not in too_long:
                too_long.append(w)
        elif all(abs(w - v) > _EPS for v in used):
            used.append(min(w, recorded_s))
    if all(abs(recorded_s - v) > _EPS for v in used):
        used.append(float(recorded_s))
    return sorted(used), sorted(too_long)


def resolve_windows(windows: Sequence[float], recorded_s: float) -> list[float]:
    """Requested windows that fit in the recorded one, ascending, plus the recorded window."""
    return split_windows(windows, recorded_s)[0]


def score_windows(on: np.ndarray, off: np.ndarray, y: np.ndarray, freqs: Sequence[float],
                  fs: float, windows: Sequence[float], methods: Sequence[str] = METHODS,
                  phases: Sequence[float] | None = None, margin: float = DEFAULT_MARGIN,
                  harmonics: int = 3, blocks: Sequence[int] | None = None,
                  latency: float = 0.0, bands: Sequence[Band] | None = None) -> list[Row]:
    """Score labelled flicker windows ``on`` against still-window baselines ``off``.

    ``on``/``off`` are (n_trials, n_ch, n) preprocessed and channel-picked; each window
    length uses the first round(w * fs) samples of both. Training-free methods take every
    still window as their baseline and score every trial. Trained methods (``TRAINED``) are
    scored leave-one-block-out over ``blocks`` (one per trial): each block's trials and still
    windows are scored by a classifier fitted, baseline included, on the other blocks. The
    flicker windows start ``latency`` s after the onset, which the templates need to know.
    ``bands`` turns on the filter bank for FBCCA and the trained methods.
    """
    on = np.asarray(on, dtype=float)
    off = np.asarray(off, dtype=float)
    y = np.asarray(y, dtype=int).reshape(-1)
    if on.ndim != 3 or on.shape != off.shape:
        raise ValueError(f"on/off must be matching (n_trials, n_ch, n), got {on.shape} and {off.shape}")
    if y.size != on.shape[0]:
        raise ValueError(f"{y.size} labels for {on.shape[0]} trials")
    if y.size < 2:
        raise ValueError(f"need at least 2 trials to score, got {y.size}")
    n_targets = len(freqs)
    if n_targets < 2:
        raise ValueError(f"need at least 2 targets to score, got freqs {list(freqs)}")
    chance = 1.0 / n_targets
    rows: list[Row] = []
    for w in windows:
        n = int(round(float(w) * fs))
        if not 0 < n <= on.shape[-1]:
            raise ValueError(f"window {w} s ({n} samples) does not fit in {on.shape[-1]} samples")
        X_on, X_off = on[..., :n], off[..., :n]
        for m in methods:
            kw = dict(freqs=freqs, fs=fs, harmonics=harmonics, phases=phases, margin=margin)
            if bands is not None and m != "cca":
                kw["bands"] = list(bands)
            clf = make(m, **kw)
            folds = 0
            if clf.needs_fit:
                if blocks is None:
                    raise ValueError(f"{m} is a trained method and needs one block per trial")
                cv = cross_val_predict(lambda: make(m, **kw), X_on, y, blocks, latency, rest=X_off)
                pred_margin, s, rest = cv.pred, cv.scores, cv.rest_scores
                assert rest is not None
                folds = cv.n_folds
                clf.baseline_mean = cv.baseline_means.mean(axis=0)
                clf.baseline_spread = float(cv.baseline_spreads.mean())
                clf.has_baseline = True
            else:
                clf.fit_baseline(X_off)
                pred_margin, s = clf.decide(X_on, latency)
                rest = clf.scores(X_off)
            pred = s.argmax(axis=1)
            control = rest.argmax(axis=1)
            hits = int(np.count_nonzero(pred == y))
            acc = accuracy(y, pred)
            go = pred_margin != ABSTAIN
            c_hits = int(np.count_nonzero(control == y))
            rows.append(Row(
                method=m, window_s=float(w), n=int(y.size), hits=hits, accuracy=acc,
                p=binom_p(hits, y.size, chance),
                itr_bpm=itr_bpm(n_targets, acc, float(w) + GAZE_SHIFT_S),
                abstain=abstain_rate(pred_margin),
                committed_accuracy=float(np.mean(pred_margin[go] == y[go])) if go.any()
                else float("nan"),
                control_hits=c_hits, control_p=binom_p(c_hits, y.size, chance),
                control_abstain=abstain_rate(clf.predict_scores(rest)),
                baseline_mean=clf.baseline_mean.copy(), baseline_spread=clf.baseline_spread,
                confusion=confusion_matrix(y, pred, n_targets),
                pred=pred, pred_margin=pred_margin, scores=s, control_pred=control,
                folds=folds))
    return rows


def trial_windows(rec: Recording, channels: Sequence[str | int] | None = POSTERIOR,
                  reject: float = 3.0) -> tuple[Windows, np.ndarray, int]:
    """Cut, artifact-reject (on the analysis channels) and channel-pick a recording.

    Returns the kept windows, their indices among the complete trials, and how many
    complete trials there were.
    """
    w = cut_windows(rec, band=rec.band, notch_hz=rec.notch)
    if len(w) == 0:
        raise ValueError(f"{rec.path}: no complete trials in {len(rec.markers)} markers")
    picked = w.pick(channels)
    kept = reject_artifacts(picked.on, picked.off, factor=reject, ch_names=picked.ch_names)
    return picked.subset(kept), kept, len(w)


def still_timing(rec: Recording, w: Windows) -> StillTiming:
    """How far the still windows reach into the following cue and towards the next flicker."""
    length = w.off.shape[-1] / rec.fs
    starts = rec.t[w.off_start]
    ends = starts + length
    offs = np.sort([m[0] for m in rec.markers if m[1] == "stim_off"])
    ons = np.sort([m[0] for m in rec.markers if m[1] == "stim_on"])
    cues = np.sort([float(s) for s in _CUE_RE.findall(rec.marker_text)])
    offset = float("nan")
    if offs.size and starts.size:
        prev_off = offs[np.clip(np.searchsorted(offs, starts, side="right") - 1, 0, None)]
        offset = float(np.median(starts - prev_off))
    into_cue, lead = [], []
    for s, e in zip(starts, ends):
        later_on = ons[ons > s]
        if later_on.size:
            lead.append(later_on[0] - e)
        later_cue = cues[cues > s]
        if later_cue.size and (not later_on.size or later_cue[0] <= later_on[0]):
            into_cue.append(max(0.0, e - later_cue[0]))
    return StillTiming(offset_s=offset, length_s=float(length),
                       into_cue_s=float(max(into_cue)) if into_cue else float("nan"),
                       lead_s=float(min(lead)) if lead else float("nan"),
                       sample_s=1.0 / rec.fs)


_BLOCK_RE = re.compile(r"^\s*\[marker\s+([-+0-9.eE]+)\]\s+block_start:(\d+)\s*$", re.MULTILINE)


def trial_blocks(kept: np.ndarray, n_targets: int, on_times: np.ndarray | None = None,
                 marker_text: str = "") -> np.ndarray:
    """Block of each kept trial, for the leave-one-block-out folds.

    From the display's ``block_start`` markers when the recording has them (each trial
    belongs to the last block that started at or before its flicker), else by position: the
    record tool cues every target once per block, so complete trial ``i`` is in block
    ``i // n_targets`` (which shifts if an early trial was dropped).
    """
    kept = np.asarray(kept, dtype=int)
    starts = sorted((float(m.group(1)), int(m.group(2))) for m in _BLOCK_RE.finditer(marker_text))
    if starts and on_times is not None and len(on_times) == kept.size:
        stamps = np.array([t for t, _ in starts])
        ids = np.array([b for _, b in starts])
        idx = np.searchsorted(stamps, np.asarray(on_times, dtype=float), side="right") - 1
        if np.all(idx >= 0):
            return ids[idx]
    return kept // int(n_targets)


def score_recording(rec: Recording, methods: Sequence[str] = METHODS,
                    windows: Sequence[float] = WINDOWS, margin: float = DEFAULT_MARGIN,
                    channels: Sequence[str | int] | None = POSTERIOR, reject: float = 3.0,
                    harmonics: int = 3, bands: Sequence[Band] | None = None) -> Report:
    """Score one cued recording at ``windows`` (clipped to, and including, the recorded one).

    Requested windows longer than the recorded one are listed in ``Report.skipped_windows``.
    Trained methods are cross-validated block by block (``trial_blocks``).
    """
    if rec.n_targets < 2:
        raise ValueError(f"{rec.path}: needs at least 2 targets, has freqs {rec.freqs.tolist()}")
    w, kept, n_total = trial_windows(rec, channels, reject)
    lengths, too_long = split_windows(windows, w.on.shape[-1] / rec.fs)
    rows = score_windows(w.on, w.off, w.labels, rec.freqs, rec.fs, lengths, methods,
                         phases=rec.phases, margin=margin, harmonics=harmonics,
                         blocks=trial_blocks(kept, rec.n_targets, rec.t[w.on_start], rec.marker_text),
                         latency=rec.latency, bands=bands)
    return Report(path=rec.path, freqs=rec.freqs.copy(), channels=tuple(w.ch_names),
                  n_total=n_total, kept=kept, labels=w.labels.copy(), margin=float(margin),
                  rows=rows, skipped_windows=tuple(too_long), still=still_timing(rec, w))


# -- text report ----------------------------------------------------------------------

def _hz(f: float) -> str:
    return f"{f:g} Hz"


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _still_note(st: StillTiming) -> list[str]:
    text = f"still windows: stim_off+{st.offset_s:.2f} s for {st.length_s:.2f} s"
    if np.isfinite(st.into_cue_s):
        text += f", up to {st.into_cue_s:.2f} s inside the next cue screen (no flicker)"
    if np.isfinite(st.lead_s) and not st.overlaps_flicker:
        text += f"; they end by the next stim_on (closest {max(st.lead_s, 0.0):.2f} s before)"
    out = [text]
    if st.overlaps_flicker:
        out.append(f"WARNING: still windows overlap the next flicker by up to {-st.lead_s:.2f} s; "
                   f"the still control and baseline are contaminated")
    return out


def _confusion_table(r: Row, freqs: Sequence[float]) -> list[str]:
    names = [_hz(f) for f in freqs]
    col = max(7, *(len(s) for s in names)) + 1
    out = [f"decoded vs cued, {r.method} at {r.window_s:g} s "
           f"(cued target per row, decoded per column):",
           " " * col + " |" + "".join(s.rjust(col) for s in names)]
    for name, counts in zip(names, r.confusion):
        out.append(name.rjust(col) + " |" + "".join(str(int(c)).rjust(col) for c in counts))
    return out


def format_report(rep: Report, n_perm: int = N_PERMUTATIONS) -> str:
    """Human-readable tables for one recording."""
    name = rep.path.name if rep.path is not None else "<recording>"
    per_target = np.bincount(rep.labels, minlength=len(rep.freqs))
    mix = ", ".join(f"{_hz(f)} x{c}" for f, c in zip(rep.freqs, per_target))
    lines = [f"== {name}",
             f"trials kept {len(rep.labels)}/{rep.n_total} [{mix}]; chance {_pct(rep.chance)}; "
             f"channels {','.join(rep.channels)}; margin {rep.margin:g} spreads"]
    if rep.dropped.size:
        lines.append(f"artifact rejection removed {rep.dropped.size}: "
                     + " ".join(f"#{i}" for i in rep.dropped))
    if rep.skipped_windows:
        lines.append("not scored (longer than the recorded window): "
                     + ", ".join(f"{w:g} s" for w in rep.skipped_windows))
    if rep.still is not None:
        lines += _still_note(rep.still)

    base_hdr = " ".join(f"{_hz(f):>8}" for f in rep.freqs)
    lines.append(f"{'method':<6} {'window':>7} {'hits/n':>7} {'acc':>5} {'p':>7} {'ITR':>6} "
                 f"{'abstain':>7} {'acc|go':>6} | {'still':>7} {'p':>6} {'abst':>5} | "
                 f"rest mean {base_hdr} {'spread':>7}")
    for r in rep.rows:
        go = "  -  " if np.isnan(r.committed_accuracy) else f"{100 * r.committed_accuracy:5.0f}%"
        base = " ".join(f"{v:8.4f}" for v in r.baseline_mean)
        name = f"{r.method}*" if r.folds else r.method
        lines.append(
            f"{name:<6} {r.window_s:>5.2f} s {r.hits:>3d}/{r.n:<3d} {100 * r.accuracy:4.0f}% "
            f"{r.p:7.4f} {r.itr_bpm:6.1f} {100 * r.abstain:6.0f}% {go} | "
            f"{r.control_hits:>3d}/{r.n:<3d} {r.control_p:6.3f} {100 * r.control_abstain:4.0f}% | "
            f"          {base} {r.baseline_spread:7.4f}")

    cv = [r.folds for r in rep.rows if r.folds]
    if cv:
        lines.append(f"* trained method: each block scored by a fit on the other "
                     f"{cv[0] - 1} blocks ({cv[0]} folds, rest mean/spread averaged over folds)")
    longest = max(r.window_s for r in rep.rows)
    for r in rep.rows:
        if r.window_s == longest:
            lines += _confusion_table(r, rep.freqs)

    b, c = rep.best, rep.worst_control
    lines.append(f"best: {b.method} {b.hits}/{b.n} ({_pct(b.accuracy)}) at {b.window_s:g} s, "
                 f"p={b.p:.4f} (this cell), max over {len(rep.rows)} cells label-permutation "
                 f"p={rep.best_max_p(n_perm):.4f}; still control there {b.control_hits}/{b.n} "
                 f"({_pct(b.control_accuracy)})")
    lines.append(f"highest still control: {c.control_hits}/{c.n} ({_pct(c.control_accuracy)}) "
                 f"{c.method} at {c.window_s:g} s; max over {len(rep.rows)} cells, "
                 f"label-permutation p={rep.control_max_p(n_perm):.3f}")
    return "\n".join(lines)


# -- CLI ------------------------------------------------------------------------------

def _split(values: Sequence[str]) -> list[str]:
    return [v.strip() for item in values for v in item.split(",") if v.strip()]


def parse_channels(text: str) -> list[str | int]:
    """Comma-separated channel names or 0-based indices ("O1,O2" or "6,7")."""
    return [int(tok) if re.fullmatch(r"[-+]?\d+", tok) else tok for tok in _split([text])]


def _usage_problem(args: argparse.Namespace, methods: list[str],
                   channels: list[str | int]) -> str | None:
    bad = [m for m in methods if m not in NAMES]
    if bad or not methods:
        return f"unknown methods {bad or methods}; choose from {list(NAMES)}"
    if not channels:
        return f"no channels in {args.channels!r}"
    # names and indices are checked against each recording's own ch_names when it is scored
    negative = [c for c in channels if isinstance(c, int) and c < 0]
    if negative:
        return f"channel indices must be >= 0, got {negative}"
    bad_w = [w for w in args.windows if not np.isfinite(w) or w <= 0]
    if bad_w:
        return f"window lengths must be positive, got {bad_w}"
    if not np.isfinite(args.margin) or args.margin < 0:
        return f"--margin must be >= 0, got {args.margin}"
    if not np.isfinite(args.reject) or args.reject < 0:
        return f"--reject must be >= 0, got {args.reject}"
    if args.harmonics < 1:
        return f"--harmonics must be >= 1, got {args.harmonics}"
    return None


def _error_text(path: Path, exc: Exception) -> str:
    msg = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
    return msg if str(path) in msg else f"{path}: {msg}"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m brainsync.score_cued",
        description="CCA / FBCCA accuracy on cued multi-target recordings.",
        epilog="exit status: 0 all scored, 1 some recording failed, 2 invalid options")
    ap.add_argument("files", nargs="*", type=Path,
                    help="*_cued.npz recordings (default: all in $BRAINSYNC_DATA_DIR/spectrum_checks)")
    ap.add_argument("--methods", nargs="+", default=list(METHODS),
                    help="classifiers, space or comma separated (default cca fbcca); trained "
                         f"ones {list(TRAINED)} are scored leave-one-block-out")
    ap.add_argument("--windows", type=float, nargs="+", default=list(WINDOWS),
                    help="window lengths in s; the recorded window is always added (default 1 2 3)")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                    help=f"abstain margin in baseline spreads (default {DEFAULT_MARGIN:g})")
    ap.add_argument("--channels", default=",".join(POSTERIOR),
                    help=f"comma-separated channel names or 0-based indices "
                         f"(default {','.join(POSTERIOR)})")
    ap.add_argument("--reject", type=float, default=3.0,
                    help="drop trials with RMS above this x median (0 keeps all; default 3)")
    ap.add_argument("--harmonics", type=int, default=3)
    ap.add_argument("--fb-bands", metavar="N:UPPER",
                    help="filter bank for fbcca and the trained methods: N sub-bands from 8m Hz "
                         "to UPPER Hz (default: fbcca's 3 bands to 45 Hz; none for trained)")
    args = ap.parse_args(argv)

    methods = [m.lower() for m in _split(args.methods)]
    channels = parse_channels(args.channels)
    problem = _usage_problem(args, methods, channels)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    files = list(args.files) or list_recordings("cued")
    if not files:
        print("error: no *_cued.npz recordings found (set BRAINSYNC_DATA_DIR or pass files)",
              file=sys.stderr)
        return 1

    status = 0
    blocks: list[str] = []
    for path in files:
        print(f"scoring {path}", file=sys.stderr)
        try:
            rec = load_recording(path)
            bands = None
            if args.fb_bands:
                n, upper = args.fb_bands.split(":")
                bands = default_bands(rec.fs, float(upper), int(n))
            rep = score_recording(rec, methods, args.windows, args.margin,
                                  channels, args.reject, args.harmonics, bands)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            print(f"error: {_error_text(path, exc)}", file=sys.stderr)
            status = 1
            continue
        if rep.skipped_windows:
            print(f"note: {path.name}: skipping windows "
                  f"{', '.join(f'{w:g} s' for w in rep.skipped_windows)}, longer than the "
                  f"recorded {max(r.window_s for r in rep.rows):g} s", file=sys.stderr)
        blocks.append(format_report(rep))
    if blocks:
        print("\n\n".join(blocks))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
