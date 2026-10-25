"""The look of the BrainChess board window: palette, fonts and where everything sits.

Matches the browser mockup (web/brainchess): chart-paper background, recorder-ink text,
15 Hz in rust and 17 Hz in blue, Michroma for the title, Atkinson Hyperlegible for the
prompt and IBM Plex Mono for labels. The fonts ship in ``fonts/`` under the SIL Open Font
License (OFL-*.txt beside them).

Flickering squares stay grey, as in the mockup's real-flicker view: tinting them would
cut the luminance swing that drives the SSVEP. The groups are told apart by a small
"15" / "17" tag in each square's corner (a square tag for 15 Hz, a rounded one for 17 Hz,
so not by colour alone).

Layout in PsychoPy ``height`` units on a screen at least LAYOUT_ASPECT wide: the board
(BOARD_SIZE tall, as before, so the squares keep their size) sits left of centre and a
column on the right holds the title, the stage, the prompt, the vote meters, the PLAY /
BACK panels (placed by the config's [layout]) and the EEG trace with the decoder windows.
"""
from __future__ import annotations

from pathlib import Path

from .render import BOARD_SIZE

FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILES = tuple(str(p) for p in sorted(FONT_DIR.glob("*.ttf")))
DISPLAY_FONT = "Michroma"
BODY_FONT = "Atkinson Hyperlegible"      # bold=True picks the Bold face
MONO_FONT = "IBM Plex Mono"
MONO_BOLD_FONT = "IBM Plex Mono SemiBold"


def hex_rgb(code: str) -> tuple[float, float, float]:
    """PsychoPy rgb (-1..1) of a '#RRGGBB' colour."""
    digits = code.lstrip("#")
    if len(digits) != 6:
        raise ValueError(f"need a #RRGGBB colour, got {code!r}")
    r, g, b = (int(digits[i:i + 2], 16) / 127.5 - 1.0 for i in (0, 2, 4))
    return (r, g, b)


PAPER = hex_rgb("#ECEFEA")
SURFACE = hex_rgb("#F6F7F3")
INK = hex_rgb("#1B2228")
INK_2 = hex_rgb("#56626A")
RULE = hex_rgb("#C6CEC8")
GRID = hex_rgb("#DCE2DC")
TRACE15 = hex_rgb("#AD530A")
TRACE17 = hex_rgb("#1E5DA6")
MARKER = hex_rgb("#B42A22")
SQ_LIGHT = hex_rgb("#DDE2DA")
SQ_DARK = hex_rgb("#A7B2AB")
SELECTED = hex_rgb("#E2BE4E")      # the chosen piece's square: amber, neither group colour
WHITE = hex_rgb("#FFFFFF")
TRACE_BACK = hex_rgb("#3F7A3A")    # c-VEP: the Back panel (target 2)
TARGET_COLORS = (TRACE15, TRACE17, TRACE_BACK)

# -- board ----------------------------------------------------------------------------------
BOARD_X = -0.26                  # board centre; its edges at -0.71 and +0.19
BOARD_Y = 0.015                  # nudged up to leave room for the file letters below
BOARD_ORIGIN = (BOARD_X, BOARD_Y)
COORD_HEIGHT = 0.02              # rank digits and file letters
COORD_GAP = 0.017                # from the board edge to the coordinate's centre
TAG_SIZE = (0.036, 0.024)        # the "15" / "17" tag in a flickering square's corner
TAG_INSET = 0.004

# -- right-hand column ----------------------------------------------------------------------
COLUMN_LEFT = 0.235
COLUMN_RIGHT = 0.735
COLUMN_WIDTH = COLUMN_RIGHT - COLUMN_LEFT
TITLE_Y = 0.448
TITLE_HEIGHT = 0.034
RULE_Y = 0.408
STEPPER_Y = 0.378
STEPPER_HEIGHT = 0.016
STAGES = (("piece", "PIECE"), ("dest", "SQUARE"), ("promo", "PROMOTE"),
          ("confirm", "CONFIRM"))
PROMPT_TOP = 0.348
PROMPT_HEIGHT = 0.03
VOTES_Y = 0.222
VOTES_HEIGHT = 0.016
VOTE_BOX = 0.016
PANEL_LABEL_HEIGHT = 0.026       # PLAY / BACK in the display face
PANEL_NOTE_HEIGHT = 0.0135
PANEL_INSET = 0.008              # flicker area inside the panel frame
# c-VEP: the confidence block (a bar per target and the lead / choice line) replaces the
# vote meters, under the prompt
CONFIDENCE_Y = 0.255             # first line's centre
CONFIDENCE_STEP = 0.024
CONFIDENCE_HEIGHT = 0.016
CONFIDENCE_LINES = 4             # three targets and the status line
CONFIDENCE_BOTTOM = CONFIDENCE_Y - (CONFIDENCE_LINES - 1) * CONFIDENCE_STEP

# -- trace strip ----------------------------------------------------------------------------
TRACE_TOP = -0.168
TRACE_BOTTOM = -0.462
TRACE_HEADER_Y = TRACE_TOP - 0.018
TRACE_LABEL_HEIGHT = 0.0125
TRACE_PLOT_LEFT = COLUMN_LEFT + 0.038
TRACE_PLOT_RIGHT = COLUMN_RIGHT - 0.01
LANES_TOP = TRACE_TOP - 0.04
LANES_BOTTOM = TRACE_BOTTOM + 0.07
WINDOWS_TOP = TRACE_BOTTOM + 0.058
WINDOWS_BOTTOM = TRACE_BOTTOM + 0.032
MARKER_Y = TRACE_BOTTOM + 0.017
TRACE_SPAN_S = 6.0

# narrowest screen the layout fits: rank digits on the left to the column's right edge
LAYOUT_LEFT = BOARD_X - BOARD_SIZE / 2 - COORD_GAP - COORD_HEIGHT / 2
LAYOUT_ASPECT = 2 * max(-LAYOUT_LEFT, COLUMN_RIGHT)

FONT_PATHS = {DISPLAY_FONT: FONT_DIR / "Michroma-Regular.ttf",
              BODY_FONT: FONT_DIR / "AtkinsonHyperlegible-Regular.ttf",
              BODY_FONT + " Bold": FONT_DIR / "AtkinsonHyperlegible-Bold.ttf",
              MONO_FONT: FONT_DIR / "IBMPlexMono-Regular.ttf",
              MONO_BOLD_FONT: FONT_DIR / "IBMPlexMono-SemiBold.ttf"}
_ADVANCES: dict[str, tuple[dict[int, int], int]] = {}


def text_width(text: str, font: str, height: float) -> float:
    """Advance width of one line of ``text`` set ``height`` tall, in the same units.

    PsychoPy's TextStim height is the em size, so this matches what it draws to within a
    few per cent (the glyphs' side bearings), without a window. Unknown glyphs count as
    the font's space.
    """
    if font not in FONT_PATHS:
        raise ValueError(f"unknown font {font!r}; expected one of {sorted(FONT_PATHS)}")
    if font not in _ADVANCES:
        from fontTools.ttLib import TTFont   # matplotlib's dependency; lazy, it is slow
        tt = TTFont(str(FONT_PATHS[font]), lazy=True)
        cmap, hmtx = tt.getBestCmap(), tt["hmtx"]
        _ADVANCES[font] = ({code: hmtx[name][0] for code, name in cmap.items()},
                           tt["head"].unitsPerEm)
    advances, upm = _ADVANCES[font]
    space = advances.get(ord(" "), upm // 2)
    return sum(advances.get(ord(c), space) for c in text) / upm * height
