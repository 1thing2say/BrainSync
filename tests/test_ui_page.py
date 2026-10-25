"""The partner page's static files: every element the scripts look up is on the page once, the
fonts are served with it, nothing is loaded from the internet and nothing on it blinks."""

from __future__ import annotations

import re
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.config import REPO_ROOT
from app.server import create_app

UI = REPO_ROOT / "ui"
FONTS = ["Geist-Variable.woff2", "GeistMono-Variable.woff2"]
# How the scripts find elements: app.js's $("id") helper, getElementById, and copyLink("id", ...),
# which looks its first argument up with $.
LOOKUPS = [re.compile(r"""\$\(\s*["']([\w-]+)["']\s*\)"""),
           re.compile(r"""getElementById\(\s*["']([\w-]+)["']\s*\)"""),
           re.compile(r"""copyLink\(\s*["']([\w-]+)["']""")]
# profile.js also looks ids up in loops, `for (const k of LISTS) $(k)`: the ids are the strings in
# these arrays.
ID_ARRAYS = re.compile(r"""const\s+(?:LISTS|STYLE_LISTS)\s*=\s*\[([^\]]*)\]""")


def page_ids(name: str) -> Counter:
    return Counter(re.findall(r"""\sid=["']([^"']+)["']""", (UI / name).read_text(encoding="utf-8")))


def looked_up(script: str) -> set[str]:
    text = (UI / script).read_text(encoding="utf-8")
    found = {m for rx in LOOKUPS for m in rx.findall(text)}
    for items in ID_ARRAYS.findall(text):
        found.update(re.findall(r"""["']([\w-]+)["']""", items))
    return found


def test_the_profile_lists_are_found():
    assert {"likes", "avoid_topics", "phrases", "avoid_phrases"} <= looked_up("profile.js")


@pytest.mark.parametrize("page,scripts", [("index.html", ["app.js", "view_core.js"]),
                                          ("profile.html", ["profile.js"])])
def test_every_looked_up_id_is_on_the_page_once(page, scripts):
    ids = page_ids(page)
    wanted = set().union(*(looked_up(s) for s in scripts))
    assert wanted, "no lookups found: has the $ helper changed?"
    missing = sorted(i for i in wanted if ids[i] == 0)
    twice = sorted(i for i in wanted if ids[i] > 1)
    assert not missing, f"{page} lacks {missing}"
    assert not twice, f"{page} has more than one of {twice}"


@pytest.mark.parametrize("page", ["index.html", "profile.html"])
def test_ids_are_unique(page):
    dupes = [i for i, n in page_ids(page).items() if n > 1]
    assert not dupes, f"{page} repeats ids {dupes}"


def test_the_fonts_ship_with_their_licence():
    for name in [*FONTS, "OFL.txt"]:
        assert (UI / "fonts" / name).stat().st_size > 0
    css = (UI / "style.css").read_text(encoding="utf-8")
    for name in FONTS:
        assert f'url("fonts/{name}")' in css


def test_the_server_serves_the_fonts(tmp_path):
    with TestClient(create_app(log_dir=tmp_path)) as c:
        for name in FONTS:
            r = c.get(f"/fonts/{name}")
            assert r.status_code == 200 and r.content == (UI / "fonts" / name).read_bytes()


@pytest.mark.parametrize("name", ["style.css", "index.html", "profile.html"])
def test_nothing_is_loaded_from_the_internet(name):
    # Offline rule: the page must work with the venue Wi-Fi down.
    assert not re.search(r"https?://", (UI / name).read_text(encoding="utf-8"))


def test_nothing_blinks():
    # PLAN rule 10: the only flicker is on the user's display; the partner page has steady fills.
    css = re.sub(r"/\*.*?\*/", "", (UI / "style.css").read_text(encoding="utf-8"), flags=re.S)
    assert "@keyframes" not in css
    assert not re.search(r"(?<![-\w])animation(-name)?\s*:", css)
    assert "blink" not in css


def test_only_the_session_page_is_held_to_the_window():
    # At 901 px and up the session page never scrolls; the profile page (same stylesheet) must.
    css = re.sub(r"/\*.*?\*/", "", (UI / "style.css").read_text(encoding="utf-8"), flags=re.S)
    held = re.findall(r"([^{}]+)\{[^{}]*overflow:\s*hidden", css)
    bodies = [sel.strip() for sel in held if re.search(r"(^|[\s,])body\b", sel.strip())]
    assert bodies == ["body.session"]
    assert '<body class="session">' in (UI / "index.html").read_text(encoding="utf-8")
    assert 'class="session"' not in (UI / "profile.html").read_text(encoding="utf-8")
