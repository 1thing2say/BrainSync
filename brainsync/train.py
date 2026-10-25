"""Train the TRCA classifier on the user's own cued recordings, and say whether it beats FBCCA.

    python -m brainsync.train data/spectrum_checks/<stamp>_cued.npz
    python -m brainsync.train a_cued.npz b_cued.npz --windows 1,1.5,2 --fusion cv --json out.json

Every kept trial (score_cued.trial_windows: whole-recording preprocessing, artifact
rejection, the analysis channels) is held out exactly once. Folds go by each trial's rank
among the trials of its target (time order across the files), fold = rank % n_folds with
n_folds the smallest per-target count, so a complete shuffled session is scored leave-one-
block-out: each block cues every target once. In each fold a TRCA (trca.TRCA, with its own
inner CV gate for the fusion weights) is fitted on the other folds' flicker windows, and
both it and FBCCA get their rest baseline from the other folds' still windows only (cut
every --step seconds). Nothing fitted on a trial, or on its still screen, ever scores it.

The held-out flicker windows are scored at every --windows length in two conditions:
"aligned" (the window starts at stim_on + latency, as the training trials do) and
"sliding" (every --step through the trial: the live decoder never knows the onset). For
each: forced-choice accuracy without (raw) and with (bc) the rest baseline, how many
windows the --margin / --threshold rules let through and how many of those were right,
and how many held-out still windows the rules let through (false alarms). With 16 trials
one trial is about 6 points, so the report also prints the binomial noise and an exact
McNemar test on the aligned per-trial results.

The verdict comes from the sliding, baseline-corrected cell at --window, judged per trial
(a trial counts as right when more than half of its sliding windows are: the windows of
one trial overlap and are not independent). TRCA "beats" FBCCA only when it is higher,
clearly above chance (by more than the 95% binomial half-width over the trials), and the
gain is either significant (exact McNemar on those per-trial results, p < 0.05) or larger
than that half-width. Otherwise it says to keep FBCCA, and why.

The live replay (online.Decoder: every window preprocessed on its own with a 2 s pad, as
in the demo) feeds the fold's models each held-out trial and counts the first commit of
--votes agreeing windows: correct, wrong or none, plus commits on its still screen. A
still screen runs from stim_off + latency to the next flicker of any trial (also one that
was dropped as incomplete or unpaired), and after the last one for one trial length, so
no flicker ever reaches a baseline or the still-commit count. --lock replays TRCA a second
time with its templates locked to each trial's onset phase ("locked"), as the demo's --lock
does with the onset the stimulus reports.

Finally a model is fitted on all kept trials and saved to data/models (or --out) for
``python -m brainsync.demo --classifier trca-phase --model <path>``. It is saved whatever the
verdict says; the verdict line says whether to use it, and the "run it" line repeats every
setting the demo must share with the model (--names, --channels, --harmonics) or that the
replay used (--window, --step, --votes, --margin, --threshold) when it is not the default.
Exit status: 0 success, 1 a data error (unreadable or mismatched files, too few trials, an
output that cannot be written), 2 invalid options.
"""
from __future__ import annotations

import argparse
import json
import math
import shlex
import sys
import time
import zipfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import stats

from .classify import ABSTAIN, CCA, FBCCA
from .online import TUNED, Decoder, Voter, _trial_spans, channels_arg, fit_rest, threshold_arg
from .paths import models_dir, unwritable_reason
from .recordings import MONTAGE, POSTERIOR, Recording, load_recording
from .score_cued import trial_windows
from .stimulus import available_configs, load_config
from .trca import N_COMP, TRCA, ModelInfo, _check_fusion, default_model_path

__all__ = ["TrialSet", "CVResult", "load_trials", "assign_folds", "cross_validate", "replay",
           "still_ends", "verdict", "demo_command", "main"]

WINDOWS = (1.0, 1.5, 2.0)
MIN_TRIALS = 4           # per target: an outer fold leaves 3, enough for the inner CV gate
RECOMMENDED_TRIALS = 8
METHODS = ("fbcca", "trca")
LOCKED = "locked"        # the replay's third method with --lock: TRCA at the onset phase
CONDITIONS = ("aligned", "sliding")
PAD_S = 2.0              # the demo's --pad
_EPS = 1e-9


# -- trials ---------------------------------------------------------------------------

@dataclass
class TrialSet:
    """The kept cued trials of one or more recordings, in file order, then time order."""

    recordings: list[Recording]
    on: np.ndarray            # (N, n_ch, n) flicker windows from stim_on + latency
    off: np.ndarray           # (N, n_ch, n) still windows from stim_off + latency
    labels: np.ndarray        # (N,) target index
    file_index: np.ndarray    # (N,) which recording
    on_start: np.ndarray      # (N,) first sample of the flicker window in its recording
    ch_names: tuple[str, ...]
    complete: np.ndarray      # (n_targets,) complete trials per target, before rejection

    @property
    def first(self) -> Recording:
        return self.recordings[0]

    @property
    def freqs(self) -> np.ndarray:
        return self.first.freqs

    @property
    def fs(self) -> float:
        return self.first.fs

    @property
    def n_targets(self) -> int:
        return self.first.n_targets

    @property
    def kept(self) -> np.ndarray:
        """Kept trials per target."""
        return np.bincount(self.labels, minlength=self.n_targets)


