"""Tests for the real-time decoder, calibration and recorded-session replay."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from brainsync import online
from brainsync.board import FakeBoard
from brainsync.classify import ABSTAIN, make
from brainsync.online import (TUNED, Decoder, RingBuffer, Voter, calibrate, consistency, fit_rest,
                              replay, replay_grid)
from brainsync.paths import recordings_dir
from brainsync.recordings import MONTAGE, load_recording, save_recording
from brainsync.signal import preprocess
from brainsync.synthetic import fake_session

FS = 250.0
FREQS = (15.0, 17.0)


def make_decoder(margin: float = 0.0, window_s: float = 2.0, step_s: float = 0.5, votes: int = 3,
                 pad_s: float = 1.0, name: str = "cca", **kw) -> Decoder:
    clf = make(name, freqs=FREQS, fs=FS, margin=margin)
    return Decoder(clf, FS, window_s=window_s, step_s=step_s, votes=votes, pad_s=pad_s, **kw)


def board_data(seconds: float, seed: int = 0, target: int | None = 0, amp: float = 2.0):
    board = FakeBoard(FREQS, amp=amp, seed=seed, switch_s=1e9)
    board.set_target(target)
    board.advance(seconds)
    return board.read()


# -- RingBuffer --------------------------------------------------------------------------

def test_ring_buffer_last_matches_stream_across_wraparound(rng):
    buf = RingBuffer(3, 50)
    stream = np.empty((3, 0))
    for size in rng.integers(0, 37, 40):
        chunk = rng.standard_normal((3, int(size)))
        buf.append(chunk)
        stream = np.concatenate([stream, chunk], axis=1)
        assert len(buf) == min(50, stream.shape[1])
        assert buf.total == stream.shape[1]
        for n in (0, 1, len(buf) // 2, len(buf)):
            np.testing.assert_array_equal(buf.last(n), stream[:, stream.shape[1] - n:])


def test_ring_buffer_chunk_larger_than_capacity_and_1d():
    buf = RingBuffer(1, 4)
    buf.append(np.arange(3.0))
    buf.append(np.arange(10.0, 20.0))
    np.testing.assert_array_equal(buf.last(4), [[16, 17, 18, 19]])
    buf.append(np.array([[7.0, 8.0]]))
    np.testing.assert_array_equal(buf.last(4), [[18, 19, 7, 8]])


def test_ring_buffer_errors():
    buf = RingBuffer(2, 10)
    buf.append(np.zeros((2, 3)))
    with pytest.raises(ValueError, match="last 4"):
        buf.last(4)
    with pytest.raises(ValueError, match=r"\(2, n\)"):
        buf.append(np.zeros((3, 5)))
    with pytest.raises(ValueError):
        RingBuffer(0, 5)
    buf.clear()
    assert len(buf) == 0 and buf.total == 0


# -- Voter -------------------------------------------------------------------------------

def test_voter_commits_on_consecutive_agreement_and_releases_on_abstain():
    v = Voter(3)
    assert [v.push(k) for k in (0, 0, 1, 0, 0)] == [False] * 5
    assert v.committed == ABSTAIN
    assert v.push(0) is True and v.committed == 0
    assert v.push(0) is False
    assert [v.push(k) for k in (1, 1, 1)] == [False, False, True] and v.committed == 1
    assert [v.push(ABSTAIN) for _ in range(3)] == [False, False, True]
    assert v.committed == ABSTAIN
    with pytest.raises(ValueError, match="votes"):
        Voter(0)


# -- Decoder -----------------------------------------------------------------------------

def test_no_decision_before_window_plus_pad_is_buffered():
    dec = make_decoder(window_s=2.0, pad_s=1.0)
    X, t = board_data(5.0)
    need = dec.needed
    assert need == 750
    assert dec.feed(X[:, :need - 1], t[:need - 1]) == []
    assert dec.decide() is None
    out = dec.feed(X[:, need - 1:need], t[need - 1:need])
    assert len(out) == 1 and out[0].end == need and out[0].time == t[need - 1]
    # then one decision every step
    out = dec.feed(X[:, need:need + 3 * dec.n_step], t[need:need + 3 * dec.n_step])
    assert [d.end for d in out] == [need + k * dec.n_step for k in (1, 2, 3)]


def test_decisions_do_not_depend_on_chunking(rng):
    X, t = board_data(12.0, seed=3)
    one = make_decoder().feed(X, t)
    dec = make_decoder()
    many = []
    pos = 0
    while pos < X.shape[1]:
        size = int(rng.integers(1, 60))
        many += dec.feed(X[:, pos:pos + size], t[pos:pos + size])
        pos += size
    assert [d.end for d in one] == [d.end for d in many]
    np.testing.assert_allclose([d.scores for d in one], [d.scores for d in many])
    assert [d.committed for d in one] == [d.committed for d in many]


def test_window_is_preprocessed_with_pad_and_pad_dropped():
    dec = make_decoder(window_s=2.0, pad_s=1.0, notch_hz=60.0)
    X, t = board_data(4.0, seed=5)
    X = X + np.linspace(-3000, 3000, X.shape[1])  # slow drift, so the pad matters
    d = dec.feed(X, t)[-1]
    seg = X[dec.rows, d.end - dec.needed:d.end]
    expected = preprocess(seg, FS, band=(1.0, 100.0), notch_hz=60.0)[:, dec.n_pad:]
    np.testing.assert_allclose(d.scores, dec.classifier.scores(expected)[0])
    no_pad = preprocess(seg[:, dec.n_pad:], FS, band=(1.0, 100.0), notch_hz=60.0)
    assert not np.allclose(dec.prepare(seg), no_pad)
    assert dec.prepare(seg).shape == (4, dec.n_window)


def test_decoder_uses_named_channels():
    dec = make_decoder(channels=("O1", "O2"))
    assert dec.rows == [6, 7] and dec.channel_names == ("O1", "O2")
    with pytest.raises(KeyError, match="Oz"):
        make_decoder(channels=("Oz",))


def test_decoder_rejects_bad_settings():
    clf = make("cca", freqs=FREQS, fs=FS)
    with pytest.raises(ValueError, match="256"):
        Decoder(clf, 256.0)
    with pytest.raises(ValueError, match="too short"):
        Decoder(clf, FS, window_s=0.02)
    with pytest.raises(ValueError, match="step_s"):
        Decoder(clf, FS, step_s=0)
    with pytest.raises(ValueError, match="votes"):
        Decoder(clf, FS, votes=0)
    dec = Decoder(clf, FS)
    with pytest.raises(ValueError, match=r"\(8, n\)"):
        dec.feed(np.zeros((4, 10)))
    with pytest.raises(ValueError, match="timestamps"):
        dec.feed(np.zeros((8, 10)), np.zeros(9))


def run_board(dec: Decoder, board: FakeBoard, seconds: float, chunk: float = 0.1):
    """(decision, true target) pairs; chunks are step-aligned, so a chunk's first sample
    carries the target of every window ending in it."""
    out = []
    for _ in range(int(round(seconds / chunk))):
        truth = board.active_target()
        board.advance(chunk)
        out += [(d, truth) for d in dec.step(board)]
    return out


def test_decoder_on_fake_board_commits_and_follows_a_switch():
    board = FakeBoard(FREQS, switch_s=10.0, seed=1)
    dec = make_decoder(margin=0.5, window_s=2.0, step_s=0.5, votes=3, pad_s=2.0, name="fbcca")
    board.set_target(ABSTAIN)
    calibrate(dec, board, 10.0)
    board.set_target(None)                    # the schedule resumes at 10 s: target 1
    board.read()
    log = run_board(dec, board, 20.0)
    by_block: dict[int, list] = {}
    for d, truth in log:
        by_block.setdefault(truth, []).append(d)
    assert set(by_block) == {0, 1}
    for target, ds in by_block.items():
        assert ds[-1].committed == target, f"block of target {target} ended on {ds[-1].committed}"
        first = next(k for k, d in enumerate(ds) if d.committed == target)
        assert first <= 8, f"target {target} took {first} windows to commit"
    changes = [d.committed for d, _ in log if d.changed]
    assert changes[:2] == [1, 0]


def test_huge_margin_never_commits():
    board = FakeBoard(FREQS, switch_s=5.0, amp=6.0, seed=2)
    dec = make_decoder(margin=1e6)
    board.set_target(ABSTAIN)
    calibrate(dec, board, 6.0)
    board.set_target(None)
    log = run_board(dec, board, 15.0)
    assert len(log) > 10
    assert all(d.raw == ABSTAIN and d.committed == ABSTAIN and not d.changed for d, _ in log)


# -- calibration -------------------------------------------------------------------------

class SpyBoard(FakeBoard):
    """FakeBoard that keeps a copy of everything read from it."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.seen: list[np.ndarray] = []

    def read(self):
        X, t = super().read()
        self.seen.append(X)
        return X, t


