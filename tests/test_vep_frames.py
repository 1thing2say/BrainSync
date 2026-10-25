"""brainsync.stimulus.coded: c-VEP and t-VEP frame generators, their markers, and the
stimulus CLI on [code] / [time] configs."""
from __future__ import annotations

import io

import numpy as np
import pytest

from brainsync.recordings import parse_events, parse_markers
from brainsync.stimulus import __main__ as cli
from brainsync.stimulus.coded import (CodeSequence, TimeSequence, coded_cued_frames,
                                      coded_free_frames, sequence_for)
from brainsync.stimulus.config import load_config, make_trials, parse_config
from brainsync.stimulus.display import MarkerWriter, play, still_luminances
from brainsync.vep.codes import bit_luminance, lagged_codes, m_sequence

CUE, STIM, REST = 3, 14, 3   # frames at 60 Hz: stim = two 7-bit cycles


def _cfg(table: str, session: dict | None = None, stimulus: dict | None = None,
         **table_values):
    data = {
        "targets": {"labels": ["a", "b", "c", "d"]},
        table: table_values,
        "layout": {"rows": 2, "cols": 2, "size": 0.3, "spacing": 0.5},
        "session": {"cue_s": CUE / 60, "stim_s": STIM / 60, "rest_s": REST / 60,
                    **(session or {})},
        "stimulus": stimulus or {},
    }
    return parse_config(data)


class FakeRenderer:
    def __init__(self, refresh: float = 60.0):
        self.period = 1.0 / refresh
        self.t = 0.0
        self.draws: list[tuple[np.ndarray, int]] = []

    def draw(self, lum, cue):
        self.draws.append((np.array(lum), cue))

    def flip(self):
        self.t += self.period
        return self.t

    def escape_pressed(self):
        return False


# -- c-VEP ---------------------------------------------------------------------------------

def test_code_sequence_shows_the_lagged_code_and_marks_cycles():
    cfg = _cfg("code", length=7, lag_frames=2)
    seq = CodeSequence(cfg)
    codes = lagged_codes(m_sequence(7), 4, 2)
    for i in range(20):
        np.testing.assert_array_equal(seq.luminance(i), bit_luminance(codes[:, i % 7]))
        assert seq.events(i) == ((f"cycle:{i // 7}",) if i % 7 == 0 else ())
    np.testing.assert_array_equal(seq.still, [0.5] * 4)
    np.testing.assert_array_equal(still_luminances(cfg), seq.still)
    seq.restart()
    np.testing.assert_array_equal(seq.luminance(3), bit_luminance(codes[:, 3]))


def test_code_depth_scales_the_bits():
    cfg = _cfg("code", stimulus={"depth": 0.5}, length=7, lag_frames=2)
    lum = CodeSequence(cfg).luminance(0)
    assert set(np.round(lum, 6).tolist()) <= {0.25, 0.75}


def test_coded_cued_frames_markers_and_luminance():
    cfg = _cfg("code", stimulus={"hold_cue": True}, length=7, lag_frames=2)
    trials = make_trials(4, 1, shuffle=False)
    frames = list(coded_cued_frames(cfg, trials, CodeSequence(cfg)))
    assert len(frames) == 4 * (CUE + STIM + REST)
    codes = lagged_codes(m_sequence(7), 4, 2)
    per = CUE + STIM + REST
    for k in range(4):
        block = frames[k * per:(k + 1) * per]
        assert block[0].events == (f"cue:{k}",) and block[0].cue == k
        for fr in block[:CUE]:
            np.testing.assert_array_equal(fr.lum, [0.5] * 4)
            assert fr.trial == -1
        stim = block[CUE:CUE + STIM]
        assert stim[0].events == (f"stim_on:{k}", "cycle:0")
        assert stim[7].events == ("cycle:1",)
        assert all(fr.events == () for i, fr in enumerate(stim) if i not in (0, 7))
        assert all(fr.trial == k and fr.cue == k for fr in stim)   # hold_cue
        for i, fr in enumerate(stim):
            np.testing.assert_array_equal(fr.lum, bit_luminance(codes[:, i % 7]))
        assert block[CUE + STIM].events == (f"stim_off:{k}",)
        assert all(fr.cue == -1 for fr in block[CUE + STIM:])


def test_cued_frames_without_held_cue_and_with_zero_cue():
    cfg = _cfg("code", session={"cue_s": 0.0}, stimulus={"hold_cue": True}, length=7,
               lag_frames=2)
    frames = list(coded_cued_frames(cfg, make_trials(4, 1, shuffle=False), CodeSequence(cfg)))
    assert frames[0].events == ("cue:0", "stim_on:0", "cycle:0")
    cfg = _cfg("code", length=7, lag_frames=2)
    frames = list(coded_cued_frames(cfg, make_trials(4, 1, shuffle=False), CodeSequence(cfg)))
    assert frames[CUE].events == ("stim_on:0", "cycle:0") and frames[CUE].cue == -1


