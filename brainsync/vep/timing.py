"""Do the display's markers and the board's sample times share one time grid?

The c-VEP and t-VEP decoders cut every epoch at a display marker (the flip time of a
cycle's or a flash's first frame, Unix seconds on the stimulus's stderr) from samples the
board stamps in Unix time on the same Mac. A template trained on one grid and applied to
another is noise against noise, so before a live run is trusted the grids are checked, not
assumed (PLAN.md V5, item 1). Nothing here needs a display or a board: the input is the
stimulus log (the "[marker <unix s>] kind:k" lines; the demo prints the log's path) and /
or a recording saved by ``python -m brainsync.vep --save`` (recordings.save_recording:
X, t, fs and the marker text).

What is checked, each as a PASS / WARN / FAIL line with its numbers (SKIP when the input
has nothing to check it on), like tools/preflight.py:

- cycle markers (c-VEP): count, median spacing against cycle_frames / refresh from the
  config (cvep_4: 63 frames at 60 Hz = 1.0500 s), every spacing's deviation in frames, the
  spacings off by more than half a frame (a dropped or duplicated frame), and the drift of
  the marker times against a perfect frame grid (a linear fit of time on frame index; the
  slope's error in ppm);
- flash markers (t-VEP): spacings are whole multiples of the slot (flash_frames +
  gap_frames) within half a frame, and the same drift fit;
- with a recording: the gap from each marker to the nearest sample (under one sample
  period, 4 ms at 250 Hz), the fraction of markers inside the recorded span, and the
  sample clock's rate from a fit of t on the sample index (its deviation from fs in ppm);
- the log's "measured refresh" line against the config, and the stimulus's "timing: ...
  dropped overall" summary.

Levels. Spacings: none off by more than SLIP_FRAMES (0.5) is PASS; isolated slips of a
single frame (every deviation under BIG_SLIP_FRAMES, 1.5, and at most SLIP_ALLOWED (2) or
SLIP_FRACTION (1 %) of the spacings off, whichever is more; one late flash makes two
spacings off, the long one before it and the short one after) are a WARN; a slip of two
frames or more, or more spacings off than that, is a FAIL. Drift (the marker clock against
the nominal frame grid, whole cycles or slots rounded, never frames) and sample rate:
within RATE_PASS_PPM (2000) PASS, within
RATE_WARN_PPM (10000) WARN, beyond FAIL. A marker farther than one sample period from the
nearest sample is a WARN up to one frame and a FAIL beyond; markers outside the recording
are a WARN, all of them outside a FAIL. Dropped frames: none PASS, up to DROPPED_FRACTION
(0.5 %) WARN, more FAIL.

    python -m brainsync.vep.timing <stimulus log>              # the path the demo prints
    python -m brainsync.vep.timing <stamp>_vep.npz             # from --save
    python -m brainsync.vep.timing log.txt run.npz -c tvep_4   # both; the config sets the grid
    python -m brainsync.vep.timing run.npz --json

A .npz argument is a recording, anything else a log. With both, the log's marker lines are
the markers and the recording supplies the samples. Exit 0 when nothing FAILed, 1 on a
FAIL, 2 on a usage error (no file, an unreadable file, an unknown config). What it cannot
see: the latency from the flip time to the photons on the panel. That is a constant the
template absorbs, and a photodiode's job.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..recordings import Event, load_recording, parse_events
from ..stimulus import StimulusConfig, load_config

__all__ = [
    "PASS", "WARN", "FAIL", "SKIP", "Check", "Spacings", "split_runs", "spacing_deviations",
    "grid_spacings", "drift_ppm", "nearest_sample_gaps", "sample_rate_ppm", "parse_refresh",
    "parse_dropped", "judge_spacings", "judge_rate", "check_markers", "check_recording",
    "check_log_lines", "run_checks", "exit_code", "main",
]

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
DEFAULT_CONFIG = "cvep_4"
SLIP_FRAMES = 0.5          # a spacing off by more than this many frames is a slip
BIG_SLIP_FRAMES = 1.5      # a slip this large (2 frames or more) fails on its own
SLIP_FRACTION = 0.01       # more than this share of slipped spacings fails...
SLIP_ALLOWED = 2           # ...but a short run may have this many (one late marker = two)
RATE_PASS_PPM = 2000.0     # drift of the markers, and the sample clock against fs
RATE_WARN_PPM = 10000.0
DROPPED_FRACTION = 0.005   # dropped frames the stimulus reports, as a share of its frames
MIN_FIT = 3                # markers needed for a drift fit

_REFRESH_RE = re.compile(r"measured refresh:\s*([0-9.]+)\s*Hz")
_DROPPED_RE = re.compile(r"(\d+) frames, (\d+) dropped overall")


@dataclass
class Check:
    """One line of the report."""

    name: str
    level: str
    reason: str
    data: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        return f"{self.level:<4} {self.name:<18} {self.reason}"


@dataclass
class Spacings:
    """Marker spacings against a frame grid, pooled over the runs of one marker kind."""

    kind: str
    count: int                      # markers
    runs: int
    expected_s: float               # one cycle, or one slot
    frame_s: float
    spacings_s: np.ndarray
    deviation_frames: np.ndarray    # signed, per spacing, from the nearest expected spacing

    @property
    def median_s(self) -> float:
        return float(np.median(self.spacings_s)) if self.spacings_s.size else float("nan")

    @property
    def max_abs_frames(self) -> float:
        return float(np.max(np.abs(self.deviation_frames))) if self.deviation_frames.size else 0.0

    @property
    def n_off(self) -> int:
        return int(np.sum(np.abs(self.deviation_frames) > SLIP_FRAMES))


# -- pure functions --------------------------------------------------------------------------

def split_runs(events: Sequence[Event], kind: str) -> list[list[float]]:
    """The times of the ``kind`` markers, grouped into flicker runs.

    A run starts at every ``stim_on`` marker; cycle markers also start one whenever the
    cycle index does not follow the previous one, since the stimulus counts cycles from 0
    at each start (a dropped frame keeps the count, so it stays within its run). Events
    are taken in time order. Markers before the first ``stim_on`` form a run of their own.
    """
    runs: list[list[float]] = []
    current: list[float] = []
    last_index: int | None = None
    for t, k, idx in sorted(events, key=lambda e: e[0]):
        if k == "stim_on":
            if current:
                runs.append(current)
            current, last_index = [], None
        elif k == kind:
            if kind == "cycle" and last_index is not None and idx != last_index + 1 and current:
                runs.append(current)
                current = []
            current.append(float(t))
            last_index = int(idx)
    if current:
        runs.append(current)
    return runs


def spacing_deviations(times: Sequence[float], period_s: float, frame_s: float,
                       multiples: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """(spacings in s, deviations in frames) of consecutive ``times``.

    The expected spacing is ``period_s``, or with ``multiples`` the nearest whole multiple
    of it (at least one), for flashes that skip slots.
    """
    if period_s <= 0 or frame_s <= 0:
        raise ValueError(f"period_s and frame_s must be positive, got {period_s}, {frame_s}")
    t = np.asarray(times, dtype=float)
    if t.size < 2:
        return np.empty(0), np.empty(0)
    spacing = np.diff(t)
    if multiples:
        n = np.maximum(1, np.round(spacing / period_s))
        expected = n * period_s
    else:
        expected = np.full_like(spacing, period_s)
    return spacing, (spacing - expected) / frame_s


def grid_spacings(runs: Sequence[Sequence[float]], kind: str, period_s: float, frame_s: float,
                  multiples: bool = False) -> Spacings:
    """Pool the spacing deviations of every run of one marker kind."""
    spacings, devs = [], []
    count = 0
    for run in runs:
        count += len(run)
        s, d = spacing_deviations(run, period_s, frame_s, multiples)
        spacings.append(s)
        devs.append(d)
    return Spacings(kind=kind, count=count, runs=len(runs), expected_s=float(period_s),
                    frame_s=float(frame_s),
                    spacings_s=np.concatenate(spacings) if spacings else np.empty(0),
                    deviation_frames=np.concatenate(devs) if devs else np.empty(0))


def drift_ppm(times: Sequence[float], period_s: float, frame_s: float) -> tuple[float, int]:
    """The marker clock's rate against the nominal frame grid: (slope error in ppm, markers
    used).

    Each marker sits on the nominal grid a whole number of periods (cycles or slots) after
    the previous one, so its grid position is the cumulative sum of the spacings rounded to
    periods, and time is fitted linearly on that position. Only whole periods are rounded,
    never frames, so a clock running 2 % slow shows as +20000 ppm instead of being taken
    for a dropped frame every cycle. A real dropped frame shifts every later marker one
    frame and biases the slope by one frame over the run (a few hundred ppm in a minute);
    the spacing check reports it as the slip it is. Fewer than MIN_FIT markers give nan.
    """
    if period_s <= 0 or frame_s <= 0:
        raise ValueError(f"period_s and frame_s must be positive, got {period_s}, {frame_s}")
    t = np.asarray(times, dtype=float)
    if t.size < MIN_FIT:
        return float("nan"), int(t.size)
    periods = np.maximum(1, np.round(np.diff(t) / period_s))
    k = np.concatenate([[0.0], np.cumsum(periods)]) * (period_s / frame_s)
    if np.ptp(k) == 0:
        return float("nan"), int(t.size)
    slope, _ = np.polyfit(k, t - t[0], 1)
    return float((slope / frame_s - 1.0) * 1e6), int(t.size)


def nearest_sample_gaps(marker_times: Sequence[float], t: Sequence[float]
                        ) -> tuple[np.ndarray, np.ndarray]:
    """(gap to the nearest sample in s for each marker inside the recording, mask of the
    markers inside [t[0], t[-1]]). ``t`` is the recording's sample times, increasing."""
    m = np.asarray(marker_times, dtype=float)
    ts = np.asarray(t, dtype=float)
    if ts.ndim != 1 or ts.size == 0:
        raise ValueError(f"t must be a non-empty 1-d array of sample times, got shape {ts.shape}")
    inside = (m >= ts[0]) & (m <= ts[-1])
    mi = m[inside]
    if mi.size == 0:
        return np.empty(0), inside
    right = np.clip(np.searchsorted(ts, mi), 1, ts.size - 1)
    gaps = np.minimum(np.abs(mi - ts[right - 1]), np.abs(ts[right] - mi))
    return gaps, inside


