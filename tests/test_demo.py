"""Tests for the live demo CLI, headless: FakeBoard and a stand-in stimulus process."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from brainsync import demo, launch
from brainsync.board import FakeBoard
from brainsync.classify import make
from brainsync.online import Decoder

HEADLESS = ["--fake", "--fast", "--no-stimulus", "--no-voice"]

# Stand-in for brainsync.stimulus --feedback stdin: reports its window, then copies each
# feedback line to a file until EOF (or exits at once with "exit", or crashes with code 1
# right after the first line with "crash").
FAKE_STIMULUS = """
import sys
print("measured refresh: 60.00 Hz", flush=True)
if sys.argv[2] == "exit":
    sys.exit(0)
with open(sys.argv[1], "w") as out:
    for line in sys.stdin:
        out.write(line)
        out.flush()
        if sys.argv[2] == "crash":
            sys.exit(1)
"""


@pytest.fixture
def fake_stimulus(monkeypatch, tmp_path):
    """Route demo.launch_stimulus to FAKE_STIMULUS; returns (calls, feedback file, set_mode)."""
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


def test_headless_fake_run_returns_zero_and_follows_the_board(capsys):
    assert demo.main([*HEADLESS, "--seconds", "30"]) == 0
    cap = capsys.readouterr()
    assert "ended: time up" in cap.out
    status = [ln for ln in cap.err.splitlines() if " committed " in ln]
    # first decision after window + pad (1.5 + 2 s), then one per 0.25 s step
    assert len(status) == pytest.approx((30 - 3.5) / 0.25 + 1, abs=2)
    assert status[0].strip().startswith("3.50 s")
    assert "[say] Keep still" in cap.err
    # FakeBoard switches every 10 s and starts the run on target 1 (after 30 s calibration)
    assert commits_from(cap.out)[:3] == ["17", "15", "17"]
    hit = int(cap.out.split("true one on ")[1].split("%")[0])
    assert hit >= 70


def test_spoken_labels_follow_commits(capsys):
    assert demo.main([*HEADLESS, "--seconds", "15"]) == 0
    cap = capsys.readouterr()
    said = [ln[6:] for ln in cap.err.splitlines() if ln.startswith("[say] ")]
    assert said[:2] == ["Keep still and look at the middle of the screen.",
                        "Look at one of the squares."]
    assert said[2:] == [c for c in commits_from(cap.out) if c != "none"]


def test_feedback_reaches_the_stimulus(fake_stimulus, capsys):
    calls, feedback, _ = fake_stimulus
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "25"]) == 0
    cap = capsys.readouterr()
    assert len(calls) == 1
    args = calls[0]["args"]
    for flag in ("--free", "--feedback", "--markers", "--hold-still"):
        assert flag in args
    assert "--refresh" not in args                    # the 60 Hz guard stays on
    assert args[args.index("--seconds") + 1] == "25"
    assert args[args.index("--markers") + 1] == "none"
    assert args[args.index("--feedback") + 1] == "stdin"
    assert calls[0]["stim"].proc.poll() == 0          # stdin closed -> it finished
    labels = {"15": "0", "17": "1", "none": "-1"}
    sent = feedback.read_text().split()
    assert sent[0] == "-1"                            # releases the still screen
    assert sent[1:] == [labels[c] for c in commits_from(cap.out)]
    assert set(sent) <= {"0", "1", "-1"}


def test_run_ends_when_the_stimulus_exits(fake_stimulus, capsys):
    calls, _, mode = fake_stimulus
    mode["value"] = "exit"
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "3000"]) == 0
    out = capsys.readouterr().out
    assert "ended: stimulus" in out           # exited, or its pipe closed first
    assert "before the flicker started" in out and "0 windows" in out
    assert calls[0]["stim"].proc.poll() is not None


def test_stimulus_crash_mid_run_exits_1(fake_stimulus, capsys):
    # regression: a crashed stimulus used to leave the demo's exit code at 0
    _, _, mode = fake_stimulus
    mode["value"] = "crash"
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "3000"]) == 1
    cap = capsys.readouterr()
    assert "ended: stimulus" in cap.out and "error: the stimulus exited with code 1" in cap.err


def test_stimulus_is_up_and_still_during_calibration(fake_stimulus, monkeypatch, capsys):
    calls, feedback, _ = fake_stimulus
    seen = []
    real = demo.calibrate

    def spy(decoder, board, seconds, **kw):
        seen.append((len(calls), calls[0]["stim"].proc.poll(),
                     feedback.read_text() if feedback.exists() else ""))
        return real(decoder, board, seconds, **kw)

    monkeypatch.setattr(demo, "calibrate", spy)
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "5"]) == 0
    assert seen == [(1, None, "")]            # window up, nothing sent: squares still


class InterruptingBoard(FakeBoard):
    """FakeBoard whose read raises KeyboardInterrupt (Ctrl-C) after ``reads`` reads."""

    reads = 400

    def read(self):
        type(self).reads -= 1
        if type(self).reads < 0:
            raise KeyboardInterrupt
        return super().read()


def test_ctrl_c_shuts_down_cleanly(fake_stimulus, monkeypatch, capsys):
    calls, _, _ = fake_stimulus
    InterruptingBoard.reads = 900                # past calibration (600 reads), into the run
    monkeypatch.setattr(demo, "FakeBoard", InterruptingBoard)
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "1000"]) == 0
    assert "ended: interrupted" in capsys.readouterr().out
    assert calls[0]["stim"].proc.poll() == 0


def test_ctrl_c_during_calibration_ends_before_decoding(fake_stimulus, monkeypatch, capsys):
    # regression: Ctrl-C in calibration used to fit a partial baseline and run the demo
    calls, feedback, _ = fake_stimulus
    InterruptingBoard.reads = 120
    monkeypatch.setattr(demo, "FakeBoard", InterruptingBoard)
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "1000"]) == 130
    cap = capsys.readouterr()
    assert "interrupted before decoding started" in cap.err
    assert "baseline from" not in cap.err and "Look at one of the squares" not in cap.err
    assert " committed " not in cap.err and cap.out == ""
    assert calls[0]["stim"].proc.poll() == 0          # closed, never released
    assert feedback.read_text() == ""


class SilentBoard(FakeBoard):
    """FakeBoard that stops delivering after calibration."""

    def read(self):
        X, t = super().read()
        assert self.elapsed < 300.0, "the run should have ended on the stall"
        if self.elapsed > 12.0:
            return X[:, :0], t[:0]
        return X, t


def test_board_stall_ends_the_run(monkeypatch, capsys):
    monkeypatch.setattr(demo, "FakeBoard", SilentBoard)
    assert demo.main([*HEADLESS, "--seconds", "60"]) == 1
    cap = capsys.readouterr()
    assert "ended: board stalled" in cap.out
    assert "sent nothing for 5 s" in cap.err


def test_fake_calibration_runs_on_a_still_board(monkeypatch, capsys):
    seen = []
    real = demo.calibrate

    def spy(decoder, board, seconds, **kw):
        seen.append(board.active_target())
        cal = real(decoder, board, seconds, **kw)
        seen.append(board.active_target())
        return cal

    monkeypatch.setattr(demo, "calibrate", spy)
    assert demo.main([*HEADLESS, "--seconds", "5", "--calibrate", "8"]) == 0
    assert seen[0] == -1                         # no tone while the baseline is fit
    assert "baseline from 19 still windows" in capsys.readouterr().err   # (8 - 3.5) / 0.25 + 1


class EmptyBoard:
    fs = 250.0
    ch_names = tuple("ABCDEFGH")

    def read(self):
        return np.empty((8, 0)), np.empty(0)


def test_run_reports_stall_after_five_seconds_of_clock():
    clf = make("cca", freqs=[15.0, 17.0], fs=250.0)
    dec = Decoder(clf, 250.0, ch_names=EmptyBoard.ch_names, channels=None)
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    assert demo.run(dec, EmptyBoard(), 30.0, lambda d: True, sleep=sleep,
                    clock=lambda: now[0]) == "board stalled"
    assert 5.0 <= now[0] < 5.3


def test_run_stops_on_reason_and_on_failed_feedback():
    clf = make("cca", freqs=[15.0, 17.0], fs=250.0)
    board = FakeBoard([15.0, 17.0])
    dec = Decoder(clf, 250.0, window_s=1.0, pad_s=0.5)
    assert demo.run(dec, board, 30.0, lambda d: True, sleep=board.advance,
                    clock=lambda: board.elapsed, stop=lambda: "stimulus exited") == "stimulus exited"
    assert demo.run(dec, board, 30.0, lambda d: False, sleep=board.advance,
                    clock=lambda: board.elapsed) == "feedback failed"
    assert dec.n_seen == dec.needed            # stopped at the first decision


def test_custom_settings_and_channels(capsys):
    assert demo.main([*HEADLESS, "--seconds", "8", "--classifier", "cca", "--window", "2",
                      "--step", "0.5", "--votes", "2", "--margin", "0", "--channels", "O1,7",
                      "--calibrate", "6"]) == 0
    err = capsys.readouterr().err
    assert "CCA(" in err and "channels O1,O2" in err and "window 2 s, step 0.5 s, 2 votes" in err
    assert sum(" committed " in ln for ln in err.splitlines()) == pytest.approx((8 - 4) / 0.5 + 1, abs=1)


@pytest.mark.parametrize("argv", [["--fast", "--no-stimulus"],
                                  [*HEADLESS, "--seconds", "0"],
                                  [*HEADLESS, "--names", "a,b,c"],
                                  [*HEADLESS, "--classifier", "svm"]])
def test_bad_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        demo.main(argv)
    assert exc.value.code == 2


def test_bad_channel_or_config_returns_2(capsys, tmp_path: Path):
    assert demo.main([*HEADLESS, "--channels", "Oz"]) == 2
    assert demo.main([*HEADLESS, "-c", str(tmp_path / "nope.toml")]) == 2
    err = capsys.readouterr().err
    assert "Oz" in err and "nope.toml" in err


def test_calibration_shorter_than_two_windows_fails(capsys):
    assert demo.main([*HEADLESS, "--calibrate", "2"]) == 1
    assert "calibration needs" in capsys.readouterr().err


def test_defaults_calibrate_long_and_use_the_threshold():
    args = demo.build_parser().parse_args([])
    assert args.calibrate >= 30.0
    assert args.threshold == 0.5 and args.margin == 0.5
    assert demo.build_parser().parse_args(["--threshold", "none"]).threshold is None


def test_short_calibration_warns(capsys):
    assert demo.main([*HEADLESS, "--seconds", "5", "--calibrate", "10"]) == 0
    assert "baseline is noisy" in capsys.readouterr().err


def test_status_line_shows_the_level(capsys):
    assert demo.main([*HEADLESS, "--seconds", "5"]) == 0
    status = [ln for ln in capsys.readouterr().err.splitlines() if " committed " in ln]
    assert status and all(" level " in ln for ln in status)