def test_calibrate_fits_baseline_on_overlapping_online_windows():
    board = SpyBoard(FREQS, seed=4)
    board.set_target(ABSTAIN)
    dec = make_decoder(window_s=2.0, step_s=0.5, pad_s=1.0)
    assert not dec.classifier.has_baseline
    cal = calibrate(dec, board, 10.0, reject=0)
    n = int(10.0 * FS)
    assert cal.n_windows == (n - dec.needed) // dec.n_step + 1 == 15
    assert dec.classifier.has_baseline and dec.classifier.baseline_spread > 0
    assert dec.n_seen == 0                      # the decoder's own buffer is untouched
    # same baseline as fitting the classifier on the same data, prepared like online windows
    X = np.concatenate(board.seen, axis=1)
    assert X.shape[1] == n
    ref = make("cca", freqs=FREQS, fs=FS).fit_baseline(dec.windows(X))
    np.testing.assert_allclose(dec.classifier.baseline_mean, ref.baseline_mean)
    assert dec.classifier.baseline_spread == pytest.approx(ref.baseline_spread)


def test_calibrate_too_short_raises():
    dec = make_decoder(window_s=2.0, step_s=0.5, pad_s=1.0)
    with pytest.raises(ValueError, match="3.5"):
        calibrate(dec, FakeBoard(FREQS), 3.0)


