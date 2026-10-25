"""brainsync.brainmap core: electrode layout, per-window features and the rest baseline, the
frame stream, the board polling loop and recording replay. All synthetic, no hardware."""
from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from brainsync.board import ADC_FULL_SCALE_UV, FakeBoard
from brainsync.brainmap import features, layout
from brainsync.brainmap.features import BANDS, Baseline, window_features
from brainsync.brainmap.stream import LABEL, LOOP_LEVEL_S, MapStream, ReplayBoard, run
from brainsync.recordings import MONTAGE, load_recording, save_recording
from brainsync.synthetic import fake_session

FS = 250.0
ALPHA = list(BANDS).index("alpha")
OCC = [MONTAGE.index("O1"), MONTAGE.index("O2")]
OTHERS = [i for i in range(len(MONTAGE)) if i not in OCC]


def noise(seconds: float, seed: int = 0, sd: float = 8.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return sd * rng.standard_normal((len(MONTAGE), int(round(seconds * FS))))


def tone(X: np.ndarray, rows: list[int], hz: float, amp: float, start: int = 0) -> np.ndarray:
    X = X.copy()
    n = X.shape[1]
    X[rows, start:] += amp * np.sin(2 * np.pi * hz * np.arange(n - start) / FS)
    return X


def times(n: int, t0: float = 1_792_700_000.0) -> np.ndarray:
    return t0 + np.arange(n) / FS


def stream_all(X: np.ndarray, **kw) -> list[dict]:
    s = MapStream(FS, MONTAGE, **kw)
    return s.push(X, times(X.shape[1]))


# --- layout ---------------------------------------------------------------------------------

CONTRACT_SITES = {
    "Fp1": (-0.247, 0.761), "Fp2": (0.247, 0.761), "C3": (-0.4, 0.0), "C4": (0.4, 0.0),
    "P7": (-0.647, -0.470), "P8": (0.647, -0.470), "O1": (-0.247, -0.761), "O2": (0.247, -0.761),
}


def test_sites_match_the_10_20_geometry() -> None:
    assert set(MONTAGE) <= set(layout.SITES)
    for name, (x, y) in CONTRACT_SITES.items():
        assert layout.SITES[name] == pytest.approx((x, y), abs=1e-9)
    for x, y in layout.SITES.values():
        assert math.hypot(x, y) < 1.0
    # The EOG pads at the outer canthi (brainsync.eog): forward of the ears, mirrored,
    # just inside the head circle.
    (lx, ly), (rx, ry) = layout.SITES["LO"], layout.SITES["RO"]
    assert lx < layout.SITES["Fp1"][0] < 0 < rx and lx == -rx and ly == ry > 0
    assert math.hypot(rx, ry) == pytest.approx(0.95, abs=1e-3)   # coordinates are rounded


def test_positions_follow_the_given_order_and_reject_unknown_sites() -> None:
    pos = layout.positions(MONTAGE)
    assert list(pos) == list(MONTAGE)
    for name, xy in pos.items():
        assert xy == list(layout.SITES[name])
    assert layout.positions(["o1"]) == {"o1": list(layout.SITES["O1"])}  # case-insensitive
    with pytest.raises(ValueError, match="Cz"):
        layout.positions(["O1", "Cz"])


def test_regions_cover_the_montage() -> None:
    assert layout.REGIONS["O1"] == layout.REGIONS["O2"] == "visual"
    assert layout.REGIONS["Fp1"] == "prefrontal"
    assert layout.REGIONS["C3"] == "sensorimotor"
    assert layout.REGIONS["P8"] == "temporal-parietal"
    assert layout.REGIONS["LO"] == layout.REGIONS["RO"] == "eye (EOG)"
    assert layout.regions(MONTAGE) == {n: layout.REGIONS[n] for n in MONTAGE}
    with pytest.raises(ValueError):
        layout.regions(["T3"])


# --- features -------------------------------------------------------------------------------

def test_window_features_shapes_and_bins() -> None:
    f = window_features(noise(2.0), FS, ssvep_hz=(15.0, 17.0))
    assert f.db.shape == (len(BANDS), 8)
    assert f.snr.shape == (2, 8)
    assert f.quality == ["ok"] * 8
    assert f.ok.all()
    assert np.isfinite(f.db).all() and np.isfinite(f.snr).all()
    # white noise of 8 µV RMS: PSD 64 / 125 µV²/Hz, about -2.9 dB, in every band but delta
    # (the 1 Hz high-pass shaves the lowest bins)
    assert np.median(f.db[list(BANDS).index("beta")]) == pytest.approx(-2.9, abs=1.0)


def test_ssvep_tone_raises_its_own_snr_only() -> None:
    # one 2 s periodogram is noisy (the 17 Hz SNR alone can reach 10 dB by chance), so a
    # single window only has to rank the two; the averaged frames below show the margin
    X = tone(noise(2.0, seed=3), OCC, 15.0, 8.0)
    f = window_features(X, FS, ssvep_hz=(15.0, 17.0))
    assert (f.snr[0, OCC] > f.snr[1, OCC]).all()
    assert (f.snr[0, OCC] > 6.0).all()


@pytest.mark.parametrize("make, expect", [
    (lambda X: X * 0.0, "flat"),
    (lambda X: X * 0.0 + ADC_FULL_SCALE_UV, "clipped"),               # railed
    (lambda X: np.where(np.arange(X.size) % 97 == 0, -ADC_FULL_SCALE_UV, X), "clipped"),
    (lambda X: X * 20.0, "noisy"),                                     # 160 µV RMS
    (lambda X: X + 0.6 * ADC_FULL_SCALE_UV, "offset"),
    (lambda X: np.where(np.arange(X.size) == 10, np.nan, X), "invalid"),
])
def test_bad_contacts_are_flagged(make, expect) -> None:
    X = noise(2.0, seed=4)
    X[2] = make(X[2])
    f = window_features(X, FS)
    assert f.quality[2] == expect and expect in features.QUALITIES
    assert not f.ok[2]
    assert f.quality[:2] + f.quality[3:] == ["ok"] * 7
    assert np.isfinite(f.db).all() and np.isfinite(f.snr).all()


def test_window_features_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        window_features(noise(2.0)[0], FS)
    with pytest.raises(ValueError, match="reference"):
        window_features(noise(0.5), FS)   # 2 Hz bins: no SNR reference


def frames_and_features(X: np.ndarray, chunk: int = 25, **kw) -> tuple[list[dict], list]:
    """Push ``X`` in small chunks and keep each frame's WindowFeatures (``MapStream.last``)."""
    s = MapStream(FS, MONTAGE, **kw)
    t = times(X.shape[1])
    frames, feats = [], []
    for i in range(0, X.shape[1], chunk):
        for f in s.push(X[:, i:i + chunk], t[i:i + chunk]):
            frames.append(f)
            feats.append(s.last)
    return frames, feats


def test_mains_hum_does_not_leak_into_delta_or_rms() -> None:
    # review finding: filtering each 2 s window on its own rang at both ends, turning a big
    # 60 Hz hum into fake delta (+17..25 dB here) and a 30 µV RMS
    X = noise(8.0, seed=16, sd=5.0)
    hum = X + 200.0 * np.sin(2 * np.pi * 60.0 * np.arange(X.shape[1]) / FS)
    quiet, _ = frames_and_features(X, baseline_s=2.0)
    loud, feats = frames_and_features(hum, baseline_s=2.0)
    settled = slice(4, None)                    # windows starting once the filters settled
    d = (np.array([f["db"]["delta"] for f in loud[settled]])
         - np.array([f["db"]["delta"] for f in quiet[settled]]))
    assert np.abs(d).max() < 1.0
    assert max(float(f.rms.max()) for f in feats[settled]) < 10.0
    assert all(q == "ok" for f in loud for q in f["quality"])
    alone = window_features(hum[:, :500], FS)  # the old per-window path, for contrast
    assert (alone.db[list(BANDS).index("delta")] - window_features(X[:, :500], FS).db[0]).min() > 10


def test_a_large_dc_offset_does_not_kick_the_causal_filter() -> None:
    X = noise(4.0, seed=17)
    a = stream_all(X, baseline_s=1.0)
    b = stream_all(X + 60_000.0, baseline_s=1.0)  # 60 mV, under the DC limit
    for fa, fb in zip(a, b):
        np.testing.assert_allclose(fb["db"]["delta"], fa["db"]["delta"], atol=0.05)


def test_causal_filter_is_chunk_independent_and_survives_nan() -> None:
    X = noise(3.0, seed=18) + 30_000.0
    X[3, 100:110] = np.nan
    X[5, 200] = ADC_FULL_SCALE_UV                # a rail spike is held over like NaN
    whole = features.CausalFilter(FS, 8)(X)
    f = features.CausalFilter(FS, 8)
    parts = [f(X[:, i:i + n]) for i, n in ((0, 1), (1, 104), (105, 3), (108, 642))]
    np.testing.assert_array_equal(np.concatenate(parts, axis=1), whole)
    assert np.isfinite(whole).all()
    assert np.abs(whole[:, 500:]).max() < 60.0   # the 30 mV DC never reached the output
    with pytest.raises(ValueError):
        f(np.zeros((7, 3)))


def test_baseline_z_scores_against_the_rest_period_with_robust_stats() -> None:
    b = Baseline(1.0, n_channels=2, n_bands=1, min_windows=3)
    rng = np.random.default_rng(0)
    vals = 10.0 + 2.0 * rng.standard_normal((5, 1, 2))
    vals[2, 0, 1] = 60.0                          # one wild window: the median ignores it
    for k, v in enumerate(vals):
        assert b.state == "collecting"
        assert np.isnan(b.z(v)).all()
        b.add(v, np.array([True, True]), elapsed_s=0.25 * k)
    assert b.state == "ready" and b.ready().all()
    assert b.status() == {"state": "ready", "elapsed_s": 1.0, "seconds": 1.0}
    med = np.median(vals, axis=0)
    sd = np.maximum(1.4826 * np.median(np.abs(vals - med), axis=0), features.STD_FLOOR_DB)
    np.testing.assert_allclose(b.stats()[0], med)
    np.testing.assert_allclose(b.z(med + sd), 1.0, rtol=1e-9)
    assert abs(b.stats()[0][0, 1] - 10.0) < 4.0   # the mean would sit near 20
    b.add(np.full((1, 2), 99.0), np.array([True, True]), elapsed_s=2.0)  # frozen once ready
    np.testing.assert_allclose(b.z(med + sd), 1.0, rtol=1e-9)


def test_baseline_std_floor_and_never_good_channels() -> None:
    b = Baseline(0.5, n_channels=2, n_bands=1, min_windows=2, std_floor_db=0.5)
    for k in range(3):
        b.add(np.array([[3.0, 3.0]]), np.array([True, False]), elapsed_s=0.25 * k)
    assert b.state == "ready"
    z = b.z(np.array([[4.0, 4.0]]))
    assert z[0, 0] == pytest.approx(2.0)          # constant baseline: std floored at 0.5 dB
    assert np.isnan(z[0, 1])                      # never good during the rest period


def test_a_late_channel_needs_a_full_baseline_of_its_own() -> None:
    # review finding: a channel that turned good after the rest period got a baseline from
    # its next 4 windows, 1 s that still overlapped its bad windows
    b = Baseline(2.0, n_channels=2, n_bands=1, hop_s=0.25, window_s=0.5, min_windows=2)
    assert b.full_windows == 8
    rng = np.random.default_rng(1)
    seq, first_z = 0, None
    while first_z is None and seq < 100:
        elapsed = 0.25 * seq
        ok = np.array([True, elapsed >= 3.0 or 1.0 <= elapsed < 1.5])   # bad, good, bad, good
        db = np.array([[3.0, 3.0]]) + 0.1 * rng.standard_normal((1, 2))
        b.add(db, ok, elapsed_s=elapsed)
        if seq == 8:
            assert b.ready()[0] and not b.ready()[1]
        if np.isfinite(b.z(db)[0, 1]):
            first_z = elapsed
        seq += 1
    # the last bad window ends at 2.75 s; windows count once they start after it (3.25 s
    # on); 8 of them reach 5.0 s
    assert first_z == pytest.approx(5.0)
    # a channel bad only at the very start is late too
    b = Baseline(1.0, n_channels=1, n_bands=1, hop_s=0.25, window_s=0.5, min_windows=2)
    for seq in range(20):
        b.add(np.array([[1.0]]), np.array([seq > 0]), elapsed_s=0.25 * seq)
        assert b.ready()[0] == (seq >= 5), seq    # windows 2..5 make 4 = 1.0 s / 0.25 s


def test_a_late_channel_in_the_stream_stays_null_until_its_own_baseline() -> None:
    X = noise(20.0, seed=12)
    fp1 = MONTAGE.index("Fp1")
    X[fp1, :int(6.0 * FS)] = 0.0                  # electrode seated 6 s in
    frames = stream_all(X, baseline_s=3.0)
    bad = [f["seq"] for f in frames if f["quality"][fp1] != "ok"]
    assert bad and frames[13]["baseline"]["state"] == "ready"
    alpha = [f["z"]["alpha"][fp1] for f in frames]
    first = next(k for k, v in enumerate(alpha) if v is not None)
    # 8 windows (window_s) after the last bad one, then 12 (3 s / 0.25 s) windows
    assert first == max(bad) + 8 + 12 - 1
    others = [f["z"]["alpha"][1] for f in frames]
    assert others[13] is not None                 # the rest kept their on-time baseline


def test_settling_filters_keep_the_first_second_out_of_the_baseline() -> None:
    frames = stream_all(noise(4.0, seed=13), baseline_s=1.0)
    first = next(f["seq"] for f in frames if f["z"]["alpha"][0] is not None)
    # ready at seq 4 (1 s), but only windows starting at 1 s on count: seq 4..7 are the 4
    assert first == 7


def test_blinks_stay_out_of_the_baseline() -> None:
    rest = 30.0
    clean = noise(2.0 + rest + 2.0, seed=14)
    blinks = clean.copy()
    w = int(0.3 * FS)
    shape = 250.0 * np.sin(np.pi * np.arange(w) / w)
    for s0 in np.arange(2.7, 2.0 + rest + 2.0, 4.0):     # a hard blink every 4 s
        i = int(s0 * FS)
        blinks[:2, i:i + w] += shape                       # Fp1, Fp2
        blinks[2:4, i:i + w] += 0.1 * shape                # C3, C4 see a little of it
    got = {}
    for name, X, kw in (("clean", clean, {}), ("blinks", blinks, {}),
                        ("unguarded", blinks, dict(artifact_ptp_uv=1e9, outlier_mads=1e9))):
        s = MapStream(FS, MONTAGE, baseline_s=rest)
        if kw:
            s.baseline = Baseline(rest, 8, len(BANDS), **kw)
        s.push(X, times(X.shape[1]))
        got[name] = s.baseline
    delta = list(BANDS).index("delta")
    med = {k: b.stats()[0][delta, :4] for k, b in got.items()}
    sd = {k: b.stats()[1][delta, :4] for k, b in got.items()}
    assert np.abs(med["blinks"] - med["clean"]).max() < 2.0
    assert sd["blinks"].max() < 2.0 * sd["clean"].max()
    assert (med["unguarded"][:2] - med["clean"][:2]).min() > 10.0   # what it guards against
    assert (got["blinks"].rejected > 0).all()             # the C3 share too: any channel
    assert got["blinks"].ready().all()


def test_baseline_rejects_running_median_outliers_and_large_channels_do_not_block() -> None:
    b = Baseline(10.0, n_channels=3, n_bands=1, min_windows=2)
    rng = np.random.default_rng(2)
    ok = np.ones(3, bool)
    for k in range(30):
        db = 10.0 + 0.3 * rng.standard_normal((1, 3))
        if k == 29:
            db[0, 1] = 30.0                                 # a muscle burst on channel 1
        # channel 2 is a big site, over the limit in every window: it must not reject the
        # other channels' windows, or nobody would ever get a baseline
        b.add(db, ok, elapsed_s=0.25 * k, ptp=np.array([40.0, 40.0, 400.0]))
    # channel 2 blocks the others only until it counts as chronic (8 windows) plus hold-off
    assert b.rejected[0] <= 8 + b.holdoff_windows
    assert b.rejected[1] == b.rejected[0] + 1             # plus the burst, by the median test
    assert b.rejected[2] == 30
    for k in range(30, 41):
        b.add(10.0 + np.zeros((1, 3)), ok, elapsed_s=0.25 * k, ptp=np.array([40.0, 40.0, 40.0]))
    assert b.ready()[:2].all()
    assert abs(b.stats()[0][0, 1] - 10.0) < 0.5


def test_baseline_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        Baseline(-1.0, n_channels=8)
    with pytest.raises(ValueError):
        Baseline(1.0, n_channels=8, std_floor_db=0.0)
    with pytest.raises(ValueError):
        Baseline(1.0, n_channels=2, n_bands=1).add(np.zeros((1, 3)), np.ones(3, bool), 0.0)
    with pytest.raises(ValueError):
        Baseline(1.0, n_channels=2, hop_s=0.0)
    with pytest.raises(ValueError):
        Baseline(1.0, n_channels=2, n_bands=1).add(np.zeros((1, 2)), np.ones(2, bool), 0.0,
                                                    ptp=np.zeros(3))


def test_alpha_on_o1_o2_lights_only_o1_o2() -> None:
    rest, task = 30.0, 16.0
    X = noise(2.0 + rest + task, seed=1)
    X = tone(X, OCC, 10.0, 5.0, start=int((2.0 + rest) * FS))
    frames = stream_all(X, baseline_s=rest)
    late = [f for f in frames if f["t"] >= times(X.shape[1])[-1] - 12.0]
    assert len(late) >= 45
    z = np.array([f["z"]["alpha"] for f in late], dtype=float)
    assert (z[:, OCC] > 2.0).all()
    assert np.abs(z[:, OTHERS].mean(axis=0)).max() < 1.5
    for f in frames:
        json.dumps(f, allow_nan=False)


def test_ssvep_snr_uses_bins_away_from_the_other_target() -> None:
    freqs = np.arange(0.0, 125.5, 0.5)
    (k15, ring15), (k17, ring17) = features.snr_bins(freqs, (15.0, 17.0))
    assert freqs[k15] == 15.0 and freqs[k17] == 17.0
    assert list(freqs[ring15]) == [13.0, 13.5, 14.0, 16.0]      # 2 Hz span, 1 guard bin,
    assert list(freqs[ring17]) == [16.0, 18.0, 18.5, 19.0]      # and not next to 15 or 17


def test_noise_only_ssvep_snr_rarely_reaches_6_db() -> None:
    # review finding: one 2 s periodogram gives noise-only SNRs of 7 dB at p95 and 10 dB at
    # p99 (measured over 200 seeds), so "> 6 dB is an SSVEP" fired by chance on one channel
    # frame in 13. The PSD averaged over 4 s of hops: p50 -0.3, p95 3.6, p99 5.0 dB
    averaged, single = [], []
    for seed in range(12):
        X = noise(20.0, seed=100 + seed)
        frames = stream_all(X, baseline_s=1.0)
        averaged += [v for f in frames[16::4] for k in ("15", "17") for v in f["ssvep"][k]]
        single += [float(v) for j in range(0, X.shape[1] - 500, 500)
                   for v in window_features(X[:, j:j + 500], FS).snr.ravel()]
    averaged, single = np.array(averaged), np.array(single)
    assert np.percentile(averaged, 95) < 4.5 and np.percentile(averaged, 99) < 6.0
    assert np.percentile(single, 95) > np.percentile(averaged, 95) + 2.0
    assert abs(np.median(averaged)) < 1.0


def test_ssvep_average_restarts_after_a_bad_window() -> None:
    X = tone(noise(10.0, seed=19), OCC, 15.0, 4.0)
    o1 = OCC[0]
    X[o1, int(6.0 * FS)] = ADC_FULL_SCALE_UV            # one sample on the rail
    frames, feats = frames_and_features(X, baseline_s=1.0)
    bad = [k for k, f in enumerate(frames) if f["quality"][o1] != "ok"]
    assert bad == list(range(bad[0], bad[-1] + 1)) and len(bad) == 8
    single = [round(float(f.snr[0, o1]), 2) for f in feats]
    got = [f["ssvep"]["15"][o1] for f in frames]
    assert got[bad[0] - 1] != single[bad[0] - 1]        # averaged before ...
    assert got[bad[-1] + 1] == single[bad[-1] + 1]      # ... the clipped second is dropped
    assert got[bad[-1] + 2] != single[bad[-1] + 2]      # and a new average starts
    assert got[-1] > 6.0 and got[-1] > frames[-1]["ssvep"]["17"][o1] + 6.0


def test_ssvep_15_beats_17_in_frames() -> None:
    X = tone(noise(6.0, seed=2), OCC, 15.0, 4.0)
    frames = stream_all(X, baseline_s=1.0)
    s15 = np.array([f["ssvep"]["15"] for f in frames])
    s17 = np.array([f["ssvep"]["17"] for f in frames])
    assert (s15[:, OCC].mean(axis=0) > s17[:, OCC].mean(axis=0) + 6.0).all()


def test_flagged_channels_and_the_collecting_baseline_give_null_z() -> None:
    X = noise(6.0, seed=5)
    X[MONTAGE.index("Fp1")] = 0.0
    X[MONTAGE.index("C4")] = ADC_FULL_SCALE_UV
    frames = stream_all(X, baseline_s=2.0)
    collecting = [f for f in frames if f["baseline"]["state"] == "collecting"]
    ready = [f for f in frames if f["baseline"]["state"] == "ready"]
    assert collecting and ready
    for f in collecting:
        assert all(v is None for band in f["z"].values() for v in band)
    for f in ready:
        assert f["quality"][0] == "flat" and f["quality"][3] == "clipped"
        for band in f["z"].values():
            assert band[0] is None and band[3] is None
            assert all(isinstance(v, float) for i, v in enumerate(band) if i not in (0, 3))
    for f in frames:
        text = json.dumps(f, allow_nan=False)
        assert "NaN" not in text and "Infinity" not in text
        assert all(isinstance(v, float) for band in f["db"].values() for v in band)


# --- stream ---------------------------------------------------------------------------------

def test_hello_message() -> None:
    s = MapStream(FS, MONTAGE, baseline_s=30.0, source="cerelog")
    h = s.hello()
    json.dumps(h, allow_nan=False)
    assert h["type"] == "hello" and h["format"] == 1 and h["source"] == "cerelog"
    assert h["channels"] == list(MONTAGE)
    assert h["positions"] == layout.positions(MONTAGE)
    assert h["regions"] == layout.regions(MONTAGE)
    assert h["bands"] == {"delta": [1.0, 4.0], "theta": [4.0, 8.0], "alpha": [8.0, 13.0],
                          "beta": [13.0, 30.0]}
    assert h["ssvep_hz"] == [15.0, 17.0]
    assert h["ssvep_keys"] == ["15", "17"]
    assert (h["fs"], h["window_s"], h["hop_s"], h["baseline_s"]) == (250.0, 2.0, 0.25, 30.0)
    assert h["label"] == LABEL == "Your EEG · 8 electrodes · scalp map, not a brain scan"


def test_hello_ssvep_keys_are_the_frame_keys_in_order() -> None:
    s = MapStream(FS, MONTAGE, baseline_s=0.0, ssvep_hz=(12.0, 8.57, 15.5))
    keys = s.hello()["ssvep_keys"]
    assert keys == ["12", "8.57", "15.5"]
    f = s.push(noise(2.0), times(500))[0]
    assert list(f["ssvep"]) == keys


def test_push_in_odd_chunks_gives_one_frame_per_hop() -> None:
    X = noise(5.0, seed=6)
    t = times(X.shape[1])
    s = MapStream(FS, MONTAGE, baseline_s=1.0)
    frames, i, sizes = [], 0, [1, 7, 499, 13, 62, 63, 250, 333, 2]
    k = 0
    while i < X.shape[1]:
        n = sizes[k % len(sizes)]
        frames += s.push(X[:, i:i + n], t[i:i + n])
        i, k = i + n, k + 1
    # 2 s window, then one frame every 62.5 samples: 1 + 3 s * 4
    assert len(frames) == 13
    assert [f["seq"] for f in frames] == list(range(13))
    ends = [500 + int(math.floor(k * 62.5 + 0.5)) - 1 for k in range(13)]
    assert [f["t"] for f in frames] == [float(t[e]) for e in ends]
    whole = stream_all(X, baseline_s=1.0)
    assert [json.dumps(f) for f in frames] == [json.dumps(f) for f in whole]
    assert s.push(np.empty((8, 0)), np.empty(0)) == []


def test_baseline_goes_collecting_to_ready_after_baseline_s() -> None:
    frames = stream_all(noise(6.0, seed=7), baseline_s=3.0)
    states = [f["baseline"]["state"] for f in frames]
    first_ready = states.index("ready")
    assert set(states[:first_ready]) == {"collecting"} and set(states[first_ready:]) == {"ready"}
    assert frames[first_ready]["baseline"]["elapsed_s"] == pytest.approx(3.0)
    assert frames[first_ready - 1]["baseline"]["elapsed_s"] == pytest.approx(2.75)
    assert frames[0]["baseline"] == {"state": "collecting", "elapsed_s": 0.0, "seconds": 3.0}
    assert frames[-1]["baseline"]["elapsed_s"] == 3.0


def test_frame_message_shape() -> None:
    f = stream_all(noise(2.0), baseline_s=0.0)[0]
    assert f["type"] == "frame"
    assert set(f) == {"type", "seq", "t", "z", "db", "ssvep", "quality", "baseline"}
    assert set(f["z"]) == set(f["db"]) == set(BANDS)
    assert set(f["ssvep"]) == {"15", "17"}
    assert all(len(v) == 8 for v in (*f["z"].values(), *f["db"].values(), *f["ssvep"].values(),
                                     f["quality"]))


def test_mapstream_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        MapStream(FS, MONTAGE[:7] + ("Cz",))
    with pytest.raises(ValueError):
        MapStream(FS, MONTAGE, window_s=0.5)          # bins too coarse for an SNR
    with pytest.raises(ValueError):
        MapStream(FS, MONTAGE, smooth=1.0)
    with pytest.raises(ValueError):
        MapStream(FS, MONTAGE, ssvep_hz=(130.0,))
    with pytest.raises(ValueError):
        MapStream(FS, MONTAGE, hop_s=0.0)
    s = MapStream(FS, MONTAGE)
    with pytest.raises(ValueError):
        s.push(np.zeros((7, 10)), np.zeros(10))
    with pytest.raises(ValueError):
        s.push(np.zeros((8, 10)), np.zeros(9))


def test_frames_are_cheap() -> None:
    X = noise(27.0, seed=8)
    s = MapStream(FS, MONTAGE, baseline_s=5.0)
    began = time.perf_counter()
    frames = s.push(X, times(X.shape[1]))
    per_frame = (time.perf_counter() - began) / len(frames)
    assert len(frames) == 101
    assert per_frame < 0.02, f"{per_frame * 1000:.1f} ms per frame"


# --- run ------------------------------------------------------------------------------------

def collect():
    out: list[dict] = []
    return out, out.append


def test_run_with_a_virtual_fake_board() -> None:
    board = FakeBoard(freqs=(10.0, 15.0, 17.0), switch_s=10.0)
    s = MapStream(board.fs, board.ch_names, baseline_s=1.0)
    out, emit = collect()
    why = run(board, s, emit, stop=lambda: board.elapsed >= 4.0)
    assert why == "stopped"
    statuses = [m for m in out if m["type"] == "status"]
    frames = [m for m in out if m["type"] == "frame"]
    assert [m["state"] for m in statuses] == ["starting", "streaming"]
    assert out.index(statuses[1]) < out.index(frames[0])
    assert len(frames) == 9
    assert [f["seq"] for f in frames] == list(range(9))
    for m in out:
        json.dumps(m, allow_nan=False)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, dt: float) -> None:
        self.now += dt


