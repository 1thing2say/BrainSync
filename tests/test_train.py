"""brainsync.train on synthetic cued sessions: the report, the saved model, and that no
held-out trial (or its still screen) ever reaches a fit or a baseline in its own fold."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from brainsync import train
from brainsync.classify import ABSTAIN, CCA, FBCCA
from brainsync.online import Decoder, _trial_spans
from brainsync.recordings import MONTAGE, POSTERIOR, load_recording, save_recording
from brainsync.synthetic import fake_session
from brainsync.trca import TRCA, load_model

FS = 250.0
FREQS4 = [11.0, 15.0, 17.0, 13.0]
FREQS2 = [15.0, 17.0]


def _blocks(n_targets: int, n_blocks: int, seed: int = 0) -> list[int]:
    """Shuffled blocks that cue every target once, like a recorded session."""
    rng = np.random.default_rng(seed)
    return [int(k) for _ in range(n_blocks) for k in rng.permutation(n_targets)]


def _write(path: Path, freqs, labels, seed: int = 0, amp: float = 2.0) -> Path:
    X, t, text = fake_session(freqs, labels, fs=FS, amp=amp, seed=seed)
    return save_recording(path, X, t, FS, MONTAGE, freqs, text)


@pytest.fixture(scope="module")
def four(tmp_path_factory) -> Path:
    """4 targets x 6 trials."""
    return _write(tmp_path_factory.mktemp("four") / "2026-01-01_000000_cued.npz", FREQS4,
                  _blocks(4, 6), seed=1)


@pytest.fixture(scope="module")
def small(tmp_path_factory) -> Path:
    """2 targets x 5 trials: quick enough for the replay and the leakage guard."""
    return _write(tmp_path_factory.mktemp("small") / "2026-01-02_000000_cued.npz", FREQS2,
                  _blocks(2, 5, seed=2), seed=2, amp=1.0)


@pytest.fixture(scope="module")
def faint(tmp_path_factory) -> Path:
    """Like ``small`` with a weak response, so FBCCA is right on only some windows."""
    return _write(tmp_path_factory.mktemp("faint") / "2026-01-02_000001_cued.npz", FREQS2,
                  _blocks(2, 5, seed=2), seed=2, amp=0.4)


@pytest.fixture(scope="module")
def trained(four, tmp_path_factory):
    """One train.main run: (exit code, stdout, model path, json path)."""
    out = tmp_path_factory.mktemp("model")
    model, report = out / "model.npz", out / "report.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = train.main([str(four), "--out", str(model), "--no-replay", "--json", str(report)])
    return code, buf.getvalue(), model, report


# -- the report and the model -------------------------------------------------------------

def test_main_reports_every_cell_and_a_verdict(trained) -> None:
    code, out, _, _ = trained
    assert code == 0
    lines = out.splitlines()
    header = next(ln for ln in lines if ln.lstrip().startswith("window"))
    assert "fbcca" in header and "trca" in header
    for w in ("1", "1.5", "2"):
        for cond in ("aligned", "sliding"):
            row = [ln for ln in lines if ln.split()[:3] == [w, "s", cond]]
            assert len(row) == 1, (w, cond)
            assert row[0].count("%") >= 10          # five numbers for each method
    verdict = lines[-1]                                # "trca beats fbcca ..." or "does not beat"
    assert verdict.startswith(("trca beats fbcca on held-out windows: ",
                               "trca does not beat fbcca on held-out windows ("))
    assert any(ln.startswith("noise: one trial = 4.2 points") for ln in lines)   # 24 trials
    assert any(ln.startswith("McNemar") for ln in lines)
    assert any(ln.startswith("fusion chosen per fold: ") for ln in lines)
    assert "replay window" not in out                  # --no-replay
    assert "11 Hz 6/6" in out and "6 folds" in out


def test_saved_model_matches_the_recording(trained) -> None:
    _, out, model, _ = trained
    assert f"model saved: {model}" in out
    assert "python -m brainsync.demo -c four_targets --classifier trca-phase --model" in out
    clf = load_model(model)
    np.testing.assert_allclose(clf.freqs, FREQS4)
    assert clf.info is not None
    assert clf.info.ch_names == POSTERIOR
    assert clf.info.trial_s == pytest.approx(3.86)
    assert clf.info.band == (1.0, 100.0) and clf.info.notch == 60.0
    assert clf.info.sources == ("2026-01-01_000000_cued.npz",)


def test_json_has_every_number_and_the_model_path(trained) -> None:
    _, _, model, report = trained
    data = json.loads(report.read_text())
    assert data["model"]["path"] == str(model)
    assert data["n_folds"] == 6 and data["kept"] == [6, 6, 6, 6]
    assert len(data["cells"]) == 3 * 2 and len(data["folds"]) == 6
    held = sorted(i for f in data["folds"] for i in f["held_out"])
    assert held == list(range(24))
    assert data["replay"] is None
    assert set(data["verdict"]) >= {"trca_bc", "fbcca_bc", "trca_beats", "text"}


def test_fixed_fbcca_fusion_scores_exactly_like_fbcca(small, capsys) -> None:
    assert train.main([str(small), "--no-save", "--no-replay", "--windows", "1.5",
                       "--fusion", "fbcca"]) == 0
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if ln.split()[:2] == ["1.5", "s"]]
    assert len(rows) == 2
    for row in rows:
        fb, tr = row.split("|")[1:3]
        assert fb.split()[-5:] == tr.split()[-5:]      # (0, 1) is FBCCA: same five numbers
        assert row.split("|")[-1].strip() == "+0.0"
    assert "fusion chosen per fold: (0,1) x5" in out


def test_replay_prints_commit_counts(small, capsys) -> None:
    assert train.main([str(small), "--no-save", "--windows", "1.5"]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("replay window 1.5 s: "))
    for m in ("fbcca", "trca"):
        assert f"{m} correct " in line
    assert "locked" not in line
    counts = [int(tok.split("/")[1].rstrip(",;")) for tok in line.split() if "/" in tok]
    assert counts == [10, 10, 10, 10]                  # trials, still screens, per method


def test_lock_replays_trca_at_the_onset_phase(small, tmp_path, capsys) -> None:
    """--lock adds a "locked" replay of the same held-out trials, phase-locked to each
    trial's stim_on, and puts --lock in the run-it line."""
    report = tmp_path / "r.json"
    assert train.main([str(small), "--out", str(tmp_path / "m.npz"), "--windows", "1.5",
                       "--lock", "--json", str(report)]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("replay window 1.5 s: "))
    assert line.count(" correct ") == 3 and "locked correct " in line
    counts = [int(tok.split("/")[1].rstrip(",;")) for tok in line.split() if "/" in tok]
    assert counts == [10] * 6
    run_it = next(ln for ln in out.splitlines() if ln.startswith("run it: "))
    assert run_it.endswith(" --lock") and "--classifier trca-phase" in run_it
    data = json.loads(report.read_text())
    assert data["settings"]["lock"] is True
    assert set(data["replay"][0]) == {"file", "fbcca", "trca", "locked"}
    # the synthetic session's response has one fixed phase per target: locked to it, TRCA
    # decodes every held-out trial at least as well as phase-free TRCA
    assert data["replay"][0]["locked"]["correct"] >= data["replay"][0]["trca"]["correct"]


