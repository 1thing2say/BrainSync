"""Tests for the server's DecoderBank: shared history, decoder cache, onset gating, re-arm."""
from __future__ import annotations

import math

import numpy as np
import pytest

from brainsync.board import FS, FakeBoard
from brainsync.classify import ABSTAIN, make
from brainsync.online import TUNED, Decoder
from brainsync.recordings import MONTAGE, POSTERIOR
from brainsync.server.decoding import DecoderBank, build_decoder
from brainsync.server.protocol import LATENCY_S

FREQS = (15.0, 17.0, 12.0, 20.0)
T0 = 1_000.0


def still(seconds: float = 6.0, seed: int = 0) -> np.ndarray:
    """Board rows of a still screen (no tone)."""
    b = FakeBoard(FREQS, seed=seed, t0=T0)
    b.set_target(-1)
    b.advance(seconds)
    return b.read()[0]


@pytest.fixture(scope="module")
def calib() -> np.ndarray:
    return still()


def make_bank(calib: np.ndarray | None = None, **kw) -> DecoderBank:
    bank = DecoderBank(**kw)
    if calib is not None:
        bank.calibrate_from(calib)
    return bank


def stream(bank: DecoderBank, board: FakeBoard, seconds: float, chunk: float = 0.05) -> list:
    out = []
    for _ in range(int(round(seconds / chunk))):
        board.advance(chunk)
        out.extend(bank.feed(*board.read()))
    return out


# -- recipe and construction --------------------------------------------------------------

def test_build_decoder_matches_the_swipe_web_recipe():
    from brainsync.swipe_web import build_decoder as swipe_build
    ours, theirs = build_decoder((15.0, 17.0), (0.0, 0.0)), swipe_build((15.0, 17.0), (0.0, 0.0))
    for attr in ("fs", "window_s", "step_s", "pad_s", "n_window", "n_step", "needed", "votes",
                 "ch_names", "band", "notch_hz"):
        assert getattr(ours, attr) == getattr(theirs, attr), attr
    assert ours.rows == theirs.rows and ours.channel_names == tuple(POSTERIOR)
    for attr in ("name", "harmonics", "margin", "threshold"):
        assert getattr(ours.classifier, attr) == getattr(theirs.classifier, attr), attr


def test_new_bank_is_idle_and_uncalibrated():
    bank = DecoderBank()
    assert not bank.calibrated and bank.active is None and bank.decoder is None
    assert bank.gate == math.inf and bank.votes == TUNED.votes and bank.n_history == 0
    # two 1.5 s windows at a 0.25 s hop (62 samples) plus the 2 s pad
    assert bank.min_calibrate_s == pytest.approx((375 + 500 + 62) / FS)


@pytest.mark.parametrize("kw, match", [
    ({"fs": 0}, "fs must be"), ({"fs": math.nan}, "fs must be"),
    ({"latency_s": -0.1}, "latency_s"), ({"latency_s": math.inf}, "latency_s"),
    ({"ch_names": ("a", "a")}, "unique"), ({"ch_names": ()}, "unique"),
])
def test_constructor_rejects_bad_arguments(kw, match):
    with pytest.raises(ValueError, match=match):
        DecoderBank(**kw)


def test_factory_output_is_checked():
    def wrong_fs(f, p):
        clf = make("cca", freqs=f, fs=500.0, phases=p)
        return Decoder(clf, 500.0, window_s=1.5, step_s=0.25)

    with pytest.raises(ValueError, match="500 Hz but the bank runs at 250"):
        DecoderBank(factory=wrong_fs)
    with pytest.raises(ValueError, match="online.Decoder"):
        DecoderBank(factory=lambda f, p: object())
    with pytest.raises(ValueError, match="channels do not fit"):
        DecoderBank(ch_names=[f"c{i}" for i in range(8)])     # no POSTERIOR channel among them
    with pytest.raises(ValueError, match="pass ch_names"):
        DecoderBank(ch_names=[f"c{i}" for i in range(8)],
                    factory=lambda f, p: build_decoder(f, p))  # MONTAGE names, not the board's
    with pytest.raises(ValueError, match="targets for 1 frequencies"):
        DecoderBank(factory=lambda f, p: build_decoder((10.0, 11.0), (0.0, 0.0)))