def _same(rec: Recording, ref: Recording) -> str | None:
    """The first field in which ``rec`` differs from ``ref``, described; None if none."""
    checks = (("freqs", rec.freqs.shape == ref.freqs.shape
               and np.allclose(rec.freqs, ref.freqs, rtol=0, atol=1e-9),
               rec.freqs.tolist(), ref.freqs.tolist()),
              ("phases", rec.phases.shape == ref.phases.shape
               and np.allclose(rec.phases, ref.phases, rtol=0, atol=1e-9),
               rec.phases.tolist(), ref.phases.tolist()),
              ("fs", rec.fs == ref.fs, rec.fs, ref.fs),
              ("ch_names", rec.ch_names == ref.ch_names, list(rec.ch_names), list(ref.ch_names)),
              ("band", tuple(rec.band) == tuple(ref.band), rec.band, ref.band),
              ("notch", rec.notch == ref.notch, rec.notch, ref.notch),
              ("latency", rec.latency == ref.latency, rec.latency, ref.latency),
              ("window_s", rec.window_s == ref.window_s, rec.window_s, ref.window_s))
    for name, ok, got, want in checks:
        if not ok:
            return f"{name} {got} differ from {want} in {ref.path}"
    return None


def load_trials(paths: Sequence[str | Path], channels: Sequence[str | int] | None = POSTERIOR,
                reject: float = 3.0) -> TrialSet:
    """Load cued recordings that share targets, montage and preprocessing, and cut their
    kept trials. Raises FileNotFoundError, KeyError or ValueError naming the file."""
    if not paths:
        raise ValueError("no recordings given")
    recs: list[Recording] = []
    parts = []
    complete = None
    for j, path in enumerate(paths):
        rec = load_recording(path)
        if rec.kind != "cued" or rec.n_targets < 2:
            raise ValueError(f"{rec.path}: a {rec.kind!r} recording with targets "
                             f"{rec.freqs.tolist()} Hz; training needs a cued recording "
                             f"with at least 2 targets")
        if recs:
            diff = _same(rec, recs[0])
            if diff:
                raise ValueError(f"{rec.path}: {diff}")
        w, _, _ = trial_windows(rec, channels, reject)
        # the same completeness rule as cut_windows, so every kept trial has its span
        spans = _trial_spans(rec, rec.latency)
        counts = np.bincount([t for _, _, t in spans], minlength=rec.n_targets)
        complete = counts if complete is None else complete + counts
        recs.append(rec)
        parts.append((w, j))
    ws = [w for w, _ in parts]
    return TrialSet(
        recordings=recs,
        on=np.concatenate([w.on for w in ws]), off=np.concatenate([w.off for w in ws]),
        labels=np.concatenate([w.labels for w in ws]).astype(int),
        file_index=np.concatenate([np.full(len(w), j) for w, j in parts]).astype(int),
        on_start=np.concatenate([w.on_start for w in ws]).astype(int),
        ch_names=tuple(ws[0].ch_names), complete=np.asarray(complete, dtype=int))


def assign_folds(y: Sequence[int], n_targets: int | None = None) -> tuple[np.ndarray, int]:
    """fold = (rank of the trial among its target's trials, in input order) % n_folds,
    with n_folds the smallest per-target count. Returns (fold per trial, n_folds)."""
    y = np.asarray(y, dtype=int).reshape(-1)
    K = int(y.max()) + 1 if n_targets is None else int(n_targets)
    counts = np.bincount(y, minlength=K)
    n_folds = int(counts.min())
    if n_folds < 2:
        raise ValueError(f"need at least 2 trials per target for folds, got counts "
                         f"{counts.tolist()}")
    rank = np.zeros(y.size, dtype=int)
    for k in range(K):
        rank[y == k] = np.arange(counts[k])
    return rank % n_folds, n_folds


def _starts(n_trial: int, n_w: int, n_step: int) -> np.ndarray:
    """Window starts 0, n_step, ... up to n_trial - n_w (sliding windows inside a trial)."""
    return np.arange(0, n_trial - n_w + 1, n_step)


def _cut(X: np.ndarray, starts: np.ndarray, n_w: int) -> np.ndarray:
    """Windows (n_trials * n_starts, n_ch, n_w), trial-major, from trials X (n_trials, n_ch, n)."""
    if X.shape[0] == 0 or len(starts) == 0:
        return np.empty((0, X.shape[1], n_w))
    return np.stack([X[:, :, s:s + n_w] for s in starts], axis=1).reshape(-1, X.shape[1], n_w)


# -- cross-validation -----------------------------------------------------------------

@dataclass
class Cell:
    """Counts for one (method, window, condition)."""

    n: int = 0             # held-out flicker windows
    raw_hits: int = 0      # argmax of raw scores
    bc_hits: int = 0       # argmax after the rest baseline
    kept: int = 0          # windows the margin / threshold rules let through
    kept_ok: int = 0
    still_n: int = 0       # held-out still windows
    still_go: int = 0      # of which not abstained (false alarms)

    def add(self, other: Cell) -> None:
        for k in vars(self):
            setattr(self, k, getattr(self, k) + getattr(other, k))

    def as_dict(self) -> dict[str, float | int | None]:
        return {**vars(self), "raw": _num(_frac(self.raw_hits, self.n)),
                "bc": _num(_frac(self.bc_hits, self.n)),
                "kept_rate": _num(_frac(self.kept, self.n)),
                "precision": _num(_frac(self.kept_ok, self.kept)),
                "still_rate": _num(_frac(self.still_go, self.still_n))}


