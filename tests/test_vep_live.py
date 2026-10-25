"""The c-VEP standard (docs/CVEP-STANDARD.md): brainsync.vep.live and clients/js/cvep.js."""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from brainsync.metrics import ABSTAIN
from brainsync.online import Decision
from brainsync.stimulus import load_config
from brainsync.stimulus.coded import CodeSequence
from brainsync.vep import live
from brainsync.vep.codes import m_sequence

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "clients" / "js" / "cvep.js"


def _run(board, dec, seconds, switches=None):
    commits, decisions = [], []
    steps = int(round(seconds / 0.1))
    for step in range(steps):
        if switches and step in switches:
            board.set_target(switches[step])
        board.advance(0.1)
        X, t = board.read()
        for d in dec.feed(X, t, board.read_events()):
            decisions.append(d)
            if d.changed and d.committed != ABSTAIN:
                commits.append(d.committed)
    return commits, decisions


def test_constants_match_the_cvep_3_config():
    cfg = load_config(live.CONFIG)
    assert cfg.n_targets == live.N_TARGETS == 3
    assert cfg.code.lag_frames == live.LAG_FRAMES == 21
    assert cfg.code.length == live.LENGTH == live.CYCLE_FRAMES == 63
    assert cfg.fill == (1.0, 1.0, 1.0)
    assert live.BITS == tuple(int(b) for b in m_sequence(63))
    seq = CodeSequence(cfg)
    for n in range(-70, 200):
        assert live.code_frame(n) == tuple(int(b) for b in seq.codes[:, n % 63])


def test_js_bits_and_constants_match_python():
    text = JS.read_text()
    bits = re.search(r"const BITS = Object\.freeze\(\[(.*?)\]\);", text, re.S).group(1)
    assert tuple(int(b) for b in re.findall(r"[01]", bits)) == live.BITS
    for name, value in (("LENGTH", 63), ("N_TARGETS", 3), ("LAG_FRAMES", 21),
                        ("CYCLE_FRAMES", 63), ("TRAIN_CYCLES", live.TRAIN_CYCLES),
                        ("BAR_CELLS", live.BAR_CELLS)):
        assert re.search(rf"const {name} = {value};", text), name


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_frame_rule_and_text_match_python():
    script = (
        f"const C = require({json.dumps(str(JS))});"
        "const out = {frames: [], bars: [], line: C.labelLine('YES', 0.42, true, 4),"
        " status: [C.statusLine(0.31, 'YES', true), C.statusLine(NaN, null, false)]};"
        "for (let n = -5; n < 130; n++) out.frames.push(C.frameBits(n));"
        "for (const v of [-0.2, 0, 0.06, 0.42, 0.99, 1.5, NaN]) out.bars.push(C.bar(v));"
        "console.log(JSON.stringify(out));")
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                    check=True).stdout)
    assert [tuple(f) for f in got["frames"]] == [live.code_frame(n) for n in range(-5, 130)]
    assert got["bars"] == [live.bar(v) for v in (-0.2, 0, 0.06, 0.42, 0.99, 1.5, math.nan)]
    d = Decision(time=0.0, scores=np.array([0.42, 0.1, -0.03]), raw=0, committed=0,
                 changed=True, gap=0.31, level=0.0)
    assert got["line"] == live.confidence_labels(d, ("YES", "NO", "HELP"))[0]
    assert got["status"][0] == live.confidence_line(d, ("YES", "NO", "HELP"))
    none = Decision(time=0.0, scores=np.zeros(3), raw=-1, committed=-1, changed=False,
                    gap=float("nan"), level=0.0)
    assert got["status"][1] == live.confidence_line(none, ("YES", "NO", "HELP"))


def test_decoder_trains_then_follows_the_gaze():
    board = live.standard_fake_board(0, seed=3)
    board.open()
    board.start_flicker()
    dec = live.CodeVepDecoder(board.fs)
    assert dec.training and dec.progress == (0, 30) and dec.committed == ABSTAIN
    seen = []
    while dec.training:              # nothing is decided while training
        board.advance(0.1)
        X, t = board.read()
        assert dec.feed(X, t, board.read_events()) == []
        seen.append(dec.progress[0])
        assert len(seen) < 400
    assert seen == sorted(seen) and dec.progress == (30, 30)
    assert dec.train_accuracy >= 0.9 and dec.chance == pytest.approx(1 / 3)
    commits, decisions = _run(board, dec, 30.0, {0: 0, 100: 1, 200: 2})
    assert commits == [0, 1, 2]
    assert decisions and all(len(d.scores) == 3 for d in decisions)


def test_template_skips_training_and_retrain_forgets_it():
    board = live.standard_fake_board(0, seed=4)
    board.open()
    board.start_flicker()
    dec = live.CodeVepDecoder(board.fs, train_cycles=10)
    _run(board, dec, 13.0)
    assert not dec.training
    other = live.CodeVepDecoder(board.fs, template=dec.template)
    assert not other.training and math.isnan(other.train_accuracy)
    other.retrain()
    assert other.training and other.template is None
    with pytest.raises(ValueError, match="targets"):
        live.CodeVepDecoder(board.fs, n_targets=4, lag_frames=21, template=dec.template)


@pytest.mark.parametrize("kw, match", [
    ({"n_targets": 1}, "n_targets"), ({"n_targets": 4, "lag_frames": 21}, "fit"),
    ({"train_cycles": 1}, "train_cycles"), ({"train_target": 3}, "train_target")])
def test_bad_arguments(kw, match):
    with pytest.raises(ValueError, match=match):
        live.CodeVepDecoder(250.0, **kw)


def test_confidence_message_and_lines():
    d = Decision(time=0.0, scores=np.array([0.42, 0.11, -0.03]), raw=0, committed=2,
                 changed=True, gap=0.31, level=0.0)
    c = live.confidence(d, ("YES", "NO", "HELP"))
    assert c.message() == {"scores": {"YES": 0.42, "NO": 0.11, "HELP": -0.03}, "raw": "YES",
                           "committed": "HELP", "lead": 0.31, "changed": True}
    lines = live.confidence_labels(d, ("YES", "NO", "HELP"))
    assert lines[0] == "YES  ███░░░░░ +0.42 <"
    assert live.confidence_line(d, ("YES", "NO", "HELP")) == "lead 0.31   choice: HELP *"
    none = Decision(time=0.0, scores=np.array([np.nan, 0.1, 0.1]), raw=-1, committed=-1,
                    changed=False, gap=float("nan"), level=0.0)
    m = live.confidence(none, ("YES", "NO", "HELP")).message()
    assert m["raw"] is None and m["committed"] is None and m["lead"] is None
    assert m["scores"]["YES"] == 0.0
