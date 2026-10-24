from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from brainsync import select_freqs as sf
from brainsync.classify import CCA, make
from brainsync.metrics import accuracy
from brainsync.recordings import (MONTAGE, POSTERIOR, Windows, list_recordings, load_recording,
                                  save_recording)
from brainsync.synthetic import SESSION_T0, fake_session

FS = 250.0
N_TRIALS = 12
FRONTAL = [MONTAGE.index(c) for c in MONTAGE if c not in POSTERIOR]


def make_run(directory, stamp: str, f0: float, amp: float = 2.0, seed: int = 0,
             rest_rhythm: float = 0.0, refresh: str = "60.00", n: int = N_TRIALS,
             rhythm_hz: float | None = None, frontal: tuple[float, float] | None = None):
    """Write a single-target fake session.

    ``rest_rhythm`` adds a continuous posterior wave (µV) at ``rhythm_hz`` (default f0);
    ``frontal`` (Hz, µV) adds a continuous wave on the non-posterior channels only.
    """
    X, t, text = fake_session([f0], [0] * n, fs=FS, amp=amp, seed=seed)
    if rest_rhythm:
        post = [MONTAGE.index(c) for c in POSTERIOR]
        hz = f0 if rhythm_hz is None else rhythm_hz
        X[post] += rest_rhythm * np.sin(2 * np.pi * hz * (t - SESSION_T0) + 0.3)
    if frontal:
        X[FRONTAL] += frontal[1] * np.sin(2 * np.pi * frontal[0] * (t - SESSION_T0))
    text = text.replace("measured refresh: 60.00 Hz", f"measured refresh: {refresh} Hz")
    return save_recording(directory / f"2026-10-23_{stamp}_trials.npz", X, t, FS, MONTAGE,
                          [f0], text, window_s=3.86, latency=0.14)


@pytest.fixture
def session_dir(tmp_path):
    """Usable 12, 15 and 17 Hz runs, a 10 Hz run riding on a resting rhythm, and extras."""
    make_run(tmp_path, "100000", 12.0, seed=7)
    make_run(tmp_path, "100100", 15.0, seed=2)
    make_run(tmp_path, "100200", 17.0, seed=1)
    make_run(tmp_path, "100300", 10.0, seed=4, rest_rhythm=3.0)
    make_run(tmp_path, "100400", 15.0, seed=5, refresh="120.02")
    make_run(tmp_path, "100500", 12.0, seed=6, amp=0.0)
    return tmp_path


def analyse_dir(directory, **kw):
    return [sf.analyse_run(load_recording(p), **kw) for p in list_recordings("trials", directory)]


# -- measures --------------------------------------------------------------------------

def test_coherent_snr_peaks_at_f0(rng):
    n = int(4 * FS)
    tt = np.arange(n) / FS
    x = np.sin(2 * np.pi * 15.0 * tt) + rng.standard_normal(n)
    on = x[None, None]
    assert sf.coherent_snr(on, FS, 15.0)[0] > 10.0
    assert abs(sf.coherent_snr(on, FS, 21.0)[0]) < 6.0


def test_peak_snr_db_uses_flanking_bins():
    grid = np.arange(0.0, 20.0, 0.5)                  # f0 = 10 Hz is bin 20
    power = np.ones((2, grid.size))
    power[:, 20] = 100.0
    power[:, [19, 21]] = 50.0                         # guard bins: ignored
    power[1, [17, 18, 22, 23]] = 4.0                  # flanks at guard=1, side=2
    snr = sf.peak_snr_db(power, grid, 10.1, guard=1, side=2)
    assert snr == pytest.approx([20.0, 10 * np.log10(25.0)])
    assert sf.peak_snr_db(np.ones(grid.size), grid, 10.0) == pytest.approx(0.0)
    # flanks past the end of the grid are dropped, not wrapped
    edge = np.ones(grid.size)
    edge[:3], edge[-4:] = 9.0, 3.0                    # wrapping would pull in the 9s
    assert sf.peak_snr_db(edge, grid, 19.5, guard=0, side=3) == pytest.approx(0.0)
    zero = np.zeros(grid.size)
    assert np.isnan(sf.peak_snr_db(zero, grid, 10.0))


def test_peak_snr_db_rejects_bad_input():
    grid = np.arange(0.0, 20.0, 0.5)
    with pytest.raises(ValueError, match="outside the spectrum"):
        sf.peak_snr_db(np.ones(grid.size), grid, 30.0)
    with pytest.raises(ValueError, match="does not match"):
        sf.peak_snr_db(np.ones(5), grid, 10.0)
    with pytest.raises(ValueError, match="guard"):
        sf.peak_snr_db(np.ones(grid.size), grid, 10.0, side=0)
    with pytest.raises(ValueError, match="no flanking bins"):
        sf.peak_snr_db(np.ones(2), np.array([0.0, 1.0]), 0.0, guard=1, side=1)


