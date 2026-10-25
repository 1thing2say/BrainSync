"""Stimulus configs (TOML), their validation, and the cued trial order.

A config names the flicker targets, where they sit, how they flicker and the session
timing. Positions and sizes are in screen heights from the screen centre (PsychoPy
``height`` units), so a fullscreen window spans y in [-0.5, 0.5] and x in
[-aspect/2, aspect/2]. ``x_offset`` (default 0) shifts the whole grid sideways, for a
layout that must leave the middle of the screen free (the Reels pair sits in the right
margin beside the video).

    name = "two_targets"
    [targets]   freqs = [15.0, 17.0]; phases_pi = [0.0, 0.0]; labels = ["15", "17"]
    [layout]    rows = 1; cols = 2; size = 0.4; spacing = 1.0; x_offset = 0.0
    [stimulus]  waveform = "sine"; hold_cue = true; depth = 1.0
    [display]   refresh_hz = 60.0; refresh_tolerance_hz = 1.0; screen = 0; background = 0.0
                aspect = 1.5
    [session]   blocks = 10; cue_s = 1.0; stim_s = 4.0; rest_s = 3.0; shuffle = true
    [card]      height = 0.6; flash_s = 0.35; flash_colors = ["#d0605a", "#5aae6e"]

The optional [card] table sets up the BrainSwipe profile card drawn at the centre between
the targets, and the box flash that confirms a swipe; configs without it are unchanged.
A flash colour is "#rrggbb" or three values in [-1, 1] (PsychoPy rgb), one per target.

Instead of the grid, [layout] positions places every target by hand, one [x, y] centre
per target in screen heights (the four-square diamond of four_diamond.toml):

    [layout]    size = 0.3; positions = [[0, 0.33], [-0.55, 0], [0.55, 0], [0, -0.33]]

rows, cols and spacing then make no sense and are refused; x_offset still shifts them.

[stimulus] fixation (default false) draws a small mark, fixation_size screen heights
across, at the centre of every square, so gaze has a point to rest on (it drifts off a
plain square after about 2 s). labels_during_flicker (default true) keeps the per-target
sentences and the centre text a decoder sends (see brainsync.stimulus.feedback) on screen
while the targets flicker; false hides them during the flicker, in case drawing text then
drops frames.

[stimulus] depth (default 1.0, in (0, 1]) is the flicker's modulation depth: luminance
swings around mid-grey 0.5 by depth / 2, so 1.0 is full black-to-white and 0.5 is a
gentler 0.25-0.75 flicker with the same mean.

A session can also mix flicker conditions (``conditions``, set from the stimulus's
--conditions, never from TOML): each one is a waveform and depth, such as "square:1" or
"sine:0.5". Every block then cues every (condition, target) pair once, and each trial
flickers with its own condition instead of [stimulus] waveform and depth.

Two optional tables pick another VEP coding (Bin et al. 2009) instead of frequencies; a
config has at most one of them, and with either ``[targets] freqs`` may be left out (the
targets are then ``labels``, or ``count`` of them):

    [code]  length = 63; lag_frames = 4; frames_per_bit = 1    # c-VEP: one m-sequence,
                                                               # target k delayed k * lag
    [time]  flash_frames = 2; gap_frames = 13; order = "random"  # t-VEP: flashes in
                                                               # rounds, never overlapping

``paradigm`` is then "code" or "time" ("freq" for an ordinary flicker config), and the
targets' ``freq`` is nan.
"""
from __future__ import annotations

import dataclasses
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..vep.codes import ORDERS, degree_for_length, flash_rate_hz
from .flicker import WAVEFORMS, photosensitive, square_duty_even

CONFIG_DIR = Path(__file__).resolve().parent / "configs"
DEFAULT_CONFIG = "two_targets"
PARADIGMS = ("freq", "time", "code")
_TWO_PI = 2.0 * math.pi
_PHASE_TOL = 1e-9

_KEYS: dict[str, frozenset[str]] = {
    "targets": frozenset({"freqs", "phases_pi", "labels", "count"}),
    "code": frozenset({"length", "lag_frames", "frames_per_bit"}),
    "time": frozenset({"flash_frames", "gap_frames", "order"}),
    "layout": frozenset({"rows", "cols", "size", "spacing", "x_offset", "shape", "spacing_x",
                         "positions"}),
    "stimulus": frozenset({"waveform", "hold_cue", "cue_color", "cue_scale", "cue_line_px",
                           "depth", "fill", "fixation", "fixation_size",
                           "labels_during_flicker"}),
    "display": frozenset({"refresh_hz", "refresh_tolerance_hz", "screen", "background",
                          "aspect"}),
    "session": frozenset({"blocks", "cue_s", "stim_s", "rest_s", "shuffle"}),
    "card": frozenset({"height", "flash_s", "flash_colors"}),
}
# Card overlap checks assume this width/height: the profile cards are 600 x 900 px portraits.
CARD_ASPECT = 2.0 / 3.0

