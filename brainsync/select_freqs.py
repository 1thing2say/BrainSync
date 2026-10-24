"""Phase-0 frequency selection: which flicker frequencies does this user's EEG follow?

    python -m brainsync.select_freqs                     # every *_trials.npz in recordings_dir()
    python -m brainsync.select_freqs path/a_trials.npz path/b_trials.npz --json

Each single-target recording holds cued trials at one frequency f0, with a still square
between trials. Per recording the report gives:

* CCA wins: how often the f0 templates beat control templates (default f0-3 and f0+3 Hz)
  on the posterior flicker windows, with a one-sided binomial p against 1/(1 + controls);
* the same test on the still windows. A resting rhythm at f0 (the user's alpha at 10 Hz)
  wins there too, which makes the frequency useless however well the flicker windows score;
* the SNR at f0 of the coherent average (flicker windows averaged from onset);
* the phase-locking value at f0 across trials on the best posterior channel, with a
  Rayleigh p corrected for picking the best of the channels.

Notes flag what a verdict hides: a control on a resting rhythm that masks f0, or a run
that fails the CCA test although its f0 phase locks to the flicker.

Runs shown at the wrong refresh rate (ProMotion) and protocol controls (eyes closed) are
listed but kept out of the verdicts and the pair table. The pair table takes each usable
frequency, pools every eligible run at it (not only the runs that passed the f0 test,
which would score the pairs on windows selected by their own outcome), scores 2-class CCA
with a still-window baseline on one window per trial, and adds the multi-class problem
over all usable frequencies for CCA and FBCCA. The same scores pooled over the usable runs
only and over the original analysis's three runs are printed beside it (--pool picks the
headline).

The pooled classes differ in size, so pairs are ranked by balanced accuracy with a
label-permutation p, plain accuracy is shown beside the rate of always naming the largest
class, and a bootstrap interval says whether the best pair's lead over the runner-up is
resolved.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal as sps

from .classify import CCA, make
from .metrics import accuracy, balanced_accuracy, binom_p
from .recordings import (POSTERIOR, Recording, Windows, cut_windows, list_recordings,
                         load_recording, reject_artifacts)

DISPLAY_HZ = 60.0          # the demo laptop's locked refresh rate
REFRESH_TOL = 1.0          # Hz; a measured refresh further from DISPLAY_HZ excludes the run
CONTROL_OFFSETS = (-3.0, 3.0)

# Runs recorded before ProMotion was switched off. Their logs also report ~120 Hz, which
# excludes them on its own; the table keeps the reason when a log lacks the refresh line.
KNOWN_EXCLUDED: dict[str, str] = {
    "2026-10-20_095822": "ProMotion on (120 Hz display)",
    "2026-10-20_101802": "ProMotion on (120 Hz display)",
    "2026-10-22_114046": "ProMotion on (120 Hz display)",
    "2026-10-22_115139": "ProMotion on (120 Hz display)",
}
# Protocol controls: reported, never pooled or judged.
KNOWN_CONTROLS: dict[str, str] = {
    "2026-10-22_151304": "eyes closed throughout (expect ~0 CCA wins)",
}
# What the original phase-0 analysis printed, for side-by-side comparison.
ORIGINAL: dict[str, str] = {
    "2026-10-22_121914": "12/16",
    "2026-10-22_123700": "10/18",
    "2026-10-22_145437": "13/20 (at latency 0)",   # --latency 0 --reject 0 gives 13/20; 7/18 at 0.14 s
    "2026-10-22_145858": "12/17, still 13/17",
    "2026-10-22_150328": "15/17",
    "2026-10-22_150837": "12/18",
}

USABLE, REJECTED, NO_RESPONSE, EXCLUDED, CONTROL = (
    "usable", "rejected", "no response", "excluded", "control")

_REFRESH_RE = re.compile(r"measured refresh:\s*([0-9]+(?:\.[0-9]*)?)\s*Hz", re.IGNORECASE)


def run_id(path: str | Path) -> str:
    """'2026-10-22_121914' from '.../2026-10-22_121914_trials.npz'."""
    stem = Path(path).stem
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def measured_refresh(marker_text: str) -> float | None:
    """The refresh rate the stimulus logged, or None if the log has no such line."""
    m = _REFRESH_RE.search(marker_text or "")
    return float(m.group(1)) if m else None


# -- spectral measures -----------------------------------------------------------------

SNR_GUARD = 1      # bins skipped on each side of the f0 bin (Hann main-lobe leakage)
SNR_SIDE = 3       # flanking bins per side that estimate the background


def peak_snr_db(power: np.ndarray, grid: np.ndarray, f0: float, guard: int = SNR_GUARD,
                side: int = SNR_SIDE) -> np.ndarray:
    """10 log10 of the f0 bin over its flanking bins, along the last axis of ``power``.

    The flanks are bins guard+1 .. guard+side away from the f0 bin on both sides; flank
    bins past either end of the grid are dropped.
    """
    grid = np.asarray(grid, dtype=float)
    power = np.asarray(power, dtype=float)
    if guard < 0 or side < 1:
        raise ValueError(f"need guard >= 0 and side >= 1, got guard={guard}, side={side}")
    if grid.ndim != 1 or grid.size < 2 or power.shape[-1] != grid.size:
        raise ValueError(f"grid of {grid.size} bins does not match power shaped {power.shape}")
    step = grid[1] - grid[0]
    centre = int(round((f0 - grid[0]) / step))
    if not 0 <= centre < grid.size:
        raise ValueError(f"{f0} Hz lies outside the spectrum ({grid[0]:g}..{grid[-1]:g} Hz)")
    offsets = np.arange(guard + 1, guard + side + 1)
    flank = np.concatenate([centre - offsets, centre + offsets])
    flank = flank[(flank >= 0) & (flank < grid.size)]
    if flank.size == 0:
        raise ValueError(f"no flanking bins around {f0} Hz on a {grid.size}-bin grid")
    background = np.take(power, flank, axis=-1).mean(axis=-1)
    peak = np.take(power, centre, axis=-1)
    out = np.full(np.shape(peak), np.nan)
    good = (background > 0) & (peak > 0)
    out[good] = 10.0 * np.log10(np.asarray(peak)[good] / np.asarray(background)[good])
    return out


def coherent_snr(on: np.ndarray, fs: float, f0: float) -> np.ndarray:
    """SNR (dB) at f0 of the onset-aligned average of flicker windows (n_trials, n_ch, n) -> (n_ch,)."""
    on = np.asarray(on, dtype=float)
    if on.ndim != 3 or on.shape[0] == 0:
        raise ValueError(f"need flicker windows shaped (n_trials>0, n_ch, n), got {on.shape}")
    avg = on.mean(axis=0)
    grid, power = sps.periodogram(avg, fs=fs, window="hann", axis=-1)
    return peak_snr_db(power, grid, f0)


def phase_locking(on: np.ndarray, fs: float, f0: float,
                  lag: np.ndarray | None = None) -> np.ndarray:
    """|mean unit phasor| of each trial's f0 Fourier coefficient, per channel -> (n_ch,).

    ``lag`` (n_trials,) is the time from stim_on to each window's first sample, so phases
    are measured against the flicker onset rather than the sample grid.
    """
    on = np.asarray(on, dtype=float)
    if on.ndim != 3 or on.shape[0] == 0:
        raise ValueError(f"need flicker windows shaped (n_trials>0, n_ch, n), got {on.shape}")
    n_trials, _, n = on.shape
    lag = np.zeros(n_trials) if lag is None else np.asarray(lag, dtype=float).reshape(-1)
    if lag.size != n_trials:
        raise ValueError(f"need one lag per trial ({n_trials}), got {lag.size}")
    tau = lag[:, None] + np.arange(n)[None, :] / fs                 # (trials, n)
    kernel = np.exp(-2j * np.pi * f0 * tau)
    coef = np.einsum("tcn,tn->tc", on - on.mean(axis=-1, keepdims=True), kernel)
    mag = np.abs(coef)
    unit = np.divide(coef, mag, out=np.zeros_like(coef), where=mag > 0)
    return np.abs(unit.mean(axis=0))


def rayleigh_p(plv: float, n: int, n_channels: int = 1) -> float:
    """Rayleigh-test p that ``n`` random phases reach ``plv`` (Zar's approximation),
    Bonferroni-corrected when ``plv`` is the best of ``n_channels`` channels."""
    if n < 1 or n_channels < 1:
        raise ValueError(f"need n >= 1 and n_channels >= 1, got n={n}, n_channels={n_channels}")
    if not np.isfinite(plv):
        return float("nan")
    r = n * float(plv)
    p = float(np.exp(np.sqrt(1 + 4 * n + 4 * (n * n - r * r)) - (1 + 2 * n)))
    return float(min(1.0, max(0.0, p) * n_channels))


def onset_lags(rec: Recording, starts: np.ndarray, latency: float) -> np.ndarray:
    """Seconds from the matching stim_on marker to each window start sample."""
    ons = np.sort([s for s, kind, _ in rec.markers if kind == "stim_on"])
    starts = np.asarray(starts, dtype=int)
    if ons.size == 0 or starts.size == 0:
        return np.zeros(starts.size)
    t0 = rec.t[starts]
    k = np.searchsorted(ons, t0 - latency + 0.5 / rec.fs, side="right") - 1
    return t0 - ons[np.clip(k, 0, None)]


# -- per-run analysis ------------------------------------------------------------------

@dataclass
class RunResult:
    """Phase-0 numbers and verdict for one single-target recording."""

    run: str
    path: Path | None
    f0: float
    refresh: float | None
    n_total: int
    n_clean: int
    controls: tuple[float, ...]
    wins: int
    p_wins: float
    still_wins: int
    p_still: float
    snr_db: float
    snr_channel: str
    plv: float
    plv_channel: str
    plv_p: float = float("nan")
    control_wins: tuple[int, ...] = ()        # flicker windows won by each control
    control_still_wins: tuple[int, ...] = ()  # still windows won by each control
    status: str = ""
    reason: str = ""
    original: str = ""
    notes: tuple[str, ...] = ()
    windows: Windows | None = field(default=None, repr=False)

    @property
    def chance(self) -> float:
        return 1.0 / (1 + len(self.controls))

    @property
    def eligible(self) -> bool:
        """Counts towards verdicts and pairs."""
        return self.status not in (EXCLUDED, CONTROL)

    def as_dict(self) -> dict:
        keys = ("run", "f0", "refresh", "n_total", "n_clean", "controls", "wins", "p_wins",
                "still_wins", "p_still", "snr_db", "snr_channel", "plv", "plv_channel",
                "plv_p", "control_wins", "control_still_wins", "status", "reason", "original",
                "notes")
        out = {k: getattr(self, k) for k in keys}
        out["path"] = str(self.path) if self.path else None
        for k in ("controls", "control_wins", "control_still_wins", "notes"):
            out[k] = list(out[k])
        out["chance"] = self.chance
        return {k: _finite_or_none(v) for k, v in out.items()}


def _finite_or_none(v):
    """NaN/inf floats become None so the JSON report stays valid JSON."""
    if isinstance(v, (float, np.floating)):
        return float(v) if np.isfinite(v) else None
    return v


def judge(wins: int, still_wins: int, n: int, chance: float,
          alpha: float = 0.05) -> tuple[str, str, float, float]:
    """(status, reason, p_wins, p_still) from the flicker and still-window win counts.

    A frequency whose templates also win on the still screen is rejected however well the
    flicker scores: the win then comes from a resting rhythm, not from the stimulus.
    """
    if n == 0:
        return NO_RESPONSE, "no clean trials", 1.0, 1.0
    p_on = binom_p(wins, n, chance)
    p_off = binom_p(still_wins, n, chance)
    if p_off < alpha:
        return (REJECTED, f"still windows also win {still_wins}/{n} (p={p_off:.2g}): a resting "
                          f"rhythm sits at f0", p_on, p_off)
    if p_on < alpha:
        return USABLE, f"f0 beats the controls (p={p_on:.2g}), still screen does not", p_on, p_off
    return NO_RESPONSE, f"f0 does not beat the controls (p={p_on:.2g})", p_on, p_off


def template_wins(clf: CCA, X: np.ndarray) -> np.ndarray:
    """How many trials each template wins (n_targets,); target 0 is f0."""
    if len(X) == 0:
        return np.zeros(clf.n_targets, dtype=int)
    return np.bincount(clf.raw_scores(X).argmax(axis=1), minlength=clf.n_targets)


def f0_wins(clf: CCA, X: np.ndarray) -> int:
    """Trials whose best template is target 0."""
    return int(template_wins(clf, X)[0])


def run_notes(f0: float, controls: Sequence[float], on_counts: np.ndarray,
              off_counts: np.ndarray, n: int, status: str, plv: float, plv_ch: str,
              plv_p: float, alpha: float = 0.05) -> tuple[str, ...]:
    """Caveats that the verdict alone hides.

    A control that wins both the still and the flicker windows sits on a resting rhythm
    (alpha near 9-10 Hz) that masks f0 in the CCA test. A lower control winning the still
    screen alone is the expected 1/f bias and is not noted. A no-response run whose f0
    phase still locks to the flicker onset did respond, just not strongly enough for CCA.
    """
    if n == 0:
        return ()
    chance = 1.0 / (1 + len(controls))
    notes = [f"control {c:g} Hz wins {int(k_on)}/{n} flicker and {int(k_off)}/{n} still "
             f"windows: a resting rhythm on the control masks f0 in the CCA test"
             for c, k_on, k_off in zip(controls, on_counts[1:], off_counts[1:])
             if binom_p(int(k_on), n, chance) < alpha and binom_p(int(k_off), n, chance) < alpha]
    if status == NO_RESPONSE and np.isfinite(plv_p) and plv_p < alpha:
        notes.append(f"phase-locked to the flicker at {f0:g} Hz (PLV {plv:.2f} {plv_ch}, "
                     f"p={plv_p:.2g}) although CCA does not pick f0")
    return tuple(notes)


def analyse_run(rec: Recording, offsets: Sequence[float] = CONTROL_OFFSETS,
                harmonics: int = 2, reject: float = 3.0, alpha: float = 0.05,
                window_s: float | None = None, latency: float | None = None,
                channels: Sequence[str] = POSTERIOR, exclude: dict[str, str] | None = None,
                controls: dict[str, str] | None = None,
                display_hz: float | None = DISPLAY_HZ) -> RunResult:
    """Score one single-target recording; ``exclude``/``controls`` map run ids to reasons.

    ``display_hz`` None disables the refresh-rate check.
    """
    if rec.kind != "trials" or rec.n_targets != 1:
        raise ValueError(f"{rec.path}: phase 0 needs a single-target recording, got kind "
                         f"{rec.kind!r} with freqs {rec.freqs.tolist()}")
    rid = run_id(rec.path) if rec.path is not None else "?"
    f0 = float(rec.freqs[0])
    ctrl = tuple(f0 + float(d) for d in offsets)
    if not ctrl or any(abs(c - f0) < 1e-9 for c in ctrl):
        raise ValueError(f"control offsets must be non-empty and non-zero, got {list(offsets)}")
    lat = rec.latency if latency is None else float(latency)
    w = cut_windows(rec, window_s=window_s, latency=lat, band=rec.band,
                    notch_hz=rec.notch).pick(channels)
    n_total = len(w)
    w = w.subset(reject_artifacts(w.on, w.off, None, reject, ch_names=w.ch_names))
    n = len(w)

    clf = CCA([f0, *ctrl], rec.fs, harmonics=harmonics)
    on_counts, off_counts = template_wins(clf, w.on), template_wins(clf, w.off)
    wins, still = int(on_counts[0]), int(off_counts[0])
    status, reason, p_on, p_off = judge(wins, still, n, 1.0 / (1 + len(ctrl)), alpha)

    snr_db, snr_ch, plv, plv_ch, plv_p = float("nan"), "-", float("nan"), "-", float("nan")
    if n:
        snr = coherent_snr(w.on, rec.fs, f0)
        k = int(np.nanargmax(snr)) if np.isfinite(snr).any() else 0
        snr_db, snr_ch = float(snr[k]), w.ch_names[k]
        pl = phase_locking(w.on, rec.fs, f0, onset_lags(rec, w.on_start, lat))
        k = int(np.argmax(pl))
        plv, plv_ch = float(pl[k]), w.ch_names[k]
        plv_p = rayleigh_p(plv, n, len(pl))
    notes = run_notes(f0, ctrl, on_counts, off_counts, n, status, plv, plv_ch, plv_p, alpha)

    refresh = measured_refresh(rec.marker_text)
    exclude = KNOWN_EXCLUDED if exclude is None else exclude
    controls = KNOWN_CONTROLS if controls is None else controls
    if rid in controls:
        status, reason = CONTROL, controls[rid]
    elif (display_hz is not None and refresh is not None
          and abs(refresh - display_hz) > REFRESH_TOL):
        status, reason = EXCLUDED, (f"display measured {refresh:.1f} Hz, not {display_hz:g} Hz "
                                    f"(ProMotion on)")
    elif rid in exclude:
        status, reason = EXCLUDED, exclude[rid]
    return RunResult(run=rid, path=rec.path, f0=f0, refresh=refresh, n_total=n_total,
                     n_clean=n, controls=ctrl, wins=wins, p_wins=p_on, still_wins=still,
                     p_still=p_off, snr_db=snr_db, snr_channel=snr_ch, plv=plv,
                     plv_channel=plv_ch, plv_p=plv_p,
                     control_wins=tuple(int(k) for k in on_counts[1:]),
                     control_still_wins=tuple(int(k) for k in off_counts[1:]),
                     status=status, reason=reason, original=ORIGINAL.get(rid, ""),
                     notes=notes, windows=w)


def frequency_verdicts(results: Sequence[RunResult]) -> dict[float, tuple[str, str]]:
    """Per frequency over eligible runs: rejected if any run's still control wins, else
    usable if any run is usable, else no response."""
    out: dict[float, tuple[str, str]] = {}
    for f0 in sorted({r.f0 for r in results if r.eligible}):
        runs = [r for r in results if r.eligible and r.f0 == f0]
        bad = [r.run for r in runs if r.status == REJECTED]
        good = [r.run for r in runs if r.status == USABLE]
        if bad:
            out[f0] = (REJECTED, f"still-screen control wins in {', '.join(bad)}")
        elif good:
            out[f0] = (USABLE, f"usable in {', '.join(good)} ({len(good)}/{len(runs)} runs)")
        else:
            out[f0] = (NO_RESPONSE, f"no run beats the controls ({len(runs)} runs)")
    return out


# -- pair table ------------------------------------------------------------------------

# Which runs of a usable frequency enter the pair table. The headline "eligible" pool takes
# every non-excluded, non-control run, so the pairs are never scored on windows picked by
# their own f0 test. "usable" keeps only runs that passed that test: selection on the
# outcome, which scores a frequency that responded in one session out of three on that one
# session. "original" is the one run per frequency that the first phase-0 analysis pooled.
N_PERMUTATIONS = 5000      # label shuffles for a pair's balanced-accuracy p
N_BOOTSTRAP = 5000         # resamples for the best-vs-runner-up interval
POOLS = ("eligible", "usable", "original")
DEFAULT_POOL = "eligible"
ORIGINAL_RUNS = ("2026-10-22_145437", "2026-10-22_123700", "2026-10-22_150328")
POOL_INFO: dict[str, str] = {
    "eligible": "every 60 Hz, non-control run (no selection on the outcome)",
    "usable": "only runs that passed the f0 test (selected on the outcome, optimistic)",
    "original": f"the original analysis's runs {', '.join(r[-6:] for r in ORIGINAL_RUNS)}",
}


def _in_pool(r: RunResult, pool: str) -> bool:
    if pool == "eligible":
        return r.eligible
    if pool == "usable":
        return r.status == USABLE
    return r.eligible and r.run in ORIGINAL_RUNS


@dataclass
class PairResult:
    """Accuracy of one classifier on a pooled set of usable frequencies.

    The pools are imbalanced (one frequency can hold three times the windows of another),
    so the ranking score is ``balanced`` (mean recall), whose chance level is 1/classes;
    ``p`` tests it by shuffling the labels against the fixed predictions. Plain ``acc`` is
    compared with ``majority``, the score of always naming the largest class.
    ``correct[k]`` marks which windows of class k were classified right.
    """

    freqs: tuple[float, ...]
    method: str
    n: int
    per_class: tuple[int, ...]
    acc: float
    balanced: float
    p: float
    rest_low: float
    pool: str = DEFAULT_POOL
    runs: tuple[str, ...] = ()
    p_majority: float = float("nan")
    correct: tuple[np.ndarray, ...] = field(default=(), repr=False, compare=False)

    @property
    def chance(self) -> float:
        return 1.0 / len(self.freqs)

    @property
    def majority(self) -> float:
        return max(self.per_class) / self.n

    def as_dict(self) -> dict:
        return {"freqs": list(self.freqs), "method": self.method, "pool": self.pool,
                "n": self.n, "per_class": list(self.per_class), "acc": self.acc,
                "majority": self.majority, "p_majority": _finite_or_none(self.p_majority),
                "balanced": self.balanced, "p": self.p, "chance": self.chance,
                "rest_low": _finite_or_none(self.rest_low), "runs": list(self.runs)}


def balanced_p(y: np.ndarray, pred: np.ndarray, n_perm: int = N_PERMUTATIONS,
               seed: int = 0) -> float:
    """One-sided label-permutation p for the balanced accuracy of fixed predictions.

    Valid because the predictions never see the labels (the baseline is fit on still
    windows), so shuffling y keeps the class sizes and breaks only the association.
    """
    y = np.asarray(y, dtype=int)
    pred = np.asarray(pred, dtype=int)
    rng = np.random.default_rng(seed)
    shuffled = np.stack([rng.permutation(y) for _ in range(n_perm)])
    classes = np.unique(y)
    hits = shuffled == pred[None]
    recall = np.stack([(hits & (shuffled == k)).sum(axis=1) / np.sum(y == k)
                       for k in classes], axis=1)
    null = recall.mean(axis=1)
    return float((1 + np.sum(null >= balanced_accuracy(y, pred) - 1e-12)) / (n_perm + 1))


def pooled_runs(results: Sequence[RunResult], freqs: Sequence[float],
                pool: str = DEFAULT_POOL) -> list[RunResult]:
    """The runs at ``freqs`` that a pair table pools under rule ``pool`` (see POOLS)."""
    if pool not in POOLS:
        raise ValueError(f"pool must be one of {POOLS}, got {pool!r}")
    wanted = {float(f) for f in freqs}
    return [r for r in results
            if r.f0 in wanted and r.windows is not None and len(r.windows) and _in_pool(r, pool)]


def covered(results: Sequence[RunResult], freqs: Sequence[float], pool: str) -> bool:
    """True when ``pool`` holds windows at every one of ``freqs``."""
    have = {r.f0 for r in pooled_runs(results, freqs, pool)}
    return all(float(f) in have for f in freqs)


def pool_windows(results: Sequence[RunResult], freqs: Sequence[float], pool: str = DEFAULT_POOL
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, tuple[str, ...]]:
    """(on, off, labels, fs, run ids) pooled under ``pool``; labels index sorted freqs."""
    freqs = sorted(float(f) for f in freqs)
    runs = pooled_runs(results, freqs, pool)
    missing = sorted(set(freqs) - {r.f0 for r in runs})
    if missing:
        raise ValueError(f"no {pool} windows at {missing} Hz")
    rates = sorted({float(r.windows.fs) for r in runs})
    if len(rates) > 1:
        raise ValueError(f"cannot pool runs sampled at different rates {rates} Hz")
    on = np.concatenate([r.windows.on for r in runs])
    off = np.concatenate([r.windows.off for r in runs])
    y = np.concatenate([np.full(len(r.windows), freqs.index(r.f0)) for r in runs])
    return on, off, y, rates[0], tuple(r.run for r in runs)


def score_pool(results: Sequence[RunResult], freqs: Sequence[float], method: str = "cca",
               harmonics: int = 3, pool: str = DEFAULT_POOL) -> PairResult:
    """One window per trial, baseline fit on the pooled still windows.

    The baseline removes each template's still-screen score, so the 1/f background and a
    resting rhythm cannot vote for the lower frequency.
    """
    freqs = tuple(sorted(float(f) for f in freqs))
    if len(freqs) < 2:
        raise ValueError(f"need at least two frequencies to classify, got {list(freqs)}")
    on, off, y, fs, runs = pool_windows(results, freqs, pool)
    clf = make(method, freqs=freqs, fs=fs, harmonics=harmonics)
    rest_low = float(np.mean(clf.raw_scores(off).argmax(axis=1) == 0))
    clf.fit_baseline(off)
    pred = clf.predict(on)
    counts = tuple(int(np.sum(y == k)) for k in range(len(freqs)))
    acc = accuracy(y, pred)
    hits = int(np.sum(pred == y))
    return PairResult(freqs=freqs, method=method, n=int(y.size), per_class=counts, acc=acc,
                      balanced=balanced_accuracy(y, pred), p=balanced_p(y, pred),
                      rest_low=rest_low, pool=pool, runs=runs,
                      p_majority=binom_p(hits, int(y.size), max(counts) / y.size),
                      correct=tuple(pred[y == k] == k for k in range(len(freqs))))


def pair_table(results: Sequence[RunResult], verdicts: dict[float, tuple[str, str]],
               harmonics: int = 3, pool: str = DEFAULT_POOL
               ) -> tuple[list[PairResult], list[PairResult]]:
    """(2-class CCA for every pair of usable frequencies, CCA and FBCCA on all of them).

    Pairs the pool holds no windows for are left out.
    """
    usable = [f for f, (s, _) in verdicts.items() if s == USABLE]
    pairs = [score_pool(results, pr, "cca", harmonics, pool)
             for pr in itertools.combinations(usable, 2) if covered(results, pr, pool)]
    multi = ([score_pool(results, usable, m, harmonics, pool) for m in ("cca", "fbcca")]
             if len(usable) >= 3 and covered(results, usable, pool) else [])
    return pairs, multi


def run_counts(results: Sequence[RunResult], f0: float) -> tuple[int, int]:
    """(usable runs, eligible runs) at f0: how reliably the frequency responded."""
    runs = [r for r in results if r.eligible and r.f0 == f0]
    return sum(r.status == USABLE for r in runs), len(runs)


def ranked(pairs: Sequence[PairResult]) -> list[PairResult]:
    """Highest balanced accuracy first, ties broken by plain accuracy."""
    return sorted(pairs, key=lambda p: (p.balanced, p.acc), reverse=True)


def best_pair(pairs: Sequence[PairResult]) -> PairResult | None:
    """Highest balanced accuracy, ties broken by plain accuracy."""
    return ranked(pairs)[0] if pairs else None


@dataclass(frozen=True)
class Lead:
    """How far the best pair leads the runner-up in balanced accuracy."""

    best: PairResult
    runner_up: PairResult
    diff: float
    lo: float
    hi: float

    @property
    def resolved(self) -> bool:
        return self.lo > 0.0

    def as_dict(self) -> dict:
        return {"best": list(self.best.freqs), "runner_up": list(self.runner_up.freqs),
                "diff": self.diff, "ci95": [self.lo, self.hi], "resolved": self.resolved}


def lead(pairs: Sequence[PairResult], n_boot: int = N_BOOTSTRAP, seed: int = 0
         ) -> Lead | None:
    """Bootstrap 95% interval of best minus runner-up balanced accuracy.

    Both pairs share the windows at their common frequency (the same pool), so each
    resample draws one set of window indices per frequency and applies it to both pairs.
    Windows from one run are not independent, so the interval is if anything too narrow.
    """
    order = ranked(pairs)
    if len(order) < 2:
        return None
    a, b = order[0], order[1]
    if not (a.correct and b.correct):
        raise ValueError("pairs carry no per-window results to resample")
    rng = np.random.default_rng(seed)
    sizes: dict[float, int] = {}
    for p in (a, b):
        for f, c in zip(p.freqs, p.correct):
            if sizes.setdefault(f, len(c)) != len(c):
                raise ValueError(f"pairs hold different windows at {f:g} Hz (mixed pools?)")
    draws = {f: rng.integers(0, n, size=(n_boot, n)) for f, n in sizes.items()}

    def boot(p: PairResult) -> np.ndarray:
        return np.mean([c[draws[f]].mean(axis=1) for f, c in zip(p.freqs, p.correct)], axis=0)

    diff = boot(a) - boot(b)
    lo, hi = np.percentile(diff, [2.5, 97.5])
    return Lead(a, b, a.balanced - b.balanced, float(lo), float(hi))


# -- report ----------------------------------------------------------------------------

def _hz(f: float) -> str:
    return f"{f:g}"


def _fmt_p(p: float) -> str:
    return f"{p:.3f}" if p >= 0.001 else f"{p:.0e}"


def format_runs(results: Sequence[RunResult]) -> str:
    head = (f"{'run':<18}{'f0':>4}{'disp':>6}{'clean':>7}  {'CCA wins':>8}{'p':>7}  "
            f"{'still':>6}{'p':>7}  {'avg SNR':>11}  {'PLV':>11}{'p':>7}  "
            f"{'orig':<22}verdict")
    lines = [head, "-" * len(head)]
    for r in results:
        disp = f"{r.refresh:.0f}" if r.refresh is not None else "?"
        snr = f"{r.snr_db:5.1f} {r.snr_channel:<3}" if np.isfinite(r.snr_db) else "    -"
        plv = f"{r.plv:.2f} {r.plv_channel:<3}" if np.isfinite(r.plv) else "    -"
        plv_p = _fmt_p(r.plv_p) if np.isfinite(r.plv_p) else "-"
        verdict = r.status if not r.reason else f"{r.status}: {r.reason}"
        lines.append(
            f"{r.run:<18}{_hz(r.f0):>4}{disp:>6}{f'{r.n_clean}/{r.n_total}':>7}  "
            f"{f'{r.wins}/{r.n_clean}':>8}{_fmt_p(r.p_wins):>7}  "
            f"{f'{r.still_wins}/{r.n_clean}':>6}{_fmt_p(r.p_still):>7}  {snr:>11}  "
            f"{plv:>11}{plv_p:>7}  {r.original or '-':<22}{verdict}")
    return "\n".join(lines)


Tables = dict[str, tuple[list[PairResult], list[PairResult]]]


def pair_tables(results: Sequence[RunResult], verdicts: dict[float, tuple[str, str]],
                harmonics: int = 3) -> Tables:
    """:func:`pair_table` under every pooling rule in POOLS."""
    return {pool: pair_table(results, verdicts, harmonics, pool) for pool in POOLS}


def ranking_stable(tables: Tables) -> bool:
    """True when every pooling rule that yields pairs picks the same best pair."""
    picks = {tuple(b.freqs) for pairs, _ in tables.values() if (b := best_pair(pairs))}
    return len(picks) <= 1


def _window_s(results: Sequence[RunResult]) -> float | None:
    for r in results:
        if r.windows is not None and len(r.windows):
            return r.windows.on.shape[-1] / r.windows.fs
    return None


def _pair_name(p: PairResult) -> str:
    return " vs ".join(_hz(f) for f in p.freqs) + " Hz"


def format_pairs(results: Sequence[RunResult], verdicts: dict[float, tuple[str, str]],
                 tables: Tables, pool: str, pair_harmonics: int) -> list[str]:
    pairs, multi = tables[pool]
    if not (pairs or multi):
        return ["Fewer than two usable frequencies: no pair table."]
    others = [o for o in POOLS if o != pool and o in tables]
    win = _window_s(results)
    out = [f"Usable frequencies, one {win:.2f} s window per trial, CCA {pair_harmonics} "
           f"harmonics with still-window baseline, pooling {pool} runs: {POOL_INFO[pool]}.",
           "  The classes differ in size, so pairs are ranked by balanced accuracy (mean "
           "recall, chance 1/classes, label-permutation p). acc is plain accuracy; majority "
           "is the score of always naming the largest class, p maj tests acc against it. "
           "rest low: share of still windows won by the lowest template before the baseline."]
    out += [f"  'bal {o}': the balanced accuracy pooling {POOL_INFO[o]}." for o in others]
    out.append(f"  {'targets':<12}{'method':<7}{'n':>4}  {'per class':<12}{'balanced':>9}"
               f"{'chance':>8}{'p':>8}{'acc':>6}{'majority':>10}{'p maj':>8}{'rest low':>10}"
               + "".join(f"{'bal ' + o:>14}" for o in others))
    alt = {o: {(q.freqs, q.method): q for q in [*tables[o][0], *tables[o][1]]} for o in others}
    for pr in [*pairs, *multi]:
        per = "/".join(str(c) for c in pr.per_class)
        extra = ""
        for o in others:
            q = alt[o].get((pr.freqs, pr.method))
            extra += f"{(f'{q.balanced:.0%} n={q.n}' if q else '-'):>14}"
        p_maj = _fmt_p(pr.p_majority) if np.isfinite(pr.p_majority) else "-"
        out.append(f"  {'/'.join(_hz(f) for f in pr.freqs):<12}{pr.method:<7}{pr.n:>4}  "
                   f"{per:<12}{pr.balanced:>9.0%}{pr.chance:>8.0%}{_fmt_p(pr.p):>8}"
                   f"{pr.acc:>6.0%}{pr.majority:>10.0%}{p_maj:>8}{pr.rest_low:>10.0%}{extra}")
    best = best_pair(pairs)
    if best is None:
        return out
    out.append(f"Best pair ({pool} runs): {_pair_name(best)} (balanced {best.balanced:.0%}, "
               f"n={best.n}).")
    ld = lead(pairs)
    if ld is not None:
        verdict = ("resolved" if ld.resolved else
                   "not resolved: the interval includes 0, so the data do not separate them")
        out.append(f"  Lead over {_pair_name(ld.runner_up)} ({ld.runner_up.balanced:.0%}): "
                   f"{ld.diff:+.0%}, 95% bootstrap interval {ld.lo:+.0%} to {ld.hi:+.0%}; "
                   f"{verdict}.")
    for o in others:
        b = best_pair(tables[o][0])
        if b is not None:
            agree = "agrees" if b.freqs == best.freqs else "differs"
            out.append(f"Best pair ({o} runs): {_pair_name(b)} (balanced {b.balanced:.0%}, "
                       f"n={b.n}), {agree}.")
    if not ranking_stable({o: tables[o] for o in [pool, *others]}):
        usable = [f for f, (s, _) in verdicts.items() if s == USABLE]
        counts = ", ".join(f"{_hz(f)} Hz {u}/{n}" for f in usable
                           for u, n in [run_counts(results, f)])
        out.append(f"The pools disagree on the best pair; the headline is the {pool} pool. "
                   f"Usable/eligible runs per frequency: {counts}.")
    return out


def format_report(results: Sequence[RunResult], verdicts: dict[float, tuple[str, str]],
                  tables: Tables, pool: str, harmonics: int, pair_harmonics: int) -> str:
    if not results:
        return "no single-target recordings found"
    ctrl = ", ".join(f"{d:+g}" for d in (np.array(results[0].controls) - results[0].f0))
    out = [f"Phase 0 frequency selection: {len(results)} single-target recordings, posterior "
           f"channels {', '.join(POSTERIOR)}",
           f"CCA wins: f0 vs controls at f0 {ctrl} Hz ({harmonics} harmonics), chance "
           f"{results[0].chance:.0%}, one-sided binomial p; 'still' is the same test on the "
           f"still-screen windows. avg SNR: coherent average, f0 bin vs bins "
           f"{SNR_GUARD + 1}-{SNR_GUARD + SNR_SIDE} away on each side (dB). PLV: phase "
           f"locking at f0 across trials on the best channel, Rayleigh p corrected for the "
           f"{len(POSTERIOR)} channels.", "", format_runs(results), ""]
    noted = [r for r in results if r.notes and r.status != EXCLUDED]
    if noted:
        out.append("Notes:")
        out += [f"  {r.run}  {note}" for r in noted for note in r.notes]
        out.append("")
    out.append("Frequencies (excluded and control runs left out):")
    for f0, (status, reason) in verdicts.items():
        out.append(f"  {_hz(f0):>4} Hz  {status:<12}{reason}")
    left_out = [r for r in results if not r.eligible]
    if left_out:
        out.append("Left out of verdicts and pairs:")
        for r in left_out:
            out.append(f"  {r.run}  {_hz(r.f0)} Hz  {r.status}: {r.reason}"
                       + (f" (CCA wins {r.wins}/{r.n_clean})" if r.status == CONTROL else ""))
    out.append("")
    out += format_pairs(results, verdicts, tables, pool, pair_harmonics)
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.select_freqs", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", type=Path,
                   help="*_trials.npz recordings (default: all in recordings_dir())")
    p.add_argument("--dir", type=Path, default=None, help="directory to search instead")
    p.add_argument("--offsets", type=float, nargs="+", default=list(CONTROL_OFFSETS),
                   metavar="HZ", help="control frequencies as offsets from f0 (default -3 3)")
    p.add_argument("--harmonics", type=int, default=2,
                   help="CCA harmonics for the f0-vs-control test (default 2)")
    p.add_argument("--pair-harmonics", type=int, default=3,
                   help="CCA/FBCCA harmonics for the pair table (default 3)")
    p.add_argument("--reject", type=float, default=3.0, metavar="FACTOR",
                   help="artifact rejection factor on window RMS; 0 keeps all (default 3)")
    p.add_argument("--alpha", type=float, default=0.05, help="significance level (default 0.05)")
    p.add_argument("--window", type=float, default=None, help="window length in s (default: file's)")
    p.add_argument("--latency", type=float, default=None, help="response latency in s (default: file's)")
    p.add_argument("--display-hz", type=float, default=DISPLAY_HZ,
                   help="expected display refresh; runs measured elsewhere are excluded (default 60)")
    p.add_argument("--exclude", action="append", default=[], metavar="RUN",
                   help="extra run id (e.g. 2026-10-22_121914) to exclude; repeatable")
    p.add_argument("--control", action="append", default=[], metavar="RUN",
                   help="extra run id to treat as a control; repeatable")
    p.add_argument("--keep-all", action="store_true",
                   help="exclude nothing: ignore the known lists and the refresh check")
    p.add_argument("--pool", choices=POOLS, default=DEFAULT_POOL,
                   help="runs pooled into the headline pair table (default eligible); "
                        + "; ".join(f"{k}: {v}" for k, v in POOL_INFO.items())
                        + ". Every pool is reported")
    p.add_argument("--json", action="store_true", help="print JSON instead of the text report")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 < args.alpha < 1:
        print(f"error: --alpha must lie in (0, 1), got {args.alpha}", file=sys.stderr)
        return 2
    try:
        return _run(args)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        msg = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"error: {msg}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    paths = list(args.files) or list_recordings("trials", directory=args.dir)
    if not paths:
        print(f"no *_trials.npz recordings in {args.dir or 'recordings_dir()'}", file=sys.stderr)
        return 1
    if args.keep_all:
        exclude, controls, display = {}, {}, None
    else:
        exclude = {**KNOWN_EXCLUDED, **{r: "excluded on the command line" for r in args.exclude}}
        controls = {**KNOWN_CONTROLS, **{r: "control (command line)" for r in args.control}}
        display = args.display_hz
    results: list[RunResult] = []
    for path in paths:
        rec = load_recording(path)
        if rec.kind != "trials" or rec.n_targets != 1:
            print(f"skipping {Path(path).name}: not a single-target recording", file=sys.stderr)
            continue
        print(f"analysing {Path(path).name} ({rec.freqs[0]:g} Hz)", file=sys.stderr)
        results.append(analyse_run(rec, args.offsets, args.harmonics, args.reject, args.alpha,
                                   args.window, args.latency, exclude=exclude,
                                   controls=controls, display_hz=display))
    verdicts = frequency_verdicts(results)
    print("scoring pairs", file=sys.stderr)
    tables = pair_tables(results, verdicts, args.pair_harmonics)
    if args.json:
        def table(pool: str) -> dict:
            pairs, multi = tables[pool]
            best = best_pair(pairs)
            ld = lead(pairs)
            return {"pairs": [q.as_dict() for q in pairs], "multi": [q.as_dict() for q in multi],
                    "best_pair": list(best.freqs) if best else None,
                    "lead": ld.as_dict() if ld else None}
        print(json.dumps({
            "runs": [r.as_dict() for r in results],
            "frequencies": {_hz(f): {"status": s, "reason": why,
                                     "usable_runs": run_counts(results, f)[0],
                                     "eligible_runs": run_counts(results, f)[1]}
                            for f, (s, why) in verdicts.items()},
            "pool": args.pool,
            **table(args.pool),
            "by_pool": {pool: table(pool) for pool in POOLS},
            "ranking_stable": ranking_stable(tables),
        }, indent=2, allow_nan=False))
    else:
        print(format_report(results, verdicts, tables, args.pool, args.harmonics,
                            args.pair_harmonics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