@dataclass
class Fold:
    index: int
    held_out: np.ndarray
    weights: tuple[float, float]
    cv_skipped: bool
    fbcca: FBCCA = field(repr=False)
    trca: TRCA = field(repr=False)


@dataclass
class CVResult:
    windows: list[float]
    n_folds: int
    fold: np.ndarray
    folds: list[Fold]
    cells: dict[tuple[str, float, str], Cell]
    # aligned, baseline-corrected correctness per trial (for McNemar), by (method, window)
    per_trial: dict[tuple[str, float], np.ndarray]
    # fraction of each trial's sliding windows right after the baseline, by (method, window)
    per_trial_sliding: dict[tuple[str, float], np.ndarray] = field(default_factory=dict)

    def cell(self, method: str, window_s: float, condition: str) -> Cell:
        return self.cells[(method, float(window_s), condition)]


def _frac(a: int, b: int) -> float:
    return a / b if b else math.nan


def model_info(ts: TrialSet) -> ModelInfo:
    """Where a model fitted on ``ts`` comes from: its channels, preprocessing, the latency
    its windows start at after stim_on, the trial length and the recordings."""
    rec = ts.first
    return ModelInfo(ch_names=ts.ch_names, band=tuple(rec.band) if rec.band else None,
                     notch=float(rec.notch), latency=float(rec.latency),
                     trial_s=float(rec.window_s),
                     sources=tuple(r.path.name if r.path else "" for r in ts.recordings),
                     created=time.time())


def _models(ts: TrialSet, harmonics: int, margin: float, threshold: float | None,
            n_comp: int, fusion: str | tuple[float, float]) -> tuple[FBCCA, TRCA]:
    rec = ts.first
    fb = FBCCA(rec.freqs, rec.fs, harmonics=harmonics, phases=rec.phases, margin=margin,
               threshold=threshold)
    tr = TRCA(rec.freqs, rec.fs, harmonics=harmonics, phases=rec.phases, margin=margin,
              threshold=threshold, n_comp=n_comp, fusion=fusion)
    return fb, tr


def _score(clf: CCA, X: np.ndarray, y: np.ndarray) -> tuple[Cell, np.ndarray]:
    """A Cell's flicker counts for windows X with targets y, and bc correctness per window."""
    raw = clf.raw_scores(X)
    s = raw - clf.baseline_mean
    pred = clf.predict_scores(s)
    go = pred != ABSTAIN
    bc_ok = s.argmax(axis=1) == y
    return Cell(n=int(y.size), raw_hits=int(np.sum(raw.argmax(axis=1) == y)),
                bc_hits=int(bc_ok.sum()), kept=int(go.sum()),
                kept_ok=int(np.sum(pred[go] == y[go]))), bc_ok


def cross_validate(ts: TrialSet, windows: Sequence[float], step_s: float = TUNED.step_s,
                   margin: float = TUNED.margin, threshold: float | None = TUNED.threshold,
                   n_comp: int = N_COMP, harmonics: int = 3,
                   fusion: str | tuple[float, float] = "cv") -> CVResult:
    """Score every kept trial once, held out of the fit and of the baseline (module docstring)."""
    fs = ts.fs
    fold, n_folds = assign_folds(ts.labels, ts.n_targets)
    n_trial = ts.on.shape[-1]
    n_step = max(1, int(round(step_s * fs)))
    wins = [float(w) for w in windows]
    cells = {(m, w, c): Cell() for m in METHODS for w in wins for c in CONDITIONS}
    per_trial = {(m, w): np.zeros(ts.labels.size, dtype=bool) for m in METHODS for w in wins}
    per_slide = {(m, w): np.zeros(ts.labels.size) for m in METHODS for w in wins}
    folds: list[Fold] = []
    for f in range(n_folds):
        test = np.flatnonzero(fold == f)
        train = np.flatnonzero(fold != f)
        fb, tr = _models(ts, harmonics, margin, threshold, n_comp, fusion)
        # the info carries the latency a phase lock counts from (replay --lock)
        tr.fit(ts.on[train], ts.labels[train], info=model_info(ts))
        folds.append(Fold(f, test, tuple(tr.weights), tr.cv_skipped, fb, tr))
        for w in wins:
            n_w = int(round(w * fs))
            starts = _starts(n_trial, n_w, n_step)
            rest = _cut(ts.off[train], starts, n_w)          # training still screens only
            y_slide = np.repeat(ts.labels[test], len(starts))
            first = np.tile(np.arange(len(starts)) == 0, test.size)   # start 0 = aligned
            on_w = _cut(ts.on[test], starts, n_w)
            off_w = _cut(ts.off[test], starts, n_w)
            for name, clf in (("fbcca", fb), ("trca", tr)):
                clf.fit_baseline(rest)
                still = clf.predict(off_w) != ABSTAIN
                for cond, sel in (("aligned", first), ("sliding", np.ones_like(first))):
                    c, bc_ok = _score(clf, on_w[sel], y_slide[sel])
                    c.still_n, c.still_go = int(sel.sum()), int(still[sel].sum())
                    cells[(name, w, cond)].add(c)
                    if cond == "aligned":
                        per_trial[(name, w)][test] = bc_ok
                    else:                                    # trial-major, as _cut cuts
                        per_slide[(name, w)][test] = bc_ok.reshape(test.size, -1).mean(axis=1)
    return CVResult(wins, n_folds, fold, folds, cells, per_trial, per_slide)