class GappyBoard:
    """Sends samples in real time except between ``gap`` (start, end) seconds."""

    def __init__(self, clock: FakeClock, gap: tuple[float, float]) -> None:
        self.fs, self.ch_names, self.clock, self.gap = FS, MONTAGE, clock, gap
        self._sent = 0
        self._rng = np.random.default_rng(0)

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        now = self.clock()
        if self.gap[0] <= now < self.gap[1]:
            return np.empty((8, 0)), np.empty(0)
        due = int(now * FS)
        n = max(0, due - self._sent)
        t = 1000.0 + (self._sent + np.arange(n)) / FS
        self._sent = due
        return 8.0 * self._rng.standard_normal((8, n)), t


def test_run_reports_no_data_after_a_stall_and_recovers() -> None:
    clock = FakeClock()
    board = GappyBoard(clock, gap=(3.0, 6.0))
    out, emit = collect()
    why = run(board, MapStream(FS, MONTAGE, baseline_s=1.0), emit, stop=lambda: clock() >= 9.0,
              stall_s=1.0, sleep=clock.sleep, clock=clock)
    assert why == "stopped"
    states = [(m["state"], m["text"]) for m in out if m["type"] == "status"]
    assert [s for s, _ in states] == ["starting", "streaming", "no-data", "streaming"]
    assert all(text for _, text in states)


