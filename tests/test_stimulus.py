"""Stimulus package tests; nothing here opens a window or imports psychopy."""
from __future__ import annotations

import io
import math
import re
import sys
from pathlib import Path

import numpy as np
import pytest

from brainsync.recordings import parse_markers
from brainsync.stimulus import __main__ as cli
from brainsync.stimulus.config import (available_configs, config_warnings, grid_positions,
                                       load_config, make_trials, override, parse_config,
                                       resolve_config, same_phase, session_trials, validate)
from brainsync.stimulus.display import (FREE_ON, Frame, MarkerWriter, SessionTiming, cued_frames,
                                        format_marker, free_frames, free_onset, layout_overflow,
                                        median_rate, play, refresh_mismatch)
from brainsync.stimulus.feedback import FeedbackReader, parse_command, parse_line
from brainsync.stimulus.flicker import (dropped_frames, frame_luminances, frames_per_cycle,
                                        luminance, n_frames, square_duty_even, to_psychopy_rgb)


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


# -- luminance ------------------------------------------------------------------------------

def test_sine_exact_frames_15hz_at_60():
    lum = luminance(15.0, 0.0, np.arange(8), 60.0)
    np.testing.assert_allclose(lum, [0.5, 1.0, 0.5, 0.0, 0.5, 1.0, 0.5, 0.0], atol=1e-12)


def test_sine_matches_formula_and_phase():
    n = np.arange(200)
    for f, ph in [(17.0, 0.0), (12.0, math.pi / 2), (15.0, math.pi)]:
        expect = 0.5 * (1 + np.sin(2 * np.pi * f * n / 60.0 + ph))
        np.testing.assert_allclose(luminance(f, ph, n, 60.0), expect, atol=1e-12)
    assert luminance(12.0, math.pi / 2, 0, 60.0) == pytest.approx(1.0)
    assert isinstance(luminance(12.0, 0.0, 3, 60.0), float)


def test_sine_period_and_range():
    # 17 Hz at 60 Hz repeats exactly every 60 frames (17 cycles), not before at an integer
    n = np.arange(600)
    lum = luminance(17.0, 0.3, n, 60.0)
    np.testing.assert_allclose(lum[60:], lum[:-60], atol=1e-9)
    assert not np.allclose(lum[4:], lum[:-4])
    assert lum.min() >= 0.0 and lum.max() <= 1.0
    assert frames_per_cycle(15.0, 60.0) == 4.0
    # spectrum peaks at the flicker frequency
    spec = np.abs(np.fft.rfft(lum - lum.mean()))
    freqs = np.fft.rfftfreq(n.size, 1 / 60.0)
    assert freqs[np.argmax(spec)] == pytest.approx(17.0)


def test_square_wave_frames_and_duty():
    np.testing.assert_array_equal(luminance(15.0, 0.0, np.arange(8), 60.0, "square"),
                                  [1, 1, 0, 0, 1, 1, 0, 0])
    np.testing.assert_array_equal(luminance(10.0, 0.0, np.arange(6), 60.0, "square"),
                                  [1, 1, 1, 0, 0, 0])
    np.testing.assert_array_equal(luminance(15.0, math.pi, np.arange(4), 60.0, "square"),
                                  [0, 0, 1, 1])
    assert square_duty_even(15.0, 60.0) and square_duty_even(10.0, 60.0)
    assert not square_duty_even(12.0, 60.0)   # 5 frames per cycle
    assert not square_duty_even(17.0, 60.0)


def test_luminance_errors_and_helpers():
    with pytest.raises(ValueError, match="triangle"):
        luminance(10.0, 0.0, 0, 60.0, "triangle")
    with pytest.raises(ValueError, match="-1"):
        luminance(10.0, 0.0, 0, -1)
    lum = frame_luminances(np.array([15.0, 17.0]), np.zeros(2), 1, 60.0)
    assert lum.shape == (2,) and lum[0] == pytest.approx(1.0)
    assert to_psychopy_rgb(0.0) == (-1.0, -1.0, -1.0)
    assert to_psychopy_rgb(1.0) == (1.0, 1.0, 1.0)
    assert n_frames(4.0, 60.0) == 240
    assert dropped_frames([0, 1 / 60, 2 / 60, 4 / 60, 5 / 60], 60.0) == 1
    assert dropped_frames([0.0], 60.0) == 0


@pytest.mark.parametrize("waveform", ["sine", "square"])
def test_full_depth_is_bit_identical_to_the_undamped_flicker(waveform):
    n = np.arange(600)
    for f, ph in [(15.0, 0.0), (17.0, 0.3), (12.0, math.pi / 2)]:
        if waveform == "sine":
            old = 0.5 * (1.0 + np.sin(2.0 * np.pi * f * n / 60.0 + ph))
        else:
            old = (np.mod((f * n) / 60.0 + ph / (2.0 * np.pi), 1.0) < 0.5).astype(float)
        assert np.array_equal(luminance(f, ph, n, 60.0, waveform), old)
        assert np.array_equal(luminance(f, ph, n, 60.0, waveform, depth=1.0), old)
    freqs, phases = np.array([15.0, 17.0]), np.array([0.0, 0.3])
    for i in (0, 1, 59):
        assert np.array_equal(frame_luminances(freqs, phases, i, 60.0, waveform, depth=1.0),
                              frame_luminances(freqs, phases, i, 60.0, waveform))


def test_half_depth_swings_around_mid_grey():
    n = np.arange(600)   # 10 s: whole cycles of 12, 15 and 17 Hz
    for f in (12.0, 15.0, 17.0):
        lum = luminance(f, 0.3, n, 60.0, depth=0.5)
        assert lum.min() >= 0.25 and lum.max() <= 0.75
        assert lum.mean() == pytest.approx(0.5, abs=1e-12)
        np.testing.assert_allclose(lum - 0.5, 0.5 * (luminance(f, 0.3, n, 60.0) - 0.5),
                                   atol=1e-12)
    np.testing.assert_array_equal(luminance(15.0, 0.0, np.arange(8), 60.0, "square", 0.5),
                                  [0.75, 0.75, 0.25, 0.25, 0.75, 0.75, 0.25, 0.25])
    assert set(luminance(12.0, 0.3, n, 60.0, "square", depth=0.5)) == {0.25, 0.75}
    lum = frame_luminances(np.array([15.0, 17.0]), np.zeros(2), 1, 60.0, depth=0.5)
    assert lum[0] == pytest.approx(0.75)
    assert luminance(15.0, 0.0, 3, 60.0, depth=0.5) == pytest.approx(0.25)


