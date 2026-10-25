"""The bridge's display commands: JSON lines on the free run's stdin, the flicker they turn
on and off for chosen targets only, the flicker_on / flicker_off markers, the text beside
each square, the four_diamond config and its fixation marks.

Nothing here opens a window or imports psychopy.
"""
from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
import types

import numpy as np
import pytest

from brainsync.stimulus import __main__ as cli
from brainsync.stimulus.config import load_config, parse_config
from brainsync.stimulus.display import (FLICKER_OFF, FLICKER_ON, FREE_ON, MarkerWriter, TextBox,
                                        free_frames, play, text_boxes, text_overlaps)
from brainsync.stimulus.feedback import (CONTROL, Command, Control, FeedbackReader,
                                         apply_control, initial_control, parse_command)
from brainsync.stimulus.flicker import frame_luminances

MARKER = re.compile(r"^\[marker (\d+\.\d{4})\] (flicker_on|flicker_off)$")


def _diamond(**stimulus):
    cfg = load_config("four_diamond")
    if stimulus:
        import dataclasses
        cfg = dataclasses.replace(cfg, **stimulus)
    return cfg


def _ctl(n: int = 4, *lines: dict) -> Control:
    state = initial_control(n)
    for fields in lines:
        state = apply_control(state, fields)
    return state


# -- parsing --------------------------------------------------------------------------------

def test_each_json_command_parses():
    four = ["Yes, let's go outside.", "Not right now.", "Maybe after lunch.", "More options"]
    cmd = parse_command(json.dumps({"labels": four}) + "\n", 4)
    assert cmd.kind == CONTROL and cmd.fields == {"labels": tuple(four)}
    assert parse_command('{"text": "Yes, let\'s go outside."}', 4).fields == {
        "text": "Yes, let's go outside."}
    assert parse_command('{"text": ""}', 4).fields == {"text": ""}
    assert parse_command('{"active": [0, 3]}', 4).fields == {"active": (0, 3)}
    assert parse_command('{"active": []}', 4).fields == {"active": ()}
    assert parse_command('{"flicker": true}', 4).fields == {"flicker": True}
    assert parse_command('  {"flicker": false}  \n', 4).fields == {"flicker": False}
    both = parse_command('{"active": [1, 2], "flicker": true}', 4).fields
    assert both == {"active": (1, 2), "flicker": True}
    assert parse_command('{"labels": ["", "", "", ""]}', 4).fields == {"labels": ("",) * 4}


def test_integer_card_and_flash_lines_still_parse():
    assert parse_command("3\n", 4) == Command("cue", 3)
    assert parse_command("-1", 4) == Command("cue", -1)
    assert parse_command("flash 2", 4) == Command("flash", 2)
    assert parse_command("card 0", 4, 1) == Command("card", 0)


@pytest.mark.parametrize("line", [
    "{", "{}", '{"text": }', "[1, 2]", '"text"', "{'text': 'single quotes'}",
    '{"scores": [0.1, 0.2]}',                          # reserved for phase D, not drawn
    '{"text": "hi", "colour": "red"}',                 # unknown key spoils the whole line
    '{"labels": ["a", "b", "c"]}',                     # one per target
    '{"labels": ["a", "b", "c", 4]}', '{"labels": "abcd"}',
    '{"text": 5}', '{"text": null}',
    '{"active": [4]}', '{"active": [-1]}', '{"active": [0, 0]}', '{"active": [true]}',
    '{"active": [1.0]}', '{"active": 1}',
    '{"flicker": 1}', '{"flicker": "true"}', '{"flicker": null}',
])
def test_bad_json_lines_are_refused(line):
    with pytest.raises(ValueError):
        parse_command(line, 4)


