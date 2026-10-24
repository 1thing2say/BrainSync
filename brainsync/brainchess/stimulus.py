"""The BrainChess board stimulus: a chess board whose squares flicker, driven over stdin.

    python -m brainsync.brainchess.stimulus --plan      # geometry and frequencies, no window
    python -m brainsync.brainchess.stimulus             # full screen, commands on stdin
    python -m brainsync.brainchess.stimulus --flip      # Black at the bottom

The game process decides what flickers; this process only draws, frame-locked. It reads
one command per line on stdin (a daemon thread parses them into a BoardState, and every
frame draws the latest state, so a slow or chatty game never stalls the flicker):

    groups <64 chars>      per square a1..h8: '.' plain, '0' target 0, '1' target 1,
                           's' selected (static amber highlight)
    pieces <path>          RGBA PNG of the pieces layer, board sized (see render.py)
    arrow <from> <to>      arrow between two square indices; 'arrow none' clears it
    panels <l0>|<l1>       side panels flickering as targets 0 (left) and 1 (right) with
                           these labels; 'panels none' hides them
    text <message>         caption above the board; 'text' alone clears it
    hold                   every flicker stands still at its frame-0 luminance
    go                     flicker runs

Bad lines are ignored with a note on stderr; EOF ends the run (exit 0), Escape aborts it
(exit 1). The window starts in hold. Once it is up and its measured refresh passed the
check against [display] refresh_hz, "measured refresh: <x> Hz" goes to stderr (a refused
rate exits with code 2 first), which brainsync.launch.launch_stimulus waits for.

The flicker frame counter starts at the first ``go`` and then counts every displayed
frame, held or not, so the flicker phase stays locked to the display from then on.
Optional markers ("go", "hold", "groups:<64 chars>") go to stderr after the flip that
shows the change. Nothing here imports psychopy until a window is opened.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, NoReturn, TextIO

import numpy as np

from ..stimulus.__main__ import _check_refresh, _refresh_arg
from ..stimulus.config import StimulusConfig, config_warnings, load_config, override
from ..stimulus.display import (MARKER_CLOCKS, MarkerWriter, SessionTiming, layout_overflow,
                                median_rate)
from ..stimulus.flicker import (dropped_frames, frame_luminances, frames_per_cycle, phase_pi,
                                to_psychopy_rgb)
from .render import BOARD_SIZE, SQUARE_SIZE, square_center

DEFAULT_CONFIG = "chess"
N_SQUARES = 64
GROUP_CHARS = frozenset(".01s")
PLAIN_GROUPS = "." * N_SQUARES
LIGHT_LUM = 0.32      # plain light squares: low contrast, so the flicker stands out
DARK_LUM = 0.22       # plain dark squares (a1 is dark)
SELECTED_LUM = 0.6    # 's' squares, tinted amber by square_colors
AMBER_TINT = np.array([1.5, 1.0, 0.25])   # per-channel gain on SELECTED_LUM, clipped to 1
ARROW_RGB = (-0.4, 0.7, -0.2)             # PsychoPy rgb, a green that reads on grey
ARROW_OPACITY = 0.85
CAPTION_Y = (BOARD_SIZE / 2 + 0.5) / 2    # midway between the board's top and the screen's
CAPTION_HEIGHT = 0.032
LABEL_HEIGHT = 0.04
VERBS = ("groups", "pieces", "arrow", "panels", "text", "hold", "go")


# -- protocol -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """One parsed stdin line. ``value`` by verb: groups -> str (64 chars), pieces -> Path,
    arrow -> (from, to) or None, panels -> (label0, label1) or None, text -> str,
    hold/go -> None."""

    verb: str
    value: str | Path | tuple[int, int] | tuple[str, str] | None = None


def check_groups(groups: str) -> str:
    """``groups`` unchanged when it is 64 characters from '.01s'; ValueError otherwise."""
    if len(groups) != N_SQUARES:
        raise ValueError(f"groups needs {N_SQUARES} characters (one per square a1..h8), "
                         f"got {len(groups)}")
    bad = sorted(set(groups) - GROUP_CHARS)
    if bad:
        raise ValueError(f"groups may hold only '.', '0', '1' and 's', got {bad}")
    return groups


def _square_arg(text: str) -> int:
    try:
        sq = int(text)
    except ValueError:
        raise ValueError(f"square must be an integer 0..63, got {text!r}") from None
    if not 0 <= sq < N_SQUARES:
        raise ValueError(f"square {sq} out of range 0..63")
    return sq


def parse_command(line: str) -> Command:
    """The Command on one protocol line; ValueError for anything malformed."""
    text = line.strip()
    if not text:
        raise ValueError("blank line")
    verb, _, rest = text.partition(" ")
    rest = rest.strip()
    if verb == "groups":
        return Command("groups", check_groups(rest))
    if verb == "pieces":
        if not rest:
            raise ValueError("pieces needs a PNG path")
        return Command("pieces", Path(rest))
    if verb == "arrow":
        if rest == "none":
            return Command("arrow", None)
        parts = rest.split()
        if len(parts) != 2:
            raise ValueError(f"arrow needs two square indices or 'none', got {rest!r}")
        a, b = (_square_arg(p) for p in parts)
        if a == b:
            raise ValueError(f"arrow needs two different squares, got {a} twice")
        return Command("arrow", (a, b))
    if verb == "panels":
        if rest == "none":
            return Command("panels", None)
        labels = rest.split("|")
        if len(labels) != 2:
            raise ValueError(f"panels needs '<label0>|<label1>' or 'none', got {rest!r}")
        return Command("panels", (labels[0].strip(), labels[1].strip()))
    if verb == "text":
        return Command("text", rest)
    if verb in ("hold", "go"):
        if rest:
            raise ValueError(f"{verb} takes no arguments, got {rest!r}")
        return Command(verb)
    raise ValueError(f"unknown command {verb!r}; expected one of {', '.join(VERBS)}")


@dataclass(frozen=True)
class BoardSnapshot:
    """What one frame shows. ``pieces_version`` counts pieces commands, so a PNG rewritten
    under the same path is reloaded too."""

    groups: str = PLAIN_GROUPS
    pieces: Path | None = None
    pieces_version: int = 0
    arrow: tuple[int, int] | None = None
    panels: tuple[str, str] | None = None
    text: str = ""
    holding: bool = True


class BoardState:
    """The latest board state, written by the reader thread and read once per frame."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = BoardSnapshot()

    def apply(self, cmd: Command) -> None:
        with self._lock:
            s = self._snap
            if cmd.verb == "groups":
                s = replace(s, groups=cmd.value)
            elif cmd.verb == "pieces":
                s = replace(s, pieces=cmd.value, pieces_version=s.pieces_version + 1)
            elif cmd.verb == "arrow":
                s = replace(s, arrow=cmd.value)
            elif cmd.verb == "panels":
                s = replace(s, panels=cmd.value)
            elif cmd.verb == "text":
                s = replace(s, text=cmd.value)
            elif cmd.verb in ("hold", "go"):
                s = replace(s, holding=cmd.verb == "hold")
            else:
                raise ValueError(f"unknown command {cmd.verb!r}; expected one of "
                                 f"{', '.join(VERBS)}")
            self._snap = s

    def snapshot(self) -> BoardSnapshot:
        """An immutable copy of the current state."""
        with self._lock:
            return self._snap