def test_coherent_average_cancels_unlocked_response(rng):
    n, trials = int(3.86 * FS), 20
    tt = np.arange(n) / FS
    locked = np.array([np.sin(2 * np.pi * 15 * tt) + 2 * rng.standard_normal(n)
                       for _ in range(trials)])[:, None]
    loose = np.array([np.sin(2 * np.pi * 15 * tt + rng.uniform(0, 2 * np.pi))
                      + 2 * rng.standard_normal(n) for _ in range(trials)])[:, None]
    assert sf.coherent_snr(locked, FS, 15.0)[0] > sf.coherent_snr(loose, FS, 15.0)[0] + 6.0


def test_phase_locking_value(rng):
    n, trials = int(3.86 * FS), 30
    tt = np.arange(n) / FS
    locked = np.stack([np.sin(2 * np.pi * 12 * tt) + rng.standard_normal(n) for _ in range(trials)])
    random = np.stack([np.sin(2 * np.pi * 12 * tt + rng.uniform(0, 2 * np.pi))
                       + rng.standard_normal(n) for _ in range(trials)])
    assert sf.phase_locking(locked[:, None], FS, 12.0)[0] > 0.9
    assert sf.phase_locking(random[:, None], FS, 12.0)[0] < 3 / np.sqrt(trials)


def test_phase_locking_uses_onset_lags(rng):
    n, trials, f0 = int(3.86 * FS), 20, 17.0
    lags = rng.uniform(0, 0.05, trials)            # window starts drift after stim_on
    tt = np.arange(n) / FS
    X = np.stack([np.sin(2 * np.pi * f0 * (lag + tt)) for lag in lags])[:, None]
    assert sf.phase_locking(X, FS, f0, lags)[0] > 0.99
    assert sf.phase_locking(X, FS, f0)[0] < 0.9
    with pytest.raises(ValueError, match="one lag per trial"):
        sf.phase_locking(X, FS, f0, lags[:3])


def test_onset_lags_follow_the_markers(session_dir, rng):
    rec = load_recording(session_dir / "2026-10-23_100200_trials.npz")
    ons = np.array([s for s, kind, _ in rec.markers if kind == "stim_on"])
    delay = rec.latency + rng.uniform(0.0, 1.5, ons.size)     # starts well past the latency
    starts = np.searchsorted(rec.t, ons + delay)
    lags = sf.onset_lags(rec, starts, rec.latency)
    assert lags == pytest.approx(rec.t[starts] - ons, abs=1e-9)
    assert np.ptp(lags) > 0.5
    assert sf.onset_lags(rec, np.array([], dtype=int), rec.latency).size == 0


def test_analyse_run_feeds_onset_lags_to_plv(session_dir, monkeypatch):
    seen = {}
    real = sf.phase_locking

    def spy(on, fs, f0, lag=None):
        seen["lag"] = lag
        return real(on, fs, f0, lag)

    monkeypatch.setattr(sf, "phase_locking", spy)
    rec = load_recording(session_dir / "2026-10-23_100200_trials.npz")
    r = sf.analyse_run(rec)
    assert seen["lag"] is not None and len(seen["lag"]) == r.n_clean
    assert seen["lag"] == pytest.approx(rec.latency, abs=1.0 / FS)


def test_rayleigh_p(rng):
    n = 30
    assert sf.rayleigh_p(1.0, n) < 1e-10
    assert sf.rayleigh_p(0.0, n) == pytest.approx(1.0)
    loose = np.abs(np.exp(1j * rng.uniform(0, 2 * np.pi, (2000, n))).mean(axis=1))
    p = np.array([sf.rayleigh_p(r, n) for r in loose])
    assert np.mean(p < 0.05) == pytest.approx(0.05, abs=0.02)   # calibrated under the null
    # the best of four channels needs the correction: four times the single-channel p
    assert sf.rayleigh_p(0.4, n, 4) == pytest.approx(4 * sf.rayleigh_p(0.4, n))
    assert sf.rayleigh_p(0.1, n, 4) == 1.0
    assert np.isnan(sf.rayleigh_p(float("nan"), n))
    with pytest.raises(ValueError, match="n >= 1"):
        sf.rayleigh_p(0.5, 0)


def test_run_id_and_refresh():
    assert sf.run_id("/x/2026-10-22_121914_trials.npz") == "2026-10-22_121914"
    assert sf.measured_refresh("a\nmeasured refresh: 119.86 Hz\n") == pytest.approx(119.86)
    assert sf.measured_refresh("no such line") is None


# -- verdict rule ----------------------------------------------------------------------

def test_judge_rejects_when_still_windows_win():
    status, reason, p_on, p_off = sf.judge(12, 13, 17, 1 / 3)
    assert status == sf.REJECTED and p_on < 0.05 and p_off < 0.05
    assert "still" in reason


