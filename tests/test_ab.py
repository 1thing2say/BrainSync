"""brainsync.ab: the live flicker A/B, run on the fake board and a fake stimulus process."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

from brainsync import ab, launch, record
from brainsync.board import FakeBoard
from brainsync.recordings import MONTAGE, list_recordings, load_recording
from brainsync.stimulus import Condition, load_config, override

AB = (Condition("square", 1.0), Condition("sine", 1.0), Condition("sine", 0.5))


def _run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    code = ab.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def _table(out: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("condition"))
    return lines[start:start + 5]


def test_fake_session_scores_every_condition_and_saves(tmp_path: Path,
                                                       capsys: pytest.CaptureFixture) -> None:
    code, out, err = _run(["--fake", "--seed", "3", "--warmup", "1",
                           "--save-dir", str(tmp_path)], capsys)
    assert code == 0, err
    assert "24 trials (4 block(s) x 2 target(s) x 3 condition(s)" in err
    assert sum(ln.startswith("trial ") for ln in err.splitlines()) == 24   # live, per trial
    table = _table(out)
    assert [ln.split()[0:2] for ln in table[1:4]] == [["square", "1"], ["sine", "1"],
                                                      ["sine", "0.5"]]
    assert all("8/8" in ln for ln in table[1:4])
    assert table[4].startswith("still screen: SNR") and "chance 50%" in table[4]
    path = list_recordings(directory=tmp_path)[0]
    assert path.name.endswith("_cued.npz") and str(path) in out
    assert f"-m brainsync.ab --score {path}" in out


def _session(seed: int = 2, blocks: int = 1) -> tuple[np.ndarray, np.ndarray, str, object]:
    cfg = override(load_config("two_targets"), blocks=blocks, rest_s=4.0, conditions=AB)
    board = FakeBoard(cfg.freqs, seed=seed, t0=1.79e9)
    X, t, log = ab.fake_session(board, cfg, seed, 1.0, 4.0, 4.0)
    return X, t, log, cfg


def test_fake_response_orders_the_conditions() -> None:
    X, t, log, cfg = _session(seed=1, blocks=4)
    conditions, trials = ab.parse_session(log)
    assert conditions == AB and len(trials) == 24
    scores = ab.score_session(X, t, 250.0, MONTAGE, cfg.freqs, cfg.phases, trials, 3.86, 0.14)
    rows = ab.summarize(scores, AB)
    assert [r.n for r in rows] == [8, 8, 8]
    snr = [r.snr_db for r in rows]
    assert snr[0] > snr[1] > snr[2] > 3.0             # square 4/pi, sine 1, sine 0.5
    assert abs(np.mean([s.still_snr_db for s in scores])) < 1.5
    assert rows[0].corr > rows[2].corr
    assert math.isnan(rows[0].comfort) and rows[0].n_rated == 0


def test_score_gives_the_same_table_as_the_session(tmp_path: Path,
                                                   capsys: pytest.CaptureFixture) -> None:
    code, out, _ = _run(["--fake", "--seed", "5", "--blocks", "2", "--method", "cca",
                         "--save-dir", str(tmp_path)], capsys)
    assert code == 0
    path = list_recordings(directory=tmp_path)[0]
    code, again, err = _run(["--score", str(path), "--method", "cca"], capsys)
    assert code == 0, err
    assert f"{path.name}: 12 of 12 trials scored" in again
    assert _table(again) == _table(out)


def test_score_refuses_a_session_without_conditions(tmp_path: Path,
                                                    capsys: pytest.CaptureFixture) -> None:
    assert record.main(["--fake", "--trials", "2", "--save-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    code, _, err = _run(["--score", str(list_recordings(directory=tmp_path)[0])], capsys)
    assert code == 1 and "not an A/B session" in err
    code, _, err = _run(["--score", str(tmp_path / "missing.npz")], capsys)
    assert code == 1 and "not found" in err


LOG = """measured refresh: 60.00 Hz
[marker 99.0000] stim_on:1
conditions: square:1 sine:0.5
cued session: 3 trials, seed 1
[marker 100.0000] cond:1
[marker 100.0000] cue:0
[marker 101.0000] stim_on:0
[marker 105.0000] stim_off:0
[marker 109.0000] rate:4
[marker 109.5000] cond:0
[marker 109.5000] cue:1
[marker 110.5000] stim_on:1
[marker 114.5000] stim_off:0
[marker 115.0000] cond:1
[marker 115.0000] cue:1
[marker 116.0000] stim_on:1
"""


def test_parse_session_groups_markers_by_trial() -> None:
    conditions, trials = ab.parse_session(LOG)
    assert conditions == (Condition("square", 1.0), Condition("sine", 0.5))
    assert len(trials) == 3                       # the stray stim_on before any cond is dropped
    first, second, third = trials
    assert (first.condition, first.target, first.t_on, first.t_off, first.rating) == (
        1, 0, 101.0, 105.0, 4)
    assert second.target == 1 and math.isnan(second.t_off)   # stim_off of another target
    assert third.t_on == 116.0 and math.isnan(third.t_off) and third.rating is None
    assert ab.parse_session("no markers here") == ((), [])


def _rated(log: str, ratings: dict[int, int]) -> str:
    """The log with a rate marker after the stim_off of each trial index in ``ratings``."""
    out, trial = [], -1
    for line in log.splitlines():
        out.append(line)
        if "] cond:" in line:
            trial += 1
        if "] stim_off:" in line and trial in ratings:
            stamp = float(line.split()[1].rstrip("]")) + 4.0
            out.append(f"[marker {stamp:.4f}] rate:{ratings[trial]}")
    return "\n".join(out) + "\n"


def test_blind_scoreboard_shows_a_trial_only_once_it_is_rated() -> None:
    X, t, log, cfg = _session()
    board = ab.Scoreboard(cfg, 250.0, MONTAGE, 3.86, 0.14, blind=True)
    assert board.update(X, t, log) == []          # nothing rated: nothing visible
    new = board.update(X, t, _rated(log, {0: 2, 3: 5}))
    assert [s.index for s in new] == [0, 3] and [s.rating for s in new] == [2, 5]
    new = board.update(X, t, _rated(log, {0: 2, 1: 1, 3: 5}))
    assert [s.index for s in new] == [1]
    assert sum(r.n_rated for r in ab.summarize(board.shown, AB)) == 3
    assert any("(n=" in ln for ln in board.lines())


def test_scoreboard_waits_for_the_still_window_and_the_edge() -> None:
    X, t, log, cfg = _session()
    _, trials = ab.parse_session(log)
    first_off = trials[0].t_off
    board = ab.Scoreboard(cfg, 250.0, MONTAGE, 3.86, 0.14, edge_s=1.0)
    cut = t <= first_off + 0.14 + 3.86 + 0.5      # still window done, edge not yet
    assert board.update(X[:, cut], t[cut], log) == []
    cut = t <= first_off + 0.14 + 3.86 + 1.1
    assert [s.index for s in board.update(X[:, cut], t[cut], log)] == [0]
    assert board.update(X[:, cut], t[cut], log) == []   # nothing new: no re-score


def test_tap_keeps_every_read() -> None:
    board = FakeBoard([15.0, 17.0], t0=0.0)
    tap = ab.Tap(board)
    assert tap.data()[0].shape == (8, 0)
    board.advance(0.5)
    tap.read()
    tap.read()                                    # empty read
    board.advance(0.5)
    tap.read()
    X, t = tap.data()
    assert X.shape == (8, 250) and np.all(np.diff(t) > 0)
    assert tap.data()[1] is t                     # consolidated once


@pytest.mark.parametrize("argv, match", [
    (["--rate"], "--rate needs the display"),
    (["--stim-s", "1"], "too short to measure an SNR"),
    (["--rest-s", "1"], "still window would overlap the next flicker"),
    (["--blocks", "0"], "--blocks must be >= 1"),
    (["--latency", "5"], "--latency must lie in"),
    (["--conditions", "sine:1,tri:1"], "unknown waveform"),
    (["--names", "a,b"], "8 different channel names"),
])
def test_bad_arguments_are_refused(argv: list[str], match: str, tmp_path: Path,
                                   capsys: pytest.CaptureFixture) -> None:
    code, _, err = _run(["--fake", "--save-dir", str(tmp_path), *argv], capsys)
    assert code == 2 and match in err and "Traceback" not in err
    assert not list(tmp_path.iterdir())


FAKE_STIMULUS = """
import json, sys, threading, time
status = open({status!r}, "w")
got = []
def read():
    for line in sys.stdin:
        got.append(json.loads(line)["text"])
        status.write(got[-1] + "\\n=====\\n"); status.flush()