def mcnemar_p(a_only: int, b_only: int) -> float:
    """Exact two-sided McNemar p from the discordant pair counts."""
    n = a_only + b_only
    if n == 0:
        return 1.0
    return float(min(1.0, 2.0 * stats.binom.cdf(min(a_only, b_only), n, 0.5)))


# -- live replay ----------------------------------------------------------------------

@dataclass
class ReplayCount:
    n: int = 0             # held-out trials replayed
    correct: int = 0
    wrong: int = 0
    still_n: int = 0       # still screens after them
    still_commits: int = 0

    @property
    def none(self) -> int:
        return self.n - self.correct - self.wrong


def still_ends(rec: Recording, spans: Sequence[tuple[int, int, int]]) -> list[int]:
    """Where each flicker span's still screen ends: at the next flicker of any trial.

    The boundaries are every stim_on + latency in the markers, also of trials that
    _trial_spans dropped (incomplete, or a stim_on left unpaired by Esc or Ctrl-C), so a
    still span never covers a flicker. After the last flicker the still screen lasts one
    trial length (rec.window_s), so the end of the session is not counted.
    """
    tol = 0.01 / rec.fs
    n_total = rec.X.shape[1]
    n_win = int(round(rec.window_s * rec.fs))
    ons = np.sort([int(np.searchsorted(rec.t, stamp + rec.latency - tol))
                   for stamp, kind, _ in rec.markers if kind == "stim_on"]).astype(int)
    ends = []
    for a, b, _ in spans:
        k = int(np.searchsorted(ons, a, side="right"))      # the first flicker after this one
        end = int(ons[k]) if k < ons.size else b + n_win
        ends.append(min(end, n_total))
    return ends


def _inside(dec: Decoder, a: int, b: int) -> list[int]:
    """Segment starts whose window (after the pad) lies wholly inside samples a..b."""
    return [int(s) for s in dec.segment_starts(b, max(0, a - dec.n_pad)) if s + dec.n_pad >= a]


def _tally(c: ReplayCount, got: int, label: int, still_got: int | None) -> None:
    """Count one held-out trial: its first flicker commit ``got`` (ABSTAIN if none) against
    ``label``, and its still screen's first commit ``still_got`` (None: no still windows)."""
    c.n += 1
    c.correct += int(got == label)
    c.wrong += int(got != ABSTAIN and got != label)
    if still_got is not None:
        c.still_n += 1
        c.still_commits += int(still_got != ABSTAIN)


def _first_commit(pred: np.ndarray, votes: int) -> int:
    voter = Voter(votes)
    for p in pred:
        if voter.push(int(p)) and voter.committed != ABSTAIN:
            return voter.committed
    return ABSTAIN


def replay(ts: TrialSet, cv: CVResult, window_s: float = TUNED.window_s,
           step_s: float = TUNED.step_s, votes: int = TUNED.votes,
           channels: Sequence[str | int] | None = POSTERIOR,
           pad_s: float = PAD_S, lock: bool = False) -> list[dict[str, ReplayCount]]:
    """Per recording, {method: ReplayCount} for the live decoder on the held-out trials.

    Each fold's models get their baseline from the still spans (stim_off + latency to the
    next flicker, see still_ends) of that fold's training trials, in every file, with
    windows prepared the decoder's way; each held-out trial is fed from scratch (fresh
    votes). Artifact-rejected trials are skipped. With ``lock`` a third method, "locked",
    replays the fold's TRCA with its templates locked to the trial's onset phase (each
    window's start time since stim_on, as the demo's --lock gets it from the stimulus),
    its baseline fitted at random phases like the demo's.
    """
    proto = cv.folds[0]
    methods = (*METHODS, LOCKED) if lock else METHODS
    # windows depend only on the decoder settings, so cut each trial's once for all folds;
    # with them each window's start in seconds since the trial's stim_on, for the lock
    flick: list[np.ndarray] = [np.empty(0)] * ts.labels.size
    still: list[np.ndarray] = [np.empty(0)] * ts.labels.size
    flick_t: list[np.ndarray] = [np.empty(0)] * ts.labels.size
    still_t: list[np.ndarray] = [np.empty(0)] * ts.labels.size
    for j, rec in enumerate(ts.recordings):
        dec = Decoder(proto.fbcca, rec.fs, window_s=window_s, step_s=step_s, votes=votes,
                      channels=channels, pad_s=pad_s, band=rec.band, notch_hz=rec.notch,
                      ch_names=rec.ch_names)
        spans = _trial_spans(rec, rec.latency)
        ends = still_ends(rec, spans)
        by_start = {a: k for k, (a, _, _) in enumerate(spans)}

        def since_onset(starts: list[int], a: int) -> np.ndarray:
            return (np.asarray(starts, dtype=float) + dec.n_pad - a) / rec.fs + rec.latency

        for i in np.flatnonzero(ts.file_index == j):
            k = by_start.get(int(ts.on_start[i]))
            if k is None:
                raise ValueError(f"{rec.path}: kept trial at sample {ts.on_start[i]} has no "
                                 f"stim_on/stim_off span")
            a, b, _ = spans[k]
            on_starts, off_starts = _inside(dec, a, b), _inside(dec, b, ends[k])
            flick[i] = dec.windows(rec.X, on_starts)
            still[i] = dec.windows(rec.X, off_starts)
            flick_t[i], still_t[i] = since_onset(on_starts, a), since_onset(off_starts, a)
    out = [{m: ReplayCount() for m in methods} for _ in ts.recordings]
    for fold in cv.folds:
        test = set(fold.held_out.tolist())
        train = [i for i in range(ts.labels.size) if i not in test]
        found = [still[i] for i in train if still[i].shape[0]]
        if not found:
            raise ValueError(f"fold {fold.index}: no still-screen windows of "
                             f"{window_s:g} s after the training trials for the baseline")
        rest = np.concatenate(found, axis=0)
        runs = [("fbcca", fold.fbcca), ("trca", fold.trca)]
        if lock:
            runs.append((LOCKED, fold.trca))        # after "trca": it refits the baseline
        for name, clf in runs:
            locked = name == LOCKED
            dec = Decoder(clf, ts.fs, window_s=window_s, step_s=step_s, votes=votes,
                          channels=channels, pad_s=pad_s, band=ts.first.band,
                          notch_hz=ts.first.notch, ch_names=ts.first.ch_names, lock=locked)
            fit_rest(dec, rest)

            def predict(X: np.ndarray, t: np.ndarray) -> np.ndarray:
                return clf.decide(X, psi=clf.phase_offsets(t))[0] if locked else clf.predict(X)

            for i in fold.held_out:
                got = _first_commit(predict(flick[i], flick_t[i]), votes) \
                    if flick[i].shape[0] else ABSTAIN
                still_got = _first_commit(predict(still[i], still_t[i]), votes) \
                    if still[i].shape[0] else None
                _tally(out[int(ts.file_index[i])][name], got, int(ts.labels[i]), still_got)
    return out