@pytest.mark.parametrize("depth", [0.0, -0.1, 1.2, math.nan, math.inf])
def test_bad_depth_is_refused(depth):
    with pytest.raises(ValueError, match=r"depth must be in \(0, 1\]"):
        luminance(15.0, 0.0, 0, 60.0, depth=depth)
    with pytest.raises(ValueError, match=r"depth must be in \(0, 1\]"):
        luminance(15.0, 0.0, 0, 60.0, "square", depth=depth)
    with pytest.raises(ValueError, match=r"depth must be in \(0, 1\]"):
        frame_luminances(np.array([15.0]), np.zeros(1), 0, 60.0, depth=depth)


# -- configs --------------------------------------------------------------------------------

def test_shipped_configs():
    assert {"two_targets", "one_target_12", "one_target_15", "one_target_17"} <= set(
        available_configs())
    two = load_config("two_targets")
    np.testing.assert_array_equal(two.freqs, [15.0, 17.0])
    np.testing.assert_array_equal(two.phases, [0.0, 0.0])
    assert two.waveform == "sine" and two.hold_cue
    assert (two.rows, two.cols, two.size) == (1, 2, 0.4)
    (x0, y0), (x1, y1) = (t.pos for t in two.targets)
    assert x1 - x0 == pytest.approx(1.0) and y0 == y1 == 0.0 and x0 == -x1
    s = two.session
    assert (s.blocks, s.cue_s, s.stim_s, s.rest_s) == (10, 1.0, 4.0, 3.0)
    assert two.refresh == 60.0
    for f in (12, 15, 17):
        one = load_config(f"one_target_{f}.toml")
        assert one.n_targets == 1 and one.freqs[0] == f
        assert one.size == pytest.approx(0.7) and one.waveform == "sine"
        assert not one.hold_cue and one.targets[0].pos == (0.0, 0.0)


def test_four_targets_low_first_config():
    four = load_config("four_targets_low_first")
    assert four.n_targets == 4
    np.testing.assert_array_equal(four.freqs, [11.0, 15.0, 17.0, 13.0])   # row by row
    np.testing.assert_array_equal(four.phases, [0.0, 0.0, 0.0, 0.0])
    assert four.labels == ("11", "15", "17", "13")
    assert four.waveform == "sine" and four.hold_cue
    assert [t.pos for t in four.targets] == pytest.approx([(-0.27, 0.27), (0.27, 0.27),
                                                          (-0.27, -0.27), (0.27, -0.27)])
    assert max(abs(t.pos[1]) for t in four.targets) + four.reach <= 0.5   # fits the height
    # the status line (anchored at its top, below the targets) and the prompt (anchored at
    # its bottom, above them) are placed as PsychoPyRenderer places them; both must stay
    # on screen with two lines (RATE_PROMPT) at about 1.25 line heights each
    from brainsync.stimulus.display import RATE_PROMPT, TEXT_GAP, TEXT_HEIGHT
    lines = RATE_PROMPT.count("\n") + 1
    edge = max(abs(t.pos[1]) for t in four.targets) + four.reach + TEXT_GAP
    assert edge + 1.25 * lines * TEXT_HEIGHT <= 0.5 + 1e-9, edge
    validate(four)
    s = four.session
    assert (s.blocks, s.cue_s, s.stim_s, s.rest_s, s.shuffle) == (8, 1.0, 4.0, 3.0, True)
    assert len(session_trials(four, seed=0)) == 32
    warn = config_warnings(four)
    assert len(warn) == 1 and "photosensitivity" in warn[0] and "15/17 Hz" in warn[0]


def test_resolve_config_by_path_and_missing(tmp_path: Path):
    p = tmp_path / "mine.toml"
    p.write_text('name = "mine"\n[targets]\nfreqs = [12.0]\n[layout]\ncols = 1\nsize = 0.5\n')
    assert load_config(str(p)).name == "mine"
    assert resolve_config("two_targets").name == "two_targets.toml"
    with pytest.raises(FileNotFoundError, match="nope"):
        load_config("nope")


def test_validation_errors():
    with pytest.raises(ValueError, match="30 Hz"):
        _cfg(targets={"freqs": [15.0, 30.0]})
    with pytest.raises(ValueError, match="half"):
        _cfg(targets={"freqs": [45.0, 17.0]})
    with pytest.raises(ValueError, match="share frequency 15"):
        _cfg(targets={"freqs": [15.0, 15.0], "phases_pi": [0.5, 0.5]})
    with pytest.raises(ValueError, match="share"):
        _cfg(targets={"freqs": [15.0, 15.0], "phases_pi": [0.0, 2.0]})
    assert _cfg(targets={"freqs": [15.0, 15.0], "phases_pi": [0.0, 1.0]}).n_targets == 2
    with pytest.raises(ValueError, match="sawtooth"):
        _cfg(stimulus={"waveform": "sawtooth"})
    with pytest.raises(ValueError, match="overlap"):
        _cfg(layout={"spacing": 0.3})
    with pytest.raises(ValueError, match="3 targets"):
        _cfg(targets={"freqs": [12.0, 15.0, 17.0]})
    with pytest.raises(ValueError, match="fps"):
        _cfg(display={"fps": 60})
    with pytest.raises(ValueError, match="1 phases_pi"):
        _cfg(targets={"phases_pi": [0.0]})
    with pytest.raises(ValueError, match="-1"):
        _cfg(session={"rest_s": -1.0})
    with pytest.raises(ValueError, match="0"):
        override(_cfg(), blocks=0)
    # a frequency legal at 60 Hz is refused once the refresh drops
    with pytest.raises(ValueError, match="15 Hz is not below half the 30 Hz"):
        override(_cfg(), refresh_hz=30.0)


def test_warnings():
    w = config_warnings(_cfg(targets={"freqs": [12.0, 17.0]}))
    assert len(w) == 1 and "15-25 Hz" in w[0] and "17" in w[0]
    assert config_warnings(_cfg(targets={"freqs": [10.0, 12.0]})) == []
    sq = config_warnings(_cfg(targets={"freqs": [10.0, 12.0]}, stimulus={"waveform": "square"}))
    assert len(sq) == 1 and "uneven" in sq[0] and "12" in sq[0]


def test_overrides():
    base = _cfg()
    cfg = override(base, blocks=5, stim_s=2.0, cue_s=None)
    assert (cfg.session.blocks, cfg.session.stim_s, cfg.session.cue_s) == (5, 2.0, 0.05)
    assert override(cfg, refresh_hz=120.0).refresh == 120.0
    assert override(base, hold_cue=True).hold_cue and not base.hold_cue
    assert override(base) is base and override(base, blocks=None) is base
    with pytest.raises(KeyError, match="speed"):
        override(base, speed=2)
    with pytest.raises(ValueError, match="refresh_hz must be positive, got -1"):
        override(base, refresh_hz=-1.0)