Color = tuple[float, float, float]


@dataclass(frozen=True)
class Target:
    """One flicker target; ``phase`` in radians, ``pos`` (x, y) in screen heights."""

    index: int
    freq: float
    phase: float
    label: str
    pos: tuple[float, float]


@dataclass(frozen=True)
class Display:
    """Monitor settings; ``aspect`` is the narrowest width/height the layout must fit."""

    refresh_hz: float = 60.0
    refresh_tolerance_hz: float = 1.0
    screen: int = 0
    background: float = 0.0
    aspect: float = 1.5


@dataclass(frozen=True)
class Session:
    blocks: int = 10
    cue_s: float = 1.0
    stim_s: float = 4.0
    rest_s: float = 3.0
    shuffle: bool = True


@dataclass(frozen=True)
class Card:
    """The centred profile card and the swipe flash.

    ``height`` in screen heights (the width follows the image's aspect); ``flash_s`` is how
    long a flashed target's box stays lit; ``flash_colors`` holds one PsychoPy rgb colour
    per target, or is empty to flash in the cue colour.
    """

    height: float = 0.6
    flash_s: float = 0.35
    flash_colors: tuple[Color, ...] = ()


@dataclass(frozen=True)
class Condition:
    """One flicker condition of a mixed session: a waveform and a modulation depth."""

    waveform: str = "sine"
    depth: float = 1.0

    @property
    def spec(self) -> str:
        """``<waveform>:<depth>``, the form parse_conditions reads."""
        return f"{self.waveform}:{self.depth:g}"

    @property
    def label(self) -> str:
        return f"{self.waveform} {self.depth:g}"


@dataclass(frozen=True)
class CodeSpec:
    """c-VEP: one m-sequence of ``length`` bits, each bit shown for ``frames_per_bit``
    frames; target k shows it delayed by ``k * lag_frames`` frames."""

    length: int = 63
    lag_frames: int = 4
    frames_per_bit: int = 1

    @property
    def cycle_frames(self) -> int:
        return self.length * self.frames_per_bit


@dataclass(frozen=True)
class TimeSpec:
    """t-VEP: flashes of ``flash_frames`` bright frames in slots of ``flash_frames +
    gap_frames``; every round flashes each target once, in ``order`` (roundrobin or
    random)."""

    flash_frames: int = 2
    gap_frames: int = 13
    order: str = "random"

    @property
    def slot_frames(self) -> int:
        return self.flash_frames + self.gap_frames


@dataclass(frozen=True)
class StimulusConfig:
    name: str
    targets: tuple[Target, ...]
    size: float
    waveform: str = "sine"
    hold_cue: bool = False
    depth: float = 1.0   # modulation depth: luminance swings 0.5 +/- depth / 2
    cue_color: tuple[float, float, float] = (1.0, -1.0, -1.0)
    cue_scale: float = 1.2
    cue_line_px: float = 8.0
    display: Display = Display()
    session: Session = Session()
    rows: int = 1
    cols: int = 1
    spacing: float = 0.0
    x_offset: float = 0.0   # screen heights the grid is shifted right (negative: left)
    shape: str = "grid"     # or "diamond": 4 targets at top, left, right, bottom
    spacing_x: float = 0.0  # diamond: the left and right squares' distance from the centre
                            # (default: spacing); the screen is wider than it is high
    fill: tuple[float, float, float] = (1.0, 1.0, 1.0)   # per-channel tint of the squares
    path: Path | None = None
    card: Card | None = None   # None: the config has no [card] table
    conditions: tuple[Condition, ...] = ()   # mixed per trial; empty: waveform and depth
    code: CodeSpec | None = None   # the [code] table (c-VEP), else None
    time: TimeSpec | None = None   # the [time] table (t-VEP), else None
    positioned: bool = False   # True: [layout] positions placed the targets, not the grid
    fixation: bool = False     # a small mark at the centre of every square
    fixation_size: float = 0.03   # the mark's width, screen heights
    labels_during_flicker: bool = True   # decoder-sent text stays up while flickering

    @property
    def paradigm(self) -> str:
        """"code" (c-VEP), "time" (t-VEP) or "freq" (flicker frequencies)."""
        return "code" if self.code is not None else "time" if self.time is not None else "freq"

    @property
    def coded(self) -> bool:
        """True for a [code] or [time] config, whose targets have no flicker frequency."""
        return self.paradigm != "freq"

    @property
    def card_spec(self) -> Card:
        """The [card] settings, or their defaults when the config has none."""
        return self.card if self.card is not None else Card()

    def flash_color(self, k: int) -> Color:
        """Target ``k``'s flash colour: its [card] flash_colors entry, else the cue colour."""
        colors = self.card_spec.flash_colors
        return colors[k] if colors else self.cue_color

    @property
    def n_targets(self) -> int:
        return len(self.targets)

    @property
    def freqs(self) -> np.ndarray:
        return np.array([t.freq for t in self.targets], dtype=float)

    @property
    def phases(self) -> np.ndarray:
        """Target phases in radians."""
        return np.array([t.phase for t in self.targets], dtype=float)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(t.label for t in self.targets)

    @property
    def refresh(self) -> float:
        return self.display.refresh_hz

    @property
    def reach(self) -> float:
        """Half the side of a target's footprint, cue box included."""
        return self.size * self.cue_scale / 2.0


