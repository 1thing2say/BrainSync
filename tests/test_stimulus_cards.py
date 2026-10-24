"""BrainSwipe stimulus tests: card and flash commands, frames, [card] config, CLI.

Nothing here opens a window or imports psychopy.
"""
from __future__ import annotations

import io
import struct
import sys
import threading
import types
import zlib
from pathlib import Path

import numpy as np
import pytest

from brainsync.stimulus import __main__ as cli
from brainsync.stimulus.config import (CARD_ASPECT, Card, card_overlap, config_warnings,
                                       load_config, override, parse_config)
from brainsync.stimulus.display import (MarkerWriter, card_paths, free_frames, play,
                                        png_size)
from brainsync.stimulus.feedback import Command, FeedbackReader, parse_command, parse_line
from brainsync.stimulus.flicker import frame_luminances, n_frames


def _cfg(**over):
    data = {
        "name": "t",
        "targets": {"freqs": [15.0, 17.0]},
        "layout": {"rows": 1, "cols": 2, "size": 0.4, "spacing": 1.0},
        "display": {"refresh_hz": 60.0},
        "session": {"blocks": 2, "cue_s": 0.05, "stim_s": 0.1, "rest_s": 0.05},
    }
    for key, val in over.items():
        data.setdefault(key, {}).update(val) if isinstance(val, dict) else data.update({key: val})
    return parse_config(data)