# -- calibration --------------------------------------------------------------------------

def test_activate_needs_calibration():
    with pytest.raises(ValueError, match="calibrate_from"):
        DecoderBank().activate((15.0,), (0.0,), T0)


@pytest.mark.parametrize("X, match", [
    (np.zeros((7, 2000)), "board rows"), (np.zeros(2000), "board rows"),
    (np.full((8, 2000), np.nan), "NaN"),
])
def test_calibrate_from_rejects_bad_shapes(X, match):
    with pytest.raises(ValueError, match=match):
        DecoderBank().calibrate_from(X)


def test_too_short_calibration_keeps_the_old_baselines(calib):
    bank = make_bank(calib)
    bank.activate(FREQS[:2], (0.0, 0.0), T0)
    clf = bank.decoder.classifier
    mean, spread = clf.baseline_mean.copy(), clf.baseline_spread
    with pytest.raises(ValueError, match="calibrate for longer"):
        bank.calibrate_from(calib[:, :900])
    assert np.array_equal(clf.baseline_mean, mean) and clf.baseline_spread == spread
    assert bank.calibrated


def test_calibrate_from_refits_existing_decoders(calib):
    bank = make_bank(calib)
    bank.activate(FREQS[:2], (0.0, 0.0), T0)
    clf = bank.decoder.classifier
    before = clf.baseline_mean.copy()
    bank.calibrate_from(still(seed=7))
    assert clf.has_baseline and not np.allclose(clf.baseline_mean, before)
    assert bank.calibration is not None and bank.calibration.n_windows >= 2


def test_calibration_data_is_copied(calib):
    X = calib.copy()
    bank = make_bank(X)
    X[:] = 0.0                                   # the caller reuses its buffer
    bank.activate(FREQS[:2], (0.0, 0.0), T0)     # the fit uses the bank's own copy
    assert bank.decoder.classifier.has_baseline


# -- activate / cache ---------------------------------------------------------------------

def test_decoders_are_cached_per_frequency_set(calib):
    calls = []

    def factory(f, p):
        calls.append((f, p))
        return build_decoder(f, p)

    bank = make_bank(calib, factory=factory)
    assert len(calls) == 1                       # the probe
    bank.activate((15.0, 17.0), (0.0, 0.0), T0)
    first = bank.decoder
    bank.activate((15.0, 17.0, 12.0), (0.0, 0.0, 0.0), T0)
    bank.activate([15.0, 17.0], [0, 0], T0)
    assert bank.decoder is first and len(calls) == 3
    bank.activate((15.0, 17.0), (0.0, 1.0), T0)   # another phase is another decoder
    assert bank.decoder is not first and len(calls) == 4
    assert bank.active == (15.0, 17.0)


@pytest.mark.parametrize("freqs, phases, match", [
    ((), (), "at least one frequency"),
    ((15.0, 17.0), (0.0,), "one phase per frequency"),
    ((15.0, 15.0), (0.0, 0.0), "unique"),
    ((15.0, 130.0), (0.0, 0.0), "lie in"),
    ((15.0, math.nan), (0.0, 0.0), "lie in"),
    ((15.0,), (math.inf,), "phases must be finite"),
    (("x",), (0.0,), "numbers"),
])
def test_activate_rejects_bad_targets(calib, freqs, phases, match):
    with pytest.raises(ValueError, match=match):
        make_bank(calib).activate(freqs, phases, T0)


def test_activate_rejects_a_nan_time(calib):
    with pytest.raises(ValueError, match="finite"):
        make_bank(calib).activate((15.0,), (0.0,), math.nan)


def test_history_fills_while_idle_and_is_capped(calib):
    bank = make_bank(calib)
    board = FakeBoard(FREQS, seed=3, t0=T0)
    assert stream(bank, board, 5.0) == []
    assert bank.n_history == build_decoder(FREQS, (0,) * 4).needed