@dataclass(frozen=True)
class Trial:
    """One cued trial: ``index`` counts trials across the session; ``condition`` indexes
    the config's conditions (0 when it has none)."""

    index: int
    block: int
    target: int
    condition: int = 0


# -- typed TOML access ----------------------------------------------------------------------

class _Table:
    """One TOML table with type-checked getters; unknown keys are refused up front."""

    def __init__(self, data: Mapping[str, Any], name: str) -> None:
        raw = data.get(name, {})
        if not isinstance(raw, Mapping):
            raise ValueError(f"[{name}] must be a table, got {raw!r}")
        extra = sorted(set(raw) - _KEYS[name])
        if extra:
            raise ValueError(f"[{name}] has unknown key(s) {extra}; "
                             f"known keys: {sorted(_KEYS[name])}")
        self.raw = raw
        self.name = name

    def __contains__(self, key: str) -> bool:
        return key in self.raw

    def _where(self, key: str) -> str:
        return f"[{self.name}] {key}"

    def number(self, key: str, default: float) -> float:
        v = self.raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{self._where(key)} must be a number, got {v!r}")
        return float(v)

    def integer(self, key: str, default: int) -> int:
        v = self.raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{self._where(key)} must be an integer, got {v!r}")
        return v

    def flag(self, key: str, default: bool) -> bool:
        v = self.raw.get(key, default)
        if not isinstance(v, bool):
            raise ValueError(f"{self._where(key)} must be true or false, got {v!r}")
        return v

    def text(self, key: str, default: str) -> str:
        v = self.raw.get(key, default)
        if not isinstance(v, str):
            raise ValueError(f"{self._where(key)} must be a string, got {v!r}")
        return v

    def numbers(self, key: str, default: list[float] | None = None) -> list[float]:
        v = self.raw.get(key, default)
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"{self._where(key)} must be a list of numbers, got {v!r}")
        bad = [x for x in v if isinstance(x, bool) or not isinstance(x, (int, float))]
        if bad:
            raise ValueError(f"{self._where(key)} must hold numbers only, got "
                             f"{bad[0]!r} in {v!r}")
        return [float(x) for x in v]

    def texts(self, key: str, default: list[str]) -> list[str]:
        v = self.raw.get(key, default)
        if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{self._where(key)} must be a list of strings, got {v!r}")
        return list(v)

    def points(self, key: str) -> list[tuple[float, float]]:
        v = self.raw.get(key, [])
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"{self._where(key)} must be a list of [x, y] pairs, got {v!r}")
        out = []
        for p in v:
            if (not isinstance(p, (list, tuple)) or len(p) != 2
                    or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in p)
                    or not all(math.isfinite(x) for x in p)):
                raise ValueError(f"{self._where(key)} must hold [x, y] pairs of finite "
                                 f"numbers, got {p!r}")
            out.append((float(p[0]), float(p[1])))
        return out

    def colors(self, key: str) -> tuple[Color, ...]:
        v = self.raw.get(key, [])
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"{self._where(key)} must be a list of colours, got {v!r}")
        return tuple(parse_color(c, f"{self._where(key)}[{i}]") for i, c in enumerate(v))


def parse_color(value: Any, where: str = "colour") -> Color:
    """PsychoPy rgb (-1..1) from "#rrggbb" or three numbers already in [-1, 1]."""
    if isinstance(value, str):
        text = value.strip()
        digits = text[1:] if text.startswith("#") else ""
        try:
            if len(digits) != 6:
                raise ValueError
            rgb = [int(digits[i:i + 2], 16) for i in (0, 2, 4)]
        except ValueError:
            raise ValueError(f"{where} must be \"#rrggbb\" or three numbers in [-1, 1], "
                             f"got {value!r}") from None
        r, g, b = (2.0 * c / 255.0 - 1.0 for c in rgb)
        return (r, g, b)
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in value)):
        raise ValueError(f"{where} must be \"#rrggbb\" or three numbers in [-1, 1], "
                         f"got {value!r}")
    if not all(-1.0 <= x <= 1.0 for x in value):
        raise ValueError(f"{where} must hold values in [-1, 1], got {list(value)}")
    return (float(value[0]), float(value[1]), float(value[2]))


