"""Board geometry and the pieces layer for the BrainChess stimulus.

The stimulus window draws the 64 squares itself (their luminance is the flicker), so the
pieces come as a separate transparent PNG laid over them. A piece can sit on a square at
any luminance from black to white while it flickers, so every glyph is drawn solid (the
filled "black" chess glyphs for both colours), filled white for White and black for
Black, with a thick outline of the opposite colour: one of the two always contrasts.

Geometry is in PsychoPy ``height`` units: the board is centred at (0, 0) and BOARD_SIZE
screen heights tall. Squares are python-chess indices (a1 = 0, h8 = 63). By default White
sits at the bottom (a1 bottom left); ``flip=True`` puts Black at the bottom (h8 bottom
left). Rendering uses a matplotlib Figure on the Agg canvas directly, never pyplot, so it
leaves no global figure state behind.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import chess

BOARD_SIZE = 0.9
SQUARE_SIZE = BOARD_SIZE / 8

# Filled glyphs U+265A..U+265F (king, queen, rook, bishop, knight, pawn); DejaVu Sans has them.
GLYPHS = {chess.KING: "♚", chess.QUEEN: "♛", chess.ROOK: "♜",
          chess.BISHOP: "♝", chess.KNIGHT: "♞", chess.PAWN: "♟"}
GLYPH_SCALE = 0.95     # glyph em size as a fraction of the square side
OUTLINE_SCALE = 0.07   # outline stroke width as a fraction of the square side
FONT = "DejaVu Sans"


def _check_square(sq: int) -> None:
    if isinstance(sq, bool) or not isinstance(sq, int) or not 0 <= sq <= 63:
        raise ValueError(f"square must be an integer 0..63 (a1=0, h8=63), got {sq!r}")


def grid_cell(sq: int, flip: bool = False) -> tuple[int, int]:
    """(column, row) of a square counted from the bottom-left of the screen, each 0..7."""
    _check_square(sq)
    col, row = chess.square_file(sq), chess.square_rank(sq)
    return (7 - col, 7 - row) if flip else (col, row)


def square_center(sq: int, flip: bool = False) -> tuple[float, float]:
    """Centre of a square in screen heights, board centred at (0, 0)."""
    col, row = grid_cell(sq, flip)
    return ((col - 3.5) * SQUARE_SIZE, (row - 3.5) * SQUARE_SIZE)


def square_at(x: float, y: float, flip: bool = False) -> int | None:
    """The square under point (x, y) in screen heights, or None off the board."""
    col = math.floor(x / SQUARE_SIZE + 4.0)
    row = math.floor(y / SQUARE_SIZE + 4.0)
    if not (0 <= col <= 7 and 0 <= row <= 7):
        return None
    if flip:
        col, row = 7 - col, 7 - row
    return chess.square(col, row)


def render_pieces(board: chess.Board, path: str | Path, px: int = 800,
                  flip: bool = False) -> Path:
    """Draw only the pieces of ``board`` on a fully transparent px x px RGBA PNG.

    The file is written beside ``path`` and renamed into place, so a stimulus loading it
    concurrently never reads a half-written image. Returns the path.
    """
    if isinstance(px, bool) or not isinstance(px, int) or px < 8:
        raise ValueError(f"px must be an integer >= 8, got {px!r}")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patheffects import withStroke

    out = Path(path)
    # 8 inches at px/8 dpi: one square is one inch, so the image is exactly px wide and
    # point sizes (72 per inch) scale with the square whatever px is.
    fig = Figure(figsize=(8, 8), dpi=px / 8)
    FigureCanvasAgg(fig)
    fig.patch.set_alpha(0.0)
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.set_xlim(0, 8)
    ax.set_ylim(0, 8)
    ax.set_axis_off()
    ax.patch.set_alpha(0.0)
    size_pt = GLYPH_SCALE * 72.0
    stroke_pt = OUTLINE_SCALE * 72.0
    for sq, piece in board.piece_map().items():
        col, row = grid_cell(sq, flip)
        fill, edge = ("white", "black") if piece.color == chess.WHITE else ("black", "white")
        ax.text(col + 0.5, row + 0.5, GLYPHS[piece.piece_type], fontsize=size_pt,
                fontfamily=FONT, color=fill, ha="center", va="center_baseline",
                path_effects=[withStroke(linewidth=stroke_pt, foreground=edge)])
    tmp = out.with_name(out.name + ".tmp")
    fig.savefig(tmp, format="png", dpi=px / 8, transparent=True)
    os.replace(tmp, out)
    return out