def test_cued_frames_status_and_finish():
    cfg = _cfg("code", length=7, lag_frames=2)
    left = iter([False, False, True])
    frames = list(coded_cued_frames(cfg, make_trials(4, 1, shuffle=False), CodeSequence(cfg),
                                    status=lambda: "hello", finish=lambda: next(left)))
    assert len(frames) == 4 * (CUE + STIM + REST) + 2
    assert frames[0].text == "hello" and frames[CUE].text == ""
    assert frames[-1].prompt and frames[-1].text == "hello"


# -- t-VEP ---------------------------------------------------------------------------------

def test_time_sequence_flashes_never_overlap_and_are_marked():
    cfg = _cfg("time", stimulus={"depth": 0.8}, flash_frames=2, gap_frames=3)
    seq = TimeSequence(cfg, seed=5)
    np.testing.assert_allclose(seq.still, [0.1] * 4)
    np.testing.assert_allclose(still_luminances(cfg), seq.still)
    lum = np.stack([seq.luminance(i) for i in range(200)])           # (frames, targets)
    bright = np.isclose(lum, 0.9)
    assert np.all(np.isclose(lum, 0.9) | np.isclose(lum, 0.1))
    assert np.all(bright.sum(axis=1) <= 1)                           # one target at a time
    onsets = [(i, seq.events(i)) for i in range(200) if seq.events(i)]
    assert all(ev == (f"flash:{int(np.flatnonzero(bright[i])[0])}",) for i, ev in onsets)
    frames = np.array([i for i, _ in onsets])
    assert np.all(np.diff(frames) == 5)                               # one slot apart
    assert frames[0] == 0
    for i in frames:
        k = int(np.flatnonzero(bright[i])[0])
        assert bright[i, k] and bright[i + 1, k] and not bright[i + 2, k]
    # every round (4 slots = 20 frames) flashes each target once
    for r in range(10):
        ks = sorted(int(np.flatnonzero(bright[i])[0]) for i in frames[4 * r:4 * r + 4])
        assert ks == [0, 1, 2, 3]


def test_time_sequence_is_seeded_and_restart_reorders():
    cfg = _cfg("time", flash_frames=2, gap_frames=13)
    a, b = TimeSequence(cfg, seed=9), TimeSequence(cfg, seed=9)
    order = lambda s: [s.events(i) for i in range(240) if s.events(i)]  # noqa: E731
    first = order(a)
    assert first == order(b)
    a.restart()
    assert any(order(a) != first for _ in range(1))
    rr = TimeSequence(_cfg("time", flash_frames=1, gap_frames=0, order="roundrobin"))
    assert [rr.events(i) for i in range(8)] == [(f"flash:{k % 4}",) for k in range(8)]


def test_time_cued_frames_restart_per_trial_and_stay_within_the_trial():
    cfg = _cfg("time", flash_frames=2, gap_frames=1)   # 3-frame slots, 12-frame rounds
    trials = make_trials(4, 2, shuffle=False)
    seq = TimeSequence(cfg, seed=1)
    frames = list(coded_cued_frames(cfg, trials, seq))
    per = CUE + STIM + REST
    for n in range(8):
        stim = frames[n * per + CUE:n * per + CUE + STIM]
        assert stim[0].events[0] == f"stim_on:{n % 4}" and stim[0].events[1].startswith("flash:")
        flashes = [fr.events for fr in stim if any(e.startswith("flash:") for e in fr.events)]
        assert len(flashes) == STIM // 3 + (1 if STIM % 3 else 0)
        for fr in frames[n * per:n * per + CUE] + frames[n * per + CUE + STIM:(n + 1) * per]:
            np.testing.assert_array_equal(fr.lum, seq.still)


# -- free run, play and the CLI ------------------------------------------------------------