def _stderr_note(msg: str) -> None:
    print(f"board: {msg}", file=sys.stderr, flush=True)


def command_stream(stream: TextIO | BinaryIO) -> TextIO:
    """Text lines from ``stream`` (stdin) with undecodable bytes replaced, not raised.

    A UnicodeDecodeError raised while decoding a buffered chunk escapes the per-line
    handling and would end the reader (and the run) as if at EOF; with replacement the
    bad line is just an unknown or malformed command, ignored like any other. A text
    stream is decoded from its byte ``buffer``; one without (already text) is used as is.
    """
    binary = getattr(stream, "buffer", None if isinstance(stream, io.TextIOBase) else stream)
    if binary is None:
        return stream
    return io.TextIOWrapper(binary, encoding="utf-8", errors="replace", newline=None)


class CommandReader:
    """Feeds protocol lines from ``stream`` into a BoardState; ``done`` once it hits EOF."""

    def __init__(self, stream: TextIO, state: BoardState,
                 note: Callable[[str], None] = _stderr_note) -> None:
        self.stream = stream
        self.state = state
        self._note = note
        self._eof = threading.Event()
        self._thread: threading.Thread | None = None
        self.n_lines = 0
        self.n_ignored = 0

    @property
    def done(self) -> bool:
        return self._eof.is_set()

    def run(self) -> None:
        """Read until EOF (blocking); called on the reader thread by start()."""
        try:
            for line in iter(self.stream.readline, ""):
                self.n_lines += 1
                try:
                    self.state.apply(parse_command(line))
                except ValueError as exc:
                    self.n_ignored += 1
                    self._note(f"ignored line {self.n_lines}: {exc}")
        except (OSError, ValueError) as exc:  # closed stream
            self._note(f"stopped reading: {exc}")
        finally:
            self._eof.set()

    def start(self) -> CommandReader:
        if self._thread is None:
            self._thread = threading.Thread(target=self.run, name="board-commands", daemon=True)
            self._thread.start()
        return self

    def wait(self, timeout: float | None = None) -> bool:
        """Block until EOF or timeout; True when EOF was reached."""
        return self._eof.wait(timeout)


