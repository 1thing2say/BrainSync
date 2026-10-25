"""python -m brainsync.vep: the fake live runs of each paradigm, the comparison table, and
the argument checks. No display, no headset (--fake --fast --no-stimulus)."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from brainsync.stimulus import load_config
from brainsync.vep import __main__ as cli
from brainsync.vep.cvep import Template

FAST = ["--fake", "--fast", "--no-stimulus", "--no-voice"]


def test_fake_cvep_run_trains_saves_and_decodes(tmp_path: Path, capsys):
    tpl = tmp_path / "tpl.npz"
    rc = cli.main(["--paradigm", "code", *FAST, "--train-cycles", "12", "--seconds", "16",
                   "--switch", "8", "--save-template", str(tpl), "--seed", "3"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert tpl.is_file() and Template.load(tpl).n_cycles == 12
    assert "training: 12 cycles" in err and "leave-one-out accuracy" in err
    assert "committed" in err and "code: " in out and "commits:" in out
    hits, n = map(int, re.search(r"gaze: (\d+)/(\d+)", out).groups())
    assert n >= 4 and hits / n >= 0.6, out                   # the decoder followed the fake user
    labels = load_config(cli.DEFAULT_CONFIGS["code"]).labels
    commits = [c.split("->")[1] for c in re.search(r"commits: (.*)", out).group(1).split()]
    assert commits[-1] == labels[1]                          # the last square looked at (16 s, switch 8)
    assert set(commits) <= {labels[0], labels[1]}, commits   # never a square nobody looked at
    # reuse the template: no training stage
    rc = cli.main(["--paradigm", "code", *FAST, "--template", str(tpl), "--seconds", "8"])
    out, err = capsys.readouterr()
    assert rc == 0 and "training:" not in err and "code: " in out


def test_fake_tvep_run(capsys):
    rc = cli.main(["--paradigm", "time", *FAST, "--seconds", "14", "--switch", "7",
                   "--min-flashes", "3"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "time-VEP tvep_4" in err and "time: " in out
    assert "decisions in" in out
    # the 6 s flash window keeps the old square's flashes for seconds after each gaze
    # switch, so decisions lag the fake user: ask that both squares looked at were
    # committed, the last one last, and that agreement beats what a wrong decoder gets
    hits, n = map(int, re.search(r"gaze: (\d+)/(\d+)", out).groups())
    assert n >= 4 and hits / n >= 0.4, out
    commits = [c.split("->")[1] for c in re.search(r"commits: (.*)", out).group(1).split()]
    assert commits[0] == "0" and commits[-1] == "1", commits


def test_fake_fvep_run_uses_the_demo_session(capsys):
    rc = cli.main(["--paradigm", "freq", *FAST, "--seconds", "8", "--calibrate", "6",
                   "--window", "2", "--switch", "4"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "SNR(" in err and "baseline from" in err
    assert "windows" in out


def test_compare_prints_the_paper_style_table(capsys):
    rc = cli.main(["--compare", "--fake", "--trials", "2", "--train-trials", "2",
                   "--max-trial", "8", "--seed", "5"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("paradigm") and "bits/min" in lines[0] and "paper" in lines[0]
    rows = {ln.split()[0]: ln for ln in lines[2:5]}
    assert set(rows) == {"time-VEP", "freq-VEP", "code-VEP"}
    assert "92.8 bits/min" in rows["code-VEP"] and "39.7 bits/min" in rows["freq-VEP"]
    assert "under 30 bits/min" in rows["time-VEP"]
    for name, ln in rows.items():
        cells = ln.split()
        targets, trials, acc = int(cells[1]), int(cells[2]), cells[3]
        assert trials == 2 * targets and acc.endswith("%")
        assert targets == (6 if name == "freq-VEP" else 4)
    assert "leave-one-out" in out and "Synthetic board" in out and "Bin et al. 2009" in out


def test_compare_rows_decode_the_fake_user(monkeypatch):
    args = cli.build_parser().parse_args(["--compare", "--fake", "--trials", "3",
                                          "--train-trials", "3", "--seed", "1"])
    code = cli.compare_code(args)
    time_ = cli.compare_time(args)
    freq = cli.compare_freq(args)
    for row in (code, time_, freq):
        assert row.n == 3 * row.n_targets
        assert row.acc >= 0.75, (row.paradigm, row.y, row.pred)
        assert np.isfinite(row.mean_delay())
    assert code.mean_delay() < freq.mean_delay() + 3.0
    assert time_.mean_delay() > code.mean_delay()          # the slow paradigm
    table = cli.compare_table([time_, freq, code], gaze_s=1.0, max_trial=args.max_trial)
    assert len(table) > 6


def test_argument_checks(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--compare"])
    assert e.value.code == 2 and "--fake" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--paradigm", "code", "--no-stimulus"])
    with pytest.raises(SystemExit):
        cli.main(["--fast"])
    with pytest.raises(SystemExit):
        cli.main(["--fake", "--votes", "0"])
    with pytest.raises(SystemExit):
        cli.main(["--fake", "--names", "a,b"])
    # a config of the wrong paradigm is a usage error (exit 2), not a crash
    assert cli.main(["--paradigm", "code", "-c", "fvep_6", *FAST]) == 2
    assert "[freq] config" in capsys.readouterr().err
    assert cli.main(["--paradigm", "freq", "-c", "cvep_4", *FAST]) == 2
    assert cli.main(["--paradigm", "time", "-c", "missing", *FAST]) == 2


def test_log_tail_reads_new_marker_lines_only(tmp_path: Path):
    log = tmp_path / "stim.log"
    log.write_text("measured refresh: 60.00 Hz\n[marker 10.5] stim_on:0\n[marker 10.5] cyc")
    tail = cli.LogTail(log)
    assert tail.events() == [(10.5, "stim_on", 0)]
    with open(log, "a") as f:
        f.write("le:0\n[marker 11.55] cycle:1\nnoise\n")
    assert tail.events() == [(10.5, "cycle", 0), (11.55, "cycle", 1)]
    assert tail.events() == []


def test_coded_stimulus_args():
    args = cli.build_parser().parse_args(["--paradigm", "code", "--ignore-refresh"])
    cfg = cli.config_for(args)
    a = cli.coded_stimulus_args(cfg, args)
    assert a[:2] == ["-c", str(cfg.path)] and "--free" in a and "--ignore-refresh" in a
    assert a[a.index("--markers") + 1] == "unix" and "--seconds" not in a


def test_coded_stimulus_args_carry_labels_and_status_unless_no_scores():
    args = cli.build_parser().parse_args(["--paradigm", "code"])
    a = cli.coded_stimulus_args(cli.config_for(args), args)
    assert "--labels" in a and a[a.index("--status") + 1] == "stdin"
    bare = cli.build_parser().parse_args(["--paradigm", "code", "--no-scores"])
    b = cli.coded_stimulus_args(cli.config_for(bare), bare)
    assert "--labels" not in b and "--status" not in b


def test_confidence_labels_and_line():
    import numpy as np
    from brainsync.online import Decision
    d = Decision(time=0.0, scores=np.array([0.42, 0.11]), raw=0, committed=0, changed=True,
                 gap=0.31, level=float("nan"))
    lines = cli.confidence_labels(d, ["YES", "NO"])
    assert len(lines) == 2
    assert lines[0].startswith("YES ") and "+0.42" in lines[0] and lines[0].endswith("<")
    assert lines[1].startswith("NO  ") and "+0.11" in lines[1] and not lines[1].endswith("<")
    assert lines[0].count("\u2588") == 3 and lines[1].count("\u2588") == 1   # of 8 cells
    assert cli.confidence_line(d, ["YES", "NO"]) == "lead 0.31   choice: YES *"
    abstained = Decision(time=0.0, scores=np.array([0.1, 0.1]), raw=-1, committed=-1,
                         changed=False, gap=float("nan"), level=float("nan"))
    lines = cli.confidence_labels(abstained, ["YES", "NO"], scale=0.2)
    assert lines[0].count("\u2588") == 4 and not lines[0].endswith("<")   # 0.1 / 0.2 of 8
    assert cli.confidence_line(abstained, ["YES", "NO"]) == "lead  -     no choice yet"
    assert cli.send_text(None, "x") is True and cli.send_labels(None, ["a", "b"]) is True


def test_save_keeps_every_sample_and_the_markers(tmp_path: Path, capsys):
    from brainsync.recordings import load_recording, parse_events
    from brainsync.vep import timing
    tpl = tmp_path / "tpl.npz"
    rc = cli.main(["--paradigm", "code", *FAST, "--train-cycles", "6", "--seconds", "6",
                   "--switch", "3", "--save-template", str(tpl), "--save", str(tmp_path),
                   "--seed", "2"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    saved = [ln for ln in err.splitlines() if ln.startswith("saved ")]
    assert len(saved) == 1 and "_vep.npz" in saved[0] and "markers" in saved[0]
    assert "timing check: python -m brainsync.vep.timing" in err
    files = list(tmp_path.glob("*_vep.npz"))
    assert len(files) == 1
    rec = load_recording(files[0])
    assert rec.X.shape[0] == 8 and rec.X.shape[1] == rec.t.size and rec.fs == 250.0
    # training (6 cycles) plus the 6 s run, every sample in order
    assert rec.t[-1] - rec.t[0] > 12.0 and np.all(np.diff(rec.t) > 0)
    assert np.allclose(np.diff(rec.t), 1 / rec.fs, atol=1e-6)   # Unix times: ~2e-7 s rounding
    cycles = parse_events(rec.marker_text, ["cycle"])
    assert len(cycles) >= 11 and cycles[0][2] == 0
    assert parse_events(rec.marker_text, ["stim_on"])
    assert np.all(np.isnan(rec.freqs)) and rec.n_targets == 4
    # the saved run passes the timing check, and the fake user's data are in it
    assert timing.main([str(files[0])]) == 0
    assert "PASS cycle spacing" in capsys.readouterr().out
    assert np.std(rec.X[6]) > 5.0            # O1 carries noise plus the response


def test_paradigm_specific_argument_checks(tmp_path: Path, capsys):
    for argv in (["--compare", "--fake", "-c", "cvep_2"],
                 ["--paradigm", "time", "--fake", "--template", str(tmp_path / "t.npz")],
                 ["--paradigm", "code", "--fake", "--template", str(tmp_path / "t.npz"),
                  "--save-template", str(tmp_path / "s.npz")],
                 ["--paradigm", "code", "--fake", "--train-cycles", "1"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2, argv
    err = capsys.readouterr().err
    assert "drop -c" in err and "--template is for" in err and "at least" in err or ">= 2" in err
