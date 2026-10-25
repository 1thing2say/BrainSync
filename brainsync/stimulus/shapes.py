"""Target outlines: a square by default, or a circle, plus or arrow (no display needed).

A shape is a polygon centred on (0, 0) that fits inside the ``size`` x ``size`` box a square
target fills, so every layout, overlap, cue-box and text check made for squares still
holds. A shape lights only part of that box (``area_fraction``); less lit area means a
weaker visual response than a square of the same size. The four arrows are one outline
turned to point up, down, left or right, so they light the same area.
"""
from __future__ import annotations

import math

SHAPES = ("square", "circle", "plus", "arrow-up", "arrow-down", "arrow-left", "arrow-right")
DEFAULT_SHAPE = "square"
CIRCLE_POINTS = 64
PLUS_ARM = 1.0 / 3.0      # the plus's bar thickness, as a fraction of size
ARROW_SHAFT = 0.4         # the arrow's shaft thickness, as a fraction of size

Point = tuple[float, float]

# unit outlines (size 1), counter-clockwise
_ARROW_RIGHT: tuple[Point, ...] = (
    (-0.5, -ARROW_SHAFT / 2), (0.0, -ARROW_SHAFT / 2), (0.0, -0.5), (0.5, 0.0),
    (0.0, 0.5), (0.0, ARROW_SHAFT / 2), (-0.5, ARROW_SHAFT / 2))
_TURNS = {"arrow-right": 0, "arrow-up": 1, "arrow-left": 2, "arrow-down": 3}   # quarter turns


def _plus() -> tuple[Point, ...]:
    a, h = PLUS_ARM / 2, 0.5
    return ((-a, -h), (a, -h), (a, -a), (h, -a), (h, a), (a, a),
            (a, h), (-a, h), (-a, a), (-h, a), (-h, -a), (-a, -a))


def _turn(points: tuple[Point, ...], quarters: int) -> tuple[Point, ...]:
    out = points
    for _ in range(quarters):
        out = tuple((-y + 0.0, x + 0.0) for x, y in out)   # 90 degrees counter-clockwise
    return out


def _unit(name: str) -> tuple[Point, ...]:
    if name == "square":
        return ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5))
    if name == "circle":
        return tuple((0.5 * math.cos(2 * math.pi * k / CIRCLE_POINTS),
                      0.5 * math.sin(2 * math.pi * k / CIRCLE_POINTS))
                     for k in range(CIRCLE_POINTS))
    if name == "plus":
        return _plus()
    if name in _TURNS:
        return _turn(_ARROW_RIGHT, _TURNS[name])
    raise ValueError(f"unknown shape {name!r}; expected one of {SHAPES}")


def vertices(name: str, size: float) -> list[Point]:
    """The outline of shape ``name`` filling a ``size`` box centred on (0, 0)."""
    if not size > 0:   # also refuses nan
        raise ValueError(f"size must be positive, got {size}")
    return [(x * size, y * size) for x, y in _unit(name)]


def area_fraction(name: str) -> float:
    """How much of its ``size`` x ``size`` box shape ``name`` lights, in (0, 1]."""
    pts = _unit(name)
    twice = sum(x0 * y1 - x1 * y0
                for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
    return abs(twice) / 2.0