# -- luminance ------------------------------------------------------------------------------

def square_luminances(groups: str, lum0: float, lum1: float, light: float = LIGHT_LUM,
                      dark: float = DARK_LUM) -> np.ndarray:
    """Luminance of every square, shape (64,): a static checker for '.', ``lum0`` / ``lum1``
    for the two flicker groups, SELECTED_LUM for 's' (square_colors tints it amber)."""
    check_groups(groups)
    sq = np.arange(N_SQUARES)
    out = np.where((sq % 8 + sq // 8) % 2 == 0, dark, light).astype(float)
    g = np.frombuffer(groups.encode("ascii"), dtype=np.uint8)
    out[g == ord("0")] = lum0
    out[g == ord("1")] = lum1
    out[g == ord("s")] = SELECTED_LUM
    return out


def square_colors(groups: str, lums: np.ndarray) -> np.ndarray:
    """PsychoPy rgb (-1..1) per square, shape (64, 3): grey from luminance, 's' amber."""
    lums = np.asarray(lums, dtype=float)
    if lums.shape != (N_SQUARES,):
        raise ValueError(f"need {N_SQUARES} luminances, got shape {lums.shape}")
    rgb01 = np.repeat(lums[:, None], 3, axis=1)
    selected = np.frombuffer(check_groups(groups).encode("ascii"), dtype=np.uint8) == ord("s")
    rgb01[selected] = np.clip(lums[selected, None] * AMBER_TINT, 0.0, 1.0)
    return 2.0 * rgb01 - 1.0


def frame_values(snap: BoardSnapshot, frame_index: int,
                 cfg: StimulusConfig) -> tuple[np.ndarray, np.ndarray]:
    """(square luminances (64,), panel luminances (2,)) for one frame.

    While holding every flicker shows its frame-0 luminance; the panel values matter only
    while panels are shown.
    """
    if cfg.n_targets != 2:
        raise ValueError(f"the board stimulus needs a two-target config, got "
                         f"{cfg.n_targets} target(s) in {cfg.name!r}")
    i = 0 if snap.holding else frame_index
    lum = frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform)
    return square_luminances(snap.groups, lum[0], lum[1]), lum[:2].copy()


def arrow_vertices(frm: int, to: int, flip: bool = False, shaft: float = 0.022,
                   head_length: float = 0.05, head_width: float = 0.06) -> np.ndarray:
    """Polygon (7, 2) of an arrow from the centre of ``frm`` to the centre of ``to``."""
    if frm == to:
        raise ValueError(f"arrow needs two different squares, got {frm} twice")
    a = np.array(square_center(frm, flip))
    b = np.array(square_center(to, flip))
    length = float(np.hypot(*(b - a)))
    u = (b - a) / length
    n = np.array([-u[1], u[0]])
    neck = a + u * max(length - head_length, 0.0)
    return np.array([a + n * shaft / 2, neck + n * shaft / 2, neck + n * head_width / 2, b,
                     neck - n * head_width / 2, neck - n * shaft / 2, a - n * shaft / 2])


def check_layout(cfg: StimulusConfig) -> None:
    """ValueError unless the config has two panels that sit fully outside the board."""
    if cfg.n_targets != 2:
        raise ValueError(f"the board stimulus needs a two-target config, got "
                         f"{cfg.n_targets} target(s) in {cfg.name!r}")
    inner = min(abs(t.pos[0]) for t in cfg.targets) - cfg.size / 2
    if inner <= BOARD_SIZE / 2:
        raise ValueError(f"config {cfg.name!r} puts the panels' inner edges at x = "
                         f"+/-{inner:.3f}, inside the board (+/-{BOARD_SIZE / 2:g}); "
                         "increase [layout] spacing")


# -- plan -----------------------------------------------------------------------------------

def plan_lines(cfg: StimulusConfig, flip: bool) -> list[str]:
    hz = cfg.refresh
    widest = max(abs(t.pos[0]) for t in cfg.targets) + cfg.reach
    inner = min(abs(t.pos[0]) for t in cfg.targets) - cfg.size / 2
    lines = [
        f"config {cfg.name}" + (f" ({cfg.path})" if cfg.path else ""),
        f"  board {BOARD_SIZE:g} screen heights, square {SQUARE_SIZE:g}, "
        + ("black at the bottom (h8 bottom left)" if flip
           else "white at the bottom (a1 bottom left)"),
        f"  {cfg.waveform} flicker, refresh {hz:g} Hz; squares and panels share the "
        "target frequencies",
    ]
    for t in cfg.targets:
        x, y = t.pos
        lines.append(f"  target {t.index}  panel {t.label!r}  {t.freq:.3f} Hz  phase "
                     f"{phase_pi(t.phase):.2f} pi  {frames_per_cycle(t.freq, hz):.3f} "
                     f"frames/cycle  panel centre ({x:+.3f}, {y:+.3f})  side {cfg.size:g}")
    lines.append(f"  panels clear the board edge by {inner - BOARD_SIZE / 2:.3f} and reach "
                 f"x = +/-{widest:.3f}: needs a screen at least {2 * widest:.3f}:1 "
                 f"(config aspect {cfg.display.aspect:g})")
    return lines


# -- PsychoPy -------------------------------------------------------------------------------

class BoardRenderer:
    """Fullscreen PsychoPy window: 64 squares in one ElementArrayStim, the pieces PNG, an
    arrow, two side panels with labels and a caption."""

    def __init__(self, cfg: StimulusConfig, flip: bool = False,
                 note: Callable[[str], None] = _stderr_note) -> None:
        from psychopy import event, visual  # lazy: never imported by tests

        self.cfg = cfg
        self.flip_board = flip
        self._visual = visual
        self._keys = event.getKeys
        self._note = note
        # The window's own timing check is skipped: measure_refresh() does that job.
        self.win = visual.Window(fullscr=True, screen=cfg.display.screen, units="height",
                                 color=to_psychopy_rgb(cfg.display.background),
                                 checkTiming=False)
        self.win.setMouseVisible(False)
        xys = np.array([square_center(sq, flip) for sq in range(N_SQUARES)])
        self.squares = visual.ElementArrayStim(
            self.win, units="height", nElements=N_SQUARES, sizes=SQUARE_SIZE, xys=xys,
            colors=square_colors(PLAIN_GROUPS, square_luminances(PLAIN_GROUPS, 0.5, 0.5)),
            colorSpace="rgb", elementTex=None, elementMask=None, interpolate=False)
        self.pieces = None
        self._pieces_version = 0
        self._pieces_tries = 0
        self.arrow = visual.ShapeStim(self.win, units="height", vertices=arrow_vertices(12, 28),
                                      fillColor=ARROW_RGB, lineColor=None,
                                      opacity=ARROW_OPACITY, closeShape=True)
        self._arrow: tuple[int, int] | None = None
        side = cfg.size
        self.panels = []
        self.labels = []
        for t in cfg.targets:
            x, y = t.pos
            self.panels.append(visual.Rect(self.win, units="height", size=(side, side),
                                           pos=t.pos, lineColor=None,
                                           fillColor=to_psychopy_rgb(0.0)))
            # wrapped to the panel width and hung from its top line, so a long label
            # ('Rook / Bishop / Knight') stacks under its panel instead of running onto
            # the board or off the screen edge
            self.labels.append(visual.TextStim(self.win, units="height", text=t.label,
                                               pos=(x, y - side / 2 - LABEL_HEIGHT / 2),
                                               height=LABEL_HEIGHT, wrapWidth=side,
                                               anchorVert="top",
                                               color=to_psychopy_rgb(0.9)))
        self._labels = tuple(cfg.labels)
        self.caption = visual.TextStim(self.win, units="height", text="", pos=(0.0, CAPTION_Y),
                                       height=CAPTION_HEIGHT, wrapWidth=cfg.display.aspect,
                                       color=to_psychopy_rgb(0.9))
        self._caption = ""
        problem = layout_overflow(cfg, *self.win.size)
        if problem:
            self._note(f"warning: {problem}")

    def measure_refresh(self, n: int = 120, warmup: int = 20) -> float:
        """Median-interval rate over ``n`` blank flips, after ``warmup`` unmeasured ones."""
        stamps = [self.win.flip() for _ in range(warmup + n)]
        return median_rate(stamps[warmup:])

    def _update_pieces(self, snap: BoardSnapshot) -> None:
        """Load a new pieces PNG once per pieces command; keep the old one if it fails."""
        if snap.pieces is None or snap.pieces_version == self._pieces_version:
            return
        try:
            if self.pieces is None:
                # interpolate: the PNG is scaled to the board's physical pixels (about
                # 2x on Retina); nearest-neighbour would leave stair-stepped glyph edges
                self.pieces = self._visual.ImageStim(self.win, image=str(snap.pieces),
                                                     units="height", pos=(0.0, 0.0),
                                                     size=(BOARD_SIZE, BOARD_SIZE),
                                                     interpolate=True)
            else:
                self.pieces.image = str(snap.pieces)
        except Exception as exc:  # psychopy/PIL raise many types for a bad image
            self._pieces_tries += 1
            # a file still being written may load on the next frame; give up after a few
            if self._pieces_tries < 10:
                return
            self._note(f"could not load pieces image {snap.pieces}: {exc}")
        self._pieces_version = snap.pieces_version
        self._pieces_tries = 0

    def draw(self, snap: BoardSnapshot, square_lums: np.ndarray,
             panel_lums: np.ndarray) -> None:
        """Queue one frame; never blocks (flip() waits for vsync)."""
        self.squares.colors = square_colors(snap.groups, square_lums)
        self.squares.draw()
        self._update_pieces(snap)
        if self.pieces is not None:
            self.pieces.draw()
        if snap.arrow is not None:
            if snap.arrow != self._arrow:
                self.arrow.vertices = arrow_vertices(*snap.arrow, flip=self.flip_board)
                self._arrow = snap.arrow
            self.arrow.draw()
        if snap.panels is not None:
            if snap.panels != self._labels:
                for label, text in zip(self.labels, snap.panels):
                    label.text = text
                self._labels = snap.panels
            for k, panel in enumerate(self.panels):
                panel.fillColor = to_psychopy_rgb(panel_lums[k])
                panel.draw()
                self.labels[k].draw()
        if snap.text:
            if snap.text != self._caption:
                self.caption.text = snap.text
                self._caption = snap.text
            self.caption.draw()

    def flip(self) -> float:
        return float(self.win.flip())

    def escape_pressed(self) -> bool:
        return "escape" in self._keys(keyList=["escape"])

    def close(self) -> None:
        self.win.close()


# -- CLI ------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.brainchess.stimulus",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                   help=f"shipped config name or path to a .toml file (default {DEFAULT_CONFIG})")
    p.add_argument("--flip", action="store_true", help="draw Black at the bottom")
    p.add_argument("--plan", action="store_true",
                   help="dry run: print the geometry and frequencies, open nothing")
    p.add_argument("--markers", choices=sorted(MARKER_CLOCKS), default="none",
                   help="clock stamped on stderr markers for go/hold/groups changes, or "
                        "'none' to print none (default none)")
    p.add_argument("--refresh", type=_refresh_arg,
                   help="display rate in Hz, or 'auto' to adopt the measured rate "
                        "(default: [display] refresh_hz)")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="warn instead of exiting when the measured rate disagrees")
    return p


