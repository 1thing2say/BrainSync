"""brainsync.vep.timing: the marker-timing check on synthetic marker lists (no files), on a
SequenceFakeBoard run (markers against samples), and its CLI on a saved fake recording and
on a log with a two-frame slip.

Levels under test (the module docstring's rules): a perfect grid PASSes; one dropped frame
(one spacing off by one frame) is a WARN; a slip of two frames is a FAIL, so the CLI exits
1 on that log; drift of 100 ppm is reported and PASSes (under 2000 ppm).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from brainsync.recordings import MONTAGE, parse_events, save_recording
from brainsync.stimulus import load_config
from brainsync.stimulus.coded import sequence_for
from brainsync.vep import timing
from brainsync.vep.fake import SequenceFakeBoard, markers_text

REFRESH = 60.0
FRAME = 1.0 / REFRESH
CYCLE = 63 * FRAME       # cvep_4: 63 frames = 1.0500 s
SLOT = 15 * FRAME        # tvep_4: 2 + 13 frames
T0 = 1_792_809_200.0


def cycle_events(n: int, period: float = CYCLE, t0: float = T0) -> list[tuple[float, str, int]]:
    return [(t0, "stim_on", 0)] + [(t0 + i * period, "cycle", i) for i in range(n)]


def log_text(events) -> str:
    return markers_text(events, REFRESH)


# -- pure functions -----------------------------------------------------------------------------

def test_perfect_grid_passes_with_no_slip_and_no_drift():
    events = cycle_events(60)
    runs = timing.split_runs(events, "cycle")
    assert len(runs) == 1 and len(runs[0]) == 60
    sp = timing.grid_spacings(runs, "cycle", CYCLE, FRAME)
    assert sp.count == 60 and sp.spacings_s.size == 59
    # Unix-scale float64 differences carry about 2e-7 s (1e-5 frames) of rounding
    assert sp.median_s == pytest.approx(CYCLE, abs=1e-6)
    assert sp.max_abs_frames < 1e-3 and sp.n_off == 0
    check = timing.judge_spacings(sp)
    assert check.level == timing.PASS and "0/59 off" in check.reason
    ppm, n = timing.drift_ppm(runs[0], CYCLE, FRAME)
    assert n == 60 and abs(ppm) < 1.0
    assert timing.judge_rate("cycle drift", ppm, "x").level == timing.PASS


def test_one_dropped_frame_is_one_spacing_off_by_one_frame_and_a_warn():
    events = cycle_events(60)
    # the display dropped a frame in cycle 30: every later marker is one frame late
    events = [(t + (FRAME if k == "cycle" and i > 30 else 0.0), k, i) for t, k, i in events]
    sp = timing.grid_spacings(timing.split_runs(events, "cycle"), "cycle", CYCLE, FRAME)
    assert sp.n_off == 1
    assert sp.max_abs_frames == pytest.approx(1.0, abs=1e-3)
    dev = sp.deviation_frames
    assert dev[30] == pytest.approx(1.0, abs=1e-3) and np.all(np.abs(np.delete(dev, 30)) < 1e-3)
    check = timing.judge_spacings(sp)
    assert check.level == timing.WARN and "1/59 off" in check.reason
    # one dropped frame shifts the rest of the run by a frame: 1 / (59 * 63) = 270 ppm of
    # apparent drift, well inside the PASS band; the spacing check names the slip
    ppm, _ = timing.drift_ppm(timing.split_runs(events, "cycle")[0], CYCLE, FRAME)
    assert 100 < ppm < 400


def test_two_frame_slip_fails_and_a_doubled_frame_is_negative():
    events = cycle_events(40)
    slipped = [(t + (2 * FRAME if k == "cycle" and i > 10 else 0.0), k, i) for t, k, i in events]
    sp = timing.grid_spacings(timing.split_runs(slipped, "cycle"), "cycle", CYCLE, FRAME)
    assert sp.n_off == 1 and sp.max_abs_frames == pytest.approx(2.0, abs=1e-3)
    assert timing.judge_spacings(sp).level == timing.FAIL
    doubled = [(t - (FRAME if k == "cycle" and i > 10 else 0.0), k, i) for t, k, i in events]
    sp = timing.grid_spacings(timing.split_runs(doubled, "cycle"), "cycle", CYCLE, FRAME)
    assert sp.deviation_frames[10] == pytest.approx(-1.0, abs=1e-3)
    assert timing.judge_spacings(sp).level == timing.WARN


def test_many_single_slips_fail():
    events = cycle_events(100)
    # a frame dropped every 10 cycles: 9 of 99 spacings off, more than 1 %
    events = [(t + (FRAME * (i // 10) if k == "cycle" else 0.0), k, i) for t, k, i in events]
    sp = timing.grid_spacings(timing.split_runs(events, "cycle"), "cycle", CYCLE, FRAME)
    assert sp.n_off == 9 and sp.max_abs_frames == pytest.approx(1.0, abs=1e-3)
    assert timing.judge_spacings(sp).level == timing.FAIL


def test_drift_of_100_ppm_is_reported():
    events = cycle_events(60, period=CYCLE * (1 + 100e-6))
    runs = timing.split_runs(events, "cycle")
    ppm, n = timing.drift_ppm(runs[0], CYCLE, FRAME)
    assert n == 60 and ppm == pytest.approx(100.0, abs=1.0)
    assert timing.judge_rate("cycle drift", ppm, "x").level == timing.PASS
    # the spacings are each 105 us long: well under half a frame
    sp = timing.grid_spacings(runs, "cycle", CYCLE, FRAME)
    assert sp.n_off == 0 and sp.max_abs_frames < 0.01
    big, _ = timing.drift_ppm([T0 + i * CYCLE * 1.02 for i in range(20)], CYCLE, FRAME)
    assert big == pytest.approx(20000.0, rel=1e-3)   # whole cycles, never frames, are rounded
    assert timing.judge_rate("cycle drift", big, "x").level == timing.FAIL
    warn, _ = timing.drift_ppm([T0 + i * CYCLE * 1.005 for i in range(20)], CYCLE, FRAME)
    assert timing.judge_rate("cycle drift", warn, "x").level == timing.WARN
    assert np.isnan(timing.drift_ppm([T0, T0 + CYCLE], CYCLE, FRAME)[0])
    with pytest.raises(ValueError, match="positive"):
        timing.drift_ppm([T0, T0 + CYCLE, T0 + 2 * CYCLE], 0.0, FRAME)


def test_split_runs_breaks_at_stim_on_and_at_a_cycle_index_reset():
    first = cycle_events(5)
    second = cycle_events(4, t0=T0 + 20.0)
    runs = timing.split_runs(first + second, "cycle")
    assert [len(r) for r in runs] == [5, 4]
    # a restart without a stim_on marker still shows as a reset of the cycle count
    no_stim_on = [e for e in first + second if e[1] != "stim_on"]
    assert [len(r) for r in timing.split_runs(no_stim_on, "cycle")] == [5, 4]
    sp = timing.grid_spacings(runs, "cycle", CYCLE, FRAME)
    assert sp.runs == 2 and sp.count == 9 and sp.spacings_s.size == 7 and sp.n_off == 0


def test_flash_spacings_are_whole_slots():
    rng = np.random.default_rng(0)
    slots = rng.integers(1, 4, size=50)             # flashes 1 to 3 slots apart
    t = T0 + np.cumsum(slots) * SLOT
    events = [(T0, "stim_on", 0)] + [(float(x), "flash", int(i % 4)) for i, x in enumerate(t)]
    runs = timing.split_runs(events, "flash")
    sp = timing.grid_spacings(runs, "flash", SLOT, FRAME, multiples=True)
    assert sp.count == 50 and sp.n_off == 0 and sp.max_abs_frames < 1e-3
    assert timing.judge_spacings(sp).level == timing.PASS
    late = list(events)
    late[20] = (late[20][0] + FRAME, "flash", late[20][2])      # one flash a frame late
    sp = timing.grid_spacings(timing.split_runs(late, "flash"), "flash", SLOT, FRAME,
                              multiples=True)
    assert sp.n_off == 2        # the spacing before it is long, the one after it short
    assert timing.judge_spacings(sp).level == timing.WARN   # SLIP_ALLOWED covers both
    ppm, _ = timing.drift_ppm(timing.split_runs(late, "flash")[0], SLOT, FRAME)
    assert abs(ppm) < 2000
    s, d = timing.spacing_deviations([T0, T0 + 3 * SLOT + 0.25 * FRAME], SLOT, FRAME,
                                     multiples=True)
    assert s[0] == pytest.approx(3 * SLOT + 0.25 * FRAME, abs=1e-6)
    assert d[0] == pytest.approx(0.25, abs=1e-3)
    with pytest.raises(ValueError, match="positive"):
        timing.spacing_deviations([T0, T0 + 1], 0.0, FRAME)


def test_check_markers_matches_the_config_paradigm():
    cvep = load_config("cvep_4")
    tvep = load_config("tvep_4")
    events = cycle_events(30)
    checks = timing.check_markers(events, cvep)
    assert [c.name for c in checks] == ["cycle spacing", "cycle drift"]
    assert all(c.level == timing.PASS for c in checks)
    wrong = timing.check_markers(events, tvep)
    assert any(c.level == timing.FAIL and "no flash markers" in c.reason for c in wrong)
    assert any(c.level == timing.FAIL and "[time] config" in c.reason for c in wrong)
    assert timing.check_markers([], cvep)[0].level == timing.FAIL
    flick = load_config("two_targets")
    assert timing.check_markers(events, flick)[0].level == timing.FAIL
    assert timing.check_markers([(T0, "stim_on", 0)], flick)[0].level == timing.SKIP


def test_log_lines_refresh_and_dropped_frames():
    cfg = load_config("cvep_4")
    text = ("measured refresh: 60.02 Hz\n[marker 1.0] cycle:0\n"
            "timing: 3780 frames, 0 dropped overall, refresh 60 Hz (measured 60.02)\n")
    assert timing.parse_refresh(text) == 60.02 and timing.parse_dropped(text) == (3780, 0)
    checks = {c.name: c for c in timing.check_log_lines(text, cfg)}
    assert checks["measured refresh"].level == timing.PASS
    assert checks["dropped frames"].level == timing.PASS
    worse = text.replace("0 dropped", "3 dropped")
    assert {c.name: c.level for c in timing.check_log_lines(worse, cfg)}["dropped frames"] == timing.WARN
    bad = text.replace("0 dropped", "40 dropped").replace("60.02 Hz", "120.00 Hz")
    levels = {c.name: c.level for c in timing.check_log_lines(bad, cfg)}
    assert levels == {"measured refresh": timing.FAIL, "dropped frames": timing.FAIL}
    none = {c.name: c.level for c in timing.check_log_lines("[marker 1.0] cycle:0\n", cfg)}
    assert none == {"measured refresh": timing.SKIP, "dropped frames": timing.SKIP}
    assert timing.parse_refresh("") is None and timing.parse_dropped("") is None


# -- markers against samples --------------------------------------------------------------------

def fake_run(seconds: float = 6.0, seed: int = 0):
    cfg = load_config("cvep_4")
    board = SequenceFakeBoard(sequence_for(cfg, seed), refresh=cfg.refresh, seed=seed, t0=T0)
    board.advance(0.5)                      # still screen first, as in the demo
    board.start_flicker()
    board.advance(seconds)
    X, t = board.read()
    return cfg, board, X, t, board.read_events()


def test_fake_board_markers_sit_on_the_frame_grid_and_inside_the_recording():
    cfg, board, X, t, events = fake_run()
    checks = timing.run_checks(cfg, recording=(t, board.fs, markers_text(events, cfg.refresh)))
    by = {c.name: c for c in checks}
    assert by["cycle spacing"].level == timing.PASS and by["cycle spacing"].data["n_off"] == 0
    assert by["cycle spacing"].data["markers"] == 6          # cycles start at 0, 1.05 ... 5.25 s
    assert by["cycle drift"].level == timing.PASS and abs(by["cycle drift"].data["ppm"]) < 1
    assert by["marker coverage"].level == timing.PASS
    assert by["marker coverage"].data["inside"] == by["marker coverage"].data["markers"] == 7
    assert by["marker to sample"].level == timing.PASS
    assert by["marker to sample"].data["max_gap_s"] < 1.0 / board.fs      # under 4 ms
    assert by["marker to sample"].data["max_gap_s"] <= 0.5 / board.fs + 1e-6   # exactly half, once
    assert by["sample rate"].level == timing.PASS and abs(by["sample rate"].data["ppm"]) < 1
    assert by["measured refresh"].level == timing.PASS
    assert by["dropped frames"].level == timing.SKIP           # the fake board never drops
    assert timing.exit_code(checks) == 0
    # the markers' frame times are exactly where the board put them
    marks = [e[0] for e in events if e[1] == "cycle"]
    assert marks == pytest.approx([board.frame_time(63 * i) for i in range(6)], abs=1e-9)


def test_recording_checks_flag_markers_outside_and_a_slow_sample_clock():
    cfg, board, X, t, events = fake_run()
    marks = [e[0] for e in events]
    outside = marks + [t[-1] + 2.0, t[0] - 1.0]
    by = {c.name: c for c in timing.check_recording(outside, t, board.fs, cfg.refresh)}
    assert by["marker coverage"].level == timing.WARN
    assert "1 before" in by["marker coverage"].reason and "1 after" in by["marker coverage"].reason
    by = {c.name: c for c in timing.check_recording([t[-1] + 2.0], t, board.fs, cfg.refresh)}
    assert by["marker coverage"].level == timing.FAIL and "marker to sample" not in by
    # samples stamped 2 % slow: the rate check fails (1 % would be a WARN), the markers
    # still sit on samples
    slow = t[0] + (t - t[0]) * 1.02
    by = {c.name: c for c in timing.check_recording(marks, slow, board.fs, cfg.refresh)}
    assert by["sample rate"].level == timing.FAIL
    assert by["sample rate"].data["fitted_hz"] == pytest.approx(board.fs / 1.02, rel=1e-6)
    by = {c.name: c for c in timing.check_recording(marks, t[0] + (t - t[0]) * 1.005, board.fs, cfg.refresh)}
    assert by["sample rate"].level == timing.WARN
    # a hole in the data: the nearest sample to a marker is a frame or more away
    hole = t[np.abs(t - marks[2]) > 0.02]
    by = {c.name: c for c in timing.check_recording(marks, hole, board.fs, cfg.refresh)}
    assert by["marker to sample"].level == timing.FAIL
    gaps, inside = timing.nearest_sample_gaps(marks, t)
    assert inside.all() and gaps.max() < 1.0 / board.fs
    ppm, rate = timing.sample_rate_ppm(t, board.fs)
    assert abs(ppm) < 1 and rate == pytest.approx(board.fs, abs=1e-3)
    with pytest.raises(ValueError, match="non-empty"):
        timing.nearest_sample_gaps(marks, np.empty(0))
    assert timing.check_recording(marks, np.empty(0), board.fs, cfg.refresh)[0].level == timing.FAIL


# -- the CLI ------------------------------------------------------------------------------------

def test_cli_passes_a_saved_fake_recording(tmp_path: Path, capsys):
    cfg, board, X, t, events = fake_run(seconds=8.0)
    path = save_recording(tmp_path / "2026-10-25_120000_vep.npz", X, t, board.fs, MONTAGE,
                          cfg.freqs, markers_text(events, cfg.refresh), kind="cued")
    rc = timing.main([str(path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    lines = out.splitlines()
    assert any(ln.startswith("PASS cycle spacing") for ln in lines)
    assert any(ln.startswith("PASS marker to sample") for ln in lines)
    assert "1.0500 s" in out and lines[-1].startswith("OK: 0 failed")
    rc = timing.main([str(path), "--json"])
    import json
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0 and {r["name"] for r in rows} >= {"cycle spacing", "sample rate"}
    # the recording plus the same markers as a log file
    log = tmp_path / "stim.log"
    log.write_text(markers_text(events, cfg.refresh))
    assert timing.main([str(log), str(path)]) == 0


def test_cli_fails_a_log_with_a_two_frame_slip(tmp_path: Path, capsys):
    events = cycle_events(50)
    events = [(t + (2 * FRAME if k == "cycle" and i >= 25 else 0.0), k, i) for t, k, i in events]
    log = tmp_path / "stim.log"
    log.write_text(log_text(events)
                   + "timing: 3150 frames, 2 dropped overall, refresh 60 Hz (measured 60.00)\n")
    rc = timing.main([str(log)])
    out = capsys.readouterr().out
    assert rc == 1
    assert any(ln.startswith("FAIL cycle spacing") and "2.00 frames" in ln
               for ln in out.splitlines())
    assert "WARN dropped frames" in out and out.splitlines()[-1].startswith("FAIL: 1 failed")
    # a single dropped frame is only a warning: exit 0
    events = cycle_events(50)
    events = [(t + (FRAME if k == "cycle" and i >= 25 else 0.0), k, i) for t, k, i in events]
    log.write_text(log_text(events))
    assert timing.main([str(log)]) == 0
    assert "WARN cycle spacing" in capsys.readouterr().out


def test_cli_usage_errors(tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as e:
        timing.main([])
    assert e.value.code == 2
    assert timing.main([str(tmp_path / "missing.log")]) == 2
    assert "no such file" in capsys.readouterr().err
    bad = tmp_path / "bad.npz"
    bad.write_bytes(b"not an npz")
    assert timing.main([str(bad)]) == 2
    log = tmp_path / "stim.log"
    log.write_text(log_text(cycle_events(3)))
    assert timing.main([str(log), "-c", "no_such_config"]) == 2
    assert "config" in capsys.readouterr().err