def test_depth_defaults_to_full_and_is_checked():
    assert _cfg().depth == 1.0
    shipped = [load_config(name) for name in available_configs()]
    # every flicker config is full depth; tvep_4 (flashes) rests at 0.1 by design
    assert all(c.depth == 1.0 for c in shipped if not c.coded)
    assert all(0.0 < c.depth <= 1.0 for c in shipped if c.coded)   # tvep_4 is pinned in test_vep_fake
    half = _cfg(stimulus={"depth": 0.5})
    assert half.depth == 0.5 and _cfg(stimulus={"depth": 1}).depth == 1.0
    for bad in (0.0, 1.5, -0.2, math.nan, math.inf):
        with pytest.raises(ValueError, match=r"\[stimulus\] depth must be in \(0, 1\]"):
            _cfg(stimulus={"depth": bad})
    base = _cfg()
    assert override(base, depth=0.5).depth == 0.5 and base.depth == 1.0
    assert override(half, depth=None) is half
    with pytest.raises(ValueError, match="depth"):
        override(base, depth=0.0)
    with pytest.raises(ValueError, match="depth"):
        override(base, depth=1.5)


@pytest.mark.parametrize("phases", [[1.0, 3.0], [1.0, -1.0], [0.5, 2.5], [0.0, 2.0],
                                    [-0.5, 1.5], [0.25, 4.25]])
def test_duplicate_phase_modulo_two_pi(phases):
    with pytest.raises(ValueError, match="same stimulus"):
        _cfg(targets={"freqs": [15.0, 15.0], "phases_pi": phases})


def test_same_phase_helper():
    assert same_phase(math.pi, 3 * math.pi) and same_phase(math.pi, -math.pi)
    assert same_phase(0.0, 2 * math.pi - 1e-12)
    assert not same_phase(0.0, math.pi) and not same_phase(0.5 * math.pi, 1.5 * math.pi)
    assert _cfg(targets={"freqs": [15.0, 15.0], "phases_pi": [1.0, 2.0]}).n_targets == 2


@pytest.mark.parametrize("section, key, value, what", [
    ("targets", "freqs", "12", "list of numbers"),
    ("targets", "freqs", [15.0, "17"], "numbers only"),
    ("targets", "freqs", [15.0, True], "numbers only"),
    ("targets", "phases_pi", 0.0, "list of numbers"),
    ("targets", "labels", ["a", 2], "list of strings"),
    ("stimulus", "hold_cue", "false", "true or false"),
    ("stimulus", "waveform", 1, "string"),
    ("stimulus", "depth", "x", "number"),
    ("stimulus", "depth", True, "number"),
    ("session", "shuffle", 1, "true or false"),
    ("session", "blocks", 2.7, "integer"),
    ("session", "blocks", True, "integer"),
    ("session", "stim_s", "4", "number"),
    ("layout", "cols", 2.0, "integer"),
    ("layout", "size", True, "number"),
    ("display", "refresh_hz", "60", "number"),
    ("display", "screen", 0.5, "integer"),
])
def test_wrong_types_are_refused(section, key, value, what):
    with pytest.raises(ValueError, match=rf"\[{section}\] {key} must .*{what}.*"
                                         + re.escape(repr(value))):
        _cfg(**{section: {key: value}})


def test_name_must_be_string():
    with pytest.raises(ValueError, match="name must be a string, got 3"):
        _cfg(name=3)


def test_layout_must_fit_screen_width_and_height():
    with pytest.raises(ValueError, match=r"reaches 1\.240 .*horizontally.*1\.5:1"):
        _cfg(layout={"spacing": 2.0})
    # same layout passes on a screen declared wide enough
    wide = _cfg(layout={"spacing": 2.0}, display={"aspect": 2.5})
    assert [t.pos for t in wide.targets] == [(-1.0, 0.0), (1.0, 0.0)]
    with pytest.raises(ValueError, match="vertically"):
        _cfg(targets={"freqs": [12.0, 15.0]},
             layout={"rows": 2, "cols": 1, "size": 0.4, "spacing": 0.6})
    with pytest.raises(ValueError, match="aspect must be positive"):
        _cfg(display={"aspect": 0.0})
    # the shipped pair fits with its cue box on a 1.5:1 screen, the MacBook is ~1.54
    two = load_config("two_targets")
    assert max(abs(t.pos[0]) for t in two.targets) + two.reach <= 0.75
    assert layout_overflow(two, 3024, 1964) is None
    msg = layout_overflow(two, 1440, 1080)
    assert msg is not None and "0.740" in msg and "0.667" in msg


def test_grid_positions():
    assert grid_positions(1, 1, 1, 0.0) == [(0.0, 0.0)]
    assert grid_positions(2, 1, 2, 1.0) == [(-0.5, 0.0), (0.5, 0.0)]
    assert grid_positions(3, 2, 2, 0.3) == pytest.approx(
        [(-0.15, 0.15), (0.15, 0.15), (-0.15, -0.15)])
    with pytest.raises(ValueError, match="too few for 5 targets"):
        grid_positions(5, 2, 2, 0.3)
    with pytest.raises(ValueError, match="rows=0"):
        grid_positions(1, 0, 1, 0.3)


# -- trial order ----------------------------------------------------------------------------

def test_trial_order_deterministic_and_balanced():
    a = make_trials(4, 25, seed=7)
    b = make_trials(4, 25, seed=7)
    assert a == b
    assert [t.target for t in a] != [t.target for t in make_trials(4, 25, seed=8)]
    assert [t.index for t in a] == list(range(100))
    for blk in range(25):
        assert sorted(t.target for t in a if t.block == blk) == [0, 1, 2, 3]
    assert len({tuple(t.target for t in a if t.block == blk) for blk in range(25)}) > 1
    plain = make_trials(3, 2, shuffle=False)
    assert [t.target for t in plain] == [0, 1, 2, 0, 1, 2]
    assert len(session_trials(load_config("two_targets"), seed=1)) == 20
    with pytest.raises(ValueError, match="0"):
        make_trials(0, 3)


# -- feedback -------------------------------------------------------------------------------

def test_parse_line():
    assert parse_line("1\n", 2) == 1
    assert parse_line(" -1 ", 2) == -1
    for bad in ("", "x", "2", "-2", "1.5"):
        with pytest.raises(ValueError):
            parse_line(bad, 2)


