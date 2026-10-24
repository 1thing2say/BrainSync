"""Tests for the layout x_offset and the shipped Reels config; no window, no psychopy."""
from __future__ import annotations

import math
import sys
import types

import numpy as np
import pytest

from brainsync.stimulus import __main__ as cli
from brainsync.stimulus.config import available_configs, load_config, parse_config
from brainsync.stimulus.display import layout_overflow


def _cfg(**layout):
    return parse_config({"name": "t", "targets": {"freqs": [15.0, 17.0]},
                         "layout": {"rows": 1, "cols": 2, "size": 0.4, "spacing": 1.0,
                                    **layout}})


def test_reels_config():
    assert "reels" in available_configs()
    cfg = load_config("reels")
    assert cfg.name == "reels" and cfg.labels == ("up", "down")
    np.testing.assert_array_equal(cfg.freqs, [15.0, 17.0])
    np.testing.assert_array_equal(cfg.phases, [0.0, 0.0])
    assert cfg.waveform == "sine" and cfg.hold_cue
    assert (cfg.rows, cfg.cols, cfg.size, cfg.spacing, cfg.x_offset) == (2, 1, 0.3, 0.65, 0.55)
    assert cfg.targets[0].pos == pytest.approx((0.55, 0.325))     # up: top
    assert cfg.targets[1].pos == pytest.approx((0.55, -0.325))    # down: bottom
    d = cfg.display
    assert (d.refresh_hz, d.refresh_tolerance_hz, d.aspect, d.background) == (60.0, 1.0, 1.5, 0.0)
    # fits a 1.5:1 screen with the cue box: 0.55 + 0.165 <= 0.75 and 0.325 + 0.165 <= 0.5
    assert 0.55 + cfg.reach <= 0.75 and 0.325 + cfg.reach <= 0.5
    # right of a 9:16 video one screen high (half width 0.28), with the box
    assert 0.55 - cfg.reach > 9 / 16 / 2


def test_x_offset_defaults_to_zero_and_existing_configs_are_unchanged():
    two = load_config("two_targets")
    assert two.x_offset == 0.0
    assert [t.pos for t in two.targets] == [(-0.5, 0.0), (0.5, 0.0)]
    for f in (12, 15, 17):
        assert load_config(f"one_target_{f}").targets[0].pos == (0.0, 0.0)


def test_x_offset_shifts_every_target():
    base, moved = _cfg(), _cfg(spacing=0.5, x_offset=-0.2)
    assert [t.pos for t in moved.targets] == [pytest.approx((-0.45, 0.0)),
                                              pytest.approx((0.05, 0.0))]
    assert [t.pos[1] for t in base.targets] == [t.pos[1] for t in moved.targets]
    assert _cfg(x_offset=0).targets[0].pos == (-0.5, 0.0)          # integer is fine
    centred = _cfg(spacing=0.5, x_offset=0.25)
    assert centred.targets[0].pos == (0.0, 0.0)
    assert math.copysign(1.0, centred.targets[0].pos[0]) == 1.0      # no -0.0


def test_x_offset_counts_in_the_screen_edge_check():
    # 0.5 + 0.1 + reach 0.24 = 0.84 > 0.75
    with pytest.raises(ValueError, match=r"reaches 0\.840 .*horizontally"):
        _cfg(x_offset=0.1)
    with pytest.raises(ValueError, match="horizontally"):
        _cfg(x_offset=-0.1)
    wide = parse_config({"targets": {"freqs": [15.0, 17.0]},
                         "layout": {"size": 0.4, "spacing": 1.0, "x_offset": 0.1},
                         "display": {"aspect": 1.8}})
    assert wide.targets[1].pos == pytest.approx((0.6, 0.0))
    # a vertical pair that is too tall still fails, offset or not
    with pytest.raises(ValueError, match="vertically"):
        parse_config({"targets": {"freqs": [15.0, 17.0]},
                      "layout": {"rows": 2, "cols": 1, "size": 0.3, "spacing": 0.7,
                                 "x_offset": 0.5}})


@pytest.mark.parametrize("value, match", [("0.5", "must be a number"), (True, "must be a number"),
                                          (math.nan, "finite"), (math.inf, "finite")])
def test_bad_x_offset(value, match):
    with pytest.raises(ValueError, match=match):
        _cfg(x_offset=value)


def test_layout_overflow_sees_the_offset():
    cfg = load_config("reels")
    assert layout_overflow(cfg, 3024, 1964) is None            # 1.54:1 MacBook
    assert "cut off" in layout_overflow(cfg, 1440, 1080)       # 4:3: edge at 0.667 < 0.715


def test_plan_lists_reels_positions(capsys):
    assert cli.main(["-c", "reels", "--plan", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert "label 'up'  15.000 Hz" in out and "centre (+0.550, +0.325)" in out
    assert "label 'down'  17.000 Hz" in out and "centre (+0.550, -0.325)" in out


class _Stim:
    def __init__(self, win, **kw):
        self.kw = kw
        self.fillColor = kw.get("fillColor")
        self.drawn = 0

    def draw(self):
        self.drawn += 1


class _Window:
    size = (3024, 1964)

    def __init__(self, **kw):
        self.kw = kw

    def setMouseVisible(self, visible):
        pass


def test_psychopy_renderer_draws_reels_at_the_offset_positions(monkeypatch, capsys):
    from brainsync.stimulus.display import PsychoPyRenderer
    fake = types.ModuleType("psychopy")
    fake.visual = types.SimpleNamespace(Window=_Window, Rect=_Stim, TextStim=_Stim)
    fake.event = types.SimpleNamespace(getKeys=lambda keyList: [], clearEvents=lambda: None)
    monkeypatch.setitem(sys.modules, "psychopy", fake)
    r = PsychoPyRenderer(load_config("reels"))
    assert [s.kw["pos"] for s in r.squares] == [pytest.approx((0.55, 0.325)),
                                                pytest.approx((0.55, -0.325))]
    assert [b.kw["pos"] for b in r.boxes] == [s.kw["pos"] for s in r.squares]
    assert r.squares[0].kw["size"] == pytest.approx((0.3, 0.3))
    assert r.boxes[0].kw["size"] == pytest.approx((0.33, 0.33))
    r.draw(np.array([1.0, 0.0]), 1)
    assert [b.drawn for b in r.boxes] == [0, 1]
    assert capsys.readouterr().err == ""                         # fits: no cut-off warning
