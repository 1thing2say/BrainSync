from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from brainsync import check
from brainsync.board import ADC_FULL_SCALE_UV, FakeBoard
from brainsync.recordings import MONTAGE

FS = 250.0


@pytest.fixture(autouse=True)
def _agg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MPLBACKEND", "Agg")


def _rows(out: str) -> dict[str, list[str]]:
    """Table rows by channel: name, DC, RMS, SNR f, SNR 2f, peak, note words..."""
    return {line.split()[0]: line.split() for line in out.splitlines()
            if line.split() and line.split()[0] in MONTAGE}


def test_fake_check_finds_10hz_peak(capsys: pytest.CaptureFixture) -> None:
    code = check.main(["--fake", "--target", "10", "--seconds", "20", "--warmup", "1",
                       "--no-show"])
    cap = capsys.readouterr()
    assert code == 0, cap.err
    rows = _rows(cap.out)
    assert set(rows) == set(MONTAGE)
    for name in ("P7", "P8", "O1", "O2"):
        assert float(rows[name][5]) == pytest.approx(10.0)  # peak 5-30 Hz
        assert float(rows[name][3]) > 6.0                   # SNR at 10 Hz
    for name in ("Fp1", "Fp2"):
        assert float(rows[name][3]) < 6.0
    assert "strongest 10 Hz response:" in cap.out
    assert "nearly the same signal" not in cap.out
    assert "settling for 1 s" in cap.err and "recorded 20/20 s" in cap.err


def test_run_check_synthetic_ssvep() -> None:
    rng = np.random.default_rng(0)
    n = int(20 * FS)
    tt = np.arange(n) / FS
    X = rng.normal(0, 5, (8, n)) + 30_000.0
    X[6:] += 3.0 * np.sin(2 * np.pi * 10.0 * tt) + 1.5 * np.sin(2 * np.pi * 20.0 * tt)
    res = check.run_check(X, FS, 10.0)
    for name in ("O1", "O2"):
        ch = res[name]
        assert ch.peak_hz == pytest.approx(10.0)
        assert ch.snr_f > 10 and ch.snr_2f > 6
    assert all(abs(res[nm].snr_f) < 6 for nm in MONTAGE[:6])
    np.testing.assert_allclose([c.dc_uv for c in res.channels], 30_000.0, atol=1.0)
    assert [c.note for c in res.channels] == [""] * 8
    assert not res.reference_suspect
    assert res.best().name in ("O1", "O2")
    with pytest.raises(KeyError, match="Cz"):
        res["Cz"]


def test_rail_flat_noisy_and_dc_notes() -> None:
    rng = np.random.default_rng(1)
    n = int(10 * FS)
    X = rng.normal(0, 10, (8, n))
    X[0] = ADC_FULL_SCALE_UV                        # railed positive
    X[1] = -ADC_FULL_SCALE_UV                       # railed negative
    X[2] = 12_345.0                                 # flat
    X[3] += 0.9 * ADC_FULL_SCALE_UV                 # offset close to the rail
    X[4] = rng.normal(0, 400, n)                    # noisy
    X[5] = rng.normal(0, 400, n) - 120_000.0        # noisy and a large DC
    res = check.run_check(X, FS, 15.0)
    notes = [c.note for c in res.channels]
    assert notes[0].startswith("railed") and "+-187.5 mV" in notes[0] and "ADS1299" in notes[0]
    assert notes[1].startswith("railed")
    assert notes[2].startswith("flat")
    assert notes[3].startswith("large DC +169 mV") and "90% of the range" in notes[3]
    assert notes[4].startswith("noisy")
    assert res.channels[5].flags[0].startswith("noisy")
    assert res.channels[5].flags[1].startswith("large DC -120 mV")
    assert notes[6] == notes[7] == ""
    assert [c.usable for c in res.channels] == [False, False, False, True, True, True, True, True]
    text = check.render(res)
    rows = _rows(text)
    assert "railed" in " ".join(rows["Fp1"]) and "flat" in " ".join(rows["C3"])
    assert "nearly the same signal" not in text  # railed/flat channels are left out of r


def test_identical_channels_warn_about_reference() -> None:
    rng = np.random.default_rng(2)
    n = int(10 * FS)
    X = rng.normal(0, 20, n) + rng.normal(0, 0.5, (8, n))
    res = check.run_check(X, FS, 15.0)
    assert res.median_r > 0.98 and res.reference_suspect
    assert "reference or bias electrode" in check.render(res)


