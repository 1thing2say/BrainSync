"""Frame-locked playback: frame schedules, markers, timing, and the PsychoPy renderer.

A session is a sequence of Frame records (target luminances, which target carries the
cue box, the events the frame shows, and for BrainSwipe the profile card and the flashed
target). ``play`` draws each one, flips (which waits for vsync), then prints the frame's
markers. Everything except PsychoPyRenderer is display-free; psychopy is imported only
when a window is opened.

The card and the flash never touch a square's fill: the card is its own image stimulus
and the flash is a thicker, coloured outline box, so the flicker frequencies stay exact.
Text (a status line under the targets, a prompt above them) is only ever on still frames.

A bridge driving the free run with JSON lines (brainsync.stimulus.feedback) sets a
sentence beside each square and a line at the centre (placed by ``text_boxes``, so no
text covers a square), and turns the flicker on and off for chosen targets only; the
first flickering frame is marked "flicker_on" and the first still frame after it
"flicker_off". A fixation mark ([stimulus] fixation) sits on each square's centre.
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

from . import shapes
from .config import StimulusConfig, Trial, card_overlap
from ..vep.codes import bit_luminance
from .feedback import Control
from .flicker import (dropped_frames, frame_luminances, n_frames, to_psychopy_fill,
                      to_psychopy_rgb)

# The flash box's line is this many times the cue box's, so a swipe reads at a glance.
FLASH_LINE_SCALE = 2.0
LABEL_HEIGHT = 0.045   # screen heights
LABEL_GAP = 0.03       # between a target's cue box and its label
TEXT_HEIGHT = 0.028    # status and prompt lines, screen heights
TEXT_GAP = 0.04        # between the targets' cue boxes and the status or prompt text
STATUS_WRAP = 1.5      # widest status or prompt line, screen heights
START_PROMPT = "press space to start"
START_PROMPT_POS = (0.0, -0.45)   # where the start prompt goes unless it would cover a target
START_PROMPT_HEIGHT = 0.03
START_PROMPT_WIDTH = 0.4          # room kept for it (about 20 characters at that height)
# Decoder-sent text: one sentence beside each square, one line at the centre.
SLOT_TEXT_HEIGHT = 0.03   # letter height, screen heights
SLOT_TEXT_WRAP = 0.44     # widest line, screen heights (about 26 characters)
SLOT_TEXT_LINES = 4       # lines kept free for each sentence (14 words fit in 3)
CENTRE_TEXT_HEIGHT = 0.035
CENTRE_TEXT_WRAP = 0.6
CENTRE_TEXT_LINES = 3
CENTRE_TEXT_MIN_WRAP = 0.2   # narrower than this between the squares: put it at the bottom
LINE_SPACING = 1.25       # a text line's height over its letter height, with room to spare
TEXT_MARGIN = 0.02        # kept clear at the screen edges
FLICKER_ON, FLICKER_OFF = "flicker_on", "flicker_off"
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
    ``prompt`` above them ("": nothing). ``labels`` holds one sentence per target, drawn
    beside its square, and ``centre`` a line at the centre of the screen (both sent by a
    decoder; empty: nothing).
    """

    lum: np.ndarray
    cue: int = -1
    events: tuple[str, ...] = ()
    trial: int = -1
    card: int = -1
    flash: int = -1
    text: str = ""
    prompt: str = ""
    labels: tuple[str, ...] = ()
    centre: str = ""


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
                control: Callable[[], Control] | None = None,
                seq: FrameSequence | None = None) -> Iterator[Frame]:
    """Continuous flicker; ``cue`` is polled once per frame, ``stop`` ends the run.

    While ``hold()`` is true the targets stand still (frame-0 luminance, no cue box), as
    a still screen for a rest baseline; ``seconds`` counts flicker frames only, and the
    flicker starts from frame 0 so its onset has no jump.

    ``card`` (the card index) and ``flash`` (target, serial) are polled once per frame,
    still frames included. A serial different from the last one seen (initially 0, as
    FeedbackReader starts) lights that target's box for [card] flash_s, restarting any
    flash in progress. Neither changes the luminances.

    ``control`` (a feedback Control) is polled once per frame too. Once its ``used`` is
    true, ``hold`` no longer matters: the targets stand still until a flicker-on (a new
    ``serial`` with ``flicker`` true), then only its ``flickering`` targets flicker, from
    frame 0, while the rest show their still luminance; ``flicker`` false holds them all
    still again. The first flickering frame carries "flicker_on" and the first still frame
    after it "flicker_off"; the frames of each flicker run share a ``trial`` number (0, 1,
    ...), so play() times every run and counts its dropped frames. The cue box follows
    ``cue`` on every frame, and the labels and centre text show on every frame (hidden
    while flickering with [stimulus] labels_during_flicker = false). Before the first JSON
    line the labels are the Control's starting ones (FeedbackReader ``labels``).

    ``seq`` replaces the frequency flicker for a [code] or [time] config
    (brainsync.stimulus.coded): flicker frame ``i`` of a run shows ``seq.luminance(i)`` and
    carries ``seq.events(i)`` (cycle:<n>, flash:<k>), the still screen is ``seq.still``, and
    every flicker-on restarts the sequence.
    """
    r = cfg.refresh
    if seq is None:
        still = frame_luminances(cfg.freqs, cfg.phases, 0, r, cfg.waveform, cfg.depth)

        def lum_at(i: int) -> np.ndarray:
            return frame_luminances(cfg.freqs, cfg.phases, i, r, cfg.waveform, cfg.depth)

        def events_at(i: int) -> tuple[str, ...]:
            return ()
    else:
        still, lum_at, events_at = seq.still, seq.luminance, seq.events
    total = None if seconds is None else n_frames(seconds, r)
    flash_len = n_frames(cfg.card_spec.flash_s, r)
    holding = hold is not None
    seen, flashing, left = 0, -1, 0
    i = 0            # frames since this flicker run began: the flicker's phase
    shown = 0        # flicker frames so far, what ``seconds`` counts
    flickering = False
    run, run_serial = -1, 0
    mask = np.ones(cfg.n_targets, dtype=bool)
    while total is None or shown < total:
        if stop is not None and stop():
            return
        image = card() if card is not None else -1
        if flash is not None:
            target, serial = flash()
            if serial != seen:
                seen, flashing, left = serial, target, flash_len
        lit = flashing if left > 0 else -1
        left = max(0, left - 1)
        # hold() before control(): the reader applies each line in order, so when an integer
        # line that came after a JSON line has ended the hold, control() already shows
        # ``used`` and this frame takes the JSON branch, never the legacy flicker below.
        held = holding and hold()
        ctl = control() if control is not None else None
        if ctl is not None and ctl.used:
            events: tuple[str, ...] = ()
            if ctl.flicker and ctl.serial != run_serial:
                # a new flicker-on: phase back to 0, only the targets active right now
                run_serial, run, i, flickering = ctl.serial, run + 1, 0, True
                if seq is not None:
                    seq.restart()
                mask = np.zeros(cfg.n_targets, dtype=bool)
                mask[list(ctl.flickering)] = True
                events = (FLICKER_ON,)
            elif not ctl.flicker and flickering:
                flickering = False
                events = (FLICKER_OFF,)
            k = cue() if cue is not None else -1
            words = not flickering or cfg.labels_during_flicker
            labels, centre = (ctl.labels, ctl.text) if words else ((), "")
            if not flickering:
                yield Frame(still, cue=k, events=events, card=image, flash=lit,
                            labels=labels, centre=centre)
                continue
            lum = np.where(mask, lum_at(i), still)
            yield Frame(lum, cue=k, events=events + events_at(i), trial=run, card=image,
                        flash=lit, labels=labels, centre=centre)
            i += 1
            shown += 1
            continue
        start = ctl.labels if ctl is not None else ()
        if held:
            yield Frame(still, card=image, flash=lit, labels=start)
            continue
        holding, flickering = False, True
        k = cue() if cue is not None else -1
        yield Frame(lum_at(i), cue=k, events=events_at(i), card=image, flash=lit,
                    labels=start if cfg.labels_during_flicker else ())
        i += 1
        shown += 1


