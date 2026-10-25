"""The BrainChess board stimulus: a chess board whose squares flicker, driven over stdin.

    python -m brainsync.brainchess.stimulus --plan      # geometry and frequencies, no window
    python -m brainsync.brainchess.stimulus             # full screen, commands on stdin
    python -m brainsync.brainchess.stimulus --flip      # Black at the bottom
    python -m brainsync.brainchess.stimulus -c chess_cvep --markers unix   # c-VEP (default
                                                        # of python -m brainsync.brainchess)

The game process decides what flickers; this process only draws, frame-locked. It reads
one command per line on stdin (a daemon thread parses them into a BoardState, and every
frame draws the latest state, so a slow or chatty game never stalls the flicker):

    groups <64 chars>      per square a1..h8: '.' plain, '0' target 0, '1' target 1,
                           's' selected (static amber highlight)
    pieces <path>          RGBA PNG of the pieces layer, board sized (see render.py)
    arrow <from> <to>      arrow between two square indices; 'arrow none' clears it
    panels <l0>|<l1>       PLAY / BACK panels flickering as targets 0 (left) and 1 (right)
                           under these labels; 'panels none' leaves them idle. A c-VEP
                           config has three: '<l0>|<l1>|<l2>', an empty label leaves that
                           panel idle (the board's groups are targets 0 and 1, BACK is 2)
    text <message>         the prompt in the side column; 'text' alone clears it
    stage <name>           the step lit in the stage row: piece, dest, promo, confirm or none
    votes <t> <n> <of>     vote meter: n of ``of`` fresh windows agree on target t (0, 1,
                           2 or '-' for none yet)
    confidence <a>|<b>|..  c-VEP: the confidence block in the side column, one line each
                           (a bar per target and the lead / choice line, or the training
                           progress); 'confidence' alone clears it
    cue <k>                c-VEP: the cue box around panel k (training); 'cue none' clears
    lanes <a,b,...>        names of the EEG lanes in the trace strip (1..8; resets it)
    eeg <t> <dt> <v...>    trace samples dt s apart, the last at stream time t, row-major
                           with one value per lane in each row
    window <t> <r> <f> <p> one decoder window ending at stream time t: named target r (0, 1,
                           2 or '-'), f 1 when the gate counted it (0: too early or board
                           still), p 1 when it fired a pick
    hold                   every flicker stands still at its frame-0 luminance
    go                     flicker runs

The standard display lines (docs/CVEP-STANDARD.md 4.1) work too: a target index moves the
cue box (-1 clears it), {"flicker": true} / {"flicker": false} are go / hold, {"text": ...}
sets the prompt and {"labels": [...]} the confidence block.

Bad lines are ignored with a note on stderr; EOF ends the run (exit 0), Escape aborts it
(exit 1). The window starts in hold. Once it is up and its measured refresh passed the
check against [display] refresh_hz, "measured refresh: <x> Hz" goes to stderr (a refused
rate exits with code 2 first), which brainsync.launch.launch_stimulus waits for.

The flicker frame counter starts at the first ``go`` and then counts every displayed
frame, held or not, so the flicker phase stays locked to the display from then on.
Optional markers ("go", "hold", "groups:<64 chars>") go to stderr after the flip that
shows the change. Nothing here imports psychopy until a window is opened.

A c-VEP config ([code], three targets: chess_cvep) follows the BrainSync standard
(docs/CVEP-STANDARD.md): frame n of target k is white when bit (n - 21 k) mod 63 of the
m-sequence is 1 and black when 0 (brainsync.vep.live.frame_bit), a still target is white,
and n counts the frames since the current layout's first flickering frame: it restarts at
every ``go`` and at every change of the groups or panels, so each screen starts on a cycle.
The first flip of every cycle prints the marker "cycle:<n>" (with --markers unix, which
python -m brainsync.brainchess passes), the board's tags read A / B, the vote meters
give way to the confidence block, and a third panel, BACK, sits beside the other two.

The look follows the browser mockup (see style.py): the board on the left, a column on
the right with the title, stage row, prompt, vote meters, the PLAY / BACK panels and the
trace strip of EEG lanes and decoder windows. Flickering squares stay grey (full
luminance swing) and carry a "15" / "17" tag. Everything that never changes is drawn
once into one cached image, and text is only re-laid out when the game changes it (at a
screen change, when the gate ignores windows anyway), so the frame loop stays cheap.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, TextIO

import numpy as np

from . import style
from ..stimulus.__main__ import _check_refresh, _refresh_arg
from ..stimulus.config import StimulusConfig, config_warnings, load_config, override
from ..stimulus.display import MARKER_CLOCKS, MarkerWriter, SessionTiming, median_rate
from ..stimulus.flicker import (dropped_frames, frame_luminances, frames_per_cycle, phase_pi,
                                to_psychopy_rgb)
from ..vep.live import BITS, frame_bit
from .render import BOARD_SIZE, SQUARE_SIZE, grid_cell, square_center
from .trace import MAX_LANES, TraceBuffer, TraceView, Window, lane_points, window_marks

DEFAULT_CONFIG = "chess"
N_SQUARES = 64
GROUP_CHARS = frozenset(".01s")
PLAIN_GROUPS = "." * N_SQUARES
LIGHT_LUM = 0.32      # luminance array value of plain light squares (drawn in SQ_LIGHT)
DARK_LUM = 0.22       # and of plain dark ones (a1 is dark; drawn in SQ_DARK)
SELECTED_LUM = 0.6    # 's' squares (drawn in SELECTED)
ARROW_OPACITY = 0.8
STAGE_NAMES = ("piece", "dest", "promo", "confirm", "none")
ABSTAIN = -1
NO_VOTES = (ABSTAIN, 0, 4)
TRACE_EVERY = 3       # frames between trace geometry updates (20 Hz at 60 Hz)
MAX_WINDOWS = 40      # decoder window ticks the strip can show (6 s at 0.25 s is 24)
MAX_MARKS = 8         # pick markers
FPS_EVERY_S = 0.5     # how often the frame-rate readout is re-laid out (text costs ~1-4 ms)
FPS_SPAN_S = 1.0      # the readout averages the frames shown over this long
VERBS = ("groups", "pieces", "arrow", "panels", "text", "stage", "votes", "lanes", "eeg",
         "window", "confidence", "cue", "hold", "go")
MAX_TARGETS = 3       # c-VEP: group A, group B and the Back panel
MAX_CONFIDENCE = style.CONFIDENCE_LINES   # lines in the confidence block
CODE_TAGS = ("A", "B")   # c-VEP tags on the group squares (f-VEP: the frequencies)


# -- protocol -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """One parsed stdin line. ``value`` by verb: groups -> str (64 chars), pieces -> Path,
    arrow -> (from, to) or None, panels -> (label0, label1[, label2]) or None,
    text/stage -> str, votes -> (target, n, of), lanes -> tuple of names,
    eeg -> (t, dt, values), window -> trace.Window, confidence -> tuple of lines,
    cue -> target or None, hold/go -> None."""

    verb: str
    value: Any = None


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


def _target_arg(text: str) -> int:
    if text not in ("0", "1", "2", "-"):
        raise ValueError(f"target must be 0, 1, 2 or '-', got {text!r}")
    return ABSTAIN if text == "-" else int(text)


def _flag_arg(text: str) -> bool:
    if text not in ("0", "1"):
        raise ValueError(f"flag must be 0 or 1, got {text!r}")
    return text == "1"


def _number(text: str, what: str) -> float:
    try:
        x = float(text)
    except ValueError:
        raise ValueError(f"{what} must be a number, got {text!r}") from None
    if not math.isfinite(x):
        raise ValueError(f"{what} must be finite, got {text!r}")
    return x


def _count(text: str, what: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise ValueError(f"{what} must be an integer, got {text!r}") from None
    if n < 0:
        raise ValueError(f"{what} must be >= 0, got {n}")
    return n


def _standard_line(text: str) -> Command:
    """A line of the standard display protocol (docs/CVEP-STANDARD.md 4.1,
    brainsync.stimulus.feedback) as a board command: a target index moves the cue box
    (-1 clears it), {"flicker": true / false} is go / hold, {"text": ...} the prompt and
    {"labels": [...]} the confidence block. {"active": [...]} has no board equivalent
    (the groups and panels decide what flickers) and is refused like any bad line."""
    if text.lstrip("-").isdigit():
        k = int(text)
        if k == ABSTAIN:
            return Command("cue", None)
        if 0 <= k < MAX_TARGETS:
            return Command("cue", k)
        raise ValueError(f"target index must be -1..{MAX_TARGETS - 1}, got {k}")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"bad JSON line: {exc}") from None
    if not isinstance(obj, dict) or len(obj) != 1:
        raise ValueError(f"a JSON line needs exactly one key, got {text!r}")
    (key, value), = obj.items()
    if key == "flicker" and isinstance(value, bool):
        return Command("go" if value else "hold")
    if key == "text" and isinstance(value, str):
        return Command("text", " ".join(value.split()))
    if key == "labels" and isinstance(value, list) and all(isinstance(v, str) for v in value):
        if len(value) > MAX_CONFIDENCE:
            raise ValueError(f"labels takes at most {MAX_CONFIDENCE} lines, got {len(value)}")
        return Command("confidence", tuple(" ".join(v.splitlines()).strip() for v in value))
    raise ValueError(f"unsupported JSON line {text!r}; the board takes flicker, text and "
                     "labels")


def parse_command(line: str) -> Command:
    """The Command on one protocol line; ValueError for anything malformed."""
    text = line.strip()
    if not text:
        raise ValueError("blank line")
    if text[0] in "{-0123456789":
        return _standard_line(text)
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
        if len(labels) not in (2, MAX_TARGETS):
            raise ValueError(f"panels needs '<label0>|<label1>', '<label0>|<label1>|<label2>' "
                             f"or 'none', got {rest!r}")
        return Command("panels", tuple(lab.strip() for lab in labels))
    if verb == "text":
        return Command("text", rest)
    if verb == "stage":
        if rest not in STAGE_NAMES:
            raise ValueError(f"stage must be one of {', '.join(STAGE_NAMES)}, got {rest!r}")
        return Command("stage", rest)
    if verb == "votes":
        parts = rest.split()
        if len(parts) != 3:
            raise ValueError(f"votes needs '<target> <n> <of>', got {rest!r}")
        target, n, of = _target_arg(parts[0]), _count(parts[1], "n"), _count(parts[2], "of")
        if not 1 <= of <= 16 or n > of:
            raise ValueError(f"votes needs 0 <= n <= of and 1 <= of <= 16, got {n} of {of}")
        return Command("votes", (target, n, of))
    if verb == "lanes":
        names = tuple(n.strip() for n in rest.split(","))
        if not rest or not all(names) or len(names) > MAX_LANES:
            raise ValueError(f"lanes needs 1..{MAX_LANES} comma-separated names, got {rest!r}")
        return Command("lanes", names)
    if verb == "eeg":
        parts = rest.split()
        if len(parts) < 3:
            raise ValueError("eeg needs '<t> <dt> <values...>'")
        t, dt = _number(parts[0], "t"), _number(parts[1], "dt")
        if dt <= 0:
            raise ValueError(f"dt must be > 0, got {dt}")
        values = np.array([_number(p, "value") for p in parts[2:]])
        return Command("eeg", (t, dt, values))
    if verb == "window":
        parts = rest.split()
        if len(parts) != 4:
            raise ValueError(f"window needs '<t> <target> <fresh> <fired>', got {rest!r}")
        return Command("window", Window(_number(parts[0], "t"), _target_arg(parts[1]),
                                        _flag_arg(parts[2]), _flag_arg(parts[3])))
    if verb == "confidence":
        lines = tuple(ln.strip() for ln in rest.split("|")) if rest else ()
        if len(lines) > MAX_CONFIDENCE:
            raise ValueError(f"confidence takes at most {MAX_CONFIDENCE} lines, got "
                             f"{len(lines)}")
        return Command("confidence", lines)
    if verb == "cue":
        if rest == "none":
            return Command("cue", None)
        if rest not in ("0", "1", "2"):
            raise ValueError(f"cue needs a panel 0, 1 or 2, or 'none', got {rest!r}")
        return Command("cue", int(rest))
    if verb in ("hold", "go"):
        if rest:
            raise ValueError(f"{verb} takes no arguments, got {rest!r}")
        return Command(verb)
    raise ValueError(f"unknown command {verb!r}; expected one of {', '.join(VERBS)}")


@dataclass(frozen=True)
class BoardSnapshot:
    """What one frame shows (the trace strip's data lives in BoardState.trace).
    ``pieces_version`` counts pieces commands, so a PNG rewritten under the same path is
    reloaded too."""

    groups: str = PLAIN_GROUPS
    pieces: Path | None = None
    pieces_version: int = 0
    arrow: tuple[int, int] | None = None
    panels: tuple[str, ...] | None = None
    text: str = ""
    stage: str = "none"
    votes: tuple[int, int, int] = NO_VOTES
    lanes: tuple[str, ...] = ()
    holding: bool = True
    confidence: tuple[str, ...] = ()
    cue: int | None = None

    @property
    def layout(self) -> tuple:
        """What a c-VEP code restarts on: the flickering squares and panels, and go."""
        return (self.groups, self.panels, self.holding)


class BoardState:
    """The latest board state, written by the reader thread and read once per frame.

    Trace samples and decoder windows go to ``trace`` (its own lock) instead of the
    snapshot, so taking a snapshot never copies them.
    """

    def __init__(self, trace: TraceBuffer | None = None) -> None:
        self._lock = threading.Lock()
        self._snap = BoardSnapshot()
        self.trace = trace if trace is not None else TraceBuffer(span_s=style.TRACE_SPAN_S)

    def apply(self, cmd: Command) -> None:
        if cmd.verb == "eeg":
            self.trace.add_samples(*cmd.value)
            return
        if cmd.verb == "window":
            self.trace.add_window(cmd.value)
            return
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
            elif cmd.verb == "stage":
                s = replace(s, stage=cmd.value)
            elif cmd.verb == "votes":
                s = replace(s, votes=cmd.value)
            elif cmd.verb == "confidence":
                s = replace(s, confidence=cmd.value)
            elif cmd.verb == "cue":
                s = replace(s, cue=cmd.value)
            elif cmd.verb == "lanes":
                self.trace.set_lanes(len(cmd.value))
                s = replace(s, lanes=cmd.value)
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


# -- luminance and colour -------------------------------------------------------------------

def _dark_squares() -> np.ndarray:
    sq = np.arange(N_SQUARES)
    return (sq % 8 + sq // 8) % 2 == 0


def square_luminances(groups: str, lum0: float, lum1: float, light: float = LIGHT_LUM,
                      dark: float = DARK_LUM) -> np.ndarray:
    """Luminance of every square, shape (64,): a static checker for '.', ``lum0`` / ``lum1``
    for the two flicker groups, SELECTED_LUM for 's'. Only the flicker groups' values reach
    the screen; square_colors paints the others in the palette."""
    check_groups(groups)
    out = np.where(_dark_squares(), dark, light).astype(float)
    g = np.frombuffer(groups.encode("ascii"), dtype=np.uint8)
    out[g == ord("0")] = lum0
    out[g == ord("1")] = lum1
    out[g == ord("s")] = SELECTED_LUM
    return out


def square_colors(groups: str, lums: np.ndarray) -> np.ndarray:
    """PsychoPy rgb (-1..1) per square, shape (64, 3): flicker groups grey from their
    luminance (the full swing, untinted), plain squares the board's light and dark, 's'
    the selected amber."""
    lums = np.asarray(lums, dtype=float)
    if lums.shape != (N_SQUARES,):
        raise ValueError(f"need {N_SQUARES} luminances, got shape {lums.shape}")
    g = np.frombuffer(check_groups(groups).encode("ascii"), dtype=np.uint8)
    out = np.where(_dark_squares()[:, None], style.SQ_DARK, style.SQ_LIGHT).astype(float)
    flicker = (g == ord("0")) | (g == ord("1"))
    out[flicker] = 2.0 * lums[flicker, None] - 1.0
    out[g == ord("s")] = style.SELECTED
    return out


def _check_targets(cfg: StimulusConfig) -> None:
    if cfg.paradigm == "code":
        if cfg.n_targets != MAX_TARGETS:
            raise ValueError(f"a c-VEP board stimulus needs a three-target config, got "
                             f"{cfg.n_targets} target(s) in {cfg.name!r}")
        c = cfg.code
        if c.length != len(BITS) or c.frames_per_bit != 1:
            raise ValueError(f"a c-VEP board stimulus shows the standard {len(BITS)}-bit "
                             f"m-sequence, one bit per frame; {cfg.name!r} has length "
                             f"{c.length}, {c.frames_per_bit} frame(s) per bit")
    elif cfg.paradigm != "freq" or cfg.n_targets != 2:
        raise ValueError(f"the board stimulus needs a two-target frequency config or a "
                         f"three-target [code] config, got {cfg.n_targets} {cfg.paradigm} "
                         f"target(s) in {cfg.name!r}")


def code_luminances(frame_index: int, cfg: StimulusConfig, holding: bool = False) -> np.ndarray:
    """c-VEP: every target's luminance on flicker frame ``frame_index`` (frames since the
    layout's first flickering frame): 1.0 (white) for a 1 bit, 0.0 (black) for a 0, with
    target k's code ``k * lag_frames`` frames late. Held, every target is white (still)."""
    if cfg.code is None:
        raise ValueError(f"config {cfg.name!r} has no [code] table")
    if holding:
        return np.ones(cfg.n_targets)
    return np.array([float(frame_bit(k, int(frame_index), cfg.code.lag_frames))
                     for k in range(cfg.n_targets)])


def frame_values(snap: BoardSnapshot, frame_index: int,
                 cfg: StimulusConfig) -> tuple[np.ndarray, np.ndarray]:
    """(square luminances (64,), panel luminances (n targets,)) for one frame.

    While holding every flicker shows its frame-0 luminance (f-VEP) or white (c-VEP); the
    panel values matter only while panels are shown. The board's groups '0' and '1' are
    targets 0 and 1; a c-VEP config's third target is the Back panel.
    """
    _check_targets(cfg)
    if cfg.paradigm == "code":
        lum = code_luminances(frame_index, cfg, snap.holding)
    else:
        i = 0 if snap.holding else frame_index
        lum = frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform, cfg.depth)
    return square_luminances(snap.groups, lum[0], lum[1]), lum.copy()