# -- folds and leakage --------------------------------------------------------------------

def test_folds_are_per_target_rank_mod_the_smallest_count() -> None:
    y = np.array([0, 1] * 7 + [0, 0])                 # 9 and 7 trials
    fold, n_folds = train.assign_folds(y)
    assert n_folds == 7
    rank = np.array([np.count_nonzero(y[:i] == y[i]) for i in range(y.size)])
    np.testing.assert_array_equal(fold, rank % 7)
    for f in range(7):
        assert set(y[fold == f]) == {0, 1}            # every fold holds out both targets
    assert np.bincount(fold).tolist() == [3, 3, 2, 2, 2, 2, 2]


def _row_index(windows: np.ndarray) -> dict[bytes, int]:
    return {w.tobytes(): i for i, w in enumerate(windows)}


def test_no_held_out_trial_reaches_its_own_fold(small, monkeypatch, capsys) -> None:
    ts = train.load_trials([small])                  # the cuts main() makes
    n = ts.labels.size
    events: list[tuple] = []
    real_fit, real_base, real_win = TRCA.fit, CCA.fit_baseline, Decoder.windows

    def fit(self, X, y, info=None):
        events.append(("fit", self, np.array(X)))
        return real_fit(self, X, y, info=info)

    def fit_baseline(self, X):
        events.append(("baseline", self, np.array(X)))
        return real_base(self, X)

    def windows(self, X, starts=None):
        out = real_win(self, X, starts)
        events.append(("windows", self, (list(starts), out)))
        return out

    monkeypatch.setattr(TRCA, "fit", fit)
    monkeypatch.setattr(CCA, "fit_baseline", fit_baseline)
    monkeypatch.setattr(Decoder, "windows", windows)
    assert train.main([str(small), "--no-save", "--windows", "1,1.5"]) == 0
    assert "replay window 1.5 s" in capsys.readouterr().out

    # trials reaching each fit: the fold fits, then the final model on everything
    on_index = _row_index(ts.on)
    fits = [(ev[1], ev[2]) for ev in events if ev[0] == "fit"]
    trained_on = [{on_index[x.tobytes()] for x in X} for _, X in fits]
    assert trained_on[-1] == set(range(n))
    held = [set(range(n)) - s for s in trained_on[:-1]]
    assert all(held) and sum(len(h) for h in held) == n
    assert set().union(*held) == set(range(n))         # every trial held out exactly once

    # which fold each classifier belongs to: its TRCA fit, or the fit just before it
    fold_of: dict[int, int] = {}
    n_fits = 0
    for kind, obj, _ in events:
        if kind == "fit":
            n_fits += 1
            if n_fits <= len(held):
                fold_of[id(obj)] = n_fits - 1
        elif kind == "baseline":
            fold_of.setdefault(id(obj), n_fits - 1)

    # still windows the CV may use: each trial's still window cut every step
    n_step = int(round(0.25 * FS))
    still_cv: dict[bytes, int] = {}
    for w in (1.0, 1.5):
        n_w = int(round(w * FS))
        for i in range(n):
            for s in range(0, ts.off.shape[-1] - n_w + 1, n_step):
                still_cv[np.ascontiguousarray(ts.off[i][:, s:s + n_w]).tobytes()] = i

    # replay windows: segment start -> the trial whose still span holds the window; every
    # window (after the pad) lies wholly inside one flicker span or one still span
    rec = load_recording(small)
    spans = _trial_spans(rec, rec.latency)
    ends = train.still_ends(rec, spans)
    kept = {int(a): i for i, a in enumerate(ts.on_start)}
    replay_rows: dict[bytes, int] = {}
    for kind, dec, payload in events:
        if kind != "windows":
            continue
        starts, out = payload
        for s, row in zip(starts, out):
            first, last = s + dec.n_pad, s + dec.needed
            owner = None
            in_flicker = any(a <= first and last <= b for a, b, _ in spans)
            for k, (a, b, _) in enumerate(spans):
                if b <= first and last <= ends[k]:
                    owner = kept.get(int(a), -1)
            assert in_flicker != (owner is not None), \
                f"replay window {first}..{last} is not inside one flicker or still span"
            if owner is not None:                    # windows inside a flicker span are not
                replay_rows[row.tobytes()] = owner   # still windows: they map to nothing

    n_baselines = 0
    for kind, clf, X in events:
        if kind != "baseline":
            continue
        n_baselines += 1
        f = fold_of[id(clf)]
        for row in X:
            key = row.tobytes()
            owner = still_cv.get(key, replay_rows.get(key))
            assert owner is not None, "a baseline window that is not a known still window"
            assert owner >= 0 and owner not in held[f], \
                f"fold {f}: the still screen of held-out trial {owner} reached a baseline"
    # CV: 5 folds x 2 windows x 2 methods; replay: 5 folds x 2 methods
    assert n_baselines == 5 * 2 * 2 + 5 * 2