def sample_rate_ppm(t: Sequence[float], fs: float) -> tuple[float, float]:
    """The sample clock against ``fs``: (deviation in ppm, fitted rate in Hz) from a linear
    fit of the sample times on their index."""
    ts = np.asarray(t, dtype=float)
    if fs <= 0:
        raise ValueError(f"fs must be positive, got {fs}")
    if ts.size < 2 or ts[-1] == ts[0]:
        return float("nan"), float("nan")
    slope, _ = np.polyfit(np.arange(ts.size), ts, 1)
    rate = 1.0 / slope
    return float((rate / fs - 1.0) * 1e6), float(rate)


def parse_refresh(text: str) -> float | None:
    """The last "measured refresh: <x> Hz" in a stimulus log, or None."""
    found = _REFRESH_RE.findall(text)
    return float(found[-1]) if found else None


def parse_dropped(text: str) -> tuple[int, int] | None:
    """(frames, dropped) from the stimulus's last "timing: ..." summary, or None."""
    found = _DROPPED_RE.findall(text)
    return (int(found[-1][0]), int(found[-1][1])) if found else None


# -- judgements -------------------------------------------------------------------------------

def judge_spacings(sp: Spacings) -> Check:
    """PASS / WARN / FAIL for pooled spacings, by the rules in the module docstring."""
    name = f"{sp.kind} spacing"
    n_sp = int(sp.spacings_s.size)
    data = {"markers": sp.count, "runs": sp.runs, "spacings": n_sp, "expected_s": sp.expected_s,
            "median_s": sp.median_s, "max_abs_frames": sp.max_abs_frames, "n_off": sp.n_off}
    if n_sp == 0:
        return Check(name, SKIP, f"{sp.count} {sp.kind} marker(s): no spacing to check", data)
    what = "a cycle" if sp.kind == "cycle" else "a slot"
    reason = (f"{sp.count} markers in {sp.runs} run(s), median {sp.median_s:.4f} s "
              f"({what} is {sp.expected_s:.4f} s), largest deviation "
              f"{sp.max_abs_frames:.2f} frames, {sp.n_off}/{n_sp} off by more than "
              f"{SLIP_FRAMES:g} frame")
    if sp.n_off == 0:
        return Check(name, PASS, reason, data)
    allowed = max(SLIP_ALLOWED, SLIP_FRACTION * n_sp)
    if sp.max_abs_frames < BIG_SLIP_FRAMES and sp.n_off <= allowed:
        return Check(name, WARN, reason + " (single dropped or doubled frames)", data)
    return Check(name, FAIL, reason + " (a slip of 2+ frames, or too many)", data)


