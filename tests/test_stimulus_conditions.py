"""Mixed flicker conditions, status text and comfort ratings in a cued session (brainsync.ab).

Nothing here opens a window or imports psychopy.
"""
from __future__ import annotations

import io
import sys
import types

import numpy as np
import pytest

from brainsync.stimulus import __main__ as cli
from brainsync.stimulus.config import (Condition, config_warnings, load_config, make_trials,
                                       override, parse_conditions, parse_config,
                                       session_trials)
from brainsync.stimulus.display import (END_PROMPT, RATE_PROMPT, MarkerWriter, cued_frames,
                                        play, still_luminances)
from brainsync.stimulus.feedback import StatusReader
from brainsync.stimulus.flicker import frame_luminances

AB = (Condition("square", 1.0), Condition("sine", 1.0), Condition("sine", 0.5))


def _cfg(conditions=AB, **session):
    timing = {"blocks": 1, "cue_s": 0.05, "stim_s": 0.1, "rest_s": 0.05, **session}
    cfg = parse_config({"name": "t", "targets": {"freqs": [15.0, 17.0]},
                        "layout": {"rows": 1, "cols": 2, "size": 0.4, "spacing": 1.0},
                        "display": {"refresh_hz": 60.0}, "session": timing})
    return override(cfg, conditions=conditions)


# -- conditions and the trial order ---------------------------------------------------------

def test_parse_conditions():
    assert parse_conditions("square:1,sine:1, sine:0.5") == AB
    assert parse_conditions("sine") == (Condition("sine", 1.0),)
    assert [c.spec for c in AB] == ["square:1", "sine:1", "sine:0.5"]
    assert [c.label for c in AB] == ["square 1", "sine 1", "sine 0.5"]


@pytest.mark.parametrize("text, match", [
    ("tri:1", "condition 'tri:1': unknown waveform"),
    ("sine:0", r"depth must be in \(0, 1\]"),
    ("sine:1.5", r"depth must be in \(0, 1\]"),
    ("sine:nan", r"depth must be in \(0, 1\]"),
    ("sine:half", "is not a number"),
    ("sine:1,,square:1", "empty condition"),
    ("sine:1,sine:1.0", "listed twice"),
])
def test_parse_conditions_refuses(text, match):
    with pytest.raises(ValueError, match=match):
        parse_conditions(text)


def test_override_validates_conditions():
    with pytest.raises(ValueError, match="depth"):
        override(load_config("two_targets"), conditions=(Condition("sine", 2.0),))


def test_square_condition_warns_about_uneven_duty():
    warnings = config_warnings(_cfg())
    assert any("17 Hz" in w and "uneven" in w for w in warnings)
    assert not any("uneven" in w for w in config_warnings(_cfg((Condition("sine", 0.5),))))


def test_one_condition_keeps_the_old_trial_order():
    for seed in (1, 7, 99):
        old = make_trials(2, 5, seed=seed)
        assert make_trials(2, 5, seed=seed, n_conditions=1) == old
        assert {t.condition for t in old} == {0}


def test_every_block_holds_every_condition_target_pair_once():
    trials = make_trials(2, 4, seed=3, n_conditions=3)
    assert len(trials) == 24
    for b in range(4):
        block = [t for t in trials if t.block == b]
        assert sorted((t.condition, t.target) for t in block) == [
            (c, k) for c in range(3) for k in range(2)]
    assert [t.index for t in trials] == list(range(24))
    assert len(session_trials(_cfg(blocks=4), seed=3)) == 24
    with pytest.raises(ValueError, match="condition"):
        make_trials(2, 1, n_conditions=0)


# -- frames ---------------------------------------------------------------------------------

def test_each_trial_flickers_with_its_own_condition():
    cfg = _cfg()   # cue 3, stim 6, rest 3 frames
    trials = make_trials(2, 1, seed=5, n_conditions=3)
    frames = list(cued_frames(cfg, trials))
    assert len(frames) == 6 * 12
    for tr in trials:
        start = tr.index * 12
        assert frames[start].events == (f"cond:{tr.condition}", f"cue:{tr.target}")
        cond = AB[tr.condition]
        stim = [f for f in frames if f.trial == tr.index]
        for i, fr in enumerate(stim):
            np.testing.assert_array_equal(fr.lum, frame_luminances(
                cfg.freqs, cfg.phases, i, 60.0, cond.waveform, cond.depth))
    # one mid-grey still screen whatever the condition, so the next one is not given away
    for fr in frames:
        if fr.trial < 0:
            np.testing.assert_array_equal(fr.lum, [0.5, 0.5])
    np.testing.assert_array_equal(still_luminances(cfg), [0.5, 0.5])
    plain = override(cfg, conditions=())
    np.testing.assert_array_equal(still_luminances(plain),
                                  frame_luminances(plain.freqs, plain.phases, 0, 60.0))