def test_feedback_reader_stringio_until_eof():
    notes: list[str] = []
    r = FeedbackReader(io.StringIO("0\nhello\n1\n\n7\n"), 2, note=notes.append)
    assert r.current == -1 and not r.done
    r.start()
    assert r.wait(5.0)
    assert r.done and r.current == 1
    assert r.n_lines == 5 and r.n_ignored == 3 and len(notes) == 3
    assert "hello" in notes[0] and "7" in notes[2]
    r2 = FeedbackReader(io.StringIO("1\n-1\n"), 2, note=notes.append)
    r2.run()
    assert r2.done and r2.current == -1
    empty = FeedbackReader(io.StringIO(""), 2).start()
    assert empty.wait(5.0) and empty.current == -1
    assert empty._thread is not None and empty._thread.daemon



def test_parse_lean_lines():
    assert parse_command("lean 2 63 1 4\n", 4).extra == (63, 1, 4)
    assert parse_command("lean 2 63 1 4", 4).value == 2
    assert parse_command("lean -1", 4).value == -1
    for bad in ("lean", "lean 4 50 1 4", "lean 0 101 1 4", "lean 0 50 5 4", "lean 0 50 1 0",
                "lean 0 x 1 4", "lean 0 50 1"):
        with pytest.raises(ValueError):
            parse_command(bad, 4)


def test_feedback_reader_keeps_the_latest_lean_without_hearing_it():
    r = FeedbackReader(io.StringIO("lean 1 70 2 4\nlean 9 1 1 1\n"), 2, note=lambda s: None)
    assert r.lean == (-1, 0, 0, 0)
    r.run()
    assert r.lean == (1, 70, 2, 4) and r.n_ignored == 1
    assert r.current == -1
    # a lean line never releases --hold-still; "lean -1" hides the meter
    r2 = FeedbackReader(io.StringIO("lean 0 40 0 4\nlean -1\n"), 2, note=lambda s: None)
    r2.run()
    assert r2.lean == (-1, 0, 0, 0) and not r2._heard.is_set()


def test_free_frames_carry_the_lean_on_flicker_frames_only():
    cfg = load_config("two_targets")
    held = iter([True, True] + [False] * 100)
    frames = list(free_frames(cfg, seconds=0.05, hold=lambda: next(held),
                              lean=lambda: (1, 80, 3, 4)))
    assert [f.lean for f in frames[:2]] == [(-1, 0, 0, 0)] * 2
    assert all(f.lean == (1, 80, 3, 4) for f in frames[2:])


def test_play_passes_the_lean_only_when_set():
    seen = []

    class Plain:
        def draw(self, lum, cue, **kw):
            seen.append(kw)

        def flip(self):
            return float(len(seen)) / 60.0

        def escape_pressed(self):
            return False

    frames = [Frame(np.zeros(2)), Frame(np.zeros(2), lean=(0, 55, 1, 4))]
    play(frames, Plain(), MarkerWriter("none"), 60.0)
    assert seen == [{}, {"lean": (0, 55, 1, 4)}]


# -- markers and playback -------------------------------------------------------------------

def test_marker_format_parses():
    line = format_marker(1792710523.80581, "stim_on:1")
    assert line == "[marker 1792710523.8058] stim_on:1"
    out = io.StringIO()
    clock = iter([100.0, 104.25]).__next__
    m = MarkerWriter("unix", stream=out, clock=clock)
    m.emit(["cue:0", "stim_on:0"])
    m.emit(["stim_off:0"])
    m.emit([])
    lines = out.getvalue().splitlines()
    assert lines == ["[marker 100.0000] cue:0", "[marker 100.0000] stim_on:0",
                     "[marker 104.2500] stim_off:0"]
    assert parse_markers(out.getvalue()) == [(100.0, "stim_on", 0), (104.25, "stim_off", 0)]
    silent = io.StringIO()
    MarkerWriter("none", stream=silent).emit(["cue:1"])
    assert silent.getvalue() == ""
    with pytest.raises(ValueError, match="lsl"):
        MarkerWriter("lsl")


class FakeRenderer:
    """Records draws; flip() advances a fake vsync clock, with optional late flips."""

    def __init__(self, refresh: float, late: set[int] = frozenset(), escape_at: int = -1):
        self.period = 1.0 / refresh
        self.late = late
        self.escape_at = escape_at
        self.t = 0.0
        self.draws: list[tuple[np.ndarray, int]] = []
        self.flips = 0
        self.log: list[str] = []

    def draw(self, lum, cue):
        self.draws.append((np.array(lum), cue))

    def flip(self):
        self.t += self.period * (2 if self.flips in self.late else 1)
        self.flips += 1
        self.log.append("flip")
        return self.t

    def escape_pressed(self):
        return len(self.draws) == self.escape_at


def test_cued_frames_schedule():
    cfg = _cfg()  # cue 3, stim 6, rest 3 frames at 60 Hz
    trials = make_trials(2, 2, seed=3)
    frames = list(cued_frames(cfg, trials))
    assert len(frames) == 4 * 12
    first = trials[0].target
    assert frames[0].events == (f"cue:{first}",) and frames[0].cue == first
    assert frames[3].events == (f"stim_on:{first}",) and frames[3].trial == 0
    assert frames[3].cue == -1  # hold_cue off by default
    stim = [f for f in frames if f.trial == 0]
    assert len(stim) == 6
    for i, fr in enumerate(stim):
        np.testing.assert_allclose(fr.lum, frame_luminances(cfg.freqs, cfg.phases, i, 60.0))
    assert frames[9].events == (f"stim_off:{first}",) and frames[9].cue == -1
    still = frame_luminances(cfg.freqs, cfg.phases, 0, 60.0)
    np.testing.assert_allclose(frames[0].lum, still)
    np.testing.assert_allclose(frames[10].lum, still)


def test_hold_cue_and_zero_cue():
    trials = make_trials(2, 1, shuffle=False)
    off = list(cued_frames(_cfg(stimulus={"hold_cue": False}), trials))
    assert off[3].trial == 0 and off[3].cue == -1
    on = list(cued_frames(_cfg(stimulus={"hold_cue": True}), trials))
    assert all(f.cue == 0 for f in on if f.trial == 0)
    nocue = list(cued_frames(_cfg(session={"cue_s": 0.0}), trials))
    assert nocue[0].events == ("stim_on:0",)
    held = list(cued_frames(_cfg(session={"cue_s": 0.0}, stimulus={"hold_cue": True}), trials))
    assert held[0].events == ("cue:0", "stim_on:0")


