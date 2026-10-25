"""Tests for the live demo CLI, headless: FakeBoard and a stand-in stimulus process."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from brainsync import demo, launch
from brainsync.board import FakeBoard
from brainsync.classify import make
from brainsync.online import Decision, Decoder

HEADLESS = ["--fake", "--fast", "--no-stimulus", "--no-voice"]

# Stand-in for brainsync.stimulus --feedback stdin: reports its window, then copies each
# feedback line to a file until EOF (or exits at once with "exit", or crashes with code 1
# right after the first line with "crash").
FAKE_STIMULUS = """
import sys, time
print("measured refresh: 60.00 Hz", flush=True)
if sys.argv[2] == "exit":
    sys.exit(0)
with open(sys.argv[1], "w") as out:
    for n, line in enumerate(sys.stdin):
        out.write(line)
        out.flush()
        if n == 0 and sys.argv[2] == "onset":
            print(f"[marker {time.time():.4f}] free_on", flush=True)
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
    assert "[speak] Keep still" in cap.err
    # FakeBoard switches every 10 s and starts the run on target 1 (after 30 s calibration)
    assert commits_from(cap.out)[:3] == ["17", "15", "17"]
    hit = int(cap.out.split("true one on ")[1].split("%")[0])
    assert hit >= 70


def test_spoken_labels_follow_commits(capsys):
    assert demo.main([*HEADLESS, "--seconds", "15"]) == 0
    cap = capsys.readouterr()
    said = [ln[8:] for ln in cap.err.splitlines() if ln.startswith("[speak] ")]
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
    assert args[args.index("--markers") + 1] == "unix"       # the free run's onset marker
    assert args[args.index("--feedback") + 1] == "stdin"
    assert calls[0]["stim"].proc.poll() == 0          # stdin closed -> it finished
    labels = {"15": "0", "17": "1", "none": "-1"}
    lines = feedback.read_text().splitlines()
    sent = [ln for ln in lines if not ln.startswith("lean ")]
    assert sent[0] == "-1"                            # releases the still screen
    assert sent[1:] == [labels[c] for c in commits_from(cap.out)]
    assert set(sent) <= {"0", "1", "-1"}
    # one lean line per decoded window, after the line that starts the flicker
    leans = [ln.split() for ln in lines if ln.startswith("lean ")]
    status = [ln for ln in cap.err.splitlines() if " committed " in ln]
    assert len(leans) == len(status) and not lines[0].startswith("lean ")
    for _, k, pct, n, m in leans:
        assert k in ("0", "1") and 50 <= int(pct) <= 100 and 0 <= int(n) <= int(m) == 4


def test_no_lean_sends_only_cues(fake_stimulus, capsys):
    _, feedback, _ = fake_stimulus
    assert demo.main(["--fake", "--fast", "--no-voice", "--seconds", "12", "--no-lean"]) == 0
    capsys.readouterr()
    assert not any(ln.startswith("lean") for ln in feedback.read_text().splitlines())


def test_confidence_is_a_softmax_in_rest_spreads():
    share = demo.confidence(np.array([0.2, 0.0, 0.0, 0.0]), spread=0.1)
    assert share.sum() == pytest.approx(1.0)
    assert share[0] == pytest.approx(np.exp(2) / (np.exp(2) + 3))
    assert demo.confidence(np.zeros(4), 0.1) == pytest.approx(np.full(4, 0.25))
    # a zero spread is treated as 1 rather than dividing by zero
    assert np.isfinite(demo.confidence(np.array([1.0, 0.0]), 0.0)).all()


def _decision(scores, raw, committed):
    return Decision(time=0.0, scores=np.asarray(scores, float), raw=raw, committed=committed,
                    changed=False)