def test_run_reports_board_errors() -> None:
    class Broken:
        fs, ch_names = FS, MONTAGE

        def read(self):
            raise RuntimeError("serial port vanished")

    out, emit = collect()
    with pytest.raises(RuntimeError):
        run(Broken(), MapStream(FS, MONTAGE), emit, sleep=lambda dt: None)
    assert out[-1] == {"type": "status", "state": "error", "text": "serial port vanished"}


def test_run_rejects_bad_timing() -> None:
    with pytest.raises(ValueError):
        run(FakeBoard((15.0,)), MapStream(FS, MONTAGE), lambda m: None, poll_s=0.0)


# --- replay ---------------------------------------------------------------------------------

@pytest.fixture
def session(tmp_path):
    X, t, markers = fake_session((15.0, 17.0), [0, 1], seed=3)
    return save_recording(tmp_path / "s_cued.npz", X, t, FS, MONTAGE, (15.0, 17.0), markers)


def test_replay_plays_a_recording_at_speed_and_ends(session) -> None:
    rec = load_recording(session)
    clock = FakeClock()
    board = ReplayBoard(session, speed=4.0, clock=clock)
    assert board.fs == FS and board.ch_names == MONTAGE and not board.finished
    out, emit = collect()
    with board:
        why = run(board, MapStream(board.fs, board.ch_names, baseline_s=2.0, source="replay"),
                  emit, sleep=clock.sleep, clock=clock)
    assert why == "ended" and board.finished
    frames = [m for m in out if m["type"] == "frame"]
    n = rec.X.shape[1]
    assert len(frames) == 1 + (n - 500) // 62.5
    assert frames[-1]["t"] == float(rec.t[-1])
    assert out[-1]["type"] == "status" and out[-1]["state"] == "ended"
    duration = n / FS
    assert clock() == pytest.approx(duration / 4.0, abs=0.2)  # 4x real time
    assert all(q != "flat" for f in frames for q in f["quality"])