class CodeClock:
    """c-VEP frame counting for the board: frames since the current layout's first
    flickering frame, restarted whenever the layout (groups, panels, go) changes, and the
    "cycle:<n>" marker on each cycle's first frame (none while holding)."""

    def __init__(self, cycle_frames: int) -> None:
        if int(cycle_frames) != cycle_frames or cycle_frames < 1:
            raise ValueError(f"cycle_frames must be a positive integer, got {cycle_frames!r}")
        self.cycle_frames = int(cycle_frames)
        self.frame = 0
        self._layout: tuple | None = None

    def step(self, snap: BoardSnapshot) -> tuple[int, list[str]]:
        """(this frame's flicker frame index, its marker events); call once per frame,
        before drawing it, then the next frame counts on from there."""
        if snap.layout != self._layout:
            self._layout = snap.layout
            self.frame = 0
        if snap.holding:
            return 0, []
        i = self.frame
        self.frame += 1
        events = [f"cycle:{i // self.cycle_frames}"] if i % self.cycle_frames == 0 else []
        return i, events


def arrow_vertices(frm: int, to: int, flip: bool = False, shaft: float = 0.022,
                   head_length: float = 0.05, head_width: float = 0.06) -> np.ndarray:
    """Polygon (7, 2) of an arrow from the centre of ``frm`` to the centre of ``to``,
    relative to the board's centre."""
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


