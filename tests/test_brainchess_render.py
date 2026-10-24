"""BrainChess board geometry and the pieces PNG; matplotlib only, no window."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

chess = pytest.importorskip("chess")   # the optional "chess" extra

from brainsync.brainchess.render import (BOARD_SIZE, SQUARE_SIZE, grid_cell, render_pieces,
                                         square_at, square_center)


def _alpha(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        assert im.mode == "RGBA"
        return np.asarray(im)[..., 3]


def _cell(alpha: np.ndarray, col: int, row: int) -> np.ndarray:
    """Pixels of the screen cell (col, row) counted from the bottom left."""
    side = alpha.shape[0] // 8
    top = (7 - row) * side
    return alpha[top:top + side, col * side:(col + 1) * side]


def test_square_geometry_corners():
    assert SQUARE_SIZE == pytest.approx(0.1125) and BOARD_SIZE == 0.9
    assert square_center(chess.A1) == pytest.approx((-0.39375, -0.39375))
    assert square_center(chess.H8) == pytest.approx((0.39375, 0.39375))
    assert square_center(chess.H1) == pytest.approx((0.39375, -0.39375))
    assert square_center(chess.A1, flip=True) == pytest.approx((0.39375, 0.39375))
    assert square_center(chess.H8, flip=True) == pytest.approx((-0.39375, -0.39375))
    assert grid_cell(chess.E2) == (4, 1) and grid_cell(chess.E2, flip=True) == (3, 6)


@pytest.mark.parametrize("flip", [False, True])
def test_square_center_and_square_at_round_trip(flip):
    for sq in range(64):
        x, y = square_center(sq, flip)
        assert square_at(x, y, flip) == sq
        # anywhere inside the square maps back to it
        q = 0.49 * SQUARE_SIZE
        assert square_at(x - q, y + q, flip) == sq and square_at(x + q, y - q, flip) == sq
        assert abs(x) < BOARD_SIZE / 2 and abs(y) < BOARD_SIZE / 2


def test_square_at_off_board_and_bad_squares():
    for x, y in ((0.46, 0.0), (0.0, -0.46), (0.45, 0.0), (-0.451, 0.2), (0.7, 0.7)):
        assert square_at(x, y) is None
    assert square_at(-0.45, -0.45) == chess.A1
    for bad in (-1, 64, 1.0, True, "a1"):
        with pytest.raises(ValueError, match="0..63"):
            square_center(bad)


def test_render_pieces_size_mode_and_transparency(tmp_path: Path):
    board = chess.Board()
    out = render_pieces(board, tmp_path / "pieces.png", px=400)
    assert out == tmp_path / "pieces.png" and out.is_file()
    assert not (tmp_path / "pieces.png.tmp").exists()
    with Image.open(out) as im:
        assert im.size == (400, 400) and im.mode == "RGBA"
    alpha = _alpha(out)
    for sq in range(64):
        col, row = grid_cell(sq)
        cell = _cell(alpha, col, row)
        if board.piece_at(sq) is None:
            assert cell.max() == 0, chess.square_name(sq)
        else:
            assert cell[25, 25] == 255, chess.square_name(sq)   # opaque at the centre


def test_render_pieces_colours_contrast(tmp_path: Path):
    board = chess.Board(None)
    board.set_piece_at(chess.D4, chess.Piece(chess.QUEEN, chess.WHITE))
    board.set_piece_at(chess.E5, chess.Piece(chess.QUEEN, chess.BLACK))
    with Image.open(render_pieces(board, tmp_path / "q.png", px=800)) as im:
        rgba = np.asarray(im)
    side = 100

    def cell(sq):
        col, row = grid_cell(sq)
        return rgba[(7 - row) * side:(8 - row) * side, col * side:(col + 1) * side]

    for sq, fill in ((chess.D4, 255), (chess.E5, 0)):
        c = cell(sq)
        solid = c[..., 3] == 255
        grey = c[..., :3].mean(axis=2)[solid]
        # both a fill and an outline of the opposite colour are present
        assert np.mean(np.abs(grey - fill) < 40) > 0.3
        assert np.mean(np.abs(grey - (255 - fill)) < 40) > 0.1


def test_render_pieces_flip_mirrors(tmp_path: Path):
    board = chess.Board("4k3/8/8/8/8/8/8/R3K2N w - - 0 1")
    plain = _alpha(render_pieces(board, tmp_path / "a.png", px=320))
    flipped = _alpha(render_pieces(board, tmp_path / "b.png", px=320, flip=True))
    for sq in (chess.A1, chess.E1, chess.H1, chess.E8):
        c, r = grid_cell(sq)
        assert _cell(plain, c, r).max() == 255
        assert _cell(flipped, 7 - c, 7 - r).max() == 255
    assert _cell(flipped, 0, 0).max() == 0          # h8 is empty, bottom left when flipped
    assert _cell(flipped, 7, 7).max() == 255        # a1 rook top right
    # occupied cells of the flipped image are the plain ones rotated 180 degrees
    occ = lambda a: {(c, r) for c in range(8) for r in range(8) if _cell(a, c, r).max()}
    assert occ(flipped) == {(7 - c, 7 - r) for c, r in occ(plain)}


def test_render_pieces_errors_and_no_pyplot(tmp_path: Path):
    with pytest.raises(ValueError, match="px"):
        render_pieces(chess.Board(), tmp_path / "x.png", px=4)
    import subprocess
    code = ("import sys, chess; from brainsync.brainchess.render import render_pieces; "
            f"render_pieces(chess.Board(), {str(tmp_path / 'z.png')!r}, px=64); "
            "print('matplotlib.pyplot' in sys.modules, 'psychopy' in sys.modules)")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert res.stdout.split() == ["False", "False"]