def test_activate_primes_from_history_without_a_decision(calib):
    bank = make_bank(calib)
    board = FakeBoard(FREQS, seed=3, t0=T0)
    stream(bank, board, 5.0)
    bank.activate(FREQS[:2], (0.0, 0.0), board.now)
    dec = bank.decoder
    assert dec.n_seen == dec.needed - 1 and dec.committed == ABSTAIN
    board.advance(1 / FS)                         # one sample: the first decision falls due
    bank.feed(*board.read())
    assert dec.n_seen == dec.needed and bank.dropped == 1   # before the gate: dropped


def test_bigger_decoder_grows_the_history(calib):
    def factory(f, p):
        clf = make("fbcca", freqs=f, fs=FS, phases=p)
        window = 2.5 if len(f) == 3 else 1.5
        return Decoder(clf, FS, window_s=window, step_s=0.25, votes=2, pad_s=2.0)

    bank = make_bank(still(8.0), factory=factory)
    board = FakeBoard(FREQS, seed=3, t0=T0)
    stream(bank, board, 4.0)
    assert bank.n_history == 875
    bank.activate(FREQS[:3], (0.0,) * 3, board.now)
    stream(bank, board, 3.0)
    assert bank.n_history == 1125
    assert bank.min_calibrate_s == pytest.approx((1125 + 62) / FS)


# -- feed ---------------------------------------------------------------------------------

def test_feed_checks_its_input(calib):
    bank = make_bank(calib)
    with pytest.raises(ValueError, match="board rows"):
        bank.feed(np.zeros((3, 10)), np.zeros(10))
    with pytest.raises(ValueError, match="timestamps"):
        bank.feed(np.zeros((8, 10)), np.zeros(9))
    assert bank.feed(np.zeros((8, 0)), np.zeros(0)) == []


def test_every_returned_decision_starts_after_the_gate(calib):
    bank = make_bank(calib)
    board = FakeBoard(FREQS, seed=4, t0=T0)
    board.set_target(0)
    stream(bank, board, 4.0)
    t_act = board.now
    bank.activate(FREQS, (0.0,) * 4, t_act)
    assert bank.gate == pytest.approx(t_act + LATENCY_S)
    ds = stream(bank, board, 6.0)
    assert ds and bank.dropped > 0
    window = bank.decoder.window_s
    assert all(d.time - window >= bank.gate for d in ds)
    # the first counted window is the first one whose start is at or after the gate
    assert ds[0].time - window < bank.gate + bank.decoder.n_step / FS


def test_pre_onset_windows_never_vote(calib):
    """The tone was on long before the layout; without gating the primed decoder would
    commit 4 windows (~1 s) after activate. With gating the commit needs 4 windows that all
    start after the gate."""
    bank = make_bank(calib)
    board = FakeBoard(FREQS, seed=5, t0=T0)
    board.set_target(1)
    stream(bank, board, 6.0)
    bank.activate(FREQS, (0.0,) * 4, board.now)
    gate = bank.gate
    ds = stream(bank, board, 6.0)
    commits = [d for d in ds if d.changed and d.committed != ABSTAIN]
    assert commits and commits[0].committed == 1
    dec = bank.decoder
    earliest = gate + dec.window_s + (dec.votes - 1) * dec.n_step / FS
    assert commits[0].time >= earliest - 1e-9
    assert commits[0].time < earliest + 2 * dec.n_step / FS     # and not much later


def test_one_big_read_gates_like_many_small_ones(calib):
    """Votes from before the gate must not leak into the first counted windows even when a
    single read spans the gate."""
    def run(chunk: float) -> list[tuple[float, int, bool]]:
        bank = make_bank(calib)
        board = FakeBoard(FREQS, seed=6, t0=T0)
        board.set_target(2)
        stream(bank, board, 4.0)
        bank.activate(FREQS, (0.0,) * 4, board.now)
        ds = stream(bank, board, 6.0, chunk=chunk)
        return [(round(d.time, 6), d.committed, d.changed) for d in ds]

    small, big = run(0.02), run(3.0)
    assert small == big
    assert any(c == 2 and ch for _, c, ch in small)