def test_fit_rest_drops_loud_windows():
    dec = make_decoder()
    X, _ = board_data(10.0, target=ABSTAIN)
    W = dec.windows(X)
    W[2] *= 50
    cal = fit_rest(dec, W)
    assert cal.n_rejected == 1 and cal.n_windows == W.shape[0] - 1
    with pytest.raises(ValueError, match="at least 2"):
        fit_rest(dec, W[:1])


# -- replay ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def synthetic_recording(tmp_path_factory) -> Path:
    labels = [0, 1] * 6
    X, t, text = fake_session(FREQS, labels, amp=3.0, seed=7)
    path = tmp_path_factory.mktemp("rec") / "2026-01-01_000000_cued.npz"
    return save_recording(path, X, t, FS, MONTAGE, FREQS, text)


def test_replay_synthetic_session(synthetic_recording):
    res = replay(synthetic_recording, classifier="cca", window_s=2.0, step_s=0.25, votes=3,
                 margin=0.5)
    assert res.n_trials == 12
    assert res.correct >= 10 and res.wrong <= 1
    assert 0.0 < res.median_delay_s < 4.0
    assert 0.0 <= res.abstain_rate <= 1.0
    assert res.calibration.n_windows > 20
    lines = res.report()
    assert "12 trials" in lines[0] and len(lines) == 4 + 12
    agree = consistency(res)
    assert agree["same"][0] > 20 and agree["same"][1] == 1.0
    assert agree["whole"][1] > 0.8


def test_replay_grid_matches_replay(synthetic_recording):
    grid = replay_grid(synthetic_recording, votes=[2, 4], margins=[0.0, 1.0],
                       thresholds=[None, 0.5], classifier="fbcca", window_s=1.5, step_s=0.25)
    assert len(grid) == 8
    for votes, margin, threshold in [(2, 0.0, None), (4, 1.0, None), (2, 1.0, 0.5), (4, 0.0, 0.5)]:
        res = grid[(votes, margin, threshold)]
        ref = replay(synthetic_recording, classifier="fbcca", window_s=1.5, step_s=0.25,
                     votes=votes, margin=margin, threshold=threshold)
        assert res.params["threshold"] == threshold
        assert [o.committed for o in res.trials] == [o.committed for o in ref.trials]
        assert [d.raw for d in res.decisions] == [d.raw for d in ref.decisions]
        assert [d.committed for d in res.decisions] == [d.committed for d in ref.decisions]
        assert res.still_commits == ref.still_commits