class FrameSequence(Protocol):
    """A coded stimulus (brainsync.stimulus.coded) as free_frames draws it."""

    still: np.ndarray

    def restart(self) -> None: ...
    def luminance(self, i: int) -> np.ndarray: ...
    def events(self, i: int) -> tuple[str, ...]: ...


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
    """``play`` passes ``card`` and ``flash``, ``text`` and ``prompt``, or ``labels`` and
    ``centre`` only on frames that set them, so a renderer for plain sessions may take
    just ``(lum, cue)``."""

    def draw(self, lum: np.ndarray, cue: int, card: int = -1, flash: int = -1,
             text: str = "", prompt: str = "", labels: Sequence[str] = (),
             centre: str = "") -> None: ...
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
            extra: dict[str, int | str | tuple[str, ...]] = {}
            if fr.card >= 0 or fr.flash >= 0:
                extra.update(card=fr.card, flash=fr.flash)
            if fr.text or fr.prompt:
                extra.update(text=fr.text, prompt=fr.prompt)
            if any(fr.labels) or fr.centre:
                extra.update(labels=fr.labels, centre=fr.centre)
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


@dataclass(frozen=True)
class TextBox:
    """Where one block of text goes. ``pos`` is the TextStim anchor point, ``anchor`` its
    (horizontal, vertical) anchor and ``align`` the line alignment, as PsychoPy names them;
    ``width`` is the wrap width and ``height`` the room kept free for the text's lines, so
    ``rect`` (left, bottom, right, top) is the most the text should cover."""

    pos: tuple[float, float]
    width: float
    height: float
    anchor: tuple[str, str]
    align: str
    side: str = "centre"

    @property
    def rect(self) -> tuple[float, float, float, float]:
        x, y = self.pos
        h, v = self.anchor
        left = {"left": x, "center": x - self.width / 2.0, "right": x - self.width}[h]
        bottom = {"bottom": y, "center": y - self.height / 2.0, "top": y - self.height}[v]
        return (left, bottom, left + self.width, bottom + self.height)


