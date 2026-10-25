"""Tests for the BrainPoint Chrome extension (clients/chrome/), headless: manifest, copies, node tests.

The JavaScript tests in clients/chrome/test/ run under ``node --test`` when node is on PATH (or
at /usr/local/bin/node) and are skipped otherwise. No browser is opened and nothing touches the
network.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "clients" / "chrome"
SDK = ROOT / "clients" / "js"
TEST_DIR = EXT / "test"
JS_FILES = sorted(EXT.glob("*.js"))
NODE = shutil.which("node") or next(
    (p for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if Path(p).exists()), None)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

CONTENT_SCRIPTS = ["flicker.js", "brainsync-client.js", "cvep.js", "hints.js", "clicker.js",
                   "point_overlay.js", "content.js"]
# brainsync.vep.codes.m_sequence(63), the c-VEP standard (docs/CVEP-STANDARD.md section 1)
STANDARD_BITS = "000001111110101011001101110110100100111000101111001010001100001"


def _manifest() -> dict:
    return json.loads((EXT / "manifest.json").read_text())


def test_manifest_is_mv3_with_all_urls_storage_and_a_worker():
    m = _manifest()
    assert m["manifest_version"] == 3
    assert m["name"] == "BrainPoint"
    assert m["minimum_chrome_version"] == "116"   # WebSocket traffic keeps the worker alive
    assert m["background"] == {"service_worker": "background.js"}   # classic: importScripts works
    assert (EXT / m["background"]["service_worker"]).is_file()
    assert m["permissions"] == ["storage"]
    assert m["host_permissions"] == ["<all_urls>"]   # captureVisibleTab needs it
    assert "debugger" not in m["permissions"] and "tabs" not in m["permissions"]


def test_esc_listens_in_every_frame_from_document_start():
    # Esc must stop the flicker even while an iframe has the focus (PROTOCOL-v3 rule 1), and
    # before any page script can swallow the key.
    esc, _ = _manifest()["content_scripts"]
    assert esc["matches"] == ["<all_urls>"]
    assert esc["js"] == ["esc.js"]
    assert esc["run_at"] == "document_start"
    assert esc["all_frames"] is True and esc["match_about_blank"] is True
    overlay = (EXT / "point_overlay.js").read_text()
    assert "Alt+Shift+B" in overlay           # the warning names the fallback
    assert "Alt+Shift+B" in json.dumps(_manifest()["commands"])


def test_content_scripts_are_top_frame_only_in_dependency_order():
    _, cs = _manifest()["content_scripts"]
    assert cs["matches"] == ["<all_urls>"]
    assert cs["run_at"] == "document_idle"
    assert cs["all_frames"] is False
    # flicker before the SDK that needs it, the SDK before the overlay, the wiring last
    assert cs["js"] == CONTENT_SCRIPTS
    for name in cs["js"]:
        assert (EXT / name).is_file(), name


def test_options_page_and_toolbar_toggle_exist():
    m = _manifest()
    assert m["options_ui"]["page"] == "options.html"
    html = (EXT / "options.html").read_text()
    assert '<script src="options.js"></script>' in html
    assert 'id="token" type="password"' in html   # the token is shown masked
    for id_ in ("serverUrl", "token", "status", "form", "reset"):
        assert f'id="{id_}"' in html, id_
    assert "default_title" in m["action"]
    assert "_execute_action" in m["commands"]


@pytest.mark.parametrize("name", ["brainsync-client.js", "flicker.js", "cvep.js"])
def test_sdk_copies_are_byte_identical_to_clients_js(name):
    assert (EXT / name).read_bytes() == (SDK / name).read_bytes(), (
        f"clients/chrome/{name} is stale: cp clients/js/{name} clients/chrome/")


def test_cvep_copy_carries_the_standard_code():
    # The overlay draws the welcome's bits; cvep.js is the fallback and must be the standard.
    text = (EXT / "cvep.js").read_text()
    block = text[text.index("const BITS = Object.freeze(["):text.index("]);")]
    bits = "".join(re.findall(r"\b[01]\b", block.split("[", 1)[1]))
    assert bits == STANDARD_BITS
    from brainsync.vep.codes import m_sequence
    assert "".join(str(int(b)) for b in m_sequence(63)) == STANDARD_BITS


def test_overlay_reads_the_code_messages_of_the_standard():
    # CVEP-STANDARD 4.2: welcome paradigm/code, layout lag, phase train with cue, client
    # cycle, server train, trained and decision.
    overlay = (EXT / "point_overlay.js").read_text()
    for needle in ('paradigm !== "code"', "t.lag", 'phase === "train"', 'type: "cycle"',
                   'case "train":', 'case "trained":', 'case "decision":', "t_client",
                   "K.labelLine", "K.statusLine", "K.bit("):
        assert needle in overlay, needle


def test_background_imports_the_sdk_and_defaults_to_the_local_server():
    bg = (EXT / "background.js").read_text()
    assert 'importScripts("flicker.js", "brainsync-client.js")' in bg
    assert 'const DEFAULT_URL = "ws://127.0.0.1:8765/ws";' in bg
    assert '{ format: "jpeg", quality: 80 }' in bg
    opts = (EXT / "options.js").read_text()
    assert 'const DEFAULT_URL = "ws://127.0.0.1:8765/ws";' in opts


def test_scan_limits_match_the_protocol():
    content = (EXT / "content.js").read_text()
    hints = (EXT / "hints.js").read_text()
    assert "const MAX_EDGE = 1280;" in content
    assert "const JPEG_QUALITY = 0.8;" in content
    assert "const MAX_HINTS = 200;" in hints
    assert "const MAX_TEXT = 120;" in hints
    from brainsync.server import protocol
    assert protocol.MAX_HINTS == 200
    assert protocol.HINT_TEXT_CHARS == 120


def test_readme_justifies_every_permission_and_names_every_file():
    m = _manifest()
    readme = (EXT / "README.md").read_text()
    for perm in [*m["permissions"], *m["host_permissions"]]:
        assert f"`{perm}`" in readme, perm
    for name in [*CONTENT_SCRIPTS, "esc.js", "background.js", "options.html", "options.js",
                 "manifest.json"]:
        assert f"`{name}`" in readme, name
    assert "Photosensitivity" in readme


def test_no_secret_is_logged():
    # The token travels only inside the worker's Connection; no script prints it.
    for path in JS_FILES:
        text = path.read_text()
        assert not re.search(r"console\.\w+\([^)]*token", text, re.I), path.name


@needs_node
def test_node_unit_tests_pass():
    tests = sorted(str(p.relative_to(ROOT)) for p in TEST_DIR.glob("*.test.mjs"))
    assert len(tests) >= 6   # hints, clicker, point_overlay, content, background, options
    # Node 24 reads --test arguments as globs, so a bare directory does not work: list files.
    proc = subprocess.run([NODE, "--test", *tests], cwd=ROOT, capture_output=True, text=True,
                          timeout=180)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert re.search(r"^ℹ fail 0$", proc.stdout, re.M), proc.stdout[-2000:]


@needs_node
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_js_parses(path):
    proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