def test_frames_draw_at_the_config_depth():
    # non-zero phases, so the still frame 0 is off mid-grey and shows the depth
    cfg = _cfg(stimulus={"depth": 0.5}, targets={"phases_pi": [0.5, 0.25]})
    trials = make_trials(2, 1, shuffle=False)
    frames = list(cued_frames(cfg, trials))
    still = frame_luminances(cfg.freqs, cfg.phases, 0, 60.0, depth=0.5)
    assert not np.allclose(still, frame_luminances(cfg.freqs, cfg.phases, 0, 60.0))
    np.testing.assert_array_equal(frames[0].lum, still)
    np.testing.assert_array_equal(frames[-1].lum, still)
    stim = [f for f in frames if f.trial == 0]
    for i, fr in enumerate(stim):
        np.testing.assert_array_equal(
            fr.lum, frame_luminances(cfg.freqs, cfg.phases, i, 60.0, depth=0.5))
    assert all(0.25 <= v <= 0.75 for fr in frames for v in fr.lum)
    held = iter([True] * 3 + [False] * 100)
    free = list(free_frames(cfg, seconds=0.1, hold=lambda: next(held)))
    for fr in free[:3]:
        np.testing.assert_array_equal(fr.lum, still)
    for i, fr in enumerate(free[3:]):
        np.testing.assert_array_equal(
            fr.lum, frame_luminances(cfg.freqs, cfg.phases, i, 60.0, depth=0.5))


def test_play_marks_after_flip_and_counts_drops():
    cfg = _cfg()
    trials = make_trials(2, 2, seed=3)
    # trial 1 spans frames 12..23 (cue 12-14, stim 15-20); frame 17 flips one vsync late
    ren = FakeRenderer(60.0, late={17})
    out = io.StringIO()

    class Writer(MarkerWriter):
        def emit(self, events):
            if events:
                ren.log.append("emit")
            super().emit(events)

    m = Writer("monotonic", stream=out, clock=lambda: ren.t)
    timing = play(cued_frames(cfg, trials), ren, m, 60.0,
                  trial_targets={t.index: t.target for t in trials})
    assert not timing.aborted and timing.n_frames == 48 == ren.flips
    assert ren.log[0] == "flip" and ren.log[1] == "emit"
    assert len(timing.trials) == 4
    assert [t.dropped for t in timing.trials] == [0, 1, 0, 0]
    assert timing.dropped == 1
    assert timing.trials[0].duration == pytest.approx(0.1)
    assert timing.trials[1].duration == pytest.approx(0.1 + 1 / 60)
    assert [t.target for t in timing.trials] == [t.target for t in trials]
    marks = parse_markers(out.getvalue())
    assert [k for _, k, _ in marks] == ["stim_on", "stim_off"] * 4
    on0, off0 = marks[0][0], marks[1][0]
    assert off0 - on0 == pytest.approx(0.1)
    s = timing.summary()
    assert s.startswith("timing: 4 trials") and "1 dropped flicker frames in 1 trial" in s
    assert "\n" not in s


def test_play_escape_aborts():
    cfg = _cfg()
    ren = FakeRenderer(60.0, escape_at=5)
    timing = play(cued_frames(cfg, make_trials(2, 2)), ren, MarkerWriter("none"), 60.0)
    assert timing.aborted and ren.flips == 5 and "ABORTED" in timing.summary()


def test_held_free_run_marks_its_onset_after_the_hold():
    cfg = _cfg()
    held = iter([True] * 3 + [False] * 100)
    out = io.StringIO()
    ren = FakeRenderer(60.0)
    play(free_frames(cfg, seconds=0.1, hold=lambda: next(held)), ren,
         MarkerWriter("monotonic", stream=out, clock=lambda: ren.t), 60.0)
    text = out.getvalue()
    assert text.count("free_on") == 1
    # the still frames flip first; the marker carries the fourth flip's time
    assert free_onset(text) == pytest.approx(ren.t - (ren.flips - 4) / 60.0, abs=1e-4)
    assert parse_markers(text) == []                   # no stim_on/off to confuse a recording
    assert free_onset("measured refresh: 60.00 Hz\n") is None
    assert free_onset("[marker 1792911476.6148] free_on\n") == 1792911476.6148


def test_free_frames_poll_feedback_and_stop():
    cfg = _cfg()
    assert len(list(free_frames(cfg, seconds=0.5))) == 30
    calls = []
    state = {"n": 0}

    def cue():
        calls.append(1)
        return 1 if len(calls) > 2 else -1

    def stop():
        state["n"] += 1
        return state["n"] > 10

    frames = list(free_frames(cfg, cue=cue, stop=stop))
    assert len(frames) == 10 and len(calls) == 10
    assert [f.cue for f in frames[:4]] == [-1, -1, 1, 1]
    for i, fr in enumerate(frames):
        np.testing.assert_allclose(fr.lum, frame_luminances(cfg.freqs, cfg.phases, i, 60.0))
    # the first flicker frame, and only it, carries the free run's onset marker
    assert [f.events for f in frames] == [(FREE_ON,)] + [()] * 9
    reader = FeedbackReader(io.StringIO("1\n"), 2, note=lambda s: None)
    reader.run()
    assert list(free_frames(cfg, cue=lambda: reader.current, stop=lambda: reader.done)) == []


def test_refresh_helpers():
    assert refresh_mismatch(60.02, 60.0, 1.0) is None
    msg = refresh_mismatch(120.1, 60.0, 1.0)
    assert msg is not None and "120.10" in msg and "60" in msg
    stamps = np.arange(50) / 59.94
    assert median_rate(stamps) == pytest.approx(59.94)
    with pytest.raises(ValueError):
        median_rate([1.0])
    assert "measured 59.94" in SessionTiming(refresh=60.0, measured_refresh=59.94).summary()
    assert Frame(np.zeros(2)).cue == -1


# -- CLI ------------------------------------------------------------------------------------

def test_plan_via_main(capsys):
    assert cli.main(["--plan", "--seed", "11"]) == 0
    out, err = capsys.readouterr()
    assert "config two_targets" in out
    assert "2 target(s), sine flicker, refresh 60 Hz" in out
    assert "15.000 Hz" in out and "17.000 Hz" in out
    assert "centre (-0.500, +0.000)" in out and "centre (+0.500, +0.000)" in out
    assert "20 trial(s)" in out and "2 min 40 s" in out and "seed 11" in out
    blocks = [ln for ln in out.splitlines() if ln.strip().startswith("block ")]
    assert len(blocks) == 10
    order = [ln.split(":")[1].split() for ln in blocks]
    assert all(sorted(o) == ["0", "1"] for o in order)
    expect = [str(t.target) for t in session_trials(load_config("two_targets"), 11)]
    assert sum(order, []) == expect
    assert "15-25 Hz" in err
    assert cli.main(["--plan", "--seed", "11"]) == 0
    assert capsys.readouterr().out == out


