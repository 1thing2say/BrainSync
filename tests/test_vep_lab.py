"""The VEP Lab explainer page (web/vep-lab): a static page, its node tests, and that its
JavaScript port of the codes and the kernel agrees with the Python it explains.

    pytest tests/test_vep_lab.py          # the node parts are skipped when node is missing

The page must load with no network (system fonts, vep_core.js beside it, the paper's DOI as
the only external link), carry the photosensitivity warning and a stop button, and draw
from the same numbers as ``python -m brainsync.vep``: the m-sequence bit for bit and the
flash VEP kernel to 1e-9.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from brainsync.synthetic import vep_kernel
from brainsync.vep.codes import m_sequence

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "web" / "vep-lab"
PAGE = LAB / "index.html"
CORE = LAB / "vep_core.js"
JS_TESTS = sorted((LAB / "test").glob("*.test.mjs"))
DOI = "https://doi.org/10.1109/MCI.2009.934562"
NODE = shutil.which("node") or next(
    (p for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if Path(p).exists()), None)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_files_present():
    assert PAGE.is_file() and CORE.is_file()
    assert JS_TESTS, "no web/vep-lab/test/*.test.mjs"


def test_page_makes_no_network_request_except_the_doi_link():
    html = page()
    urls = re.findall(r'\b(?:src|href)="([^"]+)"', html)
    external = [u for u in urls if "http" in u or u.startswith("//")]
    assert external == [DOI], external
    assert "@import" not in html and "url(" not in html.split("<body")[0]
    assert "fonts.googleapis" not in html and "<link rel=\"stylesheet\"" not in html
    assert re.findall(r'<script src="([^"]+)"', html) == ["vep_core.js"]


def test_page_has_the_warning_a_stop_button_and_the_three_paradigms():
    html = page()
    assert 'id="gate"' in html and "epilepsy" in html and "15–25 Hz" in html
    assert 'id="gate-yes"' in html and 'id="gate-no"' in html
    assert 'id="stop-all"' in html and "Stop flicker" in html
    assert "Escape" in html, "Esc must stop the flicker"
    assert html.count('data-start="') == 3
    for key in ("time", "freq", "code"):
        for part in ("seq", "resp", "fig", "dec", "live", "fix"):
            assert f'id="{key}-{part}"' in html, f"{key}-{part}"
    for text in ("TDMA", "FDMA", "CDMA", "85 %", "91 %", "39.7", "92.8", "under 30 bits/min",
                 "toy model", "10.1109/MCI.2009.934562"):
        assert text in html, text
    # never autoplay: the flicker loop starts only from a start button
    assert "requestAnimationFrame(tick)" in html
    assert html.count("requestAnimationFrame(") == 2   # startLive and tick's own re-arm
    assert re.search(r"\bautoplay\b", html) is None


def test_every_element_the_script_uses_is_on_the_page():
    html = page()
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    script = html.split("<script>", 1)[1]
    used = set(re.findall(r'\$\("([a-z-]+)"\)', script))
    assert used and not used - ids, sorted(used - ids)
    for key in ("time", "freq", "code"):
        for part in ("seq", "resp", "live", "fix", "dec"):
            assert f"{key}-{part}" in ids


def _node_eval(expr: str):
    code = (f"const V = require({json.dumps(str(CORE))}); "
            f"process.stdout.write(JSON.stringify({expr}));")
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_js_m_sequence_matches_python_bit_for_bit():
    js = _node_eval("V.mSequence(63)")
    assert js == m_sequence(63).tolist()
    assert _node_eval("V.mSequence(127)") == m_sequence(127).tolist()
    assert _node_eval("V.mSequence(63, 37)") == m_sequence(63, seed=37).tolist()


@needs_node
def test_js_kernel_matches_python_to_1e_9():
    js = np.asarray(_node_eval("V.vepKernel(250.0)"), dtype=float)
    py = vep_kernel(250.0)
    assert js.shape == py.shape == (125,)
    assert np.max(np.abs(js - py)) < 1e-9


@needs_node
def test_js_lagged_codes_and_autocorrelation_match_python():
    from brainsync.vep.codes import autocorrelation, lagged_codes

    s = m_sequence(63)
    assert _node_eval("V.laggedCodes(V.mSequence(63), 16, 4)") == lagged_codes(s, 16, 4).tolist()
    assert _node_eval("V.autocorrelation(V.mSequence(63))") == autocorrelation(s).tolist()


@needs_node
def test_node_tests_pass():
    env = dict(os.environ, NO_COLOR="1")
    proc = subprocess.run([NODE, "--test", "--test-reporter=tap", *map(str, JS_TESTS)], cwd=ROOT,
                          capture_output=True, text=True, timeout=120, env=env)
    report = proc.stdout + proc.stderr
    assert proc.returncode == 0, report[-4000:]
    assert re.search(r"^# fail 0$", report, re.M), report[-2000:]
    m = re.search(r"^# pass (\d+)$", report, re.M)
    assert m and int(m.group(1)) >= 10, report[-2000:]


@needs_node
def test_core_parses_and_exports_the_global_in_a_browser_like_scope():
    # the page reads window.VepCore; node has no window, so the file must fall back to globalThis
    code = (f"require({json.dumps(str(CORE))}); "
            "if (typeof globalThis.VepCore !== 'object') process.exit(3); "
            "if (globalThis.VepCore.mSequence(63).length !== 63) process.exit(4);")
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    # the page's own script is a plain classic script: a syntax check through a temp copy
    script = page().split("<script>", 1)[1].split("</script>", 1)[0]
    check = subprocess.run([NODE, "--check", "-"], input=script, capture_output=True, text=True,
                           timeout=60) if False else subprocess.run(
        [NODE, "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"], input=script,
        capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stderr
