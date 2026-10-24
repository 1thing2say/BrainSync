from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from brainsync import launch, record
from brainsync.recordings import cut_windows, list_recordings, load_recording


def _run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    code = record.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_fake_cued_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code, out, err = _run(["--fake", "--trials", "6", "--seed", "1", "--warmup", "1",
                           "--save-dir", str(tmp_path)], capsys)
    assert code == 0, err
    files = list_recordings(directory=tmp_path)
    assert len(files) == 1
    path = files[0]
    assert path.name.endswith("_cued.npz")
    assert len(path.stem) == len("2026-10-23_120000_cued")
    assert str(path) in out
    assert "brainsync.score_cued" in out
    rec = load_recording(path)
    assert rec.kind == "cued"
    np.testing.assert_allclose(rec.freqs, [15.0, 17.0])
    assert rec.window_s == pytest.approx(3.86)
    assert rec.latency == pytest.approx(0.14)
    assert rec.ch_names == ("Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2")
    win = cut_windows(rec)
    assert len(win) == 6
    assert np.bincount(win.labels).tolist() == [3, 3]
    assert win.on.shape == (6, 8, round(3.86 * 250))


def test_fake_flicker_windows_carry_the_cued_tone(tmp_path: Path,
                                                  capsys: pytest.CaptureFixture) -> None:
    assert _run(["--fake", "--trials", "4", "--save-dir", str(tmp_path)], capsys)[0] == 0
    rec = load_recording(list_recordings(directory=tmp_path)[0])
    win = cut_windows(rec).pick(["O1", "O2"])
    f = np.fft.rfftfreq(win.on.shape[-1], 1 / rec.fs)
    p_on, p_off = [], []
    for on, off, k in zip(win.on, win.off, win.labels):
        bin_k = np.argmin(np.abs(f - rec.freqs[k]))
        p_on.append(np.mean(np.abs(np.fft.rfft(on, axis=-1))[:, bin_k] ** 2))
        p_off.append(np.mean(np.abs(np.fft.rfft(off, axis=-1))[:, bin_k] ** 2))
    assert np.mean(p_on) > 4 * np.mean(p_off)


def test_fake_single_target_saves_trials_file(tmp_path: Path,
                                              capsys: pytest.CaptureFixture) -> None:
    code, out, err = _run(["--fake", "-c", "one_target_15", "--trials", "3",
                           "--save-dir", str(tmp_path)], capsys)
    assert code == 0, err
    files = list_recordings("trials", directory=tmp_path)
    assert len(files) == 1
    rec = load_recording(files[0])
    assert rec.kind == "trials"
    np.testing.assert_allclose(rec.freqs, [15.0])
    assert len(cut_windows(rec)) == 3
    assert "python -m brainsync." in out


