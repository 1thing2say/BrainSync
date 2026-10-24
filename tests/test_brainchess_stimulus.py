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
from brainsync.brainchess.render import BOARD_SIZE, square_center
from brainsync.brainchess.stimulus import (BoardSnapshot, BoardState, Command, CommandReader,
                                           arrow_vertices, check_layout, frame_values,
                                           parse_command, square_colors, square_luminances)
from brainsync.stimulus.config import available_configs, load_config, override
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


def test_square_colors_grey_and_amber():
    groups = "s" + "0" + "." * 62
    lums = square_luminances(groups, 1.0, 0.0)
    rgb = square_colors(groups, lums)
    assert rgb.shape == (64, 3)
    np.testing.assert_allclose(rgb[1], [1.0, 1.0, 1.0])
    np.testing.assert_allclose(rgb[2], [2 * 0.22 - 1] * 3)    # c1 is dark
    r, g, b = rgb[0]
    assert r > g > b and r == pytest.approx(0.8)            # amber, not grey
    assert np.all(rgb >= -1) and np.all(rgb <= 1)
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


def test_frame_values_follow_the_config_depth():
    cfg = override(load_config("chess"), depth=0.5)
    running = BoardSnapshot(groups=G0, holding=False)
    for i in (0, 1, 7, 123):
        sq, pn = frame_values(running, i, cfg)
        lum = frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform, 0.5)
        np.testing.assert_array_equal(pn, lum)
        np.testing.assert_array_equal(sq[:32], lum[0])
        assert np.all((pn >= 0.25) & (pn <= 0.75))
    assert "  sine flicker, depth 0.5, refresh 60 Hz" in "\n".join(bs.plan_lines(cfg, False))


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

def test_chess_config_panels_beside_the_board():
    assert "chess" in available_configs()
    cfg = load_config("chess")
    assert cfg.name == "chess" and cfg.n_targets == 2
    np.testing.assert_array_equal(cfg.freqs, [15.0, 17.0])
    np.testing.assert_array_equal(cfg.phases, [0.0, 0.0])
    assert cfg.labels == ("PLAY", "BACK") and cfg.waveform == "sine" and not cfg.hold_cue
    assert cfg.refresh == 60.0 and cfg.display.aspect == 1.5 and cfg.display.background == 0
    (x0, y0), (x1, y1) = (t.pos for t in cfg.targets)
    assert x0 < 0 < x1 and x0 == -x1 and y0 == y1 == 0.0
    for t in cfg.targets:
        assert abs(t.pos[0]) - cfg.size / 2 > BOARD_SIZE / 2
        assert abs(t.pos[0]) + cfg.reach <= cfg.display.aspect / 2
    check_layout(cfg)
    with pytest.raises(ValueError, match="inside the board"):
        check_layout(load_config("two_targets"))
    with pytest.raises(ValueError, match="two-target"):
        check_layout(load_config("one_target_15"))


def test_plan_via_main(capsys):
    assert bs.main(["--plan"]) == 0
    out, err = capsys.readouterr()
    assert "config chess" in out
    assert "board 0.9 screen heights, square 0.1125, white at the bottom" in out
    assert "15.000 Hz" in out and "17.000 Hz" in out
    assert "4.000 frames/cycle" in out and "3.529 frames/cycle" in out
    assert "panel centre (-0.590, +0.000)" in out and "panel centre (+0.590, +0.000)" in out
    assert "'PLAY'" in out and "'BACK'" in out
    assert "clear the board edge by 0.020" in out
    assert "sine flicker, depth 1, refresh 60 Hz" in out
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

    def draw(self, snap, square_lums, panel_lums):
        self.frames.append((snap, square_lums, panel_lums))

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

    def draw(self, snap, sq, pn):
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

    def close(self):
        self.closed = True


def test_board_renderer_against_fake_psychopy(monkeypatch):
    keys: list[list[str]] = [[], ["escape"]]
    visual = types.SimpleNamespace(Window=_FakePsychoWindow, ElementArrayStim=_FakeStim,
                                   ImageStim=_FakeImage, ShapeStim=_FakeStim, Rect=_FakeStim,
                                   TextStim=_FakeStim)
    event = types.SimpleNamespace(getKeys=lambda keyList: keys.pop(0) if keys else [])
    fake = types.ModuleType("psychopy")
    fake.visual, fake.event = visual, event
    monkeypatch.setitem(sys.modules, "psychopy", fake)
    notes: list[str] = []
    cfg = load_config("chess")
    r = bs.BoardRenderer(cfg, flip=True, note=notes.append)
    assert r.win.kw["fullscr"] is True and r.win.kw["units"] == "height"
    assert r.win.mouse is False and notes == []
    sq = r.squares.kw
    assert sq["nElements"] == 64 and sq["elementTex"] is None and sq["elementMask"] is None
    assert sq["sizes"] == pytest.approx(0.1125)
    np.testing.assert_allclose(sq["xys"][0], square_center(0, flip=True))
    assert [p.kw["pos"] for p in r.panels] == [t.pos for t in cfg.targets]
    assert r.measure_refresh() == pytest.approx(60.0)

    snap = BoardSnapshot(groups="s" + G0[1:], pieces=Path("/tmp/p.png"), pieces_version=1,
                         arrow=(12, 28), panels=("Q", "N"), text="pick", holding=False)
    sql, pnl = frame_values(snap, 0, cfg)
    pnl = np.array([1.0, 0.0])
    r.draw(snap, sql, pnl)
    assert r.squares.drawn == 1 and r.squares.colors.shape == (64, 3)
    assert r.pieces.drawn == 1 and r.pieces.kw["image"] == "/tmp/p.png"
    assert r.pieces.kw["size"] == (BOARD_SIZE, BOARD_SIZE)
    np.testing.assert_allclose(r.arrow.vertices[3], square_center(28, flip=True))
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


def test_board_renderer_retries_then_gives_up_on_a_bad_image(monkeypatch):
    visual = types.SimpleNamespace(Window=_FakePsychoWindow, ElementArrayStim=_FakeStim,
                                   ImageStim=_FakeImage, ShapeStim=_FakeStim, Rect=_FakeStim,
                                   TextStim=_FakeStim)
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