def judge_rate(name: str, ppm: float, detail: str, data: dict[str, Any] | None = None) -> Check:
    """PASS within RATE_PASS_PPM, WARN within RATE_WARN_PPM, FAIL beyond; SKIP for nan."""
    data = dict(data or {}, ppm=ppm)
    if not np.isfinite(ppm):
        return Check(name, SKIP, f"not enough points for a fit ({detail})", data)
    reason = f"{ppm:+.0f} ppm ({detail})"
    if abs(ppm) <= RATE_PASS_PPM:
        return Check(name, PASS, reason, data)
    if abs(ppm) <= RATE_WARN_PPM:
        return Check(name, WARN, reason, data)
    return Check(name, FAIL, reason, data)


def check_markers(events: Sequence[Event], cfg: StimulusConfig) -> list[Check]:
    """The cycle (c-VEP) or flash (t-VEP) markers against the config's frame grid."""
    frame_s = 1.0 / cfg.refresh
    out: list[Check] = []
    kinds_seen = {e[1] for e in events}
    if cfg.code is not None:
        period = cfg.code.cycle_frames * frame_s
        runs = split_runs(events, "cycle")
        if not runs:
            out.append(Check("cycle spacing", FAIL, "no cycle markers; is this a c-VEP log?"))
        else:
            sp = grid_spacings(runs, "cycle", period, frame_s)
            out.append(judge_spacings(sp))
            longest = max(runs, key=len)
            ppm, n = drift_ppm(longest, period, frame_s)
            out.append(judge_rate("cycle drift", ppm,
                                  f"{n} markers of the longest run against a {cfg.refresh:g} Hz "
                                  "frame grid", {"markers": n}))
        if "flash" in kinds_seen:
            out.append(Check("flash spacing", FAIL,
                             f"flash markers, but {cfg.name!r} is a [code] config"))
    elif cfg.time is not None:
        slot = cfg.time.slot_frames * frame_s
        runs = split_runs(events, "flash")
        if not runs:
            out.append(Check("flash spacing", FAIL, "no flash markers; is this a t-VEP log?"))
        else:
            sp = grid_spacings(runs, "flash", slot, frame_s, multiples=True)
            out.append(judge_spacings(sp))
            longest = max(runs, key=len)
            ppm, n = drift_ppm(longest, slot, frame_s)
            out.append(judge_rate("flash drift", ppm,
                                  f"{n} markers of the longest run against a {cfg.refresh:g} Hz "
                                  "frame grid", {"markers": n}))
        if "cycle" in kinds_seen:
            out.append(Check("cycle spacing", FAIL,
                             f"cycle markers, but {cfg.name!r} is a [time] config"))
    else:
        found = sorted(kinds_seen & {"cycle", "flash"})
        if found:
            out.append(Check("markers", FAIL, f"{', '.join(found)} markers, but {cfg.name!r} "
                             "is a flicker config: pass -c cvep_4 or tvep_4"))
        else:
            out.append(Check("markers", SKIP, f"{cfg.name!r} is a flicker config; no cycle or "
                             "flash grid to check"))
    return out