def _label_side(cfg: StimulusConfig, k: int) -> str:
    """Which side of target ``k`` its sentence goes on.

    In a single row every sentence goes below its square. In a grid of several rows and
    columns each one goes to the outer side of its square (left half: left, right half:
    right, a middle column: below), clear of the squares above and below it. Otherwise each
    one goes on the side a clockwise turn around the layout's centre points to (top square:
    right, right square: below, bottom: left, left: above), so in the diamond each sentence
    gets a corner of the screen to itself and none crosses another square.
    """
    xs = [t.pos[0] for t in cfg.targets]
    ys = [t.pos[1] for t in cfg.targets]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    if max(ys) - min(ys) < 1e-9:
        return "below"
    x, y = cfg.targets[k].pos
    dx, dy = x - cx, y - cy
    if cfg.shape == "grid" and not cfg.positioned and cfg.rows > 1 and cfg.cols > 1:
        return "below" if abs(dx) < 1e-9 else "right" if dx > 0 else "left"
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return "below"
    tx, ty = dy, -dx   # clockwise tangent
    if abs(tx) >= abs(ty):
        return "right" if tx > 0 else "left"
    return "below" if ty < 0 else "above"


def text_boxes(cfg: StimulusConfig, aspect: float | None = None
               ) -> tuple[tuple[TextBox, ...], TextBox]:
    """Where each target's sentence and the centre line go on a ``aspect``:1 screen
    (default [display] aspect, the narrowest the layout must fit).

    A sentence sits just outside its square's cue box on the side _label_side picks,
    SLOT_TEXT_WRAP wide at most and kept TEXT_MARGIN inside the screen; the centre line
    goes between the squares, or at the bottom of the screen when they leave too little
    room. text_overlaps() checks the result.
    """
    half_w = (cfg.display.aspect if aspect is None else aspect) / 2.0 - TEXT_MARGIN
    half_h = 0.5 - TEXT_MARGIN
    reach, gap = cfg.reach, LABEL_GAP
    h = SLOT_TEXT_LINES * SLOT_TEXT_HEIGHT * LINE_SPACING
    out = []
    for t in cfg.targets:
        x, y = t.pos
        side = _label_side(cfg, t.index)
        if side == "right":
            x0 = x + reach + gap
            out.append(TextBox((x0, y), max(0.0, min(SLOT_TEXT_WRAP, half_w - x0)), h,
                               ("left", "center"), "left", side))
        elif side == "left":
            x1 = x - reach - gap
            out.append(TextBox((x1, y), max(0.0, min(SLOT_TEXT_WRAP, x1 + half_w)), h,
                               ("right", "center"), "right", side))
        else:
            w = min(SLOT_TEXT_WRAP, 2.0 * half_w)
            cx = min(max(x, -half_w + w / 2.0), half_w - w / 2.0)
            if side == "below":
                out.append(TextBox((cx, y - reach - gap), w, h, ("center", "top"),
                                   "center", side))
            else:
                out.append(TextBox((cx, y + reach + gap), w, h, ("center", "bottom"),
                                   "center", side))
    xs = [t.pos[0] for t in cfg.targets]
    ys = [t.pos[1] for t in cfg.targets]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    hc = CENTRE_TEXT_LINES * CENTRE_TEXT_HEIGHT * LINE_SPACING
    # the room left and right of the centre among the squares level with the centre line
    level = [abs(t.pos[0] - cx) - reach for t in cfg.targets
             if abs(t.pos[1] - cy) < reach + hc / 2.0]
    room = 2.0 * (min(level) - gap) if level else 2.0 * half_w
    width = min(CENTRE_TEXT_WRAP, room, 2.0 * half_w)
    above = [t.pos[1] - reach for t in cfg.targets
             if t.pos[1] > cy and abs(t.pos[0] - cx) < reach + width / 2.0]
    below = [t.pos[1] + reach for t in cfg.targets
             if t.pos[1] < cy and abs(t.pos[0] - cx) < reach + width / 2.0]
    tall = min(above, default=half_h) - max(below, default=-half_h) - 2.0 * gap
    if width >= CENTRE_TEXT_MIN_WRAP and tall >= CENTRE_TEXT_HEIGHT * LINE_SPACING:
        # between the squares, with fewer lines when the gap is too low for all of them
        x = min(max(cx, -half_w + width / 2.0), half_w - width / 2.0)   # stay on screen
        centre = TextBox((x, cy), width, min(hc, tall), ("center", "center"), "center")
    else:
        # a square on the centre: the line goes along the bottom of the screen instead
        centre = TextBox((0.0, -half_h), min(CENTRE_TEXT_WRAP * 2.0, 2.0 * half_w), hc,
                         ("center", "bottom"), "center", "bottom")
    return tuple(out), centre