# -- still screens in the replay ----------------------------------------------------------

def _session(tmp_path: Path, name: str, labels, cut_s: float | None = None,
             drop_off: int | None = None, seed: int = 3) -> Path:
    """A fake session, optionally cut ``cut_s`` seconds into the last flicker, or with the
    stim_off of trial ``drop_off`` removed (an orphan stim_on, as after Esc)."""
    X, t, text = fake_session(FREQS2, labels, fs=FS, seed=seed)
    lines = text.splitlines()
    if drop_off is not None:
        offs = [i for i, ln in enumerate(lines) if "stim_off" in ln]
        del lines[offs[drop_off]]
    if cut_s is not None:
        on = [float(ln.split()[1].rstrip("]")) for ln in lines if "stim_on" in ln][-1]
        n = int(np.searchsorted(t, on + cut_s))
        X, t = X[:, :n], t[:n]
    return save_recording(tmp_path / name, X, t, FS, MONTAGE, FREQS2, "\n".join(lines) + "\n")


@pytest.mark.parametrize("how", ["cut", "orphan"])
def test_still_spans_end_at_the_next_flicker_of_any_trial(tmp_path, how) -> None:
    """A trial dropped as incomplete (the session stopped 3 s into its flicker) or unpaired
    (its stim_off is missing) still ends the previous trial's still screen."""
    labels = [0, 1, 1, 0, 0, 1]
    path = _session(tmp_path, f"2026-01-07_00000{len(how)}_cued.npz", labels,
                    cut_s=3.0 if how == "cut" else None, drop_off=3 if how == "orphan" else None)
    rec = load_recording(path)
    spans = _trial_spans(rec, rec.latency)
    assert len(spans) == 5                          # one trial dropped
    ons = sorted(int(np.searchsorted(rec.t, m[0] + rec.latency - 0.01 / FS))
                 for m in rec.markers if m[1] == "stim_on")
    assert len(ons) == 6
    ends = train.still_ends(rec, spans)
    for (a, b, _), end in zip(spans, ends):
        later = [o for o in ons if o > a]
        want = later[0] if later else min(b + int(round(rec.window_s * FS)), rec.X.shape[1])
        assert end == want and end > b
        for o in ons:                               # no flicker inside any still span
            assert not b <= o < end


