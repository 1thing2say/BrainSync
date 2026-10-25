"""The browser BrainChess (web/brainchess) on the c-VEP standard: the cvep.js copy, the page's
three targets and confidence block, the Back trace the JS is checked against, and its node
tests (web/brainchess/test/cvep.test.mjs, which need no npm install)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "brainchess"
TRACE = WEB / "test" / "undo_trace.json"


def test_cvep_js_is_a_byte_identical_copy():
    assert (WEB / "cvep.js").read_bytes() == (ROOT / "clients" / "js" / "cvep.js").read_bytes(), \
        "copy clients/js/cvep.js to web/brainchess/cvep.js again"


def test_page_loads_the_standard_and_has_three_targets():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert html.index('<script src="cvep.js"></script>') < html.index('<script src="core.js"></script>')
    for needle in ('id="panel2"', 'id="conf"', 'name="coding"', 'value="code" checked',
                   "K.labelLine(", "K.statusLine(", "K.bit(", "C.MockCodeDecoder", "C.userLook"):
        assert needle in html, needle


def undo_trace() -> dict:
    """split.py MoveSelector on the start position, uniform weights, undo on, driven by the
    fixed script; the rows the JS test compares MoveSelector against."""
    chess = pytest.importorskip("chess")
    from brainsync.brainchess.split import MoveSelector
    board = chess.Board()
    sel = MoveSelector(board, {m: 1.0 for m in board.legal_moves}, undo=True)
    script = json.loads(TRACE.read_text())["script"]

    def row():
        return [sel.stage, sel.groups(), list(sel.panels()) if sel.panels() else None,
                None if sel.stage == "done" else sel.prompt(), sel.can_undo]

    trace = [row()]
    for t in script:
        sel.undo() if t == 2 else sel.choose(t)
        trace.append(row())
    return {"script": script, "trace": trace, "picks": sel.picks, "undos": sel.undos,
            "move": sel.move.uci() if sel.move else None}


def test_the_js_undo_trace_is_split_py():
    want = undo_trace()
    have = json.loads(TRACE.read_text())
    for key in ("script", "trace", "picks", "undos", "move"):
        assert have[key] == want[key], f"{TRACE.name} is stale in {key!r}; regenerate it"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_cvep_tests_pass():
    res = subprocess.run(["node", "--test", "cvep.test.mjs"], cwd=WEB / "test",
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