# -- report ---------------------------------------------------------------------------

def _pct(x: float, width: int = 4) -> str:
    return f"{'-':>{width}}" if not np.isfinite(x) else f"{100 * x:{width - 1}.0f}%"


def _pair(w: tuple[float, float]) -> str:
    return f"({w[0]:g},{w[1]:g})"


def _config_for(freqs: np.ndarray) -> str | None:
    """A shipped stimulus config whose targets are ``freqs`` in this order."""
    found = []
    for name in available_configs():
        try:
            cfg = load_config(name)
        except (ValueError, KeyError, OSError):
            continue
        if cfg.freqs.shape == freqs.shape and np.allclose(cfg.freqs, freqs, atol=1e-6):
            found.append(name)
    found.sort(key=lambda n: (not n.endswith("_targets"), n))
    return found[0] if found else None


def _half_width(p: float, n: int) -> float:
    """95% binomial half-width of an accuracy p over n trials, as a fraction; p is kept
    one trial away from 0 and 1 so a perfect score still shows its noise."""
    q = min(max(p, 1 / n), 1 - 1 / n) if np.isfinite(p) else 0.5
    return 1.96 * math.sqrt(q * (1 - q) / n)


def verdict(ts: TrialSet, cv: CVResult, live_window: float) -> dict:
    """Whether TRCA reliably beats FBCCA on the sliding, baseline-corrected cell at
    ``live_window`` (module docstring), with the numbers behind it and the verdict line."""
    w = float(live_window)
    fb_s, tr_s = cv.cell("fbcca", w, "sliding"), cv.cell("trca", w, "sliding")
    x, y = _frac(tr_s.bc_hits, tr_s.n), _frac(fb_s.bc_hits, fb_s.n)
    n, chance = int(ts.labels.size), 1 / ts.n_targets
    half = _half_width(x, n)
    # per trial: right when more than half of its sliding windows are
    t_ok = cv.per_trial_sliding[("trca", w)] > 0.5
    f_ok = cv.per_trial_sliding[("fbcca", w)] > 0.5
    t_only, f_only = int(np.sum(t_ok & ~f_ok)), int(np.sum(f_ok & ~t_ok))
    pv = mcnemar_p(t_only, f_only)
    evidence = (f"McNemar per trial p={pv:.2f}, trca only {t_only}, fbcca only {f_only}; "
                f"noise ±{100 * half:.0f} points")
    if not (np.isfinite(x) and np.isfinite(y)) or x <= y:
        reason = "not higher"
    elif x - chance <= half:
        reason = (f"trca is not clearly above the {100 * chance:.0f}% chance "
                  f"(±{100 * half:.0f} points)")
    elif pv >= 0.05 and x - y <= half:
        reason = "no reliable difference"
    else:
        reason = ""
    beats = not reason
    xs, ys = _pct(x).strip(), _pct(y).strip()
    if beats:
        text = f"trca beats fbcca on held-out windows: {xs} vs {ys} ({evidence})"
    else:
        text = (f"trca does not beat fbcca on held-out windows ({xs} vs {ys}; {reason}; "
                f"{evidence}): keep --classifier fbcca")
    return {"window_s": w, "condition": "sliding", "trca_bc": _num(x), "fbcca_bc": _num(y),
            "chance": chance, "ci95_points": 100 * half, "trca_only": t_only,
            "fbcca_only": f_only, "mcnemar_p": pv, "trca_beats": beats,
            "reason": reason or None, "text": text}


