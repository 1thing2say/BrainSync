"""Runs the BrainSwipe page's JavaScript tests (tests/web/*.test.mjs) with ``node --test``.

    pytest tests/test_web_js.py          # skipped when node (>= 18) is not installed

The page's pure helpers live in brainsync/web/logic.js; the node tests cover them and a
few checks on the page files (no external resources, no looping animation on the
squares). Node is found on PATH, then at /usr/local/bin/node and /opt/homebrew/bin/node.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_TESTS = sorted((ROOT / "tests" / "web").glob("*.test.mjs"))
WEB = ROOT / "brainsync" / "web"
MIN_NODE = 18   # node:test and ES modules without flags


def find_node() -> str | None:
    """Path of a node binary new enough for ``node --test``, or None."""
    candidates = [shutil.which("node"), "/usr/local/bin/node", "/opt/homebrew/bin/node"]
    for c in candidates:
        if not c or not os.access(c, os.X_OK):
            continue
        try:
            out = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        m = re.match(r"v(\d+)\.", out.stdout.strip())
        if m and int(m.group(1)) >= MIN_NODE:
            return c
    return None


NODE = find_node()


def test_web_files_present() -> None:
    for name in ("index.html", "style.css", "app.js", "logic.js"):
        assert (WEB / name).is_file(), name
    assert JS_TESTS, "no tests/web/*.test.mjs"


@pytest.mark.skipif(NODE is None, reason=f"node >= {MIN_NODE} not found")
def test_node_logic_tests_pass() -> None:
    # files, not the folder: `node --test <dir>` treats a directory as a module to load
    proc = subprocess.run(
        [NODE, "--test", "--test-reporter=tap", *map(str, JS_TESTS)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    report = proc.stdout + proc.stderr
    assert proc.returncode == 0, report[-4000:]
    m = re.search(r"^# pass (\d+)$", report, re.M)
    assert m and int(m.group(1)) > 0, report[-2000:]
    assert re.search(r"^# fail 0$", report, re.M), report[-2000:]


@pytest.mark.skipif(NODE is None, reason=f"node >= {MIN_NODE} not found")
def test_app_and_logic_parse_as_modules() -> None:
    # app.js touches the DOM at import, so only check its syntax; logic.js must import
    # cleanly with no DOM at all
    # (from stdin as a module: older node would read a bare .js file as CommonJS)
    check = subprocess.run([NODE, "--input-type=module", "--check"],
                           input=(WEB / "app.js").read_text(encoding="utf-8"),
                           capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stderr
    imp = subprocess.run(
        [NODE, "--input-type=module", "-e",
         f"const L = await import({str((WEB / 'logic.js').as_uri())!r}); "
         "if (typeof L.clampLayout !== 'function') process.exit(3);"],
        capture_output=True, text=True, timeout=60,
    )
    assert imp.returncode == 0, imp.stderr