def test_coded_free_frames_hold_stop_and_seconds():
    cfg = _cfg("code", length=7, lag_frames=2)
    seq = CodeSequence(cfg)
    held = iter([True] * 3 + [False] * 100)
    frames = list(coded_free_frames(cfg, seq, seconds=14 / 60, hold=lambda: next(held)))
    assert len(frames) == 3 + 14
    for fr in frames[:3]:
        np.testing.assert_array_equal(fr.lum, seq.still)
        assert fr.events == ()
    assert frames[3].events == ("cycle:0",) and frames[10].events == ("cycle:1",)
    cues = iter([2, 2, -1, 1])
    stops = iter([False, False, False, True])
    frames = list(coded_free_frames(cfg, seq, cue=lambda: next(cues), stop=lambda: next(stops)))
    assert [fr.cue for fr in frames] == [2, 2, -1]
    with pytest.raises(ValueError, match="no \\[code\\] or \\[time\\]"):
        sequence_for(load_config("two_targets"))
    assert isinstance(sequence_for(load_config("cvep_4")), CodeSequence)
    assert isinstance(sequence_for(load_config("tvep_4"), seed=1), TimeSequence)
    with pytest.raises(ValueError, match="no \\[time\\]"):
        TimeSequence(cfg)
    with pytest.raises(ValueError, match="no \\[code\\]"):
        CodeSequence(_cfg("time"))


def test_play_stamps_cycle_and_flash_markers_with_the_flip_time():
    cfg = _cfg("code", length=7, lag_frames=2)
    ren = FakeRenderer()
    out = io.StringIO()
    markers = MarkerWriter("monotonic", stream=out, clock=lambda: ren.t)
    trials = make_trials(4, 1, shuffle=False)
    timing = play(coded_cued_frames(cfg, trials, CodeSequence(cfg)), ren, markers, 60.0,
                  trial_targets={t.index: t.target for t in trials})
    assert timing.n_frames == 4 * (CUE + STIM + REST) and timing.dropped == 0
    text = out.getvalue()
    events = [(f"{kind}:{k}", round(t * 60)) for t, kind, k in parse_events(text)]
    per = CUE + STIM + REST
    assert ("cycle:0", CUE + 1) in events and ("cycle:1", CUE + 8) in events
    assert ("cycle:0", per + CUE + 1) in events
    assert [e for e, _ in events[:4]] == ["cue:0", "stim_on:0", "cycle:0", "cycle:1"]
    assert len(timing.trials) == 4 and [t.target for t in timing.trials] == [0, 1, 2, 3]
    assert [k for _, _, k in parse_markers(text)] == [0, 0, 1, 1, 2, 2, 3, 3]   # unchanged
    assert len(parse_events(text, kinds=["cycle"])) == 8

    cfg = _cfg("time", flash_frames=2, gap_frames=1)
    ren, out = FakeRenderer(), io.StringIO()
    play(coded_free_frames(cfg, TimeSequence(cfg, seed=2), seconds=1.0), ren,
         MarkerWriter("monotonic", stream=out, clock=lambda: ren.t), 60.0)
    flashes = parse_events(out.getvalue(), kinds=["flash"])
    assert len(flashes) == 20 and all(k in range(4) for _, _, k in flashes)
    assert np.allclose(np.diff([t for t, _, _ in flashes]) * 60, 3)


def test_plan_for_coded_configs(capsys):
    assert cli.main(["--plan", "-c", "cvep_4", "--seed", "1"]) == 0
    text = capsys.readouterr().out
    assert "c-VEP m-sequence" in text and "63-bit m-sequence" in text
    assert "lag 16 frames = 267 ms" in text and "lag 48 frames = 800 ms" in text
    assert "nearest two targets 15 frames apart" in text
    assert "24 trial(s)" in text and "Hz  phase" not in text
    assert cli.main(["--plan", "-c", "cvep_16"]) == 0
    assert "nearest two targets 3 frames apart" in capsys.readouterr().out
    assert cli.main(["--plan", "-c", "tvep_4", "--free"]) == 0
    text = capsys.readouterr().out
    assert "t-VEP flashes" in text and "2-frame flashes (33 ms) in 15-frame slots" in text
    assert "1.00 flashes/s" in text and "free run" in text
    assert cli.main(["--plan", "-c", "fvep_6", "--seed", "1"]) == 0
    assert "8.571 Hz" in capsys.readouterr().out
    assert cli.main(["--plan", "-c", "cvep_4", "--conditions", "sine:1"]) == 2
    assert "not the [code] config" in capsys.readouterr().err
    assert cli.main(["--plan", "-c", "tvep_4", "--rate"]) == 2


def test_coded_free_frames_poll_status_every_frame():
    cfg = load_config("cvep_4")
    seq = sequence_for(cfg, seed=1)
    texts = iter(["t1", "t2", "t3"])
    held = iter([True, False, False])
    frames = list(coded_free_frames(cfg, seq, seconds=2 / 60, hold=lambda: next(held),
                                    status=lambda: next(texts)))
    assert [f.text for f in frames] == ["t1", "t2", "t3"]
    assert frames[0].cue == -1 and frames[1].events == seq.events(0)