def report_lines(ts: TrialSet, cv: CVResult, live_window: float,
                 fusion: str | tuple[float, float]) -> tuple[list[str], dict]:
    """The text report of the cross-validation, and the same numbers for --json."""
    names = [p.path.name if p.path else "<recording>" for p in ts.recordings]
    hz = "/".join(f"{f:g}" for f in ts.freqs)
    per = ", ".join(f"{f:g} Hz {k}/{c}" for f, k, c in zip(ts.freqs, ts.kept, ts.complete))
    fus = '"cv"' if fusion == "cv" else f"fixed {_pair(fusion)}"   # type: ignore[arg-type]
    lines = [f"train: {len(names)} file(s) ({', '.join(names)}); targets {hz} Hz; "
             f"channels {','.join(ts.ch_names)}",
             f"trials kept/complete per target: {per}; {cv.n_folds} folds (per-target rank "
             f"% {cv.n_folds}); chance {100 / ts.n_targets:.0f}%; fusion {fus}",
             "held out: every trial once; models and rest baselines fitted on the other "
             "folds only",
             f"{'window':>6} {'cond':<7} {'n':>4} | {'fbcca raw':>9} {'bc':>4} {'kept':>4} "
             f"{'prec':>4} {'still':>5} | {'trca raw':>8} {'bc':>4} {'kept':>4} {'prec':>4} "
             f"{'still':>5} | {'bc diff':>7}"]
    cells_json = []
    for w in cv.windows:
        for cond in CONDITIONS:
            fb, tr = cv.cell("fbcca", w, cond), cv.cell("trca", w, cond)
            d = _frac(tr.bc_hits, tr.n) - _frac(fb.bc_hits, fb.n)
            parts = []
            for c, width in ((fb, 9), (tr, 8)):
                parts.append(f"{_pct(_frac(c.raw_hits, c.n), width)} {_pct(_frac(c.bc_hits, c.n))} "
                             f"{_pct(_frac(c.kept, c.n))} {_pct(_frac(c.kept_ok, c.kept))} "
                             f"{_pct(_frac(c.still_go, c.still_n), 5)}")
            lines.append(f"{w:4g} s {cond:<7} {fb.n:>4} | {parts[0]} | {parts[1]} | "
                         f"{100 * d:+6.1f}")
            cells_json.append({"window_s": w, "condition": cond, "n": fb.n,
                               "fbcca": fb.as_dict(), "trca": tr.as_dict(),
                               "bc_diff": _num(d)})
    lines.append("raw / bc: forced-choice accuracy without / with the rest baseline; kept, "
                 "prec: windows the margin and threshold let through, and how many of those "
                 "were right; still: held-out still windows let through (false alarms)")

    n = int(ts.labels.size)
    ref = cv.cell("trca", live_window, "aligned")
    p = _frac(ref.bc_hits, ref.n)
    half = 100 * _half_width(p, n)
    lines.append(f"noise: one trial = {100 / n:.1f} points; 95% interval about ±{half:.0f} "
                 f"points at this n ({n} trials, trca aligned {live_window:g} s at "
                 f"{_pct(p).strip()})")
    mc = []
    parts = []
    for w in cv.windows:
        a, b = cv.per_trial[("trca", w)], cv.per_trial[("fbcca", w)]
        t_only, f_only = int(np.sum(a & ~b)), int(np.sum(b & ~a))
        pv = mcnemar_p(t_only, f_only)
        mc.append({"window_s": w, "trca_only": t_only, "fbcca_only": f_only, "p": pv})
        parts.append(f"{w:g} s p={pv:.2f} (trca only {t_only}, fbcca only {f_only})")
    lines.append("McNemar, aligned, baseline-corrected, per trial: " + "; ".join(parts))
    chosen = Counter(f.weights for f in cv.folds)
    skipped = sum(f.cv_skipped for f in cv.folds)
    lines.append("fusion chosen per fold: " + ", ".join(
        f"{_pair(w)} x{c}" for w, c in sorted(chosen.items(), key=lambda kv: (-kv[1], kv[0])))
        + (f" (CV gate skipped in {skipped}: too few trials)" if skipped else ""))

    data = {
        "files": [str(r.path) for r in ts.recordings], "freqs": ts.freqs.tolist(),
        "channels": list(ts.ch_names), "kept": ts.kept.tolist(), "complete": ts.complete.tolist(),
        "n_trials": n, "n_folds": cv.n_folds, "chance": 1 / ts.n_targets,
        "fusion": fusion if fusion == "cv" else list(fusion),   # type: ignore[arg-type]
        "cells": cells_json,
        "noise": {"one_trial_points": 100 / n, "ci95_points": half},
        "mcnemar": mc,
        "folds": [{"fold": f.index, "held_out": f.held_out.tolist(),
                   "weights": list(f.weights), "cv_skipped": f.cv_skipped} for f in cv.folds],
        "verdict": verdict(ts, cv, live_window),
    }
    return lines, data


def _plain(x: object) -> object:
    """json.dumps fallback for numpy scalars and arrays."""
    if isinstance(x, (np.generic, np.ndarray)):
        return x.tolist()
    raise TypeError(f"not JSON serializable: {type(x).__name__}")


def _num(x: float) -> float | None:
    return float(x) if np.isfinite(x) else None


