"""BrainSwipe profiles: the invented dating deck and the PNG cards the stimulus shows.

A deck is a TOML file of ``[[profile]]`` tables (name, age, color, detail and 1-3
``["prompt", "answer"]`` pairs). Every person in the default deck is made up and every
avatar is drawn (initials over simple shapes), never a photo. Render the deck once before
a session; the stimulus loads the PNGs as textures at startup (``--cards DIR``):

    .venv/bin/python -c "from brainsync.profiles import load_deck, render_deck; \\
        render_deck(load_deck(), 'cards')"

Cards are 600 x 900 px RGB portraits (2:3) on a dark background, written as
card_000.png, card_001.png, ... They sit between two squares flickering black to white,
so they are kept dim and low in contrast: a bright card would pull gaze off the targets.
Pillow is imported only inside the render functions, so ``load_deck`` works without it.
"""
from __future__ import annotations

import functools
import random
import re
import tomllib
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL import Image, ImageDraw, ImageFont

DEFAULT_DECK = Path(__file__).resolve().parent / "swipe_deck" / "profiles.toml"
CARD_SIZE = (600, 900)              # width, height in px: a 2:3 portrait
MARGIN = 28                         # px at CARD_SIZE; nothing is drawn in this outer band
MIN_AGE, MAX_AGE = 18, 99
MAX_PROMPTS = 3

# Colours (RGB 0-255). Text stays off pure white so the card never glares beside the flicker.
BACKGROUND = (22, 22, 26)
PANEL = (36, 36, 42)
TEXT = (226, 222, 214)
MUTED = (150, 148, 150)

_KEYS = frozenset({"name", "age", "color", "detail", "prompts"})
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_ELLIPSIS = "…"
_SUPERSAMPLE = 2                    # draw at 2x and downsample: smooth circles and text

# macOS system faces by weight, as (file, index in the collection); first one present wins.
_SYS = "/System/Library/Fonts"
_FONTS: dict[str, tuple[tuple[str, int], ...]] = {
    "bold": ((f"{_SYS}/Avenir Next.ttc", 0), (f"{_SYS}/HelveticaNeue.ttc", 1),
             (f"{_SYS}/Supplemental/Arial Bold.ttf", 0)),
    "demi": ((f"{_SYS}/Avenir Next.ttc", 2), (f"{_SYS}/HelveticaNeue.ttc", 10),
             (f"{_SYS}/Supplemental/Arial Bold.ttf", 0)),
    "regular": ((f"{_SYS}/Avenir Next.ttc", 7), (f"{_SYS}/HelveticaNeue.ttc", 0),
                (f"{_SYS}/Supplemental/Arial.ttf", 0)),
}


@dataclass(frozen=True)
class Profile:
    """One invented person; ``prompts`` holds 1-3 (prompt, answer) pairs."""

    name: str
    age: int
    prompts: tuple[tuple[str, str], ...]
    color: str                      # hex accent for the drawn avatar, e.g. "#c86b5a"
    detail: str = ""                # e.g. "Product designer · Oakland"


# -- deck loading --------------------------------------------------------------------------

def _text(raw: Mapping[str, Any], key: str, where: str, required: bool = True) -> str:
    v = raw.get(key, None if required else "")
    if not isinstance(v, str) or (required and not v.strip()):
        what = "a non-empty string" if required else "a string"
        raise ValueError(f"{where}: {key} must be {what}, got {v!r}")
    return v.strip()


def _prompts(v: Any, where: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(v, list) or not 1 <= len(v) <= MAX_PROMPTS:
        raise ValueError(f"{where}: prompts must be a list of 1 to {MAX_PROMPTS} "
                         f"[prompt, answer] pairs, got {v!r}")
    pairs = []
    for i, pair in enumerate(v):
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(s, str) and s.strip() for s in pair)):
            raise ValueError(f"{where}: prompt {i} must be a [prompt, answer] pair of "
                             f"non-empty strings, got {pair!r}")
        pairs.append((pair[0].strip(), pair[1].strip()))
    return tuple(pairs)


def _parse_profile(raw: Any, where: str) -> Profile:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: must be a table, got {raw!r}")
    extra = sorted(set(raw) - _KEYS)
    if extra:
        raise ValueError(f"{where}: unknown key(s) {extra}; expected {sorted(_KEYS)}")
    age = raw.get("age")
    # bool is an int subclass; `age = true` is a typo, not an age
    if isinstance(age, bool) or not isinstance(age, int) or not MIN_AGE <= age <= MAX_AGE:
        raise ValueError(f"{where}: age must be an integer from {MIN_AGE} to {MAX_AGE}, "
                         f"got {age!r}")
    color = raw.get("color")
    if not isinstance(color, str) or not _HEX.fullmatch(color):
        raise ValueError(f"{where}: color must be a hex colour like '#c86b5a', got {color!r}")
    return Profile(name=_text(raw, "name", where), age=age,
                   prompts=_prompts(raw.get("prompts"), where), color=color,
                   detail=_text(raw, "detail", where, required=False))


