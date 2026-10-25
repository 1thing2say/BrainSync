"""brainsync.vep.codes (m-sequences, lagged codes, flash schedules) and the [code] / [time]
stimulus config tables."""
from __future__ import annotations

import math

import numpy as np
import pytest

from brainsync.stimulus.config import (available_configs, config_warnings, load_config,
                                       parse_config)
from brainsync.vep.codes import (ORDERS, PRIMITIVE_TAPS, Schedule, autocorrelation,
                                 bit_luminance, degree_for_length, flash_rate_hz,
                                 lagged_codes, m_sequence, min_lag_distance, time_schedule)


# -- m-sequences ---------------------------------------------------------------------------

@pytest.mark.parametrize("degree", sorted(PRIMITIVE_TAPS))
def test_every_listed_polynomial_gives_a_maximal_sequence(degree):
    n = 2 ** degree - 1
    s = m_sequence(n)
    assert s.shape == (n,) and s.dtype == np.int8 and set(s.tolist()) == {0, 1}
    assert int(s.sum()) == 2 ** (degree - 1)   # one more 1 than 0
    r = autocorrelation(s)
    assert r[0] == n and set(r[1:].tolist()) == {-1}
    # full period: no shift shorter than n maps the sequence onto itself
    assert all((np.roll(s, k) != s).any() for k in range(1, n))


def test_seed_gives_a_cyclic_shift_of_the_same_sequence():
    a, b = m_sequence(63, seed=1), m_sequence(63, seed=37)
    assert any(np.array_equal(np.roll(a, k), b) for k in range(63))


@pytest.mark.parametrize("length", [0, 1, 2, 10, 64, 62, 2 ** 11 - 1, 7.5])
def test_bad_lengths_are_refused(length):
    with pytest.raises(ValueError):
        degree_for_length(length)


@pytest.mark.parametrize("seed", [0, 64, -1, 1.5])
def test_bad_seeds_are_refused(seed):
    with pytest.raises(ValueError, match="seed"):
        m_sequence(63, seed=seed)


def test_autocorrelation_refuses_non_bits():
    with pytest.raises(ValueError, match="0/1"):
        autocorrelation(np.array([0, 2, 1]))
    with pytest.raises(ValueError, match="empty"):
        autocorrelation(np.array([]))


# -- lagged codes ----------------------------------------------------------------------------

def test_lagged_codes_are_time_shifts_of_the_paper_layout():
    s = m_sequence(63)
    c = lagged_codes(s, 16, 4)
    assert c.shape == (16, 63)
    for k in range(16):
        np.testing.assert_array_equal(c[k], np.roll(s, 4 * k))
    # every pair of targets is nearly orthogonal: correlation -1/63 between +/-1 codes
    pm = 2.0 * c - 1.0
    gram = pm @ pm.T
    assert np.all(np.diag(gram) == 63)
    assert np.all(gram[~np.eye(16, dtype=bool)] == -1)
    assert min_lag_distance(16, 4, 63) == 3   # targets 0 and 15 wrap to 3 frames apart
    assert min_lag_distance(4, 16, 63) == 15
    assert min_lag_distance(1, 4, 63) == 63


def test_frames_per_bit_holds_each_bit():
    c = lagged_codes(np.array([1, 0, 0]), 2, 2, frames_per_bit=2)
    np.testing.assert_array_equal(c[0], [1, 1, 0, 0, 0, 0])
    np.testing.assert_array_equal(c[1], [0, 0, 1, 1, 0, 0])


def test_too_many_targets_for_the_cycle():
    with pytest.raises(ValueError, match="fewer targets"):
        lagged_codes(m_sequence(63), 17, 4)
    with pytest.raises(ValueError, match="fewer targets"):
        lagged_codes(m_sequence(63), 4, 21)
    lagged_codes(m_sequence(63), 4, 20)   # 3 * 20 = 60 < 63 fits
    for bad in ({"n_targets": 0}, {"lag_frames": 0}, {"frames_per_bit": 0}):
        kw = {"n_targets": 2, "lag_frames": 1, "frames_per_bit": 1, **bad}
        with pytest.raises(ValueError):
            lagged_codes(m_sequence(7), **kw)


def test_bit_luminance_matches_square_flicker_scale():
    np.testing.assert_allclose(bit_luminance(np.array([0, 1])), [0.0, 1.0])
    np.testing.assert_allclose(bit_luminance(np.array([0, 1]), depth=0.5), [0.25, 0.75])
    for depth in (0.0, 1.5, math.nan):
        with pytest.raises(ValueError):
            bit_luminance(np.array([1]), depth=depth)


# -- t-VEP schedules -------------------------------------------------------------------------

@pytest.mark.parametrize("order", ORDERS)
def test_schedule_never_overlaps_and_flashes_every_target_once_per_round(order):
    sc = time_schedule(4, 2, 13, rounds=5, order=order, seed=3)
    assert isinstance(sc, Schedule)
    assert sc.slot_frames == 15 and sc.n_frames == 5 * 4 * 15
    m = sc.matrix()
    assert m.shape == (4, sc.n_frames)
    assert np.all(m.sum(axis=0) <= 1)           # at most one target bright on any frame
    assert np.all(m.sum(axis=1) == 5 * 2)       # 5 flashes of 2 frames per target
    frames = np.array([f for f, _ in sc.onsets])
    assert np.all(np.diff(frames) == 15)        # consecutive onsets a whole slot apart
    for r in range(5):
        assert sorted(k for _, k in sc.onsets[r * 4:(r + 1) * 4]) == [0, 1, 2, 3]
    for k in range(4):
        on = sc.onsets_of(k)
        assert on.size == 5 and np.all(np.diff(on) >= 15)
        assert np.all(m[k, on] == 1) and np.all(m[k, on + 2] == 0)