def test_similarity_of_rows() -> None:
    rng = np.random.default_rng(5)
    base = rng.normal(size=2000)
    same = np.vstack([base, 2 * base + 1, -base])
    assert check.similarity(same, np.ones(3, bool)) == pytest.approx(1.0)
    indep = rng.normal(size=(4, 20_000))
    assert check.similarity(indep, np.ones(4, bool)) < 0.05
    assert np.isnan(check.similarity(indep, np.array([True, False, False, False])))


def test_snr_of_a_pure_tone_and_bounds() -> None:
    rng = np.random.default_rng(3)
    n = int(20 * FS)
    x = np.sin(2 * np.pi * 12.0 * np.arange(n) / FS)[None] + rng.normal(0, 0.5, (1, n))
    f, p = check.welch_psd(x, FS, 4.0)
    assert f[1] - f[0] == pytest.approx(0.25)
    assert check.local_snr_db(f, p, 12.0)[0] > 15
    assert abs(check.local_snr_db(f, p, 20.0)[0]) < 6
    assert np.isnan(check.local_snr_db(f, p, 200.0)[0])
    assert check.strongest_hz(f, p)[0] == pytest.approx(12.0)
    with pytest.raises(ValueError, match="8 samples"):
        check.welch_psd(np.zeros((1, 4)), FS)


def test_second_harmonic_past_nyquist_shows_na() -> None:
    rng = np.random.default_rng(4)
    res = check.run_check(rng.normal(0, 5, (8, int(10 * FS))), FS, 100.0)
    assert np.isnan(res.channels[0].snr_2f)
    assert _rows(check.render(res))["O1"][4] == "n/a"


def test_save_dir_writes_figure_and_data(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = check.main(["--fake", "--target", "12.5", "--seconds", "10", "--warmup", "0",
                       "--no-show", "--save-dir", str(tmp_path / "out")])
    assert code == 0, capsys.readouterr().err
    pngs = list((tmp_path / "out").glob("check_12.5Hz_*.png"))
    npzs = list((tmp_path / "out").glob("check_12.5Hz_*.npz"))
    assert len(pngs) == 1 and len(npzs) == 1
    assert pngs[0].stem == npzs[0].stem
    with np.load(npzs[0]) as d:
        assert d["X"].shape == (8, 2500)
        assert float(d["target"]) == 12.5
        assert d["ch_names"].tolist() == list(MONTAGE)
    assert not list((tmp_path / "out").glob(".probe_*"))


@pytest.mark.parametrize("argv, match", [
    (["--seconds", "4"], "twice --segment"),
    (["--names", "a,b"], "8 channel names"),
    (["--names", "a,a,b,c,d,e,f,g"], "must not repeat a name, got ['a']"),
    (["--target", "0"], "--target"),
    (["--target", "130"], "(0, 125) Hz at 250 Hz"),
    (["--low", "50", "--high", "40"], "--low < --high"),
])
def test_bad_arguments(argv: list[str], match: str, capsys: pytest.CaptureFixture) -> None:
    assert check.main(["--fake", "--no-show", "--warmup", "0", *argv]) == 2
    err = capsys.readouterr().err
    assert match in err and "Traceback" not in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_unwritable_save_dir_fails_before_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)

    def no_recording(*a: object, **k: object) -> None:
        raise AssertionError("must not record")

    monkeypatch.setattr(check, "record", no_recording)
    try:
        assert check.main(["--fake", "--no-show", "--seconds", "8", "--save-dir", str(ro)]) == 2
    finally:
        ro.chmod(0o755)
    assert "cannot write to" in capsys.readouterr().err


def test_target_bound_follows_the_board_rate(monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture) -> None:
    from brainsync import board as bd

    class Fast(FakeBoard):
        serial_port = "/dev/cu.fast"

        def __init__(self, serial_port: str | None = None, ch_names: object = None,
                     verbose: bool = False) -> None:
            super().__init__([150.0], fs=500.0, switch_s=float("inf"), amp=5.0,
                             ch_names=ch_names)

    monkeypatch.setattr(bd, "CerelogBoard", Fast)
    common = ["--no-show", "--warmup", "0", "--seconds", "8", "--high", "240"]
    assert check.main([*common, "--target", "150"]) == 0
    assert float(_rows(capsys.readouterr().out)["O1"][3]) > 6
    assert check.main([*common, "--target", "260"]) == 2
    assert "(0, 250) Hz at 500 Hz" in capsys.readouterr().err


def test_run_check_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match=r"\(3, 100\)"):
        check.run_check(np.zeros((3, 100)), FS, 10.0)
    with pytest.raises(ValueError, match="125"):
        check.run_check(np.zeros((8, 3000)), FS, 125.0)