def _events(prev: BoardSnapshot | None, snap: BoardSnapshot) -> list[str]:
    """Marker events for what changed between two consecutive frames."""
    events = []
    if prev is None or snap.holding != prev.holding:
        events.append("hold" if snap.holding else "go")
    if prev is None or snap.groups != prev.groups:
        events.append(f"groups:{snap.groups}")
    return events


def _run_display(cfg: StimulusConfig, args: argparse.Namespace) -> int:
    renderer = BoardRenderer(cfg, args.flip)
    try:
        measured = renderer.measure_refresh()
        checked = _check_refresh(cfg, args, measured)
        if checked is None:
            return 2
        cfg = checked
        # launchers wait for this line, so it comes only after the refresh check passed
        print(f"measured refresh: {measured:.2f} Hz", file=sys.stderr, flush=True)
        markers = MarkerWriter(args.markers)
        state = BoardState()
        reader = CommandReader(command_stream(sys.stdin), state).start()
        timing = SessionTiming(refresh=cfg.refresh, measured_refresh=measured)
        frame = 0              # flicker frames since the first go
        started = False
        prev: BoardSnapshot | None = None
        last: float | None = None
        while not reader.done:
            if renderer.escape_pressed():
                timing.aborted = True
                break
            snap = state.snapshot()
            started = started or not snap.holding
            renderer.draw(snap, *frame_values(snap, frame, cfg))
            t = renderer.flip()
            markers.emit(_events(prev, snap))
            timing.n_frames += 1
            if last is not None:
                timing.dropped += dropped_frames((last, t), cfg.refresh)
            last, prev = t, snap
            frame += int(started)
        print(timing.summary(), flush=True)
        if reader.n_ignored:
            print(f"ignored {reader.n_ignored} of {reader.n_lines} command line(s)",
                  file=sys.stderr)
        if timing.aborted:
            print("aborted", file=sys.stderr)
            return 1
        return 0
    finally:
        renderer.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        if isinstance(args.refresh, float):
            cfg = override(cfg, refresh_hz=args.refresh)
        check_layout(cfg)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for w in config_warnings(cfg):
        print(f"warning: {w}", file=sys.stderr)
    if args.plan:
        print("\n".join(plan_lines(cfg, args.flip)))
        return 0
    try:
        return _run_display(cfg, args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def hard_exit(code: int) -> NoReturn:
    """Flush and leave at once, skipping interpreter shutdown.

    The command reader is a daemon thread blocked in stdin's readline, holding the
    buffered reader's lock. When the run ends while stdin is still open (Escape, a
    refused refresh, a crash) rather than at EOF, interpreter shutdown tries to take
    that lock and aborts with "Fatal Python error: _enter_buffered_busy" (SIGABRT, exit
    code -6), which the game reads as a stimulus crash.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    hard_exit(main())