def status_prompt_boxes(cfg: StimulusConfig, aspect: float | None = None
                        ) -> tuple[TextBox, TextBox]:
    """Where the status lines (under the targets) and the prompt (above them) go on a
    ``aspect``:1 screen (default [display] aspect).

    When the targets reach so close to the bottom (top) edge that not even one line fits
    there, as in four_diamond, the status (prompt) moves to the centre between the squares:
    the prompt ends just above the middle, the status starts just below it, both as wide as
    the centre line's box. With a square on the centre itself they go along the screen's
    bottom and top edges instead, over whatever is there, so they are always on screen.
    """
    ratio = cfg.display.aspect if aspect is None else aspect
    half_h = 0.5 - TEXT_MARGIN
    wrap = min(STATUS_WRAP, ratio - 2.0 * TEXT_MARGIN)
    bottom = min(t.pos[1] for t in cfg.targets) - cfg.reach - TEXT_GAP
    top = max(t.pos[1] for t in cfg.targets) + cfg.reach + TEXT_GAP
    _, centre = text_boxes(cfg, ratio)
    middle = centre.side != "bottom"   # room between the squares
    cx, cy = centre.pos
    if bottom - TEXT_HEIGHT * LINE_SPACING >= -half_h:
        status = TextBox((0.0, bottom), wrap, bottom + half_h, ("center", "top"), "left", "below")
    elif middle:
        status = TextBox((cx, cy - LABEL_GAP / 2.0), centre.width, centre.height / 2.0,
                         ("center", "top"), "left", "centre")
    else:
        status = TextBox((0.0, -half_h), wrap, TEXT_HEIGHT * LINE_SPACING,
                         ("center", "bottom"), "left", "bottom")
    if top + TEXT_HEIGHT * LINE_SPACING <= half_h:
        prompt = TextBox((0.0, top), wrap, half_h - top, ("center", "bottom"), "center", "above")
    elif middle:
        prompt = TextBox((cx, cy + LABEL_GAP / 2.0), centre.width, centre.height / 2.0,
                         ("center", "bottom"), "center", "centre")
    else:
        prompt = TextBox((0.0, half_h), wrap, TEXT_HEIGHT * LINE_SPACING,
                         ("center", "top"), "center", "top")
    return status, prompt