def _replay_lines(ts: TrialSet, counts: list[dict[str, ReplayCount]],
                  window_s: float) -> tuple[list[str], list[dict]]:
    lines, data = [], []
    methods = tuple(counts[0]) if counts else METHODS
    rows = list(zip([r.path.name if r.path else "<recording>" for r in ts.recordings], counts))
    if len(rows) > 1:
        total = {m: ReplayCount() for m in methods}
        for _, c in rows:
            for m in methods:
                for k in vars(total[m]):
                    setattr(total[m], k, getattr(total[m], k) + getattr(c[m], k))
        rows.append(("all files", total))
    for name, c in rows:
        text = "; ".join(f"{m} correct {c[m].correct}/{c[m].n} wrong {c[m].wrong}, still "
                         f"commits {c[m].still_commits}/{c[m].still_n}" for m in methods)
        prefix = f"{name}: " if len(rows) > 1 else ""
        lines.append(f"replay window {window_s:g} s: {prefix}{text}")
        data.append({"file": name, **{m: {**vars(c[m]), "none": c[m].none} for m in methods}})
    return lines, data


# -- CLI ------------------------------------------------------------------------------

def fusion_arg(text: str) -> str | tuple[float, float]:
    """--fusion: "cv", "trca" (1:0), "fbcca" (0:1) or "A:B" (a_trca:a_cca)."""
    t = text.strip().lower()
    named = {"cv": "cv", "trca": (1.0, 0.0), "fbcca": (0.0, 1.0)}
    if t in named:
        return named[t]
    parts = t.split(":")
    try:
        if len(parts) != 2:
            raise ValueError
        return _check_fusion((float(parts[0]), float(parts[1])))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected cv, trca, fbcca or A:B with A, B >= 0 not both 0, got {text!r}") from None


def _floats(text: str) -> list[float]:
    try:
        out = [float(v) for v in text.split(",") if v.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated seconds, got {text!r}") from None
    if not out or not all(np.isfinite(v) and v > 0 for v in out):
        raise argparse.ArgumentTypeError(f"window lengths must be positive, got {text!r}")
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.train", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", type=Path, help="*_cued.npz recordings of one setup")
    p.add_argument("--channels", type=channels_arg, default=list(POSTERIOR),
                   help="analysis channels, names or indices (default " + ",".join(POSTERIOR) + ")")
    p.add_argument("--windows", type=_floats, default=list(WINDOWS),
                   help="held-out window lengths in s, comma-separated (default 1,1.5,2; "
                        "--window is always added)")
    p.add_argument("--step", type=float, default=TUNED.step_s,
                   help=f"sliding hop and still-window hop in s (default {TUNED.step_s:g})")
    p.add_argument("--window", type=float, default=TUNED.window_s,
                   help=f"live window for the replay and the verdict (default {TUNED.window_s:g})")
    p.add_argument("--votes", type=int, default=TUNED.votes,
                   help=f"replay: agreeing windows to commit (default {TUNED.votes})")
    p.add_argument("--margin", type=float, default=TUNED.margin,
                   help=f"abstain margin in rest spreads (default {TUNED.margin:g})")
    p.add_argument("--threshold", type=threshold_arg, default=TUNED.threshold,
                   help=f"abstain level in rest spreads, or 'none' (default {TUNED.threshold})")
    p.add_argument("--reject", type=float, default=3.0,
                   help="drop trials with RMS above this x median (0 keeps all; default 3)")
    p.add_argument("--n-comp", type=int, default=N_COMP,
                   help=f"TRCA filters per target (default {N_COMP})")
    p.add_argument("--harmonics", type=int, default=3, help="template harmonics (default 3)")
    p.add_argument("--fusion", type=fusion_arg, default="cv",
                   help="cv (inner CV gate, default), trca, fbcca, or A:B weights")
    p.add_argument("--out", type=Path, default=None,
                   help="model path (.npz; default data/models/<stamp>_trca.npz)")
    p.add_argument("--no-save", action="store_true", help="evaluate only, save no model")
    p.add_argument("--no-replay", action="store_true", help="skip the live replay")
    p.add_argument("--lock", action="store_true",
                   help="replay TRCA also phase-locked to each trial's onset (the demo's "
                        "--lock), and put --lock in the run-it line")
    p.add_argument("--json", type=Path, default=None, help="write every number here as JSON")
    return p


