"""The browser code's node tests (tests/js/) and a syntax check of every ui/*.js file."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.config import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.mark.parametrize("path", sorted((REPO_ROOT / "tests" / "js").glob("*.test.js")),
                         ids=lambda p: p.name)
def test_node_tests_pass(path):
    r = subprocess.run([NODE, "--test", str(path.relative_to(REPO_ROOT))], cwd=REPO_ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


@pytest.mark.parametrize("path", sorted((REPO_ROOT / "ui").glob("*.js")), ids=lambda p: p.name)
def test_ui_scripts_parse(path):
    r = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_every_element_app_js_uses_is_on_the_page():
    """A renamed or removed id in ui/index.html would silently break a handler in ui/app.js."""
    import re

    page = (REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    used = set(re.findall(r'\$\("([^"]+)"\)', (REPO_ROOT / "ui" / "app.js").read_text(encoding="utf-8")))
    assert used and not used - ids, f"missing from index.html: {sorted(used - ids)}"


def test_the_page_loads_its_scripts_in_order():
    import re

    page = (REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    # ``?v=N`` only busts old caches; the cores come before app.js, which uses them.
    scripts = re.findall(r'<script src="([^"?]+)(?:\?[^"]*)?"', page)
    assert scripts == ["listen_core.js", "voice_core.js", "view_core.js", "flicker_core.js",
                       "speech.js", "app.js"]
