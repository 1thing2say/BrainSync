"""The two sides of the bridge <-> display contract, checked against each other.

brainsync.voice_bridge writes the display's stdin and reads its stderr; brainsync.stimulus
(the free run with --feedback stdin --hold-still) does the reverse. Each side has its own tests
against stand-ins; these tests join the real code of both:

1. The exact stdin lines a Bridge writes for a whole session (hello_ack, a page, a trial, the
   confirm screen and its two-target trial, a page turn, a trial cut short by STOP, the
   disconnect) go through the display's real command line (brainsync.stimulus.__main__, with the
   bridge's own stimulus_args) and its feedback reader, frame by frame. Every frame must
   flicker exactly the targets the contract says, and the labels, centre text and box must be
   the ones the server sent.
2. The display's marker and refresh lines (format_marker, MarkerWriter and the lines
   __main__ prints) parse in the bridge as the same times and names.
3. A Bridge with its real PsychoPyDisplay launches the real display command line in a
   subprocess (only the window is a stand-in), takes the refresh from its output, sends
   stim_onset from its flicker_on marker, and relaunches it after Esc.

Run: pytest tests/test_bridge_display_contract.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from brainsync import voice_bridge as vb
from brainsync.stimulus import __main__ as cli
from brainsync.stimulus import load_config
from brainsync.stimulus.display import FLICKER_OFF, FLICKER_ON, MarkerWriter, format_marker
from brainsync.stimulus.feedback import CONTROL, parse_command
from brainsync.stimulus.flicker import frame_luminances
from tests.test_stimulus_commands import GatedStdin, _GatedWindow
from tests.test_voice_bridge import SLOTS, Peer, StubEngine, with_server

CONFIG = "four_diamond"
GAP = 4   # frames between two stdin lines in the display run
PAGE = {"s1": "Yes, let's go outside.", "s2": "Not right now.", "s3": "Maybe after lunch.",
        "s4": "More options"}
PAGE2 = {"s1": "I'm doing well.", "s2": "Could you help me?", "s3": "A drink, please.",
         "s4": "More options"}
CONFIRM = {"s1": "Say it", "s2": "", "s3": "", "s4": "Back"}


class PlanEngine(StubEngine):
    """StubEngine whose trials commit by plan: an index into each trial's active targets
    (None: no commit)."""

    def __init__(self, plan: list[int | None]) -> None:
        super().__init__(commit_to=None)
        self.plan = list(plan)

    def start_trial(self, trial_id: int, targets: list[int], t_start: float) -> None:
        self.commit_to = self.plan.pop(0) if self.plan else None
        super().start_trial(trial_id, targets, t_start)


def bridge_for(url: str, display, engine) -> vb.Bridge:
    args = vb.build_parser().parse_args(["--url", url, "-c", CONFIG, "--fake", "--no-stimulus",
                                         "--reconnect-s", "0.1", "--status-interval", "0.2"])
    return vb.Bridge(args, load_config(CONFIG), engine, display)


def labels_of(slots: dict[str, str]) -> tuple[str, ...]:
    return tuple(slots[s] for s in SLOTS)


async def bridge_session() -> tuple[list[str], dict[str, int]]:
    """Every stdin line a Bridge writes for one scripted session, and how many it had written
    at each named point."""
    display = vb.HeadlessDisplay(60.0)
    marks: dict[str, int] = {}

    def mark(name: str) -> None:
        marks[name] = len(display.lines)

    async def script(peer: Peer) -> None:
        mark("launch")
        await peer.handshake()   # hello_ack with labels One/Two/Three/More options, baseline 1
        mark("ack")
        await peer.send(type="display", text="", highlight=None, labels=PAGE)
        await peer.quiet(0.1)
        mark("page")
        await peer.send(type="stim_start", trial_id=1, active_slots=SLOTS)
        await peer.recv("stim_onset")
        assert (await peer.recv("selection"))["slot"] == "s2"
        mark("trial1")
        await peer.send(type="stim_stop", trial_id=1, reason="selected")
        await peer.quiet(0.1)
        mark("stop1")
        await peer.send(type="display", text=PAGE["s2"], labels=CONFIRM, highlight="s2")
        await peer.quiet(0.1)
        mark("confirm")
        await peer.send(type="stim_start", trial_id=2, active_slots=["s1", "s4"])
        await peer.recv("stim_onset")
        assert (await peer.recv("selection"))["slot"] == "s1"
        mark("trial2")
        await peer.send(type="stim_stop", trial_id=2, reason="selected")
        await peer.quiet(0.1)
        mark("stop2")
        await peer.send(type="display", text="", highlight=None, labels=PAGE2)
        await peer.quiet(0.1)
        mark("page2")
        await peer.send(type="stim_start", trial_id=3, active_slots=SLOTS)
        await peer.recv("stim_onset")
        mark("trial3")
        # STOP during a trial: stim_stop (paused), then pause (INTERFACE-v2-draft.md section 3)
        await peer.send(type="stim_stop", trial_id=3, reason="paused")
        await peer.send(type="pause", reason="stop_button")
        await peer.quiet(0.1)
        mark("pause")
        await peer.ws.close(4000)

    # trial 1 commits to s2, trial 2 (s1, s4) to s1 "Say it", trial 3 never
    await with_server(script, lambda url: bridge_for(url, display, PlanEngine([1, 0, None])))
    return list(display.lines), marks


def expected_lum(lines: list[str], k: int, frame: int, cfg) -> np.ndarray:
    """What the contract says frame ``frame`` shows once the first ``k`` lines are applied
    (line i applied at frame GAP * (i + 1)): still, or the active set of the latest
    {"flicker": true} flickering from phase 0 at that line's frame."""
    still = frame_luminances(cfg.freqs, cfg.phases, 0, cfg.refresh, cfg.waveform, cfg.depth)
    active = list(range(cfg.n_targets))
    on_at, on_set = None, active
    for i, line in enumerate(lines[:k]):
        cmd = parse_command(line, cfg.n_targets)
        if cmd.kind != CONTROL:
            continue
        if "active" in cmd.fields:
            active = list(cmd.fields["active"])
        if cmd.fields.get("flicker") is True:
            on_at, on_set = GAP * (i + 1), active
        elif cmd.fields.get("flicker") is False:
            on_at = None
    if on_at is None:
        return still
    lum = frame_luminances(cfg.freqs, cfg.phases, frame - on_at, cfg.refresh, cfg.waveform,
                           cfg.depth)
    mask = np.zeros(cfg.n_targets, dtype=bool)
    mask[on_set] = True
    return np.where(mask, lum, still)


