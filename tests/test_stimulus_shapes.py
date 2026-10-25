"""Target shapes: outlines, areas and their config; nothing here opens a window."""
from __future__ import annotations

import math

import pytest

from brainsync.stimulus.config import config_warnings, load_config, parse_config
from brainsync.stimulus.shapes import SHAPES, area_fraction, vertices


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


def _shape_notes(cfg):
    return [w for w in config_warnings(cfg) if w.startswith("shapes:")]


@pytest.mark.parametrize("name", SHAPES)
def test_every_shape_fits_inside_the_square_box(name):
    pts = vertices(name, 0.3)
    assert len(pts) >= 4
    assert all(abs(x) <= 0.15 + 1e-12 and abs(y) <= 0.15 + 1e-12 for x, y in pts)
    assert max(x for x, _ in pts) == pytest.approx(0.15)   # touches the box's edges
    assert max(y for _, y in pts) == pytest.approx(0.15)


def test_areas():
    assert area_fraction("square") == pytest.approx(1.0)
    assert area_fraction("circle") == pytest.approx(math.pi / 4, rel=1e-2)
    assert area_fraction("plus") == pytest.approx(5.0 / 9.0)
    assert area_fraction("arrow-right") == pytest.approx(0.45)


def test_arrows_are_one_outline_turned():
    right = vertices("arrow-right", 1.0)
    assert max(right) == pytest.approx((0.5, 0.0))           # the tip
    assert max(vertices("arrow-up", 1.0), key=lambda p: p[1]) == pytest.approx((0.0, 0.5))
    assert min(vertices("arrow-left", 1.0)) == pytest.approx((-0.5, 0.0))
    assert min(vertices("arrow-down", 1.0), key=lambda p: p[1]) == pytest.approx((0.0, -0.5))
    areas = {area_fraction(f"arrow-{d}") for d in ("up", "down", "left", "right")}
    assert max(areas) - min(areas) < 1e-12


def test_unknown_shape_and_bad_size_raise():
    with pytest.raises(ValueError, match="unknown shape 'star'"):
        vertices("star", 0.3)
    with pytest.raises(ValueError, match="size must be positive"):
        vertices("plus", 0.0)


# -- [targets] shapes --------------------------------------------------------------------------

def test_shapes_default_to_square():
    assert [t.shape for t in _cfg().targets] == ["square", "square"]
    assert [t.shape for t in load_config("cvep_4").targets] == ["square"] * 4


def test_per_target_shapes_parse():
    cfg = _cfg(targets={"shapes": ["circle", "arrow-left"]})
    assert [t.shape for t in cfg.targets] == ["circle", "arrow-left"]
    assert [t.label for t in cfg.targets] == ["15", "17"]   # the rest is unchanged


def test_wrong_shape_count_and_unknown_shape_raise():
    with pytest.raises(ValueError, match=r"\[targets\] has 1 shapes for 2 target"):
        _cfg(targets={"shapes": ["plus"]})
    with pytest.raises(ValueError, match=r"\[targets\] shapes: target 1 has unknown shape "
                                         r"'star'; expected one of"):
        _cfg(targets={"shapes": ["plus", "star"]})
    with pytest.raises(ValueError, match=r"\[targets\] shapes must be a list of strings"):
        _cfg(targets={"shapes": "plus"})


def test_area_warning_for_arrows_and_plus_only():
    assert _shape_notes(_cfg()) == []
    assert _shape_notes(_cfg(targets={"shapes": ["circle", "square"]})) == []
    notes = _shape_notes(_cfg(targets={"shapes": ["arrow-up", "plus"]}))
    assert len(notes) == 1   # one note per config, not per target
    assert "target 0 (arrow-up), target 1 (plus)" in notes[0]
    assert "45%/56%" in notes[0] and "weaker visual response" in notes[0]
    assert "[layout] size" in notes[0]
    one = _shape_notes(_cfg(targets={"shapes": ["square", "arrow-down"]}))
    assert len(one) == 1 and "target 1 (arrow-down) light 45%" in one[0]
    assert "target 0" not in one[0]


def test_shaped_cvep_configs_load():
    arrows = load_config("cvep_4_arrows")
    assert [t.shape for t in arrows.targets] == ["arrow-up", "arrow-left", "arrow-right",
                                                 "arrow-down"]
    base = load_config("cvep_4")
    assert arrows.labels == base.labels and arrows.code == base.code
    assert [t.pos for t in arrows.targets] == [t.pos for t in base.targets]
    assert arrows.size > base.size
    plus = load_config("cvep_3_plus")
    assert [t.shape for t in plus.targets] == ["plus"] * 3
    base = load_config("cvep_3")
    assert plus.labels == base.labels and plus.code == base.code and plus.size > base.size
    assert len(_shape_notes(arrows)) == 1 and len(_shape_notes(plus)) == 1
    assert "light 45% of" in _shape_notes(arrows)[0]   # four arrows, one area