def check_recording(marker_times: Sequence[float], t: Sequence[float], fs: float,
                    refresh: float) -> list[Check]:
    """Markers against the recorded samples: coverage, nearest-sample gaps, the sample rate."""
    ts = np.asarray(t, dtype=float)
    out: list[Check] = []
    if ts.size == 0:
        return [Check("recording", FAIL, "the recording holds no samples")]
    span = float(ts[-1] - ts[0])
    m = np.asarray(marker_times, dtype=float)
    if m.size == 0:
        out.append(Check("marker coverage", FAIL, f"no markers to place in the {span:.1f} s "
                         "recording"))
    else:
        gaps, inside = nearest_sample_gaps(m, ts)
        n_in = int(inside.sum())
        frac = n_in / m.size
        data = {"markers": int(m.size), "inside": n_in, "span_s": span}
        reason = f"{n_in}/{m.size} markers inside the {span:.1f} s recording"
        level = PASS if n_in == m.size else FAIL if n_in == 0 else WARN
        if level == WARN:
            before = int(np.sum(m < ts[0]))
            reason += f" ({before} before its first sample, {m.size - n_in - before} after its last)"
        out.append(Check("marker coverage", level, reason, data))
        if gaps.size:
            period = 1.0 / fs
            worst, med = float(gaps.max()), float(np.median(gaps))
            data = {"max_gap_s": worst, "median_gap_s": med, "sample_period_s": period}
            reason = (f"nearest sample {1e3 * worst:.2f} ms away at worst, median "
                      f"{1e3 * med:.2f} ms (a sample period is {1e3 * period:.1f} ms)")
            level = PASS if worst <= period else WARN if worst <= 1.0 / refresh else FAIL
            out.append(Check("marker to sample", level, reason, data))
    ppm, rate = sample_rate_ppm(ts, fs)
    out.append(judge_rate("sample rate", ppm,
                          f"{rate:.3f} Hz fitted over {ts.size} samples, fs {fs:g} Hz",
                          {"fitted_hz": rate, "fs": fs, "samples": int(ts.size)}))
    return out


