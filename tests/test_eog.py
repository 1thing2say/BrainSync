"""Tests for the EOG gaze prior: features, layout and fitted modes, abstentions, drift,
the decoder's prior hook, the DecoderBank wiring and the offline scorer."""
from __future__ import annotations

import numpy as np
import pytest

from brainsync import eog
from brainsync.board import FakeBoard
from brainsync.classify import ABSTAIN, make
from brainsync.eog import EOG_MONTAGE, GazeDecoder, raw_windows, score_gaze
from brainsync.online import TUNED, Decoder, fit_rest
from brainsync.recordings import MONTAGE, cut_windows, load_recording, parse_markers, \
    pair_trials, save_recording
from brainsync.server.decoding import DecoderBank
from brainsync.stimulus.config import load_config
from brainsync.synthetic import fake_session

FS = 250.0
LO, RO, FP1, FP2 = (EOG_MONTAGE.index(c) for c in ("LO", "RO", "Fp1", "Fp2"))
DIAMOND = [(0.0, 1.0), (-1.0, 0.0), (1.0, 0.0), (0.0, -1.0)]   # top, left, right, bottom
AMP = 200.0   # µV for a look at a target, about 10 degrees at 20 µV/degree
DC = np.linspace(-40_000.0, -20_000.0, 8)[:, None]           # the board's electrode offsets


def add_gaze(X: np.ndarray, gaze: tuple[float, float], gain_v: float = 1.0,
             sl: slice = slice(None)) -> None:
    """Add a look of (h, v) µV: the cornea's dipole on the canthi and the forehead."""
    h, v = gaze
    X[LO, sl] -= h / 2
    X[RO, sl] += h / 2
    X[FP1, sl] += gain_v * v
    X[FP2, sl] += gain_v * v


def add_blink(X: np.ndarray, at_s: float, amp: float = 300.0, width_s: float = 0.08) -> None:
    n = X.shape[1]
    tt = np.arange(n) / FS
    bump = amp * np.exp(-0.5 * ((tt - at_s) / width_s) ** 2)
    X[FP1] += bump
    X[FP2] += bump