def test_reader_applies_json_lines_and_notes_bad_ones():
    notes: list[str] = []
    stream = io.StringIO(
        '{"labels": ["a", "b", "c", "More options"]}\n'
        '{"text": "centre"}\n'
        "not json at all\n"
        '{"active": [0, 3]}\n'
        '{"flicker": tru}\n'
        '{"flicker": true}\n'
        "2\n"
        '{"active": [1]}\n')
    r = FeedbackReader(stream, 4, note=notes.append)
    assert r.control == initial_control(4) and not r.control.used
    r.run()
    c = r.control
    assert c.used and c.labels == ("a", "b", "c", "More options") and c.text == "centre"
    assert c.flicker and c.serial == 1
    assert c.flickering == (0, 3)      # captured at the flicker-on
    assert c.active == (1,)            # waits for the next flicker-on
    assert r.current == 2 and r.done
    assert r.n_lines == 8 and r.n_ignored == 2
    assert [n.split(":")[0] for n in notes] == ["ignored line 3", "ignored line 5"]


def test_json_lines_do_not_count_as_heard():
    r = FeedbackReader(io.StringIO('{"flicker": false}\n{"text": "x"}\n'), 4,
                       note=lambda s: None)
    r.start().wait(5.0)
    assert r.control.used and r._heard.is_set() is False


def test_apply_control_order_and_serial():
    one = _ctl(4, {"active": [2], "flicker": True})
    assert one.flickering == (2,) and one.serial == 1
    again = apply_control(one, {"flicker": True})
    assert again.serial == 2 and again.flickering == (2,)
    off = apply_control(again, {"flicker": False})
    assert not off.flicker and off.serial == 2 and off.flickering == (2,)
    # labels and text survive a flicker line, and one line can set several keys
    full = _ctl(2, {"labels": ["x", "y"], "text": "t"}, {"flicker": True})
    assert full.labels == ("x", "y") and full.text == "t" and full.flickering == (0, 1)


# -- frame schedule -------------------------------------------------------------------------

class Script:
    """A Control that changes at given frame numbers, as a reader polled once per frame."""

    def __init__(self, n: int, changes: dict[int, dict]):
        self.state = initial_control(n)
        self.changes = changes
        self.frame = -1

    def __call__(self) -> Control:
        self.frame += 1
        if self.frame in self.changes:
            self.state = apply_control(self.state, self.changes[self.frame])
        return self.state


def _frames(cfg, changes, n, **kw):
    script = Script(cfg.n_targets, changes)
    gen = free_frames(cfg, control=script, **kw)
    return [next(gen) for _ in range(n)]


def test_flicker_only_between_on_and_off_for_the_active_targets():
    cfg = _diamond()
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform)
    frames = _frames(cfg, {0: {"labels": ["a", "b", "c", "d"]},
                           5: {"active": [0, 3], "flicker": True},
                           15: {"flicker": False}}, 20)
    for fr in frames[:5] + frames[15:]:
        np.testing.assert_array_equal(fr.lum, still)
        assert fr.trial == -1
    for i, fr in enumerate(frames[5:15]):
        full = frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform)
        np.testing.assert_array_equal(fr.lum[[0, 3]], full[[0, 3]])   # from phase 0
        np.testing.assert_array_equal(fr.lum[[1, 2]], still[[1, 2]])  # inactive: still
        assert fr.trial == 0
    assert any(not np.allclose(fr.lum[0], still[0]) for fr in frames[5:15])
    marks = [(k, fr.events) for k, fr in enumerate(frames) if fr.events]
    assert marks == [(5, (FLICKER_ON,)), (15, (FLICKER_OFF,))]
    assert all(fr.labels == ("a", "b", "c", "d") for fr in frames)


def test_each_flicker_on_restarts_the_phase_and_takes_the_new_active_set():
    cfg = _diamond()
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform)
    frames = _frames(cfg, {0: {"flicker": True},          # first run: all four
                           7: {"flicker": False},
                           9: {"active": [0, 3]},
                           10: {"flicker": True},         # second run: s1 and s4 only
                           13: {"active": [1]},           # waits for the next flicker-on
                           14: {"flicker": True}}, 18)    # repeated on: restart, new set
    for i, fr in enumerate(frames[0:7]):
        np.testing.assert_array_equal(
            fr.lum, frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform))
    for i, fr in enumerate(frames[10:14]):
        lum = frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform)
        np.testing.assert_array_equal(fr.lum[[0, 3]], lum[[0, 3]])
        np.testing.assert_array_equal(fr.lum[[1, 2]], still[[1, 2]])
    for i, fr in enumerate(frames[14:]):
        lum = frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform)
        assert fr.lum[1] == lum[1]
        np.testing.assert_array_equal(fr.lum[[0, 2, 3]], still[[0, 2, 3]])
    events = [(k, fr.events) for k, fr in enumerate(frames) if fr.events]
    assert events == [(0, (FLICKER_ON,)), (7, (FLICKER_OFF,)), (10, (FLICKER_ON,)),
                      (14, (FLICKER_ON,))]
    assert [fr.trial for fr in frames[:8]] == [0] * 7 + [-1]
    assert {fr.trial for fr in frames[10:14]} == {1} and {fr.trial for fr in frames[14:]} == {2}


