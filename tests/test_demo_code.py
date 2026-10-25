"""The live demo on the c-VEP standard (docs/CVEP-STANDARD.md), headless: the standard's
synthetic board and a stand-in stimulus process; no window, no board, no serial port."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from brainsync import demo, launch
from brainsync.metrics import ABSTAIN
from brainsync.stimulus.display import format_marker
from brainsync.vep import live

HEADLESS = ["--fake", "--fast", "--no-stimulus", "--no-voice"]

# Stand-in for brainsync.stimulus --free --feedback stdin: reports its window, then copies
# each feedback line to a file until EOF ("exit": exits at once; "crash": code 1 after the
# third line).
FAKE_STIMULUS = """
import sys
print("measured refresh: 60.00 Hz", flush=True)
if sys.argv[2] == "exit":
    sys.exit(0)
with open(sys.argv[1], "w") as out:
    for n, line in enumerate(sys.stdin, 1):
        out.write(line)
        out.flush()
        if sys.argv[2] == "crash" and n >= 3:
            sys.exit(1)
"""


@pytest.fixture
def fake_stimulus(monkeypatch, tmp_path):
    """Route demo.launch_stimulus to FAKE_STIMULUS; returns (calls, feedback file, mode)."""
    calls: list[dict] = []
    feedback = tmp_path / "feedback.txt"
    mode = {"value": "copy"}

    def fake_launch(args, stdin=None, **kw):
        cmd = [sys.executable, "-c", FAKE_STIMULUS, str(feedback), mode["value"]]
        stim = launch.launch_stimulus(args, stdin=stdin, command=cmd, log_dir=tmp_path)
        calls.append({"args": list(args), "stim": stim})
        return stim

    monkeypatch.setattr(demo, "launch_stimulus", fake_launch)
    return calls, feedback, mode


def commits_from(out: str) -> list[str]:
    line = next((ln for ln in out.splitlines() if ln.strip().startswith("commits:")), "")
    return [c.split("->")[1] for c in line.split()[1:]]


def json_lines(feedback: Path) -> list[dict]:
    return [json.loads(ln) for ln in feedback.read_text().splitlines() if ln.startswith("{")]


# -- arguments and paradigm ---------------------------------------------------------------

def test_the_default_is_the_cvep_standard():
    args = demo.build_parser().parse_args([])
    cfg = demo.resolve(args)
    assert args.paradigm == "code" and args.config == "cvep_3" == live.CONFIG
    assert cfg.labels == ("YES", "NO", "HELP") and cfg.code.lag_frames == live.LAG_FRAMES
    assert args.votes == live.VOTES and args.margin == 0.0
    assert args.train_cycles == live.TRAIN_CYCLES and args.cycles == live.CYCLES


@pytest.mark.parametrize("argv, paradigm, config", [
    (["--paradigm", "freq"], "freq", "two_targets"),
    (["-c", "two_targets"], "freq", "two_targets"),
    (["-c", "cvep_2"], "code", "cvep_2"),
    (["--paradigm", "code"], "code", "cvep_3"),
])
def test_the_paradigm_follows_the_flag_or_the_config(argv, paradigm, config):
    args = demo.build_parser().parse_args(argv)
    demo.resolve(args)
    assert (args.paradigm, args.config) == (paradigm, config)


@pytest.mark.parametrize("argv, message", [
    (["--paradigm", "code", "-c", "two_targets"], "not --paradigm code"),
    (["--paradigm", "freq", "-c", "cvep_3"], "not --paradigm freq"),
    (["-c", "tvep_4"], "t-VEP"),
    (["--no-stimulus"], "--no-stimulus needs --fake"),
])
def test_inconsistent_paradigms_exit_2(argv, message, capsys):
    assert demo.main([*argv, "--no-voice"]) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--train-cycles", "1"], ["--cycles", "0"],
                                  ["--votes", "0"], ["--margin", "-0.1"],
                                  ["--paradigm", "freq", "--template", "t.npz"],
                                  ["--template", "a.npz", "--save-template", "b.npz"]])
def test_bad_code_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        demo.main([*HEADLESS, *argv])
    assert exc.value.code == 2


def test_status_texts_match_the_standard():
    assert demo.training_text("YES", 12, 30) == "training: keep looking at YES   cycle 12/30"
    assert (demo.trained_text(0.83, 1 / 3)
            == "template ready: 83 % on held-out cycles (chance 33 %)")
    assert demo.trained_text(float("nan"), 0.5).startswith("template ready: - %")


def test_code_stimulus_args_tail_markers_and_hold_still():
    args = demo.build_parser().parse_args(["--ignore-refresh"])
    cfg = demo.resolve(args)
    out = demo.code_stimulus_args(args, cfg, ["--x"])
    assert out[:2] == ["-c", str(cfg.path)]
    for flag in ("--free", "--hold-still", "--no-wait", "--labels", "--ignore-refresh"):
        assert flag in out
    assert out[out.index("--markers") + 1] == "unix"       # the cycle:<n> lines
    assert out[out.index("--feedback") + 1] == "stdin"
    assert "--seconds" not in out and out[-1] == "--x"     # EOF ends it, after training


def test_fake_board_is_the_standard_one():
    args = demo.build_parser().parse_args(HEADLESS)
    cfg = demo.resolve(args)
    board, decoder = demo.build_code(args, cfg)
    ref = live.standard_fake_board()
    assert type(board) is type(ref) and board.n_targets == 3 and board.virtual
    assert decoder.training and decoder.n_targets == 3 and decoder.lag_frames == 21
    assert decoder.votes == live.VOTES and decoder.cycles == live.CYCLES


# -- runs ---------------------------------------------------------------------------------

def test_headless_run_trains_then_follows_the_fake_gaze(capsys):
    assert demo.main([*HEADLESS, "--seconds", "40"]) == 0
    cap = capsys.readouterr()
    assert "ended: time up" in cap.out and "c-VEP" in cap.out
    assert "template ready:" in cap.out and "(chance 33 %)" in cap.out
    # the fake gaze moves YES -> NO -> HELP -> YES every 10 s after training
    assert commits_from(cap.out) == ["YES", "NO", "HELP", "YES"]
    hit = int(cap.out.split("true one on ")[1].split("%")[0])
    assert hit >= 70
    status = [ln for ln in cap.err.splitlines() if " committed " in ln]
    assert len(status) == pytest.approx(40 / 1.05, abs=3)  # one decision per cycle
    said = [ln[8:] for ln in cap.err.splitlines() if ln.startswith("[speak] ")]
    assert said == ["Look at the YES square and keep looking at it.",
                    "Look at one of the squares.", "YES", "NO", "HELP", "YES"]


def test_feedback_lines_follow_the_standard(fake_stimulus, capsys):
    calls, feedback, _ = fake_stimulus
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "25"]) == 0
    out = capsys.readouterr().out
    assert len(calls) == 1 and calls[0]["stim"].proc.poll() == 0   # stdin closed: finished
    args = calls[0]["args"]
    assert "cvep_3" in args[1] and "--labels" in args and "--seconds" not in args
    lines = feedback.read_text().splitlines()
    assert json.loads(lines[0]) == {"flicker": True}       # JSON first: the flicker is ours
    assert lines[1] == "0"                                 # the cue box on the YES square
    assert json.loads(lines[2]) == {"text": "training: keep looking at YES   cycle 0/30"}
    texts = [m["text"] for m in json_lines(feedback) if "text" in m]
    counts = [t for t in texts if t.startswith("training:")]
    assert counts[-1].endswith("cycle 30/30")
    ready = next(i for i, t in enumerate(texts) if t.startswith("template ready:"))
    assert texts[ready].endswith("(chance 33 %)")
    after = texts[ready + 1:]
    assert after and all(t.startswith("lead ") for t in after)
    assert any("choice: YES" in t for t in after)
    labels = [m["labels"] for m in json_lines(feedback) if "labels" in m]
    assert labels and all(len(row) == 3 for row in labels)
    assert all(row[0].startswith("YES ") and row[1].startswith("NO ")
               and row[2].startswith("HELP") for row in labels)
    assert any(row[0].endswith("<") for row in labels)    # the best square is marked
    # integer lines: the training cue, clearing it, then every commit
    ints = [ln for ln in lines if not ln.startswith("{")]
    names = {"YES": "0", "NO": "1", "HELP": "2"}
    assert ints == ["0", "-1", *[names[c] for c in commits_from(out)]]


def test_template_round_trip_skips_training(tmp_path, capsys):
    path = tmp_path / "t.npz"
    assert demo.main([*HEADLESS, "--seconds", "5", "--save-template", str(path)]) == 0
    assert path.exists() and "template saved to" in capsys.readouterr().err
    assert demo.main([*HEADLESS, "--seconds", "16", "--template", str(path)]) == 0
    cap = capsys.readouterr()
    assert "training:" not in cap.err and "keep looking" not in cap.err
    assert "template ready" not in cap.out          # no held-out accuracy without training
    assert commits_from(cap.out)[:2] == ["YES", "NO"]


def test_stimulus_exit_during_training_ends_the_run(fake_stimulus, capsys):
    _, _, mode = fake_stimulus
    mode["value"] = "exit"
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "100"]) == 0
    out = capsys.readouterr().out
    assert "ended: stimulus" in out and ("before the flicker started" in out
                                         or "during training" in out)
    assert "0 decisions" in out


def test_stimulus_crash_exits_1(fake_stimulus, capsys):
    _, _, mode = fake_stimulus
    mode["value"] = "crash"
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "100"]) == 1
    assert "error: the stimulus exited with code 1" in capsys.readouterr().err


class _Wrapped:
    """A standard fake board with a hook on read (Ctrl-C, stalls, no flicker)."""

    def __init__(self, board, on_read):
        self.board, self.on_read = board, on_read

    def __getattr__(self, name):
        return getattr(self.board, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        X, t = self.board.read()
        return self.on_read(self.board, X, t)


def _wrap(monkeypatch, on_read, start_flicker=True):
    real = live.standard_fake_board

    def make(*a, **kw):
        board = real(*a, **kw)
        if not start_flicker:
            board.start_flicker = lambda: None
        return _Wrapped(board, on_read)

    monkeypatch.setattr(demo.live, "standard_fake_board", make)


def test_ctrl_c_during_training_ends_before_decoding(monkeypatch, capsys):
    def on_read(board, X, t):
        if board.elapsed > 10.0:
            raise KeyboardInterrupt
        return X, t

    _wrap(monkeypatch, on_read)
    assert demo.main([*HEADLESS, "--seconds", "100"]) == 130
    cap = capsys.readouterr()
    assert "interrupted before decoding started" in cap.err and cap.out == ""


def test_ctrl_c_while_decoding_exits_0(monkeypatch, capsys):
    def on_read(board, X, t):
        if board.elapsed > 50.0:
            raise KeyboardInterrupt
        return X, t

    _wrap(monkeypatch, on_read)
    assert demo.main([*HEADLESS, "--seconds", "100"]) == 0
    assert "ended: interrupted" in capsys.readouterr().out


def test_board_stall_exits_1(monkeypatch, capsys):
    def on_read(board, X, t):
        assert board.elapsed < 300.0, "the run should have ended on the stall"
        return (X[:, :0], t[:0]) if board.elapsed > 40.0 else (X, t)

    _wrap(monkeypatch, on_read)
    assert demo.main([*HEADLESS, "--seconds", "100"]) == 1
    cap = capsys.readouterr()
    assert "ended: board stalled" in cap.out and "sent nothing for 5 s" in cap.err


def test_no_cycle_markers_times_out_training(monkeypatch, capsys):
    _wrap(monkeypatch, lambda board, X, t: (X, t), start_flicker=False)
    assert demo.main([*HEADLESS, "--seconds", "10"]) == 1
    assert "is the display flickering?" in capsys.readouterr().err


def test_real_board_path_reads_cycles_from_the_stimulus_log(monkeypatch, tmp_path, capsys):
    # the real-board path: no --fake, so cycle events come from the stimulus log's
    # cycle:<n> markers (LogTail), which this stand-in board writes there as it flickers
    stims: list[launch.Stimulus] = []
    real_launch = launch.launch_stimulus

    def fake_launch(args, stdin=None, **kw):
        cmd = [sys.executable, "-c", FAKE_STIMULUS, str(tmp_path / "fb.txt"), "copy"]
        stims.append(real_launch(args, stdin=stdin, command=cmd, log_dir=tmp_path))
        return stims[-1]

    class LoggingBoard:
        def __init__(self, serial_port=None, ch_names=(), verbose=False):
            self.board = live.standard_fake_board(0, clock=time.time)
            self.fs, self.ch_names = self.board.fs, self.board.ch_names
            self.started = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self):
            if not self.started and stims and "flicker" in (tmp_path / "fb.txt").read_text():
                self.board.start_flicker()
                self.started = True
            X, t = self.board.read()
            with open(stims[0].log, "a") as log:
                for ts, kind, n in self.board.read_events():
                    log.write(format_marker(ts, f"{kind}:{n}") + "\n")
            return X, t

    monkeypatch.setattr(demo, "launch_stimulus", fake_launch)
    monkeypatch.setattr(demo, "CerelogBoard", LoggingBoard)
    assert demo.main(["--no-voice", "--train-cycles", "3", "--seconds", "4"]) == 0
    cap = capsys.readouterr()
    assert "Cerelog X8" in cap.err and "template ready" in cap.out
    assert "ended: time up" in cap.out
    assert sum(" committed " in ln for ln in cap.err.splitlines()) >= 1


def test_on_decision_failure_ends_the_session(capsys):
    args = demo.build_parser().parse_args([*HEADLESS, "--seconds", "30"])
    cfg = demo.resolve(args)
    board, decoder = demo.build_code(args, cfg)
    seen = []
    ended, code = demo.code_session(
        args, cfg, board, decoder, stimulus_args=[],
        on_decision=lambda d, t: seen.append((d, t)) and False)
    assert (ended, code) == ("stimulus pipe closed", 0) and len(seen) == 1
    # the first cycle after decoding began (within 1.05 s), its 1.05 s and the 0.5 s pad
    assert 1.5 <= seen[0][1] <= 2.7
    assert seen[0][0].committed in (ABSTAIN, 0)