def load_deck(path: str | Path | None = None) -> list[Profile]:
    """Profiles from a deck TOML (default: the bundled DEFAULT_DECK), validated."""
    path = Path(path) if path is not None else DEFAULT_DECK
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path}: not valid TOML: {exc}") from None
    extra = sorted(set(data) - {"profile"})
    if extra:
        raise ValueError(f"{path}: unknown top-level key(s) {extra}; expected [[profile]] "
                         f"tables only")
    raw = data.get("profile", [])
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: no profiles; add [[profile]] tables")
    return [_parse_profile(entry, f"{path}: profile {i}") for i, entry in enumerate(raw)]


# -- drawing helpers -----------------------------------------------------------------------

def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _mix(a: Sequence[int], b: Sequence[int], t: float) -> tuple[int, int, int]:
    """Linear blend from a (t=0) to b (t=1), rounded to RGB ints."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


@functools.lru_cache(maxsize=64)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    from PIL import ImageFont

    for file, index in _FONTS[weight]:
        if Path(file).is_file():
            try:
                return ImageFont.truetype(file, size, index=index)
            except OSError:
                continue
    return ImageFont.load_default(size=size)   # size= needs Pillow >= 10.1 (pyproject)


def wrap(text: str, font: ImageFont.FreeTypeFont, width: float) -> list[str]:
    """Greedy word wrap to ``width`` px; a word wider than a line is split by characters."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}" if line else word
        if font.getlength(trial) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        while font.getlength(word) > width:
            cut = 1                 # at least one character per line, however narrow
            while font.getlength(word[:cut + 1]) <= width:
                cut += 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def _clip(lines: list[str], font: ImageFont.FreeTypeFont, width: float,
          max_lines: int) -> list[str]:
    """The first ``max_lines`` lines, the last ending in an ellipsis if text was cut."""
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1]
    while last and font.getlength(last.rstrip() + _ELLIPSIS) > width:
        last = last[:-1]
    kept[-1] = last.rstrip() + _ELLIPSIS
    return kept


def _line_h(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


@dataclass(frozen=True)
class _Block:
    """One laid-out prompt box: its label, answer lines and fonts, height in px."""

    label: str
    lines: tuple[str, ...]
    label_font: Any
    answer_font: Any
    pad: int
    gap: int
    height: int


def _layout_prompts(prompts: Sequence[tuple[str, str]], width: int, k: float,
                    answer_px: int, max_lines: int) -> list[_Block]:
    label_font = _font("demi", round(17 * k))
    answer_font = _font("demi", round(answer_px * k))
    pad, gap = round(18 * k), round(8 * k)
    inner = width - 2 * pad
    blocks = []
    for prompt, answer in prompts:
        label = _clip(wrap(prompt, label_font, inner), label_font, inner, 1)[0]
        lines = _clip(wrap(answer, answer_font, inner), answer_font, inner, max_lines)
        height = (2 * pad + _line_h(label_font) + gap
                  + len(lines) * round(_line_h(answer_font) * 1.08))
        blocks.append(_Block(label, tuple(lines), label_font, answer_font, pad, gap, height))
    return blocks


def _fit_prompts(prompts: Sequence[tuple[str, str]], width: int, space: int,
                 k: float) -> list[_Block]:
    """Largest answer size, then most lines, whose boxes fit in ``space`` px of height."""
    blocks: list[_Block] = []
    for max_lines in (4, 3, 2, 1):
        for answer_px in (26, 24, 22, 20):
            blocks = _layout_prompts(prompts, width, k, answer_px, max_lines)
            if sum(b.height for b in blocks) <= space:
                return blocks
    return blocks               # one line each at the smallest size; always fits a card


def _initials(name: str) -> str:
    words = [w for w in re.split(r"[\s\-]+", name) if w]
    return "".join(w[0].upper() for w in words[:2]) or "?"


def _draw_avatar(img: Image.Image, box: tuple[int, int, int, int], profile: Profile,
                 k: float) -> None:
    """Abstract 'photo': accent gradient, soft dots, two hills and an initials badge."""
    from PIL import Image as PILImage
    from PIL import ImageDraw

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    accent = _rgb(profile.color)
    dark = _mix(accent, BACKGROUND, 0.45)
    photo = PILImage.new("RGB", (w, h), dark)
    d = ImageDraw.Draw(photo)
    top, bottom = _mix(accent, BACKGROUND, 0.35), _mix(accent, BACKGROUND, 0.7)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=_mix(top, bottom, y / max(1, h - 1)))
    # zlib.crc32, unlike hash(), is the same in every run, so a card renders identically
    rng = random.Random(zlib.crc32(f"{profile.name}|{profile.age}".encode()))
    for _ in range(7):
        r = rng.uniform(0.04, 0.11) * w
        cx, cy = rng.uniform(0, w), rng.uniform(0, h * 0.6)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_mix(top, accent, rng.uniform(0.2, 0.45)))
    for frac, shade in ((0.62, 0.62), (0.78, 0.78)):
        cx = rng.uniform(0.15, 0.85) * w
        d.ellipse([cx - w * 0.75, h * frac, cx + w * 0.75, h * (frac + 0.9)],
                  fill=_mix(accent, BACKGROUND, shade))
    radius = min(w, h) * 0.24
    cx, cy = w / 2, h * 0.47
    ring = round(6 * k)
    d.ellipse([cx - radius - ring, cy - radius - ring, cx + radius + ring, cy + radius + ring],
              fill=_mix(accent, BACKGROUND, 0.8))
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
              fill=_mix(accent, TEXT, 0.15))
    d.text((cx, cy), _initials(profile.name), font=_font("bold", round(radius * 0.8)),
           fill=_mix(accent, BACKGROUND, 0.82), anchor="mm")
    mask = PILImage.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=round(22 * k), fill=255)
    img.paste(photo, (x0, y0), mask)