def test_hold_still_waits_for_flicker_true_not_for_integer_lines():
    cfg = _diamond()
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform)
    script = Script(4, {0: {"text": "Hello"}, 30: {"flicker": True}})
    heard = iter([False] * 2 + [True] * 100)   # an integer line arrives at frame 2
    gen = free_frames(cfg, control=script, hold=lambda: not next(heard), cue=lambda: 2)
    frames = [next(gen) for _ in range(35)]
    for fr in frames[:30]:
        np.testing.assert_array_equal(fr.lum, still)
        assert fr.cue == 2 and fr.centre == "Hello"   # the box moves, the flicker waits
    assert frames[30].events == (FLICKER_ON,)
    assert not np.allclose(frames[31].lum, still)


def test_integer_only_decoder_keeps_the_old_hold_still():
    cfg = _diamond()
    script = Script(4, {})                     # never a JSON line: Control.used stays false
    heard = iter([False] * 3 + [True] * 100)
    frames = list(free_frames(cfg, seconds=0.1, control=script, hold=lambda: not next(heard),
                              cue=lambda: 1))
    assert len(frames) == 3 + 6
    for i, fr in enumerate(frames[3:]):
        np.testing.assert_array_equal(
            fr.lum, frame_luminances(cfg.freqs, cfg.phases, i, cfg.refresh, cfg.waveform))
        # the legacy free run marks only its first flicker frame: "free_on", the onset a
        # phase-locked decoder reads (never "flicker_on", which is the JSON lines' marker)
        assert fr.events == ((FREE_ON,) if i == 0 else ()) and fr.cue == 1


def test_a_json_line_stops_a_flicker_started_by_an_integer_line():
    cfg = _diamond()
    frames = _frames(cfg, {4: {"labels": ["", "", "", ""]}}, 8)
    # the integer-started flicker's first frame is marked free_on (the legacy onset)
    assert [fr.events for fr in frames] == \
        [(FREE_ON,)] + [()] * 3 + [(FLICKER_OFF,)] + [()] * 3
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform)
    for fr in frames[4:]:
        np.testing.assert_array_equal(fr.lum, still)


def test_seconds_counts_flicker_frames_and_stop_ends_the_run():
    cfg = _diamond()
    script = Script(4, {0: {"flicker": False}, 10: {"flicker": True}})
    frames = list(free_frames(cfg, seconds=0.1, control=script))
    assert len(frames) == 10 + 6
    n = iter(range(100))
    frames = list(free_frames(cfg, control=Script(4, {0: {"flicker": True}}),
                              stop=lambda: next(n) >= 5))
    assert len(frames) == 5


def test_labels_hidden_during_flicker_when_the_config_says_so():
    cfg = _diamond(labels_during_flicker=False)
    frames = _frames(cfg, {0: {"labels": ["a", "b", "c", "d"], "text": "mid"},
                           3: {"flicker": True}, 6: {"flicker": False}}, 9)
    assert [fr.labels for fr in frames[:3]] == [("a", "b", "c", "d")] * 3
    assert all(fr.labels == () and fr.centre == "" for fr in frames[3:6])
    assert all(fr.centre == "mid" for fr in frames[6:])


# -- markers through play() -----------------------------------------------------------------