def test_replay_reads_return_what_is_due() -> None:
    X = noise(2.0, seed=9)
    t = times(X.shape[1])
    clock = FakeClock()
    board = ReplayBoard((X, t, FS, MONTAGE), speed=2.0, clock=clock)
    assert board.read()[0].shape == (8, 0)      # the first read starts the clock
    clock.now = 0.25
    got, ts = board.read()
    assert got.shape == (8, 125)
    np.testing.assert_array_equal(got, X[:, :125])
    np.testing.assert_array_equal(ts, t[:125])
    clock.now = 10.0
    rest, ts = board.read()
    assert rest.shape == (8, 375) and board.finished
    assert board.read()[0].shape == (8, 0)


def test_replay_loops_with_increasing_time() -> None:
    X = noise(1.0, seed=10)
    t = times(X.shape[1])
    clock = FakeClock()
    board = ReplayBoard((X, t, FS, MONTAGE), clock=clock, loop=True)
    board.read()
    clock.now = 2.5
    got, ts = board.read()
    assert got.shape == (8, 625) and not board.finished
    k = int(0.5 * FS)                                             # stream.LOOP_LEVEL_S
    drift = (X[:, -k:].mean(axis=1) - X[:, :k].mean(axis=1)) * 249.0 / (250 - k)
    lap = X - drift[:, None] * (np.arange(250) / 249.0)           # the drift as a ramp
    for k in range(2):
        np.testing.assert_allclose(got[:, 250 * k:250 * (k + 1)], lap, atol=1e-9)
    assert (np.diff(ts) > 0).all()
    np.testing.assert_allclose(np.diff(ts), 1.0 / FS, atol=1e-6)  # unix-time resolution