def _png(path: Path, width: int = 6, height: int = 9) -> Path:
    """A tiny valid RGB PNG, written without Pillow."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))
    rows = b"".join(b"\x00" + b"\x40\x40\x40" * width for _ in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


@pytest.fixture
def card_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cards"
    d.mkdir()
    for i in (2, 0, 1):   # written out of order: the index follows the sorted names
        _png(d / f"card_{i:03d}.png")
    (d / "notes.txt").write_text("not a card")
    return d


# -- command parsing ------------------------------------------------------------------------

def test_parse_command_kinds():
    assert parse_command("1\n", 2, 0) == Command("cue", 1)
    assert parse_command(" -1 ", 2, 0) == Command("cue", -1)
    assert parse_command("card 3\n", 2, 5) == Command("card", 3)
    assert parse_command("  card   0 ", 2, 1) == Command("card", 0)
    assert parse_command("flash 1", 2, 0) == Command("flash", 1)
    assert parse_command("flash 0", 2, 0) == Command("flash", 0)


@pytest.mark.parametrize("line, n_cards", [
    ("", 3), ("x", 3), ("2", 3), ("-2", 3), ("1.5", 3),
    ("card", 3), ("card 3", 3), ("card -1", 3), ("card x", 3), ("card 0", 0),
    ("card 1 2", 3), ("flash", 3), ("flash 2", 3), ("flash -1", 3), ("flash a", 3),
    ("swipe 1", 3), ("CARD 1", 3),
])
def test_parse_command_refuses(line, n_cards):
    with pytest.raises(ValueError):
        parse_command(line, 2, n_cards)


def test_parse_line_unchanged_for_integer_callers():
    assert parse_line("1\n", 2) == 1 and parse_line("-1", 2) == -1
    with pytest.raises(ValueError):
        parse_line("card 1", 2)
    with pytest.raises(ValueError):
        parse_line("flash 0", 2)


def test_reader_card_and_flash_do_not_set_heard_or_cue():
    notes: list[str] = []
    r = FeedbackReader(io.StringIO("card 2\nflash 1\nflash 0\ncard 9\nflash 5\nbad\n"), 2,
                       note=notes.append, n_cards=3)
    assert r.card == 0 and r.flash == (-1, 0)
    # run() reads to EOF, which itself counts as heard; check heard on the event instead
    r.run()
    assert not r._heard.is_set() and r.current == -1
    assert r.card == 2 and r.flash == (0, 2)
    assert r.n_lines == 6 and r.n_ignored == 3 and len(notes) == 3
    assert "card 9" in notes[0] and "flash 5" in notes[1] and "bad" in notes[2]


def test_reader_integer_lines_still_set_heard_and_current():
    r = FeedbackReader(io.StringIO("card 1\n1\nflash 1\n-1\ncard 0\n"), 2,
                       note=lambda s: None, n_cards=2)
    r.run()
    assert r._heard.is_set() and r.current == -1
    assert r.card == 0 and r.flash == (1, 1) and r.n_ignored == 0


def test_reader_without_cards():
    notes: list[str] = []
    r = FeedbackReader(io.StringIO("card 0\nflash 1\n0\n"), 2, note=notes.append)
    assert r.card == -1
    r.run()
    assert r.card == -1 and r.flash == (1, 1) and r.current == 0
    assert r.n_ignored == 1 and "card 0" in notes[0]
    with pytest.raises(ValueError, match="n_cards"):
        FeedbackReader(io.StringIO(""), 2, n_cards=-1)


def test_reader_flash_serial_counts_repeats():
    r = FeedbackReader(io.StringIO("flash 1\nflash 1\nflash 1\n"), 2, note=lambda s: None)
    r.run()
    assert r.flash == (1, 3)


# -- frames ---------------------------------------------------------------------------------

def test_free_frames_carry_card_through_hold_and_flicker():
    cfg = _cfg()
    held = iter([True] * 4 + [False] * 100)
    cards = iter([0, 0, 1, 1, 1, 2, 2, 3, 3, 3])
    frames = list(free_frames(cfg, seconds=0.1, cue=lambda: 1, hold=lambda: next(held),
                              card=lambda: next(cards)))
    assert len(frames) == 4 + 6
    assert [f.card for f in frames] == [0, 0, 1, 1, 1, 2, 2, 3, 3, 3]
    assert [f.cue for f in frames] == [-1] * 4 + [1] * 6
    assert all(f.flash == -1 for f in frames)
    assert all(f.card == -1 for f in free_frames(cfg, seconds=0.05))


def test_flash_lasts_flash_s_and_restarts_on_new_serial():
    cfg = _cfg(card={"flash_s": 0.35})
    n = n_frames(0.35, 60.0)
    assert n == 21
    # serial 1 on target 1 from frame 3; serial 2 on target 0 from frame 10
    polls = [(-1, 0)] * 3 + [(1, 1)] * 7 + [(0, 2)] * 40
    it = iter(polls)
    frames = list(free_frames(cfg, seconds=len(polls) / 60.0, flash=lambda: next(it)))
    got = [f.flash for f in frames]
    assert got == [-1] * 3 + [1] * 7 + [0] * n + [-1] * (len(polls) - 10 - n)
    # an unchanged serial never restarts the flash
    it2 = iter([(1, 1)] * 40)
    got2 = [f.flash for f in free_frames(cfg, seconds=40 / 60.0, flash=lambda: next(it2))]
    assert got2 == [1] * n + [-1] * (40 - n)


def test_flash_shows_during_hold_and_zero_length_disables():
    cfg = _cfg(card={"flash_s": 0.05})
    held = iter([True] * 5 + [False] * 10)
    frames = list(free_frames(cfg, seconds=0.05, hold=lambda: next(held),
                              flash=lambda: (0, 1)))
    assert [f.flash for f in frames] == [0, 0, 0, -1, -1, -1, -1, -1]
    off = _cfg(card={"flash_s": 0.0})
    assert all(f.flash == -1 for f in free_frames(off, seconds=0.1, flash=lambda: (0, 1)))


def test_cards_and_flashes_never_change_luminance():
    """The key safety property: the flicker is the same with or without the feed."""
    cfg = _cfg(card={"flash_s": 0.1})
    rng = np.random.default_rng(0)
    plain = list(free_frames(cfg, seconds=3.0, cue=lambda: 0))
    serials = np.cumsum(rng.random(180) < 0.1)
    targets = rng.integers(0, 2, 180)
    i = iter(range(180))
    j = iter(range(180))

    def flash():
        k = next(i)
        return int(targets[k]), int(serials[k])

    busy = list(free_frames(cfg, seconds=3.0, cue=lambda: 0,
                            card=lambda: next(j) // 7, flash=flash))
    assert len(busy) == len(plain) == 180
    assert any(f.flash >= 0 for f in busy) and len({f.card for f in busy}) > 10
    for a, b in zip(plain, busy):
        np.testing.assert_array_equal(a.lum, b.lum)
        assert a.cue == b.cue
    # the same holds through a hold-still phase
    h1, h2 = iter([True] * 30 + [False] * 300), iter([True] * 30 + [False] * 300)
    still = list(free_frames(cfg, seconds=1.0, hold=lambda: next(h1)))
    still_busy = list(free_frames(cfg, seconds=1.0, hold=lambda: next(h2),
                                  card=lambda: 4, flash=lambda: (1, 1)))
    assert [f.lum.tolist() for f in still] == [f.lum.tolist() for f in still_busy]


class _Recorder:
    """Renderer that takes the card and flash keywords."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.t = 0.0

    def draw(self, lum, cue, card=-1, flash=-1):
        self.calls.append((np.array(lum), cue, card, flash))

    def flip(self):
        self.t += 1 / 60.0
        return self.t

    def escape_pressed(self):
        return False