@pytest.mark.parametrize("how", ["cut", "orphan"])
def test_replay_never_feeds_a_dropped_trials_flicker_as_still(tmp_path, monkeypatch, how) -> None:
    labels = _blocks(2, 5, seed=8)
    path = _session(tmp_path, f"2026-01-07_10000{len(how)}_cued.npz", labels,
                    cut_s=3.0 if how == "cut" else None, drop_off=6 if how == "orphan" else None)
    rec = load_recording(path)
    spans = _trial_spans(rec, rec.latency)
    assert len(spans) == len(labels) - 1
    n_stim = int(round(4.0 * FS))
    flicker = [(o, o + n_stim) for o in
               (int(np.searchsorted(rec.t, m[0] + rec.latency - 0.01 / FS))
                for m in rec.markers if m[1] == "stim_on")]
    seen: list[tuple[int, int]] = []
    real = Decoder.windows

    def windows(self, X, starts=None):
        seen.extend((int(s) + self.n_pad, int(s) + self.needed) for s in starts)
        return real(self, X, starts)

    monkeypatch.setattr(Decoder, "windows", windows)
    ts = train.load_trials([path])
    train.replay(ts, train.cross_validate(ts, [1.5], fusion=(0.0, 1.0)))
    for first, last in seen:
        if any(a <= first and last <= b for a, b, _ in spans):
            continue                                  # a kept trial's flicker window
        hit = [(o, e) for o, e in flicker if first < e and o < last]
        assert not hit, f"still window {first}..{last} overlaps the flicker at {hit}"


def test_last_still_span_lasts_one_trial(tmp_path) -> None:
    X, t, text = fake_session(FREQS2, [0, 1], fs=FS, seed=3)
    pad = np.repeat(X[:, -1:], int(30 * FS), axis=1)          # 30 s more after the session
    X2 = np.concatenate([X, pad], axis=1)
    t2 = t[0] + np.arange(X2.shape[1]) / FS
    rec = load_recording(save_recording(tmp_path / "2026-01-08_000000_cued.npz", X2, t2, FS,
                                        MONTAGE, FREQS2, text))
    spans = _trial_spans(rec, rec.latency)
    assert train.still_ends(rec, spans)[-1] == spans[-1][1] + int(round(rec.window_s * FS))


# -- replay counts ---------------------------------------------------------------------------

def test_tally_counts_each_outcome() -> None:
    c = train.ReplayCount()
    train._tally(c, 1, 1, ABSTAIN)          # right, still screen quiet
    train._tally(c, ABSTAIN, 1, 0)          # no commit, still screen committed
    train._tally(c, 0, 1, None)             # wrong, no still windows
    train._tally(c, 0, 0, 1)                # right, still screen committed
    assert (c.n, c.correct, c.wrong, c.none, c.still_n, c.still_commits) == (4, 2, 1, 1, 3, 2)


