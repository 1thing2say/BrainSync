"""BrainChess board stimulus: protocol, state, luminance and CLI; no window, no psychopy."""
from __future__ import annotations

import io
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("chess")   # brainchess.stimulus imports render, which needs it

from brainsync.brainchess import stimulus as bs
from brainsync.brainchess import style
from brainsync.brainchess.render import BOARD_SIZE, SQUARE_SIZE, square_center
from brainsync.brainchess.trace import Window
from brainsync.brainchess.stimulus import (BoardSnapshot, BoardState, Command, CommandReader,
                                           arrow_vertices, check_layout, frame_values,
                                           parse_command, square_colors, square_luminances)
from brainsync.stimulus.config import available_configs, load_config
from brainsync.stimulus.flicker import frame_luminances

G0 = "0" * 32 + "1" * 32


# -- protocol -------------------------------------------------------------------------------

def test_parse_every_verb():
    assert parse_command(f"groups {G0}\n") == Command("groups", G0)
    mixed = "s" + "." * 62 + "1"
    assert parse_command(f"  groups {mixed}  ").value == mixed
    assert parse_command("pieces /tmp/a b.png") == Command("pieces", Path("/tmp/a b.png"))
    assert parse_command("arrow 12 28") == Command("arrow", (12, 28))
    assert parse_command("arrow  0   63") == Command("arrow", (0, 63))
    assert parse_command("arrow none") == Command("arrow", None)
    assert parse_command("panels PLAY|BACK") == Command("panels", ("PLAY", "BACK"))
    assert parse_command("panels  Queen | Knight ") == Command("panels", ("Queen", "Knight"))
    assert parse_command("panels none") == Command("panels", None)
    assert parse_command("text White to move: look at a piece") == Command(
        "text", "White to move: look at a piece")
    assert parse_command("text") == Command("text", "")
    assert parse_command("text   ") == Command("text", "")
    assert parse_command("hold") == Command("hold") and parse_command("go\n") == Command("go")
    assert parse_command("stage confirm") == Command("stage", "confirm")
    assert parse_command("stage none") == Command("stage", "none")
    assert parse_command("votes 1 3 4") == Command("votes", (1, 3, 4))
    assert parse_command("votes - 0 4") == Command("votes", (-1, 0, 4))
    assert parse_command("lanes P7, P8,O1,O2") == Command("lanes", ("P7", "P8", "O1", "O2"))
    assert parse_command("window 12.25 0 1 0") == Command("window", Window(12.25, 0, True,
                                                                           False))
    assert parse_command("window 3 - 0 1").value == Window(3.0, -1, False, True)
    eeg = parse_command("eeg 10.5 0.02 1.5 -2 3e1 0")
    assert eeg.verb == "eeg" and eeg.value[:2] == (10.5, 0.02)
    np.testing.assert_array_equal(eeg.value[2], [1.5, -2.0, 30.0, 0.0])


@pytest.mark.parametrize("line, match", [
    ("", "blank"),
    ("   \n", "blank"),
    ("groups " + "." * 63, "64 characters"),
    ("groups " + "." * 65, "64 characters"),
    ("groups", "64 characters"),
    ("groups " + "x" + "." * 63, "only"),
    ("groups " + "2" + "." * 63, "only"),
    ("pieces", "PNG path"),
    ("arrow 12", "two square"),
    ("arrow 1 2 3", "two square"),
    ("arrow 12 64", "out of range"),
    ("arrow -1 5", "out of range"),
    ("arrow e2 e4", "integer"),
    ("arrow 7 7", "different"),
    ("panels PLAY", "label0"),
    ("panels a|b|c", "label0"),
    ("hold now", "no arguments"),
    ("go 1", "no arguments"),
    ("flash 3", "unknown command"),
    ("GO", "unknown command"),
    ("stage", "stage must be"),
    ("stage castle", "stage must be"),
    ("votes 0 1", "votes needs"),
    ("votes 2 1 4", "target must be"),
    ("votes 0 5 4", "n <= of"),
    ("votes 0 0 0", "n <= of"),
    ("votes 0 -1 4", ">= 0"),
    ("votes 0 x 4", "integer"),
    ("lanes", "comma-separated"),
    ("lanes O1,,O2", "comma-separated"),
    ("lanes " + ",".join("abcdefghi"), "comma-separated"),
    ("eeg 1 0.02", "eeg needs"),
    ("eeg 1 0 5", "dt must be"),
    ("eeg x 0.02 5", "t must be a number"),
    ("eeg 1 0.02 5 nan", "finite"),
    ("window 1 0 1", "window needs"),
    ("window 1 2 1 0", "target must be"),
    ("window 1 0 yes 0", "flag must be"),
    ("window inf 0 1 0", "finite"),
])
def test_parse_errors(line, match):
    with pytest.raises(ValueError, match=match):
        parse_command(line)


