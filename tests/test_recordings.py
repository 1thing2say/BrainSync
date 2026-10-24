from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from brainsync.recordings import (MONTAGE, POSTERIOR, Recording, Windows, channel_indices,
                                  cut_windows, list_recordings, load_recording, parse_markers,
                                  reject_artifacts, save_recording)
from brainsync.signal import preprocess
from brainsync.synthetic import RESPONSE_LATENCY, fake_session

FS = 250.0
FREQS = [15.0, 17.0]
LABELS = [1, 0, 0, 1, 1, 0]
CUE, STIM, REST, LEAD = 1.0, 4.0, 3.0, 1.0


@pytest.fixture
def session_file(tmp_path):
    X, t, text = fake_session(FREQS, LABELS, fs=FS, cue_s=CUE, stim_s=STIM, rest_s=REST, seed=3)
    path = save_recording(tmp_path / "2026-10-23_120000_cued.npz", X, t, FS, MONTAGE, FREQS,
                          text, window_s=3.86, latency=0.14)
    return path, X, t, text


def expected_starts(n_trials: int, latency: float = 0.14):
    period = CUE + STIM + REST
    on = [round((LEAD + i * period + CUE + latency) * FS) for i in range(n_trials)]
    off = [round((LEAD + i * period + CUE + STIM + latency) * FS) for i in range(n_trials)]
    return np.array(on), np.array(off)


def test_parse_markers_keeps_only_stim_lines():
    text = (
        "warning: 2 target(s) flicker at 15-25 Hz\n"
        "measured refresh: 60.01 Hz\n"
        "[marker 1792714722.6782] session_start\n"
        "[marker 1792714722.7116] cue:1\n"
        "[marker 1792714723.7114] stim_on:1\n"
        "  [marker 1792714727.7114] stim_off:1  \n"
        "[marker 1792714731.5] stim_on:12\n"
        "1.9344 \tWARNING \tMonitor specification not found.\n"
        "[marker 1792714735.25] stim_off:12\n"
        "[marker 1792714735.25] block_end:0\n"
    )
    assert parse_markers(text) == [
        (1792714723.7114, "stim_on", 1),
        (1792714727.7114, "stim_off", 1),
        (1792714731.5, "stim_on", 12),
        (1792714735.25, "stim_off", 12),
    ]
    assert parse_markers("") == []


def test_save_load_roundtrip_cued(session_file):
    path, X, t, text = session_file
    rec = load_recording(path)
    assert rec.kind == "cued"
    np.testing.assert_array_equal(rec.X, X)
    np.testing.assert_array_equal(rec.t, t)
    assert rec.fs == FS and rec.ch_names == MONTAGE
    np.testing.assert_array_equal(rec.freqs, FREQS)
    np.testing.assert_array_equal(rec.phases, [0.0, 0.0])
    assert rec.window_s == 3.86 and rec.latency == 0.14
    assert rec.band == (1.0, 100.0) and rec.notch == 60.0
    assert rec.marker_text == text
    assert [k for _, kind, k in rec.markers if kind == "stim_on"] == LABELS
    with np.load(path) as d:
        assert set(d.files) == {"X", "t", "fs", "ch_names", "freqs", "phases", "f0", "markers",
                                "window_s", "latency", "band", "notch"}
        assert float(d["f0"]) == 15.0


def test_single_target_file_has_f0_only_and_targets_zero(tmp_path):
    X, t, text = fake_session([12.0, 15.0], [1, 1, 1], seed=1)
    path = save_recording(tmp_path / "x_trials.npz", X, t, FS, MONTAGE, [12.0], text)
    with np.load(path) as d:
        assert set(d.files) == {"X", "t", "fs", "ch_names", "f0", "markers", "window_s",
                                "latency", "band", "notch"}
    rec = load_recording(path)
    assert rec.kind == "trials" and rec.n_targets == 1
    np.testing.assert_array_equal(rec.freqs, [12.0])
    np.testing.assert_array_equal(rec.phases, [0.0])
    assert {k for _, _, k in rec.markers} == {0}
    w = cut_windows(rec)
    np.testing.assert_array_equal(w.labels, [0, 0, 0])


def test_save_recording_validates(tmp_path):
    X, t, text = fake_session([15.0], [0], seed=0)
    with pytest.raises(ValueError, match="npz"):
        save_recording(tmp_path / "a.txt", X, t, FS, MONTAGE, [15.0], text)
    with pytest.raises(ValueError, match="X"):
        save_recording(tmp_path / "a.npz", X[:, :-1], t, FS, MONTAGE, [15.0], text)
    with pytest.raises(ValueError, match="15.0"):
        save_recording(tmp_path / "a.npz", X, t, FS, MONTAGE, [15.0, 17.0], text, kind="trials")
    with pytest.raises(ValueError, match="phases"):
        save_recording(tmp_path / "a.npz", X, t, FS, MONTAGE, [15.0, 17.0], text, phases=[0.0])