def board(seconds: float, gaze: tuple[float, float] = (0.0, 0.0), seed: int = 0,
          noise: float = 5.0, gain_v: float = 1.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    X = noise * rng.standard_normal((8, int(round(seconds * FS)))) + DC
    add_gaze(X, gaze, gain_v)
    return X


def gaze_decoder(**kw) -> GazeDecoder:
    g = GazeDecoder(FS, EOG_MONTAGE, **kw)
    g.fit_centre(board(6.0, seed=99))
    return g


# -- features and layout mode -------------------------------------------------------------------

def test_features_follow_the_eye_dipole():
    g = gaze_decoder()
    right = g.features(board(1.5, (AMP, 0.0), seed=1))
    up = g.features(board(1.5, (0.0, AMP), seed=2))
    assert right == pytest.approx((AMP, 0.0), abs=3.0)
    assert up == pytest.approx((0.0, AMP), abs=3.0)
    assert g.features(board(1.5, seed=3)) == pytest.approx((0.0, 0.0), abs=3.0)


def test_layout_mode_classifies_the_diamond():
    g = gaze_decoder()
    g.set_targets(DIAMOND)
    assert g.mode == "layout" and g.n_targets == 4
    for k, (x, y) in enumerate(DIAMOND):
        d = g.decide_window(board(1.5, (AMP * x, AMP * y), seed=10 + k))
        assert d.raw == k and d.reason == ""
        assert d.scores[k] == pytest.approx(1.0, abs=0.05)
        assert d.scores[3 - k] == pytest.approx(-1.0, abs=0.05)   # the opposite corner
    pred, scores = g.decide(np.stack([board(1.5, (AMP * x, AMP * y), seed=k)
                                      for k, (x, y) in enumerate(DIAMOND)]))
    assert pred.tolist() == [0, 1, 2, 3] and scores.shape == (4, 4)


def test_abstains_at_the_centre_on_a_blink_and_between_targets():
    g = gaze_decoder()
    g.set_targets(DIAMOND)
    assert g.decide_window(board(1.5, (5.0, 5.0), seed=4)).reason == "centre"
    X = board(1.5, (AMP, 0.0), seed=5)
    add_blink(X, 0.7)
    d = g.decide_window(X)
    assert d.raw == ABSTAIN and d.reason == "blink"
    between = g.decide_window(board(1.5, (AMP, AMP), seed=6))     # exactly between top and right
    assert between.raw == ABSTAIN and between.reason == "margin"
    assert g.score_uv(np.zeros(2)).tolist() == [0.0] * 4


def test_confidence_scales_a_weak_look():
    g = gaze_decoder(min_uv=50.0)
    g.set_targets(DIAMOND)
    weak = g.decide_window(board(1.5, (0.0, 60.0), seed=7))   # just over min_uv, top
    assert weak.raw == 0 and 0.9 < weak.scores[0] <= 1.0
    half = g.score_uv(np.array([0.0, 25.0]))                  # half of min_uv: half the weight
    assert half[0] == pytest.approx(0.5, abs=1e-9) and half[3] == pytest.approx(-0.5, abs=1e-9)


# -- fitted mode and drift ----------------------------------------------------------------------

def corners_windows(gain_v: float, n_each: int = 4, seed: int = 0):
    corners = [(-1.0, 1.0), (1.0, 1.0), (-1.0, -1.0), (1.0, -1.0)]
    W, y = [], []
    for k, (x, v) in enumerate(corners):
        for i in range(n_each):
            W.append(board(1.5, (AMP * x, AMP * v), seed=seed + 100 * k + i, gain_v=gain_v))
            y.append(k)
    return corners, np.stack(W), np.array(y)


def test_fit_learns_an_anisotropic_gain_the_layout_cannot():
    corners, W, y = corners_windows(gain_v=0.1)
    layout = gaze_decoder()
    layout.set_targets(corners)
    pred_layout, _ = layout.decide(W)
    assert np.all(pred_layout == ABSTAIN)       # cosines too close: every window hits the margin
    fitted = gaze_decoder()
    means = fitted.fit(W[1:], y[1:])
    assert fitted.mode == "fitted" and means.shape == (4, 2)
    assert fitted.radii is not None and np.all(fitted.radii > AMP)
    held_out = fitted.decide_window(W[0])
    assert held_out.raw == 0 and held_out.reason == ""
    pred, _ = fitted.decide(W)
    assert pred.tolist() == y.tolist()


def test_recentre_removes_pad_drift():
    corners, W, y = corners_windows(gain_v=1.0)
    g = gaze_decoder()
    g.fit(W, y)
    drifted = board(1.5, (AMP * corners[3][0] + 250.0, AMP * corners[3][1] + 250.0), seed=55)
    assert g.decide_window(drifted).raw != 3    # the drift points the gaze elsewhere
    g.recentre(drifted, 3)                     # a confirmed look at target 3
    assert g.decide_window(drifted).raw == 3
    again = board(1.5, (AMP * corners[0][0] + 250.0, AMP * corners[0][1] + 250.0), seed=56)
    assert g.decide_window(again).raw == 0
    with pytest.raises(ValueError, match="fitted"):
        gaze_decoder().recentre(drifted, 0)


def test_fit_centre_leaves_blinks_out():
    X = board(8.0, seed=8)
    for at in (1.0, 3.0, 5.0, 7.0):
        add_blink(X, at)
    g = GazeDecoder(FS, EOG_MONTAGE)
    centre = g.fit_centre(X)
    assert g.has_centre
    true_v = float(np.mean(DC[[FP1, FP2]]))
    assert centre[1] == pytest.approx(true_v, abs=3.0)
    assert abs(float(X[[FP1, FP2]].mean()) - true_v) > 10.0   # the naive mean is pulled up


# -- validation ---------------------------------------------------------------------------------

def test_constructor_and_input_checks():
    with pytest.raises(KeyError, match="LO"):
        GazeDecoder(FS, MONTAGE)
    with pytest.raises(ValueError, match="Nyquist"):
        GazeDecoder(FS, EOG_MONTAGE, lowpass_hz=200.0)
    with pytest.raises(ValueError, match="margin"):
        GazeDecoder(FS, EOG_MONTAGE, margin=-1.0)
    with pytest.raises(ValueError, match="gain"):
        GazeDecoder(FS, EOG_MONTAGE, gain=(1.0, 0.0))
    g = GazeDecoder(FS, EOG_MONTAGE)
    with pytest.raises(ValueError, match="centre"):
        g.set_targets([(0.0, 0.0), (1.0, 0.0)])
    with pytest.raises(ValueError, match="n_targets >= 2"):
        g.set_targets([(1.0, 0.0)])
    with pytest.raises(ValueError, match="fit the centre first"):
        g.fit(np.zeros((2, 8, 375)), [0, 1])
    with pytest.raises(ValueError, match="board rows"):
        g.features(np.zeros((4, 375)))
    with pytest.raises(ValueError, match="no targets"):
        g.score_uv(np.array([1.0, 0.0]))
    with pytest.raises(ValueError, match=">= 1.5 s"):
        g.fit_centre(np.zeros((8, 100)))
    assert g.decide_window(board(1.5, (AMP, 0.0))).reason == "no_targets"
    g.fit_centre(board(3.0))
    with pytest.raises(ValueError, match="no trial labelled 1"):
        g.fit(np.stack([board(1.5, (AMP, 0)), board(1.5, (0, AMP))]), [0, 2])


# -- streaming and the decoder's prior ---------------------------------------------------------

def test_feed_latest_and_bonus():
    g = gaze_decoder(weight=2.0)
    assert g.latest() is None and g.bonus() is None
    g.set_targets(DIAMOND)
    X = board(1.5, (-AMP, 0.0), seed=9)          # left
    g.feed(X[:, :100])
    assert not g.ready and g.bonus() is None
    g.feed(X[:, 100:])
    assert g.ready
    d = g.latest()
    assert d is not None and d.raw == 1
    bonus = g.bonus()
    assert bonus is not None and bonus[1] == pytest.approx(2.0, abs=0.1)
    g.feed(board(1.5, (3.0, 0.0), seed=11))      # back to the centre: the newest window wins
    assert g.bonus() is None
    g.reset()
    assert not g.ready
    g.feed(np.zeros((8, 0)))                     # an empty read is fine


FREQS = (15.0, 17.0)


def still_board(seconds: float, seed: int = 0, names=EOG_MONTAGE) -> tuple[np.ndarray, np.ndarray]:
    b = FakeBoard(FREQS, seed=seed, t0=1000.0, ch_names=names)
    b.set_target(-1)
    b.advance(seconds)
    return b.read()


def test_decoder_prior_is_added_in_baseline_spreads():
    X_still, _ = still_board(8.0)
    def cca():
        return make("cca", freqs=FREQS, fs=FS, margin=0.5)

    plain = Decoder(cca(), FS, window_s=1.5, step_s=0.5, votes=2, pad_s=1.0,
                    ch_names=EOG_MONTAGE)
    fit_rest(plain, plain.windows(X_still))
    seen: list[int] = []
    biased = Decoder(cca(), FS, window_s=1.5, step_s=0.5, votes=2, pad_s=1.0,
                     ch_names=EOG_MONTAGE,
                     prior=lambda: (seen.append(1), np.array([-3.0, 3.0]))[1])
    biased.classifier.fit_baseline(plain.windows(X_still))
    X, t = still_board(6.0, seed=1)
    a = plain.feed(X, t)
    b = biased.feed(X, t)
    assert len(a) == len(b) == len(seen) > 2
    spread = plain.classifier.baseline_spread
    for da, db in zip(a, b):
        assert db.scores - da.scores == pytest.approx([-3.0 * spread, 3.0 * spread])
        assert db.raw == 1
    assert b[-1].committed == 1
    # None leaves the window untouched.
    neutral = Decoder(cca(), FS, window_s=1.5, step_s=0.5, votes=2, pad_s=1.0,
                      ch_names=EOG_MONTAGE, prior=lambda: None)
    neutral.classifier.fit_baseline(plain.windows(X_still))
    for da, dn in zip(a, neutral.feed(X, t)):
        assert dn.scores == pytest.approx(da.scores) and dn.raw == da.raw
    bad = Decoder(cca(), FS, window_s=1.5, step_s=0.5, pad_s=1.0, ch_names=EOG_MONTAGE,
                  prior=lambda: np.array([1.0, 2.0, 3.0]))
    with pytest.raises(ValueError, match="one per target"):
        bad.feed(X, t)


# -- DecoderBank wiring --------------------------------------------------------------------------

def test_bank_checks_the_gaze_decoder():
    with pytest.raises(ValueError, match="channels"):
        DecoderBank(FS, ch_names=MONTAGE, gaze=GazeDecoder(FS, EOG_MONTAGE))
    with pytest.raises(ValueError, match="Hz"):
        DecoderBank(FS, ch_names=EOG_MONTAGE, gaze=GazeDecoder(125.0, EOG_MONTAGE))
    with pytest.raises(ValueError, match="GazeDecoder"):
        DecoderBank(FS, ch_names=EOG_MONTAGE, gaze=object())  # type: ignore[arg-type]


def stream(bank: DecoderBank, seconds: float, gaze: tuple[float, float], seed: int,
           chunk: float = 0.05) -> list:
    b = FakeBoard(FREQS, seed=seed, t0=2000.0, ch_names=EOG_MONTAGE, clock=None)
    b.set_target(-1)                                  # no SSVEP at all: only the eyes know
    out = []
    for _ in range(int(round(seconds / chunk))):
        b.advance(chunk)
        X, t = b.read()
        X = X + DC
        add_gaze(X, gaze)
        out.extend(bank.feed(X, t))
    return out


def test_bank_gaze_prior_resolves_a_layout_with_positions():
    gaze = GazeDecoder(FS, EOG_MONTAGE, window_s=TUNED.window_s, weight=2.0)
    bank = DecoderBank(FS, ch_names=EOG_MONTAGE, gaze=gaze)
    X_still, _ = still_board(8.0, seed=3)
    bank.calibrate_from(X_still + DC)
    assert gaze.has_centre
    now = 2000.0
    bank.activate(FREQS, (0.0, 0.0), now, positions=[(-1.0, 0.0), (1.0, 0.0)])
    assert bank.decoder is not None and bank.decoder.prior is not None
    decisions = stream(bank, 6.0, (AMP, 0.0), seed=4)      # looking right: target 1
    committed = [d.committed for d in decisions]
    assert 1 in committed and committed[-1] == 1
    assert all(d.raw in (1, ABSTAIN) for d in decisions)
    # Without positions the prior is off: the same look adds nothing to the scores.
    bank.activate(FREQS, (0.0, 0.0), now + 10.0)
    assert bank._gaze_prior() is None and gaze.n_targets == 0
    decisions = stream(bank, 6.0, (AMP, 0.0), seed=5)
    assert not any(d.raw == 1 and d.gap > 3.0 for d in decisions)
    bank.activate(FREQS, (0.0, 0.0), now + 20.0, positions=[(-1.0, 0.0), (1.0, 0.0)])
    bank.deactivate()
    assert bank._gaze_prior() is None
    with pytest.raises(ValueError, match="one position per target"):
        bank.activate(FREQS, (0.0, 0.0), now + 30.0, positions=[(1.0, 0.0)])
    bank.activate((15.0,), (0.0,), now + 40.0, positions=[(1.0, 0.0)])   # one target: no prior
    assert bank._gaze_prior() is None


# -- offline scoring -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def eog_recording(tmp_path_factory):
    freqs = (13.0, 15.0, 17.0, 11.0)
    labels = [0, 1, 2, 3] * 3
    X, t, text = fake_session(freqs, labels, fs=FS, cue_s=1.0, stim_s=3.0, rest_s=2.0,
                              amp=3.0, seed=5)
    for t_on, t_off, k in pair_trials(parse_markers(text)):
        sl = slice(int(np.searchsorted(t, t_on)), int(np.searchsorted(t, t_off)))
        x, y = DIAMOND[k]
        add_gaze(X, (AMP * x, AMP * y), sl=sl)
    path = tmp_path_factory.mktemp("eog") / "2026-10-25_100000_cued.npz"
    save_recording(path, X, t, FS, EOG_MONTAGE, freqs, text, window_s=2.86, latency=0.14)
    return load_recording(path)


def test_raw_windows_match_cut_windows(eog_recording):
    raw = raw_windows(eog_recording, 1.5)
    win = cut_windows(eog_recording, 1.5)
    assert raw.on.shape == win.on.shape and raw.off.shape == win.off.shape
    assert raw.labels.tolist() == win.labels.tolist() == [0, 1, 2, 3] * 3
    # Raw windows keep the board's DC offset; the filtered ones are demeaned.
    assert abs(raw.on.mean()) > 1000.0 and abs(win.on.mean()) < 5.0
    with pytest.raises(ValueError, match="positive"):
        raw_windows(eog_recording, 0.0)


def test_score_gaze_layout_and_fitted(eog_recording):
    s = score_gaze(eog_recording, DIAMOND, window_s=1.5)
    assert s.n == 12 and s.gaze_hits == 12 and s.gaze_abstain == 0
    assert s.fused_hits >= s.ssvep_hits and s.fused_abstain <= s.ssvep_abstain
    held_out = score_gaze(eog_recording, None, window_s=1.5)
    assert held_out.gaze_hits == 12
    text = eog.format_score(s)
    assert "gaze" in text and "fused" in text and "12 trials" in text
    assert s.rate(6) == pytest.approx(0.5)


def test_score_gaze_needs_the_eog_channels(tmp_path):
    X, t, text = fake_session((15.0, 17.0), [0, 1, 0, 1], fs=FS, seed=1)
    path = save_recording(tmp_path / "plain_cued.npz", X, t, FS, MONTAGE, (15.0, 17.0), text)
    with pytest.raises(ValueError, match="no EOG channels"):
        score_gaze(load_recording(path))
    assert eog.main([str(path)]) == 1


def test_cli(eog_recording, capsys):
    assert eog.main([str(eog_recording.path), "--windows", "1.5",
                     "--positions", "0,1;-1,0;1,0;0,-1"]) == 0
    out = capsys.readouterr().out
    assert "gaze  : 100.0 % correct" in out
    assert eog.main([str(eog_recording.path), "--positions", "0,1;bad"]) == 2
    assert eog._parse_positions(None) is None


def test_four_eog_config_is_a_two_by_two_grid():
    cfg = load_config("four_eog")
    assert cfg.n_targets == 4 and list(cfg.freqs) == [13.0, 15.0, 17.0, 11.0]
    pos = [t.pos for t in cfg.targets]
    assert pos[0] == (-0.3, 0.3) and pos[1] == (0.3, 0.3)
    assert pos[2] == (-0.3, -0.3) and pos[3] == (0.3, -0.3)
    assert cfg.waveform == "sine" and cfg.hold_cue