def test_lean_counts_agreeing_windows_until_the_commit():
    lean = demo.Lean(votes=3)
    lead0 = [0.3, 0.0]
    assert lean.line(_decision(lead0, -1, -1), 0.1) == "lean 0 95 0 3"   # abstained: no vote
    assert lean.update(_decision(lead0, 0, -1), 0.1)[2] == 1
    assert lean.update(_decision(lead0, 0, -1), 0.1)[2] == 2
    assert lean.update(_decision(lead0, 0, 0), 0.1)[2] == 3              # committed: full
    assert lean.update(_decision(lead0, -1, 0), 0.1)[2] == 3             # still committed
    # the lead moves to target 1: its count starts over
    assert lean.update(_decision([0.0, 0.3], 1, 0), 0.1) == (1, 95, 1)
    assert lean.update(_decision([0.0, 0.3], 1, 0), 0.1)[2] == 2
    assert lean.update(_decision([0.3, 0.0], 0, 0), 0.1)[2] == 3


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


# -- the trained classifier (--classifier trca --model) -----------------------------------

def _trca_model(path: Path, freqs, ch_names) -> Path:
    """A small TRCA model fitted directly on synthetic trials (no CV), saved to ``path``."""
    from brainsync.synthetic import ssvep_trials
    from brainsync.trca import TRCA, ModelInfo

    X, y = ssvep_trials(freqs, n_per_target=3, n_channels=len(ch_names), seconds=3.86, seed=1)
    info = ModelInfo(ch_names=tuple(ch_names), band=(1.0, 100.0), notch=60.0, latency=0.14,
                     trial_s=3.86, sources=("synthetic",), created=0.0)
    return TRCA(freqs, 250.0, fusion=(1.0, 1.0)).fit(X, y, info=info).save(path)


@pytest.fixture(scope="module")
def two_target_model(tmp_path_factory) -> Path:
    """A model trained by brainsync.train on a fake session with two_targets' 15 / 17 Hz."""
    from brainsync import train
    from brainsync.recordings import save_recording
    from brainsync.synthetic import fake_session

    root = tmp_path_factory.mktemp("trca_demo")
    labels = [0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0]
    X, t, text = fake_session([15.0, 17.0], labels, seed=4)
    rec = save_recording(root / "2026-01-01_000000_cued.npz", X, t, 250.0,
                         ("Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2"), [15.0, 17.0], text)
    model = root / "model.npz"
    assert train.main([str(rec), "--out", str(model), "--no-replay", "--windows", "1.5"]) == 0
    return model


def test_parser_accepts_trca_with_a_model(tmp_path: Path) -> None:
    args = demo.build_parser().parse_args(["--classifier", "trca", "--model", str(tmp_path)])
    assert args.classifier == "trca" and args.model == str(tmp_path)
    assert demo.build_parser().parse_args([]).model is None


@pytest.mark.parametrize("argv", [["--classifier", "trca"],
                                  ["--classifier", "fbcca", "--model", "m.npz"],
                                  ["--model", "latest"]])
def test_trca_and_model_go_together(argv) -> None:
    with pytest.raises(SystemExit) as exc:
        demo.main([*HEADLESS, *argv])
    assert exc.value.code == 2


def test_headless_run_with_a_trained_model(two_target_model, capsys) -> None:
    assert demo.main([*HEADLESS, "--classifier", "trca", "--model", str(two_target_model),
                      "--seconds", "20"]) == 0
    cap = capsys.readouterr()
    assert "TRCA(" in cap.err and "model=['2026-01-01_000000_cued.npz']" in cap.err
    assert "baseline from" in cap.err and "ended: time up" in cap.out
    hit = int(cap.out.split("true one on ")[1].split("%")[0])
    assert hit >= 70


def test_lock_needs_trca() -> None:
    with pytest.raises(SystemExit) as exc:
        demo.main([*HEADLESS, "--lock"])
    assert exc.value.code == 2


def test_lock_takes_the_onset_from_the_stimulus_log(two_target_model, fake_stimulus,
                                                     capsys) -> None:
    _, _, mode = fake_stimulus
    mode["value"] = "onset"
    assert demo.main(["--fake", "--fast", "--no-voice", "--classifier", "trca", "--model",
                      str(two_target_model), "--lock", "--seconds", "6"]) == 0
    cap = capsys.readouterr()
    assert "flicker onset at 17" in cap.err and "warning: " not in cap.err
    status = [ln for ln in cap.err.splitlines() if " committed " in ln]
    assert status and all("  locked" in ln for ln in status)