async def test_bridge_lines_drive_the_real_display_as_the_contract_says(monkeypatch, capsys):
    import brainsync.stimulus.display as disp

    lines, marks = await bridge_session()
    n = len(lines)
    # A JSON line comes first: an integer line before it would end --hold-still by itself.
    assert lines[0] == vb.FLICKER_OFF and parse_command(lines[0], 4).kind == CONTROL
    # The ends of the trials, STOP and the disconnect each hold the flicker still.
    assert lines[marks["trial1"] - 2:marks["trial1"]] == ['{"active": [0, 1, 2, 3]}',
                                                          vb.FLICKER_ON]
    assert lines[marks["trial2"] - 2:marks["trial2"]] == ['{"active": [0, 3]}', vb.FLICKER_ON]
    tail = lines[marks["pause"]:]   # the disconnect's, and the bridge stopping
    assert tail and set(tail) == {vb.FLICKER_OFF}

    # The display, started with the bridge's own command line (minus the window).
    args = vb.build_parser().parse_args(["-c", CONFIG, "--no-wait"])
    argv = vb.stimulus_args(args)
    assert "--seconds" not in argv
    script = [(GAP * (i + 1), line) for i, line in enumerate(lines)] + [(GAP * (n + 1) + 2, None)]
    stdin = GatedStdin(script)
    _GatedWindow.stdin = stdin
    _GatedWindow.instances = []
    monkeypatch.setattr(disp, "PsychoPyRenderer", _GatedWindow)
    monkeypatch.setattr(sys, "stdin", stdin)
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert "feedback: ignored" not in err   # the display understood every line
    draws = _GatedWindow.instances[0].draws
    assert len(draws) >= GAP * (n + 1)
    cfg = load_config(CONFIG)

    # Every frame flickers exactly what the lines so far say, and nothing else.
    for f, d in enumerate(draws[:GAP * (n + 1)]):
        k = min(f // GAP, n)
        np.testing.assert_allclose(d["lum"], expected_lum(lines, k, f, cfg), atol=1e-12,
                                   err_msg=f"frame {f} after {k} line(s)")

    def at(name: str) -> dict:   # the last frame before the next line after ``name``
        return draws[GAP * marks[name] + GAP - 1]

    still = expected_lum(lines, 0, 0, cfg)
    assert at("ack")["labels"] == ("One", "Two", "Three", "More options")
    assert at("page")["labels"] == labels_of(PAGE) and at("page")["centre"] == ""
    assert at("page")["cue"] == -1
    assert at("trial1")["labels"] == labels_of(PAGE)   # labels_during_flicker = true
    assert not np.allclose(at("trial1")["lum"], still)
    np.testing.assert_allclose(at("stop1")["lum"], still)
    assert at("confirm")["labels"] == labels_of(CONFIRM)
    assert at("confirm")["centre"] == PAGE["s2"] and at("confirm")["cue"] == 1
    t2 = at("trial2")["lum"]
    assert not np.allclose(t2[[0, 3]], still[[0, 3]])
    np.testing.assert_allclose(t2[[1, 2]], still[[1, 2]])   # only "Say it" and "Back"
    assert at("page2")["labels"] == labels_of(PAGE2) and at("page2")["cue"] == -1
    assert at("page2")["centre"] == ""
    np.testing.assert_allclose(at("pause")["lum"], still)

    # The display's stderr, read the way the bridge reads it.
    err_lines = err.splitlines()
    rates = [vb.parse_refresh(ln) for ln in err_lines if vb.parse_refresh(ln)]
    assert rates == [("measured", 60.0)]
    markers = [(i, vb.parse_marker(ln)) for i, ln in enumerate(err_lines) if vb.parse_marker(ln)]
    assert [m[1] for _, m in markers] == ["flicker_on", "flicker_off"] * 3   # trials 1, 2, 3
    first_rate = next(i for i, ln in enumerate(err_lines) if vb.parse_refresh(ln))
    assert markers[0][0] > first_rate
    stamps = [m[0] for _, m in markers]
    assert stamps == sorted(stamps) and stamps[0] > 1.7e9   # unix seconds
    assert out.startswith("timing: 3 trials")


async def test_an_integer_line_never_starts_the_flicker(monkeypatch, capsys):
    """A highlight before any label (hello_ack without labels): the bridge's launch line
    keeps the display still, where the integer alone would end --hold-still."""
    import brainsync.stimulus.display as disp

    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await peer.recv("hello")
        await peer.send(type="hello_ack", protocol=2, roles={s: "option" for s in SLOTS})
        await peer.send(type="display", highlight="s3")
        await peer.quiet(0.1)
        await peer.ws.close(4000)

    await with_server(script, lambda url: bridge_for(url, display, PlanEngine([])))
    lines = list(display.lines)
    assert lines[:2] == [vb.FLICKER_OFF, "2"]
    script_lines = [(GAP * (i + 1), line) for i, line in enumerate(lines)]
    stdin = GatedStdin(script_lines + [(GAP * (len(lines) + 2), None)])
    _GatedWindow.stdin = stdin
    _GatedWindow.instances = []
    monkeypatch.setattr(disp, "PsychoPyRenderer", _GatedWindow)
    monkeypatch.setattr(sys, "stdin", stdin)
    assert cli.main(vb.stimulus_args(vb.build_parser().parse_args(["-c", CONFIG, "--no-wait"]))) == 0
    _, err = capsys.readouterr()
    cfg = load_config(CONFIG)
    still = expected_lum([], 0, 0, cfg)
    draws = _GatedWindow.instances[0].draws
    for f, d in enumerate(draws):
        np.testing.assert_allclose(d["lum"], still, err_msg=f"frame {f} flickered")
    assert draws[GAP * 2 + 1]["cue"] == 2   # the box moved, nothing else
    assert vb.parse_marker(err) is None and "flicker_on" not in err


def test_bridge_parses_the_display_marker_and_refresh_lines():
    t = 1792809201.51234
    out = io.StringIO()
    writer = MarkerWriter("unix", stream=out, clock=lambda: t)
    writer.emit([FLICKER_ON])
    writer.emit([FLICKER_OFF])
    parsed = [vb.parse_marker(line) for line in out.getvalue().splitlines()]
    assert parsed == [(pytest.approx(t, abs=1e-4), "flicker_on"),
                      (pytest.approx(t, abs=1e-4), "flicker_off")]
    # The names the bridge acts on are the display's own constants.
    assert parsed[0][1] == FLICKER_ON and parsed[1][1] == FLICKER_OFF
    assert vb.parse_marker(format_marker(t, FLICKER_ON)) == (pytest.approx(t, abs=1e-4), FLICKER_ON)
    # Other markers parse too; only flicker_on stamps a stim_onset.
    assert vb.parse_marker(format_marker(t, "stim_on:2"))[1] == "stim_on:2"
    # The refresh lines exactly as brainsync.stimulus.__main__ prints them.
    measured, adopted = 59.94, 60.0
    assert vb.parse_refresh(f"measured refresh: {measured:.2f} Hz") == ("measured", 59.94)
    assert vb.parse_refresh(f"using refresh {adopted:g} Hz") == ("using", 60.0)
    # Neither kind of line is taken for the other, and notes are neither.
    assert vb.parse_marker(f"measured refresh: {measured:.2f} Hz") is None
    assert vb.parse_refresh(format_marker(t, FLICKER_ON)) is None
    note = "feedback: ignored line 3: unknown key(s) ['scores']"
    assert vb.parse_marker(note) is None and vb.parse_refresh(note) is None


# ---------------------------------------------------------------------------
# The real display command line in a subprocess, launched by PsychoPyDisplay
# ---------------------------------------------------------------------------

WINDOW = r'''
"""brainsync.stimulus's real command line with a stand-in window: 59.94 Hz, flips paced at
60 Hz, Esc pressed once the file named by argv[1] exists (it is then removed)."""
import os, sys, time
from brainsync.stimulus import display

ESC = sys.argv.pop(1)


class Window:
    def __init__(self, cfg, cards=()):
        self.next = time.monotonic()

    def measure_refresh(self):
        return 59.94

    def draw(self, lum, cue, **kw):
        pass

    def flip(self):
        self.next += 1 / 60
        wait = self.next - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return time.time()

    def escape_pressed(self):
        if os.path.exists(ESC):
            os.remove(ESC)
            return True
        return False

    def close(self):
        pass


display.PsychoPyRenderer = Window
from brainsync.stimulus.__main__ import main
sys.exit(main(sys.argv[1:]))
'''


async def test_bridge_runs_the_real_display_process(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="voice_bridge")
    runner = tmp_path / "display_window.py"
    runner.write_text(WINDOW, encoding="utf-8")
    esc = tmp_path / "esc"
    argv = vb.stimulus_args(vb.build_parser().parse_args(["-c", CONFIG, "--no-wait"]))
    display = vb.PsychoPyDisplay(argv, 60.0, timeout=60,
                                 command=[sys.executable, str(runner), str(esc), *argv])

    async def script(peer: Peer) -> None:
        hello = await peer.handshake()
        assert hello["refresh_hz"] == 59.94   # from the display's "measured refresh" line
        sent = time.time()
        await peer.send(type="stim_start", trial_id=1, active_slots=["s1", "s4"])
        onset = await peer.recv("stim_onset", timeout=10)
        sel = await peer.recv("selection", timeout=5)   # committed at once, held for the onset
        assert onset["trial_id"] == 1 and sent - 0.05 <= onset["t_unix"] <= time.time()
        assert sel["slot"] == "s1" and sel["t_unix"] >= onset["t_unix"]
        await peer.send(type="stim_stop", trial_id=1, reason="selected")
        await peer.quiet(0.2)
        esc.touch()   # Esc: the display exits and the flicker with it
        while (await peer.recv("status", timeout=10))["display_ok"]:
            pass
        while not (await peer.recv("status", timeout=60))["display_ok"]:
            pass
        # The relaunched display, after the baseline the server sends on resume.
        await peer.send(type="baseline", baseline_id=2, seconds=0.1)
        assert (await peer.recv("baseline_done", timeout=5))["ok"] is True
        sent = time.time()
        await peer.send(type="stim_start", trial_id=2, active_slots=SLOTS)
        onset = await peer.recv("stim_onset", timeout=10)
        assert onset["trial_id"] == 2 and onset["t_unix"] >= sent - 0.05
        await peer.send(type="stim_stop", trial_id=2, reason="timeout")
        await peer.quiet(0.2)
        await peer.ws.close(4000)

    try:
        bridge, code = await with_server(
            script, lambda url: bridge_for(url, display, PlanEngine([0, None])))
    finally:
        await asyncio.to_thread(display.close)
    assert code == 0
    said = [r.getMessage() for r in caplog.records]
    assert not [m for m in said if "feedback: ignored" in m], said   # every line understood
    assert any("display down" in m for m in said)
    assert sum("display up at 59.94 Hz" in m for m in said) == 2