class TextRenderer:
    """Records what play() draws, including the decoder's labels and centre text."""

    def __init__(self, refresh: float):
        self.period = 1.0 / refresh
        self.t = 0.0
        self.flips = 0
        self.draws: list[dict] = []
        self.log: list[str] = []

    def draw(self, lum, cue, card=-1, flash=-1, text="", prompt="", labels=(), centre=""):
        self.draws.append({"lum": np.array(lum), "cue": cue, "labels": tuple(labels),
                           "centre": centre})

    def flip(self):
        self.t += self.period
        self.flips += 1
        self.log.append(f"flip {self.flips}")
        return self.t

    def escape_pressed(self):
        return False


def test_markers_are_stamped_right_after_the_flip_that_shows_them():
    cfg = _diamond()
    ren = TextRenderer(60.0)
    out = io.StringIO()

    class Writer(MarkerWriter):
        def emit(self, events):
            if events:
                ren.log.append("emit " + ",".join(events))
            super().emit(events)

    frames = _frames(cfg, {0: {"labels": ["a", "", "", "NEXT"]},
                           3: {"active": [0, 3], "flicker": True},
                           9: {"flicker": False}}, 12)
    timing = play(frames, ren, Writer("unix", stream=out, clock=lambda: ren.t), 60.0)
    lines = out.getvalue().splitlines()
    assert [MARKER.match(ln).group(2) for ln in lines] == ["flicker_on", "flicker_off"]
    on, off = (float(MARKER.match(ln).group(1)) for ln in lines)
    # stamped at the flip of frame 3 (the 4th flip) and frame 9 (the 10th)
    assert on == pytest.approx(4 / 60, abs=1e-4) and off == pytest.approx(10 / 60, abs=1e-4)
    assert ren.log.index("emit flicker_on") == ren.log.index("flip 4") + 1
    assert ren.log.index("emit flicker_off") == ren.log.index("flip 10") + 1
    assert ren.draws[0]["labels"] == ("a", "", "", "NEXT")
    # the flicker run is timed like a cued trial
    assert len(timing.trials) == 1 and timing.trials[0].duration == pytest.approx(6 / 60)


def test_play_passes_text_only_when_there_is_some():
    cfg = _diamond()
    calls = []

    class Plain:
        def draw(self, lum, cue, **kw):
            calls.append(kw)

        def flip(self):
            return 0.0

        def escape_pressed(self):
            return False

    frames = _frames(cfg, {0: {"labels": ["", "", "", ""]}, 2: {"text": "hi"}}, 3)
    play(frames, Plain(), MarkerWriter("none"), 60.0)
    assert calls[:2] == [{}, {}]
    assert calls[2] == {"labels": ("", "", "", ""), "centre": "hi"}


# -- the whole display process, stdin to markers --------------------------------------------

class GatedStdin:
    """stdin whose line k only arrives once the fake display has flipped ``at`` frames,
    and each flip waits until the reader has applied every line due by then, so the frame
    a command lands on is exact. A None line is EOF."""

    def __init__(self, script: list[tuple[int, str | None]]):
        self.script = script
        self.calls = 0
        self.frame = 0
        self.cv = threading.Condition()

    def readline(self) -> str:
        with self.cv:
            i = self.calls
            self.calls += 1
            self.cv.notify_all()
            if i >= len(self.script):
                return ""
            at, line = self.script[i]
            self.cv.wait_for(lambda: self.frame >= at, timeout=10)
            return "" if line is None else line + "\n"

    def advance(self, frame: int) -> None:
        with self.cv:
            self.frame = frame
            self.cv.notify_all()
            due = sum(1 for at, line in self.script if at <= frame and line is not None)
            # the reader asking for line due + 1 means line due was applied
            self.cv.wait_for(lambda: self.calls > due, timeout=10)
            eof = any(line is None and at <= frame for at, line in self.script)
        if eof:
            time.sleep(0.1)   # the reader thread marks EOF right after its last read


class _GatedWindow(TextRenderer):
    stdin: GatedStdin
    instances: list = []

    def __init__(self, cfg):
        super().__init__(60.0)
        self.cfg = cfg
        _GatedWindow.instances.append(self)

    def flip(self):
        t = super().flip()
        self.stdin.advance(self.flips)
        return t

    def measure_refresh(self):
        return 60.0

    def close(self):
        pass