def tag_position(sq: int, flip: bool = False) -> tuple[float, float]:
    """Screen centre of the "15" / "17" tag in square ``sq``'s top-right corner."""
    cx, cy = square_center(sq, flip)
    w, h = style.TAG_SIZE
    half = SQUARE_SIZE / 2 - style.TAG_INSET
    return (style.BOARD_X + cx + half - w / 2, style.BOARD_Y + cy + half - h / 2)


def check_layout(cfg: StimulusConfig) -> None:
    """ValueError unless the config has two panels (three for c-VEP) in the side column,
    clear of the board, the vote meters (c-VEP: the confidence block) above and the trace
    strip below, on a screen the layout fits."""
    _check_targets(cfg)
    board_right = style.BOARD_X + BOARD_SIZE / 2
    inner = min(t.pos[0] for t in cfg.targets) - cfg.size / 2
    if inner <= board_right:
        raise ValueError(f"config {cfg.name!r} puts a panel's left edge at x = {inner:+.3f}, "
                         f"inside the board (right edge {board_right:+.3f}); move the panels "
                         "right with [layout] x_offset")
    outer = max(t.pos[0] for t in cfg.targets) + cfg.reach
    if outer > style.COLUMN_RIGHT + 1e-9:
        raise ValueError(f"config {cfg.name!r} puts the panels out to x = {outer:+.3f}, past "
                         f"the side column's edge {style.COLUMN_RIGHT:+.3f}")
    top = max(t.pos[1] for t in cfg.targets) + cfg.size / 2
    bottom = min(t.pos[1] for t in cfg.targets) - cfg.size / 2
    # room for the active panels' labels above them, under the meters or confidence block
    ceiling = (style.CONFIDENCE_BOTTOM if cfg.paradigm == "code" else style.VOTES_Y) - 0.06
    floor = style.TRACE_TOP + 0.03       # room for the Hz pills below them
    if top > ceiling or bottom < floor:
        raise ValueError(f"config {cfg.name!r} puts the panels between y = {bottom:+.3f} and "
                         f"{top:+.3f}; they must stay within {floor:+.3f}..{ceiling:+.3f}, "
                         "between the vote meters (or confidence block) and the trace strip")
    xs = sorted(t.pos[0] for t in cfg.targets)
    if any(b - a < 2 * cfg.reach - 1e-9 for a, b in zip(xs, xs[1:])):
        raise ValueError(f"config {cfg.name!r} puts the panels closer than their cue boxes "
                         f"({2 * cfg.reach:.3f} apart); widen [layout] spacing")
    if cfg.display.aspect < style.LAYOUT_ASPECT - 1e-9:
        raise ValueError(f"config {cfg.name!r} allows a {cfg.display.aspect:g}:1 screen; the "
                         f"board and side column need {style.LAYOUT_ASPECT:.3f}:1")