# -- loading --------------------------------------------------------------------------------

def available_configs() -> list[str]:
    """Names of the configs shipped in configs/."""
    return sorted(p.stem for p in CONFIG_DIR.glob("*.toml"))


def resolve_config(name_or_path: str | Path) -> Path:
    """A config path: an existing file, else a bare name (with or without .toml) in configs/."""
    p = Path(name_or_path).expanduser()
    if p.is_file():
        return p
    if p.parent == Path("."):
        shipped = CONFIG_DIR / (p.name if p.suffix == ".toml" else p.name + ".toml")
        if shipped.is_file():
            return shipped
    raise FileNotFoundError(
        f"stimulus config {str(name_or_path)!r} not found; shipped configs: "
        f"{', '.join(available_configs())}")


def load_config(name_or_path: str | Path = DEFAULT_CONFIG) -> StimulusConfig:
    """Read and validate a TOML config (a path, or a name in configs/)."""
    path = resolve_config(name_or_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return parse_config(data, default_name=path.stem, path=path)


def diamond_positions(n: int, spacing: float,
                      spacing_x: float | None = None) -> list[tuple[float, float]]:
    """Four targets at top, left, right, bottom (the layout of the four-target SSVEP
    studies, e.g. Nakanishi et al. 2013): ``spacing`` from the centre up and down,
    ``spacing_x`` (default ``spacing``) to the sides."""
    if n != 4:
        raise ValueError(f"[layout] shape = \"diamond\" needs 4 targets, got {n}")
    sy = float(spacing)
    sx = sy if spacing_x is None else float(spacing_x)
    return [(0.0, sy), (-sx, 0.0), (sx, 0.0), (0.0, -sy)]


def grid_positions(n: int, rows: int, cols: int, spacing: float) -> list[tuple[float, float]]:
    """Centres of ``n`` cells filled row by row from the top left, grid centred on (0, 0)."""
    if rows < 1 or cols < 1:
        raise ValueError(f"layout needs rows >= 1 and cols >= 1, got rows={rows} cols={cols}")
    if n > rows * cols:
        raise ValueError(f"layout rows={rows} cols={cols} has {rows * cols} cell(s), "
                         f"too few for {n} targets")
    cell = np.arange(n)
    xs = (cell % cols - 0.5 * (cols - 1)) * spacing
    ys = (0.5 * (rows - 1) - cell // cols) * spacing
    return [(float(x) + 0.0, float(y) + 0.0) for x, y in zip(xs, ys)]  # + 0.0 drops -0.0


def explicit_positions(value: Any, n: int) -> list[tuple[float, float]]:
    """[layout] positions: one [x, y] pair per target in screen heights from the centre
    (y up), for layouts a grid cannot express, such as the four-target diamond."""
    if not isinstance(value, (list, tuple)) or len(value) != n:
        raise ValueError(f"[layout] positions must list {n} [x, y] pairs (one per target), "
                         f"got {value!r}")
    out = []
    for k, pair in enumerate(value):
        ok = (isinstance(pair, (list, tuple)) and len(pair) == 2
              and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pair))
        if not ok or not all(math.isfinite(float(v)) for v in pair):
            raise ValueError(f"[layout] positions[{k}] must be two finite numbers, got {pair!r}")
        out.append((float(pair[0]) + 0.0, float(pair[1]) + 0.0))
    return out


def parse_config(data: Mapping[str, Any], default_name: str = "config",
                 path: Path | None = None) -> StimulusConfig:
    """Build a StimulusConfig from parsed TOML, raising ValueError on bad values or types."""
    extra = sorted(set(data) - set(_KEYS) - {"name"})
    if extra:
        raise ValueError(f"unknown top-level key(s) {extra}; expected 'name' and the tables "
                         f"{sorted(_KEYS)}")
    name = data.get("name", default_name)
    if not isinstance(name, str):
        raise ValueError(f"name must be a string, got {name!r}")
    tg, lay, stim, disp, ses = (_Table(data, k) for k in
                                ("targets", "layout", "stimulus", "display", "session"))

    if "code" in data and "time" in data:
        raise ValueError("a config has [code] (c-VEP) or [time] (t-VEP), not both")
    code = time = None
    if "code" in data:
        ct = _Table(data, "code")
        code = CodeSpec(length=ct.integer("length", 63), lag_frames=ct.integer("lag_frames", 4),
                        frames_per_bit=ct.integer("frames_per_bit", 1))
    if "time" in data:
        tt = _Table(data, "time")
        time = TimeSpec(flash_frames=tt.integer("flash_frames", 2),
                        gap_frames=tt.integer("gap_frames", 13), order=tt.text("order", "random"))
    coded = code is not None or time is not None

    if "freqs" in tg:
        freqs = tg.numbers("freqs")
        n = len(freqs)
        if n == 0:
            raise ValueError("[targets] freqs is empty")
        if "count" in tg and tg.integer("count", n) != n:
            raise ValueError(f"[targets] count {tg.integer('count', n)} disagrees with "
                             f"{n} freqs")
    elif not coded:
        raise ValueError("[targets] needs freqs")
    else:
        n = tg.integer("count", len(tg.texts("labels", [])))
        if n < 1:
            raise ValueError("[targets] needs labels or count with [code] or [time]")
        freqs = [math.nan] * n
    phases_pi = tg.numbers("phases_pi", [0.0] * n)
    labels = tg.texts("labels", [str(k) for k in range(n)] if coded
                      else [f"{f:g}" for f in freqs])
    for what, got in (("phases_pi", len(phases_pi)), ("labels", len(labels))):
        if got != n:
            raise ValueError(f"[targets] has {got} {what} for {n} target(s)")

    positioned = "positions" in lay
    if positioned:
        grid = sorted(k for k in ("rows", "cols", "spacing") if k in lay)
        if grid:
            raise ValueError(f"[layout] positions places the targets by hand; drop {grid}")
    rows = lay.integer("rows", 1)
    cols = lay.integer("cols", n)
    size = lay.number("size", 0.4)
    spacing = lay.number("spacing", 1.0 if n > 1 and not positioned else 0.0)
    x_offset = lay.number("x_offset", 0.0)
    if not math.isfinite(x_offset):
        raise ValueError(f"[layout] x_offset must be finite, got {x_offset}")
    shape = lay.text("shape", "grid")
    if shape not in ("grid", "diamond"):
        raise ValueError(f'[layout] shape must be "grid" or "diamond", got {shape!r}')
    if positioned and "shape" in lay:
        raise ValueError("[layout] positions places the targets by hand; drop shape")
    spacing_x = lay.number("spacing_x", spacing)
    if "spacing_x" in data.get("layout", {}) and shape != "diamond":
        raise ValueError('[layout] spacing_x is for shape = "diamond"')
    fill = stim.numbers("fill", [1.0, 1.0, 1.0])
    if len(fill) != 3 or not all(0.0 <= v <= 1.0 for v in fill) or max(fill) <= 0.0:
        raise ValueError(f"[stimulus] fill must be three values in [0, 1], not all 0, "
                         f"got {fill}")
    if positioned:
        given = lay.points("positions")
        if len(given) != n:
            raise ValueError(f"[layout] has {len(given)} positions for {n} target(s)")
    elif shape == "diamond":
        given = diamond_positions(n, spacing, spacing_x)
    else:
        given = grid_positions(n, rows, cols, spacing)

    color = stim.numbers("cue_color", [1.0, -1.0, -1.0])
    if len(color) != 3 or not all(-1.0 <= v <= 1.0 for v in color):
        raise ValueError(f"[stimulus] cue_color must be three values in [-1, 1], got {color}")

    card = None
    if "card" in data:
        ct = _Table(data, "card")
        card = Card(height=ct.number("height", 0.6), flash_s=ct.number("flash_s", 0.35),
                    flash_colors=ct.colors("flash_colors"))

    positions = [(x + x_offset + 0.0, y + 0.0) for x, y in given]
    cfg = StimulusConfig(
        name=name,
        targets=tuple(Target(index=k, freq=freqs[k], phase=phases_pi[k] * math.pi,
                             label=labels[k], pos=positions[k]) for k in range(n)),
        size=size,
        waveform=stim.text("waveform", "sine"),
        hold_cue=stim.flag("hold_cue", False),
        depth=stim.number("depth", 1.0),
        cue_color=(color[0], color[1], color[2]),
        cue_scale=stim.number("cue_scale", 1.2),
        cue_line_px=stim.number("cue_line_px", 8.0),
        display=Display(refresh_hz=disp.number("refresh_hz", 60.0),
                        refresh_tolerance_hz=disp.number("refresh_tolerance_hz", 1.0),
                        screen=disp.integer("screen", 0),
                        background=disp.number("background", 0.0),
                        aspect=disp.number("aspect", 1.5)),
        session=Session(blocks=ses.integer("blocks", 10),
                        cue_s=ses.number("cue_s", 1.0),
                        stim_s=ses.number("stim_s", 4.0),
                        rest_s=ses.number("rest_s", 3.0),
                        shuffle=ses.flag("shuffle", True)),
        rows=rows,
        cols=cols,
        spacing=spacing,
        x_offset=x_offset,
        shape=shape,
        spacing_x=spacing_x,
        fill=(fill[0], fill[1], fill[2]),
        path=path,
        card=card,
        code=code,
        time=time,
        positioned=positioned,
        fixation=stim.flag("fixation", False),
        fixation_size=stim.number("fixation_size", 0.03),
        labels_during_flicker=stim.flag("labels_during_flicker", True),
    )
    validate(cfg)
    return cfg


# -- checks ---------------------------------------------------------------------------------

def same_phase(a: float, b: float, tol: float = _PHASE_TOL) -> bool:
    """True when two phases (radians) are equal modulo 2 pi."""
    return abs(math.remainder(a - b, _TWO_PI)) <= tol


def _check_targets(cfg: StimulusConfig) -> None:
    nyquist = cfg.refresh / 2.0
    for t in cfg.targets:
        if t.freq <= 0:
            raise ValueError(f"target {t.index}: frequency must be positive, got {t.freq}")
        if t.freq >= nyquist:
            raise ValueError(f"target {t.index}: {t.freq:g} Hz is not below half the "
                             f"{cfg.refresh:g} Hz refresh ({nyquist:g} Hz)")
    for i, a in enumerate(cfg.targets):
        for b in cfg.targets[i + 1:]:
            if math.isclose(a.freq, b.freq, abs_tol=1e-9) and same_phase(a.phase, b.phase):
                raise ValueError(f"targets {a.index} and {b.index} share frequency "
                                 f"{a.freq:g} Hz and phase ({a.phase / math.pi:g} pi vs "
                                 f"{b.phase / math.pi:g} pi), so they are the same stimulus")


def _check_coding(cfg: StimulusConfig) -> None:
    """The [code] or [time] table against the target count."""
    if any(math.isfinite(t.freq) for t in cfg.targets):
        raise ValueError(f"[targets] freqs do not apply to a [{cfg.paradigm}] config; drop "
                         "them (use labels or count)")
    if cfg.code is not None:
        c = cfg.code
        degree_for_length(c.length)   # raises for anything but 2^n - 1
        for key in ("lag_frames", "frames_per_bit"):
            if getattr(c, key) < 1:
                raise ValueError(f"[code] {key} must be >= 1, got {getattr(c, key)}")
        if (cfg.n_targets - 1) * c.lag_frames >= c.cycle_frames:
            raise ValueError(f"[code] {cfg.n_targets} targets {c.lag_frames} frames apart "
                             f"need {(cfg.n_targets - 1) * c.lag_frames + 1} frames, but one "
                             f"cycle has {c.cycle_frames}; fewer targets, a shorter "
                             "lag_frames or a longer length")
    if cfg.time is not None:
        t = cfg.time
        if t.flash_frames < 1:
            raise ValueError(f"[time] flash_frames must be >= 1, got {t.flash_frames}")
        if t.gap_frames < 0:
            raise ValueError(f"[time] gap_frames must be >= 0, got {t.gap_frames}")
        if t.order not in ORDERS:
            raise ValueError(f"[time] unknown order {t.order!r}; expected one of {ORDERS}")


def _check_geometry(cfg: StimulusConfig) -> None:
    if cfg.size <= 0:
        raise ValueError(f"[layout] size must be positive, got {cfg.size}")
    if cfg.cue_scale < 1.0:
        raise ValueError(f"[stimulus] cue_scale must be >= 1 (box outside the target), "
                         f"got {cfg.cue_scale}")
    if cfg.n_targets > 1 and not cfg.positioned and cfg.spacing < cfg.size:
        raise ValueError(f"[layout] spacing {cfg.spacing:g} < size {cfg.size:g}: "
                         "targets would overlap")
    if cfg.shape == "diamond" and cfg.spacing_x < cfg.size and cfg.spacing < cfg.size:
        raise ValueError(f"[layout] diamond spacing {cfg.spacing:g} and spacing_x "
                         f"{cfg.spacing_x:g} are both under size {cfg.size:g}: "
                         "neighbouring squares would overlap")
    for i, a in enumerate(cfg.targets):
        for b in cfg.targets[i + 1:]:
            if (abs(a.pos[0] - b.pos[0]) < cfg.size - 1e-9
                    and abs(a.pos[1] - b.pos[1]) < cfg.size - 1e-9):
                raise ValueError(f"[layout] targets {a.index} and {b.index} would overlap: "
                                 f"centres {a.pos} and {b.pos} are closer than the "
                                 f"{cfg.size:g} side")
    if not 0.0 < cfg.fixation_size < cfg.size:   # also refuses nan
        raise ValueError(f"[stimulus] fixation_size must be in (0, size {cfg.size:g}), "
                         f"got {cfg.fixation_size}")
    aspect = cfg.display.aspect
    if aspect <= 0:
        raise ValueError(f"[display] aspect must be positive, got {aspect}")
    xs = [abs(t.pos[0]) for t in cfg.targets]
    ys = [abs(t.pos[1]) for t in cfg.targets]
    top, side = max(ys) + cfg.reach, max(xs) + cfg.reach
    if top > 0.5 + 1e-9:
        raise ValueError(f"layout reaches {top:.3f} screen heights from the centre "
                         f"vertically (cue box included), past the screen edge at 0.5")
    if side > aspect / 2 + 1e-9:
        raise ValueError(f"layout reaches {side:.3f} screen heights from the centre "
                         f"horizontally (cue box included), past the edge at {aspect / 2:g} "
                         f"of a {aspect:g}:1 screen; reduce spacing or size")


def _check_card(cfg: StimulusConfig) -> None:
    c = cfg.card_spec
    if not 0.0 < c.height <= 1.0:
        raise ValueError(f"[card] height must be in (0, 1] screen heights, got {c.height}")
    # TOML accepts inf and nan; caught here, not by n_frames once the window is up
    if not (math.isfinite(c.flash_s) and c.flash_s >= 0):
        raise ValueError(f"[card] flash_s must be finite and >= 0, got {c.flash_s}")
    if c.flash_colors and len(c.flash_colors) != cfg.n_targets:
        raise ValueError(f"[card] has {len(c.flash_colors)} flash_colors for "
                         f"{cfg.n_targets} targets")


def card_overlap(cfg: StimulusConfig, aspect: float = CARD_ASPECT) -> str | None:
    """Warning text when the centred card would cover a target or its cue box.

    The card is ``card_spec.height`` high and ``aspect`` times that wide, centred on (0, 0).
    A card over a square would not change its luminance (the squares are drawn on top),
    but it would cover part of the cue box and pull gaze off the target.
    """
    c = cfg.card_spec
    half_w, half_h = c.height * aspect / 2.0, c.height / 2.0
    for t in cfg.targets:
        x, y = abs(t.pos[0]), abs(t.pos[1])
        for part, half in (("square", cfg.size / 2.0), ("cue box", cfg.reach)):
            if half_w > x - half + 1e-9 and half_h > y - half + 1e-9:
                return (f"card {half_w * 2:.3f} x {c.height:.3f} screen heights reaches "
                        f"x = +/-{half_w:.3f}, y = +/-{half_h:.3f} and overlaps target "
                        f"{t.index}'s {part} (inner edge at x = {max(x - half, 0.0):.3f}); "
                        "lower [card] height")
    return None


def _check_flicker(waveform: str, depth: float, where: str) -> None:
    if waveform not in WAVEFORMS:
        raise ValueError(f"{where} unknown waveform {waveform!r}; expected one of {WAVEFORMS}")
    if not 0.0 < depth <= 1.0:   # also refuses nan
        raise ValueError(f"{where} depth must be in (0, 1], got {depth}")


def parse_conditions(text: str) -> tuple[Condition, ...]:
    """Comma-separated ``<waveform>[:<depth>]`` specs, e.g. "square:1,sine:1,sine:0.5"."""
    out: list[Condition] = []
    for part in (p.strip() for p in text.split(",")):
        if not part:
            raise ValueError(f"empty condition in {text!r}")
        waveform, _, depth_text = part.partition(":")
        try:
            depth = float(depth_text) if depth_text else 1.0
        except ValueError:
            raise ValueError(f"condition {part!r}: depth {depth_text!r} is not a number") \
                from None
        _check_flicker(waveform, depth, f"condition {part!r}:")
        cond = Condition(waveform, depth)
        if cond in out:
            raise ValueError(f"condition {cond.spec} is listed twice in {text!r}")
        out.append(cond)
    return tuple(out)


def validate(cfg: StimulusConfig) -> None:
    """Raise ValueError for a config that cannot be shown correctly."""
    _check_flicker(cfg.waveform, cfg.depth, "[stimulus]")
    for c in cfg.conditions:
        _check_flicker(c.waveform, c.depth, f"condition {c.spec}:")
    d = cfg.display
    if d.refresh_hz <= 0:
        raise ValueError(f"[display] refresh_hz must be positive, got {d.refresh_hz}")
    if d.refresh_tolerance_hz < 0:
        raise ValueError(f"[display] refresh_tolerance_hz must be >= 0, got "
                         f"{d.refresh_tolerance_hz}")
    if not 0.0 <= d.background <= 1.0:
        raise ValueError(f"[display] background must be a luminance in [0, 1], got "
                         f"{d.background}")
    if cfg.coded:
        _check_coding(cfg)
    else:
        _check_targets(cfg)
    _check_geometry(cfg)
    _check_card(cfg)
    s = cfg.session
    if s.blocks < 1:
        raise ValueError(f"[session] blocks must be >= 1, got {s.blocks}")
    if s.stim_s <= 0:
        raise ValueError(f"[session] stim_s must be positive, got {s.stim_s}")
    for key in ("cue_s", "rest_s"):
        value = getattr(s, key)
        if value < 0:
            raise ValueError(f"[session] {key} must be >= 0, got {value}")


def config_warnings(cfg: StimulusConfig) -> list[str]:
    """Human-readable warnings: photosensitive band, uneven square-wave duty cycles, and
    (with a [card] table) a card that covers a target, assuming CARD_ASPECT."""
    notes: list[str] = []
    risky = sorted({t.freq for t in cfg.targets if photosensitive(t.freq)})
    if risky:
        listed = "/".join(f"{f:g}" for f in risky)
        notes.append(f"photosensitivity: {listed} Hz lies inside 15-25 Hz, where flicker "
                     "is most likely to provoke a seizure in photosensitive people; ask "
                     "about epilepsy before every session")
    if cfg.code is not None:
        notes.append("photosensitivity: an m-sequence flicker is broadband and includes the "
                     "15-25 Hz band most likely to provoke a seizure in photosensitive "
                     "people; ask about epilepsy before every session")
    elif cfg.time is not None:
        hz = flash_rate_hz(cfg.n_targets, cfg.time.flash_frames, cfg.time.gap_frames,
                           cfg.refresh)
        if hz > 4.0:
            notes.append(f"t-VEP: each target flashes {hz:.2f} times a second, above the "
                         "4 Hz where consecutive flash responses start to overlap; raise "
                         "[time] gap_frames")
    if not cfg.coded and (cfg.waveform == "square"
                          or any(c.waveform == "square" for c in cfg.conditions)):
        hz = cfg.refresh
        for t in cfg.targets:
            if not square_duty_even(t.freq, hz):
                notes.append(f"target {t.index} ({t.freq:g} Hz): {hz / t.freq:.3g} frames "
                             f"per cycle at {hz:g} Hz cannot split evenly into on and off, "
                             "so the square duty cycle is uneven; prefer waveform = \"sine\"")
    if cfg.card is not None:
        overlap = card_overlap(cfg)
        if overlap:
            notes.append(overlap)
    return notes


# -- overrides ------------------------------------------------------------------------------

_SESSION_FIELDS = frozenset(f.name for f in dataclasses.fields(Session))
_DISPLAY_FIELDS = frozenset(f.name for f in dataclasses.fields(Display))
_TOP_FIELDS = frozenset({"hold_cue", "waveform", "depth", "conditions"})


def _subset(values: Mapping[str, Any], names: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k in names}


def override(cfg: StimulusConfig, **changes: Any) -> StimulusConfig:
    """A re-validated copy with session, display or hold_cue/waveform/depth/conditions
    fields replaced.

    Keyword names are field names (``blocks``, ``stim_s``, ``refresh_hz``, ``depth``...);
    ``None`` values are skipped so argparse defaults can be passed straight through.
    """
    given = {k: v for k, v in changes.items() if v is not None}
    unknown = sorted(set(given) - _SESSION_FIELDS - _DISPLAY_FIELDS - _TOP_FIELDS)
    if unknown:
        raise KeyError(f"cannot override {unknown}")
    if not given:
        return cfg
    out = dataclasses.replace(
        cfg,
        session=dataclasses.replace(cfg.session, **_subset(given, _SESSION_FIELDS)),
        display=dataclasses.replace(cfg.display, **_subset(given, _DISPLAY_FIELDS)),
        **_subset(given, _TOP_FIELDS),
    )
    validate(out)
    return out


# -- trial order ----------------------------------------------------------------------------

def make_trials(n_targets: int, blocks: int, shuffle: bool = True,
                seed: int | None = None, n_conditions: int = 1) -> list[Trial]:
    """Trial order: each block cues every (condition, target) pair once, in a seeded random
    order if asked. With one condition this is every target once per block."""
    if n_targets < 1:
        raise ValueError(f"need at least one target, got {n_targets}")
    if blocks < 1:
        raise ValueError(f"need at least one block, got {blocks}")
    if n_conditions < 1:
        raise ValueError(f"need at least one condition, got {n_conditions}")
    per_block = n_targets * n_conditions
    if shuffle:
        keys = np.random.default_rng(seed).random((blocks, per_block))
        grid = np.argsort(keys, axis=1, kind="stable")
    else:
        grid = np.broadcast_to(np.arange(per_block), (blocks, per_block))
    return [Trial(index=i, block=i // per_block, target=int(p) % n_targets,
                  condition=int(p) // n_targets)
            for i, p in enumerate(grid.ravel())]


def session_trials(cfg: StimulusConfig, seed: int | None = None) -> list[Trial]:
    """make_trials for a config's session settings."""
    s = cfg.session
    return make_trials(cfg.n_targets, s.blocks, s.shuffle, seed, max(1, len(cfg.conditions)))
