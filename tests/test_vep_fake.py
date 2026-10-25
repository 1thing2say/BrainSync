"""synthetic.vep_kernel / sequence_response and brainsync.vep.fake.SequenceFakeBoard."""
from __future__ import annotations

import numpy as np
import pytest

from brainsync.board import POSTERIOR_ROWS
from brainsync.recordings import parse_events
from brainsync.stimulus.coded import CodeSequence, TimeSequence
from brainsync.stimulus.config import load_config, parse_config
from brainsync.synthetic import sequence_response, vep_kernel
from brainsync.vep.codes import lagged_codes, m_sequence
from brainsync.vep.fake import SequenceFakeBoard, markers_text

FS = 250.0


def _code_cfg(length: int = 63, lag: int = 16):
    return parse_config({"targets": {"labels": ["a", "b", "c", "d"]},
                         "code": {"length": length, "lag_frames": lag},
                         "layout": {"rows": 2, "cols": 2, "size": 0.3, "spacing": 0.5}})


# -- kernel --------------------------------------------------------------------------------

def test_kernel_shape_peak_and_decay():
    k = vep_kernel(FS)
    assert k.shape == (125,) and np.max(np.abs(k)) == pytest.approx(1.0)
    assert np.all(k[:int(0.06 * FS)] == 0)                       # nothing before the latency
    peak = int(np.argmax(k)) / FS
    assert 0.09 <= peak <= 0.12                                   # positive peak near 100 ms
    trough = int(np.argmin(k)) / FS
    assert 0.17 <= trough <= 0.21                                 # trough near 190 ms
    assert np.max(np.abs(k[int(0.4 * FS):])) < 0.05               # silent by 400 ms
    for kw in ({"fs": 0}, {"seconds": 0}, {"latency": -0.1}, {"latency": 0.5}, {"decay": 0},
               {"freq": 0}):
        with pytest.raises(ValueError):
            vep_kernel(**{"fs": FS, **kw})


def test_sequence_response_is_a_causal_convolution():
    k = vep_kernel(FS)
    stim = np.zeros(400)
    stim[10] = 1.0
    r = sequence_response(stim, k)
    assert r.shape == stim.shape
    np.testing.assert_allclose(r[10:10 + k.size], k)
    assert np.all(r[:10] == 0)
    stim[20] = 1.0
    r2 = sequence_response(stim, k)
    np.testing.assert_allclose(r2[20:20 + k.size - 10], k[:k.size - 10] + k[10:])
    with pytest.raises(ValueError):
        sequence_response(np.array([]), k)


# -- the board -----------------------------------------------------------------------------

def test_board_streams_samples_markers_and_the_response_without_chunking_artifacts():
    cfg = _code_cfg()
    seq = CodeSequence(cfg)
    one = SequenceFakeBoard(seq, fixated=2, amp=5.0, noise=0.0, t0=1000.0)
    one.start_flicker()
    one.advance(3.0)
    X1, t1 = one.read()
    assert X1.shape == (8, 750) and t1.shape == (750,)
    np.testing.assert_allclose(t1, 1000.0 + np.arange(750) / FS)
    assert np.all(X1[[0, 1, 2, 3]] == 0)                 # noise 0: only the posterior rows
    two = SequenceFakeBoard(CodeSequence(cfg), fixated=2, amp=5.0, noise=0.0, t0=1000.0)
    two.start_flicker()
    parts = []
    for step in (0.013, 0.5, 1.0, 0.987, 0.5):
        two.advance(step)
        parts.append(two.read()[0])
    np.testing.assert_allclose(np.concatenate(parts, axis=1), X1, atol=1e-12)

    events = one.read_events()
    assert events[0] == (1000.0, "stim_on", 2)
    cycles = [(t, k) for t, kind, k in events if kind == "cycle"]
    assert cycles == [(1000.0, 0), (pytest.approx(1000.0 + 63 / 60), 1),
                      (pytest.approx(1000.0 + 126 / 60), 2)]   # frame 189 starts at 3.15 s
    assert one.read_events() == []                       # handed out once
    text = markers_text(events)
    assert text.startswith("measured refresh: 60.00 Hz\n")
    parsed = parse_events(text)
    assert parsed[0] == (1000.0, "stim_on", 2) and len(parsed) == len(events)

    # the posterior rows carry the fixated target's code through the kernel
    codes = lagged_codes(m_sequence(63), 4, 16)
    frames = np.floor(np.arange(750) * 60 / FS + 1e-9).astype(int)
    stim = codes[2, frames % 63] - 0.5
    expected = 5.0 * sequence_response(stim, vep_kernel(FS))
    for row, gain in zip(POSTERIOR_ROWS, one.gains):
        np.testing.assert_allclose(X1[row], gain * expected, atol=1e-9)
    # and that response tells the fixated target from the others
    ref = [sequence_response(codes[k, frames % 63] - 0.5, vep_kernel(FS)) for k in range(4)]
    corr = [np.corrcoef(X1[POSTERIOR_ROWS[0]], r)[0, 1] for r in ref]
    assert int(np.argmax(corr)) == 2 and corr[2] > 0.99 and max(corr[:2] + corr[3:]) < 0.3