def test_load_recording_errors(tmp_path):
    with pytest.raises(FileNotFoundError, match="nope.npz"):
        load_recording(tmp_path / "nope.npz")
    np.savez(tmp_path / "bad.npz", X=np.zeros((8, 10)), t=np.zeros(10))
    with pytest.raises(KeyError, match="fs"):
        load_recording(tmp_path / "bad.npz")


def test_list_recordings(tmp_path):
    for name in ("b_cued.npz", "a_trials.npz", "c_trials.npz", "c_trials.png",
                 "d_trials_pooled.png", "e_10Hz.npz", "f_sweep.npz"):
        (tmp_path / name).write_bytes(b"")
    names = lambda ps: [p.name for p in ps]  # noqa: E731
    assert names(list_recordings(directory=tmp_path)) == ["a_trials.npz", "b_cued.npz",
                                                          "c_trials.npz"]
    assert names(list_recordings("trials", tmp_path)) == ["a_trials.npz", "c_trials.npz"]
    assert names(list_recordings("cued", tmp_path)) == ["b_cued.npz"]
    assert list_recordings(directory=tmp_path / "missing") == []
    with pytest.raises(ValueError, match="sweep"):
        list_recordings("sweep", tmp_path)


def test_list_recordings_defaults_to_env_dir(tmp_path, monkeypatch):
    (tmp_path / "spectrum_checks").mkdir()
    (tmp_path / "spectrum_checks" / "z_cued.npz").write_bytes(b"")
    monkeypatch.setenv("BRAINSYNC_DATA_DIR", str(tmp_path))
    assert [p.name for p in list_recordings()] == ["z_cued.npz"]


def test_cut_windows_labels_and_positions_exact(session_file):
    path, X, _, _ = session_file
    rec = load_recording(path)
    w = cut_windows(rec)
    on, off = expected_starts(len(LABELS))
    np.testing.assert_array_equal(w.labels, LABELS)
    np.testing.assert_array_equal(w.on_start, on)
    np.testing.assert_array_equal(w.off_start, off)
    n = round(3.86 * FS)
    assert w.on.shape == w.off.shape == (len(LABELS), 8, n)
    Y = preprocess(X, FS, band=(1.0, 100.0), notch_hz=60.0)
    np.testing.assert_allclose(w.on[2], Y[:, on[2]:on[2] + n])
    np.testing.assert_allclose(w.off[4], Y[:, off[4]:off[4] + n])
    assert len(w) == len(LABELS) and w.fs == FS and w.ch_names == MONTAGE


def test_cut_windows_overrides(session_file):
    rec = load_recording(session_file[0])
    w = cut_windows(rec, window_s=1.0, latency=0.0)
    on, off = expected_starts(len(LABELS), latency=0.0)
    np.testing.assert_array_equal(w.on_start, on)
    np.testing.assert_array_equal(w.off_start, off)
    assert w.on.shape[-1] == 250
    with pytest.raises(ValueError, match="-1"):
        cut_windows(rec, window_s=-1.0)


def test_cut_windows_flicker_carries_cued_frequency(session_file):
    rec = load_recording(session_file[0])
    w = cut_windows(rec).pick(POSTERIOR)
    freqs = np.fft.rfftfreq(w.on.shape[-1], 1 / FS)
    for trial, label in zip(w.on, w.labels):
        spec = np.abs(np.fft.rfft(trial, axis=-1)).mean(axis=0)
        band = (freqs > 13) & (freqs < 19)
        assert freqs[band][np.argmax(spec[band])] == pytest.approx(FREQS[label], abs=0.3)
    on_power = np.mean(w.on ** 2)
    off_power = np.mean(w.off ** 2)
    assert on_power > off_power


def test_cut_windows_drops_incomplete_trials(session_file):
    rec = load_recording(session_file[0])
    on, off = expected_starts(len(LABELS))
    # end the data inside the last trial's still window
    cut = off[-1] + 100
    short = dataclasses.replace(rec, X=rec.X[:, :cut], t=rec.t[:cut])
    w = cut_windows(short)
    np.testing.assert_array_equal(w.labels, LABELS[:-1])
    # orphan markers: a stim_on without stim_off, a stim_off without stim_on, a mismatch
    t_end = rec.t[-1]
    extra = [(t_end - 50.0, "stim_off", 0), (rec.t[0] + 0.5, "stim_on", 1)]
    markers = [m for m in rec.markers if not (m[1] == "stim_off" and abs(m[0] - rec.t[off[1] - 35]) < 1e-3)]
    odd = dataclasses.replace(rec, markers=markers + extra)
    w = cut_windows(odd)
    np.testing.assert_array_equal(w.labels, [LABELS[0]] + LABELS[2:])
    # a marker before the first sample is dropped
    early = dataclasses.replace(rec, markers=[(rec.t[0] - 10.0, "stim_on", 0),
                                              (rec.t[0] - 6.0, "stim_off", 0)])
    w = cut_windows(early)
    assert len(w) == 0 and w.on.shape == (0, 8, 965)