def test_replay_ignores_commitment_left_from_previous_trial(synthetic_recording):
    # every trial must earn its own commit: with an impossible margin nothing is committed
    res = replay(synthetic_recording, classifier="cca", margin=1e6)
    assert res.correct == 0 and res.wrong == 0
    assert all(o.committed == ABSTAIN and np.isnan(o.delay_s) for o in res.trials)


def test_replay_custom_factory_and_chunk_size(synthetic_recording):
    def factory(rec):
        clf = make("cca", freqs=rec.freqs, fs=rec.fs, margin=0.0)
        return Decoder(clf, rec.fs, window_s=2.0, step_s=0.5, votes=2, pad_s=1.0,
                       ch_names=rec.ch_names)
    a = replay(synthetic_recording, factory, chunk_s=0.1)
    b = replay(synthetic_recording, factory, chunk_s=0.37)
    assert [d.raw for d in a.decisions] == [d.raw for d in b.decisions]
    assert a.params["votes"] == 2 and a.params["window_s"] == 2.0


def test_replay_requires_trials(tmp_path):
    X, t, _ = fake_session(FREQS, [0, 1], seed=1)
    path = save_recording(tmp_path / "x_cued.npz", X, t, FS, MONTAGE, FREQS, "no markers\n")
    with pytest.raises(ValueError, match="no complete"):
        replay(path)


def test_cli_replays_and_reports(synthetic_recording, capsys):
    assert online.main([str(synthetic_recording), "--classifier", "cca", "--trials"]) == 0
    out = capsys.readouterr().out
    assert "12 trials" in out and "offline agreement" in out and "trial 11" in out
    assert online.main([str(synthetic_recording.with_name("missing.npz"))]) == 1


def test_tuned_defaults_are_the_documented_ones():
    assert (TUNED.classifier, TUNED.window_s, TUNED.step_s, TUNED.votes, TUNED.margin,
            TUNED.threshold) == ("fbcca", 1.5, 0.25, 4, 0.5, 0.5)


def _tuned_decoder(threshold=TUNED.threshold) -> Decoder:
    clf = make(TUNED.classifier, freqs=FREQS, fs=FS, margin=TUNED.margin, threshold=threshold)
    return Decoder(clf, FS, window_s=TUNED.window_s, step_s=TUNED.step_s, votes=TUNED.votes,
                   pad_s=2.0)


def test_tuned_decoder_mostly_abstains_without_a_target():
    # regression: the 0.5-spread lead alone abstained on only ~29% of rest windows, so the
    # demo kept committing with nobody looking; the level threshold must hold it back
    rest_X, rest_t = board_data(90.0, seed=11, target=ABSTAIN)
    rates = {}
    for thr in (None, TUNED.threshold):
        dec = _tuned_decoder(thr)
        cal = FakeBoard(FREQS, seed=12)
        cal.set_target(ABSTAIN)
        calibrate(dec, cal, 60.0)
        rates[thr] = online.idle(dec, rest_X, rest_t)
    assert rates[None].abstain_rate < 0.4
    assert rates[TUNED.threshold].abstain_rate > 0.5
    assert rates[TUNED.threshold].commits < rates[None].commits
    # and it still follows an attended target
    dec = _tuned_decoder()
    cal = FakeBoard(FREQS, seed=12)
    cal.set_target(ABSTAIN)
    calibrate(dec, cal, 30.0)
    X, t = board_data(20.0, seed=13, target=1, amp=3.0)
    ds = dec.feed(X, t)
    assert ds[-1].committed == 1 and np.mean([d.raw == 1 for d in ds]) > 0.6


def test_idle_resets_and_counts():
    dec = make_decoder(margin=1e6)
    dec.classifier.fit_baseline(dec.windows(board_data(10.0, target=ABSTAIN)[0]))
    X, t = board_data(12.0, seed=3, target=ABSTAIN)
    res = online.idle(dec, X, t)
    assert res.n_windows == res.n_abstain > 0 and res.commits == 0
    assert res.abstain_rate == 1.0 and dec.n_seen == 0
    assert res.seconds == pytest.approx(res.n_windows * 0.5)
    assert "abstained on 100%" in res.summary()