def test_plan_overrides_and_errors(capsys):
    assert cli.main(["--plan", "-c", "one_target_12", "--blocks", "3", "--stim-s", "2",
                     "--seed", "0"]) == 0
    out, err = capsys.readouterr()
    assert "3 block(s) x 1 = 3 trial(s)" in out and "stim 2 +" in out and err == ""
    assert cli.main(["--plan", "--free"]) == 0
    assert "free run" in capsys.readouterr().out
    assert cli.main(["--plan", "-c", "missing_config"]) == 2
    assert "missing_config" in capsys.readouterr().err
    assert cli.main(["--plan", "--refresh", "30"]) == 2
    assert "30 Hz refresh" in capsys.readouterr().err
    assert cli.main(["--plan", "--no-hold-cue"]) == 0
    assert "cue held through flicker: no" in capsys.readouterr().out
    assert cli.main(["--plan"]) == 0
    assert "flicker depth 1\n" in capsys.readouterr().out
    assert cli.main(["--plan", "--depth", "0.5"]) == 0
    assert "flicker depth 0.5\n" in capsys.readouterr().out
    assert cli.main(["--plan", "--depth", "0"]) == 2
    assert "depth must be in (0, 1]" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--plan", "--depth", "half"])
    with pytest.raises(SystemExit):
        cli.main(["--plan", "--seconds", "5"])
    with pytest.raises(SystemExit):
        cli.main(["--plan", "--refresh", "fast"])
    with pytest.raises(SystemExit):
        cli.main(["--windowed"])


def test_import_does_not_load_psychopy():
    import subprocess
    code = ("import sys, brainsync.stimulus, brainsync.stimulus.display, "
            "brainsync.stimulus.__main__; print('psychopy' in sys.modules)")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert res.stdout.strip() == "False"


class _FakeWindow(FakeRenderer):
    """Stands in for PsychoPyRenderer so main() runs a whole session without a display."""

    measured = 60.01
    instances: list[_FakeWindow] = []

    def __init__(self, cfg):
        super().__init__(60.0)
        self.cfg = cfg
        self.closed = False
        _FakeWindow.instances.append(self)

    def measure_refresh(self):
        return self.measured

    def wait_for_start(self, still):
        raise AssertionError("--no-wait should skip the start screen")

    def close(self):
        self.closed = True


@pytest.fixture
def fake_window(monkeypatch):
    import brainsync.stimulus.display as disp
    _FakeWindow.instances = []
    _FakeWindow.measured = 60.01
    monkeypatch.setattr(disp, "PsychoPyRenderer", _FakeWindow)
    return _FakeWindow


def test_main_cued_session_with_fake_window(fake_window, capsys):
    rc = cli.main(["--no-wait", "--blocks", "2", "--cue-s", "0.05", "--stim-s", "0.1",
                   "--rest-s", "0.05", "--seed", "4", "--markers", "monotonic"])
    out, err = capsys.readouterr()
    assert rc == 0 and fake_window.instances[0].closed
    lines = err.splitlines()
    assert "measured refresh: 60.01 Hz" in lines
    first_marker = next(i for i, ln in enumerate(lines) if ln.startswith("[marker"))
    assert lines.index("measured refresh: 60.01 Hz") < first_marker
    expect = [t.target for t in make_trials(2, 2, seed=4)]
    cues = [int(ln.rsplit(":", 1)[1]) for ln in lines if ln.endswith(tuple(
        f"] cue:{k}" for k in range(2)))]
    assert cues == expect
    marks = parse_markers(err)
    assert [(k, tg) for _, k, tg in marks] == [
        (kind, tg) for tg in expect for kind in ("stim_on", "stim_off")]
    assert out.startswith("timing: 4 trials, 0 dropped") and out.count("\n") == 1


def test_main_refuses_refresh_mismatch(fake_window, capsys):
    fake_window.measured = 120.1
    assert cli.main(["--no-wait", "--blocks", "1"]) == 2
    err = capsys.readouterr().err
    assert "error: measured refresh 120.10" in err
    # launchers wait for "measured refresh:", so a refused rate must never print it
    assert "measured refresh: " not in err
    assert "[marker" not in err and fake_window.instances[0].closed
    # --ignore-refresh runs anyway; --refresh auto adopts the measured rate
    assert cli.main(["--no-wait", "--blocks", "1", "--stim-s", "0.05", "--rest-s", "0",
                     "--cue-s", "0", "--ignore-refresh", "--markers", "none"]) == 0
    out = capsys.readouterr().out
    assert "refresh 60 Hz (measured 120.10)" in out
    assert cli.main(["--no-wait", "--blocks", "1", "--stim-s", "0.05", "--cue-s", "0",
                     "--rest-s", "0", "--refresh", "auto", "--markers", "none"]) == 0
    assert "refresh 120 Hz" in capsys.readouterr().out


def test_main_free_run_with_seconds(fake_window, capsys):
    assert cli.main(["--free", "--seconds", "0.5", "--no-wait", "--markers", "none"]) == 0
    out = capsys.readouterr().out
    assert "30 frames, 0 dropped overall" in out
    assert {c for _, c in fake_window.instances[0].draws} == {-1}


def test_main_start_screen_shows_the_depth(fake_window, monkeypatch, tmp_path, capsys):
    seen = []
    monkeypatch.setattr(fake_window, "wait_for_start", lambda self, still: seen.append(still))
    src = resolve_config("two_targets").read_text()
    assert "phases_pi = [0.0, 0.0]" in src
    p = tmp_path / "phased.toml"
    p.write_text(src.replace("phases_pi = [0.0, 0.0]", "phases_pi = [0.5, 0.25]"))
    assert cli.main(["-c", str(p), "--depth", "0.5", "--blocks", "1", "--stim-s", "0.05",
                     "--cue-s", "0", "--rest-s", "0", "--markers", "none"]) == 0
    capsys.readouterr()
    cfg = load_config(str(p))
    assert len(seen) == 1
    np.testing.assert_array_equal(
        seen[0], frame_luminances(cfg.freqs, cfg.phases, 0, 60.0, depth=0.5))
    assert not np.allclose(seen[0], frame_luminances(cfg.freqs, cfg.phases, 0, 60.0))