def test_board_failure_is_reported(monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture) -> None:
    from brainsync import board as bd

    class Broken(FakeBoard):
        serial_port = "/dev/cu.usbserial-test"

        def __init__(self, **kw: object) -> None:
            super().__init__([15.0])

        def __enter__(self) -> "Broken":
            raise RuntimeError("could not start the Cerelog X8 on /dev/cu.usbserial-test")

    monkeypatch.setattr(bd, "CerelogBoard", Broken)
    assert check.main(["--no-show", "--seconds", "8"]) == 1
    err = capsys.readouterr().err
    assert "usbserial-test" in err and "error:" in err


@pytest.mark.parametrize("segment, match", [
    ("1", "too coarse"),
    ("0", "--segment must be positive"),
    ("-2", "--segment must be positive"),
    ("0.02", "need at least 8"),
])
def test_bad_segment_fails_before_recording(segment: str, match: str,
                                            monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture) -> None:
    def no_recording(*a: object, **k: object) -> None:
        raise AssertionError("must not record with an unusable --segment")

    monkeypatch.setattr(check, "record", no_recording)
    code = check.main(["--fake", "--no-show", "--warmup", "0", "--seconds", "10",
                       "--segment", segment])
    err = capsys.readouterr().err
    assert code == 2 and match in err and "Traceback" not in err


def test_shortest_usable_segment_runs(capsys: pytest.CaptureFixture) -> None:
    assert check.segment_problem(1.5, 250.0) == ""
    assert "at least 1.5 s" in check.segment_problem(1.0, 250.0)
    code = check.main(["--fake", "--no-show", "--warmup", "0", "--seconds", "10",
                       "--segment", "1.5", "--target", "10"])
    cap = capsys.readouterr()
    assert code == 0, cap.err
    assert float(_rows(cap.out)["O1"][3]) > 3


def test_railed_and_flat_channels_show_na_and_are_never_best() -> None:
    rng = np.random.default_rng(6)
    n = int(10 * FS)
    X = rng.normal(0, 5, (8, n))
    X[0] = ADC_FULL_SCALE_UV  # railed
    X[1] = 5_000.0            # flat
    res = check.run_check(X, FS, 15.0)
    for ch in res.channels[:2]:
        assert np.isnan(ch.snr_f) and np.isnan(ch.snr_2f) and np.isnan(ch.peak_hz)
    rows = _rows(check.render(res))
    assert rows["Fp1"][3:6] == ["n/a", "n/a", "n/a"] and rows["Fp2"][3:6] == ["n/a"] * 3
    # every usable channel negative: the best is still a usable one
    neg = [check.ChannelStats(c.name, c.dc_uv, c.rms_uv, -2.0 - i, c.snr_2f, c.peak_hz, c.flags)
           if c.usable else c for i, c in enumerate(res.channels)]
    res_neg = check.CheckResult(res.target, res.fs, neg, res.median_r, res.freqs, res.power)
    assert res_neg.best() is not None and res_neg.best().usable
    assert res_neg.best().snr_f == pytest.approx(-4.0)


def test_no_usable_channel_says_so() -> None:
    X = np.full((8, int(10 * FS)), ADC_FULL_SCALE_UV)
    res = check.run_check(X, FS, 15.0)
    assert res.best() is None
    text = check.render(res)
    assert "response: none (no usable channel)" in text
    assert all(_rows(text)[nm][3] == "n/a" for nm in MONTAGE)


def test_ctrl_c_in_warmup_ends_the_check(monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture) -> None:
    # regression: record() swallowed Ctrl-C, so the check went on to record and plot
    calls: list[dict] = []

    def fake_record(board, seconds, **kw):
        calls.append(kw)
        raise KeyboardInterrupt

    monkeypatch.setattr(check, "record", fake_record)
    assert check.main(["--fake", "--target", "10", "--no-show", "--warmup", "2"]) == 130
    assert len(calls) == 1 and not calls[0].get("keep_on_interrupt")
    cap = capsys.readouterr()
    assert "interrupted before the check started" in cap.err and cap.out == ""


def test_main_recording_keeps_data_on_ctrl_c(monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture) -> None:
    seen: list[dict] = []
    real = check.record

    def spy(board, seconds, **kw):
        seen.append(kw)
        return real(board, seconds, **kw)

    monkeypatch.setattr(check, "record", spy)
    assert check.main(["--fake", "--target", "10", "--no-show", "--warmup", "0",
                       "--seconds", "8"]) == 0
    assert seen[-1].get("keep_on_interrupt") is True