def test_judge_usable_and_no_response():
    assert sf.judge(15, 4, 17, 1 / 3)[0] == sf.USABLE
    assert sf.judge(7, 8, 18, 1 / 3)[0] == sf.NO_RESPONSE
    assert sf.judge(0, 0, 0, 1 / 3)[0] == sf.NO_RESPONSE


# -- per-run analysis on fake sessions -------------------------------------------------

def test_analyse_run_classifies_fake_sessions(session_dir):
    by_freq = {(r.f0, r.status) for r in analyse_dir(session_dir)}
    assert (12.0, sf.USABLE) in by_freq
    assert (15.0, sf.USABLE) in by_freq
    assert (17.0, sf.USABLE) in by_freq
    assert (10.0, sf.REJECTED) in by_freq
    assert (15.0, sf.EXCLUDED) in by_freq
    assert (12.0, sf.NO_RESPONSE) in by_freq


def test_analyse_run_numbers(session_dir):
    rec = load_recording(session_dir / "2026-10-23_100200_trials.npz")
    r = sf.analyse_run(rec)
    assert r.run == "2026-10-23_100200" and r.f0 == 17.0
    assert r.controls == (14.0, 20.0) and r.chance == pytest.approx(1 / 3)
    assert r.n_total == N_TRIALS and 0 < r.n_clean <= N_TRIALS
    assert r.wins >= r.n_clean - 1 and r.p_wins < 1e-3
    assert r.wins + sum(r.control_wins) == r.n_clean
    assert r.still_wins + sum(r.control_still_wins) == r.n_clean
    assert r.snr_db > 10 and r.snr_channel in POSTERIOR
    assert r.plv > 0.8 and r.plv_p < 1e-3
    assert r.plv_p == pytest.approx(sf.rayleigh_p(r.plv, r.n_clean, len(POSTERIOR)))
    assert r.refresh == pytest.approx(60.0) and r.notes == ()


def test_analyse_run_reports_the_best_channel(session_dir):
    rec = load_recording(session_dir / "2026-10-23_100200_trials.npz")
    r = sf.analyse_run(rec)
    snr = sf.coherent_snr(r.windows.on, FS, r.f0)
    plv = sf.phase_locking(r.windows.on, FS, r.f0, sf.onset_lags(rec, r.windows.on_start,
                                                                 rec.latency))
    assert np.ptp(snr) > 0.5 and np.ptp(plv) > 0.0          # channels differ: min != max
    assert r.snr_db == pytest.approx(np.nanmax(snr))
    assert r.snr_channel == r.windows.ch_names[int(np.nanargmax(snr))]
    assert r.plv == pytest.approx(plv.max())
    assert r.plv_channel == r.windows.ch_names[int(np.argmax(plv))]


def test_analyse_run_uses_posterior_channels_only(tmp_path):
    # a strong wave at the f0+3 control on the frontal and central channels only
    path = make_run(tmp_path, "100700", 15.0, seed=2, frontal=(18.0, 20.0))
    rec = load_recording(path)
    r = sf.analyse_run(rec)
    assert r.windows.ch_names == POSTERIOR and r.windows.on.shape[1] == len(POSTERIOR)
    assert r.status == sf.USABLE and r.wins >= r.n_clean - 1
    assert r.snr_channel in POSTERIOR and r.plv_channel in POSTERIOR
    # the same run judged on every channel loses f0 to the frontal control
    everything = sf.analyse_run(rec, channels=MONTAGE)
    assert everything.control_wins[1] >= everything.n_clean - 1


def test_notes_flag_a_masking_control(tmp_path):
    # a large 9 Hz resting rhythm beats a phase-locked 12 Hz response in every window
    path = make_run(tmp_path, "100800", 12.0, seed=3, rest_rhythm=8.0, rhythm_hz=9.0)
    r = sf.analyse_run(load_recording(path))
    assert r.status == sf.NO_RESPONSE and r.plv_p < 0.05
    assert any("control 9 Hz" in note and "masks f0" in note for note in r.notes)
    assert any("phase-locked" in note for note in r.notes)
    plain = sf.analyse_run(load_recording(make_run(tmp_path, "100900", 12.0, seed=3)))
    assert plain.status == sf.USABLE and plain.notes == ()


def test_run_notes_ignore_the_1_over_f_bias():
    # the lower control winning only the still screen is the expected 1/f bias
    on, off = np.array([10, 1, 1]), np.array([2, 9, 1])
    assert sf.run_notes(15.0, (12.0, 18.0), on, off, 12, sf.USABLE, 0.9, "O1", 1e-6) == ()
    on = np.array([1, 10, 1])
    notes = sf.run_notes(15.0, (12.0, 18.0), on, off, 12, sf.NO_RESPONSE, 0.2, "O1", 0.5)
    assert len(notes) == 1 and "control 12 Hz" in notes[0]
    assert sf.run_notes(15.0, (12.0, 18.0), on, off, 0, sf.NO_RESPONSE, 0.9, "O1", 0.0) == ()


