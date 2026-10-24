"""Tests for the BrainSwipe profile deck and its card renderer (headless, Pillow only)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from brainsync import profiles
from brainsync.profiles import (CARD_SIZE, DEFAULT_DECK, MARGIN, Profile, load_deck,
                                render_card, render_deck)

Image = pytest.importorskip("PIL.Image")

LONG = ("This answer goes on and on about sourdough starters, tide pools, the best "
        "dumplings in the Richmond, three different bridges and a goose that once chased "
        "me across a parking lot in Alameda for no reason I have ever understood. " * 3)


def _deck(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "deck.toml"
    path.write_text(body)
    return path


GOOD = '''
[[profile]]
name = "Ada"
age = 30
color = "#c86b5a"
detail = "Engineer · Oakland"
prompts = [["My simple pleasures", "Tea."]]
'''


def _profile(**over) -> Profile:
    base = dict(name="Ada", age=30, prompts=(("My simple pleasures", "Tea."),),
                color="#c86b5a", detail="Engineer · Oakland")
    base.update(over)
    return Profile(**base)


def _arr(img) -> np.ndarray:
    return np.asarray(img, dtype=np.float64)


# -- deck loading --------------------------------------------------------------------------

def test_default_deck_loads():
    deck = load_deck()
    assert DEFAULT_DECK.name == "profiles.toml" and DEFAULT_DECK.is_file()
    assert 18 <= len(deck) <= 25
    assert len({p.name for p in deck}) == len(deck)          # every card is someone new
    for p in deck:
        assert isinstance(p, Profile)
        assert p.name and 22 <= p.age <= 38
        assert 1 <= len(p.prompts) <= 3
        assert all(q and a for q, a in p.prompts)
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", p.color)
        assert p.detail


def test_default_deck_has_no_branding():
    text = DEFAULT_DECK.read_text().lower()
    assert "hinge" not in text


def test_load_deck_from_path_and_str(tmp_path):
    path = _deck(tmp_path, GOOD)
    for arg in (path, str(path)):
        (p,) = load_deck(arg)
        assert p == _profile()
        assert isinstance(p.prompts, tuple) and isinstance(p.prompts[0], tuple)


def test_detail_is_optional(tmp_path):
    (p,) = load_deck(_deck(tmp_path, GOOD.replace('detail = "Engineer · Oakland"\n', "")))
    assert p.detail == ""


@pytest.mark.parametrize("old, new, match", [
    ('name = "Ada"\n', "", r"name"),
    ('name = "Ada"', 'name = "  "', r"name"),
    ("age = 30", 'age = "thirty"', r"age"),
    ("age = 30", "age = 12", r"age"),
    ("age = 30", "age = true", r"age"),
    ('prompts = [["My simple pleasures", "Tea."]]', "prompts = []", r"prompts"),
    ('prompts = [["My simple pleasures", "Tea."]]', 'prompts = [["Only a prompt"]]', r"prompt"),
    ('prompts = [["My simple pleasures", "Tea."]]', 'prompts = [["Q", ""]]', r"prompt"),
    ('prompts = [["My simple pleasures", "Tea."]]',
     'prompts = [["a", "b"], ["c", "d"], ["e", "f"], ["g", "h"]]', r"prompts"),
    ('color = "#c86b5a"', 'color = "red"', r"color"),
    ('color = "#c86b5a"', 'color = "#c86b5"', r"color"),
    ('detail = "Engineer · Oakland"', "detail = 3", r"detail"),
    ('age = 30', 'age = 30\nheight = 180', r"unknown"),
])
def test_bad_entries_name_file_and_index(tmp_path, old, new, match):
    body = GOOD + GOOD.replace(old, new)            # the bad entry is profile 1
    assert body.count("[[profile]]") == 2
    path = _deck(tmp_path, body)
    with pytest.raises(ValueError, match=match) as err:
        load_deck(path)
    assert str(path) in str(err.value)
    assert "profile 1" in str(err.value)


def test_empty_or_malformed_deck(tmp_path):
    with pytest.raises(ValueError, match="no profiles"):
        load_deck(_deck(tmp_path, ""))
    with pytest.raises(ValueError, match="deck.toml"):
        load_deck(_deck(tmp_path, "[[profile]\nname="))
    with pytest.raises(ValueError, match="unknown"):
        load_deck(_deck(tmp_path, GOOD + "\ntitle = 'x'\n"))
    with pytest.raises(FileNotFoundError):
        load_deck(tmp_path / "missing.toml")


def test_load_deck_does_not_need_pillow():
    # only the renderer needs Pillow; the deck must load (e.g. in the demo) without it
    code = ("import sys; sys.modules['PIL'] = None\n"
            "from brainsync.profiles import load_deck\n"
            "print(len(load_deck()))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert int(out.stdout) == len(load_deck())


# -- card rendering ------------------------------------------------------------------------

def test_card_size_and_mode():
    img = render_card(_profile())
    assert CARD_SIZE == (600, 900)
    assert img.size == CARD_SIZE and img.mode == "RGB"
    small = render_card(_profile(), size=(300, 450))
    assert small.size == (300, 450) and small.mode == "RGB"


def test_card_is_deterministic():
    p = load_deck()[0]
    assert render_card(p).tobytes() == render_card(p).tobytes()


def test_cards_differ_between_profiles():
    a, b = load_deck()[:2]
    assert render_card(a).tobytes() != render_card(b).tobytes()


def _margin_is_background(img) -> None:
    arr = _arr(img)
    bg = arr[0, 0]
    np.testing.assert_array_equal(bg, profiles.BACKGROUND)
    band = MARGIN // 2              # the outer band never holds text or shapes
    for strip in (arr[:band], arr[-band:], arr[:, :band], arr[:, -band:]):
        assert np.all(strip == bg)


def test_long_answers_wrap_inside_the_card():
    p = _profile(name="Bartholomew-Maximilian Featherstonehaugh", detail="x " * 80,
                 prompts=(("A shower thought I recently had " * 3, LONG),
                          ("My simple pleasures", LONG), ("Typical Sunday", LONG)))
    img = render_card(p)
    assert img.size == CARD_SIZE
    _margin_is_background(img)


def test_default_deck_cards_keep_their_margin():
    for p in load_deck():
        _margin_is_background(render_card(p))


def test_wrap_breaks_on_words_and_long_words():
    from PIL import ImageFont
    font = ImageFont.load_default(size=20)
    lines = profiles.wrap("one two three four five six seven", font, 120)
    assert len(lines) > 1 and " ".join(lines) == "one two three four five six seven"
    assert all(font.getlength(line) <= 120 for line in lines)
    lines = profiles.wrap("x" * 80, font, 100)
    assert "".join(lines) == "x" * 80 and all(font.getlength(line) <= 100 for line in lines)
    assert profiles.wrap("", font, 100) == []


def test_mean_luminance_is_moderate():
    # The card sits between two squares flickering full black to full white; a bright card
    # would compete for gaze and add its own luminance to the peripheral field, so the
    # whole card stays dark on average (below 45% of white, in Rec. 709 luma).
    for p in load_deck():
        arr = _arr(render_card(p)) / 255.0
        luma = arr @ np.array([0.2126, 0.7152, 0.0722])
        assert luma.mean() < 0.45, p.name
        assert luma.max() < 0.95, p.name        # no pure-white glare either


# -- deck rendering ------------------------------------------------------------------------

def test_render_deck_writes_numbered_pngs(tmp_path):
    deck = load_deck()[:3]
    out = tmp_path / "cards"            # created when missing
    paths = render_deck(deck, out)
    assert [p.name for p in paths] == ["card_000.png", "card_001.png", "card_002.png"]
    assert sorted(out.glob("*.png")) == paths
    for p in paths:
        with Image.open(p) as img:
            assert img.size == CARD_SIZE and img.mode == "RGB"


def test_render_deck_names_sort_in_deck_order_past_1000(tmp_path, monkeypatch):
    # the stimulus indexes cards by sorted name: card_1000 must not sort before card_101
    from brainsync.stimulus.display import card_paths

    class Blank:
        def save(self, path, format):
            Path(path).write_bytes(b"")

    monkeypatch.setattr(profiles, "render_card", lambda profile: Blank())
    deck = load_deck()
    paths = render_deck([deck[i % len(deck)] for i in range(1001)], tmp_path)
    assert card_paths(tmp_path) == paths
    assert paths[0].name == "card_0000.png" and paths[-1].name == "card_1000.png"


def test_render_deck_empty_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="no profiles"):
        render_deck([], tmp_path)


def test_render_deck_replaces_stale_cards(tmp_path):
    # the stimulus loads every PNG in the folder, so a smaller deck must not leave old cards
    deck = load_deck()
    render_deck(deck[:4], tmp_path)
    (tmp_path / "notes.txt").write_text("kept")
    paths = render_deck(deck[:2], tmp_path)
    assert sorted(tmp_path.glob("*.png")) == paths and len(paths) == 2
    assert (tmp_path / "notes.txt").read_text() == "kept"