def test_main_free_feedback_stdin_ends_at_eof(fake_window, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\nbad\n"))
    assert cli.main(["--free", "--feedback", "stdin", "--no-wait", "--markers", "none"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("timing: ") and "ignored line 2" in err


class _FakeStim:
    def __init__(self, win, **kw):
        self.kw = kw
        self.fillColor = kw.get("fillColor")
        self.drawn = 0

    def draw(self):
        self.drawn += 1


class _FakeRect(_FakeStim):
    pass


class _FakeShapeStim(_FakeStim):
    pass


class _FakePsychoWindow:
    size = (3024, 1964)   # 14" MacBook Pro, 1.54:1

    def __init__(self, **kw):
        self.kw = kw
        self.mouse = True
        self.t = 0.0
        self.closed = False

    def setMouseVisible(self, visible):
        self.mouse = visible

    def flip(self):
        self.t += 1 / 60.0
        return self.t

    def close(self):
        self.closed = True


def test_psychopy_renderer_against_fake_psychopy(monkeypatch, capsys):
    import types

    from brainsync.stimulus.display import Aborted, PsychoPyRenderer
    keys: list[list[str]] = [[], ["escape"]]
    visual = types.SimpleNamespace(Window=_FakePsychoWindow, Rect=_FakeStim, TextStim=_FakeStim,
                                   ShapeStim=_FakeStim)
    event = types.SimpleNamespace(getKeys=lambda keyList: keys.pop(0) if keys else [],
                                  clearEvents=lambda: None)
    fake = types.ModuleType("psychopy")
    fake.visual, fake.event = visual, event
    monkeypatch.setitem(sys.modules, "psychopy", fake)
    cfg = load_config("two_targets")
    r = PsychoPyRenderer(cfg)
    assert r.win.kw["fullscr"] is True and r.win.kw["units"] == "height"
    assert r.win.mouse is False and capsys.readouterr().err == ""
    assert r.measure_refresh() == pytest.approx(60.0)
    r.draw(np.array([1.0, 0.0]), 1)
    assert r.squares[0].fillColor == (1.0, 1.0, 1.0) and r.squares[1].fillColor == (-1, -1, -1)
    assert [b.drawn for b in r.boxes] == [0, 1]
    assert r.boxes[0].kw["size"] == pytest.approx((0.48, 0.48))
    assert not r.escape_pressed() and r.escape_pressed()
    keys[:] = [[], ["escape"]]
    with pytest.raises(Aborted):
        r.wait_for_start(np.zeros(2))
    r.close()
    assert r.win.closed
    monkeypatch.setattr(_FakePsychoWindow, "size", (1440, 1080))   # 4:3 is too narrow
    PsychoPyRenderer(cfg)
    assert "cut off" in capsys.readouterr().err


def test_psychopy_renderer_draws_target_shapes(monkeypatch):
    import dataclasses
    import types

    from brainsync.stimulus import shapes
    from brainsync.stimulus.display import PsychoPyRenderer
    visual = types.SimpleNamespace(Window=_FakePsychoWindow, Rect=_FakeRect,
                                   ShapeStim=_FakeShapeStim, TextStim=_FakeStim)
    event = types.SimpleNamespace(getKeys=lambda keyList: [], clearEvents=lambda: None)
    fake = types.ModuleType("psychopy")
    fake.visual, fake.event = visual, event
    monkeypatch.setitem(sys.modules, "psychopy", fake)
    cfg = _cfg(targets={"freqs": [13.0, 15.0, 17.0]}, layout={"cols": 3, "spacing": 0.5})
    names = ["square", "plus", "arrow-up"]
    cfg = dataclasses.replace(cfg, targets=tuple(
        dataclasses.replace(t, shape=n) for t, n in zip(cfg.targets, names)))
    r = PsychoPyRenderer(cfg)
    square, plus, arrow = r.squares
    assert type(square) is _FakeRect
    assert square.kw["size"] == (cfg.size, cfg.size) and square.kw["pos"] == cfg.targets[0].pos
    for stim, t in ((plus, cfg.targets[1]), (arrow, cfg.targets[2])):
        assert type(stim) is _FakeShapeStim
        assert stim.kw["vertices"] == shapes.vertices(t.shape, cfg.size)
        assert stim.kw["pos"] == t.pos and stim.kw["lineColor"] is None
    r.draw(np.array([1.0, 0.0, 1.0]), -1)
    assert [s.fillColor for s in r.squares] == [(1.0, 1.0, 1.0), (-1, -1, -1), (1.0, 1.0, 1.0)]
    assert [s.drawn for s in r.squares] == [1, 1, 1]


def test_ready_line_follows_the_refresh_check(fake_window, capsys):
    assert cli.main(["--no-wait", "--blocks", "1", "--stim-s", "0.05", "--cue-s", "0",
                     "--rest-s", "0", "--refresh", "auto", "--markers", "none"]) == 0
    err = capsys.readouterr().err.splitlines()
    assert err.index("using refresh 60 Hz") < err.index("measured refresh: 60.01 Hz")


def test_free_frames_hold_still_until_released():
    cfg = _cfg()
    held = [True] * 5 + [False] * 100
    calls = iter(held)
    frames = list(free_frames(cfg, seconds=0.1, cue=lambda: 1, hold=lambda: next(calls)))
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform)
    assert len(frames) == 5 + 6                       # seconds counts flicker frames only
    for fr in frames[:5]:
        np.testing.assert_array_equal(fr.lum, still)
        assert fr.cue == -1
    for i, fr in enumerate(frames[5:]):             # flicker from frame 0, no jump
        np.testing.assert_array_equal(
            fr.lum, frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform))
        assert fr.cue == 1


def test_free_frames_hold_ends_on_stop():
    cfg = _cfg()
    n = iter(range(100))
    frames = list(free_frames(cfg, hold=lambda: True, stop=lambda: next(n) >= 7))
    assert len(frames) == 7


def test_feedback_reader_heard_after_first_valid_line():
    r = FeedbackReader(io.StringIO("junk\n-1\n"), 2, note=lambda s: None)
    assert not r.heard
    r.run()
    assert r.heard and r.current == -1


def test_hold_still_needs_stdin_feedback():
    with pytest.raises(SystemExit):
        cli.main(["--free", "--hold-still", "--plan"])
    assert cli.main(["--free", "--feedback", "stdin", "--hold-still", "--plan"]) == 0


def test_four_targets_config_has_the_gate_frequencies():
    """The page-mode gate set (PLAN.md section 14), recordable with brainsync.record."""
    from brainsync.stimulus.config import load_config

    cfg = load_config("four_targets")
    assert cfg.n_targets == 4
    assert cfg.freqs.tolist() == [13.0, 15.0, 17.0, 11.0]
    assert cfg.phases.tolist() == [0.0] * 4
    assert cfg.labels == ("13", "15", "17", "11")


def test_feedback_reader_takes_text_lines_on_the_same_stream():
    notes: list[str] = []
    r = FeedbackReader(io.StringIO('{"text": "hello"}\n0\n{"nope": 1}\n{bad\n{"text": ""}\n'),
                       2, note=notes.append)
    assert r.control.text == ""
    assert not r.heard
    r.run()
    assert r.current == 0 and r.heard
    assert r.control.text == ""   # the last line cleared it
    assert r.n_lines == 5 and r.n_ignored == 2 and len(notes) == 2
    r2 = FeedbackReader(io.StringIO('{"text": "YES  ####  0.42"}\n'), 2, note=notes.append)
    r2.run()
    assert r2.control.text == "YES  ####  0.42" and r2.current == -1


def test_free_frames_show_the_centre_text_on_flicker_frames_too():
    from brainsync.stimulus.feedback import Control
    cfg = load_config("two_targets")
    states = iter([Control(used=True, labels=("", ""), text="a", active=(0, 1)),
                   Control(used=True, labels=("", ""), text="b", active=(0, 1), flicker=True,
                           serial=1, flickering=(0, 1)),
                   Control(used=True, labels=("", ""), text="c", active=(0, 1), flicker=True,
                           serial=1, flickering=(0, 1))])
    frames = list(free_frames(cfg, seconds=2 / 60, control=lambda: next(states)))
    assert [f.centre for f in frames] == ["a", "b", "c"]
    assert frames[0].trial == -1 and frames[1].trial == frames[2].trial == 0
    plain = list(free_frames(cfg, seconds=2 / 60))
    assert all(f.centre == "" and not any(f.labels) for f in plain)


def test_labels_flag_in_a_free_run():
    with pytest.raises(SystemExit):
        cli.main(["--labels", "--plan"])
    with pytest.raises(SystemExit):   # a free run's text comes as JSON lines on --feedback
        cli.main(["--free", "--feedback", "stdin", "--status", "stdin", "--plan"])
    assert cli.main(["--free", "--feedback", "stdin", "--labels", "--plan"]) == 0
    assert cli.main(["--free", "--labels", "--plan"]) == 0


def test_feedback_reader_takes_label_lines_for_every_target():
    notes: list[str] = []
    r = FeedbackReader(io.StringIO('{"labels": ["YES  .4", "NO  .1"]}\n{"labels": ["one"]}\n'
                                   '{"labels": [1, 2]}\n0\n'), 2, note=notes.append)
    assert r.control.labels == ("", "")
    r.run()
    assert r.control.labels == ("YES  .4", "NO  .1") and r.current == 0
    assert r.n_ignored == 2 and all("labels" in n for n in notes)
    seeded = FeedbackReader(io.StringIO(""), 2, labels=("YES", "NO"))
    assert seeded.control.labels == ("YES", "NO") and not seeded.control.used
    with pytest.raises(ValueError, match="labels"):
        FeedbackReader(io.StringIO(""), 2, labels=("YES",))


def test_free_frames_show_the_starting_labels_until_a_labels_line():
    from brainsync.stimulus.feedback import apply_control, initial_control
    cfg = load_config("two_targets")
    start = initial_control(2, ("YES", "NO"))
    on = apply_control(start, {"labels": ("a", "b"), "flicker": True})
    states = iter([start, start, on])
    held = iter([True, False])
    frames = list(free_frames(cfg, seconds=2 / 60, hold=lambda: next(held),
                              control=lambda: next(states)))
    assert [f.labels for f in frames] == [("YES", "NO"), ("YES", "NO"), ("a", "b")]
    # held, then the legacy flicker, then the JSON branch's first flicker run
    assert [f.trial for f in frames] == [-1, -1, 0] and frames[2].events == ("flicker_on",)


def test_diamond_layout_and_fill():
    cfg = load_config("cvep_4")
    assert cfg.shape == "diamond" and cfg.fill == (1.0, 1.0, 1.0)
    assert [t.pos for t in cfg.targets] == [(0.0, 0.32), (-0.55, 0.0), (0.55, 0.0),
                                            (0.0, -0.32)]
    from brainsync.stimulus.config import diamond_positions, parse_config
    with pytest.raises(ValueError, match="needs 4 targets"):
        diamond_positions(3, 0.3)
    assert diamond_positions(4, 0.3) == [(0.0, 0.3), (-0.3, 0.0), (0.3, 0.0), (0.0, -0.3)]
    base = {"targets": {"freqs": [10.0, 12.0, 14.0, 16.0]}, "layout": {"shape": "diamond",
            "size": 0.2, "spacing": 0.3}}
    assert parse_config(base).shape == "diamond" and parse_config(base).spacing_x == 0.3
    with pytest.raises(ValueError, match="overlap"):
        parse_config({**base, "layout": {"shape": "diamond", "size": 0.25, "spacing": 0.2,
                                         "spacing_x": 0.2}})
    with pytest.raises(ValueError, match="shape"):
        parse_config({**base, "layout": {"shape": "ring"}})
    with pytest.raises(ValueError, match="spacing_x"):
        parse_config({**base, "layout": {"rows": 2, "cols": 2, "spacing_x": 0.5}})
    with pytest.raises(ValueError, match="fill"):
        parse_config({**base, "stimulus": {"fill": [0.0, 0.0, 0.0]}})
    from brainsync.stimulus.flicker import to_psychopy_fill
    assert to_psychopy_fill(1.0, (1.0, 0.0, 0.0)) == (1.0, -1.0, -1.0)
    assert to_psychopy_fill(0.5) == (0.0, 0.0, 0.0)


def test_vep_configs_place_their_text_clear_of_every_square():
    from brainsync.stimulus.display import text_boxes, text_overlaps
    for name in ("cvep_2", "cvep_3", "cvep_4", "tvep_4"):
        for aspect in (1.5, 1.6, 16 / 9):
            assert text_overlaps(load_config(name), aspect) == [], name
    slots, centre = text_boxes(load_config("cvep_4"))
    assert [b.side for b in slots] == ["right", "above", "below", "left"]
    assert centre.pos == (0.0, 0.0)
    # a 2 x 2 grid: each label on its outer side, the one-line centre between the rows
    slots, centre = text_boxes(load_config("tvep_4"))
    assert [b.side for b in slots] == ["left", "right", "left", "right"]
    assert centre.side == "centre" and centre.height < 3 * 0.035 * 1.25


def test_cvep_3_is_a_white_row_of_three_with_the_widest_lags():
    from brainsync.stimulus.display import text_boxes
    from brainsync.vep.codes import min_lag_distance
    cfg = load_config("cvep_3")
    assert cfg.labels == ("YES", "NO", "HELP") and cfg.fill == (1.0, 1.0, 1.0)
    assert [t.pos for t in cfg.targets] == [(-0.55, 0.0), (0.0, 0.0), (0.55, 0.0)]
    assert cfg.code.lag_frames == 21
    assert min_lag_distance(3, 21, cfg.code.cycle_frames) == 21
    assert 0.55 + cfg.reach <= cfg.display.aspect / 2
    slots, centre = text_boxes(cfg)
    assert all(b.side == "below" for b in slots)
    assert centre.side == "bottom"   # a square sits on the centre