def test_condition_marker_rides_on_the_onset_without_a_cue():
    trials = make_trials(2, 1, shuffle=False, n_conditions=3)
    frames = list(cued_frames(_cfg(cue_s=0.0), trials))
    assert frames[0].events == ("cond:0", "stim_on:0")


def test_status_text_shows_on_still_frames_only():
    frames = list(cued_frames(_cfg(), make_trials(2, 1, seed=1, n_conditions=3),
                              status=lambda: "scores"))
    assert all(fr.text == "" for fr in frames if fr.trial >= 0)
    assert all(fr.text == "scores" for fr in frames if fr.trial < 0)
    assert all(fr.prompt == "" for fr in frames)


def test_rating_prompt_waits_for_a_key_and_drops_early_presses():
    # each trial first drops whatever was pressed before its prompt (5, then 9)
    answers = iter([5, None, None, 3, 9, 2])
    trials = make_trials(2, 1, shuffle=False, n_conditions=1)[:2]
    frames = list(cued_frames(_cfg((Condition(),)), trials, rate=lambda: next(answers)))
    prompts = [i for i, fr in enumerate(frames) if fr.prompt == RATE_PROMPT]
    assert len(prompts) == 2                      # two empty polls after the dropped 5
    marked = [fr.events for fr in frames if any(e.startswith("rate:") for e in fr.events)]
    assert marked == [("rate:3",), ("rate:2",)]
    after = frames[prompts[-1] + 1]
    assert after.events == ("rate:3",) and after.trial < 0
    assert frames[-1].events == ("rate:2",)


def test_end_screen_holds_until_finish():
    left = iter([False, False, False, True])
    frames = list(cued_frames(_cfg(), make_trials(2, 1, n_conditions=3), status=lambda: "t",
                              finish=lambda: next(left)))
    assert [fr.prompt for fr in frames[-3:]] == [END_PROMPT] * 3
    assert all(fr.text == "t" for fr in frames[-3:])


class _TextRenderer:
    def __init__(self):
        self.t = 0.0
        self.calls = []

    def draw(self, lum, cue, **kw):
        self.calls.append(kw)

    def flip(self):
        self.t += 1 / 60
        return self.t

    def escape_pressed(self):
        return False


def test_play_passes_text_only_when_a_frame_has_some():
    ren = _TextRenderer()
    frames = cued_frames(_cfg(), make_trials(2, 1, n_conditions=3), status=lambda: "s")
    play(frames, ren, MarkerWriter("none"), 60.0)
    assert {tuple(sorted(kw)) for kw in ren.calls} == {(), ("prompt", "text")}
    assert ren.calls[0] == {"text": "s", "prompt": ""}


# -- the stdin status reader ----------------------------------------------------------------

def test_status_reader_keeps_the_latest_text():
    notes = []
    stream = io.StringIO('{"text": "a"}\nnot json\n{"text": 3}\n{"text": "b\\nc"}\n')
    reader = StatusReader(stream, note=notes.append)
    assert reader.text == "" and not reader.done
    reader.run()
    assert reader.text == "b\nc" and reader.done and reader.n_lines == 4
    assert len(notes) == 2 and "ignored status line 2" in notes[0]


# -- the PsychoPy renderer against a fake psychopy ------------------------------------------

class _Stim:
    def __init__(self, win, **kw):
        self.kw = kw
        self.renders = 0
        self.drawn = 0
        self._text = kw.get("text", "")

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        self.renders += 1
        self._text = value

    def draw(self):
        self.drawn += 1


class _Window:
    size = (3024, 1964)

    def __init__(self, **kw):
        self.t = 0.0

    def setMouseVisible(self, visible):
        pass

    def flip(self):
        self.t += 1 / 60
        return self.t

    def close(self):
        pass


def _renderer(monkeypatch, keys):
    from brainsync.stimulus.display import PsychoPyRenderer
    fake = types.ModuleType("psychopy")
    fake.visual = types.SimpleNamespace(Window=_Window, Rect=_Stim, TextStim=_Stim)
    fake.event = types.SimpleNamespace(getKeys=lambda keyList: [k for k in keys.pop(0)
                                                                if k in keyList]
                                       if keys else [], clearEvents=lambda: None)
    monkeypatch.setitem(sys.modules, "psychopy", fake)
    return PsychoPyRenderer(load_config("two_targets"))