def test_replay_on_a_clear_session_commits_correctly(tmp_path) -> None:
    path = _write(tmp_path / "2026-01-09_000000_cued.npz", FREQS2, _blocks(2, 5, seed=5),
                  seed=5, amp=4.0)
    ts = train.load_trials([path])
    cv = train.cross_validate(ts, [1.5])
    counts = train.replay(ts, cv)[0]
    for m in ("fbcca", "trca"):
        c = counts[m]
        assert c.n == 10 and c.none >= 0 and c.wrong == 0 and c.correct >= 8, (m, vars(c))
        assert c.still_n == 10 and 0 <= c.still_commits <= c.still_n


# -- the cross-validation's numbers ------------------------------------------------------------

def test_score_subtracts_the_baseline_and_counts_kept_windows() -> None:
    class Stub:
        baseline_mean = np.array([0.0, 10.0])

        def raw_scores(self, X):
            return np.tile([1.0, 2.0], (len(X), 1))      # raw argmax 1, bc argmax 0

        def predict_scores(self, s):
            return np.where(np.arange(len(s)) == 0, ABSTAIN, s.argmax(axis=1))

    c, bc_ok = train._score(Stub(), np.zeros((3, 1, 5)), np.array([0, 0, 1]))
    assert (c.n, c.raw_hits, c.bc_hits, c.kept, c.kept_ok) == (3, 1, 2, 2, 1)
    np.testing.assert_array_equal(bc_ok, [True, True, False])


def test_cross_validate_cells_match_direct_scoring(faint) -> None:
    ts = train.load_trials([faint])
    cv = train.cross_validate(ts, [1.5])
    n, n_w, n_step = ts.labels.size, 375, int(round(0.25 * FS))
    starts = train._starts(ts.on.shape[-1], n_w, n_step)
    fb = FBCCA(ts.freqs, FS, phases=ts.first.phases)
    aligned = fb.raw_scores(ts.on[..., :n_w]).argmax(axis=1) == ts.labels
    sliding = fb.raw_scores(train._cut(ts.on, starts, n_w)).argmax(axis=1) \
        == np.repeat(ts.labels, len(starts))
    assert 0 < aligned.sum() < n and 0 < sliding.sum() < sliding.size  # not saturated
    a, sl = cv.cell("fbcca", 1.5, "aligned"), cv.cell("fbcca", 1.5, "sliding")
    assert (a.n, a.raw_hits) == (n, int(aligned.sum()))
    assert (sl.n, sl.raw_hits) == (sliding.size, int(sliding.sum()))
    for m in ("fbcca", "trca"):
        a, sl = cv.cell(m, 1.5, "aligned"), cv.cell(m, 1.5, "sliding")
        assert int(cv.per_trial[(m, 1.5)].sum()) == a.bc_hits
        assert cv.per_trial_sliding[(m, 1.5)].sum() * len(starts) == pytest.approx(sl.bc_hits)
        assert a.still_n == n and sl.still_n == sl.n


@pytest.mark.parametrize("pred, go", [(ABSTAIN, False), (0, True)])
def test_still_windows_count_what_the_rules_let_through(small, monkeypatch, pred, go) -> None:
    monkeypatch.setattr(CCA, "predict", lambda self, X: np.full(len(X), pred))
    cv = train.cross_validate(train.load_trials([small]), [1.5])
    for c in cv.cells.values():
        assert c.still_n > 0 and c.still_go == (c.still_n if go else 0)


def test_harmonics_reach_the_fold_models_and_the_saved_model(small, tmp_path) -> None:
    cv = train.cross_validate(train.load_trials([small]), [1.5], harmonics=2)
    assert all(f.trca.harmonics == 2 and f.fbcca.harmonics == 2 for f in cv.folds)
    out = tmp_path / "h2.npz"
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        assert train.main([str(small), "--harmonics", "2", "--windows", "1.5", "--no-replay",
                           "--out", str(out)]) == 0
    assert load_model(out).harmonics == 2 and "--harmonics 2" in buf.getvalue()


# -- the report and the verdict, on hand-made counts ---------------------------------------

