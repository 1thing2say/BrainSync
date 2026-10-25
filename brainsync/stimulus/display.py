"""Frame-locked playback: frame schedules, markers, timing, and the PsychoPy renderer.

A session is a sequence of Frame records (target luminances, which target carries the
cue box, the events the frame shows, and for BrainSwipe the profile card and the flashed
target). ``play`` draws each one, flips (which waits for vsync), then prints the frame's
markers. Everything except PsychoPyRenderer is display-free; psychopy is imported only
when a window is opened.

The card and the flash never touch a square's fill: the card is its own image stimulus
and the flash is a thicker, coloured outline box, so the flicker frequencies stay exact.
Text (a status line under the targets, a prompt above them) is only ever on still frames,
except a free run's ``status`` line, which is redrawn on flicker frames too (opt-in: check
the dropped-frame count).
"""
from __future__ import annotations

import statistics
import struct
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO

import numpy as np

from .config import StimulusConfig, Trial, card_overlap
from ..vep.codes import bit_luminance
from .flicker import dropped_frames, frame_luminances, n_frames, to_psychopy_rgb

# The flash box's line is this many times the cue box's, so a swipe reads at a glance.
FLASH_LINE_SCALE = 2.0
LABEL_HEIGHT = 0.045   # screen heights
LABEL_GAP = 0.03       # between a target's cue box and its label
TEXT_HEIGHT = 0.028    # status and prompt lines, screen heights
TEXT_GAP = 0.04        # between the targets' cue boxes and the status or prompt text
RATE_KEYS = ("1", "2", "3", "4", "5")
RATE_PROMPT = ("How comfortable was that flicker?\n"
               "1 unpleasant  2  3 fine  4  5 comfortable")
END_PROMPT = "Session over. Press space to close."
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

MARKER_CLOCKS: dict[str, Callable[[], float] | None] = {
    "unix": time.time,
    "monotonic": time.monotonic,
    "none": None,
}


class Aborted(Exception):
    """Escape was pressed."""


@dataclass(frozen=True)
class Frame:
    """One display frame. ``trial`` >= 0 marks a flicker frame of that trial.

    ``card`` is the profile card shown (-1: none) and ``flash`` the target whose box is
    flashing (-1: none); neither affects ``lum``. ``text`` is drawn under the targets and
    ``prompt`` above them ("": nothing).
    """

    lum: np.ndarray
    cue: int = -1
    events: tuple[str, ...] = ()
    trial: int = -1
    card: int = -1
    flash: int = -1
    text: str = ""
    prompt: str = ""


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


def still_luminances(cfg: StimulusConfig) -> np.ndarray:
    """What the targets show between flickers: the flicker's frame 0, so onset has no jump.

    A mixed-condition session shows mid-grey 0.5 instead (the mean of every waveform at
    every depth), so the still screen is the same whatever condition comes next. A [code]
    config also rests at 0.5 (the m-sequence is bright half the time); a [time] config
    rests at its "off" level (0.5 - depth / 2), since a flash is the only bright state.
    """
    if cfg.conditions or cfg.code is not None:
        return np.full(cfg.n_targets, 0.5)
    if cfg.time is not None:
        return bit_luminance(np.zeros(cfg.n_targets), cfg.depth)
    return frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform, cfg.depth)