def test_lock_without_an_onset_decodes_phase_free(two_target_model, capsys) -> None:
    assert demo.main([*HEADLESS, "--classifier", "trca", "--model", str(two_target_model),
                      "--lock", "--seconds", "6"]) == 0
    cap = capsys.readouterr()
    assert "warning: no stimulus; decoding phase-free" in cap.err
    status = [ln for ln in cap.err.splitlines() if " committed " in ln]
    assert status and not any("locked" in ln for ln in status)


def test_model_with_other_targets_exits_2(tmp_path: Path, capsys) -> None:
    model = _trca_model(tmp_path / "four_trca.npz", [11.0, 15.0, 17.0, 13.0],
                        ("P7", "P8", "O1", "O2"))
    assert demo.main([*HEADLESS, "--classifier", "trca", "--model", str(model)]) == 2
    assert "freq" in capsys.readouterr().err


def test_model_on_other_channels_exits_2(tmp_path: Path, capsys) -> None:
    model = _trca_model(tmp_path / "all8_trca.npz", [15.0, 17.0],
                        ("Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2"))
    assert demo.main([*HEADLESS, "--classifier", "trca", "--model", str(model)]) == 2
    assert "channels" in capsys.readouterr().err


def test_latest_model_without_any_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("BRAINSYNC_DATA_DIR", str(tmp_path))
    assert demo.main([*HEADLESS, "--classifier", "trca", "--model", "latest"]) == 2
    assert "no trained model" in capsys.readouterr().err


def test_latest_model_is_found_in_the_models_dir(two_target_model, tmp_path: Path,
                                                 monkeypatch, capsys) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "2026-01-01_000000_trca.npz").write_bytes(two_target_model.read_bytes())
    monkeypatch.setenv("BRAINSYNC_DATA_DIR", str(tmp_path))
    assert demo.main([*HEADLESS, "--classifier", "trca", "--model", "latest",
                      "--seconds", "5"]) == 0
    assert "TRCA(" in capsys.readouterr().err


def test_printed_run_it_command_runs(tmp_path: Path, capsys) -> None:
    """train's "run it" line, with non-default channels, harmonics and decoder settings,
    is a demo command check_compatible accepts as printed."""
    import shlex

    from brainsync import train
    from brainsync.recordings import MONTAGE, save_recording
    from brainsync.synthetic import fake_session

    labels = [0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0]
    X, t, text = fake_session([15.0, 17.0], labels, seed=4)
    rec = save_recording(tmp_path / "2026-01-02_000000_cued.npz", X, t, 250.0, MONTAGE,
                         [15.0, 17.0], text)
    model = tmp_path / "o1o2_h2.npz"
    assert train.main([str(rec), "--out", str(model), "--no-replay", "--windows", "1",
                       "--channels", "O1,O2", "--harmonics", "2", "--window", "1",
                       "--votes", "3", "--margin", "0.3"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("run it: "))
    argv = shlex.split(line[len("run it: "):])[3:]                 # after python -m brainsync.demo
    assert demo.main([*argv, *HEADLESS, "--seconds", "20"]) == 0
    cap = capsys.readouterr()
    assert "TRCA(" in cap.err and "ended: time up" in cap.out


@pytest.mark.parametrize("payload", [b"", b"PK\x03\x04" + b"\0" * 64, b"not a model\n"])
def test_unreadable_model_exits_2(two_target_model, tmp_path: Path, monkeypatch, capsys,
                                  payload: bytes) -> None:
    models = tmp_path / "models"
    models.mkdir()
    bad = models / "2026-01-02_000000_trca.npz"          # newer than any good model
    bad.write_bytes(payload)
    (models / "2026-01-01_000000_trca.npz").write_bytes(two_target_model.read_bytes())
    assert demo.main([*HEADLESS, "--classifier", "trca", "--model", str(bad)]) == 2
    assert "not a readable TRCA model file" in capsys.readouterr().err
    monkeypatch.setenv("BRAINSYNC_DATA_DIR", str(tmp_path))
    assert demo.main([*HEADLESS, "--classifier", "trca", "--model", "latest"]) == 2
    assert f"{bad.name}: not a readable TRCA model file" in capsys.readouterr().err