def test_rest_rhythm_wins_still_windows(session_dir):
    r = sf.analyse_run(load_recording(session_dir / "2026-10-23_100300_trials.npz"))
    assert r.still_wins >= r.n_clean - 1
    assert r.status == sf.REJECTED


def test_exclusion_and_control_tables(session_dir):
    rec = load_recording(session_dir / "2026-10-23_100100_trials.npz")
    assert sf.analyse_run(rec).status == sf.USABLE
    r = sf.analyse_run(rec, exclude={"2026-10-23_100100": "test reason"})
    assert (r.status, r.reason) == (sf.EXCLUDED, "test reason") and not r.eligible
    r = sf.analyse_run(rec, controls={"2026-10-23_100100": "eyes closed"})
    assert r.status == sf.CONTROL and not r.eligible
    promo = load_recording(session_dir / "2026-10-23_100400_trials.npz")
    assert "120.0 Hz" in sf.analyse_run(promo).reason
    assert sf.analyse_run(promo, display_hz=None).status == sf.USABLE


def test_analyse_run_rejects_bad_input(session_dir, tmp_path):
    rec = load_recording(session_dir / "2026-10-23_100100_trials.npz")
    with pytest.raises(ValueError, match="offsets"):
        sf.analyse_run(rec, offsets=(0.0, 3.0))
    X, t, text = fake_session([15.0, 17.0], [0, 1], fs=FS)
    cued = save_recording(tmp_path / "2026-10-23_110000_cued.npz", X, t, FS, MONTAGE,
                          [15.0, 17.0], text)
    with pytest.raises(ValueError, match="single-target"):
        sf.analyse_run(load_recording(cued))


# -- verdicts and pairs ----------------------------------------------------------------

def result(run: str, f0: float, status: str) -> sf.RunResult:
    return sf.RunResult(run=run, path=None, f0=f0, refresh=60.0, n_total=10, n_clean=10,
                        controls=(f0 - 3, f0 + 3), wins=5, p_wins=0.5, still_wins=2,
                        p_still=0.9, snr_db=0.0, snr_channel="O1", plv=0.3,
                        plv_channel="O1", status=status)


def test_frequency_verdict_rejection_beats_usable():
    runs = [result("a", 10.0, sf.USABLE), result("b", 10.0, sf.REJECTED),
            result("c", 12.0, sf.NO_RESPONSE), result("d", 12.0, sf.USABLE),
            result("e", 15.0, sf.NO_RESPONSE), result("f", 17.0, sf.REJECTED),
            result("g", 17.0, sf.EXCLUDED), result("h", 17.0, sf.CONTROL),
            result("i", 20.0, sf.EXCLUDED)]
    verdicts = sf.frequency_verdicts(runs)
    assert {f: s for f, (s, _) in verdicts.items()} == {
        10.0: sf.REJECTED, 12.0: sf.USABLE, 15.0: sf.NO_RESPONSE, 17.0: sf.REJECTED}
    assert "b" in verdicts[10.0][1] and "1/2 runs" in verdicts[12.0][1]
    assert sf.run_counts(runs, 12.0) == (1, 2) and sf.run_counts(runs, 17.0) == (0, 1)


def test_second_run_with_resting_rhythm_rejects_frequency(session_dir):
    make_run(session_dir, "100600", 10.0, seed=9)              # plain, usable 10 Hz run
    results = analyse_dir(session_dir)
    tens = {r.run[-6:]: r.status for r in results if r.f0 == 10.0}
    assert tens == {"100300": sf.REJECTED, "100600": sf.USABLE}
    assert sf.frequency_verdicts(results)[10.0][0] == sf.REJECTED


def test_frequency_verdicts_and_pairs(session_dir):
    results = analyse_dir(session_dir)
    verdicts = sf.frequency_verdicts(results)
    assert {f: s for f, (s, _) in verdicts.items()} == {
        10.0: sf.REJECTED, 12.0: sf.USABLE, 15.0: sf.USABLE, 17.0: sf.USABLE}
    tables = sf.pair_tables(results, verdicts)
    assert set(tables) == set(sf.POOLS) and sf.DEFAULT_POOL == "eligible"
    for pool in ("eligible", "usable"):
        pairs, multi = tables[pool]
        assert [p.freqs for p in pairs] == [(12.0, 15.0), (12.0, 17.0), (15.0, 17.0)]
        assert [m.method for m in multi] == ["cca", "fbcca"]
        assert all(m.freqs == (12.0, 15.0, 17.0) and m.pool == pool for m in multi)
        for p in pairs:
            assert p.chance == 0.5 and p.acc > 0.6 and p.p < 0.05 and p.pool == pool
    assert tables["original"] == ([], [])            # none of the original runs is here
    n12 = {r.run: len(r.windows) for r in results if r.f0 == 12.0}
    usable12 = tables["usable"][0][0]
    eligible12 = tables["eligible"][0][0]
    # eligible pooling keeps the silent 12 Hz run; usable pooling drops it
    assert usable12.per_class[0] == n12["2026-10-23_100000"]
    assert eligible12.per_class[0] == sum(n12.values())
    assert "2026-10-23_100500" in eligible12.runs and "2026-10-23_100500" not in usable12.runs
    assert sf.best_pair(tables["eligible"][0]) is not None