def test_set_onset_moves_the_gate(calib):
    bank = make_bank(calib)
    board = FakeBoard(FREQS, seed=8, t0=T0)
    board.set_target(0)
    stream(bank, board, 4.0)
    t_act = board.now
    bank.activate(FREQS, (0.0,) * 4, t_act)
    bank.set_onset(t_act + 0.5)
    assert bank.gate == pytest.approx(t_act + 0.5 + LATENCY_S)
    ds = stream(bank, board, 5.0)
    assert ds and all(d.time - bank.decoder.window_s >= t_act + 0.5 + LATENCY_S for d in ds)


def test_a_later_onset_undoes_counted_votes(calib):
    bank = make_bank(calib)
    board = FakeBoard(FREQS, seed=8, t0=T0)
    board.set_target(0)
    stream(bank, board, 4.0)
    bank.activate(FREQS, (0.0,) * 4, board.now)
    stream(bank, board, 2.2)                     # a couple of counted windows
    dec = bank.decoder
    assert len(dec.voter._recent) > 0
    bank.set_onset(board.now)                    # later than the activation gate
    assert len(dec.voter._recent) == 0
    bank.set_onset(board.now - 1.0)              # earlier: counted votes stay valid
    dec.voter.push(0)
    bank.set_onset(board.now - 2.0)
    assert len(dec.voter._recent) == 1


def test_set_onset_errors(calib):
    bank = make_bank(calib)
    with pytest.raises(ValueError, match="activate"):
        bank.set_onset(T0)
    bank.activate((15.0,), (0.0,), T0)
    with pytest.raises(ValueError, match="finite"):
        bank.set_onset(math.inf)


def test_deactivate_stops_decisions_but_keeps_the_history(calib):
    bank = make_bank(calib)
    board = FakeBoard(FREQS, seed=9, t0=T0)
    board.set_target(0)
    stream(bank, board, 4.0)
    bank.activate(FREQS, (0.0,) * 4, board.now)
    assert stream(bank, board, 3.0)
    bank.deactivate()
    assert bank.active is None and bank.gate == math.inf
    assert stream(bank, board, 2.0) == []
    assert bank.n_history == bank._hist.capacity


def test_rearm_resets_only_the_voter(calib):
    bank = make_bank(calib)
    bank.rearm()                                  # nothing active: no error
    board = FakeBoard(FREQS, seed=10, t0=T0)
    board.set_target(3)
    stream(bank, board, 4.0)
    bank.activate(FREQS, (0.0,) * 4, board.now)
    ds = stream(bank, board, 4.0)
    dec = bank.decoder
    assert dec.committed == 3
    seen = dec.n_seen
    bank.rearm()
    assert dec.committed == ABSTAIN and dec.n_seen == seen      # buffer untouched
    more = stream(bank, board, 1.5)
    recommit = [d for d in more if d.changed and d.committed == 3]
    assert recommit and recommit[0].time - more[0].time == pytest.approx(
        (dec.votes - 1) * dec.n_step / FS)
    assert ds


def test_default_channel_names_are_the_montage():
    assert DecoderBank().ch_names == tuple(MONTAGE)


# -- trained classifiers --------------------------------------------------------------------

from brainsync.classify import ETRCA  # noqa: E402
from brainsync.signal import preprocess  # noqa: E402
from brainsync.synthetic import ssvep_trials  # noqa: E402


def trained_model(periodic: bool | None = None):
    X, y = ssvep_trials(FREQS, FS, n_per_target=4, seconds=2.0, snr_db=0.0, seed=3)
    return make("etrca", freqs=FREQS, fs=FS, periodic=periodic).fit(preprocess(X, FS), y)


def test_build_decoder_with_a_trained_model():
    model = trained_model()
    dec = build_decoder((17.0, 15.0), (0.0, 0.0), trained=model)
    clf = dec.classifier
    assert isinstance(clf, ETRCA) and clf.freqs.tolist() == [17.0, 15.0]
    assert clf.margin == TUNED.margin and clf.threshold == TUNED.threshold
    assert dec.uses_offsets and dec.window_s == TUNED.window_s
    assert model.n_targets == 4                          # the model itself is untouched
    with pytest.raises(ValueError, match="not one of the trained"):
        build_decoder((15.0, 11.0), (0.0, 0.0), trained=model)
    with pytest.raises(ValueError, match="phases"):
        build_decoder((15.0, 17.0), (0.0, 1.0), trained=model)
    with pytest.raises(ValueError, match="Hz but the board"):
        build_decoder((15.0,), (0.0,), fs=256.0, trained=model)
    raw = trained_model(periodic=False)                  # 500-sample templates, 375-sample window
    with pytest.raises(ValueError, match="templates are 500 samples"):
        build_decoder((15.0, 17.0), (0.0, 0.0), trained=raw)