def _hand_cv(K: int, n: int, per_window: int, tr_slide, fb_slide,
             aligned=((10, 12), (13, 14))) -> tuple[train.TrialSet, train.CVResult]:
    """A TrialSet / CVResult of n trials and K targets with given sliding fractions per
    trial (trca, fbcca); aligned (raw, bc) hits for fbcca then trca."""
    freqs = np.array([11.0, 15.0, 17.0, 13.0][:K])
    rec = SimpleNamespace(path=Path("x_cued.npz"), freqs=freqs, n_targets=K)
    labels = np.arange(n) % K
    ts = train.TrialSet([rec], np.empty(0), np.empty(0), labels, np.zeros(n, int),
                        np.zeros(n, int), tuple(POSTERIOR), np.bincount(labels, minlength=K))
    tr_slide, fb_slide = np.asarray(tr_slide, float), np.asarray(fb_slide, float)
    w = 1.5
    cells = {}
    for name, slide, (raw, bc) in (("fbcca", fb_slide, aligned[0]), ("trca", tr_slide, aligned[1])):
        hits = int(round(slide.sum() * per_window))
        cells[(name, w, "aligned")] = train.Cell(n=n, raw_hits=raw, bc_hits=bc, kept=8,
                                                 kept_ok=6, still_n=n, still_go=4)
        cells[(name, w, "sliding")] = train.Cell(n=n * per_window, raw_hits=hits,
                                                 bc_hits=hits, kept=n, kept_ok=n // 2,
                                                 still_n=n * per_window, still_go=0)
    folds = [train.Fold(0, np.arange(n), (1.0, 0.0), False, None, None)]
    per_trial = {("fbcca", w): np.arange(n) < aligned[0][1], ("trca", w): np.arange(n) < aligned[1][1]}
    cv = train.CVResult([w], 2, np.zeros(n, int), folds, cells, per_trial,
                        {("trca", w): tr_slide, ("fbcca", w): fb_slide})
    return ts, cv


def test_report_prints_the_exact_numbers() -> None:
    ts, cv = _hand_cv(2, 16, 10, [1.0] * 15 + [0.0], [0.2] * 8 + [1.0] * 8)
    lines, data = train.report_lines(ts, cv, 1.5, "cv")
    row = next(ln for ln in lines if ln.split()[:3] == ["1.5", "s", "aligned"])
    fb, tr, diff = row.split("|")[1:]
    assert fb.split() == ["62%", "75%", "50%", "75%", "25%"]       # 10, 12, 8, 6/8, 4 of 16
    assert tr.split() == ["81%", "88%", "50%", "75%", "25%"]
    assert diff.strip() == "+12.5"
    slide = next(ln for ln in lines if ln.split()[:3] == ["1.5", "s", "sliding"])
    assert slide.split("|")[-1].strip() == "+33.8"                 # 150 vs 96 of 160
    # trca aligned 14/16: 196 * sqrt(0.875 * 0.125 / 16) = 16.2
    assert next(ln for ln in lines if ln.startswith("noise:")).startswith(
        "noise: one trial = 6.2 points; 95% interval about ±16 points")
    mc = next(ln for ln in lines if ln.startswith("McNemar"))
    assert "p=0.50 (trca only 2, fbcca only 0)" in mc                # trials 12 and 13
    assert data["noise"]["ci95_points"] == pytest.approx(196 * np.sqrt(0.875 * 0.125 / 16))
    assert data["cells"][0]["bc_diff"] == pytest.approx(2 / 16)


