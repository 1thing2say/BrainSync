"""Tests for the reusable live-session seams of brainsync.demo, headless: FakeBoard and a
stand-in stimulus process (the pattern of tests/test_demo.py)."""
from __future__ import annotations

import argparse
import io
import sys
from types import SimpleNamespace

import pytest

from brainsync import demo, launch
from brainsync.board import FakeBoard
from brainsync.classify import ABSTAIN
from brainsync.online import Decision, Decoder

HEADLESS = ["--fake", "--fast", "--no-stimulus", "--no-voice"]

# Stand-in for brainsync.stimulus --feedback stdin: reports its window, then copies each
# feedback line to a file until EOF.
FAKE_STIMULUS = """
import sys
print("measured refresh: 60.00 Hz", flush=True)
with open(sys.argv[1], "w") as out:
    for line in sys.stdin:
        out.write(line)
        out.flush()
"""


@pytest.fixture
def fake_stimulus(monkeypatch, tmp_path):
    """Route demo.launch_stimulus to FAKE_STIMULUS; returns (calls, feedback file)."""
    calls: list[dict] = []
    feedback = tmp_path / "feedback.txt"

    def fake_launch(args, stdin=None, **kw):
        cmd = [sys.executable, "-c", FAKE_STIMULUS, str(feedback)]
        stim = launch.launch_stimulus(args, stdin=stdin, command=cmd, log_dir=tmp_path)
        calls.append({"args": list(args), "stim": stim})
        return stim

    monkeypatch.setattr(demo, "launch_stimulus", fake_launch)
    return calls, feedback


def parse(argv: list[str]) -> argparse.Namespace:
    p = demo.build_parser()
    args = p.parse_args(argv)
    demo.check_arguments(p, args)
    return args


# -- arguments --------------------------------------------------------------------------

def test_live_arguments_match_the_demo_parser():
    p = argparse.ArgumentParser(prog="other")
    demo.add_live_arguments(p)
    assert vars(p.parse_args([])) == vars(demo.build_parser().parse_args([]))


def test_live_arguments_take_other_defaults():
    p = argparse.ArgumentParser(prog="other")
    demo.add_live_arguments(p, config="swipe", seconds=600.0)
    p.add_argument("--cards", default=None)     # another app can add its own flags
    args = p.parse_args(["--cards", "d"])
    assert args.config == "swipe" and args.seconds == 600.0 and args.cards == "d"
    assert "default 600" in p.format_help()


def test_check_arguments_rejects_fast_without_fake():
    p = demo.build_parser()
    with pytest.raises(SystemExit) as exc:
        demo.check_arguments(p, p.parse_args(["--fast"]))
    assert exc.value.code == 2


def test_stimulus_args_append_extra():
    args = demo.build_parser().parse_args(["--seconds", "40", "--ignore-refresh"])
    base = demo.stimulus_args(args)
    assert base == demo._stimulus_args(args)            # old name kept for record's tests
    assert base[-1] == "--ignore-refresh"
    assert demo.stimulus_args(args, extra=["--cards", "/tmp/d"]) == [*base, "--cards", "/tmp/d"]


# -- send ---------------------------------------------------------------------------------

def _pipe_stim(pipe) -> launch.Stimulus:
    return launch.Stimulus(SimpleNamespace(stdin=pipe), None)   # type: ignore[arg-type]


def test_send_writes_ints_and_command_lines():
    pipe = io.StringIO()
    stim = _pipe_stim(pipe)
    assert demo.send(stim, ABSTAIN) and demo.send(stim, 1) and demo.send(stim, "card 3")
    assert pipe.getvalue() == "-1\n1\ncard 3\n"
    assert demo.send(None, "flash 0")                   # no stimulus: nothing to fail


def test_send_reports_a_closed_pipe():
    pipe = io.StringIO()
    pipe.close()
    assert demo.send(_pipe_stim(pipe), "flash 1") is False


# -- build ----------------------------------------------------------------------------------

def test_build_makes_the_configured_decoder():
    args = parse([*HEADLESS, "--window", "2", "--step", "0.5", "--votes", "2",
                  "--classifier", "cca"])
    cfg, board, decoder = demo.build(args)
    assert cfg.labels == ("15", "17")
    assert isinstance(board, FakeBoard) and isinstance(decoder, Decoder)
    assert decoder.window_s == 2.0 and decoder.step_s == 0.5 and decoder.votes == 2
    assert "CCA(" in repr(decoder.classifier)


def test_build_raises_on_a_bad_config(tmp_path):
    args = parse([*HEADLESS, "-c", str(tmp_path / "nope.toml")])
    with pytest.raises(demo.BUILD_ERRORS):
        demo.build(args)


# -- session -----------------------------------------------------------------------------------