def test_replay_loop_has_no_step_at_the_seam_and_no_creeping_offset() -> None:
    # review finding: a recording's DC drifts tens of mV, so a plain loop stepped by that
    # much at every seam, which rings through the 1 Hz high-pass for seconds
    rng = np.random.default_rng(15)
    n = int(2.0 * FS)
    slopes = np.array([1, -1, 1, 1, 0.5, 1, 1, 2])[:, None]
    drift = 20_000.0 + np.linspace(0.0, 40_000.0, n) * slopes   # 20-80 mV per 2 s lap
    X = drift + 8.0 * rng.standard_normal((8, n))
    clock = FakeClock()
    board = ReplayBoard((X, times(n), FS, MONTAGE), clock=clock, loop=True)
    board.read()
    clock.now = 10 * n / FS + 0.5                                 # ten laps and a bit
    got, _ = board.read()
    k = int(LOOP_LEVEL_S * FS)
    np.testing.assert_allclose(got[:, n - k:n].mean(axis=1), got[:, :k].mean(axis=1),
                               atol=1e-6)                         # same level either side
    steps = np.abs(np.diff(got, axis=1))
    inside = np.ones(steps.shape[1], dtype=bool)
    inside[n - 1::n] = False                                      # last sample -> first
    for lap in range(1, 11):                                      # one ordinary sample step
        assert (steps[:, lap * n - 1] <= steps[:, inside].max(axis=1)).all()
        assert (steps[:, lap * n - 1] < 100.0).all()
    for lap in range(10):                                         # the offsets do not pile up
        np.testing.assert_allclose(got[:, lap * n:(lap + 1) * n], got[:, :n])
    assert (np.abs(got - X[:, :1]).max(axis=1) < 200.0).all()     # 40 mV of drift is gone

    # through the stream: an 8 s recording drifting 2 mV (a plain loop steps 2 mV at every
    # seam: "noisy" frames and a delta blow-up for 2 s each lap)
    n = int(8.0 * FS)
    X = 20_000.0 + np.linspace(0.0, 2_000.0, n) + 8.0 * rng.standard_normal((8, n))
    for loop_offsets in (True, False):
        clock = FakeClock()
        board = ReplayBoard((X, times(n), FS, MONTAGE), clock=clock, loop=True)
        board.read()
        clock.now = 5 * n / FS
        got, _ = board.read()
        if not loop_offsets:
            got = np.tile(X, 5)
        frames = MapStream(FS, MONTAGE, baseline_s=1.0).push(got, times(got.shape[1]))
        flagged = sum(q != "ok" for f in frames[8:] for q in f["quality"])
        delta = np.array([f["db"]["delta"] for f in frames[8:]])
        if loop_offsets:
            assert flagged == 0
            assert delta.max() - np.median(delta) < 4.0
        else:
            assert flagged > 0 and delta.max() - np.median(delta) > 20.0