def test_cut_windows_rejects_out_of_range_target(session_file):
    rec = load_recording(session_file[0])
    bad = dataclasses.replace(rec, markers=[(rec.t[300], "stim_on", 5), (rec.t[1300], "stim_off", 5)])
    with pytest.raises(ValueError, match="5"):
        cut_windows(bad)


def test_windows_subset_and_pick(session_file):
    w = cut_windows(load_recording(session_file[0]))
    s = w.subset([3, 1])
    np.testing.assert_array_equal(s.labels, [LABELS[3], LABELS[1]])
    np.testing.assert_array_equal(s.on[0], w.on[3])
    p = w.pick(POSTERIOR)
    assert p.on.shape[1] == 4 and p.ch_names == POSTERIOR
    np.testing.assert_array_equal(p.off[:, 0], w.off[:, MONTAGE.index("P7")])


def test_channel_indices():
    assert channel_indices(MONTAGE, POSTERIOR) == [4, 5, 6, 7]
    assert channel_indices(MONTAGE, [0, "O2"]) == [0, 7]
    assert channel_indices(MONTAGE, None) == list(range(8))
    with pytest.raises(KeyError, match="Cz"):
        channel_indices(MONTAGE, ["Cz"])
    with pytest.raises(ValueError, match="9"):
        channel_indices(MONTAGE, [9])


def _windows(rng, n=12, n_ch=8, n_s=200):
    return rng.standard_normal((n, n_ch, n_s)), rng.standard_normal((n, n_ch, n_s))


def test_reject_artifacts_flags_either_window(rng):
    on, off = _windows(rng)
    on[2, 5] *= 40.0
    off[7, 6] *= 40.0
    keep = reject_artifacts(on, off, POSTERIOR)
    np.testing.assert_array_equal(keep, [i for i in range(12) if i not in (2, 7)])
    assert keep.dtype.kind == "i"


def test_reject_artifacts_ignores_unselected_channels(rng):
    on, off = _windows(rng)
    on[4, 0] *= 100.0  # Fp1 blink, not posterior
    np.testing.assert_array_equal(reject_artifacts(on, off, POSTERIOR), np.arange(12))
    assert 4 not in reject_artifacts(on, off, None)
    assert 4 not in reject_artifacts(on, off, [0, 1])


def test_reject_artifacts_factor(rng):
    on, off = _windows(rng)
    on[0] *= 2.0
    np.testing.assert_array_equal(reject_artifacts(on, off, POSTERIOR, factor=0), np.arange(12))
    assert 0 in reject_artifacts(on, off, POSTERIOR, factor=3.0)
    assert 0 not in reject_artifacts(on, off, POSTERIOR, factor=1.5)
    with pytest.raises(ValueError, match="-1"):
        reject_artifacts(on, off, POSTERIOR, factor=-1)


def test_reject_artifacts_shapes(rng):
    on, off = _windows(rng)
    with pytest.raises(ValueError):
        reject_artifacts(on, off[:5])
    with pytest.raises(ValueError, match="4"):
        reject_artifacts(on[:, :4], off[:, :4], POSTERIOR)
    np.testing.assert_array_equal(reject_artifacts(on[:, :4], off[:, :4], [0, 3]), np.arange(12))
    empty = np.empty((0, 8, 10))
    assert reject_artifacts(empty, empty, POSTERIOR).size == 0


@pytest.mark.realdata
def test_real_recordings_load_and_cut(real_recordings):
    kinds = {"trials": 0, "cued": 0}
    for path in real_recordings:
        rec = load_recording(path)
        assert isinstance(rec, Recording) and rec.ch_names == MONTAGE and rec.fs == 250.0
        kinds[rec.kind] += 1
        w = cut_windows(rec)
        assert isinstance(w, Windows) and len(w) > 0
        keep = reject_artifacts(w.on, w.off, POSTERIOR, ch_names=w.ch_names)
        assert 0 < keep.size <= len(w)
        if rec.kind == "cued":
            np.testing.assert_array_equal(rec.freqs, [15.0, 17.0])
            assert set(w.labels.tolist()) == {0, 1}
        else:
            assert rec.n_targets == 1 and set(w.labels.tolist()) == {0}
    assert kinds["trials"] >= 1 and kinds["cued"] >= 1


@pytest.mark.realdata
def test_real_recording_roundtrips_with_same_keys(real_recordings, tmp_path):
    for kind in ("trials", "cued"):
        src = next(p for p in real_recordings if p.name.endswith(f"_{kind}.npz"))
        rec = load_recording(src)
        out = save_recording(tmp_path / src.name, rec.X, rec.t, rec.fs, rec.ch_names, rec.freqs,
                             rec.marker_text, rec.window_s, rec.latency, rec.phases, rec.band,
                             rec.notch)
        with np.load(src) as a, np.load(out) as b:
            assert set(a.files) == set(b.files)
        again = load_recording(out)
        assert again.kind == rec.kind and again.markers == rec.markers
        np.testing.assert_array_equal(again.X, rec.X)


def test_response_latency_matches_default_window_latency():
    assert RESPONSE_LATENCY == pytest.approx(0.14)