def _draw_header(d: ImageDraw.ImageDraw, profile: Profile, x: int, y: int, width: int,
                 k: float) -> int:
    """Name (shrunk to fit), age and detail line from (x, y); returns the bottom y."""
    age = str(profile.age)
    age_gap = round(14 * k)
    name_px = 44
    while True:
        name_font, age_font = _font("bold", round(name_px * k)), _font("regular", round(30 * k))
        room = width - age_gap - age_font.getlength(age)
        if name_font.getlength(profile.name) <= room or name_px <= 28:
            break
        name_px -= 2
    name = _clip(wrap(profile.name, name_font, room), name_font, room, 1)[0]
    base = y + name_font.getmetrics()[0]           # shared baseline for name and age
    d.text((x, base), name, font=name_font, fill=TEXT, anchor="ls")
    d.text((x + name_font.getlength(name) + age_gap, base), age, font=age_font, fill=MUTED,
           anchor="ls")
    y = base + name_font.getmetrics()[1]
    if profile.detail:
        detail_font = _font("regular", round(20 * k))
        detail = _clip(wrap(profile.detail, detail_font, width), detail_font, width, 1)[0]
        d.text((x, y + round(4 * k)), detail, font=detail_font, fill=MUTED, anchor="la")
        y += round(4 * k) + _line_h(detail_font)
    return y


# -- rendering -----------------------------------------------------------------------------

def render_card(profile: Profile, size: tuple[int, int] = CARD_SIZE) -> Image.Image:
    """One profile card: header, drawn avatar and prompt boxes on a dark background."""
    from PIL import Image as PILImage
    from PIL import ImageDraw

    out_w, out_h = size
    W, H = out_w * _SUPERSAMPLE, out_h * _SUPERSAMPLE
    k = W / CARD_SIZE[0]                    # px per layout unit, layout designed at 600 wide
    margin, gap = round(MARGIN * k), round(14 * k)
    img = PILImage.new("RGB", (W, H), BACKGROUND)
    d = ImageDraw.Draw(img)
    x0, x1 = margin, W - margin
    inner = x1 - x0

    y = _draw_header(d, profile, x0, margin, inner, k) + gap
    bottom = H - margin
    n = len(profile.prompts)
    # the avatar keeps at least this much height; prompts get the rest, shrinking to fit
    avatar_min = round(0.24 * H)
    blocks = _fit_prompts(profile.prompts, inner, bottom - y - avatar_min - n * gap, k)
    avatar_h = min(bottom - y - sum(b.height for b in blocks) - n * gap, round(0.5 * H))
    _draw_avatar(img, (x0, y, x1, y + avatar_h), profile, k)
    y += avatar_h + gap

    for b in blocks:
        d.rounded_rectangle([x0, y, x1, y + b.height], radius=round(16 * k), fill=PANEL)
        ty = y + b.pad
        d.text((x0 + b.pad, ty), b.label, font=b.label_font,
               fill=_mix(_rgb(profile.color), TEXT, 0.35), anchor="la")
        ty += _line_h(b.label_font) + b.gap
        for line in b.lines:
            d.text((x0 + b.pad, ty), line, font=b.answer_font, fill=TEXT, anchor="la")
            ty += round(_line_h(b.answer_font) * 1.08)
        y += b.height + gap

    # box averaging, not Lanczos: Lanczos rings past the text's brightness into glare
    return img.reduce(_SUPERSAMPLE)


def render_deck(profiles: Sequence[Profile], out_dir: str | Path) -> list[Path]:
    """Write card_000.png, card_001.png, ... into ``out_dir``; returns their paths in order.

    Old card_*.png files there are removed first: the stimulus loads every PNG in the
    folder, so a smaller deck must not leave stale cards behind. Decks of more than 1000
    get wider numbers (card_0000.png, ...) so the names still sort in deck order.
    """
    if not profiles:
        raise ValueError("no profiles to render")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("card_*.png"):
        old.unlink()
    # the stimulus indexes cards by sorted name, and "card_1000" sorts before "card_101"
    width = max(3, len(str(len(profiles) - 1)))
    paths = []
    for i, profile in enumerate(profiles):
        path = out / f"card_{i:0{width}d}.png"
        render_card(profile).save(path, format="PNG")
        paths.append(path)
    return paths