def test_replay_rejects_bad_input() -> None:
    X = noise(1.0)
    with pytest.raises(ValueError):
        ReplayBoard((X, times(X.shape[1]), FS, MONTAGE), speed=0.0)
    with pytest.raises(ValueError):
        ReplayBoard((X, times(10), FS, MONTAGE))
    with pytest.raises(ValueError):
        ReplayBoard((X[:, :0], times(0), FS, MONTAGE))


# --- the page's fixture: real stream output, checked by extension/brainmap/test/*.test.mjs ------

PAGE_DIR = Path(__file__).resolve().parents[1] / "extension" / "brainmap"
FIXTURE = PAGE_DIR / "test" / "frames_fixture.json"
NODE = shutil.which("node") or next(
    (p for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if Path(p).exists()), None)


def _load_make_fixture():
    spec = importlib.util.spec_from_file_location("brainmap_make_fixture",
                                                  PAGE_DIR / "test" / "make_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _same(a, b, path="") -> None:
    """Equal up to the 0.01 rounding of dB and z, which another BLAS may flip in the last digit."""
    if isinstance(a, float) or isinstance(b, float):
        assert isinstance(a, (int, float)) and isinstance(b, (int, float)), path
        assert abs(a - b) <= 0.0101, f"{path}: {a} != {b}"
    elif isinstance(a, dict):
        assert isinstance(b, dict) and list(a) == list(b), path
        for k in a:
            _same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _same(x, y, f"{path}[{i}]")
    else:
        assert a == b, f"{path}: {a!r} != {b!r}"


def test_page_fixture_on_disk_matches_the_stream() -> None:
    make_fixture = _load_make_fixture()
    built = make_fixture.build()
    on_disk = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _same(built, on_disk)
    msgs = built["messages"]
    assert msgs[0]["type"] == "hello"
    assert [m["state"] for m in msgs if m["type"] == "status"] == ["starting", "streaming", "ended"]
    frames = [m for m in msgs if m["type"] == "frame"]
    assert {f["baseline"]["state"] for f in frames} == {"collecting", "ready"}
    assert {q for f in frames for q in f["quality"]} >= {"ok", "flat", "clipped", "noisy"}
    json.dumps(built, allow_nan=False)   # no NaN anywhere


def test_page_fixture_check_flag(capsys, tmp_path) -> None:
    make_fixture = _load_make_fixture()
    stale = tmp_path / "f.json"
    stale.write_text("{}\n")
    assert make_fixture.main(["--check", "--out", str(stale)]) == 1
    assert "error:" in capsys.readouterr().err
    assert make_fixture.main(["--out", str(stale)]) == 0
    assert make_fixture.main(["--check", "--out", str(stale)]) == 0


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_page_node_tests_pass() -> None:
    tests = sorted(str(p) for p in (PAGE_DIR / "test").glob("*.test.mjs"))
    assert len(tests) >= 2   # map, fixture
    proc = subprocess.run([NODE, "--test", *tests], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