def test_original_pool_takes_the_listed_runs(session_dir, monkeypatch):
    monkeypatch.setattr(sf, "ORIGINAL_RUNS", ("2026-10-23_100500", "2026-10-23_100100",
                                              "2026-10-23_100200", "2026-10-23_100400"))
    results = analyse_dir(session_dir)
    runs = [r.run for r in sf.pooled_runs(results, [12.0, 15.0, 17.0], "original")]
    # the silent 12 Hz run counts although it failed its test; the ProMotion run never does
    assert runs == ["2026-10-23_100100", "2026-10-23_100200", "2026-10-23_100500"]
    pairs, multi = sf.pair_table(results, sf.frequency_verdicts(results), pool="original")
    assert [p.freqs for p in pairs] == [(12.0, 15.0), (12.0, 17.0), (15.0, 17.0)]
    assert len(multi) == 2
    monkeypatch.setattr(sf, "ORIGINAL_RUNS", ("2026-10-23_100100", "2026-10-23_100200"))
    pairs, multi = sf.pair_table(results, sf.frequency_verdicts(results), pool="original")
    assert [p.freqs for p in pairs] == [(15.0, 17.0)] and multi == []


def window_result(run: str, f0: float, on: np.ndarray, off: np.ndarray) -> sf.RunResult:
    n = len(on)
    w = Windows(on=on, off=off, labels=np.zeros(n, dtype=int), on_start=np.arange(n),
                off_start=np.arange(n), fs=FS, ch_names=POSTERIOR)
    return dataclasses.replace(result(run, f0, sf.USABLE), windows=w)


def rhythm_pool(rng, n_trials: int = 20, n: int = 500):
    """12 vs 15 Hz trials under a strong 12 Hz resting rhythm on two channels.

    The rhythm runs through flicker and still windows alike; the SSVEP sits on the other
    two channels. Without a still-screen baseline the 12 Hz template wins every window.
    """
    tt = np.arange(n) / FS

    def window(ssvep_hz: float | None) -> np.ndarray:
        x = rng.standard_normal((len(POSTERIOR), n))
        x[:2] += 1.0 * np.sin(2 * np.pi * 12.0 * tt + rng.uniform(0, 2 * np.pi))
        if ssvep_hz is not None:
            x[2:] += 0.8 * np.sin(2 * np.pi * ssvep_hz * tt + rng.uniform(0, 2 * np.pi))
        return x

    runs = []
    for k, f0 in enumerate((12.0, 15.0)):
        on = np.stack([window(f0) for _ in range(n_trials)])
        off = np.stack([window(None) for _ in range(n_trials)])
        runs.append(window_result(f"run{k}", f0, on, off))
    return runs


def test_pair_table_uses_the_still_window_baseline(rng):
    runs = rhythm_pool(rng)
    pr = sf.score_pool(runs, [12.0, 15.0])
    on, off, y, _, _ = sf.pool_windows(runs, [12.0, 15.0])
    raw = make("cca", freqs=(12.0, 15.0), fs=FS, harmonics=3).predict(on)
    assert accuracy(y, raw) <= 0.6                     # the rhythm votes 12 Hz everywhere
    assert pr.rest_low >= 0.9
    assert pr.acc >= 0.9 and pr.balanced >= 0.9


def test_pair_table_fits_the_baseline_on_still_windows(session_dir, monkeypatch):
    results = analyse_dir(session_dir)
    seen = []
    real = CCA.fit_baseline

    def spy(self, X_rest):
        seen.append(np.array(X_rest))
        return real(self, X_rest)

    monkeypatch.setattr(CCA, "fit_baseline", spy)
    for method in ("cca", "fbcca"):
        seen.clear()
        sf.score_pool(results, [15.0, 17.0], method)
        _, off, _, _, _ = sf.pool_windows(results, [15.0, 17.0])
        assert len(seen) == 1 and np.array_equal(seen[0], off)