def test_board_state_apply_and_snapshot():
    state = BoardState()
    s0 = state.snapshot()
    assert s0 == BoardSnapshot() and s0.holding and s0.groups == "." * 64
    assert s0.pieces is None and s0.arrow is None and s0.panels is None and s0.text == ""
    for line in (f"groups {G0}", "pieces /tmp/p.png", "arrow 12 28", "panels Q|N",
                 "text hello", "go"):
        state.apply(parse_command(line))
    s = state.snapshot()
    assert s == BoardSnapshot(groups=G0, pieces=Path("/tmp/p.png"), pieces_version=1,
                              arrow=(12, 28), panels=("Q", "N"), text="hello", holding=False)
    assert s0 == BoardSnapshot()                       # snapshots are immutable copies
    state.apply(parse_command("pieces /tmp/p.png"))    # same path again: reload
    for line in ("arrow none", "panels none", "text", "hold"):
        state.apply(parse_command(line))
    s = state.snapshot()
    assert s.pieces_version == 2 and s.arrow is None and s.panels is None
    assert s.text == "" and s.holding and s.groups == G0
    with pytest.raises(ValueError, match="unknown command"):
        state.apply(Command("flash"))


def test_board_state_routes_the_strip_to_the_trace():
    state = BoardState()
    for line in ("stage dest", "votes 0 2 4", "lanes O1,O2"):
        state.apply(parse_command(line))
    s = state.snapshot()
    assert s.stage == "dest" and s.votes == (0, 2, 4) and s.lanes == ("O1", "O2")
    assert state.trace.n_lanes == 2
    state.apply(parse_command("eeg 5.0 0.02 1 2 3 4 5 6"))      # three rows of two lanes
    state.apply(parse_command("window 5.0 1 1 1"))
    assert state.snapshot() == s                     # the strip's data is not in snapshots
    view = state.trace.view()
    np.testing.assert_allclose(view.times, [4.96, 4.98, 5.0])
    np.testing.assert_array_equal(view.values, [[1, 2], [3, 4], [5, 6]])
    assert view.windows == (Window(5.0, 1, True, True),)
    with pytest.raises(ValueError, match="rows of 2 lanes"):
        state.apply(parse_command("eeg 6.0 0.02 1 2 3"))
    assert BoardSnapshot().stage == "none" and BoardSnapshot().votes == (-1, 0, 4)


def test_command_reader_stringio_bad_lines_and_eof():
    notes: list[str] = []
    stream = io.StringIO(f"groups {G0}\nbogus\narrow 1 99\n\ntext ready\ngo\n")
    state = BoardState()
    reader = CommandReader(stream, state, note=notes.append).start()
    assert reader.wait(2.0) and reader.done
    assert reader.n_lines == 6 and reader.n_ignored == 3
    assert len(notes) == 3 and notes[0].startswith("ignored line 2: unknown command")
    assert "line 3" in notes[1] and "out of range" in notes[1] and "line 4" in notes[2]
    s = state.snapshot()
    assert s.groups == G0 and s.text == "ready" and not s.holding and s.arrow is None


def test_command_reader_ignores_an_undecodable_line():
    # regression: a non-UTF-8 byte on stdin raised UnicodeDecodeError outside the per-line
    # handling, which stopped the reader (and the run) as at EOF and lost the good lines
    notes: list[str] = []
    raw = f"groups {G0}\nbogus\n".encode() + b"\xff\xfe junk\ntext hi\n"
    state = BoardState()
    reader = CommandReader(bs.command_stream(io.BytesIO(raw)), state, note=notes.append)
    reader.run()
    assert reader.n_lines == 4 and reader.n_ignored == 2
    assert not any(n.startswith("stopped reading") for n in notes)
    s = state.snapshot()
    assert s.groups == G0 and s.text == "hi"