@pytest.mark.parametrize("K, n, tr_slide, fb_slide, beats, reason", [
    # clearly better, significant per trial (8 vs 1 discordant, p = 0.04)
    (2, 16, [1.0] * 15 + [0.0], [0.2] * 8 + [1.0] * 7 + [1.0], True, None),
    # not significant per trial, but 15 points ahead with a ±11 point noise
    (2, 16, [0.95] * 16, [0.8] * 16, True, None),
    (2, 16, [0.6] * 16, [0.6] * 16, False, "not higher"),
    (2, 16, [0.6] * 16, [0.9] * 16, False, "not higher"),
    # above chance, 5 points ahead, within the ±21 point noise, p = 1
    (2, 16, [0.75] * 16, [0.7] * 16, False, "no reliable difference"),
    # pure noise: one window ahead of FBCCA, but below the 25% chance
    (4, 32, [74 / 320] * 32, [73 / 320] * 32, False, "not clearly above the 25% chance"),
    # above chance and ahead, but fewer than half the windows right in every trial: the
    # per-trial test has nothing to go on and 10 points is inside the ±15 point noise
    (4, 32, [0.45] * 32, [0.35] * 32, False, "no reliable difference"),
])
def test_verdict_needs_evidence(K, n, tr_slide, fb_slide, beats, reason) -> None:
    ts, cv = _hand_cv(K, n, 10, tr_slide, fb_slide)
    v = train.verdict(ts, cv, 1.5)
    assert v["trca_beats"] is beats
    x, y = np.mean(tr_slide), np.mean(fb_slide)
    assert v["trca_bc"] == pytest.approx(x) and v["fbcca_bc"] == pytest.approx(y)
    if beats:
        assert v["reason"] is None
        assert v["text"].startswith(f"trca beats fbcca on held-out windows: {100 * x:.0f}% vs "
                                    f"{100 * y:.0f}% (McNemar per trial p=")
    else:
        assert reason in v["reason"] and reason in v["text"]
        assert v["text"].startswith(f"trca does not beat fbcca on held-out windows "
                                    f"({100 * x:.0f}% vs {100 * y:.0f}%; ")
        assert v["text"].endswith("): keep --classifier fbcca")


def test_verdict_counts_trials_not_windows() -> None:
    ts, cv = _hand_cv(2, 16, 10, [1.0] * 15 + [0.0], [0.2] * 8 + [1.0] * 8)
    v = train.verdict(ts, cv, 1.5)
    # trials, by majority of their windows: trca right on 0-14, fbcca on 8-15
    assert (v["trca_only"], v["fbcca_only"]) == (8, 1)
    assert v["mcnemar_p"] == pytest.approx(train.mcnemar_p(8, 1)) and v["mcnemar_p"] < 0.05
    assert v["ci95_points"] == pytest.approx(196 * np.sqrt(0.9375 * 0.0625 / 16))


# -- the demo command ------------------------------------------------------------------------

def test_run_it_repeats_every_non_default_setting(small, tmp_path, capsys) -> None:
    out = tmp_path / "m o.npz"                                    # a space survives quoting
    assert train.main([str(small), "--channels", "O1,O2", "--harmonics", "2", "--windows", "1",
                       "--window", "1", "--votes", "3", "--margin", "0.3", "--threshold", "none",
                       "--step", "0.5", "--no-replay", "--n-comp", "1", "--out", str(out)]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("run it: "))
    argv = shlex.split(line[len("run it: "):])
    assert argv[:3] == ["python", "-m", "brainsync.demo"]
    from brainsync import demo
    args = demo.build_parser().parse_args(argv[3:])
    assert args.config == "two_targets" and args.classifier == "trca-phase"
    assert args.model == str(out)
    assert args.channels == ["O1", "O2"] and args.harmonics == 2 and args.names == list(MONTAGE)
    assert (args.window, args.step, args.votes, args.margin, args.threshold) == \
        (1.0, 0.5, 3, 0.3, None)


def test_run_it_with_defaults_adds_nothing(trained) -> None:
    _, out, model, _ = trained
    assert ("run it: python -m brainsync.demo -c four_targets --classifier trca-phase "
            f"--model {model}\n") in out


def test_run_it_names_another_montage(tmp_path, capsys) -> None:
    names = ["A1", "A2", "A3", "A4", "P7", "P8", "O1", "O2"]
    X, t, text = fake_session(FREQS2, _blocks(2, 4), fs=FS, seed=6)
    path = save_recording(tmp_path / "2026-01-10_000000_cued.npz", X, t, FS, names, FREQS2, text)
    assert train.main([str(path), "--windows", "1.5", "--no-replay",
                       "--out", str(tmp_path / "m.npz")]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("run it: "))
    assert "--names A1,A2,A3,A4,P7,P8,O1,O2" in line and "--channels" not in line


# -- errors --------------------------------------------------------------------------------

def test_too_few_trials_per_target_exits_1(tmp_path, capsys) -> None:
    path = _write(tmp_path / "2026-01-03_000000_cued.npz", FREQS4, _blocks(4, 3))
    assert train.main([str(path), "--no-save"]) == 1
    err = capsys.readouterr().err
    assert "error: target" in err and "has 3 kept trials; record at least 4 (8 recommended)" in err


