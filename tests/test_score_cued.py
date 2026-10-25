"""score_cued on synthetic cued sessions with known labels (plus the real recordings when present)."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from brainsync import paths, score_cued
from brainsync.classify import ABSTAIN, make
from brainsync.metrics import itr_bpm
from brainsync.recordings import MONTAGE, POSTERIOR, load_recording, save_recording
from brainsync.score_cued import (DEFAULT_MARGIN, GAZE_SHIFT_S, Report, format_report, main,
                                  parse_channels, resolve_windows, score_recording,
                                  score_windows, split_windows)
from brainsync.synthetic import fake_session, rest_trials, ssvep_trials

FS = 250.0
FREQS = [15.0, 17.0]
LABELS = [0, 1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1]
WINDOW_S = 3.86
LATENCY = 0.14
# fake_session layout: 1 s lead, then per trial 1 s cue, 4 s flicker, 3 s rest
LEAD_S, CUE_S, STIM_S, REST_S = 1.0, 1.0, 4.0, 3.0
PERIOD_S = CUE_S + STIM_S + REST_S


def _write_session(path: Path, labels=LABELS, freqs=FREQS, amp: float = 2.0, seed: int = 0,
                   window_s: float = WINDOW_S, artifacts: dict[int, str] | None = None) -> Path:
    """Fake cued session; ``artifacts`` maps trial index to "on" or "off" for a 300 µV burst."""
    X, t, text = fake_session(freqs, labels, fs=FS, amp=amp, seed=seed)
    rng = np.random.default_rng(seed + 100)
    post = [MONTAGE.index(c) for c in POSTERIOR]
    for trial, where in (artifacts or {}).items():
        stim_on = LEAD_S + trial * PERIOD_S + CUE_S
        start = stim_on + LATENCY + (STIM_S if where == "off" else 0.0)
        a, b = int(round((start + 0.5) * FS)), int(round((start + 2.5) * FS))
        X[post, a:b] += 300.0 * rng.standard_normal((len(post), b - a))
    return save_recording(path, X, t, FS, MONTAGE, freqs, text, window_s=window_s,
                          latency=LATENCY)


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_session(tmp_path_factory.mktemp("cued") / "2026-01-01_000000_cued.npz")


@pytest.fixture(scope="module")
def report(session: Path) -> score_cued.Report:
    return score_recording(load_recording(session))


def _shuffled(X: np.ndarray, y: np.ndarray, seed: int) -> tuple[np.ndarray, np.ndarray]:
    idx = np.random.default_rng(seed).permutation(y.size)
    return X[idx], y[idx]


# -- windows --------------------------------------------------------------------------

def test_resolve_windows_appends_recorded_and_drops_longer() -> None:
    assert resolve_windows([1, 2, 3], 3.86) == [1.0, 2.0, 3.0, 3.86]
    assert resolve_windows([3, 1, 5, 1], 3.86) == [1.0, 3.0, 3.86]
    assert resolve_windows([3.86, 2], 3.86) == [2.0, 3.86]
    assert resolve_windows([], 2.0) == [2.0]


def test_split_windows_reports_too_long() -> None:
    assert split_windows([0.5, 2, 5, 9, 5], 3.86) == ([0.5, 2.0, 3.86], [5.0, 9.0])
    assert split_windows([1, 2], 3.86)[1] == []


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_resolve_windows_rejects_nonpositive(bad: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        resolve_windows([1.0, bad], 3.86)


def test_resolve_windows_rejects_bad_recorded_length() -> None:
    with pytest.raises(ValueError, match="0"):
        resolve_windows([1.0], 0.0)


# -- score_windows --------------------------------------------------------------------

@pytest.fixture(scope="module")
def arrays() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    on, y = ssvep_trials(FREQS, FS, n_per_target=12, seconds=2.0, snr_db=-12.0, seed=3)
    on, y = _shuffled(on, y, seed=7)          # interleave targets so label shifts matter
    off = rest_trials(on.shape[0], FS, on.shape[1], 2.0, seed=4)
    return on, off, y


def test_short_windows_use_first_seconds_and_own_baseline(arrays) -> None:
    on, off, y = arrays
    rows = score_windows(on, off, y, FREQS, FS, [1.0, 2.0], ["cca", "fbcca"], margin=0.0)
    assert [(r.method, r.window_s) for r in rows] == [
        ("cca", 1.0), ("fbcca", 1.0), ("cca", 2.0), ("fbcca", 2.0)]
    for r in rows:
        n = int(round(r.window_s * FS))
        clf = make(r.method, freqs=FREQS, fs=FS).fit_baseline(off[..., :n])
        np.testing.assert_allclose(r.baseline_mean, clf.baseline_mean)
        assert r.baseline_spread == pytest.approx(clf.baseline_spread)
        np.testing.assert_allclose(r.scores, clf.scores(on[..., :n]))
        np.testing.assert_array_equal(r.pred, clf.scores(on[..., :n]).argmax(axis=1))
        assert r.hits == int(np.sum(r.pred == y)) and r.n == y.size
        assert r.abstain == 0.0
        assert r.itr_bpm == pytest.approx(itr_bpm(2, r.accuracy, r.window_s + GAZE_SHIFT_S))


def test_ignores_samples_past_the_window(arrays) -> None:
    on, off, y = arrays
    n = int(FS)
    garbage_on, garbage_off = on.copy(), off.copy()
    garbage_on[..., n:] = 1e6 * np.random.default_rng(0).standard_normal(garbage_on[..., n:].shape)
    garbage_off[..., n:] = -5e5
    a = score_windows(on, off, y, FREQS, FS, [1.0], ["cca"])[0]
    b = score_windows(garbage_on, garbage_off, y, FREQS, FS, [1.0], ["cca"])[0]
    np.testing.assert_allclose(a.scores, b.scores)


def test_decodes_synthetic_ssvep_and_still_control_at_chance(arrays) -> None:
    on, off, y = arrays
    for r in score_windows(on, off, y, FREQS, FS, [2.0], ["cca", "fbcca"]):
        assert r.accuracy >= 0.9, r
        assert r.p < 1e-4
        assert r.control_p > 0.01, r


def test_control_is_scored_against_the_cued_labels(arrays) -> None:
    """Still windows that (wrongly) carry the cued frequency must score high against y."""
    on, _, y = arrays
    leak, y2 = ssvep_trials(FREQS, FS, n_per_target=12, seconds=2.0, snr_db=-12.0, seed=9)
    leak, y2 = _shuffled(leak, y2, seed=7)
    np.testing.assert_array_equal(y, y2)
    for r in score_windows(on, leak, y, FREQS, FS, [2.0], ["cca", "fbcca"], margin=0.0):
        assert r.control_hits == int(np.sum(r.control_pred == y))
        assert r.control_accuracy >= 0.9, r
        assert r.control_p < 1e-3
        assert int(np.sum(r.control_pred == np.roll(y, 1))) < r.control_hits - 5


def test_control_uses_the_still_baseline(arrays) -> None:
    """A 15 Hz component common to every still window must be removed by the baseline."""
    on, off, y = arrays
    tt = np.arange(off.shape[-1]) / FS
    biased = off + 0.6 * np.sin(2 * np.pi * FREQS[0] * tt)
    for r in score_windows(on, biased, y, FREQS, FS, [2.0], ["cca", "fbcca"], margin=0.0):
        clf = make(r.method, freqs=FREQS, fs=FS).fit_baseline(biased)
        expected = clf.scores(biased).argmax(axis=1)
        raw = clf.raw_scores(biased).argmax(axis=1)
        assert np.all(raw == 0)                      # the bias alone would say 15 Hz
        np.testing.assert_array_equal(r.control_pred, expected)
        assert 0 < np.sum(r.control_pred == 1) < y.size
        assert r.control_hits == int(np.sum(expected == y))


def test_margin_controls_abstention(arrays) -> None:
    on, off, y = arrays
    none = score_windows(on, off, y, FREQS, FS, [2.0], ["cca"], margin=0.0)[0]
    huge = score_windows(on, off, y, FREQS, FS, [2.0], ["cca"], margin=1e6)[0]
    assert none.abstain == 0.0 and none.control_abstain == 0.0
    assert huge.abstain == 1.0 and huge.control_abstain == 1.0
    assert np.isnan(huge.committed_accuracy)
    np.testing.assert_array_equal(none.pred, huge.pred)   # forced-choice accuracy is unaffected
    assert none.hits == huge.hits


@pytest.mark.parametrize("method", ["cca", "fbcca"])
def test_abstention_numbers_at_an_intermediate_margin(arrays, method: str) -> None:
    on, off, y = arrays
    # weak flicker so some flicker decisions are wrong and some are close
    on = off + 0.3 * (on - off.mean())
    clf = make(method, freqs=FREQS, fs=FS).fit_baseline(off)
    margin = float(np.median(clf.gaps(clf.scores(on))))
    r = score_windows(on, off, y, FREQS, FS, [2.0], [method], margin=margin)[0]

    clf.margin = margin
    s = clf.scores(on)
    pm = clf.predict_scores(s)
    ctl = clf.predict_scores(clf.scores(off))
    go = pm != ABSTAIN
    np.testing.assert_array_equal(r.pred_margin, pm)
    assert r.abstain == pytest.approx(np.mean(pm == ABSTAIN))
    assert 0.0 < r.abstain < 1.0
    assert r.committed_accuracy == pytest.approx(np.mean(s.argmax(axis=1)[go] == y[go]))
    assert r.committed_accuracy != pytest.approx(r.accuracy)
    assert r.control_abstain == pytest.approx(np.mean(ctl == ABSTAIN))
    assert r.control_abstain != pytest.approx(r.abstain)


def test_confusion_rows_are_cued_columns_decoded() -> None:
    strong, _ = ssvep_trials([15.0], FS, n_per_target=12, seconds=2.0, snr_db=-5.0, seed=1)
    weak = rest_trials(12, FS, strong.shape[1], 2.0, seed=2)        # 17 Hz cued, nothing there
    on = np.concatenate([strong, weak])
    y = np.repeat([0, 1], 12)
    off = rest_trials(24, FS, strong.shape[1], 2.0, seed=3)
    rows = score_windows(on, off, y, FREQS, FS, [2.0], ["cca", "fbcca"], margin=0.0)
    for r in rows:
        expected = np.zeros((2, 2), dtype=int)
        np.add.at(expected, (y, r.pred), 1)
        assert expected[0, 1] != expected[1, 0], expected      # asymmetric, so order matters
        np.testing.assert_array_equal(r.confusion, expected)

    rep = Report(path=None, freqs=np.asarray(FREQS), channels=POSTERIOR, n_total=24,
                 kept=np.arange(24), labels=y, margin=0.0, rows=rows)
    lines = format_report(rep, n_perm=50).splitlines()
    for r in rows:
        head = lines.index(f"decoded vs cued, {r.method} at 2 s "
                            f"(cued target per row, decoded per column):")
        assert lines[head + 1].split("|")[1].split() == ["15", "Hz", "17", "Hz"]
        for k, name in enumerate(["15 Hz", "17 Hz"]):
            label, counts = lines[head + 2 + k].split("|")
            assert label.strip() == name
            assert [int(c) for c in counts.split()] == r.confusion[k].tolist()


def test_score_windows_validates_inputs(arrays) -> None:
    on, off, y = arrays
    with pytest.raises(ValueError, match="labels"):
        score_windows(on, off, y[:-1], FREQS, FS, [1.0])
    with pytest.raises(ValueError, match="matching"):
        score_windows(on, off[:, :2], y, FREQS, FS, [1.0])
    with pytest.raises(ValueError, match="does not fit"):
        score_windows(on, off, y, FREQS, FS, [3.0])
    with pytest.raises(ValueError, match="2 targets"):
        score_windows(on, off, y, [15.0], FS, [1.0])
    with pytest.raises(ValueError, match="nope"):
        score_windows(on, off, y, FREQS, FS, [1.0], ["nope"])


# -- whole recordings -----------------------------------------------------------------

def test_fake_session_scores_with_known_labels(report: score_cued.Report) -> None:
    assert report.n_total == len(LABELS)
    assert report.channels == POSTERIOR
    np.testing.assert_array_equal(report.labels, np.asarray(LABELS)[report.kept])
    assert sorted({r.window_s for r in report.rows}) == [1.0, 2.0, 3.0, WINDOW_S]
    assert {r.method for r in report.rows} == {"cca", "fbcca"}
    assert report.margin == DEFAULT_MARGIN
    assert report.skipped_windows == ()
    for method in ("cca", "fbcca"):
        full = report.row(method, WINDOW_S)
        assert full.accuracy >= 0.95, full
        assert full.control_accuracy <= 0.75, full
        assert full.control_hits == int(np.sum(full.control_pred == report.labels))
        assert report.row(method, 2.0).accuracy >= 0.85
    assert report.control_max_p(n_perm=500) > 0.01


def test_artifact_rejection_drops_the_noisy_trials(tmp_path: Path) -> None:
    path = _write_session(tmp_path / "art_cued.npz", artifacts={5: "on", 12: "off"})
    rep = score_recording(load_recording(path))
    assert rep.n_total == len(LABELS)
    np.testing.assert_array_equal(rep.dropped, [5, 12])
    np.testing.assert_array_equal(rep.kept, [i for i in range(len(LABELS)) if i not in (5, 12)])
    np.testing.assert_array_equal(rep.labels, np.delete(LABELS, [5, 12]))
    assert all(r.n == len(LABELS) - 2 for r in rep.rows)
    text = format_report(rep, n_perm=50)
    assert f"trials kept {len(LABELS) - 2}/{len(LABELS)}" in text
    assert "artifact rejection removed 2: #5 #12" in text

    keep_all = score_recording(load_recording(path), methods=["cca"], reject=0)
    assert keep_all.dropped.size == 0 and len(keep_all.labels) == len(LABELS)
    assert "artifact rejection" not in format_report(keep_all, n_perm=50)


def test_clean_session_drops_nothing(report: score_cued.Report) -> None:
    assert report.dropped.size == 0
    assert "artifact rejection" not in format_report(report, n_perm=50)


def test_still_timing_reports_cue_overlap(report: score_cued.Report) -> None:
    st = report.still
    assert st is not None
    assert st.offset_s == pytest.approx(LATENCY, abs=1 / FS)
    assert st.length_s == pytest.approx(WINDOW_S)
    # still ends at stim_off + 0.14 + 3.86 = next stim_on; the 1 s cue lies inside it
    assert st.into_cue_s == pytest.approx(CUE_S, abs=1.5 / FS)
    assert st.lead_s == pytest.approx(0.0, abs=1.5 / FS)
    assert not st.overlaps_flicker
    text = format_report(report, n_perm=50)
    assert "1.00 s inside the next cue screen" in text and "WARNING" not in text


def test_still_window_reaching_the_next_flicker_is_flagged(tmp_path: Path) -> None:
    path = _write_session(tmp_path / "long_cued.npz", window_s=4.2)
    rep = score_recording(load_recording(path), methods=["cca"], windows=[2.0])
    assert rep.still.overlaps_flicker
    assert rep.still.lead_s == pytest.approx(-0.34, abs=1.5 / FS)
    assert "WARNING: still windows overlap the next flicker by up to 0.34 s" in \
        format_report(rep, n_perm=50)


def test_still_windows_alone_give_chance(tmp_path: Path) -> None:
    rep = score_recording(load_recording(_write_session(tmp_path / "x_cued.npz", amp=0.0, seed=5)))
    for r in rep.rows:
        assert r.p > 0.001, r
        assert r.control_p > 0.001, r


def test_control_max_p_is_a_permutation_p(report: score_cued.Report) -> None:
    p = report.control_max_p(n_perm=400, seed=1)
    assert 1 / 401 <= p <= 1.0
    assert p == report.control_max_p(n_perm=400, seed=1)
    rigged = Report(path=None, freqs=report.freqs, channels=report.channels,
                    n_total=report.n_total, kept=report.kept, labels=report.labels,
                    margin=report.margin, rows=[report.rows[0]])
    rigged.rows[0] = score_cued.Row(**{**rigged.rows[0].__dict__,
                                       "control_pred": report.labels.copy()})
    assert rigged.control_max_p(n_perm=400) < 0.01
    with pytest.raises(ValueError, match="n_perm"):
        report.control_max_p(n_perm=0)


def test_best_max_p_corrects_for_the_cell_choice(report: score_cued.Report) -> None:
    p_best = report.best_max_p(n_perm=2000, seed=1)
    assert p_best >= report.best.p * 0.5          # never far below the single-cell p
    assert p_best == report.best_max_p(n_perm=2000, seed=1)
    noisy = Report(path=None, freqs=report.freqs, channels=report.channels,
                   n_total=report.n_total, kept=report.kept, labels=report.labels,
                   margin=report.margin,
                   rows=[score_cued.Row(**{**r.__dict__, "pred": np.random.default_rng(k)
                                           .integers(0, 2, r.n)})
                         for k, r in enumerate(report.rows)])
    # with 8 coin-flip cells the best one looks better alone than it is
    assert noisy.best_max_p(n_perm=2000) > noisy.best.p


def test_format_report_gives_the_corrected_best_p(report: score_cued.Report) -> None:
    text = format_report(report, n_perm=50)
    assert re.search(r"best: .* p=\d\.\d{4} \(this cell\), max over 8 cells "
                     r"label-permutation p=\d\.\d{4}", text)


def test_channels_follow_the_recording_names(tmp_path: Path,
                                             capsys: pytest.CaptureFixture) -> None:
    # regression: --channels was checked against the fixed montage, not the file's names
    names = ("Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "Oz")
    X, t, text = fake_session(FREQS, LABELS[:6], fs=FS, amp=2.0, seed=3)
    path = save_recording(tmp_path / "2026-01-01_000000_cued.npz", X, t, FS, names, FREQS,
                          text, window_s=WINDOW_S, latency=LATENCY)
    assert main([str(path), "--methods", "cca", "--windows", "2",
                 "--channels", "P7,P8,O1,Oz"]) == 0
    assert "channels P7,P8,O1,Oz" in capsys.readouterr().out
    assert main([str(path), "--methods", "cca", "--windows", "2", "--channels", "9"]) == 1
    assert "9" in capsys.readouterr().err
    assert main([str(path), "--methods", "cca", "--windows", "2"]) == 1   # default O2 absent
    assert "'O2'" in capsys.readouterr().err


def test_channels_and_reject_options(session: Path) -> None:
    rec = load_recording(session)
    rep = score_recording(rec, methods=["cca"], windows=[2.0], channels=["O1", "O2"], reject=0)
    assert rep.channels == ("O1", "O2")
    assert len(rep.kept) == rep.n_total == len(LABELS)
    assert [r.window_s for r in rep.rows] == [2.0, WINDOW_S]
    by_index = score_recording(rec, methods=["cca"], windows=[2.0], channels=[6, 7], reject=0)
    assert by_index.channels == ("O1", "O2")
    np.testing.assert_allclose(by_index.rows[0].scores, rep.rows[0].scores)
    with pytest.raises(KeyError, match="Oz"):
        score_recording(rec, channels=["Oz"])


def test_rejects_single_target_recording(tmp_path: Path) -> None:
    X, t, text = fake_session([15.0], [0, 0, 0], fs=FS)
    path = save_recording(tmp_path / "a_trials.npz", X, t, FS, MONTAGE, [15.0], text)
    with pytest.raises(ValueError, match="2 targets"):
        score_recording(load_recording(path))


def test_format_report_lists_every_row(report: score_cued.Report) -> None:
    text = format_report(report, n_perm=50)
    assert "trials kept 20/20 [15 Hz x10, 17 Hz x10]" in text and "chance 50%" in text
    assert text.count("\ncca ") == 4 and text.count("\nfbcca ") == 4
    assert f"decoded vs cued, cca at {WINDOW_S:g} s (cued target per row" in text
    assert f"decoded vs cued, fbcca at {WINDOW_S:g} s (cued target per row" in text
    assert "rest mean" in text
    best = report.best
    assert (f"best: {best.method} {best.hits}/{best.n}" in text
            and f"still control there {best.control_hits}/{best.n}" in text)
    worst = report.worst_control
    assert re.search(rf"highest still control: {worst.control_hits}/{worst.n} .* "
                     rf"max over 8 cells, label-permutation p=\d\.\d{{3}}", text)


# -- CLI ------------------------------------------------------------------------------

def test_parse_channels() -> None:
    assert parse_channels("O1, O2") == ["O1", "O2"]
    assert parse_channels("6,7") == [6, 7]
    assert parse_channels("P8,7") == ["P8", 7]


def test_main_explicit_files_and_options(session: Path, capsys: pytest.CaptureFixture) -> None:
    code = main([str(session), "--methods", "fbcca", "--windows", "1", "2",
                 "--margin", "0.5", "--channels", "O1,O2,P8"])
    out, err = capsys.readouterr()
    assert code == 0
    assert "scoring" in err and "scoring" not in out
    assert session.name in out
    assert "\nfbcca " in out and "\ncca " not in out
    assert "channels O1,O2,P8" in out and "margin 0.5" in out
    assert " 1.00 s" in out and " 2.00 s" in out and f" {WINDOW_S:.2f} s" in out


def test_main_numeric_channels_and_long_windows(session: Path, capsys: pytest.CaptureFixture) -> None:
    assert main([str(session), "--methods", "cca", "--windows", "0.5", "2", "5",
                 "--channels", "6,7"]) == 0
    out, err = capsys.readouterr()
    assert "channels O1,O2" in out
    assert "skipping windows 5 s, longer than the recorded 3.86 s" in err
    assert "not scored (longer than the recorded window): 5 s" in out
    rows = [ln.split()[1] for ln in out.splitlines() if ln.startswith("cca ")]
    assert rows == ["0.50", "2.00", f"{WINDOW_S:.2f}"]


def test_main_default_finds_cued_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture) -> None:
    rec_dir = tmp_path / "spectrum_checks"
    _write_session(rec_dir / "2026-01-02_000000_cued.npz", seed=1)
    _write_session(rec_dir / "2026-01-01_000000_cued.npz", seed=2)
    X, t, text = fake_session([15.0], [0, 0], fs=FS)
    save_recording(rec_dir / "2026-01-03_000000_trials.npz", X, t, FS, MONTAGE, [15.0], text)
    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path))
    assert main(["--methods", "cca", "--windows", "2"]) == 0
    out = capsys.readouterr().out
    assert out.index("2026-01-01_000000_cued") < out.index("2026-01-02_000000_cued")
    assert "_trials" not in out


def test_main_no_recordings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path))
    assert main([]) == 1
    assert "no *_cued.npz" in capsys.readouterr().err


@pytest.mark.parametrize("argv, needle", [
    (["--methods", "svm"], "svm"),
    (["--channels", "-1"], ">= 0"),
    (["--channels", ","], "no channels"),
    (["--windows", "0"], "positive"),
    (["--margin", "-1"], "margin"),
    (["--reject", "-1"], "reject"),
    (["--harmonics", "0"], "harmonics"),
])
def test_main_bad_options_exit_2(session: Path, capsys: pytest.CaptureFixture,
                                 argv: list[str], needle: str) -> None:
    assert main([str(session), *argv]) == 2
    out, err = capsys.readouterr()
    assert needle in err and out == "" and "scoring" not in err


def test_main_recording_errors_exit_1(session: Path, tmp_path: Path,
                                      capsys: pytest.CaptureFixture) -> None:
    missing = tmp_path / "missing_cued.npz"
    assert main([str(missing), str(session), "--methods", "cca"]) == 1
    out, err = capsys.readouterr()
    assert str(missing) in err and session.name in out

    X, t, text = fake_session([15.0], [0, 0, 0], fs=FS)
    single = save_recording(tmp_path / "s_trials.npz", X, t, FS, MONTAGE, [15.0], text)
    assert main([str(single)]) == 1
    err = capsys.readouterr().err
    error_line = next(ln for ln in err.splitlines() if ln.startswith("error:"))
    assert error_line.count(str(single)) == 1 and "2 targets" in error_line


# -- real recordings ------------------------------------------------------------------

REFERENCE = "spectrum_checks/2026-10-22_162029_cued.npz"


@pytest.mark.realdata(REFERENCE)
def test_reference_recording_decodes() -> None:
    rep = score_recording(load_recording(paths.data_dir() / REFERENCE))
    assert len(rep.labels) >= 15
    best = rep.best
    assert best.accuracy >= 0.75 and best.p < 0.01
    # the still control at every cell that reaches the 75% target stays at most 65%
    passing = [r for r in rep.rows if r.accuracy >= 0.75]
    assert passing and all(r.control_accuracy <= 0.65 for r in passing), passing
    # and the highest control over all cells is consistent with chance
    assert rep.control_max_p() > 0.05
    # the best cell stays significant after correcting for picking it among the cells
    assert rep.best_max_p() < 0.05
    for method in ("cca", "fbcca"):
        assert rep.row(method, max(r.window_s for r in rep.rows)).control_p > 0.01


# -- trained methods, held out ---------------------------------------------------------

def test_trained_methods_are_scored_leave_one_trial_out(arrays) -> None:
    on, off, y = arrays
    rows = score_windows(on, off, y, FREQS, FS, [2.0], ["cca", "etrca-sf", "mscca"], margin=0.0)
    assert [(r.method, r.held_out) for r in rows] == [("cca", False), ("etrca-sf", True),
                                                      ("mscca", True)]
    et = rows[1]
    assert et.accuracy >= 0.9 and et.n == y.size and et.abstain == 0.0
    # trial 5 scored by a fit on every other trial, baseline on their still windows
    t = 5
    train = np.arange(y.size) != t
    clf = make("etrca-sf", freqs=FREQS, fs=FS).fit(on[train], y[train]).fit_baseline(off[train])
    np.testing.assert_allclose(et.scores[t], clf.scores(on[t])[0])
    np.testing.assert_allclose(et.control_pred[t], clf.scores(off[t]).argmax())
    assert et.pred[t] == et.scores[t].argmax()
    assert et.confusion.sum() == y.size and et.baseline_mean.shape == (2,)
    assert et.baseline_spread > 0


def test_held_out_needs_enough_trials_per_target(arrays) -> None:
    on, off, y = arrays
    few = np.flatnonzero(y == 0)[:2].tolist() + np.flatnonzero(y == 1)[:3].tolist()
    with pytest.raises(ValueError, match="trca-sf.*hold trial.*at least 2"):
        score_windows(on[few], off[few], y[few], FREQS, FS, [1.0], ["trca-sf"])
    rows = score_windows(on[few], off[few], y[few], FREQS, FS, [1.0], ["mscca"])  # 1 trial is enough
    assert rows[0].held_out and rows[0].n == 5


def test_report_marks_held_out_rows(session: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([str(session), "--methods", "fbcca,etrca-sf", "--windows", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "+ held out" in out
    assert re.search(r"^etrca-sf\+\s+2\.00 s", out, re.MULTILINE)
    assert re.search(r"^fbcca\s+2\.00 s", out, re.MULTILINE)
    rep = score_recording(load_recording(session), ["etrca-sf"], [2.0])
    assert rep.rows[0].held_out and rep.rows[0].accuracy >= 0.9


def test_cli_rejects_unknown_but_accepts_trained_names(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--methods", "nope"]) == 2
    assert "choose from" in capsys.readouterr().err
    assert score_cued._usage_problem(
        type("A", (), {"windows": [1.0], "margin": 1.0, "reject": 3.0, "harmonics": 3,
                       "channels": "O1"})(), ["trca-r-sf", "etrca-r-sf"], ["O1"]) is None
def test_trial_blocks_prefer_block_markers() -> None:
    from brainsync.score_cued import trial_blocks
    text = ("[marker 100.0] block_start:0\n[marker 108.0] block_end:0\n"
            "[marker 108.1] block_start:1\n[marker 116.0] block_end:1\n")
    kept = np.array([1, 2, 3])                       # trial 0 dropped: by position blocks would shift
    on = np.array([105.0, 109.0, 113.0])
    np.testing.assert_array_equal(trial_blocks(kept, 2, on, text), [0, 1, 1])
    np.testing.assert_array_equal(trial_blocks(kept, 2), [0, 1, 1])      # positional fallback
    np.testing.assert_array_equal(trial_blocks(np.array([0, 1, 2, 3]), 2, np.array([101., 105., 109., 113.]), text), [0, 0, 1, 1])
    np.testing.assert_array_equal(trial_blocks(kept, 2, np.array([99.0, 109.0, 113.0]), text), [0, 1, 1])  # before any block: fallback