def test_command_reader_closed_stream():
    notes: list[str] = []
    stream = io.StringIO("go\n")
    stream.close()
    reader = CommandReader(stream, BoardState(), note=notes.append)
    reader.run()
    assert reader.done and notes and notes[0].startswith("stopped reading")


# -- luminance ------------------------------------------------------------------------------

def test_square_luminances_checker_and_groups():
    plain = square_luminances("." * 64, 0.9, 0.1)
    assert plain.shape == (64,)
    assert plain[0] == 0.22 and plain[1] == 0.32 and plain[8] == 0.32 and plain[9] == 0.22
    assert plain[63] == 0.22 and plain[7] == 0.32          # h8 dark, h1 light
    for sq in range(64):
        dark = (sq % 8 + sq // 8) % 2 == 0
        assert plain[sq] == (0.22 if dark else 0.32)
    groups = "0" + "1" + "s" + "." * 61
    lum = square_luminances(groups, 0.9, 0.1, light=0.4, dark=0.3)
    assert lum[0] == 0.9 and lum[1] == 0.1 and lum[2] == bs.SELECTED_LUM
    assert lum[3] == 0.4 and lum[4] == 0.3
    with pytest.raises(ValueError, match="64"):
        square_luminances("." * 10, 0.5, 0.5)
    with pytest.raises(ValueError, match="only"):
        square_luminances("?" * 64, 0.5, 0.5)


def test_square_colors_grey_flicker_on_the_palette_board():
    groups = "s" + "0" + "1" + "." * 61
    lums = square_luminances(groups, 1.0, 0.25)
    rgb = square_colors(groups, lums)
    assert rgb.shape == (64, 3)
    np.testing.assert_allclose(rgb[1], [1.0, 1.0, 1.0])     # flicker: grey, full swing
    np.testing.assert_allclose(rgb[2], [-0.5, -0.5, -0.5])
    np.testing.assert_allclose(rgb[3], style.SQ_LIGHT)      # d1 is light
    np.testing.assert_allclose(rgb[4], style.SQ_DARK)       # e1 is dark
    np.testing.assert_allclose(rgb[0], style.SELECTED)      # amber, not grey
    r, g, b = rgb[0]
    assert r > g > b
    assert np.all(rgb >= -1) and np.all(rgb <= 1)
    dark = [sq for sq in range(64) if (sq % 8 + sq // 8) % 2 == 0]
    plain = square_colors("." * 64, square_luminances("." * 64, 0.0, 1.0))
    np.testing.assert_allclose(plain[dark], np.tile(style.SQ_DARK, (32, 1)))
    with pytest.raises(ValueError, match="64 luminances"):
        square_colors(groups, np.zeros(3))


def test_frame_values_hold_and_go():
    cfg = load_config("chess")
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform)
    held = BoardSnapshot(groups=G0, holding=True)
    running = BoardSnapshot(groups=G0, holding=False)
    for i in (0, 1, 7, 123):
        sq, pn = frame_values(held, i, cfg)
        np.testing.assert_allclose(pn, still)
        np.testing.assert_allclose(sq[:32], still[0])
        np.testing.assert_allclose(sq[32:], still[1])
        sq, pn = frame_values(running, i, cfg)
        lum = frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform)
        np.testing.assert_allclose(pn, lum)
        np.testing.assert_allclose(sq[:32], lum[0])
        np.testing.assert_allclose(sq[32:], lum[1])
    # plain squares never flicker
    for i in range(10):
        sq, _ = frame_values(BoardSnapshot(holding=False), i, cfg)
        np.testing.assert_array_equal(sq, square_luminances("." * 64, 0, 0))
    one = load_config("one_target_15")
    with pytest.raises(ValueError, match="two-target"):
        frame_values(running, 0, one)


def test_arrow_vertices():
    v = arrow_vertices(12, 28)                     # e2 -> e4, straight up
    assert v.shape == (7, 2)
    np.testing.assert_allclose(v[3], square_center(28))
    tail = (v[0] + v[6]) / 2
    np.testing.assert_allclose(tail, square_center(12), atol=1e-12)
    assert np.allclose(v[:, 0].mean(), square_center(12)[0])
    flipped = arrow_vertices(12, 28, flip=True)
    np.testing.assert_allclose(flipped[3], square_center(28, flip=True))
    with pytest.raises(ValueError, match="different"):
        arrow_vertices(5, 5)


# -- config and CLI -------------------------------------------------------------------------

def test_chess_config_panels_in_the_side_column():
    assert "chess" in available_configs()
    cfg = load_config("chess")
    assert cfg.name == "chess" and cfg.n_targets == 2
    np.testing.assert_array_equal(cfg.freqs, [15.0, 17.0])
    np.testing.assert_array_equal(cfg.phases, [0.0, 0.0])
    assert cfg.labels == ("PLAY", "BACK") and cfg.waveform == "sine" and not cfg.hold_cue
    assert cfg.refresh == 60.0 and cfg.display.aspect == 1.5
    (x0, y0), (x1, y1) = (t.pos for t in cfg.targets)
    assert x0 == pytest.approx(0.335) and x1 == pytest.approx(0.615) and y0 == y1 == 0.0
    board_right = style.BOARD_X + BOARD_SIZE / 2
    for t in cfg.targets:
        assert t.pos[0] - cfg.size / 2 >= style.COLUMN_LEFT - 1e-9 > board_right
        assert t.pos[0] + cfg.reach <= style.COLUMN_RIGHT + 1e-9 <= cfg.display.aspect / 2
    assert x1 - x0 - cfg.size == pytest.approx(0.08)        # the gap between the panels
    assert style.LAYOUT_ASPECT <= cfg.display.aspect
    check_layout(cfg)
    with pytest.raises(ValueError, match="inside the board"):
        check_layout(load_config("two_targets"))
    with pytest.raises(ValueError, match="two-target"):
        check_layout(load_config("one_target_15"))


@pytest.mark.parametrize("changes, match", [
    ({"x_offset": 0.48}, "past the side column"),           # 0.74: inside 0.75, not 0.735
    ({"x_offset": 0.3}, "inside the board"),
    ({"rows": 2, "cols": 1, "x_offset": 0.48}, "between the vote meters"),   # stacked
])
def test_check_layout_rejects_panels_that_do_not_fit(tmp_path, changes, match):
    original = Path(load_config("chess").path).read_text()
    lines = original.splitlines()
    for key, value in changes.items():
        lines = [f"{key} = {value}" if ln.split("=")[0].strip() == key else ln for ln in lines]
    path = tmp_path / "moved.toml"
    path.write_text("\n".join(lines))
    with pytest.raises(ValueError, match=match):
        check_layout(load_config(str(path)))


def test_check_layout_needs_a_wide_enough_screen():
    from dataclasses import replace
    cfg = load_config("chess")
    with pytest.raises(ValueError, match="need 1.474:1"):
        check_layout(replace(cfg, display=replace(cfg.display, aspect=1.45)))


def test_layout_warning_and_tag_positions():
    assert bs.layout_warning(1512, 982) is None              # a MacBook: 1.54:1
    assert "narrower" in bs.layout_warning(1024, 768)        # 4:3
    x, y = bs.tag_position(0)                                # a1, bottom left
    cx, cy = square_center(0)
    half = SQUARE_SIZE / 2
    assert x + style.TAG_SIZE[0] / 2 == pytest.approx(style.BOARD_X + cx + half - style.TAG_INSET)
    assert y + style.TAG_SIZE[1] / 2 == pytest.approx(style.BOARD_Y + cy + half - style.TAG_INSET)
    fx, fy = bs.tag_position(0, flip=True)                   # a1 top right when flipped
    assert fx > x and fy > y


def test_text_width_matches_the_font_metrics():
    w = style.text_width("BRAINCHESS", style.DISPLAY_FONT, 0.05)
    assert w == pytest.approx(0.4804, abs=1e-3)               # measured against PsychoPy
    assert style.text_width("", style.MONO_FONT, 0.02) == 0
    mono = style.text_width("O1", style.MONO_FONT, 0.02)
    assert style.text_width("O1O1", style.MONO_FONT, 0.02) == pytest.approx(2 * mono)
    assert style.text_width("\u2603", style.MONO_FONT, 0.02) > 0   # no glyph: a space
    with pytest.raises(ValueError, match="unknown font"):
        style.text_width("x", "Comic Sans", 0.02)
    assert len(style.FONT_FILES) == 5 and all(Path(f).exists() for f in style.FONT_FILES)
    assert style.hex_rgb("#FFFFFF") == (1.0, 1.0, 1.0) and style.hex_rgb("000000") == (-1, -1, -1)
    with pytest.raises(ValueError, match="RRGGBB"):
        style.hex_rgb("#FFF")


def test_plan_via_main(capsys):
    assert bs.main(["--plan"]) == 0
    out, err = capsys.readouterr()
    assert "config chess" in out
    assert ("board 0.9 screen heights, square 0.1125, centre (-0.260, +0.015), white at the "
            "bottom") in out
    assert "15.000 Hz" in out and "17.000 Hz" in out
    assert "4.000 frames/cycle" in out and "3.529 frames/cycle" in out
    assert "panel centre (+0.335, +0.000)" in out and "panel centre (+0.615, +0.000)" in out
    assert "'PLAY'" in out and "'BACK'" in out
    assert "clear the board edge by 0.045" in out and "grey with a 15 / 17 tag" in out
    assert "at least 1.474:1" in out
    assert "15-25 Hz" in err                              # photosensitivity warning
    assert bs.main(["--plan", "--flip"]) == 0
    assert "black at the bottom (h8 bottom left)" in capsys.readouterr().out


def test_plan_errors(capsys):
    assert bs.main(["--plan", "-c", "missing_config"]) == 2
    assert "missing_config" in capsys.readouterr().err
    assert bs.main(["--plan", "-c", "two_targets"]) == 2
    assert "inside the board" in capsys.readouterr().err
    assert bs.main(["--plan", "--refresh", "30"]) == 2
    assert "30 Hz refresh" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        bs.main(["--plan", "--refresh", "fast"])


def test_import_does_not_load_psychopy():
    code = ("import sys, brainsync.brainchess.stimulus, brainsync.brainchess.render; "
            "print('psychopy' in sys.modules, 'matplotlib.pyplot' in sys.modules)")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert res.stdout.split() == ["False", "False"]



def _exit_with_stdin_open(exit_call: str) -> tuple[int, str, str]:
    """Run a reader on an open stdin pipe, then exit with ``exit_call``; (code, out, err)."""
    code = ("import sys, time\n"
            "from brainsync.brainchess import stimulus as bs\n"
            "bs.CommandReader(bs.command_stream(sys.stdin), bs.BoardState()).start()\n"
            "time.sleep(0.3)\n"
            "print('bye')\n"
            f"{exit_call}\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        rc = proc.wait(timeout=30)   # stdin stays open, as the game keeps it on Escape
        return rc, proc.stdout.read(), proc.stderr.read()
    finally:
        proc.kill()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def test_exit_with_stdin_open_keeps_its_code():
    # Escape ends the run while the reader thread is blocked in readline on the open
    # pipe; a plain sys.exit then aborted at interpreter shutdown with code -6
    rc, out, err = _exit_with_stdin_open("bs.hard_exit(1)")
    assert rc == 1, err
    assert "Fatal Python error" not in err
    assert out == "bye\n"   # buffered output is flushed before leaving

class _FakeBoard:
    """Stands in for BoardRenderer so main() runs without a display."""

    measured = 60.01
    instances: list[_FakeBoard] = []

    def __init__(self, cfg, flip=False):
        self.cfg, self.flip_board = cfg, flip
        self.t = 0.0
        self.frames: list[tuple[BoardSnapshot, np.ndarray, np.ndarray]] = []
        self.escape_after: int | None = None
        self.closed = False
        _FakeBoard.instances.append(self)

    def measure_refresh(self):
        return self.measured

    def draw(self, snap, square_lums, panel_lums, trace=None):
        self.frames.append((snap, square_lums, panel_lums))
        self.trace = trace

    def flip(self):
        self.t += 1 / 60.0
        if len(self.frames) == 3:
            self.t += 0.05                             # one late frame
        return self.t

    def escape_pressed(self):
        return self.escape_after is not None and len(self.frames) >= self.escape_after

    def close(self):
        self.closed = True


@pytest.fixture
def fake_board(monkeypatch):
    _FakeBoard.instances = []
    _FakeBoard.measured = 60.01
    monkeypatch.setattr(bs, "BoardRenderer", _FakeBoard)
    return _FakeBoard


def test_main_runs_until_eof(fake_board, monkeypatch, capsys):
    import threading
    gate = threading.Event()
    release = threading.Event()

    class Stdin(io.StringIO):
        lines = [f"groups {G0}\n", "go\n", "bad line\n"]

        def readline(self, *args):
            if not self.lines:
                release.wait(5)
                return ""
            gate.wait(5)
            return self.lines.pop(0)

    monkeypatch.setattr(sys, "stdin", Stdin())

    def draw(self, snap, sq, pn, trace=None):
        assert isinstance(trace, bs.TraceBuffer)
        self.frames.append((snap, sq, pn))
        if len(self.frames) == 5:
            gate.set()
        # EOF only once the flicker has run a while, so frames show the commands
        if sum(not f[0].holding for f in self.frames) == 10:
            release.set()

    monkeypatch.setattr(_FakeBoard, "draw", draw)
    assert bs.main(["--markers", "monotonic", "--flip"]) == 0
    out, err = capsys.readouterr()
    board = fake_board.instances[0]
    assert board.closed and board.flip_board
    assert "measured refresh: 60.01 Hz" in err
    assert err.index("15-25 Hz") < err.index("measured refresh")
    assert "ignored line 3: unknown command" in err and "ignored 1 of 3" in err
    assert "timing:" in out and "1 dropped overall" in out and "measured 60.01" in out
    snaps = [f[0] for f in board.frames]
    assert snaps[0].holding and snaps[-1].groups == G0 and not snaps[-1].holding
    # held frames are still; the first go frame is flicker frame 0, then it counts up
    cfg = load_config("chess")
    first_go = next(k for k, s in enumerate(snaps) if not s.holding)
    for k in (0, first_go, first_go + 1, first_go + 5):
        i = max(0, k - first_go)
        lum = frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform)
        np.testing.assert_allclose(board.frames[k][2], lum)
    assert "] hold" in err and "] go" in err and f"] groups:{G0}" in err


def test_main_escape_aborts(fake_board, monkeypatch, capsys):
    class Stdin(io.StringIO):
        def readline(self, *args):
            import time
            time.sleep(5)
            return ""

    monkeypatch.setattr(sys, "stdin", Stdin())
    orig = _FakeBoard.__init__

    def init(self, cfg, flip=False):
        orig(self, cfg, flip)
        self.escape_after = 10

    monkeypatch.setattr(_FakeBoard, "__init__", init)
    assert bs.main([]) == 1
    out, err = capsys.readouterr()
    assert "aborted" in err and "ABORTED" in out and fake_board.instances[0].closed
    assert "[marker" not in err                           # --markers defaults to none


def test_main_refuses_refresh_mismatch(fake_board, capsys):
    fake_board.measured = 120.0
    assert bs.main([]) == 2
    err = capsys.readouterr().err
    assert "measured refresh 120.00 Hz differs" in err and "measured refresh:" not in err
    assert fake_board.instances[0].closed and not fake_board.instances[0].frames


# -- renderer against a fake psychopy -------------------------------------------------------

class _FakeStim:
    def __init__(self, win=None, **kw):
        self.kw = kw
        self.drawn = 0
        for k, v in kw.items():
            setattr(self, k, v)

    def draw(self):
        self.drawn += 1


class _FakeImage(_FakeStim):
    fail = 0

    def __init__(self, win=None, **kw):
        if _FakeImage.fail:
            _FakeImage.fail -= 1
            raise OSError("truncated")
        super().__init__(win, **kw)
        self.loads = 1

    def __setattr__(self, name, value):
        if name == "image" and hasattr(self, "loads"):
            object.__setattr__(self, "loads", self.loads + 1)
        object.__setattr__(self, name, value)


class _FakePsychoWindow:
    size = (1512, 982)

    def __init__(self, **kw):
        self.kw = kw
        self.t = 0.0
        self.closed = False

    def setMouseVisible(self, visible):
        self.mouse = visible

    def flip(self):
        self.t += 1 / 60.0
        return self.t

    def clearBuffer(self):
        self.cleared = True

    def close(self):
        self.closed = True


def _fake_visual() -> types.SimpleNamespace:
    return types.SimpleNamespace(Window=_FakePsychoWindow, ElementArrayStim=_FakeStim,
                                 ImageStim=_FakeImage, ShapeStim=_FakeStim, Rect=_FakeStim,
                                 TextStim=_FakeStim, Circle=_FakeStim, BufferImageStim=_FakeStim)


def test_board_renderer_against_fake_psychopy(monkeypatch):
    keys: list[list[str]] = [[], ["escape"]]
    visual = _fake_visual()
    event = types.SimpleNamespace(getKeys=lambda keyList: keys.pop(0) if keys else [])
    fake = types.ModuleType("psychopy")
    fake.visual, fake.event = visual, event
    monkeypatch.setitem(sys.modules, "psychopy", fake)
    notes: list[str] = []
    cfg = load_config("chess")
    r = bs.BoardRenderer(cfg, flip=True, note=notes.append)
    assert r.win.kw["fullscr"] is True and r.win.kw["units"] == "height"
    assert r.win.mouse is False and notes == []
    assert r.win.kw["color"] == style.PAPER
    sq = r.squares.kw
    assert sq["nElements"] == 64 and sq["elementTex"] is None and sq["elementMask"] is None
    assert sq["sizes"] == pytest.approx(0.1125)
    np.testing.assert_allclose(sq["xys"][0],
                               np.array(square_center(0, flip=True)) + style.BOARD_ORIGIN)
    assert [p.kw["pos"] for p in r.panels] == [t.pos for t in cfg.targets]
    # everything static is one cached image: title, stage row, coordinates, idle panels...
    assert len(r.static) == 1 and r.win.cleared
    texts = [s.kw.get("text") for s in r.static[0].kw["stim"] if "text" in s.kw]
    for want in ("BRAINCHESS ", "15", "17", "PIECE", "SQUARE", "PROMOTE", "CONFIRM", "VOTES",
                 "a", "h", "1", "8", "PLAY", "BACK", "EEG \u00b7 DECODER WINDOWS", "too early"):
        assert want in texts
    assert r.measure_refresh() == pytest.approx(60.0)

    snap = BoardSnapshot(groups="s" + G0[1:], pieces=Path("/tmp/p.png"), pieces_version=1,
                         arrow=(12, 28), panels=("Q", "N"), text="pick", holding=False,
                         stage="promo", votes=(1, 3, 4), lanes=("O1", "O2"))
    sql, pnl = frame_values(snap, 0, cfg)
    pnl = np.array([1.0, 0.0])
    trace = bs.TraceBuffer(n_lanes=2, clock=lambda: 0.0)
    trace.add_samples(10.0, 0.02, np.arange(20.0))
    trace.add_window(Window(9.75, 0, True, False))
    trace.add_window(Window(10.0, 1, True, True))
    r.draw(snap, sql, pnl, trace)
    assert r.static[0].drawn == 1
    assert r.squares.drawn == 1 and r.squares.colors.shape == (64, 3)
    assert r.tags[0].drawn == 31 and r.tags[1].drawn == 32     # a1 is selected, not tagged
    assert r.tags[1].pos == bs.tag_position(63, flip=True)
    lit = [d.fillColor for d in r._step_dots]
    assert lit == [None, None, style.MARKER, None]           # promo
    assert [m.fillColor for m in r._meters[1]] == [style.TRACE17] * 3 + [None]
    assert all(m.fillColor is None for m in r._meters[0])
    assert r._counts[(3, 4)].kw["text"] == "3 of 4" and r._counts[(3, 4)].drawn == 1
    assert [lb.kw["text"] for lb in r.lane_labels] == ["O1", "O2"]
    assert all(lane.drawn == 1 and len(lane.vertices) == 10 for lane in r.lanes)
    assert r.ticks.opacities[:2].tolist() == [1.0, 1.0] and r.ticks.opacities[2] == 0
    np.testing.assert_allclose(r.ticks.colors[:2], [style.TRACE15, style.TRACE17])
    assert r.mark_lines.opacities.tolist() == [1.0] + [0.0] * 7
    assert r.mark_heads.xys[0][1] == pytest.approx(style.MARKER_Y)
    assert r.pieces.drawn == 1 and r.pieces.kw["image"] == "/tmp/p.png"
    assert r.pieces.kw["size"] == (BOARD_SIZE, BOARD_SIZE)
    np.testing.assert_allclose(r.arrow.vertices[3],
                               np.array(square_center(28, flip=True)) + style.BOARD_ORIGIN)
    assert r.arrow.drawn == 1
    assert [p.fillColor for p in r.panels] == [(1.0, 1.0, 1.0), (-1.0, -1.0, -1.0)]
    assert [lb.text for lb in r.labels] == ["Q", "N"] and r.caption.text == "pick"
    assert r.caption.drawn == 1
    r.draw(snap, sql, pnl)                       # same pieces version: no reload
    assert r.pieces.loads == 1
    r.draw(BoardSnapshot(pieces=Path("/tmp/p.png"), pieces_version=2), sql, pnl)
    assert r.pieces.loads == 2 and r.arrow.drawn == 2 and r.caption.drawn == 2
    assert all(p.drawn == 2 for p in r.panels)   # hidden panels and caption not drawn
    assert not r.escape_pressed() and r.escape_pressed()
    r.close()
    assert r.win.closed


def test_board_renderer_fps_readout(monkeypatch):
    fake = types.ModuleType("psychopy")
    fake.visual, fake.event = _fake_visual(), types.SimpleNamespace(getKeys=lambda keyList: [])
    monkeypatch.setitem(sys.modules, "psychopy", fake)
    r = bs.BoardRenderer(load_config("chess"))
    x, y = r.fps_text.kw["pos"]
    assert x == pytest.approx(-1512 / 982 / 2 + 0.012) and y > 0.48       # top-left corner
    assert r.fps_text.kw["text"] == "-- fps"
    for _ in range(90):                          # 1.5 s at 60 Hz
        r.flip()
    assert r.fps_text.text == "60.0 fps \u00b7 0 dropped" and r.dropped == 0
    r.win.t += 2 / 60                            # one late flip, two frames long
    r.flip()
    assert r.dropped == 1                        # counted like the timing summary: per flip
    for _ in range(40):
        r.flip()
    assert r.fps_text.text.endswith("\u00b7 1 dropped") and float(r.fps_text.text.split()[0]) < 60
    r.draw(BoardSnapshot(), np.full(64, 0.3), np.zeros(2))
    assert r.fps_text.drawn == 1


def test_board_renderer_retries_then_gives_up_on_a_bad_image(monkeypatch):
    visual = _fake_visual()
    fake = types.ModuleType("psychopy")
    fake.visual, fake.event = visual, types.SimpleNamespace(getKeys=lambda keyList: [])
    monkeypatch.setitem(sys.modules, "psychopy", fake)
    notes: list[str] = []
    r = bs.BoardRenderer(load_config("chess"), note=notes.append)
    sql, pnl = np.full(64, 0.3), np.zeros(2)
    snap = BoardSnapshot(pieces=Path("/tmp/half.png"), pieces_version=1)
    _FakeImage.fail = 2                          # half-written file: loads on the third try
    for _ in range(3):
        r.draw(snap, sql, pnl)
    assert r.pieces is not None and notes == []
    _FakeImage.fail = 0
    monkeypatch.setattr(_FakeImage, "__setattr__", _raise_on_image)
    snap2 = BoardSnapshot(pieces=Path("/tmp/bad.png"), pieces_version=2)
    for _ in range(12):
        r.draw(snap2, sql, pnl)
    assert len(notes) == 1 and "could not load pieces image /tmp/bad.png" in notes[0]
    assert r.pieces.drawn == 1 + 12             # the old image stays up


def _raise_on_image(self, name, value):
    if name == "image":
        raise OSError("broken")
    object.__setattr__(self, name, value)
