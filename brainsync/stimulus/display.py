"""Frame-locked playback: frame schedules, markers, timing, and the PsychoPy renderer.

A session is a sequence of Frame records (target luminances, which target carries the
cue box, and the events the frame shows). ``play`` draws each one, flips (which waits
for vsync), then prints the frame's markers. Everything except PsychoPyRenderer is
display-free; psychopy is imported only when a window is opened.
"""
from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol, TextIO

import numpy as np

from .config import StimulusConfig, Trial
from .flicker import dropped_frames, frame_luminances, n_frames, to_psychopy_rgb

MARKER_CLOCKS: dict[str, Callable[[], float] | None] = {
    "unix": time.time,
    "monotonic": time.monotonic,
    "none": None,
}


class Aborted(Exception):
    """Escape was pressed."""


@dataclass(frozen=True)
class Frame:
    """One display frame. ``trial`` >= 0 marks a flicker frame of that trial."""

    lum: np.ndarray
    cue: int = -1
    events: tuple[str, ...] = ()
    trial: int = -1


def format_marker(stamp: float, event: str) -> str:
    """``[marker <seconds>] <event>``, the line brainsync.recordings.parse_markers reads."""
    return f"[marker {stamp:.4f}] {event}"


class MarkerWriter:
    """Prints event markers to a stream, stamped with a unix or monotonic clock."""

    def __init__(self, mode: str = "unix", stream: TextIO | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        if mode not in MARKER_CLOCKS:
            raise ValueError(f"unknown marker mode {mode!r}; expected one of "
                             f"{sorted(MARKER_CLOCKS)}")
        self.mode = mode
        self.stream = stream
        self.clock = clock if clock is not None else MARKER_CLOCKS[mode]
        self.lines: list[str] = []

    def emit(self, events: Iterable[str]) -> None:
        """Print every event with one timestamp, then flush."""
        events = tuple(events)
        if self.mode == "none" or not events or self.clock is None:
            return
        stamp = self.clock()
        out = self.stream if self.stream is not None else sys.stderr
        for ev in events:
            line = format_marker(stamp, ev)
            self.lines.append(line)
            print(line, file=out)
        out.flush()


def cued_frames(cfg: StimulusConfig, trials: list[Trial]) -> Iterator[Frame]:
    """Frames of a cued session: per trial cue, flicker (from frame 0), then rest.

    Cue and rest frames hold the flicker's frame-0 luminance, so onset has no jump.
    Rest gets at least one frame so stim_off is always shown.
    """
    r = cfg.refresh
    freqs, phases = cfg.freqs, cfg.phases
    still = frame_luminances(freqs, phases, 0, r, cfg.waveform)
    n_cue = n_frames(cfg.session.cue_s, r)
    n_stim = max(1, n_frames(cfg.session.stim_s, r))
    n_rest = max(1, n_frames(cfg.session.rest_s, r))
    for tr in trials:
        k = tr.target
        for i in range(n_cue):
            yield Frame(still, cue=k, events=(f"cue:{k}",) if i == 0 else ())
        stim_cue = k if cfg.hold_cue else -1
        onset = ((f"cue:{k}",) if n_cue == 0 and cfg.hold_cue else ()) + (f"stim_on:{k}",)
        for i in range(n_stim):
            events = onset if i == 0 else ()
            yield Frame(frame_luminances(freqs, phases, i, r, cfg.waveform), cue=stim_cue,
                        events=events, trial=tr.index)
        for i in range(n_rest):
            yield Frame(still, events=(f"stim_off:{k}",) if i == 0 else ())


def free_frames(cfg: StimulusConfig, seconds: float | None = None,
                cue: Callable[[], int] | None = None,
                stop: Callable[[], bool] | None = None,
                hold: Callable[[], bool] | None = None) -> Iterator[Frame]:
    """Continuous flicker; ``cue`` is polled once per frame, ``stop`` ends the run.

    While ``hold()`` is true the targets stand still (frame-0 luminance, no cue box), as
    a still screen for a rest baseline; ``seconds`` counts flicker frames only, and the
    flicker starts from frame 0 so its onset has no jump.
    """
    r = cfg.refresh
    still = frame_luminances(cfg.freqs, cfg.phases, 0, r, cfg.waveform)
    total = None if seconds is None else n_frames(seconds, r)
    holding = hold is not None
    i = 0
    while total is None or i < total:
        if stop is not None and stop():
            return
        if holding and hold():
            yield Frame(still)
            continue
        holding = False
        k = cue() if cue is not None else -1
        yield Frame(frame_luminances(cfg.freqs, cfg.phases, i, r, cfg.waveform), cue=k)
        i += 1


@dataclass
class TrialTiming:
    trial: int
    target: int = -1
    onset: float = float("nan")
    duration: float = float("nan")
    dropped: int = 0


@dataclass
class SessionTiming:
    refresh: float
    measured_refresh: float | None = None
    trials: list[TrialTiming] = field(default_factory=list)
    n_frames: int = 0
    dropped: int = 0
    aborted: bool = False

    @property
    def trial_dropped(self) -> int:
        return sum(t.dropped for t in self.trials)

    def summary(self) -> str:
        """One-line timing report."""
        parts = []
        if self.trials:
            d = [t.duration for t in self.trials if np.isfinite(t.duration)]
            bad = sum(1 for t in self.trials if t.dropped)
            parts.append(f"{len(self.trials)} trials")
            parts.append(f"{self.trial_dropped} dropped flicker frames in {bad} trial(s)")
            if d:
                parts.append(f"flicker {statistics.fmean(d):.3f} s mean "
                             f"({min(d):.3f}-{max(d):.3f})")
        parts.append(f"{self.n_frames} frames, {self.dropped} dropped overall")
        hz = f"refresh {self.refresh:g} Hz"
        if self.measured_refresh is not None:
            hz += f" (measured {self.measured_refresh:.2f})"
        parts.append(hz)
        if self.aborted:
            parts.append("ABORTED")
        return "timing: " + ", ".join(parts)


class Renderer(Protocol):
    def draw(self, lum: np.ndarray, cue: int) -> None: ...
    def flip(self) -> float: ...
    def escape_pressed(self) -> bool: ...


def play(frames: Iterable[Frame], renderer: Renderer, markers: MarkerWriter,
         refresh: float, trial_targets: dict[int, int] | None = None,
         measured_refresh: float | None = None) -> SessionTiming:
    """Draw, flip and mark every frame; per-trial dropped frames and flicker durations.

    A trial's flicker spans its stim_on flip to the flip of the first frame after it; any
    interval in that span longer than 1.5 refresh periods counts as dropped.
    """
    timing = SessionTiming(refresh=refresh, measured_refresh=measured_refresh)
    current: TrialTiming | None = None
    last: float | None = None
    try:
        for fr in frames:
            if renderer.escape_pressed():
                raise Aborted
            renderer.draw(fr.lum, fr.cue)
            t = renderer.flip()
            markers.emit(fr.events)
            timing.n_frames += 1
            late = last is not None and dropped_frames((last, t), refresh) > 0
            timing.dropped += int(late)
            if current is not None:
                current.dropped += int(late)
                if fr.trial != current.trial:
                    current.duration = t - current.onset
                    current = None
            if fr.trial >= 0 and current is None:
                target = (trial_targets or {}).get(fr.trial, -1)
                current = TrialTiming(trial=fr.trial, target=target, onset=t)
                timing.trials.append(current)
            last = t
    except Aborted:
        timing.aborted = True
    return timing


def refresh_mismatch(measured: float, expected: float, tolerance: float) -> str | None:
    """Error text when the measured refresh is off by more than ``tolerance`` Hz."""
    if abs(measured - expected) <= tolerance:
        return None
    return (f"measured refresh {measured:.2f} Hz differs from the configured {expected:g} Hz "
            f"by more than {tolerance:g} Hz; every flicker frequency would be off by "
            f"{measured / expected:.3f}x (turn ProMotion off, or pass --refresh auto / "
            "--ignore-refresh)")


def median_rate(flip_times: list[float] | np.ndarray) -> float:
    """Refresh rate from flip timestamps (median interval)."""
    d = np.diff(np.asarray(flip_times, dtype=float))
    d = d[d > 0]
    if d.size == 0:
        raise ValueError(f"cannot measure refresh from {len(flip_times)} flip(s)")
    return float(1.0 / np.median(d))


def layout_overflow(cfg: StimulusConfig, width_px: float, height_px: float) -> str | None:
    """Warning text when the targets (cue box included) spill past a window's side edges."""
    half_width = width_px / height_px / 2.0
    widest = max(abs(t.pos[0]) for t in cfg.targets) + cfg.reach
    if widest <= half_width + 1e-9:
        return None
    return (f"targets reach x = +/-{widest:.3f} screen heights but this "
            f"{2 * half_width:.3f}:1 window ends at +/-{half_width:.3f}; they will be cut off")


class PsychoPyRenderer:
    """Fullscreen PsychoPy window: one grey square per target plus an outline cue box each."""

    def __init__(self, cfg: StimulusConfig) -> None:
        from psychopy import event, visual  # lazy: never imported by tests

        self.cfg = cfg
        self._visual = visual
        self._keys = event.getKeys
        self._clear_keys = event.clearEvents
        # The window's own timing check is skipped: measure_refresh() does that job.
        self.win = visual.Window(fullscr=True, screen=cfg.display.screen, units="height",
                                 color=to_psychopy_rgb(cfg.display.background),
                                 checkTiming=False)
        self.win.setMouseVisible(False)
        side, box = cfg.size, 2.0 * cfg.reach
        self.squares = []
        self.boxes = []
        for t in cfg.targets:
            self.squares.append(visual.Rect(self.win, size=(side, side), pos=t.pos,
                                            lineColor=None, fillColor=to_psychopy_rgb(0.0)))
            self.boxes.append(visual.Rect(self.win, size=(box, box), pos=t.pos,
                                          fillColor=None, lineColor=cfg.cue_color,
                                          lineWidth=cfg.cue_line_px))
        problem = layout_overflow(cfg, *self.win.size)
        if problem:
            print(f"warning: {problem}", file=sys.stderr)

    def measure_refresh(self, n: int = 120, warmup: int = 20) -> float:
        """Median-interval rate over ``n`` blank flips, after ``warmup`` unmeasured ones."""
        stamps = [self.win.flip() for _ in range(warmup + n)]
        return median_rate(stamps[warmup:])

    def draw(self, lum: np.ndarray, cue: int) -> None:
        for k, square in enumerate(self.squares):
            square.fillColor = to_psychopy_rgb(lum[k])
            square.draw()
        if 0 <= cue < len(self.boxes):
            self.boxes[cue].draw()

    def flip(self) -> float:
        return float(self.win.flip())

    def escape_pressed(self) -> bool:
        return "escape" in self._keys(keyList=["escape"])

    def wait_for_start(self, still: np.ndarray, text: str = "press space to start") -> None:
        """Show the still targets and a prompt until space (Escape raises Aborted)."""
        prompt = self._visual.TextStim(self.win, text=text, pos=(0.0, -0.45), height=0.03,
                                       color=to_psychopy_rgb(0.7))
        self._clear_keys()
        pressed: list[str] = []
        while "space" not in pressed:
            self.draw(still, -1)
            prompt.draw()
            self.win.flip()
            pressed = self._keys(keyList=["space", "escape"])
            if "escape" in pressed:
                raise Aborted

    def close(self) -> None:
        self.win.close()