def cued_frames(cfg: StimulusConfig, trials: list[Trial],
                status: Callable[[], str] | None = None,
                rate: Callable[[], int | None] | None = None,
                finish: Callable[[], bool] | None = None) -> Iterator[Frame]:
    """Frames of a cued session: per trial cue, flicker (from frame 0), then rest.

    Cue and rest frames hold still_luminances. Rest gets at least one frame so stim_off
    is always shown. In a mixed-condition session each trial flickers with its own
    condition, and its first frame also carries a cond:<j> marker.

    ``status()`` is polled on every still frame (never during the flicker) for the text
    under the targets. With ``rate``, a comfort prompt follows each rest until ``rate()``
    returns a rating, marked rate:<n>; it is polled once before the prompt shows, to
    drop keys pressed earlier. With ``finish``, still frames follow the last trial until
    ``finish()`` turns true.
    """
    r = cfg.refresh
    freqs, phases = cfg.freqs, cfg.phases
    still = still_luminances(cfg)
    n_cue = n_frames(cfg.session.cue_s, r)
    n_stim = max(1, n_frames(cfg.session.stim_s, r))
    n_rest = max(1, n_frames(cfg.session.rest_s, r))

    def note() -> str:
        return status() if status is not None else ""

    for tr in trials:
        k = tr.target
        if cfg.conditions:
            cond = cfg.conditions[tr.condition]
            waveform, depth, head = cond.waveform, cond.depth, (f"cond:{tr.condition}",)
        else:
            waveform, depth, head = cfg.waveform, cfg.depth, ()
        for i in range(n_cue):
            yield Frame(still, cue=k, events=head + (f"cue:{k}",) if i == 0 else (),
                        text=note())
        stim_cue = k if cfg.hold_cue else -1
        onset = ((head + ((f"cue:{k}",) if cfg.hold_cue else ())) if n_cue == 0 else ()) \
            + (f"stim_on:{k}",)
        for i in range(n_stim):
            events = onset if i == 0 else ()
            yield Frame(frame_luminances(freqs, phases, i, r, waveform, depth),
                        cue=stim_cue, events=events, trial=tr.index)
        for i in range(n_rest):
            yield Frame(still, events=(f"stim_off:{k}",) if i == 0 else (), text=note())
        if rate is not None:
            rate()
            while (score := rate()) is None:
                yield Frame(still, text=note(), prompt=RATE_PROMPT)
            yield Frame(still, events=(f"rate:{score}",), text=note())
    if finish is not None:
        while not finish():
            yield Frame(still, text=note(), prompt=END_PROMPT)


def free_frames(cfg: StimulusConfig, seconds: float | None = None,
                cue: Callable[[], int] | None = None,
                stop: Callable[[], bool] | None = None,
                hold: Callable[[], bool] | None = None,
                card: Callable[[], int] | None = None,
                flash: Callable[[], tuple[int, int]] | None = None,
                status: Callable[[], str] | None = None) -> Iterator[Frame]:
    """Continuous flicker; ``cue`` is polled once per frame, ``stop`` ends the run.

    ``status()`` is polled on every frame, flicker frames included, for the text under
    the targets (a decoder's confidence line); None draws none.

    While ``hold()`` is true the targets stand still (frame-0 luminance, no cue box), as
    a still screen for a rest baseline; ``seconds`` counts flicker frames only, and the
    flicker starts from frame 0 so its onset has no jump.

    ``card`` (the card index) and ``flash`` (target, serial) are polled once per frame,
    still frames included. A serial different from the last one seen (initially 0, as
    FeedbackReader starts) lights that target's box for [card] flash_s, restarting any
    flash in progress. Neither changes the luminances.
    """
    r = cfg.refresh
    still = frame_luminances(cfg.freqs, cfg.phases, 0, r, cfg.waveform, cfg.depth)
    total = None if seconds is None else n_frames(seconds, r)
    flash_len = n_frames(cfg.card_spec.flash_s, r)
    holding = hold is not None
    seen, flashing, left = 0, -1, 0
    i = 0
    while total is None or i < total:
        if stop is not None and stop():
            return
        shown = card() if card is not None else -1
        if flash is not None:
            target, serial = flash()
            if serial != seen:
                seen, flashing, left = serial, target, flash_len
        lit = flashing if left > 0 else -1
        left = max(0, left - 1)
        text = status() if status is not None else ""
        if holding and hold():
            yield Frame(still, card=shown, flash=lit, text=text)
            continue
        holding = False
        k = cue() if cue is not None else -1
        yield Frame(frame_luminances(cfg.freqs, cfg.phases, i, r, cfg.waveform, cfg.depth),
                    cue=k, card=shown, flash=lit, text=text)
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
    """``play`` passes ``card`` and ``flash``, or ``text`` and ``prompt``, only on frames
    that set them, so a renderer for plain sessions may take just ``(lum, cue)``."""

    def draw(self, lum: np.ndarray, cue: int, card: int = -1, flash: int = -1,
             text: str = "", prompt: str = "") -> None: ...
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
            extra: dict[str, int | str] = {}
            if fr.card >= 0 or fr.flash >= 0:
                extra.update(card=fr.card, flash=fr.flash)
            if fr.text or fr.prompt:
                extra.update(text=fr.text, prompt=fr.prompt)
            renderer.draw(fr.lum, fr.cue, **extra)
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