def test_decision_level_matches_classifier():
    dec = make_decoder()
    X, t = board_data(8.0, seed=2, target=0, amp=4.0)
    dec.classifier.fit_baseline(dec.windows(board_data(10.0, seed=9, target=ABSTAIN)[0]))
    for d in dec.feed(X, t):
        assert d.level == pytest.approx(float(np.max(d.scores)) / dec.classifier.baseline_spread)


def _cut_session(seconds_cut_end: float = 0.0, seconds_cut_start: float = 0.0):
    labels = [0, 1] * 6
    X, t, text = fake_session(FREQS, labels, amp=3.0, seed=7)
    a = int(round(seconds_cut_start * FS))
    b = X.shape[1] - int(round(seconds_cut_end * FS))
    from brainsync.recordings import Recording, parse_markers
    return Recording(path=None, X=X[:, a:b], t=t[a:b], fs=FS, ch_names=MONTAGE,
                     freqs=np.asarray(FREQS), phases=np.zeros(2), markers=parse_markers(text),
                     window_s=3.86, latency=0.14, kind="cued", marker_text=text)


@pytest.mark.parametrize("end, start", [(6.0, 0.0), (0.0, 3.0)])
def test_replay_counts_the_same_trials_as_cut_windows(end, start):
    # regression: replay kept trials cut off by the end (or start) of the data
    from brainsync.recordings import cut_windows
    rec = _cut_session(end, start)
    n = len(cut_windows(rec))
    assert n == 11
    res = replay(rec, classifier="cca", window_s=2.0, step_s=0.5, votes=2, margin=0.0,
                 threshold=None)
    assert res.n_trials == n
    assert all(o.n_windows > 0 for o in res.trials)


def test_cli_threshold_and_index_channels(synthetic_recording, capsys):
    assert online.main([str(synthetic_recording), "--classifier", "cca", "--threshold", "none",
                        "--channels", "4,5,O1,7"]) == 0
    out = capsys.readouterr().out
    assert "threshold none" in out
    assert online.main([str(synthetic_recording), "--threshold", "1.5", "--idle",
                        str(synthetic_recording)]) == 0
    out = capsys.readouterr().out
    assert "threshold 1.5" in out and "idle " in out and "self-check" in out


# -- real recordings ---------------------------------------------------------------------

REFERENCE = "spectrum_checks/2026-10-22_162029_cued.npz"


@pytest.mark.realdata(REFERENCE)
def test_reference_session_replay_meets_acceptance():
    res = replay(recordings_dir() / Path(REFERENCE).name)
    assert res.n_trials == 20
    assert res.correct >= 0.5 * res.n_trials, res.summary()    # 12/20 at the defaults
    assert res.wrong <= 2, res.summary()                         # 0
    assert res.still_commits <= 0.4 * res.n_still, res.summary()  # 5/20


EYES_CLOSED = "spectrum_checks/2026-10-22_151304_trials.npz"


@pytest.mark.realdata(REFERENCE, EYES_CLOSED)
def test_tuned_decoder_abstains_with_eyes_closed():
    # regression: with the margin alone the replayed decoder abstained on 33% of the
    # eyes-closed windows and committed 28 times in 166 s
    res = replay(recordings_dir() / Path(REFERENCE).name)
    rest = load_recording(recordings_dir() / Path(EYES_CLOSED).name)
    out = online.idle(res.decoder, rest.X, rest.t)
    assert out.n_windows > 600
    assert out.abstain_rate >= 0.65, out.summary()     # 73%
    assert out.commits <= 10, out.summary()            # 7


@pytest.mark.realdata(REFERENCE)
def test_reference_session_online_matches_offline():
    res = replay(recordings_dir() / Path(REFERENCE).name)
    agree = consistency(res)
    n, same = agree["same"]
    assert n > 100 and same >= 0.95
    # preprocessing the whole recording instead differs only near the window's newest edge
    assert agree["whole"][1] >= 0.8