def test_main_runs_the_bridge_command_line_until_eof(monkeypatch, capsys):
    import brainsync.stimulus.display as disp
    script = [
        (0, '{"labels": ["Yes.", "Not now.", "Maybe.", "More options"], "text": ""}'),
        (0, "-1"),                       # an integer before the flicker: box only
        (5, '{"active": [0, 1, 2, 3]}'),
        (5, '{"flicker": true}'),
        (12, '{"flicker": false}'),
        (14, "0"),                       # the picked box
        (14, '{"text": "Yes.", "labels": ["Say it", "", "", "Back"]}'),
        (16, "not a command"),
        (18, '{"active": [0, 3]}'),
        (18, '{"flicker": true}'),
        (24, '{"flicker": false}'),
        (27, None),
    ]
    stdin = GatedStdin(script)
    _GatedWindow.stdin = stdin
    _GatedWindow.instances = []
    monkeypatch.setattr(disp, "PsychoPyRenderer", _GatedWindow)
    monkeypatch.setattr(sys, "stdin", stdin)
    rc = cli.main(["-c", "four_diamond", "--free", "--feedback", "stdin", "--hold-still",
                   "--markers", "unix", "--no-wait"])
    out, err = capsys.readouterr()
    assert rc == 0
    lines = err.splitlines()
    ready = lines.index("measured refresh: 60.00 Hz")
    marks = [MARKER.match(ln) for ln in lines if ln.startswith("[marker")]
    assert all(marks) and lines.index(next(ln for ln in lines if ln.startswith("[marker"))) \
        > ready
    assert [m.group(2) for m in marks] == ["flicker_on", "flicker_off"] * 2
    stamps = [float(m.group(1)) for m in marks]
    assert stamps == sorted(stamps) and stamps[0] > 1.7e9           # unix seconds
    assert any("ignored line 8" in ln for ln in lines)
    win = _GatedWindow.instances[0]
    cfg = load_config("four_diamond")
    still = frame_luminances(cfg.freqs, cfg.phases, 0, 60.0, cfg.waveform)
    draws = win.draws
    assert 27 <= len(draws) < 40                                # EOF ends the run
    for k in [*range(0, 5), *range(12, 18), *range(24, len(draws))]:
        np.testing.assert_array_equal(draws[k]["lum"], still)
    for i, d in enumerate(draws[5:12]):                         # all four, from phase 0
        np.testing.assert_array_equal(
            d["lum"], frame_luminances(cfg.freqs, cfg.phases, i, 60.0, cfg.waveform))
    for i, d in enumerate(draws[18:24]):                        # confirm: s1 and s4 only
        lum = frame_luminances(cfg.freqs, cfg.phases, i, 60.0, cfg.waveform)
        np.testing.assert_array_equal(d["lum"][[0, 3]], lum[[0, 3]])
        np.testing.assert_array_equal(d["lum"][[1, 2]], still[[1, 2]])
    assert draws[3]["labels"] == ("Yes.", "Not now.", "Maybe.", "More options")
    assert draws[3]["cue"] == -1 and draws[14]["cue"] == 0
    assert draws[15]["labels"] == ("Say it", "", "", "Back") and draws[15]["centre"] == "Yes."
    assert out.startswith("timing: 2 trials, 0 dropped")


# -- four_diamond and the text layout -------------------------------------------------------

def test_four_diamond_config():
    cfg = load_config("four_diamond")
    np.testing.assert_array_equal(cfg.freqs, [13.0, 15.0, 17.0, 11.0])
    np.testing.assert_array_equal(cfg.phases, [0.0] * 4)
    assert [t.pos for t in cfg.targets] == [(0.0, 0.33), (-0.55, 0.0), (0.55, 0.0),
                                            (0.0, -0.33)]
    assert cfg.size == 0.3 and cfg.waveform == "sine" and cfg.refresh == 60.0
    assert cfg.fixation and cfg.positioned and cfg.hold_cue and cfg.labels_during_flicker
    s = cfg.session
    assert (s.cue_s, s.stim_s, s.rest_s) == (1.0, 4.0, 3.0) and 6 <= s.blocks <= 8
    # the top cue box stays on screen
    assert max(abs(t.pos[1]) for t in cfg.targets) + cfg.reach <= 0.5


