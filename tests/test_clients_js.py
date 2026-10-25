"""Tests for the JavaScript client SDK and the reference page, headless.

The JavaScript tests in clients/js/test run under ``node --test`` when node is on PATH (or
at /usr/local/bin/node) and are skipped otherwise. No browser is opened and nothing touches
the network. clients/js/flicker.js must stay a byte-identical copy of extension/flicker.js.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "clients" / "js"
WEB = ROOT / "clients" / "web"
TEST_DIR = JS / "test"
JS_FILES = sorted([*JS.glob("*.js"), *WEB.glob("*.js")])
NODE = shutil.which("node") or next(
    (p for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if Path(p).exists()), None)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_flicker_copy_is_byte_identical():
    assert (JS / "flicker.js").read_bytes() == (ROOT / "extension" / "flicker.js").read_bytes(), (
        "clients/js/flicker.js drifted: cp extension/flicker.js clients/js/flicker.js")


@needs_node
def test_node_unit_tests_pass():
    tests = sorted(str(p.relative_to(ROOT)) for p in TEST_DIR.glob("*.test.mjs"))
    assert len(tests) >= 2   # client SDK, web app
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


def test_reference_page_loads_its_files_in_order():
    html = (WEB / "index.html").read_text()
    srcs = re.findall(r'<script src="([^"]+)"></script>', html)
    assert srcs == ["/static/flicker.js", "/static/cvep.js", "/static/brainsync-client.js",
                    "/static/app.js"]
    assert '<link rel="stylesheet" href="/static/style.css">' in html
    for name in ("flicker.js", "cvep.js", "brainsync-client.js"):
        assert (JS / name).is_file()
    for name in ("app.js", "style.css"):
        assert (WEB / name).is_file()
    # every element app.js looks up exists, and the warning gate is there before any flicker
    app = (WEB / "app.js").read_text()
    for el_id in set(re.findall(r'\$\("([a-z-]+)"\)', app)):
        assert f'id="{el_id}"' in html, el_id
    assert "Photosensitivity warning" in html
    assert re.search(r'<button id="resume"[^>]*hidden', html)


def test_reference_page_speaks_protocol_v3():
    client = (JS / "brainsync-client.js").read_text()
    app = (WEB / "app.js").read_text()
    assert "const PROTOCOL = 3;" in client
    assert "Object.freeze([4000, 4001, 4002])" in client
    assert "Object.freeze([1000, 2000, 4000, 5000])" in client
    assert 'Object.freeze(["yes", "no", "help"])' in app
    assert 'mode: "slots"' in app