def test_pool_follows_the_rule(session_dir):
    results = analyse_dir(session_dir)
    on, off, y, fs, runs = sf.pool_windows(results, [15.0, 17.0], "eligible")
    n15 = sum(len(r.windows) for r in results if r.f0 == 15.0 and r.eligible)
    assert int(np.sum(y == 0)) == n15 and on.shape == off.shape and fs == FS
    assert "2026-10-23_100400" not in runs                      # ProMotion run
    _, _, y12, _, _ = sf.pool_windows(results, [12.0, 15.0], "usable")
    assert int(np.sum(y12 == 0)) == len(results[0].windows)
    with pytest.raises(ValueError, match="no eligible windows"):
        sf.pool_windows(results, [20.0, 25.0])
    with pytest.raises(ValueError, match=r"no eligible windows at \[20.0\]"):
        sf.pool_windows(results, [15.0, 20.0])          # one class missing
    assert sf.covered(results, [15.0, 17.0], "eligible")
    assert not sf.covered(results, [15.0, 20.0], "eligible")
    with pytest.raises(ValueError, match="pool must be"):
        sf.pooled_runs(results, [15.0], "all")
    with pytest.raises(ValueError, match="two frequencies"):
        sf.score_pool(results, [15.0])


def test_pool_refuses_mixed_sampling_rates(session_dir):
    results = analyse_dir(session_dir)
    r15 = next(r for r in results if r.f0 == 15.0 and r.status == sf.USABLE)
    odd = dataclasses.replace(r15, windows=dataclasses.replace(r15.windows, fs=256.0))
    with pytest.raises(ValueError, match="different rates"):
        sf.pool_windows([*results, odd], [15.0, 17.0])


def test_ranking_stable():
    a = sf.PairResult((12.0, 17.0), "cca", 10, (5, 5), 0.9, 0.9, 0.01, 0.5)
    b = sf.PairResult((15.0, 17.0), "cca", 10, (5, 5), 0.8, 0.8, 0.01, 0.5)
    c = sf.PairResult((15.0, 17.0), "cca", 10, (5, 5), 0.7, 0.7, 0.01, 0.5)
    d = sf.PairResult((12.0, 17.0), "cca", 10, (5, 5), 0.6, 0.6, 0.01, 0.5)
    assert sf.ranking_stable({"usable": ([a, b], []), "eligible": ([a, b], [])})
    assert not sf.ranking_stable({"usable": ([a, b], []), "eligible": ([c, d], [])})
    assert sf.ranking_stable({"usable": ([a, b], []), "original": ([], [])})


def test_best_pair_ranks_on_balanced_then_acc():
    # regression: ranking on plain accuracy let a big class carry a pair
    a = sf.PairResult((12.0, 17.0), "cca", 10, (8, 2), 0.8, 0.5, 0.6, 0.5)
    b = sf.PairResult((15.0, 17.0), "cca", 10, (5, 5), 0.7, 0.7, 0.01, 0.5)
    c = sf.PairResult((12.0, 15.0), "cca", 10, (5, 5), 0.6, 0.7, 0.01, 0.5)
    assert sf.best_pair([a, b, c]) is b
    assert sf.ranked([a, b, c]) == [b, c, a]
    assert sf.best_pair([]) is None
    assert a.majority == 0.8 and a.chance == 0.5


def test_balanced_p_sees_through_a_majority_guess():
    y = np.array([0] * 40 + [1] * 10)
    guess = np.zeros(50, dtype=int)                  # 80% plain accuracy, no information
    assert sf.balanced_p(y, guess, n_perm=500) == 1.0
    good = y.copy()
    good[:4] = 1
    assert sf.balanced_p(y, good, n_perm=500) < 0.01
    assert sf.balanced_p(y, good, n_perm=500) == sf.balanced_p(y, good, n_perm=500)


def test_score_pool_reports_the_majority_rate(session_dir):
    results = analyse_dir(session_dir)
    pr = sf.score_pool(results, [12.0, 15.0])
    assert pr.majority == max(pr.per_class) / pr.n
    assert [len(c) for c in pr.correct] == list(pr.per_class)
    assert pr.balanced == pytest.approx(np.mean([c.mean() for c in pr.correct]))
    assert 0.0 <= pr.p_majority <= 1.0 and 0.0 < pr.p <= 1.0
    d = pr.as_dict()
    assert d["majority"] == pr.majority and "correct" not in d


def _pair_with(freqs, hits_per_class):
    correct = tuple(np.array(h, dtype=bool) for h in hits_per_class)
    n = sum(len(c) for c in correct)
    bal = float(np.mean([c.mean() for c in correct]))
    return sf.PairResult(tuple(freqs), "cca", n, tuple(len(c) for c in correct), bal, bal,
                         0.01, 0.5, correct=correct)