class _OldRenderer(_Recorder):
    """A renderer written before cards existed: draw(lum, cue) only."""

    def draw(self, lum, cue):
        self.calls.append((np.array(lum), cue))


def test_play_passes_card_and_flash_only_when_set():
    cfg = _cfg(card={"flash_s": 0.05})
    flashes = iter([(1, 1)] + [(1, 1)] * 20)
    ren = _Recorder()
    play(free_frames(cfg, seconds=0.1, card=lambda: 2, flash=lambda: next(flashes)), ren,
         MarkerWriter("none"), 60.0)
    assert [c[2] for c in ren.calls] == [2] * 6
    assert [c[3] for c in ren.calls] == [1, 1, 1, -1, -1, -1]
    old = _OldRenderer()
    timing = play(free_frames(cfg, seconds=0.1), old, MarkerWriter("none"), 60.0)
    assert timing.n_frames == 6 and all(len(c) == 2 for c in old.calls)


# -- config ---------------------------------------------------------------------------------

def test_swipe_config():
    cfg = load_config("swipe")
    two = load_config("two_targets")
    assert cfg.labels == ("Pass", "Like")
    np.testing.assert_array_equal(cfg.freqs, [15.0, 17.0])
    np.testing.assert_array_equal(cfg.phases, two.phases)
    assert cfg.waveform == "sine" and cfg.hold_cue
    assert (cfg.size, cfg.spacing, cfg.refresh, cfg.display.aspect) == (0.4, 1.0, 60.0, 1.5)
    assert [t.pos for t in cfg.targets] == [t.pos for t in two.targets]
    assert cfg.card is not None
    assert cfg.card.height == pytest.approx(0.6) and cfg.card.flash_s == pytest.approx(0.35)
    (pr, pg, pb), (lr, lg, lb) = cfg.card.flash_colors
    assert pr > pg and pr > pb          # Pass is reddish
    assert lg > lr and lg > lb          # Like is greenish
    assert all(-1.0 <= v <= 1.0 for c in cfg.card.flash_colors for v in c)
    assert card_overlap(cfg) is None
    assert not any("card" in w for w in config_warnings(cfg))
    assert cfg.flash_color(0) == cfg.card.flash_colors[0]