def test_trials_round_up_to_whole_blocks(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code, _, err = _run(["--fake", "--trials", "5", "--save-dir", str(tmp_path)], capsys)
    assert code == 0
    assert "6 trials (3 block(s) x 2 target(s)" in err
    assert len(cut_windows(load_recording(list_recordings(directory=tmp_path)[0]))) == 6


@pytest.mark.parametrize("argv, match", [
    (["--rest-s", "1"], "still window"),
    (["--cue-s", "0", "--rest-s", "3"], "shorter than the 3.86 s window"),
    (["--trials", "0"], "--trials"),
    (["--latency", "5"], "--latency"),
    (["--names", "a,b,c"], "8 channel names"),
    (["-c", "no_such_config"], "no_such_config"),
])
def test_bad_arguments_save_nothing(argv: list[str], match: str, tmp_path: Path,
                                    capsys: pytest.CaptureFixture) -> None:
    code, _, err = _run(["--fake", "--save-dir", str(tmp_path), *argv], capsys)
    assert code == 2
    assert match in err
    assert not list(tmp_path.iterdir())


def test_still_window_exactly_fitting_is_allowed(tmp_path: Path,
                                                 capsys: pytest.CaptureFixture) -> None:
    code, _, err = _run(["--fake", "--trials", "2", "--cue-s", "0.86", "--rest-s", "3",
                         "--save-dir", str(tmp_path)], capsys)
    assert code == 0, err
    assert len(cut_windows(load_recording(list_recordings(directory=tmp_path)[0]))) == 2


def test_stimulus_args_pass_timing_through() -> None:
    args = record.build_parser().parse_args(["--cue-s", "1.5", "--no-hold-cue", "--seed", "4"])
    out = record._stimulus_args(args, blocks=7)
    assert out[:2] == ["-c", "two_targets"]
    joined = " ".join(out)
    for part in ("--blocks 7", "--cue-s 1.5", "--stim-s 4", "--rest-s 3", "--markers unix",
                 "--no-wait", "--no-hold-cue", "--seed 4"):
        assert part in joined
    # every flag must be one the stimulus CLI accepts
    from brainsync.stimulus.__main__ import build_parser as stim_parser
    stim_parser().parse_args(out + ["--plan"])


def test_stimulus_command_adds_nothing() -> None:
    cmd = launch.stimulus_command(["-c", "two_targets"])
    assert cmd == [sys.executable, "-m", "brainsync.stimulus", "-c", "two_targets"]
    assert launch.stimulus_command(["--refresh", "60"]).count("--refresh") == 1


def _refresh_verdict(cmd: list[str], measured: float) -> str:
    """Run the stimulus's own refresh check on a launcher command line."""
    from brainsync.stimulus import __main__ as stim
    args = stim.build_parser().parse_args(cmd[3:])
    cfg = stim._configure(args)
    checked = stim._check_refresh(cfg, args, measured)
    return "refused" if checked is None else f"{checked.refresh:g}"


@pytest.mark.parametrize("which", ["record", "demo"])
def test_launched_stimulus_refuses_a_120_hz_display(which: str,
                                                    capsys: pytest.CaptureFixture) -> None:
    # regression: the launcher used to append --refresh auto, which adopted 120 Hz
    from brainsync import demo
    if which == "record":
        args = record._stimulus_args(record.build_parser().parse_args([]), blocks=10)
    else:
        args = demo._stimulus_args(demo.build_parser().parse_args([]))
    cmd = launch.stimulus_command(args)
    assert "auto" not in cmd
    assert _refresh_verdict(cmd, 120.0) == "refused"
    assert "error: measured refresh 120.00 Hz differs" in capsys.readouterr().err
    assert _refresh_verdict(cmd, 59.9) == "60"


def test_ignore_refresh_is_passed_through() -> None:
    from brainsync import demo
    rec_args = record._stimulus_args(record.build_parser().parse_args(["--ignore-refresh"]), 1)
    demo_args = demo._stimulus_args(demo.build_parser().parse_args(["--ignore-refresh"]))
    assert "--ignore-refresh" in rec_args and "--ignore-refresh" in demo_args
    assert "--ignore-refresh" not in record._stimulus_args(record.build_parser().parse_args([]), 1)


def _script(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_launch_returns_when_refresh_is_measured(tmp_path: Path) -> None:
    code = ("import sys, time\n"
            "print('starting', file=sys.stderr, flush=True)\n"
            "time.sleep(0.3)\n"
            "print('measured refresh: 60.00 Hz', file=sys.stderr, flush=True)\n"
            "print('[marker 1.0000] stim_on:0', file=sys.stderr, flush=True)\n"
            "time.sleep(0.2)\n")
    stim = launch.launch_stimulus([], command=_script(code), timeout=20, log_dir=tmp_path)
    assert stim.log.parent == tmp_path
    assert stim.finish(timeout=10) == 0
    assert "stim_on:0" in stim.text()
    proc, log = stim  # unpacks like a tuple
    assert isinstance(proc, subprocess.Popen) and log == stim.log


def test_launch_raises_with_log_path_when_stimulus_exits(tmp_path: Path) -> None:
    code = "import sys; print('error: no display', file=sys.stderr); sys.exit(2)"
    with pytest.raises(RuntimeError, match="exited with code 2") as info:
        launch.launch_stimulus([], command=_script(code), timeout=20, log_dir=tmp_path)
    logs = list(tmp_path.glob("brainsync_stimulus_*.log"))
    assert len(logs) == 1 and str(logs[0]) in str(info.value)
    assert "no display" in logs[0].read_text()
    assert "(error: no display)" in str(info.value)       # the reason, not just the log path


def test_launch_kills_the_stimulus_on_ctrl_c(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def spy(*a: object, **k: object) -> subprocess.Popen:
        started.append(real_popen(*a, **k))
        return started[-1]

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(launch.subprocess, "Popen", spy)
    monkeypatch.setattr(launch.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        launch.launch_stimulus([], command=_script("import time; time.sleep(30)"), timeout=20,
                               log_dir=tmp_path)
    assert started and started[0].poll() is not None


def test_launch_times_out_and_kills(tmp_path: Path) -> None:
    code = "import time; time.sleep(30)"
    with pytest.raises(RuntimeError, match="within 0.5 s"):
        launch.launch_stimulus([], command=_script(code), timeout=0.5, log_dir=tmp_path)


def test_speak_silent_prints_only(monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("say must not run")

    monkeypatch.setattr(launch.subprocess, "Popen", boom)
    launch.speak("look at the square", False)
    assert "[say] look at the square" in capsys.readouterr().err
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    launch.speak("no say here", True)
    assert "no say here" in capsys.readouterr().err


def test_speak_passes_voice_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/usr/bin/say")
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **k: seen.append(cmd))
    launch.speak("hello", "Samantha")
    launch.speak("hi", True)
    assert seen == [["say", "-v", "Samantha", "hello"], ["say", "hi"]]


FAKE_STIMULUS = """
import sys, time
def mark(ev):
    print(f"[marker {time.time():.4f}] {ev}", file=sys.stderr, flush=True)
print("measured refresh: 60.00 Hz", file=sys.stderr, flush=True)
for k in (0, 1):
    mark(f"cue:{k}"); time.sleep(0.3)
    mark(f"stim_on:{k}"); time.sleep(0.5)
    mark(f"stim_off:{k}"); time.sleep(0.3)
print("finished: 2 trials, 0 dropped frames")
"""


def test_live_path_with_fake_board_and_fake_stimulus(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture) -> None:
    import time

    from brainsync import board as bd

    seen: list[list[str]] = []

    def fake_launch(args: list[str]) -> launch.Stimulus:
        seen.append(list(args))
        return launch.launch_stimulus(args, command=_script(FAKE_STIMULUS), timeout=20,
                                      log_dir=tmp_path)

    def fake_cerelog(serial_port: str | None = None, ch_names: list[str] | None = None,
                     verbose: bool = False) -> bd.FakeBoard:
        return bd.FakeBoard([15.0, 17.0], ch_names=ch_names, clock=time.time)

    monkeypatch.setattr(record, "launch_stimulus", fake_launch)
    monkeypatch.setattr(record, "speak", lambda text, voice=True: None)
    monkeypatch.setattr(bd, "CerelogBoard", fake_cerelog)
    out_dir = tmp_path / "rec"
    code, out, err = _run(["--trials", "2", "--cue-s", "0.3", "--stim-s", "0.5",
                           "--rest-s", "0.3", "--latency", "0.1", "--warmup", "0.2",
                           "--save-dir", str(out_dir)], capsys)
    assert code == 0, err
    assert "--blocks" in seen[0] and seen[0][seen[0].index("--blocks") + 1] == "1"
    rec = load_recording(list_recordings(directory=out_dir)[0])
    assert "measured refresh" in rec.marker_text and "finished: 2 trials" in rec.marker_text
    assert rec.window_s == pytest.approx(0.4)
    win = cut_windows(rec)
    assert sorted(win.labels.tolist()) == [0, 1]


def test_slow_stimulus_startup_does_not_cut_the_last_trial(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture) -> None:
    import time

    from brainsync import board as bd

    def slow_launch(args: list[str]) -> launch.Stimulus:
        time.sleep(2.0)  # PsychoPy import and refresh measurement, while the board buffers
        return launch.launch_stimulus(args, command=_script(FAKE_STIMULUS), timeout=20,
                                      log_dir=tmp_path)

    def fake_cerelog(serial_port: str | None = None, ch_names: list[str] | None = None,
                     verbose: bool = False) -> bd.FakeBoard:
        return bd.FakeBoard([15.0, 17.0], ch_names=ch_names, clock=time.time)

    monkeypatch.setattr(record, "TAIL_S", 0.5)
    monkeypatch.setattr(record, "launch_stimulus", slow_launch)
    monkeypatch.setattr(record, "speak", lambda text, voice=True: None)
    monkeypatch.setattr(bd, "CerelogBoard", fake_cerelog)
    code, _, err = _run(["--trials", "2", "--cue-s", "0.3", "--stim-s", "0.5", "--rest-s", "0.3",
                         "--latency", "0.1", "--warmup", "0", "--save-dir", str(tmp_path / "rec")],
                        capsys)
    assert code == 0, err
    rec = load_recording(list_recordings(directory=tmp_path / "rec")[0])
    assert len(cut_windows(rec)) == 2
    assert "incomplete" not in err


def test_duplicate_names_are_an_argument_error(tmp_path: Path,
                                               capsys: pytest.CaptureFixture) -> None:
    code, _, err = _run(["--fake", "--names", "Fp1,Fp1,C3,C4,P7,P8,O1,O2",
                         "--save-dir", str(tmp_path)], capsys)
    assert code == 2
    assert "must not repeat a name, got ['Fp1']" in err and "Traceback" not in err
    assert not list(tmp_path.iterdir())


def test_score_command_uses_this_interpreter(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code, out, _ = _run(["--fake", "--trials", "2", "--save-dir", str(tmp_path)], capsys)
    assert code == 0
    path = list_recordings(directory=tmp_path)[0]
    assert f"score it with: {sys.executable} -m brainsync.score_cued {path}" in out
    assert record.score_command(Path("/a b/x.npz"), 1, python="/v/bin/python") == \
        "/v/bin/python -m brainsync.select_freqs '/a b/x.npz'"


@pytest.mark.skipif(sys.platform == "win32" or __import__("os").geteuid() == 0,
                    reason="needs POSIX permissions as a normal user")
def test_unwritable_save_dir_fails_before_recording(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)

    def no_session(*a: object, **k: object) -> None:
        raise AssertionError("must not record into an unwritable directory")

    monkeypatch.setattr(record, "fake_session", no_session)
    try:
        code, _, err = _run(["--fake", "--trials", "2", "--save-dir", str(ro)], capsys)
        code_nested, _, err_nested = _run(["--fake", "--save-dir", str(ro / "sub")], capsys)
    finally:
        ro.chmod(0o755)
    assert code == 2 and "cannot save into" in err and "--save-dir" in err
    assert code_nested == 2 and "cannot save into" in err_nested
    assert not list(ro.iterdir())


def test_default_save_dir_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("BRAINSYNC_DATA_DIR", str(blocker))
    code, _, err = _run(["--trials", "2"], capsys)    # fails before touching the board
    assert code == 2 and "spectrum_checks" in err


def test_fake_session_never_lands_in_the_real_recordings(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("BRAINSYNC_DATA_DIR", str(data))
    monkeypatch.setattr(record, "fake_dir", lambda: tmp_path / "fake")
    code, out, err = _run(["--fake", "--trials", "2", "--warmup", "0"], capsys)
    assert code == 0, err
    assert not (data / "spectrum_checks").exists() or not list(
        (data / "spectrum_checks").iterdir())
    saved = list_recordings(directory=tmp_path / "fake")
    assert len(saved) == 1 and str(saved[0]) in out


def test_fake_dir_is_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    from brainsync.paths import recordings_dir
    monkeypatch.setenv("BRAINSYNC_DATA_DIR", str(tmp_path))
    assert record.fake_dir().parent == Path(tempfile.gettempdir())
    assert recordings_dir() not in (record.fake_dir(), *record.fake_dir().parents)


class _CtrlCBoard:
    """Stands in for CerelogBoard: Ctrl-C arrives on read number ``at``."""

    fs = 250.0
    ch_names = ("Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2")
    at = 3

    def __init__(self, *a: object, **k: object) -> None:
        self.reads = 0

    def __enter__(self) -> "_CtrlCBoard":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        self.reads += 1
        if self.reads == self.at:
            raise KeyboardInterrupt
        return np.zeros((8, 10)), np.arange(10) / 250.0


def test_ctrl_c_in_warmup_never_starts_the_stimulus(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path,
                                                    capsys: pytest.CaptureFixture) -> None:
    from brainsync import board as bd

    def no_launch(args: list[str]) -> launch.Stimulus:
        raise AssertionError("the stimulus must not start after Ctrl-C")

    monkeypatch.setattr(bd, "CerelogBoard", _CtrlCBoard)
    monkeypatch.setattr(record, "launch_stimulus", no_launch)
    code, out, err = _run(["--trials", "2", "--warmup", "5", "--save-dir", str(tmp_path)], capsys)
    assert code == 130 and "interrupted before the session started" in err
    assert out == "" and not list_recordings(directory=tmp_path)


def test_save_failure_after_the_session_falls_back(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture) -> None:
    real_save = record.save_recording
    primary = tmp_path / "primary"

    def flaky(path: Path, *a: object, **k: object) -> Path:
        if Path(path).parent == primary:
            raise PermissionError(13, "Permission denied", str(path))
        return real_save(path, *a, **k)

    monkeypatch.setattr(record, "save_recording", flaky)
    monkeypatch.setattr(record, "fallback_dir", lambda: tmp_path / "fallback")
    code, out, err = _run(["--fake", "--trials", "2", "--save-dir", str(primary)], capsys)
    assert code == 0, err
    saved = list_recordings(directory=tmp_path / "fallback")
    assert len(saved) == 1 and str(saved[0]) in out
    assert "could not save" in err and "Permission denied" in err
    assert len(cut_windows(load_recording(saved[0]))) == 2


def test_save_failure_everywhere_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture) -> None:
    def broken(path: Path, *a: object, **k: object) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(record, "save_recording", broken)
    monkeypatch.setattr(record, "fallback_dir", lambda: tmp_path / "fallback")
    code, out, err = _run(["--fake", "--trials", "2", "--save-dir", str(tmp_path / "p")], capsys)
    assert code == 1 and "could not be saved anywhere" in err and out == ""


@pytest.mark.parametrize("trials, cue_s, rest_s", [(10, 2.0, 2.0), (2, 3.0, 1.0), (2, 3.86, 0.0)])
def test_fake_keeps_the_last_still_window_when_rest_is_short(
        trials: int, cue_s: float, rest_s: float, tmp_path: Path,
        capsys: pytest.CaptureFixture) -> None:
    # rest + 1 s lead < stim: the last still window runs past the last rest
    code, _, err = _run(["--fake", "--trials", str(trials), "--cue-s", f"{cue_s:g}",
                         "--rest-s", f"{rest_s:g}", "--seed", "1", "--warmup", "0",
                         "--save-dir", str(tmp_path)], capsys)
    assert code == 0, err
    win = cut_windows(load_recording(list_recordings(directory=tmp_path)[0]))
    assert len(win) == trials
    assert np.bincount(win.labels).tolist() == [trials // 2] * 2
    assert "incomplete" not in err


def test_trailing_time_covers_the_still_window() -> None:
    assert record.trailing_s(4.0, 3.0) == pytest.approx(1.5)
    assert record.trailing_s(4.0, 1.0) == pytest.approx(3.5)
    assert record.trailing_s(4.0, 6.0) == pytest.approx(0.5)
    from brainsync.board import FakeBoard
    board = FakeBoard([15.0, 17.0], t0=1000.0)
    X, t, text = record.fake_session(board, [0, 1], cue_s=3.0, stim_s=4.0, rest_s=0.5)
    last_off = max(float(line.split("]")[0].split()[1]) for line in text.splitlines()
                   if "stim_off:" in line)
    assert t[-1] >= last_off + 4.0


def test_two_recordings_in_one_second_get_two_files(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(record.time, "time", lambda: 1_792_000_000.25)
    paths = []
    for _ in range(2):
        code, out, err = _run(["--fake", "--trials", "2", "--warmup", "0",
                               "--save-dir", str(tmp_path)], capsys)
        assert code == 0, err
        paths.append(Path(out.splitlines()[0]))
    assert paths[0] != paths[1]
    assert list_recordings(directory=tmp_path) == paths  # second name sorts after the first
    stems = [p.stem for p in paths]
    assert all(len(s) == len("2026-10-23_120000_cued") for s in stems)
    for p in paths:
        assert len(cut_windows(load_recording(p))) == 2


def test_reserve_path_skips_taken_names(tmp_path: Path) -> None:
    start = 1_792_000_000.9
    first = record.reserve_path(tmp_path, start, "trials")
    second = record.reserve_path(tmp_path, start, "trials")
    assert first.exists() and second.exists() and first != second
    assert first.name.endswith("_trials.npz") and sorted([second, first]) == [first, second]
    with pytest.raises(FileExistsError, match="no free trials file name"):
        record.reserve_path(tmp_path, start, "trials", tries=2)


def test_failed_save_leaves_no_empty_reservation(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture) -> None:
    def broken(path: Path, *a: object, **k: object) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(record, "save_recording", broken)
    monkeypatch.setattr(record, "fallback_dir", lambda: tmp_path / "fallback")
    assert _run(["--fake", "--trials", "2", "--save-dir", str(tmp_path / "p")], capsys)[0] == 1
    assert not list((tmp_path / "p").iterdir()) and not list((tmp_path / "fallback").iterdir())


def test_ctrl_c_during_the_session_keeps_the_data(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture) -> None:
    import time

    from brainsync import board as bd

    class Interrupting(bd.FakeBoard):
        reads = 0

        def read(self):
            type(self).reads += 1
            if type(self).reads == 25:
                raise KeyboardInterrupt
            return super().read()

    def fake_cerelog(serial_port: str | None = None, ch_names: list[str] | None = None,
                     verbose: bool = False) -> bd.FakeBoard:
        return Interrupting([15.0, 17.0], ch_names=ch_names, clock=time.time)

    def fake_launch(args: list[str]) -> launch.Stimulus:
        return launch.launch_stimulus(args, command=_script(FAKE_STIMULUS), timeout=20,
                                      log_dir=tmp_path)

    monkeypatch.setattr(record, "launch_stimulus", fake_launch)
    monkeypatch.setattr(record, "speak", lambda text, voice=True: None)
    monkeypatch.setattr(bd, "CerelogBoard", fake_cerelog)
    code, out, err = _run(["--trials", "2", "--cue-s", "0.3", "--stim-s", "0.5", "--rest-s",
                           "0.3", "--latency", "0.1", "--warmup", "0",
                           "--save-dir", str(tmp_path / "rec")], capsys)
    assert code == 0, err
    assert "interrupted; keeping" in err
    assert len(list_recordings(directory=tmp_path / "rec")) == 1