def layout_warning(width_px: float, height_px: float) -> str | None:
    """Warning text when a window is too narrow for the board and side column."""
    aspect = width_px / height_px
    if aspect >= style.LAYOUT_ASPECT - 1e-9:
        return None
    return (f"this {aspect:.3f}:1 window is narrower than the {style.LAYOUT_ASPECT:.3f}:1 "
            "the board and side column need; their edges will be cut off")


# -- plan -----------------------------------------------------------------------------------

def plan_lines(cfg: StimulusConfig, flip: bool) -> list[str]:
    hz = cfg.refresh
    board_right = style.BOARD_X + BOARD_SIZE / 2
    inner = min(t.pos[0] for t in cfg.targets) - cfg.size / 2
    lines = [
        f"config {cfg.name}" + (f" ({cfg.path})" if cfg.path else ""),
        f"  board {BOARD_SIZE:g} screen heights, square {SQUARE_SIZE:g}, centre "
        f"({style.BOARD_X:+.3f}, {style.BOARD_Y:+.3f}), "
        + ("black at the bottom (h8 bottom left)" if flip
           else "white at the bottom (a1 bottom left)"),
    ]
    if cfg.paradigm == "code":
        c = cfg.code
        lines.append(f"  c-VEP: one {c.length}-bit m-sequence, white / black, a cycle of "
                     f"{c.cycle_frames} frames ({c.cycle_frames / hz:.2f} s at {hz:g} Hz) "
                     "restarted with every screen; group A squares show target 0's code, "
                     "group B target 1's, the BACK panel target 2's")
    else:
        lines.append(f"  {cfg.waveform} flicker, depth {cfg.depth:g}, refresh {hz:g} Hz; squares "
                     "and panels share the target frequencies; flickering squares stay grey "
                     "with a 15 / 17 tag")
    for t in cfg.targets:
        x, y = t.pos
        if cfg.paradigm == "code":
            lag = t.index * cfg.code.lag_frames
            what = f"code lag {lag} frames ({1000 * lag / hz:.0f} ms)"
        else:
            what = (f"{t.freq:.3f} Hz  phase {phase_pi(t.phase):.2f} pi  "
                    f"{frames_per_cycle(t.freq, hz):.3f} frames/cycle")
        lines.append(f"  target {t.index}  panel {t.label!r}  {what}  panel centre "
                     f"({x:+.3f}, {y:+.3f})  side {cfg.size:g}")
    lines.append(f"  panels clear the board edge by {inner - board_right:.3f}; side column "
                 f"x = {style.COLUMN_LEFT:+.3f}..{style.COLUMN_RIGHT:+.3f} with the prompt, "
                 f"votes and a {style.TRACE_SPAN_S:g} s EEG and decoder-window strip")
    lines.append(f"  the layout needs a screen at least {style.LAYOUT_ASPECT:.3f}:1 "
                 f"(config aspect {cfg.display.aspect:g})")
    return lines


# -- PsychoPy -------------------------------------------------------------------------------

def _stadium(width: float, height: float, n: int = 10) -> np.ndarray:
    """Vertices of a pill (rectangle with round ends), centred at (0, 0)."""
    r = height / 2
    half = max(width / 2 - r, 0.0)
    angles = np.linspace(-math.pi / 2, math.pi / 2, n)
    right = [(half + r * math.cos(a), r * math.sin(a)) for a in angles]
    left = [(-half - r * math.cos(a), -r * math.sin(a)) for a in angles]
    return np.array(right + left)


def _rgb255(rgb: tuple[float, float, float]) -> tuple[int, int, int]:
    r, g, b = (int(round((c + 1.0) * 127.5)) for c in rgb)
    return (r, g, b)