def test_demo_calibration_length_pins_the_rest_offset():
    # regression: a 10 s baseline misjudged the 15-vs-17 Hz rest offset by about the
    # abstain margin; the demo's default length must do clearly better
    from brainsync.demo import CALIBRATE_S
    errs: dict[float, list[float]] = {10.0: [], CALIBRATE_S: []}
    spreads = []
    for seconds in errs:
        for seed in range(12):
            dec = _tuned_decoder()
            board = FakeBoard(FREQS, seed=100 + seed)
            board.set_target(ABSTAIN)
            cal = calibrate(dec, board, seconds)
            errs[seconds].append(cal.mean[0] - cal.mean[1])   # truth is 0 for white noise
            spreads.append(cal.spread)
    spread = float(np.median(spreads))
    rms = {s: float(np.sqrt(np.mean(np.square(e)))) / spread for s, e in errs.items()}
    assert rms[CALIBRATE_S] < 0.8 * rms[10.0], rms   # about 0.3 vs 0.45 spreads
    assert rms[CALIBRATE_S] < TUNED.margin, rms


# -- trained classifiers: onset alignment, held-out replay, loading --------------------------

from brainsync.synthetic import ssvep_trials  # noqa: E402


def _trained_clf(seed: int = 11):
    X, y = ssvep_trials(FREQS, FS, n_per_target=6, seconds=3.0, snr_db=0.0, seed=seed)
    X = preprocess(X, FS)
    return make("etrca", freqs=FREQS, fs=FS).fit(X[..., :500], y), X, y


def test_align_passes_the_window_offset_to_a_trained_classifier():
    clf, X, y = _trained_clf()
    names = ("a", "b", "c", "d")
    dec = Decoder(clf, FS, window_s=2.0, step_s=0.5, votes=1, pad_s=1.0, channels=None,
                  ch_names=names)
    assert dec.uses_offsets and not dec.aligned
    assert dec.offset_for(123.0) == 0                 # unaligned: no offset
    dec.align(100.0)
    assert dec.aligned and dec.offset_for(101.0) == 250 and dec.offset_for(99.5) == -125
    with pytest.raises(ValueError, match="finite"):
        dec.align(float("nan"))
    trial = X[0]                                      # response aligned at t = 100.0
    t = 100.0 + np.arange(trial.shape[1]) / FS
    out = dec.feed(trial, t)
    assert len(out) == 1
    window = dec.prepare(trial)                       # the pad is the first second
    np.testing.assert_allclose(out[0].scores, clf.scores(window, offset=250)[0])
    assert out[0].raw == y[0]
    # a calibration-free decoder ignores the alignment
    plain = make_decoder(window_s=2.0, pad_s=1.0, votes=1)
    plain.align(100.0)
    assert not plain.uses_offsets and plain.offset_for(101.0) == 0


def test_replay_trained_classifier_is_refitted_per_trial(synthetic_recording):
    res = replay(synthetic_recording, classifier="etrca", margin=0.0, threshold=None, votes=2)
    assert res.params["held_out"] is True and res.params["classifier"] == "etrca"
    assert res.decoder.classifier.fitted and res.decoder.aligned
    assert res.decoder.classifier.has_baseline
    assert res.correct >= 0.75 * res.n_trials and res.wrong <= 2
    plain = replay(synthetic_recording, classifier="cca", margin=0.0, threshold=None, votes=2)
    assert plain.params["held_out"] is False
    agree = consistency(res)
    assert agree["same"][1] == 1.0 and agree["same"][0] > 0


def test_trained_from_recording(synthetic_recording):
    clf = online.trained_from_recording(synthetic_recording)
    assert clf.name == "etrca" and clf.fitted and clf.freqs.tolist() == list(FREQS)
    assert clf.templates.shape[1] == 4 and clf.cycle is not None and not clf.has_baseline
    assert clf.margin == TUNED.margin and clf.threshold == TUNED.threshold
    sub = clf.for_freqs([17.0, 15.0])
    assert sub.freqs.tolist() == [17.0, 15.0]
    other = online.trained_from_recording(synthetic_recording, "mscca", channels=["O1", "O2"])
    assert other.name == "mscca" and other.templates.shape[1] == 2
    with pytest.raises(ValueError, match="not a trained classifier"):
        online.trained_from_recording(synthetic_recording, "cca")
    with pytest.raises(KeyError):
        online.trained_from_recording(synthetic_recording, channels=["Oz"])


def test_cli_trained_classifier_reports_held_out(synthetic_recording, capsys):
    rc = online.main([str(synthetic_recording), "--classifier", "mscca", "--trials"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mscca" in out and "inside the last trial" in out
