"""The browser code's node tests (tests/js/) and a syntax check of every ui/*.js file."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.config import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_listen_core_node_tests_pass():
    r = subprocess.run([NODE, "--test", "tests/js/listen_core.test.js"], cwd=REPO_ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


@pytest.mark.parametrize("path", sorted((REPO_ROOT / "ui").glob("*.js")), ids=lambda p: p.name)
def test_ui_scripts_parse(path):
    r = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