def _check(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for flag, value in (("--step", args.step), ("--window", args.window)):
        if not (np.isfinite(value) and value > 0):
            p.error(f"{flag} must be positive, got {value}")
    if args.votes < 1:
        p.error(f"--votes must be >= 1, got {args.votes}")
    if not (np.isfinite(args.margin) and args.margin >= 0):
        p.error(f"--margin must be >= 0, got {args.margin}")
    if not (np.isfinite(args.reject) and args.reject >= 0):
        p.error(f"--reject must be >= 0, got {args.reject}")
    if args.n_comp < 1 or args.harmonics < 1:
        p.error(f"--n-comp and --harmonics must be >= 1, got {args.n_comp}, {args.harmonics}")
    if args.out is not None and args.out.suffix != ".npz":
        p.error(f"--out must end in .npz, got {args.out}")
    if args.out is not None and args.no_save:
        p.error("--out and --no-save contradict each other")


def _windows(requested: Sequence[float], live: float, trial_s: float) -> list[float]:
    fit = sorted({round(float(w), 9) for w in [*requested, live]})
    too_long = [w for w in fit if w > trial_s + _EPS]
    if live > trial_s + _EPS:
        raise ValueError(f"--window {live:g} s is longer than the {trial_s:g} s trials")
    if too_long:
        print(f"note: skipping windows {', '.join(f'{w:g} s' for w in too_long)}, longer "
              f"than the {trial_s:g} s trials", file=sys.stderr)
    return [w for w in fit if w <= trial_s + _EPS]


def demo_command(args: argparse.Namespace, ts: TrialSet, cfg: str, path: Path) -> str:
    """The demo command line for a saved model: the settings it must share with the model
    (board channel names, analysis channels, harmonics) and those the replay used, each
    only when it differs from the demo's default."""
    rec = ts.first
    out = ["python", "-m", "brainsync.demo", "-c", cfg, "--classifier", "trca-phase",
           "--model", str(path)]
    if tuple(rec.ch_names) != tuple(MONTAGE):
        out += ["--names", ",".join(rec.ch_names)]
    if tuple(ts.ch_names) != tuple(POSTERIOR):
        out += ["--channels", ",".join(ts.ch_names)]
    for flag, value, default in (("--harmonics", args.harmonics, 3),
                                 ("--window", args.window, TUNED.window_s),
                                 ("--step", args.step, TUNED.step_s),
                                 ("--votes", args.votes, TUNED.votes),
                                 ("--margin", args.margin, TUNED.margin)):
        if value != default:
            out += [flag, f"{value:g}"]
    if args.threshold != TUNED.threshold:
        out += ["--threshold", "none" if args.threshold is None else f"{args.threshold:g}"]
    if getattr(args, "lock", False):
        out.append("--lock")
    return " ".join(shlex.quote(part) for part in out)


def _check_writable(args: argparse.Namespace) -> None:
    """Fail before the cross-validation, not after it, when an output cannot be written."""
    dirs = [] if args.no_save else [args.out.parent if args.out is not None else models_dir()]
    if args.json is not None:
        dirs.append(args.json.parent)
    for d in dirs:
        reason = unwritable_reason(d)
        if reason:
            raise ValueError(f"cannot write to {d}: {reason}")


def run(args: argparse.Namespace) -> int:
    """Everything after the options are checked; data errors raise (main prints them)."""
    _check_writable(args)
    ts = load_trials(args.files, args.channels, args.reject)
    low = [(f, c) for f, c in zip(ts.freqs, ts.kept) if c < MIN_TRIALS]
    if low:
        f, c = low[0]
        raise ValueError(f"target {f:g} Hz has {c} kept trials; record at least {MIN_TRIALS} "
                         f"({RECOMMENDED_TRIALS} recommended) per target")
    trial_s = ts.on.shape[-1] / ts.fs
    wins = _windows(args.windows, args.window, trial_s)
    print(f"cross-validating {ts.labels.size} trials...", file=sys.stderr)
    cv = cross_validate(ts, wins, args.step, args.margin, args.threshold, args.n_comp,
                        args.harmonics, args.fusion)
    lines, data = report_lines(ts, cv, float(round(args.window, 9)), args.fusion)
    verdict_line = data["verdict"]["text"]
    if not args.no_replay:
        print("replaying through the live decoder...", file=sys.stderr)
        counts = replay(ts, cv, args.window, args.step, args.votes, args.channels,
                        lock=args.lock)
        more, data["replay"] = _replay_lines(ts, counts, args.window)
        lines += more
    else:
        data["replay"] = None

    rec = ts.first
    info = model_info(ts)
    _, model = _models(ts, args.harmonics, 0.0, None, args.n_comp, args.fusion)
    model.fit(ts.on, ts.labels, info=info)
    table = ", ".join(f"{_pair(k)} {100 * v:.0f}%" for k, v in model.cv_table.items())
    lines.append(f"final model on all {ts.labels.size} trials: fusion {_pair(model.weights)}"
                 + (f" (inner CV accuracy {table})" if table else "")
                 + (" (CV gate skipped: too few trials)" if model.cv_skipped else ""))
    path = None
    if args.no_save:
        lines.append("model not saved (--no-save)")
    else:
        path = model.save(args.out if args.out is not None else default_model_path())
        cfg = _config_for(ts.freqs) or (f"<config with targets "
                                        f"{'/'.join(f'{f:g}' for f in ts.freqs)} Hz in this order>")
        lines.append(f"model saved: {path}")
        lines.append(f"run it: {demo_command(args, ts, cfg, path)}")
        if (tuple(rec.band) if rec.band else None) != (1.0, 100.0) or float(rec.notch) != 60.0:
            lines.append(f"note: the demo filters 1-100 Hz with a 60 Hz notch; this model's "
                         f"recordings used {rec.band} Hz and {rec.notch:g} Hz, so the demo "
                         f"will refuse it")
    lines.append(verdict_line)
    data["model"] = {"path": None if path is None else str(path),
                     "weights": list(model.weights),
                     "cv_table": [{"weights": list(k), "accuracy": v}
                                  for k, v in model.cv_table.items()],
                     "cv_skipped": model.cv_skipped}
    data["settings"] = {"windows": wins, "step_s": args.step, "window_s": args.window,
                        "votes": args.votes, "margin": args.margin, "threshold": args.threshold,
                        "reject": args.reject, "n_comp": args.n_comp,
                        "harmonics": args.harmonics, "lock": args.lock}
    print("\n".join(lines))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(data, indent=2, allow_nan=False, default=_plain) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    _check(p, args)
    try:
        return run(args)
    # OSError covers unreadable and unwritable files; EOFError and BadZipFile are an empty
    # or truncated recording
    except (OSError, EOFError, zipfile.BadZipFile, KeyError, ValueError,
            np.linalg.LinAlgError) as exc:
        msg = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