def test_board_no_target_still_screen_and_restart():
    seq = CodeSequence(_code_cfg())
    b = SequenceFakeBoard(seq, fixated=-1, amp=5.0, noise=1.0, seed=3)
    b.start_flicker()
    b.advance(2.0)
    X, _ = b.read()
    assert X.shape == (8, 500)
    assert np.std(X[list(POSTERIOR_ROWS)]) == pytest.approx(np.std(X[:4]), rel=0.2)
    kinds = [kind for _, kind, _ in b.read_events()]
    assert kinds[:2] == ["stim_on", "cycle"] and kinds.count("cycle") == 2
    b.stop_flicker()
    b.advance(1.0)
    assert b.read_events() == [] and not b.flickering
    b.set_target(1)
    b.start_flicker()                                    # frame 0 again, at the next sample
    assert b.frame_time(0) == pytest.approx(b.t0 + 3.0)
    b.advance(0.1)
    ev = b.read_events()
    assert ev[0] == (pytest.approx(b.t0 + 3.0), "stim_on", 1) and ev[1][1:] == ("cycle", 0)
    with pytest.raises(ValueError):
        b.set_target(4)
    with pytest.raises(ValueError):
        SequenceFakeBoard(seq, fs=0)
    with pytest.raises(ValueError):
        SequenceFakeBoard(seq, ch_names=["a", "b"])
    with pytest.raises(ValueError):
        b.advance(-1)
    b.stop_flicker()
    with pytest.raises(ValueError, match="not running"):
        b.frame_time(0)


def test_board_follows_a_clock():
    now = [500.0]
    b = SequenceFakeBoard(CodeSequence(_code_cfg()), clock=lambda: now[0])
    b.start_flicker()
    now[0] += 1.0
    X, t = b.read()
    assert X.shape == (8, 250) and t[0] == pytest.approx(500.0)
    with pytest.raises(ValueError):
        b.advance(1.0)
    X, _ = b.read()
    assert X.shape == (8, 0)


def test_time_board_flashes_give_isolated_kernels():
    shipped = load_config("tvep_4")       # 2-frame flashes, 15-frame slots, 4 targets, 0.8
    assert shipped.depth == 0.8 and shipped.time.order == "random"
    # round robin: a target's own flashes are exactly a round (1 s) apart, so each response
    # stands alone (in the shuffled order two own flashes can be one slot apart)
    cfg = parse_config({"targets": {"count": 4},
                        "time": {"flash_frames": 2, "gap_frames": 13, "order": "roundrobin"},
                        "stimulus": {"depth": 0.8},
                        "layout": {"rows": 2, "cols": 2, "size": 0.3, "spacing": 0.5}})
    seq = TimeSequence(cfg, seed=4)
    b = SequenceFakeBoard(seq, fixated=1, amp=10.0, noise=0.0, t0=0.0)
    b.start_flicker()
    b.advance(4.0)
    X, t = b.read()
    flashes = [(tt, k) for tt, kind, k in b.read_events() if kind == "flash"]
    assert len(flashes) == 16 and [k for _, k in flashes[:4]] == [0, 1, 2, 3]
    mine = [tt for tt, k in flashes if k == 1]
    assert len(mine) == 4 and np.allclose(np.diff(mine), 1.0)
    row = X[POSTERIOR_ROWS[0]]
    kernel = vep_kernel(FS)
    for onset in mine[:3]:
        i0 = int(np.searchsorted(t, onset))
        seg = row[i0:i0 + 100]
        # peak near onset + 100 ms, nothing before the latency, response only to own flashes
        assert 0.08 <= (int(np.argmax(seg)) / FS) <= 0.13
        assert np.all(np.abs(row[i0 - 5:i0 + int(0.06 * FS) - 1]) < 1e-9)
    pulse = np.r_[np.ones(int(round(2 * FS / 60))), np.zeros(200)]
    assert np.max(np.abs(row)) == pytest.approx(
        10.0 * b.gains[0] * 0.8 * np.max(np.abs(sequence_response(pulse, kernel))), rel=0.05)
    # the shuffled order still never lets two flashes share a frame
    sh = SequenceFakeBoard(TimeSequence(shipped, seed=1), fixated=-1, amp=0.0, noise=0.0)
    sh.start_flicker()
    sh.advance(5.0)
    ts = [tt for tt, kind, _ in sh.read_events() if kind == "flash"]
    assert np.all(np.diff(ts) >= 15 / 60 - 1e-9)