def tag_image(target: int, px_height: int = 96, text: str | None = None):
    """PIL RGBA image of the "15" (square corners) or "17" (pill) tag; ``text`` replaces
    the frequency (c-VEP: "A" / "B")."""
    from PIL import Image, ImageDraw, ImageFont

    w = int(round(px_height * style.TAG_SIZE[0] / style.TAG_SIZE[1]))
    img = Image.new("RGBA", (w, px_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = _rgb255(style.TARGET_COLORS[target]) + (255,)
    radius = px_height // 2 if target == 1 else 0
    draw.rounded_rectangle((0, 0, w - 1, px_height - 1), radius=radius, fill=fill)
    font = ImageFont.truetype(str(style.FONT_PATHS[style.MONO_BOLD_FONT]),
                              int(px_height * 0.72))
    if text is None:
        text = "15" if target == 0 else "17"
    draw.text((w / 2, px_height / 2), text, font=font, fill=(255, 255, 255, 255), anchor="mm")
    return img


class BoardRenderer:
    """Fullscreen PsychoPy window in the mockup's style: the board (64 squares in one
    ElementArrayStim, tags, the pieces PNG, an arrow), the side column (stage row, prompt,
    vote meters, PLAY / BACK panels) and the trace strip."""

    def __init__(self, cfg: StimulusConfig, flip: bool = False,
                 note: Callable[[str], None] = _stderr_note) -> None:
        from psychopy import event, visual  # lazy: never imported by tests

        _check_targets(cfg)
        self.cfg = cfg
        self.code = cfg.paradigm == "code"
        self.flip_board = flip
        self._visual = visual
        self._keys = event.getKeys
        self._note = note
        # The window's own timing check is skipped: measure_refresh() does that job.
        self.win = visual.Window(fullscr=True, screen=cfg.display.screen, units="height",
                                 color=style.PAPER, colorSpace="rgb", checkTiming=False)
        self.win.setMouseVisible(False)
        self._frame = 0
        self.static = self._static_layer()
        xys = np.array([square_center(sq, flip) for sq in range(N_SQUARES)]) \
            + np.array(style.BOARD_ORIGIN)
        self.squares = visual.ElementArrayStim(
            self.win, units="height", nElements=N_SQUARES, sizes=SQUARE_SIZE, xys=xys,
            colors=square_colors(PLAIN_GROUPS, square_luminances(PLAIN_GROUPS, 0.5, 0.5)),
            colorSpace="rgb", elementTex=None, elementMask=None, interpolate=False)
        self.tags = [visual.ImageStim(self.win,
                                      image=tag_image(k, text=CODE_TAGS[k] if self.code else None),
                                      units="height", size=style.TAG_SIZE, interpolate=True)
                     for k in (0, 1)]
        self._tag_pos = [tag_position(sq, flip) for sq in range(N_SQUARES)]
        self.pieces = None
        self._pieces_version = 0
        self._pieces_tries = 0
        self.arrow = visual.ShapeStim(self.win, units="height",
                                      vertices=self._arrow_vertices((12, 28)),
                                      fillColor=style.MARKER, lineColor=None,
                                      opacity=ARROW_OPACITY, closeShape=True)
        self._arrow: tuple[int, int] | None = None
        self._make_panels()
        self._make_header()
        self._make_confidence()
        self._make_trace()
        self._make_fps()

    # -- construction --------------------------------------------------------------------

    def _text(self, text: str, pos: tuple[float, float], height: float,
              font: str = style.MONO_FONT, color=style.INK, bold: bool = False,
              anchor: str = "left", wrap: float | None = None, valign: str = "center"):
        return self._visual.TextStim(self.win, text=text, units="height", pos=pos,
                                     height=height, font=font, fontFiles=list(style.FONT_FILES),
                                     bold=bold, color=color, colorSpace="rgb",
                                     anchorHoriz=anchor, alignText=anchor, anchorVert=valign,
                                     wrapWidth=wrap if wrap is not None else 2.0)

    def _rect(self, pos, size, fill=None, line=None, line_width: float = 1.0):
        return self._visual.Rect(self.win, units="height", pos=pos, size=size,
                                 fillColor=fill, lineColor=line, lineWidth=line_width,
                                 colorSpace="rgb")

    def _circle(self, pos, radius: float, fill=None, line=None, line_width: float = 1.5):
        return self._visual.Circle(self.win, units="height", pos=pos, radius=radius, edges=24,
                                   fillColor=fill, lineColor=line, lineWidth=line_width,
                                   colorSpace="rgb")

    def _title(self) -> list:
        h = style.TITLE_HEIGHT
        parts = [("BRAINCHESS ", style.INK), ("15", style.TRACE15), ("/", style.INK_2),
                 ("17", style.TRACE17)]
        if self.code:
            parts = [("BRAINCHESS ", style.INK), ("C", style.TRACE15), ("-", style.INK_2),
                     ("VEP", style.TRACE17)]
        width = style.text_width("".join(p for p, _ in parts), style.DISPLAY_FONT, h)
        h *= min(1.0, style.COLUMN_WIDTH / width)
        out, x = [], style.COLUMN_LEFT
        for text, color in parts:
            out.append(self._text(text, (x, style.TITLE_Y), h, style.DISPLAY_FONT, color))
            x += style.text_width(text, style.DISPLAY_FONT, h)
        return out

    def _stepper(self) -> list:
        out, x = [], style.COLUMN_LEFT
        self._step_dots = []
        r = style.STEPPER_HEIGHT * 0.32
        for _, label in style.STAGES:
            self._step_dots.append(self._circle((x + r, style.STEPPER_Y), r, None, style.INK_2))
            x += 2 * r + 0.008
            out.append(self._text(label, (x, style.STEPPER_Y), style.STEPPER_HEIGHT,
                                  style.MONO_BOLD_FONT, style.INK_2))
            x += style.text_width(label, style.MONO_BOLD_FONT, style.STEPPER_HEIGHT) + 0.022
        return out

    def _vote_row(self) -> list:
        h, box, y = style.VOTES_HEIGHT, style.VOTE_BOX, style.VOTES_Y
        out = [self._text("VOTES", (style.COLUMN_LEFT, y), h, color=style.INK_2)]
        x = style.COLUMN_LEFT + style.text_width("VOTES", style.MONO_FONT, h) + 0.02
        self._meters: list[list] = [[], []]
        for k, (label, color) in enumerate((("15 Hz", style.TRACE15), ("17 Hz", style.TRACE17))):
            out.append(self._text(label, (x, y), h, style.MONO_BOLD_FONT, style.INK))
            x += style.text_width(label, style.MONO_BOLD_FONT, h) + 0.008
            for _ in range(4):
                pos = (x + box / 2, y)
                self._meters[k].append(self._rect(pos, (box, box), None, color, 2.0) if k == 0
                                       else self._circle(pos, box / 2, None, color, 2.0))
                x += box + 0.006
            x += 0.016
        return out

    def _coordinates(self) -> list:
        out = []
        h, gap = style.COORD_HEIGHT, style.COORD_GAP
        left = style.BOARD_X - BOARD_SIZE / 2 - gap
        below = style.BOARD_Y - BOARD_SIZE / 2 - gap
        for i in range(8):
            col, row = grid_cell(i * 9, self.flip_board)    # a1, b2, ... h8: one per file/rank
            x = style.BOARD_X + (col - 3.5) * SQUARE_SIZE
            y = style.BOARD_Y + (row - 3.5) * SQUARE_SIZE
            out.append(self._text("abcdefgh"[i], (x, below), h, color=style.INK_2,
                                  anchor="center"))
            out.append(self._text(str(i + 1), (left, y), h, color=style.INK_2, anchor="center"))
        return out

    def _idle_panel(self, k: int, t) -> list:
        """A panel waiting for its turn: dashed frame, label, frequency, what it is for.
        All of it inside the panel's square, which the active panel paints over."""
        x, y = t.pos
        side = self.cfg.size
        out = []
        n = 9
        dash = side / n * 0.55
        for j in range(n):
            off = -side / 2 + (j + 0.5) * side / n
            for pos, size in (((x + off, y + side / 2), (dash, 0.002)),
                              ((x + off, y - side / 2), (dash, 0.002)),
                              ((x - side / 2, y + off), (0.002, dash)),
                              ((x + side / 2, y + off), (0.002, dash))):
                out.append(self._rect(pos, size, style.INK_2))
        out.append(self._text(t.label, (x, y + side * 0.2), style.PANEL_LABEL_HEIGHT,
                              style.DISPLAY_FONT, style.INK_2, anchor="center"))
        out += self._hz_pill(k, (x, y + side * 0.02))
        note = "undo the last pick" if k == 2 else "used to confirm or promote"
        out.append(self._text(note, (x, y - side * 0.24),
                              style.PANEL_NOTE_HEIGHT, color=style.INK_2, anchor="center",
                              wrap=side * 0.8))
        return out

    def _hz_pill(self, k: int, pos) -> list:
        h = style.PANEL_NOTE_HEIGHT * 1.15
        if self.code:
            lag = k * self.cfg.code.lag_frames
            text = f"+{1000 * lag / self.cfg.refresh:.0f} ms"
        else:
            text = f"{self.cfg.targets[k].freq:g} Hz"
        w = style.text_width(text, style.MONO_BOLD_FONT, h) + 0.016
        color = style.TARGET_COLORS[k]
        if k != 1:
            shape = self._rect(pos, (w, h * 1.7), None, color, 1.5)
        else:
            shape = self._visual.ShapeStim(self.win, units="height", pos=pos,
                                           vertices=_stadium(w, h * 1.7), fillColor=None,
                                           lineColor=color, lineWidth=1.5, closeShape=True,
                                           colorSpace="rgb")
        return [shape, self._text(text, pos, h, style.MONO_BOLD_FONT, color, anchor="center")]

    def _trace_frame(self) -> list:
        mid = (style.COLUMN_LEFT + style.COLUMN_RIGHT) / 2
        out = [self._rect((mid, (style.TRACE_TOP + style.TRACE_BOTTOM) / 2),
                          (style.COLUMN_WIDTH, style.TRACE_TOP - style.TRACE_BOTTOM),
                          style.SURFACE, style.RULE, 1.5)]
        h = style.TRACE_LABEL_HEIGHT
        out.append(self._text("EEG · DECODER WINDOWS",
                              (style.COLUMN_LEFT + 0.012, style.TRACE_HEADER_Y), h,
                              style.MONO_BOLD_FONT, style.INK_2))
        # legend, laid right to left from the strip's right edge
        x = style.COLUMN_RIGHT - 0.012
        sw = h * 0.8
        legend = ((("too early", "stale"), ("no answer", "none"), ("BACK", 2), ("B", 1),
                   ("A", 0)) if self.code else
                  (("too early", "stale"), ("no answer", "none"), ("17 Hz", 1), ("15 Hz", 0)))
        for label, kind in legend:
            x -= style.text_width(label, style.MONO_FONT, h)
            out.append(self._text(label, (x, style.TRACE_HEADER_Y), h, color=style.INK_2))
            x -= sw / 2 + 0.006
            pos = (x, style.TRACE_HEADER_Y)
            if kind == 0:
                out.append(self._rect(pos, (sw, sw), style.TRACE15))
            elif kind == 1:
                out.append(self._circle(pos, sw / 2, style.TRACE17))
            elif kind == 2:
                out.append(self._rect(pos, (sw, sw * 0.6), style.TRACE_BACK))
            elif kind == "none":
                out.append(self._rect(pos, (sw, 0.0025), style.INK_2))
            else:
                out.append(self._rect(pos, (sw, sw), style.RULE))
            x -= sw / 2 + 0.014
        out.append(self._text("win", (style.COLUMN_LEFT + 0.012,
                                      (style.WINDOWS_TOP + style.WINDOWS_BOTTOM) / 2), h,
                              color=style.INK_2))
        return out

    def _static_layer(self) -> list:
        """Everything that never changes, drawn once into a single image."""
        stims = self._title()
        stims.append(self._rect(((style.COLUMN_LEFT + style.COLUMN_RIGHT) / 2, style.RULE_Y),
                                (style.COLUMN_WIDTH, 0.002), style.RULE))
        stims += self._stepper() + ([] if self.code else self._vote_row()) + self._coordinates()
        stims.append(self._rect(style.BOARD_ORIGIN, (BOARD_SIZE + 0.006,) * 2, style.INK))
        for k, t in enumerate(self.cfg.targets):
            stims += self._idle_panel(k, t)
        stims += self._trace_frame()
        try:
            layer = self._visual.BufferImageStim(self.win, stim=stims)
            self.win.clearBuffer()
            return [layer]
        except Exception as exc:  # noqa: BLE001 - any capture failure: draw them each frame
            self._note(f"could not cache the static layer ({exc}); drawing it every frame")
            return stims

    def _make_panels(self) -> None:
        side = self.cfg.size
        self.panel_frames, self.panels, self.labels = [], [], []
        for k, t in enumerate(self.cfg.targets):
            x, y = t.pos
            # a hair wider than the panel, so it covers the idle panel's dashed outline
            outer = side + 0.006
            self.panel_frames.append(self._rect(t.pos, (outer, outer), style.TARGET_COLORS[k]))
            inner = side - 2 * style.PANEL_INSET
            self.panels.append(self._rect(t.pos, (inner, inner), to_psychopy_rgb(0.5)))
            self.labels.append(self._text(t.label, (x, y + side / 2 + 0.012),
                                          style.PANEL_LABEL_HEIGHT * 0.8, style.DISPLAY_FONT,
                                          style.INK, anchor="center", wrap=side + 0.06,
                                          valign="bottom"))
        self._labels = tuple(self.cfg.labels)
        cue = side * self.cfg.cue_scale
        self.cue_box = self._rect(self.cfg.targets[0].pos, (cue, cue), None, style.MARKER, 4.0)
        self._cue: int | None = None

    def _make_confidence(self) -> None:
        """c-VEP: the lines under the prompt, one TextStim each, re-laid out on change."""
        self.confidence = [self._text("", (style.COLUMN_LEFT,
                                           style.CONFIDENCE_Y - j * style.CONFIDENCE_STEP),
                                      style.CONFIDENCE_HEIGHT, style.MONO_FONT, style.INK)
                           for j in range(MAX_CONFIDENCE)]
        self._confidence: tuple[str, ...] = ()

    def _make_header(self) -> None:
        self.caption = self._text("", (style.COLUMN_LEFT, style.PROMPT_TOP), style.PROMPT_HEIGHT,
                                  style.BODY_FONT, style.INK, bold=True,
                                  wrap=style.COLUMN_WIDTH, valign="top")
        self._caption = ""
        self._stage: str | None = None
        self._votes: tuple[int, int, int] | None = None
        self._counts: dict[tuple[int, int], Any] = {}

    def _elements(self, n: int, sizes, **kw):
        return self._visual.ElementArrayStim(self.win, units="height", nElements=n, sizes=sizes,
                                             xys=np.zeros((n, 2)), colorSpace="rgb",
                                             opacities=0.0, elementTex=None, elementMask=None,
                                             **kw)

    def _make_trace(self) -> None:
        self.lanes: list = []
        self.lane_labels: list = []
        self._lane_names: tuple[str, ...] = ()
        self._lane_points: list[np.ndarray] = []
        lanes_h = style.LANES_TOP - style.WINDOWS_BOTTOM
        self.grid = self._elements(8, (0.0015, lanes_h), colors=style.GRID)
        self.ticks = self._elements(MAX_WINDOWS, np.full((MAX_WINDOWS, 2), 0.01),
                                    colors=style.INK_2)
        self.mark_lines = self._elements(MAX_MARKS, (0.002, lanes_h), colors=style.MARKER)
        self.mark_heads = self._elements(MAX_MARKS, 0.011, oris=45.0, colors=style.MARKER)

    def _make_fps(self) -> None:
        """Frame-rate readout in the screen's top-left corner, above the board."""
        w, h = self.win.size
        self.fps_text = self._text("-- fps", (-w / h / 2 + 0.012, 0.5 - 0.016), 0.014,
                                   color=style.INK_2)
        self._flips: deque[float] = deque()
        self._fps_due = 0.0
        self.dropped = 0

    def _count_flip(self, t: float) -> None:
        """Track the frame rate and dropped frames from flip times; refresh the readout."""
        if self._flips:
            self.dropped += dropped_frames((self._flips[-1], t), self.cfg.refresh)
        self._flips.append(t)
        while t - self._flips[0] > FPS_SPAN_S:
            self._flips.popleft()
        if t >= self._fps_due and len(self._flips) > 1:
            fps = (len(self._flips) - 1) / (self._flips[-1] - self._flips[0])
            self.fps_text.text = f"{fps:4.1f} fps \u00b7 {self.dropped} dropped"
            self._fps_due = t + FPS_EVERY_S

    def _arrow_vertices(self, arrow: tuple[int, int]) -> np.ndarray:
        return arrow_vertices(*arrow, flip=self.flip_board) + np.array(style.BOARD_ORIGIN)

    def measure_refresh(self, n: int = 120, warmup: int = 20) -> float:
        """Median-interval rate over ``n`` blank flips, after ``warmup`` unmeasured ones."""
        stamps = [self.win.flip() for _ in range(warmup + n)]
        problem = layout_warning(*self.win.size)
        if problem:
            self._note(f"warning: {problem}")
        return median_rate(stamps[warmup:])

    # -- per frame -----------------------------------------------------------------------

    def _update_pieces(self, snap: BoardSnapshot) -> None:
        """Load a new pieces PNG once per pieces command; keep the old one if it fails."""
        if snap.pieces is None or snap.pieces_version == self._pieces_version:
            return
        try:
            if self.pieces is None:
                # interpolate: the PNG is scaled to the board's physical pixels (about
                # 2x on Retina); nearest-neighbour would leave stair-stepped glyph edges
                self.pieces = self._visual.ImageStim(self.win, image=str(snap.pieces),
                                                     units="height", pos=style.BOARD_ORIGIN,
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

    def _draw_tags(self, groups: str) -> None:
        for k, char in enumerate("01"):
            tag = self.tags[k]
            for sq, g in enumerate(groups):
                if g == char:
                    tag.pos = self._tag_pos[sq]
                    tag.draw()

    def _draw_header(self, snap: BoardSnapshot) -> None:
        if snap.stage != self._stage:
            for (name, _), dot in zip(style.STAGES, self._step_dots):
                active = name == snap.stage
                dot.fillColor = style.MARKER if active else None
                dot.lineColor = style.MARKER if active else style.INK_2
            self._stage = snap.stage
        for dot in self._step_dots:
            dot.draw()
        if snap.text != self._caption:
            self.caption.text = snap.text
            self._caption = snap.text
        if snap.text:
            self.caption.draw()
        if self.code:
            return
        target, n, of = snap.votes
        if snap.votes != self._votes:
            for k, meter in enumerate(self._meters):
                lit = n if target == k else 0
                for j, stim in enumerate(meter):
                    stim.fillColor = style.TARGET_COLORS[k] if j < lit else None
            self._votes = snap.votes
        for meter in self._meters:
            for stim in meter:
                stim.draw()
        if (n, of) not in self._counts:
            self._counts[(n, of)] = self._text(f"{n} of {of}",
                                               (style.COLUMN_RIGHT, style.VOTES_Y),
                                               style.VOTES_HEIGHT, color=style.INK_2,
                                               anchor="right")
        self._counts[(n, of)].draw()

    def _draw_panels(self, snap: BoardSnapshot, panel_lums: np.ndarray) -> None:
        if snap.cue is not None and snap.cue < len(self.panels):
            if snap.cue != self._cue:
                self.cue_box.pos = self.cfg.targets[snap.cue].pos
                self._cue = snap.cue
            self.cue_box.draw()
        if snap.panels is None:
            return
        if snap.panels != self._labels:
            for label, text in zip(self.labels, snap.panels):
                label.text = text
            self._labels = snap.panels
        for k, panel in enumerate(self.panels):
            # a panel the command left out, or (c-VEP) gave no label, stays idle
            if k >= len(snap.panels) or (self.code and not snap.panels[k]):
                continue
            self.panel_frames[k].draw()
            panel.fillColor = to_psychopy_rgb(panel_lums[k])
            panel.draw()
            self.labels[k].draw()

    def _draw_confidence(self, snap: BoardSnapshot) -> None:
        if not self.code:
            return
        if snap.confidence != self._confidence:
            for j, stim in enumerate(self.confidence):
                stim.text = snap.confidence[j] if j < len(snap.confidence) else ""
            self._confidence = snap.confidence
        for j in range(min(len(snap.confidence), len(self.confidence))):
            if snap.confidence[j]:
                self.confidence[j].draw()

    def _set_lanes(self, names: tuple[str, ...]) -> None:
        lane = (style.LANES_TOP - style.LANES_BOTTOM) / max(len(names), 1)
        self.lanes = [self._visual.ShapeStim(self.win, units="height",
                                             vertices=[(0.0, 0.0), (0.0, 0.0)],
                                             closeShape=False, fillColor=None,
                                             lineColor=style.INK, lineWidth=1.2,
                                             colorSpace="rgb") for _ in names]
        self.lane_labels = [self._text(name, (style.COLUMN_LEFT + 0.012,
                                              style.LANES_TOP - lane * (i + 0.5)),
                                       style.TRACE_LABEL_HEIGHT, color=style.INK_2)
                            for i, name in enumerate(names)]
        self._lane_names = names
        self._lane_points = [np.empty((0, 2)) for _ in names]

    def _update_trace(self, view: TraceView) -> None:
        x0, x1, span = style.TRACE_PLOT_LEFT, style.TRACE_PLOT_RIGHT, style.TRACE_SPAN_S
        if view.values.shape[1] == len(self.lanes):
            self._lane_points = lane_points(view, x0, x1, style.LANES_TOP, style.LANES_BOTTOM,
                                            span)
            for stim, pts in zip(self.lanes, self._lane_points):
                if len(pts) >= 2:
                    stim.vertices = pts
        mid_y = (style.LANES_TOP + style.WINDOWS_BOTTOM) / 2
        # one grid line per stream second, like recorder paper
        xys, alpha = np.zeros((8, 2)), np.zeros(8)
        if view.t_now is not None:
            t0 = view.t_now - span
            for j, s in enumerate(range(math.ceil(t0), math.ceil(t0) + 8)):
                x = x0 + (s - t0) / span * (x1 - x0)
                if x <= x1:
                    xys[j], alpha[j] = (x, mid_y), 1.0
        self.grid.xys, self.grid.opacities = xys, alpha
        top, bottom = style.WINDOWS_TOP, style.WINDOWS_BOTTOM
        xys = np.zeros((MAX_WINDOWS, 2))
        sizes = np.full((MAX_WINDOWS, 2), 0.01)
        colors = np.tile(np.array(style.INK_2), (MAX_WINDOWS, 1))
        alpha = np.zeros(MAX_WINDOWS)
        fired = []
        for j, (left, width, w) in enumerate(window_marks(view, x0, x1, span)[-MAX_WINDOWS:]):
            x = left + width / 2
            if w.raw == 0:
                xys[j], sizes[j], colors[j] = (x, (top + bottom) / 2), (width, top - bottom), \
                    style.TRACE15
            elif w.raw == 1:                       # a little shorter: not by colour alone
                h = top - bottom - 0.005
                xys[j], sizes[j], colors[j] = (x, bottom + h / 2), (width, h), style.TRACE17
            elif w.raw == 2:                       # c-VEP Back: shorter still
                h = top - bottom - 0.01
                xys[j], sizes[j], colors[j] = (x, bottom + h / 2), (width, h), style.TRACE_BACK
            else:
                xys[j], sizes[j] = (x, (top + bottom) / 2), (width, 0.0025)
            alpha[j] = 1.0 if w.fresh else 0.3
            if w.fired:
                fired.append(x)
        self.ticks.xys, self.ticks.sizes, self.ticks.colors = xys, sizes, colors
        self.ticks.opacities = alpha
        lines, heads, alpha = np.zeros((MAX_MARKS, 2)), np.zeros((MAX_MARKS, 2)), np.zeros(MAX_MARKS)
        for j, x in enumerate(fired[-MAX_MARKS:]):
            lines[j], heads[j], alpha[j] = (x, mid_y), (x, style.MARKER_Y), 1.0
        self.mark_lines.xys, self.mark_lines.opacities = lines, alpha
        self.mark_heads.xys, self.mark_heads.opacities = heads, alpha

    def _draw_trace(self, snap: BoardSnapshot, trace: TraceBuffer | None) -> None:
        if snap.lanes != self._lane_names:
            self._set_lanes(snap.lanes)
        if trace is not None and self._frame % TRACE_EVERY == 0:
            self._update_trace(trace.view())
        self.grid.draw()
        for label in self.lane_labels:
            label.draw()
        for stim, pts in zip(self.lanes, self._lane_points):
            if len(pts) >= 2:
                stim.draw()
        self.ticks.draw()
        self.mark_lines.draw()
        self.mark_heads.draw()

    def draw(self, snap: BoardSnapshot, square_lums: np.ndarray, panel_lums: np.ndarray,
             trace: TraceBuffer | None = None) -> None:
        """Queue one frame; never blocks (flip() waits for vsync)."""
        for stim in self.static:
            stim.draw()
        self.squares.colors = square_colors(snap.groups, square_lums)
        self.squares.draw()
        self._draw_tags(snap.groups)
        self._update_pieces(snap)
        if self.pieces is not None:
            self.pieces.draw()
        if snap.arrow is not None:
            if snap.arrow != self._arrow:
                self.arrow.vertices = self._arrow_vertices(snap.arrow)
                self._arrow = snap.arrow
            self.arrow.draw()
        self._draw_panels(snap, panel_lums)
        self._draw_header(snap)
        self._draw_confidence(snap)
        self._draw_trace(snap, trace)
        self.fps_text.draw()
        self._frame += 1

    def flip(self) -> float:
        t = float(self.win.flip())
        self._count_flip(t)
        return t

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
        code = CodeClock(cfg.code.cycle_frames) if cfg.paradigm == "code" else None
        prev: BoardSnapshot | None = None
        last: float | None = None
        while not reader.done:
            if renderer.escape_pressed():
                timing.aborted = True
                break
            snap = state.snapshot()
            started = started or not snap.holding
            cycles: list[str] = []
            if code is not None:   # c-VEP: frames since this layout's first flickering frame
                index, cycles = code.step(snap)
            else:
                index = frame
            renderer.draw(snap, *frame_values(snap, index, cfg), state.trace)
            t = renderer.flip()
            markers.emit(_events(prev, snap) + cycles)
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
