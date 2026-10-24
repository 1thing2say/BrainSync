"""BrainChess trace strip: display samples from board reads, the stimulus-side buffer and
the lane and window geometry. No window, no psychopy."""
from __future__ import annotations

import numpy as np
import pytest

from brainsync.brainchess.trace import (TraceBuffer, TraceFeed, TraceView, Window, lane_points,
                                        window_marks)


# -- TraceFeed (game side) ------------------------------------------------------------------

def test_feed_block_means_at_50_hz_and_timing():
    feed = TraceFeed([2, 0], fs=250.0)
    assert feed.factor == 5 and feed.dt == pytest.approx(0.02)
    X = np.zeros((3, 12))
    X[0] = np.arange(12)
    X[2] = 100.0
    t_end, out = feed.push(X, end=1012)
    # two full blocks of five; two samples carried over, so the last block ends at 1010
    assert out.shape == (2, 2) and t_end == pytest.approx(1010 / 250)
    np.testing.assert_allclose(out[:, 0], 0.0)               # a constant lane: no drift left
    assert out[1, 1] > 0                                      # rising lane sits above its mean
    assert feed.push(np.zeros((3, 2)), end=1014) is None      # 4 carried: no block yet
    t_end, out = feed.push(np.zeros((3, 1)), end=1015)
    assert len(out) == 1 and t_end == pytest.approx(1015 / 250)


def test_feed_removes_drift_but_keeps_the_rhythm():
    fs = 250.0
    feed = TraceFeed([0], fs=fs)
    n = int(fs * 6)
    t = np.arange(n) / fs
    x = 500.0 + 40.0 * t + 10.0 * np.sin(2 * np.pi * 7.0 * t)   # DC offset, drift and a rhythm
    _, out = feed.push(x[None, :], end=n)
    tail, before = out[-100:, 0], out[-200:-100, 0]           # the last 2 s, the 2 s before
    # the offset is gone and the drift leaves only a constant lag (slope x tau = 16), which
    # the strip's per-lane centring takes off: the lane no longer wanders
    assert abs(tail.mean() - 40.0 * 0.4) < 2.0
    assert abs(tail.mean() - before.mean()) < 1.0
    assert 5.0 < tail.std() < 10.0                            # the 7 Hz rhythm survives


def test_feed_chunking_does_not_change_the_output():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(2, 103))
    whole = TraceFeed([0, 1], fs=250.0).push(X, end=103)[1]
    feed = TraceFeed([0, 1], fs=250.0)
    parts = [feed.push(X[:, a:b], end=b) for a, b in ((0, 7), (7, 50), (50, 51), (51, 103))]
    pieces = np.concatenate([p[1] for p in parts if p is not None])
    np.testing.assert_allclose(pieces, whole)


def test_feed_rejects_bad_settings():
    with pytest.raises(ValueError, match="at least one"):
        TraceFeed([], fs=250.0)
    with pytest.raises(ValueError, match="positive"):
        TraceFeed([0], fs=0.0)


# -- TraceBuffer (stimulus side) ------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_buffer_extrapolates_by_at_most_lead_s():
    clock = Clock()
    buf = TraceBuffer(n_lanes=1, span_s=6.0, lead_s=0.3, clock=clock)
    assert buf.now() is None and buf.view().t_now is None
    buf.add_samples(20.0, 0.02, [1.0, 2.0])
    assert buf.now() == pytest.approx(20.0)
    clock.t += 0.1
    assert buf.now() == pytest.approx(20.1)
    clock.t += 5.0
    assert buf.now() == pytest.approx(20.3)                    # capped: the game went quiet
    buf.add_window(Window(19.0, 0, True, False))              # older: does not move time back
    assert buf.now() == pytest.approx(20.3)