def test_bank_with_trained_model_aligns_to_the_onset(calib):
    model = trained_model()
    bank = make_bank(calib, trained=model)
    assert bank.calibrated and bank.trained is model   # the probe is a plain CCA decoder
    bank.activate((15.0, 17.0), (0.0, 0.0), now=T0 + 6.0)
    dec = bank.decoder
    assert isinstance(dec.classifier, ETRCA) and dec.classifier.has_baseline
    assert dec.aligned and dec._align == pytest.approx(T0 + 6.0 + LATENCY_S)
    bank.set_onset(T0 + 6.5)
    assert dec._align == pytest.approx(T0 + 6.5 + LATENCY_S)
    board = FakeBoard(FREQS, seed=1, t0=T0 + 6.0)
    board.set_target(0)
    out = stream(bank, board, 4.0)
    assert out and all(d.scores.shape == (2,) for d in out)
    with pytest.raises(ValueError, match="either a decoder factory or a trained"):
        DecoderBank(factory=lambda f, p: build_decoder(f, p), trained=model)


def test_learn_adds_a_confirmed_trial_and_rebuilds_the_decoder(calib):
    model = trained_model()
    bank = make_bank(calib, trained=model)
    assert len(bank._hist._data[0]) >= int((2.0 + 2.0 + 20.0) * FS)   # template + pad + 20 s
    board = FakeBoard(FREQS, seed=2, t0=T0 + 6.0)
    board.set_target(-1)
    bank.activate((15.0, 17.0), (0.0, 0.0), now=T0 + 6.0)
    stream(bank, board, 3.0)                                 # still screen fills the pad
    bank.set_onset(T0 + 9.0)
    board.set_target(0)
    stream(bank, board, 5.0)
    old = bank.decoder
    before = model.n_train.copy()
    assert bank.learn(15.0, bank.gate) is True
    assert model.n_train.tolist() == [before[0] + 1, *before[1:]]
    new = bank.decoder
    assert new is not old and new.classifier.has_baseline and new.aligned
    assert new._align == pytest.approx(bank.gate) and new.classifier.freqs.tolist() == [15.0, 17.0]
    out = stream(bank, board, 2.0)
    assert out and all(d.scores.shape == (2,) for d in out)
    assert bank.learn(15.0, T0 - 100.0) is False             # before any history
    assert bank.learn(17.0, T0 + 6.0) is False               # no pad before the first sample
    with pytest.raises(ValueError, match="not one of the trained"):
        bank.learn(11.0, bank.gate)
    with pytest.raises(ValueError, match="finite"):
        bank.learn(15.0, math.nan)
    with pytest.raises(ValueError, match="no trained classifier"):
        make_bank(calib).learn(15.0, T0)


def test_learn_refuses_a_window_that_reaches_past_the_commit(calib):
    """The learnt window must end before the pick's commit, else it holds the next screen."""
    model = trained_model()                                  # 2 s templates
    bank = make_bank(calib, trained=model)
    board = FakeBoard(FREQS, seed=4, t0=T0 + 6.0)
    board.set_target(-1)
    bank.activate((15.0, 17.0), (0.0, 0.0), now=T0 + 6.0)
    stream(bank, board, 3.0)
    bank.set_onset(T0 + 9.0)
    board.set_target(0)
    stream(bank, board, 5.0)
    gate = bank.gate
    assert bank.learn(15.0, gate, t_end=gate + 1.5) is False     # committed too early: not learnt
    assert model.n_train.tolist() == [4, 4, 4, 4]
    assert bank.learn(15.0, gate, t_end=gate + 2.5) is True      # the window fits before the commit
    assert model.n_train.tolist() == [5, 4, 4, 4]
