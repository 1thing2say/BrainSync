"""Tests for the BrainScroll Chrome extension, headless: fixture, manifest, mock page, node tests.

The JavaScript tests run under ``node --test`` when node is on PATH (or at
/usr/local/bin/node) and are skipped otherwise. No browser is opened.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"
TEST_DIR = EXT / "test"
JS_FILES = sorted(EXT.glob("*.js"))
NODE = shutil.which("node") or next(
    (p for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if Path(p).exists()), None)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _load_make_fixture():
    spec = importlib.util.spec_from_file_location("make_fixture", TEST_DIR / "make_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_on_disk_matches_brainsync_flicker():
    make_fixture = _load_make_fixture()
    built = make_fixture.build()
    on_disk = json.loads((TEST_DIR / "flicker_fixture.json").read_text())
    assert on_disk == built, "stale fixture: run .venv/bin/python extension/test/make_fixture.py"
    assert (TEST_DIR / "flicker_fixture.json").read_text() == make_fixture.dumps(built)
    assert {(c["waveform"], c["freq"], c["depth"]) for c in built["cases"]} == {
        (w, f, d) for w in ("sine", "square") for f in (12.0, 15.0, 17.0) for d in (1.0, 0.5)}
    assert len(built["cases"]) == 24
    assert all(len(c["values"]) == 241 for c in built["cases"])


def test_make_fixture_check_flag(capsys, tmp_path):
    make_fixture = _load_make_fixture()
    assert make_fixture.main(["--check"]) == 0
    stale = tmp_path / "f.json"
    stale.write_text("{}\n")
    assert make_fixture.main(["--check", "--out", str(stale)]) == 1
    assert "error:" in capsys.readouterr().err


@needs_node
def test_node_unit_tests_pass():
    tests = sorted(str(p.relative_to(ROOT)) for p in TEST_DIR.glob("*.test.mjs"))
    assert len(tests) >= 3   # flicker, overlay, background
    # Node 24 reads --test arguments as globs, so a bare directory does not work: list files.
    proc = subprocess.run([NODE, "--test", *tests], cwd=ROOT, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert re.search(r"^ℹ fail 0$", proc.stdout, re.M), proc.stdout[-2000:]


@needs_node
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_js_parses(path):
    proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_manifest_is_mv3_and_references_files_that_exist():
    m = json.loads((EXT / "manifest.json").read_text())
    assert m["manifest_version"] == 3
    assert m["name"] == "BrainScroll"
    assert (EXT / m["background"]["service_worker"]).is_file()
    assert m["background"]["service_worker"] == "background.js"
    [cs] = m["content_scripts"]
    assert cs["matches"] == ["https://www.instagram.com/*"]
    assert cs["run_at"] == "document_idle"
    # flicker and scroller before the overlay that uses them, the glue last
    assert cs["js"] == ["flicker.js", "scroller.js", "overlay.js", "content.js"]
    for name in cs["js"]:
        assert (EXT / name).is_file(), name
    assert set(m["permissions"]) <= {"debugger", "tabs", "storage"}
    assert m["host_permissions"] == ["https://www.instagram.com/*"]


def test_readme_justifies_every_permission():
    m = json.loads((EXT / "manifest.json").read_text())
    readme = (EXT / "README.md").read_text()
    for perm in [*m["permissions"], *m["host_permissions"]]:
        assert f"`{perm}`" in readme, perm


def test_mock_page_references_files_that_exist():
    page = TEST_DIR / "reels_mock.html"
    html = page.read_text()
    srcs = re.findall(r'<script src="([^"]+)"', html)
    assert srcs == ["../flicker.js", "../scroller.js", "../overlay.js"]
    for src in srcs:
        assert (page.parent / src).resolve().is_file(), src
    # the container heuristic needs a snapping scroll column with a <video> per reel
    assert "scroll-snap-type: y mandatory" in html
    assert "overflow-y: scroll" in html
    assert "<video" in html
    assert 'agent: "page"' in html
    assert "ws://127.0.0.1:8765" in html


def test_extension_defaults_match_the_shared_protocol():
    overlay = (EXT / "overlay.js").read_text()
    background = (EXT / "background.js").read_text()
    assert 'const DEFAULT_URL = "ws://127.0.0.1:8765";' in background
    for snippet in ('label: "up", freq: 15.0, phase: 0.0, x: 0.55, y: 0.325, size: 0.3',
                    'label: "down", freq: 17.0, phase: 0.0, x: 0.55, y: -0.325, size: 0.3',
                    "refresh_hz: 60.0", "refresh_tolerance_hz: 1.0"):
        assert snippet in overlay, snippet