def start_prompt_box(cfg: StimulusConfig, aspect: float | None = None) -> TextBox:
    """Where "press space to start" goes: low on the screen (START_PROMPT_POS), or where
    status_prompt_boxes puts the prompt when that spot would cover a target's cue box."""
    box = TextBox(START_PROMPT_POS, START_PROMPT_WIDTH, START_PROMPT_HEIGHT * LINE_SPACING,
                  ("center", "center"), "center", "below")
    feet = [(t.pos[0] - cfg.reach, t.pos[1] - cfg.reach, t.pos[0] + cfg.reach,
             t.pos[1] + cfg.reach) for t in cfg.targets]
    if not any(_rects_overlap(box.rect, foot) for foot in feet):
        return box
    return status_prompt_boxes(cfg, aspect)[1]


def _rects_overlap(a: tuple[float, float, float, float],
                   b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] - 1e-9 and b[0] < a[2] - 1e-9 and a[1] < b[3] - 1e-9 \
        and b[1] < a[3] - 1e-9


def text_overlaps(cfg: StimulusConfig, aspect: float | None = None) -> list[str]:
    """Problems with text_boxes(cfg, aspect): a text box over any target's cue box, over
    another text box, or past the screen edge. Empty when the layout is clean."""
    ratio = cfg.display.aspect if aspect is None else aspect
    slots, centre = text_boxes(cfg, ratio)
    named = [(f"target {k}'s sentence", b) for k, b in enumerate(slots)]
    named.append(("the centre line", centre))
    feet = [(t.index, (t.pos[0] - cfg.reach, t.pos[1] - cfg.reach, t.pos[0] + cfg.reach,
                       t.pos[1] + cfg.reach)) for t in cfg.targets]
    problems = []
    for i, (name, box) in enumerate(named):
        r = box.rect
        if box.width <= 0:
            problems.append(f"{name} has no room beside its square")
        for k, foot in feet:
            if _rects_overlap(r, foot):
                problems.append(f"{name} covers target {k}")
        for other, obox in named[i + 1:]:
            if _rects_overlap(r, obox.rect):
                problems.append(f"{name} overlaps {other}")
        if r[0] < -ratio / 2 - 1e-9 or r[2] > ratio / 2 + 1e-9 or r[1] < -0.5 - 1e-9 \
                or r[3] > 0.5 + 1e-9:
            problems.append(f"{name} runs past the screen edge")
    return problems


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
    """Fullscreen PsychoPy window: one grey square (or [targets] shapes outline) per target
    plus an outline cue box each.

    With ``cards``, every PNG becomes one ImageStim at init (textures are never loaded in
    the frame loop), centred, [card] height high and as wide as its own aspect says, and
    each target's label (Pass, Like) is written under its cue box in its flash colour, so
    the audience can see what each square does. Labels are static and drawn outside the
    squares, so they never touch the flicker. Status text goes under the targets and
    prompts above them, in a monospaced font so a status table lines up; a text stimulus
    is re-rendered only when its text changes.

    The decoder's sentences (one TextStim per target, placed by text_boxes for this
    window's aspect) and its centre line are created once, empty, and drawn only while
    they hold text. With [stimulus] fixation each square gets a small plus in the cue
    colour at its centre, drawn over the square on every frame. The squares are grey unless
    the config's [stimulus] fill tints them.
    """

    def __init__(self, cfg: StimulusConfig, cards: Sequence[str | Path] = ()) -> None:
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
        self.squares = []   # the flicker targets, whatever their shape
        self.boxes = []
        self.flashes = []
        for t in cfg.targets:
            if t.shape == "square":
                self.squares.append(visual.Rect(self.win, size=(side, side), pos=t.pos,
                                                lineColor=None, fillColor=to_psychopy_rgb(0.0)))
            else:   # ShapeStim tessellates the concave plus and arrows
                self.squares.append(visual.ShapeStim(self.win,
                                                     vertices=shapes.vertices(t.shape, side),
                                                     pos=t.pos, lineColor=None,
                                                     fillColor=to_psychopy_rgb(0.0)))
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
        if cards:
            for t in cfg.targets:
                self.labels.append(visual.TextStim(self.win, text=t.label,
                                                   pos=label_pos(cfg, t.index),
                                                   height=LABEL_HEIGHT,
                                                   color=cfg.flash_color(t.index)))
        aspect = self.win.size[0] / self.win.size[1]
        # under and above the targets, or between them when they reach the screen's edge
        status, prompt = status_prompt_boxes(cfg, aspect)
        self.status = visual.TextStim(self.win, text="", pos=status.pos, height=TEXT_HEIGHT,
                                      color=to_psychopy_rgb(0.75), font="Menlo",
                                      wrapWidth=status.width, anchorHoriz=status.anchor[0],
                                      anchorVert=status.anchor[1], alignText=status.align)
        self.prompt = visual.TextStim(self.win, text="", pos=prompt.pos, height=TEXT_HEIGHT,
                                      color=to_psychopy_rgb(0.9), font="Menlo",
                                      wrapWidth=prompt.width, anchorHoriz=prompt.anchor[0],
                                      anchorVert=prompt.anchor[1], alignText=prompt.align)
        self._start_box = start_prompt_box(cfg, aspect)
        slots, centre = text_boxes(cfg, aspect)
        # reported on the first frame that shows decoder text, so a session that never
        # gets any (a cued recording, BrainSwipe) prints nothing
        self._text_problems = text_overlaps(cfg, aspect)
        self.slot_texts = [
            visual.TextStim(self.win, text="", pos=b.pos, height=SLOT_TEXT_HEIGHT,
                            wrapWidth=max(b.width, SLOT_TEXT_HEIGHT),
                            color=to_psychopy_rgb(0.9),
                            anchorHoriz=b.anchor[0], anchorVert=b.anchor[1],
                            alignText=b.align)
            for b in slots]
        self.centre = visual.TextStim(self.win, text="", pos=centre.pos,
                                      height=CENTRE_TEXT_HEIGHT, wrapWidth=centre.width,
                                      color=to_psychopy_rgb(0.95),
                                      anchorHoriz=centre.anchor[0],
                                      anchorVert=centre.anchor[1], alignText=centre.align)
        # a plus at each square's centre: two thin bars, in the cue colour so it shows on
        # both the dark and the light phase of the flicker
        self.fixations = []
        if cfg.fixation:
            arm, bar = cfg.fixation_size, cfg.fixation_size / 5.0
            for t in cfg.targets:
                for size in ((arm, bar), (bar, arm)):
                    self.fixations.append(visual.Rect(self.win, size=size, pos=t.pos,
                                                      lineColor=None,
                                                      fillColor=cfg.cue_color))
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
             text: str = "", prompt: str = "", labels: Sequence[str] = (),
             centre: str = "") -> None:
        # the card goes first so a square drawn over an oversized card keeps its luminance
        if 0 <= card < len(self.cards):
            self.cards[card].draw()
        for k, square in enumerate(self.squares):
            square.fillColor = to_psychopy_fill(lum[k], self.cfg.fill)
            square.draw()
        for mark in self.fixations:
            mark.draw()
        for label in self.labels:
            label.draw()
        if self._text_problems and (any(labels) or centre):
            for problem in self._text_problems:
                print(f"warning: decoder text: {problem}", file=sys.stderr, flush=True)
            self._text_problems = []
        for stim, words in zip(self.slot_texts, labels):
            self._draw_text(stim, words)
        self._draw_text(self.centre, centre)
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

    def wait_for_start(self, still: np.ndarray, text: str = START_PROMPT,
                       stop: Callable[[], bool] | None = None) -> None:
        """Show the still targets and a prompt until space (Escape raises Aborted), or until
        ``stop()`` is true (polled every frame; the caller checks why it returned)."""
        box = self._start_box
        prompt = self._visual.TextStim(self.win, text=text, pos=box.pos,
                                       height=START_PROMPT_HEIGHT, wrapWidth=box.width,
                                       anchorHoriz=box.anchor[0], anchorVert=box.anchor[1],
                                       color=to_psychopy_rgb(0.7))
        self._clear_keys()
        pressed: list[str] = []
        while "space" not in pressed:
            if stop is not None and stop():
                return
            self.draw(still, -1, card=0 if self.cards else -1)
            prompt.draw()
            self.win.flip()
            pressed = self._keys(keyList=["space", "escape"])
            if "escape" in pressed:
                raise Aborted

    def close(self) -> None:
        self.win.close()