def test_renderer_draws_text_and_renders_it_once(monkeypatch):
    r = _renderer(monkeypatch, [])
    assert r.status.kw["pos"][1] < -0.24 < 0.24 < r.prompt.kw["pos"][1]   # clear of the boxes
    assert r.status.kw["font"] == "Menlo"
    r.draw(np.zeros(2), -1)
    assert r.status.drawn == 0 and r.prompt.drawn == 0
    for _ in range(3):
        r.draw(np.zeros(2), -1, text="scores", prompt="")
    assert r.status.drawn == 3 and r.status.renders == 1 and r.prompt.drawn == 0
    r.draw(np.zeros(2), -1, text="new", prompt="rate")
    assert r.status.renders == 2 and r.prompt.text == "rate"


def test_renderer_rating_keeps_escape(monkeypatch):
    r = _renderer(monkeypatch, [["x"], ["2", "4"], ["escape"], []])
    assert r.rating() is None
    assert r.rating() == 4
    assert r.rating() is None and r.escape_pressed()   # polled by rating, not lost
    keys_left = [["space"]]
    r._keys = lambda keyList: keys_left.pop(0) if keys_left else []
    assert r.close_pressed() and not r.close_pressed()


# -- the command line -----------------------------------------------------------------------

class _Window60:
    instances: list = []

    def __init__(self, cfg):
        self.cfg = cfg
        self.t = 0.0
        self.texts: list[str] = []
        self.ratings = iter([4, 2, 5, 1, 3, 3, 2, 2, 1, 1, 5, 5])
        _Window60.instances.append(self)

    def measure_refresh(self):
        return 60.0

    def draw(self, lum, cue, text="", prompt=""):
        if text:
            self.texts.append(text)

    def flip(self):
        self.t += 1 / 60
        return self.t

    def escape_pressed(self):
        return False

    def rating(self):
        return next(self.ratings)

    def close_pressed(self):
        return False

    def close(self):
        pass


@pytest.fixture
def window(monkeypatch):
    import brainsync.stimulus.display as disp
    _Window60.instances = []
    monkeypatch.setattr(disp, "PsychoPyRenderer", _Window60)
    return _Window60


def test_main_runs_a_mixed_session_with_ratings_and_status(window, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"text": "board"}\n'))
    rc = cli.main(["--no-wait", "--blocks", "1", "--cue-s", "0.05", "--stim-s", "0.1",
                   "--rest-s", "0.05", "--seed", "2", "--conditions", "square:1,sine:0.5",
                   "--rate", "--status", "stdin"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    lines = err.splitlines()
    assert "conditions: square:1 sine:0.5" in lines
    assert lines.index("conditions: square:1 sine:0.5") < next(
        i for i, ln in enumerate(lines) if ln.startswith("[marker"))
    events = [ln.split("] ", 1)[1] for ln in lines if ln.startswith("[marker")]
    order = make_trials(2, 1, seed=2, n_conditions=2)
    assert [e for e in events if e.startswith("cond:")] == [f"cond:{t.condition}" for t in order]
    assert [e for e in events if e.startswith("rate:")] == ["rate:2", "rate:1", "rate:3",
                                                            "rate:2"]
    assert "board" in window.instances[0].texts   # the status reached the screen
    assert out.startswith("timing: 4 trials")


def test_main_plan_lists_condition_target_pairs(capsys):
    assert cli.main(["--plan", "--blocks", "1", "--seed", "3", "--conditions",
                     "square:1,sine:0.5"]) == 0
    out = capsys.readouterr().out
    assert "conditions square:1, sine:0.5 mixed per trial" in out
    assert "1 block(s) x 2 x 2 condition(s) = 4 trial(s)" in out
    pairs = [f"{t.target}/{('square:1', 'sine:0.5')[t.condition]}"
             for t in make_trials(2, 1, seed=3, n_conditions=2)]
    assert f"block 1/1: {' '.join(pairs)}" in out


@pytest.mark.parametrize("argv", [["--free", "--conditions", "sine:1"], ["--free", "--rate"],
                                  ["--free", "--status", "stdin"]])
def test_conditions_rate_and_status_need_a_cued_session(argv, capsys):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "cued session" in capsys.readouterr().err


def test_bad_conditions_are_a_clean_error(capsys):
    assert cli.main(["--plan", "--conditions", "sine:3"]) == 2
    assert "error: condition 'sine:3': depth" in capsys.readouterr().err