threading.Thread(target=read, daemon=True).start()
def mark(ev):
    print(f"[marker {{time.time():.4f}}] {{ev}}", file=sys.stderr, flush=True)
print("measured refresh: 60.00 Hz", file=sys.stderr, flush=True)
print("conditions: square:1 sine:0.5", file=sys.stderr, flush=True)
for j, k in ((0, 0), (1, 0)):
    mark(f"cond:{{j}}"); mark(f"cue:{{k}}"); time.sleep(0.2)
    mark(f"stim_on:{{k}}"); time.sleep(1.7)
    mark(f"stim_off:{{k}}"); time.sleep(1.7)
    mark("rate:4")
end = time.time() + 15
while time.time() < end and not any("2 of 2 trials scored" in g for g in got):
    time.sleep(0.05)
print("timing: 2 trials, 0 dropped flicker frames")
"""


def test_live_path_scores_while_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture) -> None:
    import time

    from brainsync import board as bd

    seen: list[list[str]] = []
    status = tmp_path / "status.txt"

    def fake_launch(args: list[str], stdin: object = None) -> launch.Stimulus:
        seen.append(list(args))
        code = FAKE_STIMULUS.format(status=str(status))
        return launch.launch_stimulus(args, stdin=stdin, command=[sys.executable, "-c", code],
                                      timeout=20, log_dir=tmp_path)

    def fake_cerelog(serial_port: str | None = None, ch_names: list[str] | None = None,
                     verbose: bool = False) -> bd.FakeBoard:
        return bd.FakeBoard([15.0, 17.0], ch_names=ch_names, clock=time.time)

    monkeypatch.setattr(ab, "launch_stimulus", fake_launch)
    monkeypatch.setattr(ab, "speak", lambda text, voice=True: None)
    monkeypatch.setattr(ab, "EDGE_S", 0.2)
    monkeypatch.setattr(ab, "POLL_S", 0.1)
    monkeypatch.setattr(bd, "CerelogBoard", fake_cerelog)
    code, out, err = _run(["-c", "one_target_15", "--conditions", "square:1,sine:0.5",
                           "--blocks", "1",
                           "--cue-s", "0.2", "--stim-s", "1.7", "--rest-s", "1.7",
                           "--latency", "0.1", "--warmup", "0", "--rate", "--seed", "4",
                           "--save-dir", str(tmp_path / "rec")], capsys)
    assert code == 0, err
    args = seen[0]
    assert args[args.index("--conditions") + 1] == "square:1,sine:0.5"
    assert args[args.index("--status") + 1] == "stdin" and "--rate" in args
    assert args[args.index("--seed") + 1] == "4" and "--no-wait" in args
    shown = status.read_text().split("=====\n")
    assert shown[0].startswith("scores appear here")
    final = next(s for s in shown if "2 of 2 trials scored" in s)
    assert "square 1" in final and "4.0 (n=1)" in final        # reached the screen
    assert "trial 1/2  square 1 at 15 Hz" in err and "comfort 4" in err
    assert "2 of 2 trials scored" in out
    rec = load_recording(list_recordings(directory=tmp_path / "rec")[0])
    conditions, trials = ab.parse_session(rec.marker_text)
    assert conditions == (Condition("square", 1.0), Condition("sine", 0.5))
    assert [tr.rating for tr in trials] == [4, 4]
    assert rec.window_s == pytest.approx(1.6)