def _run(argv: list[str], **kw) -> tuple[tuple[str, int], argparse.Namespace, Decoder]:
    args = parse(argv)
    cfg, board, decoder = demo.build(args)
    kw.setdefault("stimulus_args", demo.stimulus_args(args))
    kw.setdefault("on_decision", lambda d, elapsed: True)
    return demo.session(args, cfg, board, decoder, **kw), args, decoder


def test_session_hands_each_decision_and_its_time_to_the_caller(capsys):
    seen: list[tuple[Decision, float]] = []
    flicker: list[tuple[object, int]] = []
    holder: dict[str, Decoder] = {}

    def on_flicker(stim):
        flicker.append((stim, holder["decoder"].n_seen))
        return True

    args = parse([*HEADLESS, "--seconds", "10", "--calibrate", "8"])
    cfg, board, decoder = demo.build(args)
    holder["decoder"] = decoder
    ended, code = demo.session(args, cfg, board, decoder, stimulus_args=demo.stimulus_args(args),
                               on_flicker=on_flicker,
                               on_decision=lambda d, t: seen.append((d, t)) is None,
                               prompts=("Hold still.", "Start swiping."))
    assert (ended, code) == ("time up", 0)
    assert flicker == [(None, 0)]                      # once, before any decoding
    assert seen[0][1] == pytest.approx(3.5)            # window + pad after the flicker began
    assert seen[-1][1] == pytest.approx(10.0, abs=0.3)
    err = capsys.readouterr().err
    said = [ln[8:] for ln in err.splitlines() if ln.startswith("[speak] ")]
    assert said == ["Hold still.", "Start swiping."]
    assert "baseline from" in err
    assert " committed " not in err                    # status lines are the caller's
    assert capsys.readouterr().out == ""               # so is the summary


def test_session_sends_the_callers_lines_after_the_start(fake_stimulus):
    calls, feedback = fake_stimulus
    args = parse(["--fake", "--fast", "--no-voice", "--seconds", "8", "--calibrate", "8"])
    cfg, board, decoder = demo.build(args)
    flashes: list[int] = []

    def on_decision(d: Decision, elapsed: float) -> bool:
        if not d.changed or d.committed == ABSTAIN:
            return True
        flashes.append(d.committed)
        return demo.send(stim[0], f"flash {d.committed}")

    stim: list = []

    def on_flicker(s):
        stim.append(s)
        return demo.send(s, "card 0")

    ended, code = demo.session(args, cfg, board, decoder,
                               stimulus_args=demo.stimulus_args(args, ["--cards", "deck"]),
                               on_flicker=on_flicker, on_decision=on_decision)
    assert (ended, code) == ("time up", 0)
    assert calls[0]["args"][-2:] == ["--cards", "deck"]
    assert calls[0]["stim"].proc.poll() == 0           # closed by the session
    lines = feedback.read_text().splitlines()
    assert lines[:2] == ["-1", "card 0"]               # the start line comes first
    assert lines[2:] == [f"flash {k}" for k in flashes] and flashes


def test_failed_feedback_ends_the_session(fake_stimulus):
    (ended, code), _, _ = _run(["--fake", "--fast", "--no-voice", "--seconds", "300"],
                               on_decision=lambda d, t: False)
    assert (ended, code) == ("stimulus pipe closed", 0)


def test_failed_flicker_start_skips_decoding():
    decisions: list[Decision] = []
    (ended, code), _, decoder = _run([*HEADLESS, "--seconds", "300"],
                                     on_flicker=lambda s: False,
                                     on_decision=lambda d, t: decisions.append(d) is None)
    assert (ended, code) == ("stimulus pipe closed", 0) and decisions == []


class InterruptingBoard(FakeBoard):
    """FakeBoard whose read raises KeyboardInterrupt (Ctrl-C) after ``reads`` reads."""

    reads = 0

    def read(self):
        type(self).reads -= 1
        if type(self).reads < 0:
            raise KeyboardInterrupt
        return super().read()


def test_ctrl_c_during_calibration_is_not_started(monkeypatch, capsys):
    InterruptingBoard.reads = 120
    monkeypatch.setattr(demo, "FakeBoard", InterruptingBoard)
    (ended, code), _, _ = _run([*HEADLESS, "--seconds", "100"])
    assert (ended, code) == ("not started", 130)
    assert "interrupted before decoding started" in capsys.readouterr().err


def test_errors_in_the_callers_handler_end_with_code_1(capsys):
    def boom(d: Decision, elapsed: float) -> bool:
        raise RuntimeError("speech engine gone")

    (ended, code), _, _ = _run([*HEADLESS, "--seconds", "30"], on_decision=boom)
    assert (ended, code) == ("error", 1)
    assert "error: speech engine gone" in capsys.readouterr().err