def test_buffer_keeps_only_the_span_and_infers_the_step():
    clock = Clock()
    buf = TraceBuffer(n_lanes=2, span_s=6.0, lead_s=0.0, clock=clock)
    for k in range(1, 11):                                    # 10 s of 1 s chunks
        buf.add_samples(float(k), 0.5, np.ones((2, 2)) * k)
        buf.add_window(Window(float(k), k % 2, True, False))
        buf.add_window(Window(k + 0.25, -1, False, False))
    view = buf.view()
    assert view.t_now == pytest.approx(10.25)
    assert view.times.min() >= 10.25 - 6.0 and view.times.max() == pytest.approx(10.0)
    assert view.values.shape == (len(view.times), 2)
    assert min(w.t for w in view.windows) >= 10.25 - 7.0      # pruned beyond span + 1 s
    assert view.step == pytest.approx(0.25)


def test_buffer_lanes_and_bad_samples():
    buf = TraceBuffer(n_lanes=4)
    buf.add_samples(1.0, 0.02, np.zeros(8))
    buf.set_lanes(2)                                          # new lanes: samples dropped
    assert buf.n_lanes == 2 and buf.view().times.size == 0
    with pytest.raises(ValueError, match="rows of 2"):
        buf.add_samples(1.0, 0.02, [1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="dt > 0"):
        buf.add_samples(1.0, 0.0, [1.0, 2.0])
    with pytest.raises(ValueError, match="1..8"):
        buf.set_lanes(9)


# -- geometry -------------------------------------------------------------------------------

def _view(times, values, windows=(), t_now=None, step=0.25) -> TraceView:
    times = np.asarray(times, dtype=float)
    return TraceView(times[-1] if t_now is None else t_now, times,
                     np.asarray(values, dtype=float), tuple(windows), step)


def test_lane_points_stack_scale_and_clip():
    t = np.linspace(4.0, 10.0, 301)
    sine = np.sin(2 * np.pi * 2 * t)
    spike = np.where(np.arange(301) == 150, 1e6, sine)        # one huge artefact
    lanes = lane_points(_view(t, np.column_stack([sine, spike])), 0.0, 1.0, 1.0, 0.0, 6.0)
    assert len(lanes) == 2 and lanes[0].shape == (301, 2)
    np.testing.assert_allclose(lanes[0][[0, -1], 0], [0.0, 1.0])   # span maps onto x0..x1
    assert 0.5 < lanes[0][:, 1].min() and lanes[0][:, 1].max() < 1.0  # lane 0: the top half
    assert lanes[1][:, 1].max() <= 0.25 + 0.5 * 0.42 + 1e-9  # the spike is clipped to its lane
    assert abs(np.median(lanes[0][:, 1]) - 0.75) < 0.01


def test_lane_points_without_data():
    empty = TraceView(None, np.empty(0), np.empty((0, 3)), (), 0.25)
    assert [p.shape for p in lane_points(empty, 0, 1, 1, 0, 6)] == [(0, 2)] * 3
    flat = lane_points(_view([1.0, 2.0], [[5.0], [5.0]]), 0, 1, 1, 0, 6.0)
    np.testing.assert_allclose(flat[0][:, 1], 0.5)            # a flat lane sits mid-lane


def test_window_marks_end_at_the_window_and_clip():
    windows = [Window(t, 0, True, False) for t in (3.9, 4.1, 7.0, 10.0, 10.2)]
    view = _view([10.0], [[0.0]], windows, t_now=10.0)
    marks = window_marks(view, 0.0, 0.6, 6.0, gap=0.0)        # 0.1 per second
    assert [m[2].t for m in marks] == [4.1, 7.0, 10.0, 10.2]  # 3.9 is off the left edge
    left, width, _ = marks[2]
    assert left + width == pytest.approx(0.6) and width == pytest.approx(0.025)
    left, width, _ = marks[3]                                 # past the right edge: clipped
    assert left == pytest.approx(0.595) and width == pytest.approx(0.005)
    left, width, _ = marks[0]                                 # half off the left: clipped
    assert left == pytest.approx(0.0) and width == pytest.approx(0.01)
    none = TraceView(None, np.empty(0), np.empty((0, 1)), tuple(windows), 0.25)
    assert window_marks(none, 0, 1, 6) == []