def test_lead_resolves_only_a_clear_gap():
    clear = sf.lead([_pair_with((12.0, 17.0), [[1] * 60, [1] * 60]),
                     _pair_with((15.0, 17.0), [[1] * 30 + [0] * 30, [1] * 60])])
    assert clear.best.freqs == (12.0, 17.0) and clear.diff == pytest.approx(0.25)
    assert clear.resolved and clear.lo > 0.15
    close = sf.lead([_pair_with((12.0, 17.0), [[1] * 8 + [0] * 2, [1] * 8 + [0] * 2]),
                     _pair_with((15.0, 17.0), [[1] * 7 + [0] * 3, [1] * 8 + [0] * 2])])
    assert close.diff == pytest.approx(0.05) and not close.resolved
    assert close.lo < 0 < close.hi
    assert close.as_dict()["resolved"] is False
    assert sf.lead([clear.best]) is None


def test_lead_resamples_the_shared_frequency_once():
    # both pairs share the 17 Hz windows: identical 17 Hz columns cancel in every resample
    shared = [1, 0] * 20
    a = _pair_with((12.0, 17.0), [[1] * 40, shared])
    b = _pair_with((15.0, 17.0), [[1] * 40, shared])
    ld = sf.lead([a, b])
    assert ld.lo == ld.hi == 0.0


def test_lead_refuses_mismatched_windows():
    a = _pair_with((12.0, 17.0), [[1] * 10, [1] * 10])
    b = _pair_with((15.0, 17.0), [[1] * 10, [1] * 12])
    with pytest.raises(ValueError, match="different windows at 17 Hz"):
        sf.lead([a, b])
    with pytest.raises(ValueError, match="no per-window"):
        sf.lead([sf.PairResult((12.0, 17.0), "cca", 10, (5, 5), 0.9, 0.9, 0.01, 0.5), a])


# -- CLI -------------------------------------------------------------------------------

def test_main_text_report(session_dir, capsys):
    assert sf.main(["--dir", str(session_dir)]) == 0
    out, err = capsys.readouterr()
    assert "analysing" in err and "analysing" not in out
    for stamp in ("100000", "100100", "100200", "100300", "100400", "100500"):
        assert f"2026-10-23_{stamp}" in out
    assert "Best pair (eligible runs):" in out and "Best pair (usable runs):" in out
    assert out.index("Best pair (eligible runs):") < out.index("Best pair (usable runs):")
    assert "pooling eligible runs" in out and "bal usable" in out and "bal original" in out
    assert "balanced accuracy" in out and "Lead over" in out
    assert "12/15/17" in out and "fbcca" in out
    assert "ProMotion" in out and "Rayleigh" in out