def test_configs_without_card_table_keep_working():
    two = load_config("two_targets")
    assert two.card is None
    assert two.card_spec == Card()
    assert two.flash_color(1) == two.cue_color      # no colours given: the cue colour
    assert config_warnings(two) == config_warnings(_cfg(targets={"freqs": [15.0, 17.0]}))
    assert override(load_config("swipe"), blocks=3).card == load_config("swipe").card


def test_card_table_parsing_and_defaults():
    cfg = _cfg(card={})
    assert cfg.card == Card(height=0.6, flash_s=0.35, flash_colors=())
    hexed = _cfg(card={"flash_colors": ["#ff0000", "#00FF80"], "height": 0.5})
    assert hexed.card.flash_colors == ((1.0, -1.0, -1.0), (-1.0, 1.0, pytest.approx(0.00392,
                                                                                    abs=1e-4)))
    listed = _cfg(card={"flash_colors": [[0.5, -0.5, -0.5], "#000000"]})
    assert listed.card.flash_colors == ((0.5, -0.5, -0.5), (-1.0, -1.0, -1.0))


@pytest.mark.parametrize("table, match", [
    ({"flash_colors": ["#ff0000"]}, "1 flash_colors for 2 targets"),
    ({"flash_colors": ["#ff0000", "red"]}, "red"),
    ({"flash_colors": ["#ff0000", "#12345"]}, "12345"),
    ({"flash_colors": ["#ff0000", [0.0, 2.0, 0.0]]}, r"\[-1, 1\]"),
    ({"flash_colors": ["#ff0000", [0.0, 0.0]]}, "three"),
    ({"flash_colors": "#ff0000"}, "list"),
    ({"height": 0.0}, "height must be in"),
    ({"height": 1.2}, "height must be in"),
    ({"height": "tall"}, "number"),
    ({"flash_s": -0.1}, "flash_s must be finite and >= 0"),
    # TOML accepts inf and nan; either would crash n_frames once the window is up
    ({"flash_s": float("inf")}, "flash_s must be finite and >= 0"),
    ({"flash_s": float("nan")}, "flash_s must be finite and >= 0"),
    ({"height": float("nan")}, "height must be in"),
    ({"height": float("inf")}, "height must be in"),
    ({"width": 0.4}, "unknown key"),
])
def test_card_table_refuses(table, match):
    with pytest.raises(ValueError, match=match):
        _cfg(card=table)


def test_card_overlap_warnings():
    # 0.6 high at 2:3 is 0.4 wide: |x| < 0.2, clear of the cue boxes' inner edge at 0.26
    assert CARD_ASPECT == pytest.approx(2 / 3)
    assert card_overlap(_cfg(card={"height": 0.6})) is None
    # 0.8 high is 0.533 wide: |x| < 0.267 crosses the cue boxes at 0.26, not the squares
    box = card_overlap(_cfg(card={"height": 0.8}))
    assert box is not None and "cue box" in box and "0.267" in box
    assert any("cue box" in w for w in config_warnings(_cfg(card={"height": 0.8})))
    # 1.0 high is 0.667 wide: |x| < 0.333 crosses the squares' inner edges at 0.3
    sq = card_overlap(_cfg(card={"height": 1.0}))
    assert sq is not None and "square" in sq
    # a wider image overlaps sooner; the aspect can be the real image's
    assert card_overlap(_cfg(card={"height": 0.6}), aspect=1.0) is not None
    # a single centred target always sits under the card
    one = load_config("one_target_15")
    assert card_overlap(one) is not None
    assert not any("card" in w for w in config_warnings(one))   # no [card]: no warning


# -- card files -----------------------------------------------------------------------------

def test_card_paths_sorted_and_png_size(card_dir: Path, tmp_path: Path):
    paths = card_paths(card_dir)
    assert [p.name for p in paths] == ["card_000.png", "card_001.png", "card_002.png"]
    assert png_size(paths[0]) == (6, 9)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no .png"):
        card_paths(empty)
    with pytest.raises(ValueError, match="not a directory"):
        card_paths(tmp_path / "missing")
    with pytest.raises(ValueError, match="not a PNG"):
        png_size(card_dir / "notes.txt")