def test_roundrobin_is_fixed_and_random_is_seeded():
    rr = time_schedule(3, 1, 4, rounds=2, order="roundrobin")
    assert [k for _, k in rr.onsets] == [0, 1, 2, 0, 1, 2]
    a = time_schedule(4, 2, 13, rounds=4, order="random", seed=7)
    b = time_schedule(4, 2, 13, rounds=4, order="random", seed=7)
    assert a == b
    assert any(time_schedule(4, 2, 13, rounds=4, order="random", seed=s) != a
               for s in range(1, 6))


def test_flash_rate_and_bad_schedule_values():
    assert flash_rate_hz(4, 2, 13, 60.0) == pytest.approx(1.0)
    assert flash_rate_hz(1, 1, 0, 60.0) == pytest.approx(60.0)
    with pytest.raises(ValueError):
        flash_rate_hz(4, 2, 13, 0.0)
    with pytest.raises(ValueError, match="order"):
        time_schedule(4, 2, 13, 1, order="sorted")
    for bad in ({"n_targets": 0}, {"flash_frames": 0}, {"gap_frames": -1}, {"rounds": 0}):
        kw = {"n_targets": 2, "flash_frames": 1, "gap_frames": 1, "rounds": 1, **bad}
        with pytest.raises(ValueError):
            time_schedule(**kw)


# -- config tables ---------------------------------------------------------------------------

def test_shipped_paradigm_configs():
    assert {"cvep_4", "cvep_16", "tvep_4", "fvep_6"} <= set(available_configs())
    c4 = load_config("cvep_4")
    assert c4.paradigm == "code" and c4.coded and c4.n_targets == 4
    assert c4.code.length == 63 and c4.code.lag_frames == 16 and c4.code.cycle_frames == 63
    assert all(math.isnan(t.freq) for t in c4.targets) and c4.labels == ("0", "1", "2", "3")
    assert c4.session.stim_s == pytest.approx(5 * 63 / 60)
    c16 = load_config("cvep_16")
    assert c16.n_targets == 16 and c16.code.lag_frames == 4 and (c16.rows, c16.cols) == (4, 4)
    assert c16.labels == tuple(str(k) for k in range(16))
    t4 = load_config("tvep_4")
    assert t4.paradigm == "time" and t4.time.slot_frames == 15 and t4.time.order == "random"
    assert flash_rate_hz(t4.n_targets, t4.time.flash_frames, t4.time.gap_frames,
                         t4.refresh) == pytest.approx(1.0)
    f6 = load_config("fvep_6")
    assert f6.paradigm == "freq" and not f6.coded and f6.waveform == "square"
    np.testing.assert_allclose(f6.refresh / f6.freqs, [4, 5, 6, 7, 8, 10], atol=1e-3)
    assert any("m-sequence" in w for w in config_warnings(c4))
    assert not any("t-VEP" in w for w in config_warnings(t4))
    assert sum("duty" in w for w in config_warnings(f6)) == 2   # 5- and 7-frame cycles


def _coded(table: str, **table_values):
    data = {"targets": {"labels": ["a", "b", "c", "d"]}, table: table_values,
            "layout": {"rows": 2, "cols": 2, "size": 0.3, "spacing": 0.5}}
    return data


def test_code_and_time_tables_validate():
    cfg = parse_config(_coded("code", length=31, lag_frames=7))
    assert cfg.code.cycle_frames == 31 and cfg.paradigm == "code"
    cfg = parse_config(_coded("time", flash_frames=1, gap_frames=0, order="roundrobin"))
    assert cfg.time.slot_frames == 1
    assert any("4 Hz" in w for w in config_warnings(cfg))   # 15 flashes/s per target
    with pytest.raises(ValueError, match="not both"):
        parse_config({**_coded("code"), "time": {}})
    with pytest.raises(ValueError, match="2\\^n - 1"):
        parse_config(_coded("code", length=64))
    with pytest.raises(ValueError, match="fewer targets"):
        parse_config(_coded("code", length=7, lag_frames=3))
    with pytest.raises(ValueError, match="lag_frames"):
        parse_config(_coded("code", lag_frames=0))
    with pytest.raises(ValueError, match="flash_frames"):
        parse_config(_coded("time", flash_frames=0))
    with pytest.raises(ValueError, match="gap_frames"):
        parse_config(_coded("time", gap_frames=-1))
    with pytest.raises(ValueError, match="order"):
        parse_config(_coded("time", order="alphabetical"))
    with pytest.raises(ValueError, match="unknown key"):
        parse_config(_coded("code", lag_s=1))
    with pytest.raises(ValueError, match="must be an integer"):
        parse_config(_coded("code", lag_frames=2.5))


def test_coded_targets_need_labels_or_count_and_refuse_freqs():
    grid = {"cols": 3, "size": 0.3, "spacing": 0.5}
    cfg = parse_config({"targets": {"count": 3}, "code": {}, "layout": grid})
    assert cfg.n_targets == 3 and cfg.labels == ("0", "1", "2")
    with pytest.raises(ValueError, match="labels or count"):
        parse_config({"targets": {}, "code": {}})
    with pytest.raises(ValueError, match="do not apply"):
        parse_config({"targets": {"freqs": [15.0, 17.0]}, "code": {}})
    with pytest.raises(ValueError, match="needs freqs"):
        parse_config({"targets": {"labels": ["a", "b"]}})
    with pytest.raises(ValueError, match="disagrees"):
        parse_config({"targets": {"freqs": [15.0, 17.0], "count": 3}})
    with pytest.raises(ValueError, match="2 labels for 3"):
        parse_config({"targets": {"count": 3, "labels": ["a", "b"]}, "code": {},
                      "layout": grid})