def test_main_json_and_keep_all(session_dir, capsys):
    assert sf.main(["--dir", str(session_dir), "--json", "--keep-all"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {r["status"] for r in data["runs"]} >= {sf.USABLE, sf.REJECTED}
    assert not any(r["status"] == sf.EXCLUDED for r in data["runs"])
    assert data["frequencies"]["10"]["status"] == sf.REJECTED
    assert data["frequencies"]["12"]["usable_runs"] == 1
    assert data["frequencies"]["12"]["eligible_runs"] == 2
    assert data["pool"] == "eligible" and set(data["by_pool"]) == set(sf.POOLS)
    assert data["pairs"] == data["by_pool"]["eligible"]["pairs"]
    assert "2026-10-23_100500" in data["pairs"][0]["runs"]
    assert len(data["best_pair"]) == 2 and isinstance(data["ranking_stable"], bool)
    assert all("plv_p" in r and "notes" in r for r in data["runs"])


def test_main_pool_option(session_dir, capsys):
    assert sf.main(["--dir", str(session_dir), "--json", "--pool", "usable"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pool"] == "usable"
    assert data["pairs"] == data["by_pool"]["usable"]["pairs"]
    assert "2026-10-23_100500" not in data["pairs"][0]["runs"]


def test_main_passes_pair_harmonics(session_dir, capsys, monkeypatch):
    seen = []

    def spy(name, **kw):
        seen.append(kw["harmonics"])
        return make(name, **kw)

    monkeypatch.setattr(sf, "make", spy)
    assert sf.main(["--dir", str(session_dir), "--json", "--pair-harmonics", "2"]) == 0
    capsys.readouterr()
    assert seen and set(seen) == {2}


def _no_nan(token):
    raise ValueError(f"non-JSON constant {token}")


def test_main_json_is_strict_without_clean_trials(session_dir, capsys):
    # a tiny rejection factor drops every trial, leaving SNR and PLV undefined
    assert sf.main(["--dir", str(session_dir), "--json", "--reject", "0.01"]) == 0
    data = json.loads(capsys.readouterr().out, parse_constant=_no_nan)
    assert all(r["n_clean"] == 0 for r in data["runs"])
    assert all(r["snr_db"] is None and r["plv"] is None for r in data["runs"])
    assert data["pairs"] == [] and data["best_pair"] is None


def test_main_files_and_offsets(session_dir, capsys):
    files = [str(session_dir / "2026-10-23_100100_trials.npz"),
             str(session_dir / "2026-10-23_100200_trials.npz")]
    assert sf.main([*files, "--offsets", "-2", "2", "4", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["runs"][0]["controls"] == [13.0, 17.0, 19.0]
    assert data["runs"][0]["chance"] == pytest.approx(0.25)
    assert [p["freqs"] for p in data["pairs"]] == [[15.0, 17.0]] and data["multi"] == []


def test_main_empty_dir(tmp_path, capsys):
    assert sf.main(["--dir", str(tmp_path)]) == 1
    assert "no *_trials.npz" in capsys.readouterr().err


# -- real data -------------------------------------------------------------------------

REAL_F0 = {"2026-10-20_095822": 10, "2026-10-20_101802": 10, "2026-10-22_114046": 15,
           "2026-10-22_115139": 15, "2026-10-22_121914": 15, "2026-10-22_123700": 15,
           "2026-10-22_145437": 12, "2026-10-22_145858": 10, "2026-10-22_150328": 17,
           "2026-10-22_150837": 12, "2026-10-22_151304": 12, "2026-10-22_154842": 12}


@pytest.mark.realdata(*[f"spectrum_checks/{rid}_trials.npz" for rid in REAL_F0])
@pytest.mark.slow
def test_realdata_reproduces_phase0(capsys):
    assert sf.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out, parse_constant=_no_nan)
    runs = {r["run"]: r for r in data["runs"]}
    assert {rid: r["f0"] for rid, r in runs.items()} == REAL_F0
    for rid in sf.KNOWN_EXCLUDED:
        assert runs[rid]["status"] == sf.EXCLUDED and "ProMotion" in runs[rid]["reason"]
    assert runs["2026-10-22_151304"]["status"] == sf.CONTROL
    assert runs["2026-10-22_151304"]["wins"] <= 2
    status = {f: v["status"] for f, v in data["frequencies"].items()}
    assert status == {"10": sf.REJECTED, "12": sf.USABLE, "15": sf.USABLE, "17": sf.USABLE}
    ten = runs["2026-10-22_145858"]
    assert ten["status"] == sf.REJECTED and ten["p_wins"] < 0.05 and ten["p_still"] < 0.05
    for rid in ("2026-10-22_121914", "2026-10-22_123700", "2026-10-22_150328",
                "2026-10-22_150837"):
        assert runs[rid]["status"] == sf.USABLE
    # headline: every eligible run pooled and ranked by balanced accuracy. 12 Hz holds 56
    # windows to 17 Hz's 17, so 12/17's plain accuracy is no better than always naming 12 Hz,
    # yet its balanced accuracy leads 15/17 by a gap the data cannot resolve.
    assert data["pool"] == "eligible"
    pairs = {tuple(p["freqs"]): p for p in data["pairs"]}
    p1217, p1517 = pairs[(12.0, 17.0)], pairs[(15.0, 17.0)]
    assert p1217["acc"] <= p1217["majority"] and p1217["p_majority"] > 0.5
    assert p1517["acc"] >= 0.75 and p1517["p_majority"] < 0.05
    assert all(p["chance"] == 0.5 and p["p"] < 0.01 for p in data["pairs"])
    assert data["best_pair"] == [12.0, 17.0] and p1217["balanced"] > p1517["balanced"]
    assert data["lead"]["runner_up"] == [15.0, 17.0] and data["lead"]["resolved"] is False
    assert data["ranking_stable"] is False
    for m in data["multi"]:
        assert m["balanced"] > 0.5 and m["chance"] == pytest.approx(1 / 3) and m["p"] < 0.01
    # the original analysis's three runs give back its numbers
    orig = data["by_pool"]["original"]
    assert orig["best_pair"] == [15.0, 17.0]
    assert {tuple(p["freqs"]): p["acc"] for p in orig["pairs"]}[(15.0, 17.0)] >= 0.8
    multi = {m["method"]: m["acc"] for m in orig["multi"]}
    assert multi["cca"] == pytest.approx(0.66, abs=0.03)
    assert multi["fbcca"] == pytest.approx(0.62, abs=0.03)
    assert data["frequencies"]["12"]["usable_runs"] == 1
    assert data["frequencies"]["12"]["eligible_runs"] == 3
    # 145437 fails the CCA test yet locks to the flicker: the report says so
    assert any("phase-locked" in n for n in runs["2026-10-22_145437"]["notes"])


def test_main_reports_errors_instead_of_tracebacks(tmp_path, capsys):
    # regression: a missing file or a bad --alpha ended in a Python traceback
    assert sf.main([str(tmp_path / "nonexistent_trials.npz")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "nonexistent_trials.npz" in err and "Traceback" not in err
    assert sf.main(["--alpha", "2", str(tmp_path / "x_trials.npz")]) == 2
    assert "error: --alpha must lie in (0, 1), got 2" in capsys.readouterr().err