def label_pos(cfg: StimulusConfig, k: int) -> tuple[float, float]:
    """Centre of target ``k``'s label: just below its cue box, never over the square."""
    x, y = cfg.targets[k].pos
    return (x, y - cfg.reach - LABEL_GAP - LABEL_HEIGHT / 2.0)


def card_paths(directory: str | Path) -> list[Path]:
    """The card images in ``directory``: its *.png files sorted by name (card k is [k])."""
    d = Path(directory).expanduser()
    if not d.is_dir():
        raise ValueError(f"--cards {str(directory)!r} is not a directory")
    paths = sorted((p for p in d.glob("*.png") if p.is_file()), key=lambda p: p.name)
    if not paths:
        raise ValueError(f"--cards {str(directory)!r} has no .png files")
    return paths


def png_size(path: str | Path) -> tuple[int, int]:
    """(width, height) in pixels from a PNG's IHDR header, without decoding the image."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a PNG file")
    width, height = struct.unpack(">II", head[16:24])
    if width == 0 or height == 0:
        raise ValueError(f"{path} has an empty image ({width} x {height})")
    return width, height


class PsychoPyRenderer:
    """Fullscreen PsychoPy window: one grey square per target plus an outline cue box each.

    With ``cards``, every PNG becomes one ImageStim at init (textures are never loaded in
    the frame loop), centred, [card] height high and as wide as its own aspect says, and
    each target's label (Pass, Like) is written under its cue box in its flash colour, so
    the audience can see what each square does; ``labels`` writes them (light grey)
    without cards. Labels are static and drawn outside the
    squares, so they never touch the flicker. Status text goes under the targets and
    prompts above them, in a monospaced font so a status table lines up; a text stimulus
    is re-rendered only when its text changes.
    """

    def __init__(self, cfg: StimulusConfig, cards: Sequence[str | Path] = (),
                 labels: bool = False) -> None:
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
        self.flashes = []
        for t in cfg.targets:
            self.squares.append(visual.Rect(self.win, size=(side, side), pos=t.pos,
                                            lineColor=None, fillColor=to_psychopy_rgb(0.0)))
            self.boxes.append(visual.Rect(self.win, size=(box, box), pos=t.pos,
                                          fillColor=None, lineColor=cfg.cue_color,
                                          lineWidth=cfg.cue_line_px))
            self.flashes.append(visual.Rect(self.win, size=(box, box), pos=t.pos,
                                            fillColor=None, lineColor=cfg.flash_color(t.index),
                                            lineWidth=cfg.cue_line_px * FLASH_LINE_SCALE))
        problem = layout_overflow(cfg, *self.win.size)
        if problem:
            print(f"warning: {problem}", file=sys.stderr)
        height = cfg.card_spec.height
        self.labels = []
        if cards or labels:
            for t in cfg.targets:
                color = cfg.flash_color(t.index) if cards else to_psychopy_rgb(0.9)
                self.labels.append(visual.TextStim(self.win, text=t.label,
                                                   pos=label_pos(cfg, t.index),
                                                   height=LABEL_HEIGHT, color=color))
        bottom = min(t.pos[1] for t in cfg.targets) - cfg.reach - TEXT_GAP
        top = max(t.pos[1] for t in cfg.targets) + cfg.reach + TEXT_GAP
        self.status = visual.TextStim(self.win, text="", pos=(0.0, bottom), height=TEXT_HEIGHT,
                                      color=to_psychopy_rgb(0.75), font="Menlo",
                                      wrapWidth=1.5, anchorVert="top", alignText="left")
        self.prompt = visual.TextStim(self.win, text="", pos=(0.0, top), height=TEXT_HEIGHT,
                                      color=to_psychopy_rgb(0.9), font="Menlo",
                                      wrapWidth=1.5, anchorVert="bottom")
        self._texts: dict[int, str] = {}
        self._escaped = False
        self.cards = []
        for path in cards:
            w, h = png_size(path)
            overlap = card_overlap(cfg, aspect=w / h)
            if overlap:
                print(f"warning: {Path(path).name}: {overlap}", file=sys.stderr)
            self.cards.append(visual.ImageStim(self.win, image=str(path), pos=(0.0, 0.0),
                                               size=(height * w / h, height),
                                               interpolate=True))

    def measure_refresh(self, n: int = 120, warmup: int = 20) -> float:
        """Median-interval rate over ``n`` blank flips, after ``warmup`` unmeasured ones."""
        stamps = [self.win.flip() for _ in range(warmup + n)]
        return median_rate(stamps[warmup:])

    def _draw_text(self, stim: object, text: str) -> None:
        if not text:
            return
        if self._texts.get(id(stim)) != text:
            stim.text = text
            self._texts[id(stim)] = text
        stim.draw()

    def draw(self, lum: np.ndarray, cue: int, card: int = -1, flash: int = -1,
             text: str = "", prompt: str = "") -> None:
        # the card goes first so a square drawn over an oversized card keeps its luminance
        if 0 <= card < len(self.cards):
            self.cards[card].draw()
        for k, square in enumerate(self.squares):
            square.fillColor = to_psychopy_rgb(lum[k])
            square.draw()
        for label in self.labels:
            label.draw()
        if 0 <= cue < len(self.boxes):
            self.boxes[cue].draw()
        if 0 <= flash < len(self.flashes):
            self.flashes[flash].draw()
        self._draw_text(self.status, text)
        self._draw_text(self.prompt, prompt)

    def flip(self) -> float:
        return float(self.win.flip())

    def escape_pressed(self) -> bool:
        if self._escaped:
            return True
        return "escape" in self._keys(keyList=["escape"])

    def poll_keys(self, keys: Sequence[str]) -> list[str]:
        """Which of ``keys`` were pressed since the last poll. An Escape seen here is kept
        for the next escape_pressed(), so polling never swallows it."""
        pressed = self._keys(keyList=[*keys, "escape"])
        if "escape" in pressed:
            self._escaped = True
        return [k for k in pressed if k != "escape"]

    def rating(self) -> int | None:
        """The last comfort key (1-5) pressed since the previous call, or None."""
        pressed = self.poll_keys(RATE_KEYS)
        return int(pressed[-1]) if pressed else None

    def close_pressed(self) -> bool:
        """Space or Escape on the end screen: closes it normally (not an abort)."""
        return bool(self._keys(keyList=["space", "escape"]))

    def wait_for_start(self, still: np.ndarray, text: str = "press space to start") -> None:
        """Show the still targets and a prompt until space (Escape raises Aborted)."""
        prompt = self._visual.TextStim(self.win, text=text, pos=(0.0, -0.45), height=0.03,
                                       color=to_psychopy_rgb(0.7))
        self._clear_keys()
        pressed: list[str] = []
        while "space" not in pressed:
            self.draw(still, -1, card=0 if self.cards else -1)
            prompt.draw()
            self.win.flip()
            pressed = self._keys(keyList=["space", "escape"])
            if "escape" in pressed:
                raise Aborted

    def close(self) -> None:
        self.win.close()