def test_positions_layout_parsing_and_errors():
    base = {"targets": {"freqs": [13.0, 15.0]}, "display": {"refresh_hz": 60.0}}
    cfg = parse_config({**base, "layout": {"size": 0.2, "positions": [[0, 0.2], [0.3, -0.1]],
                                           "x_offset": 0.1}})
    assert [t.pos for t in cfg.targets] == [pytest.approx((0.1, 0.2)),
                                            pytest.approx((0.4, -0.1))]
    for layout, match in [
        ({"size": 0.2, "positions": [[0, 0]]}, "1 positions for 2"),
        ({"size": 0.2, "positions": [[0, 0], [0.1, 0.1]]}, "overlap"),
        ({"size": 0.2, "positions": [[0, 0], [0.3, 0]], "cols": 2}, "drop"),
        ({"size": 0.2, "positions": [[0, 0], [0.3]]}, r"\[x, y\]"),
        ({"size": 0.2, "positions": [[0, 0], [True, 0]]}, r"\[x, y\]"),
        ({"size": 0.2, "positions": "diamond"}, "pairs"),
        ({"size": 0.2, "positions": [[0, 0], [0, 0.6]]}, "vertically"),
    ]:
        with pytest.raises(ValueError, match=match):
            parse_config({**base, "layout": layout})
    with pytest.raises(ValueError, match="fixation_size"):
        parse_config({**base, "layout": {"size": 0.2, "positions": [[0, 0], [0.3, 0]]},
                      "stimulus": {"fixation": True, "fixation_size": 0.25}})


def test_text_never_covers_a_square_or_other_text():
    for aspect in (1.5, 1.54, 1.6, 16 / 9):
        assert text_overlaps(load_config("four_diamond"), aspect) == []
        assert text_overlaps(load_config("two_targets"), aspect) == []
    slots, centre = text_boxes(load_config("four_diamond"))
    # each sentence sits beside its own square, in its own corner of the screen
    assert [b.side for b in slots] == ["right", "above", "below", "left"]
    assert centre.pos == (0.0, 0.0) and centre.width >= 0.5
    corners = [(np.sign(b.rect[0] + b.rect[2]), np.sign(b.rect[1] + b.rect[3])) for b in slots]
    assert sorted(corners) == sorted([(1, 1), (-1, 1), (1, -1), (-1, -1)])
    for b in slots:
        assert 0.3 <= b.width <= 0.44 and b.height >= 3 * 0.03


def test_text_overlap_check_catches_a_bad_layout():
    crowded = parse_config({"targets": {"freqs": [13.0, 15.0, 17.0]},
                            "layout": {"size": 0.3,
                                       "positions": [[0, 0.3], [-0.2, -0.2], [0.2, -0.2]]},
                            "display": {"refresh_hz": 60.0}})
    assert text_overlaps(crowded)
    box = TextBox((0.0, 0.0), 0.4, 0.1, ("left", "top"), "left")
    assert box.rect == pytest.approx((0.0, -0.1, 0.4, 0.0))


# -- the PsychoPy renderer against a fake psychopy ------------------------------------------

class _Stim:
    def __init__(self, win, **kw):
        self.win = win
        self.kw = kw
        self.text = kw.get("text")
        self.fillColor = kw.get("fillColor")
        self.text_sets = 0
        self.drawn = 0

    def __setattr__(self, name, value):
        if name == "text" and "text" in self.__dict__:
            self.__dict__["text_sets"] += 1
        object.__setattr__(self, name, value)

    def draw(self):
        self.drawn += 1
        self.win.order.append(self)


class _Window:
    size = (2880, 1800)   # 16:10

    def __init__(self, **kw):
        self.kw = kw
        self.order: list[_Stim] = []
        self.flips = 0

    def flip(self):
        self.flips += 1
        return self.flips / 60.0

    def setMouseVisible(self, visible):
        pass

    def close(self):
        pass


