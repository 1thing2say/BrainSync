"""The VEP decoders on synthetic data: SNR (f-VEP), Template (c-VEP), FlashAverager
(t-VEP), and SyncDecoder over a marker stream from SequenceFakeBoard."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from brainsync.metrics import ABSTAIN, accuracy
from brainsync.online import Decoder
from brainsync.recordings import MONTAGE, POSTERIOR, channel_indices
from brainsync.signal import preprocess
from brainsync.stimulus.coded import CodeSequence, TimeSequence
from brainsync.stimulus.config import load_config, parse_config
from brainsync.synthetic import rest_trials, ssvep_trials
from brainsync.vep.cvep import Template, cross_validate, fit_template, lag_samples
from brainsync.vep.fake import SequenceFakeBoard
from brainsync.vep.fvep import SNR
from brainsync.vep.sync import SyncDecoder, cut_epochs, epochs_by_target, first_commit
from brainsync.vep.tvep import FlashAverager

FS = 250.0
ROWS = channel_indices(MONTAGE, POSTERIOR)
BAND = (1.0, 30.0)


def _trials(board: SequenceFakeBoard, targets, stim_s: float, rest_s: float = 1.0):
    """Cued trials on a fake board: (X, t, events, [(t_on, t_off, k)])."""
    Xs, ts, events, spans = [], [], [], []
    board.advance(rest_s)
    for k in targets:
        board.set_target(k)
        board.start_flicker()
        t_on = board.now
        board.advance(stim_s)
        board.stop_flicker()
        t_off = board.now
        board.advance(rest_s)
        spans.append((t_on, t_off, k))
    X, t = board.read()
    return X, t, board.read_events(), spans


# -- f-VEP: the SNR rule -------------------------------------------------------------------

def test_snr_classifier_matches_the_cca_interface_and_decodes():
    freqs = load_config("fvep_6").freqs
    X, y = ssvep_trials(freqs, FS, n_per_target=8, seconds=4.0, snr_db=-8.0, seed=1)
    X = preprocess(X, FS)
    clf = SNR(freqs, FS)
    s = clf.raw_scores(X)
    assert s.shape == (48, 6)
    pred, scores = clf.decide(X)
    assert pred.shape == (48,) and scores.shape == (48, 6)
    assert accuracy(y, pred) >= 0.9
    assert accuracy(y, SNR(freqs, FS, harmonics=2).predict(X)) >= 0.9
    rest = preprocess(rest_trials(20, FS, 4, 4.0, seed=2), FS)
    clf.fit_baseline(rest[:10])
    assert clf.has_baseline and accuracy(y, clf.predict(X)) >= 0.9
    assert abs(float(clf.scores(rest[10:]).mean())) < 1.0     # unseen rest sits near 0 dB
    assert clf.scores(X)[np.arange(y.size), y].mean() > clf.scores(rest[10:]).max(axis=1).mean()
    assert "SNR(" in repr(clf) and clf.name == "snr"
    dec = Decoder(SNR(freqs, FS), FS, window_s=4.0, step_s=0.5, votes=1)   # plugs in
    assert dec.classifier is not None
    for kw in ({"neighbour_hz": 0}, {"exclude_hz": 2.0}, {"resolution_hz": -1}):
        with pytest.raises(ValueError):
            SNR(freqs, FS, **kw)
    # a harmonic above Nyquist is skipped, the fundamental always counts
    assert SNR([100.0], FS, harmonics=3).raw_scores(X[:1]).shape == (1, 1)


# -- c-VEP: the shifted template -------------------------------------------------------------

@pytest.fixture(scope="module")
def cvep_data():
    cfg = load_config("cvep_4")
    board = SequenceFakeBoard(CodeSequence(cfg), amp=3.0, noise=8.0, seed=1, t0=1000.0)
    targets = [k for _ in range(6) for k in range(4)]
    X, t, events, spans = _trials(board, targets, stim_s=cfg.session.stim_s)
    return cfg, preprocess(X[ROWS], FS, BAND), t, events, spans


def _cycles_by_target(cfg, X, t, events, spans, n):
    out: dict[int, list[np.ndarray]] = {}
    for t_on, t_off, k in spans:
        inside = [e for e in events if e[1] == "cycle" and t_on <= e[0] < t_off]
        ep, _ = cut_epochs(X, t, [e[0] for e in inside], n)
        out.setdefault(k, []).append(ep)
    return {k: np.concatenate(v) for k, v in out.items()}


def test_template_fit_cross_validate_decide_and_save(cvep_data, tmp_path: Path):
    cfg, X, t, events, spans = cvep_data
    n = int(round(cfg.code.cycle_frames / 60 * FS))
    lag = lag_samples(cfg.code.lag_frames, FS, 60.0)
    assert n == 262 and lag == pytest.approx(66.667, abs=1e-3)
    cycles = _cycles_by_target(cfg, X, t, events, spans, n)
    assert set(cycles) == {0, 1, 2, 3} and all(c.shape == (30, 4, n) for c in cycles.values())
    train = {k: c[:15] for k, c in cycles.items()}
    test = {k: c[15:] for k, c in cycles.items()}
    tpl = fit_template(train, 4, lag, FS, base=0, ch_names=POSTERIOR)
    assert tpl.n_cycles == 60 and tpl.n_samples == n and tpl.shift(3) == 200
    y, pred = cross_validate(train, 4, lag, FS)
    assert y.size == 60 and accuracy(y, pred) >= 0.9
    hits = total = 0
    for k, c in test.items():
        p, s = tpl.decide(c)
        assert s.shape == (15, 4)
        hits += int(np.sum(p == k))
        total += p.size
    assert hits / total >= 0.9
    # a single cycle is weaker than the mean of two
    k0 = test[2]
    _, s1 = tpl.decide(k0)
    _, s2 = tpl.decide(np.stack([k0[i:i + 2].mean(axis=0) for i in range(0, 14, 2)]))
    lead = lambda s: float((np.sort(s, axis=1)[:, -1] - np.sort(s, axis=1)[:, -2]).mean())  # noqa: E731
    assert lead(s2) > lead(s1)
    # a margin makes it abstain rather than guess on noise
    noise = np.random.default_rng(0).standard_normal((5, 4, n))
    p, _ = tpl.decide(noise, margin=0.3)
    assert np.all(p == ABSTAIN)
    with pytest.raises(ValueError, match="margin"):
        tpl.decide(noise, margin=-1)
    # the template applies to a target's own alignment: fit on target 1 only, same answer
    one = fit_template({1: train[1]}, 4, lag, FS, base=0)
    np.testing.assert_allclose(one.mean, np.roll(train[1], -67, axis=-1).mean(axis=0))
    p, _ = one.decide(test[3])
    assert np.mean(p == 3) >= 0.8
    path = tpl.save(tmp_path / "tpl.npz")
    back = Template.load(path)
    np.testing.assert_array_equal(back.mean, tpl.mean)
    assert (back.base, back.n_targets, back.lag_samples, back.fs, back.n_cycles,
            back.ch_names) == (0, 4, lag, FS, 60, tuple(POSTERIOR))


def test_template_validation():
    mean = np.zeros((4, 262))
    with pytest.raises(ValueError, match="base"):
        Template(mean, base=4, n_targets=4, lag_samples=66.7, fs=FS)
    with pytest.raises(ValueError, match="do not fit"):
        Template(mean, base=0, n_targets=5, lag_samples=66.7, fs=FS)
    with pytest.raises(ValueError, match="channel names"):
        Template(mean, base=0, n_targets=4, lag_samples=66.7, fs=FS, ch_names=("a",))
    tpl = Template(mean, base=0, n_targets=4, lag_samples=66.7, fs=FS)
    with pytest.raises(ValueError, match="template"):
        tpl.scores(np.zeros((2, 4, 100)))
    with pytest.raises(ValueError, match="no cycles"):
        fit_template({}, 4, 66.7, FS)
    with pytest.raises(ValueError, match="not among"):
        fit_template({7: np.zeros((2, 4, 262))}, 4, 66.7, FS)
    with pytest.raises(ValueError, match="differ in shape"):
        fit_template({0: np.zeros((2, 4, 262)), 1: np.zeros((2, 3, 262))}, 4, 66.7, FS)
    with pytest.raises(ValueError, match="at least 2"):
        cross_validate({0: np.zeros((1, 4, 262))}, 4, 66.7, FS)
    with pytest.raises(ValueError):
        lag_samples(16, 0, 60)


# -- t-VEP: flash averages -------------------------------------------------------------------

@pytest.fixture(scope="module")
def tvep_data():
    cfg = load_config("tvep_4")
    board = SequenceFakeBoard(TimeSequence(cfg, seed=2), amp=4.0, noise=8.0, seed=3, t0=2000.0)
    targets = [k for _ in range(3) for k in range(4)]
    X, t, events, spans = _trials(board, targets, stim_s=cfg.session.stim_s)
    return cfg, preprocess(X[ROWS], FS, BAND), t, events, spans


def test_flash_averager_decodes_cued_trials(tvep_data):
    cfg, X, t, events, spans = tvep_data
    avg = FlashAverager(4, FS, epoch_s=0.5, min_flashes=2)
    assert avg.n_samples == 125
    hits = 0
    for t_on, t_off, k in spans:
        inside = [e for e in events if t_on <= e[0] < t_off]
        groups = epochs_by_target(X, t, inside, "flash", avg.n_samples)
        assert set(groups) == {0, 1, 2, 3}
        counts = avg.counts(groups)
        assert np.all(counts >= 5)                        # 6 rounds, the last may be cut
        pred, s = avg.decide(groups)
        assert s.shape == (4,) and np.all(np.isfinite(s))
        hits += int(pred == k)
    assert hits >= 10                                     # of 12 trials
    # too few flashes of one target: abstain, never a guess
    groups[0] = groups[0][:1]
    pred, s = avg.decide(groups)
    assert pred == ABSTAIN and np.isnan(s[0]) and np.isfinite(s[1:]).all()
    strict = FlashAverager(4, FS, epoch_s=0.5, min_flashes=2, margin=5.0)
    assert strict.decide({k: g for k, g in groups.items() if k} | {0: groups[1]})[0] == ABSTAIN


def test_flash_averager_validation():
    for kw in ({"n_targets": 0}, {"fs": 0}, {"epoch_s": 0}, {"min_flashes": 0}, {"margin": -1}):
        with pytest.raises(ValueError):
            FlashAverager(**{"n_targets": 4, "fs": FS, **kw})
    avg = FlashAverager(2, FS, epoch_s=0.2)
    with pytest.raises(ValueError, match="epochs must be"):
        avg.decide({0: np.zeros((3, 4, 10))})
    with pytest.raises(ValueError, match="not among"):
        avg.decide({5: np.zeros((3, 4, 50))})
    with pytest.raises(ValueError, match="no flash epochs"):
        avg.decide({})
    with pytest.raises(ValueError, match="channel count"):
        avg.decide({0: np.zeros((3, 4, 50)), 1: np.zeros((3, 2, 50))})


# -- cut_epochs ------------------------------------------------------------------------------

def test_cut_epochs_skips_incomplete_and_early_onsets():
    t = 10.0 + np.arange(100) / FS
    X = np.arange(200, dtype=float).reshape(2, 100)
    ep, kept = cut_epochs(X, t, [9.0, 10.0, 10.1, 10.35], 20)   # 10.35 s: sample 88 + 20 > 100
    assert kept == [1, 2] and ep.shape == (2, 2, 20)
    np.testing.assert_array_equal(ep[0, 0], np.arange(20))
    np.testing.assert_array_equal(ep[1, 0], np.arange(25, 45))
    ep, kept = cut_epochs(X, t, [], 20)
    assert ep.shape == (0, 2, 20) and kept == []
    with pytest.raises(ValueError):
        cut_epochs(X, t[:50], [10.0], 20)
    with pytest.raises(ValueError):
        cut_epochs(X, t, [10.0], 0)


# -- the live decoder ------------------------------------------------------------------------

def _stream(board: SequenceFakeBoard, dec: SyncDecoder, seconds: float, step: float = 0.2):
    out = []
    for _ in range(int(round(seconds / step))):
        board.advance(step)
        X, t = board.read()
        out += dec.feed(X, t, board.read_events())
    return out


def test_sync_decoder_commits_the_fixated_code_target(cvep_data):
    cfg, X, t, events, spans = cvep_data
    n = int(round(cfg.code.cycle_frames / 60 * FS))
    lag = lag_samples(cfg.code.lag_frames, FS, 60.0)
    tpl = fit_template(_cycles_by_target(cfg, X, t, events, spans, n), cfg.n_targets, lag, FS)
    board = SequenceFakeBoard(CodeSequence(cfg), amp=3.0, noise=8.0, seed=11, t0=5000.0)
    dec = SyncDecoder(FS, template=tpl, votes=2, cycles=2, pad_s=0.5)
    assert dec.paradigm == "code" and dec.kind == "cycle" and dec.n_targets == 4
    results = []
    for k in (3, 0, 2):
        dec.reset()
        board.set_target(k)
        board.start_flicker()
        t_on = board.now
        decisions = _stream(board, dec, 6.0)
        board.stop_flicker()
        board.advance(1.0)
        board.read(), board.read_events()
        first = first_commit(decisions)
        assert first is not None and first.scores.shape == (4,)
        results.append((k, first.committed, first.time - t_on))
    assert all(k == got for k, got, _ in results)
    # two cycles (2.1 s) plus the pad is the earliest possible commit
    assert all(2.0 <= delay <= 3.5 for _, _, delay in results)
    # decisions come once per cycle, with the cycle index as the clock
    assert all(d.end > 0 and np.isfinite(d.gap) for d in decisions)
    assert dec.dropped == 0


def test_sync_decoder_ignores_markers_without_samples_and_resets():
    cfg = load_config("cvep_4")
    tpl = Template(np.zeros((4, 262)) + np.sin(np.arange(262) / 7.0), base=0, n_targets=4,
                   lag_samples=lag_samples(16, FS, 60.0), fs=FS)
    dec = SyncDecoder(FS, template=tpl, votes=1)
    # a marker from before the stream began: no epoch, no decision
    board = SequenceFakeBoard(CodeSequence(cfg), amp=0.0, noise=1.0, t0=100.0)
    board.start_flicker()
    board.advance(0.5)
    board.read(), board.read_events()                     # thrown away, markers included
    stale = [(100.0, "cycle", 0), (99.0, "flash", 1)]
    board.advance(2.0)
    X, t = board.read()
    out = dec.feed(X, t, stale + board.read_events())
    assert out == [] and dec.dropped == 1                  # cycle:0 dropped, flash ignored
    board.advance(2.0)
    X, t = board.read()
    out = dec.feed(X, t, board.read_events())
    assert len(out) >= 1                                   # cycles 1 and 2 are complete now
    dec.reset()
    assert dec.committed == ABSTAIN and dec.n_seen == 0 and dec.dropped == 0
    with pytest.raises(ValueError, match="exactly one"):
        SyncDecoder(FS)
    with pytest.raises(ValueError, match="exactly one"):
        SyncDecoder(FS, template=tpl, averager=FlashAverager(4, FS))
    with pytest.raises(ValueError, match="Hz"):
        SyncDecoder(200.0, template=tpl)
    with pytest.raises(ValueError, match="channels"):
        SyncDecoder(FS, template=tpl, channels=["O1", "O2"])
    with pytest.raises(ValueError, match="rows"):
        dec.feed(np.zeros((2, 10)), np.arange(10) / FS)
    with pytest.raises(ValueError, match="X must be"):
        dec.feed(np.zeros((8, 10)), np.arange(5) / FS)


def test_sync_decoder_commits_the_fixated_flash_target():
    cfg = load_config("tvep_4")
    board = SequenceFakeBoard(TimeSequence(cfg, seed=8), amp=4.0, noise=8.0, seed=9, t0=7000.0)
    dec = SyncDecoder(FS, averager=FlashAverager(4, FS, epoch_s=0.5, min_flashes=3),
                      votes=2, flash_window_s=8.0)
    assert dec.paradigm == "time" and dec.kind == "flash"
    results = []
    for k in (1, 3):
        dec.reset()
        board.set_target(k)
        board.start_flicker()
        t_on = board.now
        decisions = _stream(board, dec, 10.0)
        board.stop_flicker()
        board.advance(1.0)
        board.read(), board.read_events()
        first = first_commit(decisions)
        assert first is not None
        results.append((k, first.committed, first.time - t_on))
    assert all(k == got for k, got, _ in results)
    assert all(3.0 <= delay <= 9.0 for _, _, delay in results)   # 3 flashes per target first


def test_snr_grows_with_the_response_and_its_ring_clears_the_hann_lobe():
    """With a Hann window the bins within 2 / T of a line are its own leakage; the ring
    must start beyond them or the ratio saturates and a strong response scores like a weak
    one (the live confidence bars and the margin then say nothing)."""
    freqs = [10.0, 15.0]
    clf = SNR(freqs, FS)
    inner, outer = clf.ring(int(2.0 * FS))        # a 2 s window: bins 0.5 Hz, lobe 1 Hz
    assert inner == pytest.approx(1.0) and outer == pytest.approx(3.0)
    inner4, outer4 = clf.ring(int(4.0 * FS))
    assert inner4 == pytest.approx(0.5) and outer4 == pytest.approx(1.5)
    scores = []
    for snr_db in (-12.0, -6.0, 0.0):
        X, y = ssvep_trials(freqs, FS, n_per_target=6, seconds=2.0, snr_db=snr_db, seed=4)
        s = clf.raw_scores(preprocess(X, FS))
        scores.append(float(np.mean(s[np.arange(y.size), y])))
    assert scores[0] < scores[1] - 3.0 < scores[2] - 6.0, scores   # dB keep rising
    with pytest.raises(ValueError, match="no bin within"):
        SNR(freqs, FS, exclude_hz=0.01, resolution_hz=0.5).raw_scores(np.ones((4, 500)) + np.arange(500) % 2)