def check_log_lines(text: str, cfg: StimulusConfig) -> list[Check]:
    """The log's measured refresh against the config, and the dropped-frame summary."""
    out: list[Check] = []
    measured = parse_refresh(text)
    tol = cfg.display.refresh_tolerance_hz
    if measured is None:
        out.append(Check("measured refresh", SKIP, "no 'measured refresh' line"))
    else:
        level = PASS if abs(measured - cfg.refresh) <= tol else FAIL
        out.append(Check("measured refresh", level,
                         f"{measured:.2f} Hz (config {cfg.refresh:g} Hz, tolerance {tol:g})",
                         {"measured_hz": measured, "config_hz": cfg.refresh}))
    dropped = parse_dropped(text)
    if dropped is None:
        out.append(Check("dropped frames", SKIP, "no 'timing:' summary line (the run may not "
                         "have ended)"))
    else:
        frames, n_drop = dropped
        share = n_drop / frames if frames else 0.0
        level = PASS if n_drop == 0 else WARN if share <= DROPPED_FRACTION else FAIL
        out.append(Check("dropped frames", level, f"{n_drop} of {frames} frames",
                         {"frames": frames, "dropped": n_drop}))
    return out


def run_checks(cfg: StimulusConfig, log_text: str | None = None,
               recording: tuple[np.ndarray, float, str] | None = None) -> list[Check]:
    """Every check for a log text and / or a recording (its t, fs and marker text).

    The markers come from the log when there is one, else from the recording's text; the
    recording's samples are checked against them.
    """
    if log_text is None and recording is None:
        raise ValueError("need a stimulus log, a recording, or both")
    text = log_text if log_text is not None else recording[2]  # type: ignore[index]
    events = parse_events(text)
    out = check_log_lines(text, cfg) + check_markers(events, cfg)
    if recording is not None:
        t, fs, _ = recording
        out += check_recording([e[0] for e in events], t, fs, cfg.refresh)
    return out


def exit_code(checks: Sequence[Check]) -> int:
    return 1 if any(c.level == FAIL for c in checks) else 0


# -- the command line --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.vep.timing", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", type=Path,
                   help="a stimulus log and / or a <stamp>_vep.npz recording (--save)")
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                   help=f"stimulus config name or .toml path for the frame grid (default "
                        f"{DEFAULT_CONFIG})")
    p.add_argument("--json", action="store_true", help="one JSON list instead of the lines")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"error: config {args.config!r}: {exc}", file=sys.stderr)
        return 2
    log_text: str | None = None
    recording: tuple[np.ndarray, float, str] | None = None
    for path in args.files:
        if not path.is_file():
            print(f"error: no such file: {path}", file=sys.stderr)
            return 2
        try:
            if path.suffix == ".npz":
                rec = load_recording(path)
                recording = (rec.t, rec.fs, rec.marker_text)
            else:
                log_text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, KeyError, ValueError) as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return 2
    checks = run_checks(cfg, log_text, recording)
    if args.json:
        print(json.dumps([{"name": c.name, "level": c.level, "reason": c.reason, **c.data}
                          for c in checks], default=float))
    else:
        for c in checks:
            print(c.line())
        n_fail = sum(c.level == FAIL for c in checks)
        n_warn = sum(c.level == WARN for c in checks)
        print(f"{'FAIL' if n_fail else 'OK'}: {n_fail} failed, {n_warn} warning(s), "
              f"config {cfg.name}")
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