def _fake_psychopy(monkeypatch):
    fake = types.ModuleType("psychopy")
    fake.visual = types.SimpleNamespace(Window=_Window, Rect=_Stim, TextStim=_Stim)
    fake.event = types.SimpleNamespace(getKeys=lambda keyList: [], clearEvents=lambda: None)
    monkeypatch.setitem(sys.modules, "psychopy", fake)


def test_renderer_draws_fixation_labels_and_centre(monkeypatch, capsys):
    from brainsync.stimulus.display import SLOT_TEXT_HEIGHT, PsychoPyRenderer
    _fake_psychopy(monkeypatch)
    cfg = load_config("four_diamond")
    r = PsychoPyRenderer(cfg)
    assert len(r.fixations) == 8                    # a plus (two bars) per square
    assert [m.kw["pos"] for m in r.fixations[::2]] == [t.pos for t in cfg.targets]
    assert r.fixations[0].kw["size"] == pytest.approx((0.03, 0.006))
    assert all(m.kw["fillColor"] == cfg.cue_color for m in r.fixations)
    slots, centre = text_boxes(cfg, 1.6)
    for stim, box in zip(r.slot_texts, slots):
        assert stim.kw["pos"] == box.pos and stim.kw["wrapWidth"] == box.width
        assert (stim.kw["anchorHoriz"], stim.kw["anchorVert"]) == box.anchor
        assert stim.kw["alignText"] == box.align and stim.kw["height"] == SLOT_TEXT_HEIGHT
    assert r.centre.kw["pos"] == centre.pos
    lum = np.full(4, 0.5)
    r.win.order.clear()
    r.draw(lum, -1)
    # no decoder text: squares then fixation marks, nothing else
    assert r.win.order == r.squares + r.fixations
    r.win.order.clear()
    labels = ("Yes.", "", "Maybe.", "More options")
    r.draw(lum, 3, labels=labels, centre="Hi")
    drawn = [r.slot_texts[k] for k in (0, 2, 3)]
    assert r.win.order == r.squares + r.fixations + drawn + [r.centre, r.boxes[3]]
    assert [s.text for s in drawn] == ["Yes.", "Maybe.", "More options"]
    sets = [s.text_sets for s in r.slot_texts]
    for _ in range(5):                               # unchanged text is not re-rendered
        r.draw(lum, -1, labels=labels, centre="Hi")
    assert [s.text_sets for s in r.slot_texts] == sets
    assert capsys.readouterr().err == ""


def test_renderer_warns_once_when_decoder_text_cannot_fit(monkeypatch, capsys):
    from brainsync.stimulus.display import PsychoPyRenderer
    _fake_psychopy(monkeypatch)
    r = PsychoPyRenderer(load_config("one_target_15"))
    r.draw(np.zeros(1), -1)
    assert capsys.readouterr().err == ""             # no text yet: nothing to say
    r.draw(np.zeros(1), -1, labels=("x",))
    r.draw(np.zeros(1), -1, labels=("x",))
    err = capsys.readouterr().err
    assert "warning: decoder text:" in err and err.count("covers target 0") == 1


# -- review fixes: hold before control, a rejected JSON line, text on screen, start screen ---

def test_hold_is_read_before_control_so_a_burst_never_flickers():
    """The reader can apply a JSON line and then an integer line between two polls. Read in
    the other order, a frame would see an unused Control and an ended hold, and flicker."""
    cfg = _diamond()
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform)
    state = {"ctl": initial_control(4), "polls": 0}

    def hold() -> bool:
        state["polls"] += 1
        if state["polls"] == 3:   # the burst lands while this frame is being built
            state["ctl"] = apply_control(state["ctl"], {"flicker": False})
            return False          # ...and its integer line ended --hold-still
        return state["polls"] < 3

    gen = free_frames(cfg, control=lambda: state["ctl"], hold=hold, cue=lambda: 1)
    frames = [next(gen) for _ in range(6)]
    for fr in frames:
        np.testing.assert_array_equal(fr.lum, still)
        assert fr.events == () and fr.trial == -1