# -- CLI ------------------------------------------------------------------------------------

def test_cli_cards_needs_free(card_dir: Path):
    with pytest.raises(SystemExit):
        cli.main(["--plan", "--cards", str(card_dir)])


def test_cli_cards_empty_dir(tmp_path: Path, capsys):
    assert cli.main(["--plan", "--free", "--cards", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err
    assert cli.main(["--plan", "--free", "--cards", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_plan_mentions_cards(card_dir: Path, capsys):
    assert cli.main(["--plan", "-c", "swipe", "--free", "--cards", str(card_dir)]) == 0
    out, err = capsys.readouterr()
    assert "label 'Pass'" in out and "label 'Like'" in out
    assert "3 card(s)" in out and str(card_dir) in out and "0.6" in out
    assert "free run" in out and "overlap" not in err
    assert cli.main(["--plan", "-c", "swipe", "--free"]) == 0
    assert "card(s)" not in capsys.readouterr().out


def test_cli_cards_warn_on_overlap_without_card_table(card_dir: Path, capsys):
    # one_target_15 has no [card]; --cards puts the default card over its centred square
    assert cli.main(["--plan", "-c", "one_target_15", "--free", "--cards",
                     str(card_dir)]) == 0
    assert "warning: card" in capsys.readouterr().err


class _CardWindow:
    """Stands in for PsychoPyRenderer and records card and flash per frame."""

    instances: list[_CardWindow] = []

    def __init__(self, cfg, cards=()):
        self.cfg = cfg
        self.cards = list(cards)
        self.draws: list[tuple] = []
        self.t = 0.0
        self.closed = False
        _CardWindow.instances.append(self)

    def measure_refresh(self):
        return 60.0

    def wait_for_start(self, still):
        raise AssertionError("--no-wait should skip the start screen")

    def draw(self, lum, cue, card=-1, flash=-1):
        self.draws.append((np.array(lum), cue, card, flash))

    def flip(self):
        self.t += 1 / 60.0
        return self.t

    def escape_pressed(self):
        return False

    def close(self):
        self.closed = True


@pytest.fixture
def card_window(monkeypatch):
    import brainsync.stimulus.display as disp
    _CardWindow.instances = []
    monkeypatch.setattr(disp, "PsychoPyRenderer", _CardWindow)
    return _CardWindow


def test_main_free_run_with_cards(card_window, card_dir: Path, capsys):
    assert cli.main(["-c", "swipe", "--free", "--seconds", "0.2", "--no-wait",
                     "--markers", "none", "--cards", str(card_dir)]) == 0
    out, err = capsys.readouterr()
    win = card_window.instances[0]
    assert [p.name for p in win.cards] == ["card_000.png", "card_001.png", "card_002.png"]
    assert "loaded 3 card(s)" in err and "12 frames" in out
    assert {d[2] for d in win.draws} == {0} and {d[3] for d in win.draws} == {-1}
    cfg = load_config("swipe")
    for i, d in enumerate(win.draws):
        np.testing.assert_array_equal(d[0], frame_luminances(cfg.freqs, cfg.phases, i, 60.0))


class _LockstepStdin:
    """Stdin stand-in that hands the reader thread a line only when the display sends it.

    Read call c returns line c; send() returns once the reader has asked for the next
    line, i.e. has applied the one just sent, so the frame drawn next must show it.
    """

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.lines: list[str] = []
        self.calls = 0

    def readline(self) -> str:
        with self.cond:
            self.calls += 1
            self.cond.notify_all()
            # a hung display gives EOF instead of a hung test
            self.cond.wait_for(lambda: len(self.lines) >= self.calls, timeout=10)
            return self.lines[self.calls - 1] if len(self.lines) >= self.calls else ""

    def send(self, line: str) -> None:
        with self.cond:
            self.lines.append(line)
            self.cond.notify_all()
            n = len(self.lines)
            if line:   # after EOF the reader never asks again
                assert self.cond.wait_for(lambda: self.calls > n, timeout=5), \
                    f"reader never applied {line!r}"


class _LockstepWindow(_CardWindow):
    """_CardWindow that sends ``schedule[n]`` on stdin once ``n`` frames are drawn."""

    stdin: _LockstepStdin
    schedule: dict[int, str] = {}

    def flip(self):
        line = self.schedule.get(len(self.draws))
        if line is not None:
            self.stdin.send(line)
        return super().flip()


def test_main_free_cards_from_stdin(card_dir: Path, monkeypatch, capsys):
    import brainsync.stimulus.display as disp
    stdin = _LockstepStdin()
    _CardWindow.instances = []
    _LockstepWindow.stdin = stdin
    _LockstepWindow.schedule = {10: "card 2\n", 20: "flash 1\n", 60: "card 7\n",
                                70: "1\n", 120: ""}
    monkeypatch.setattr(disp, "PsychoPyRenderer", _LockstepWindow)
    monkeypatch.setattr(sys, "stdin", stdin)
    assert cli.main(["-c", "swipe", "--free", "--feedback", "stdin", "--hold-still",
                     "--no-wait", "--markers", "none", "--cards", str(card_dir)]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("timing: ") and "ignored line 3" in err
    win = _CardWindow.instances[0]
    assert win.closed and len(win.draws) >= 120
    cards = [d[2] for d in win.draws]
    flashes = [d[3] for d in win.draws]
    cues = [d[1] for d in win.draws]
    # card 0 from the first frame, card 2 from its line on; card 7 (of 3) never shows
    assert cards[:10] == [0] * 10 and set(cards[10:]) == {2}
    # the flash lights target 1 for flash_s, still screen or not
    cfg = load_config("swipe")
    n_flash = n_frames(cfg.card.flash_s, 60.0)
    assert flashes[:20] == [-1] * 20 and flashes[20:20 + n_flash] == [1] * n_flash
    assert set(flashes[20 + n_flash:]) == {-1}
    # card and flash lines do not end --hold-still; the cue line "1" does
    still = frame_luminances(cfg.freqs, cfg.phases, 0, 60.0)
    for d in win.draws[:70]:
        np.testing.assert_array_equal(d[0], still)
    assert set(cues[:70]) == {-1} and set(cues[70:]) == {1}
    for i, d in enumerate(win.draws[70:]):
        np.testing.assert_array_equal(d[0], frame_luminances(cfg.freqs, cfg.phases, i, 60.0))


# -- PsychoPy renderer against a fake psychopy -------------------------------------------------

class _FakeStim:
    """Any psychopy stimulus; draw() appends it to its window's draw order."""

    def __init__(self, win, **kw):
        self.win = win
        self.kw = kw
        self.fillColor = kw.get("fillColor")
        self.drawn = 0

    def draw(self):
        self.drawn += 1
        self.win.order.append(self)


class _FakePsychoWindow:
    size = (3024, 1964)

    def __init__(self, **kw):
        self.kw = kw
        self.order: list[_FakeStim] = []
        self.t = 0.0

    def setMouseVisible(self, visible):
        pass

    def flip(self):
        self.t += 1 / 60.0
        return self.t

    def close(self):
        pass


def _fake_psychopy(monkeypatch):
    visual = types.SimpleNamespace(Window=_FakePsychoWindow, Rect=_FakeStim,
                                   TextStim=_FakeStim, ImageStim=_FakeStim)
    event = types.SimpleNamespace(getKeys=lambda keyList: [], clearEvents=lambda: None)
    fake = types.ModuleType("psychopy")
    fake.visual, fake.event = visual, event
    monkeypatch.setitem(sys.modules, "psychopy", fake)


def test_psychopy_renderer_cards_and_flash(monkeypatch, tmp_path: Path, capsys):
    from brainsync.stimulus.display import PsychoPyRenderer
    _fake_psychopy(monkeypatch)
    paths = [_png(tmp_path / "card_000.png", 600, 900), _png(tmp_path / "card_001.png", 90, 60)]
    cfg = load_config("swipe")
    r = PsychoPyRenderer(cfg, cards=paths)
    # textures are created once, at init, sized from each image's own aspect
    assert len(r.cards) == 2
    assert r.cards[0].kw["image"] == str(paths[0]) and r.cards[0].kw["pos"] == (0.0, 0.0)
    assert r.cards[0].kw["size"] == pytest.approx((0.4, 0.6))
    assert r.cards[1].kw["size"] == pytest.approx((0.9, 0.6))
    # the landscape card crosses the squares, so the renderer warns
    assert "card_001.png" in capsys.readouterr().err
    lum = np.array([0.25, 0.75])
    r.win.order.clear()
    r.draw(lum, 0, card=1, flash=1)
    assert r.squares[0].fillColor == pytest.approx((-0.5, -0.5, -0.5))
    assert r.squares[1].fillColor == pytest.approx((0.5, 0.5, 0.5))
    # card first, then squares, labels, cue box, flash box
    assert r.win.order == [r.cards[1], r.squares[0], r.squares[1], *r.labels, r.boxes[0],
                           r.flashes[1]]
    assert r.flashes[1].kw["lineColor"] == cfg.card.flash_colors[1]
    assert r.flashes[1].kw["lineWidth"] > r.boxes[1].kw["lineWidth"]
    assert r.flashes[1].kw["fillColor"] is None
    assert r.flashes[1].kw["size"] == r.boxes[1].kw["size"]
    # old two-argument call still works and draws no card or flash
    r.win.order.clear()
    r.draw(lum, -1)
    assert r.win.order == r.squares + r.labels
    # out-of-range card and flash indices draw nothing extra
    r.win.order.clear()
    r.draw(lum, -1, card=5, flash=3)
    assert r.win.order == r.squares + r.labels


def test_psychopy_renderer_labels_name_each_square(monkeypatch, tmp_path: Path):
    from brainsync.stimulus.display import LABEL_HEIGHT, PsychoPyRenderer, label_pos
    _fake_psychopy(monkeypatch)
    cfg = load_config("swipe")
    r = PsychoPyRenderer(cfg, cards=[_png(tmp_path / "card_000.png", 600, 900)])
    assert [lab.kw["text"] for lab in r.labels] == ["Pass", "Like"]
    for k, lab in enumerate(r.labels):
        x, y = lab.kw["pos"]
        tx, ty = cfg.targets[k].pos
        assert lab.kw["color"] == cfg.card.flash_colors[k]
        # centred under its own square, wholly below the cue box and still on screen
        assert x == pytest.approx(tx)
        assert y + LABEL_HEIGHT / 2 < ty - cfg.reach
        assert y - LABEL_HEIGHT / 2 > -0.5
        assert label_pos(cfg, k) == lab.kw["pos"]
    # the left label sits under the left square, which is Pass at 15 Hz
    assert r.labels[0].kw["pos"][0] < 0 and cfg.targets[0].freq == 15.0


def test_psychopy_renderer_without_cards(monkeypatch, capsys):
    from brainsync.stimulus.display import PsychoPyRenderer
    _fake_psychopy(monkeypatch)
    cfg = load_config("two_targets")
    r = PsychoPyRenderer(cfg)
    # no cards: the plain demo draws no labels, exactly as before
    assert r.cards == [] and r.labels == [] and capsys.readouterr().err == ""
    assert r.flashes[0].kw["lineColor"] == cfg.cue_color
    r.draw(np.zeros(2), -1, card=0, flash=0)
    assert r.flashes[0].drawn == 1