def test_files_with_different_freqs_exit_1(small, tmp_path, capsys) -> None:
    other = _write(tmp_path / "2026-01-04_000000_cued.npz", [15.0, 19.0], _blocks(2, 5))
    assert train.main([str(small), str(other), "--no-save"]) == 1
    err = capsys.readouterr().err
    assert "freqs" in err and other.name in err


@pytest.mark.parametrize("name, value", [
    ("freqs", np.array([15.0, 19.0])), ("phases", np.array([0.0, 1.0])), ("fs", 256.0),
    ("ch_names", tuple(reversed(MONTAGE))), ("band", (3.0, 40.0)), ("notch", 50.0),
    ("latency", 0.2), ("window_s", 3.0)])
def test_every_shared_field_is_compared(small, name, value) -> None:
    rec = load_recording(small)
    assert train._same(rec, rec) is None
    diff = train._same(dataclasses.replace(rec, **{name: value}), rec)
    assert diff is not None and diff.startswith(f"{name} ")


def test_files_with_different_montages_exit_1(small, tmp_path, capsys) -> None:
    X, t, text = fake_session(FREQS2, _blocks(2, 5), fs=FS, seed=7)
    other = save_recording(tmp_path / "2026-01-11_000000_cued.npz", X, t, FS,
                           list(reversed(MONTAGE)), FREQS2, text)
    assert train.main([str(small), str(other), "--no-save"]) == 1
    err = capsys.readouterr().err
    assert "ch_names" in err and other.name in err


@pytest.mark.parametrize("cut", [0, 3000])
def test_damaged_recording_exits_1(small, tmp_path, capsys, cut) -> None:
    bad = tmp_path / "2026-01-12_000000_cued.npz"
    bad.write_bytes(small.read_bytes()[:cut])
    assert train.main([str(bad), "--no-save"]) == 1
    assert capsys.readouterr().err.startswith("error: ")


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="file permissions do not stop this user")
@pytest.mark.parametrize("flag", ["--out", "--json"])
def test_unwritable_output_exits_1_before_the_cross_validation(small, tmp_path, capsys,
                                                                flag) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        name = "m.npz" if flag == "--out" else "r.json"
        extra = [] if flag == "--out" else ["--no-save"]
        assert train.main([str(small), flag, str(locked / "sub" / name), *extra]) == 1
    finally:
        locked.chmod(0o700)
    err = capsys.readouterr().err
    assert err.startswith("error: cannot write to ") and "cross-validating" not in err


def test_out_under_a_file_exits_1(small, tmp_path, capsys) -> None:
    (tmp_path / "file").write_text("")
    assert train.main([str(small), "--out", str(tmp_path / "file" / "m.npz")]) == 1
    assert "error: cannot write to" in capsys.readouterr().err


def test_single_target_trials_file_exits_1(tmp_path, capsys) -> None:
    X, t, text = fake_session([15.0], [0] * 5, fs=FS)
    path = save_recording(tmp_path / "2026-01-05_000000_trials.npz", X, t, FS, MONTAGE,
                          [15.0], text)
    assert train.main([str(path), "--no-save"]) == 1
    assert "at least 2 targets" in capsys.readouterr().err


def test_missing_file_exits_1(tmp_path, capsys) -> None:
    assert train.main([str(tmp_path / "nope_cued.npz"), "--no-save"]) == 1
    assert "not found" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--fusion", "svm"], ["--fusion", "0:0"],
                                  ["--fusion", "1:-1"], ["--windows", "0"],
                                  ["--votes", "0"], ["--out", "model.txt"]])
def test_bad_options_exit_2(small, argv) -> None:
    with pytest.raises(SystemExit) as exc:
        train.main([str(small), *argv])
    assert exc.value.code == 2


@pytest.mark.parametrize("text, want", [("cv", "cv"), ("trca", (1.0, 0.0)),
                                        ("FBCCA", (0.0, 1.0)), ("1:0.3", (1.0, 0.3))])
def test_fusion_arg(text, want) -> None:
    assert train.fusion_arg(text) == want


def test_mcnemar_exact() -> None:
    assert train.mcnemar_p(0, 0) == 1.0
    assert train.mcnemar_p(5, 0) == pytest.approx(2 / 32)
    assert train.mcnemar_p(3, 3) == 1.0