def test_a_rejected_json_line_still_ends_the_integer_flicker_mode():
    notes: list[str] = []
    r = FeedbackReader(io.StringIO('{"scores": [0.1, 0.2, 0.3, 0.4]}\n2\n'), 4,
                       note=notes.append)
    r.run()
    assert r.control.used and not r.control.flicker and r.current == 2
    assert notes and notes[0].startswith("ignored line 1")
    cfg = _diamond()
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform)
    gen = free_frames(cfg, control=lambda: r.control, hold=lambda: not r.heard,
                      cue=lambda: r.current)
    for _ in range(5):   # the integer line only moves the box
        fr = next(gen)
        np.testing.assert_array_equal(fr.lum, still)
        assert fr.cue == 2


@pytest.mark.parametrize("name", ["four_diamond", "two_targets", "one_target_15", "reels",
                                  "swipe", "chess"])
def test_status_prompt_and_start_prompt_stay_on_screen(name):
    from brainsync.stimulus.display import start_prompt_box, status_prompt_boxes
    cfg = load_config(name)
    for aspect in (cfg.display.aspect, 16 / 9):
        status, prompt = status_prompt_boxes(cfg, aspect)
        for box in (status, prompt):
            left, bottom, right, top = box.rect
            assert -0.5 <= bottom and top <= 0.5, (name, box)
            assert -aspect / 2 <= left and right <= aspect / 2, (name, box)
        start = start_prompt_box(cfg, aspect)
        for t in cfg.targets:
            foot = (t.pos[0] - cfg.reach, t.pos[1] - cfg.reach, t.pos[0] + cfg.reach,
                    t.pos[1] + cfg.reach)
            r = start.rect
            assert not (r[0] < foot[2] and foot[0] < r[2] and r[1] < foot[3]
                        and foot[1] < r[3]), f"{name}: start prompt covers target {t.index}"


def test_four_diamond_status_and_prompt_sit_between_the_squares():
    from brainsync.stimulus.display import status_prompt_boxes
    cfg = load_config("four_diamond")
    status, prompt = status_prompt_boxes(cfg, 1.5)
    _, centre = text_boxes(cfg, 1.5)
    assert status.side == prompt.side == "centre"
    assert prompt.rect[1] >= status.rect[3]          # prompt above, status below the middle
    for box in (status, prompt):
        for t in cfg.targets:
            foot = (t.pos[0] - cfg.reach, t.pos[1] - cfg.reach, t.pos[0] + cfg.reach,
                    t.pos[1] + cfg.reach)
            r = box.rect
            assert not (r[0] < foot[2] and foot[0] < r[2] and r[1] < foot[3] and foot[1] < r[3])


def test_renderer_places_status_and_prompt_from_the_layout(monkeypatch):
    from brainsync.stimulus.display import PsychoPyRenderer, status_prompt_boxes
    _fake_psychopy(monkeypatch)
    cfg = load_config("four_diamond")
    r = PsychoPyRenderer(cfg)
    status, prompt = status_prompt_boxes(cfg, 2880 / 1800)
    assert r.status.kw["pos"] == status.pos and r.status.kw["anchorVert"] == "top"
    assert r.prompt.kw["pos"] == prompt.pos and r.prompt.kw["anchorVert"] == "bottom"


def test_start_screen_reads_stdin_and_ends_at_eof(monkeypatch, capsys):
    """The bridge's display (no --no-wait) reads its lines on the start screen, and EOF there
    (the bridge stopping) closes the window instead of leaving it up."""
    import brainsync.stimulus.display as disp
    _fake_psychopy(monkeypatch)
    seen = []
    real = disp.PsychoPyRenderer.wait_for_start

    def spy(self, still, text=disp.START_PROMPT, stop=None):
        seen.append(stop)
        real(self, still, text, stop)

    monkeypatch.setattr(disp.PsychoPyRenderer, "wait_for_start", spy)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"flicker": false}\n{"text": "hi"}\n'))
    rc = cli.main(["-c", "four_diamond", "--free", "--feedback", "stdin", "--hold-still",
                   "--markers", "unix"])
    err = capsys.readouterr().err
    assert rc == 0 and seen and seen[0] is not None
    assert "stdin closed before start" in err and "[marker" not in err
