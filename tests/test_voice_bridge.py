"""brainsync.voice_bridge: the protocol-2 bridge (INTERFACE-v2-draft.md) between app.server and
the decoder + PsychoPy display.

The first test runs the real bridge (FakeBoard in fast virtual time, real FBCCA decoder,
headless display) against tools/contract_check.py in-process, the way
tests/test_contract_check.py runs the fake decoder. The rest drive a Bridge with a stub engine
and a recording display stand-in against small scripted servers, so each rule is checked on its
own and fast: the display-command mapping, not_ready answers, no selection before its onset,
flicker off on disconnect and pause, an `error` keeps it quiet, close code 4000 stops it, and a
display that exits is reported and relaunched.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from websockets.asyncio.server import serve

from app.config import load_config
from app.schemas import DecoderHello, hello_problems, parse_decoder_to_server
from brainsync import voice_bridge as vb
from brainsync.stimulus import load_config as load_stimulus
from tools.contract_check import Report, build_parser, serve_once

FOUR = """
name = "four_test"

[targets]
freqs = [13.0, 15.0, 17.0, 11.0]
phases_pi = [0.0, 0.0, 0.0, 0.0]
labels = ["13", "15", "17", "11"]

[layout]
rows = 2
cols = 2
size = 0.3
spacing = 0.6

[stimulus]
waveform = "sine"

[display]
refresh_hz = 60.0
refresh_tolerance_hz = 1.0
"""
SLOTS = ["s1", "s2", "s3", "s4"]
ON, OFF = vb.FLICKER_ON, vb.FLICKER_OFF


@pytest.fixture
def four(tmp_path: Path) -> Path:
    """A 13/15/17/11 Hz stimulus config like four_diamond.toml (independent of the shipped one)."""
    path = tmp_path / "four_test.toml"
    path.write_text(FOUR, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_display_lines_map_slots_to_target_indices():
    labels = ["", "", "", ""]
    lines, labels = vb.display_lines(
        {"type": "display", "text": "", "highlight": None,
         "labels": {"s1": "Yes, let's go.", "s2": "Not now.", "s3": "Maybe later.", "s4": "More options"}},
        SLOTS, labels)
    assert lines == ['{"labels": ["Yes, let\'s go.", "Not now.", "Maybe later.", "More options"]}',
                     '{"text": ""}', "-1"]
    assert labels == ["Yes, let's go.", "Not now.", "Maybe later.", "More options"]
    # The confirm screen: only the labels it names change; a highlight slot becomes its index.
    lines, labels = vb.display_lines(
        {"type": "display", "labels": {"s1": "Say it", "s4": "Back"}, "highlight": "s3"}, SLOTS, labels)
    assert lines == ['{"labels": ["Say it", "Not now.", "Maybe later.", "Back"]}', "2"]
    # Left-out fields are unchanged (no line); unknown slots are ignored.
    lines, labels = vb.display_lines({"type": "display", "text": "Say this?",
                                      "labels": {"s9": "x"}}, SLOTS, labels)
    assert lines == ['{"text": "Say this?"}']
    assert labels == ["Say it", "Not now.", "Maybe later.", "Back"]
    assert vb.display_lines({"type": "display"}, SLOTS, labels)[0] == []


def test_active_line_and_output_parsing():
    assert vb.active_line([0, 3]) == '{"active": [0, 3]}'
    assert ON == '{"flicker": true}' and OFF == '{"flicker": false}'
    assert vb.parse_marker("[marker 1792809201.512000] flicker_on") == (1792809201.512, "flicker_on")
    assert vb.parse_marker("[marker 1792809206.5] flicker_off") == (1792809206.5, "flicker_off")
    assert vb.parse_marker("measured refresh: 59.94 Hz") is None
    assert vb.parse_refresh("measured refresh: 59.94 Hz") == ("measured", 59.94)
    assert vb.parse_refresh("using refresh 60 Hz") == ("using", 60.0)
    assert vb.parse_refresh("[marker 1.0] flicker_on") is None


def test_hello_matches_the_server_config(four):
    cfg = load_stimulus(four)
    hello = vb.hello_message(cfg, 59.94, "brainsync-fbcca",
                             {"window_s": 1.5, "step_s": 0.25, "votes": 4}, "fbcca_minus_baseline")
    parsed = DecoderHello.model_validate(hello)
    assert parsed.protocol == 2 and [s.slot for s in parsed.slots] == SLOTS
    assert [s.freq_hz for s in parsed.slots] == [13.0, 15.0, 17.0, 11.0]
    app_cfg = load_config()
    assert hello_problems(parsed, app_cfg.stim.slots, app_cfg.stim.waveform,
                          app_cfg.display.expected_refresh_hz) == []


def test_bridge_names_say_fake_with_the_fake_board():
    p = vb.build_parser()
    assert vb.bridge_name(p.parse_args(["--fake"])) == "fake-brainsync-fbcca"
    assert vb.bridge_name(p.parse_args(["--classifier", "cca"])) == "brainsync-cca"


@pytest.mark.parametrize("argv", [["--fast"], ["--window", "0"], ["--votes", "0"],
                                  ["--reconnect-s", "0"], ["--status-interval", "-1"],
                                  ["--names", "a,b"]])
def test_bad_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        vb.main(argv)
    assert exc.value.code == 2


def test_missing_config_exits_2(capsys):
    assert vb.main(["--fake", "--no-stimulus", "-c", "no_such_config"]) == 2
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The real bridge passes tools/contract_check.py
# ---------------------------------------------------------------------------


BASE = load_config()
CHECK_CFG = BASE.model_copy(update={
    "stim": BASE.stim.model_copy(update={"cue_s": 0.02, "rest_s": 0.02, "max_trial_s": 6.0}),
})


async def test_fake_bridge_passes_contract_check(four):
    out = io.StringIO()
    report = Report(out=out)
    args = build_parser(CHECK_CFG).parse_args([
        "--host", "127.0.0.1", "--port", "0", "--trials", "4", "--baseline-s", "0.5",
        "--baseline-timeout", "10", "--hello-timeout", "10", "--quiet-s", "0.5", "--wait", "20",
    ])
    bound: asyncio.Queue[int] = asyncio.Queue()
    check = asyncio.create_task(serve_once(CHECK_CFG, args, report, bound.put_nowait))
    port = await asyncio.wait_for(bound.get(), 5)
    bridge_args = vb.build_parser().parse_args([
        "--url", f"ws://127.0.0.1:{port}/ws/decoder", "-c", str(four), "--fake", "--fast",
        "--no-stimulus", "--no-reconnect", "--status-interval", "0.2", "--seed", "3"])
    client = asyncio.create_task(vb.serve(bridge_args))
    await asyncio.wait_for(check, 60)
    code = await asyncio.wait_for(client, 10)
    text = out.getvalue()
    assert report.ok, text
    assert code == 0
    assert "PASS  hello matches config/default.yaml" in text
    assert "PASS  baseline 1 recorded" in text and "PASS  baseline 2 recorded" in text
    assert "INFO  trial 2: confirm trial: only s1, s4 flicker" in text
    # The fake board carries an active target's tone, so the decoder commits in every trial.
    for trial in range(1, 5):
        assert f"PASS  trial {trial}: selection is an active slot" in text, text
    assert "PASS  score_kind in hello when selections carry scores" in text
    assert "PASS  nothing answers the paused trial" in text


# ---------------------------------------------------------------------------
# Scripted servers against a Bridge with a stub engine and a recording display
# ---------------------------------------------------------------------------


class StubEngine:
    """Stands in for BoardEngine: a baseline finishes at once, a trial commits ``commit_to``
    (an index into its active targets) at once, or never with None."""

    decoder_params = {"window_s": 1.0, "step_s": 0.5, "votes": 2}
    score_kind = "stub_minus_baseline"

    def __init__(self, commit_to: int | None = 0) -> None:
        self.commit_to = commit_to
        self.calls: list[tuple] = []
        self.ok = True

    def attach(self, loop, on_commit, on_baseline) -> None:
        self.loop, self.on_commit, self.on_baseline = loop, on_commit, on_baseline

    def start(self) -> None:
        pass

    def start_baseline(self, baseline_id: int, seconds: float) -> None:
        self.calls.append(("baseline", baseline_id))
        self.loop.call_soon_threadsafe(self.on_baseline, baseline_id, True, "")

    def start_trial(self, trial_id: int, targets: list[int], t_start: float) -> None:
        self.calls.append(("trial", trial_id, list(targets)))
        if self.commit_to is not None:
            k = targets[self.commit_to]
            scores = np.zeros(4)
            scores[k] = 0.5
            self.loop.call_soon_threadsafe(self.on_commit, trial_id, k, scores)

    def stop(self) -> None:
        self.calls.append(("stop",))

    def board_ok(self) -> bool:
        return self.ok

    @property
    def message(self) -> str:
        return "stub board"

    def close(self) -> None:
        pass


def make_bridge(url: str, four: Path, engine: StubEngine | None = None,
                display: vb.HeadlessDisplay | None = None, *extra: str) -> vb.Bridge:
    args = vb.build_parser().parse_args(["--url", url, "-c", str(four), "--fake", "--no-stimulus",
                                         "--reconnect-s", "0.1", "--status-interval", "0.2", *extra])
    cfg = load_stimulus(four)
    return vb.Bridge(args, cfg, engine or StubEngine(), display or vb.HeadlessDisplay(cfg.refresh))


class Peer:
    """The server side of one bridge connection, for scripted tests."""

    def __init__(self, ws) -> None:
        self.ws = ws

    async def send(self, **msg) -> None:
        await self.ws.send(json.dumps(msg))

    async def recv(self, *types: str, timeout: float = 3.0) -> dict:
        """The next message of one of ``types`` (status messages are skipped unless asked for)."""
        deadline = time.monotonic() + timeout
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), max(deadline - time.monotonic(), 0.01))
            parse_decoder_to_server(raw)  # every message the bridge sends is valid v2
            msg = json.loads(raw)
            if msg["type"] in types:
                return msg
            assert msg["type"] == "status", f"unexpected {msg}"

    async def quiet(self, seconds: float, allow: tuple[str, ...] = ("status",)) -> list[dict]:
        """Everything sent within ``seconds``; fails on any type not in ``allow``."""
        got = []
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), left)
            except TimeoutError:
                break
            msg = json.loads(raw)
            assert msg["type"] in allow, f"unexpected {msg}"
            got.append(msg)
        return got

    async def handshake(self, baseline: bool = True, hello: bool = True) -> dict | None:
        """hello (unless already read) -> hello_ack, then baseline 1 unless ``baseline`` False."""
        got = await self.recv("hello") if hello else None
        await self.send(type="hello_ack", protocol=2,
                        roles={"s1": "option", "s2": "option", "s3": "option", "s4": "next"},
                        labels={"s1": "One", "s2": "Two", "s3": "Three", "s4": "More options"})
        if baseline:
            await self.send(type="baseline", baseline_id=1, seconds=0.1)
            done = await self.recv("baseline_done")
            assert done == {"type": "baseline_done", "baseline_id": 1, "ok": True, "message": ""}
        return got


async def with_server(script, bridge_factory):
    """Run ``script(peer)`` for each connection (a list: one per connection) against a bridge;
    returns the bridge and its exit code."""
    scripts = list(script) if isinstance(script, list) else [script]
    errors: list[BaseException] = []
    finished = asyncio.Event()

    async def handler(ws) -> None:
        if not scripts:
            await ws.close(4000, "test over")
            return
        step = scripts.pop(0)
        try:
            await step(Peer(ws))
        except BaseException as exc:  # reported by the test, not swallowed by websockets
            errors.append(exc)
            finished.set()
            raise
        if not scripts:
            finished.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        bridge = bridge_factory(f"ws://127.0.0.1:{port}/ws/decoder")
        task = asyncio.create_task(bridge.run())
        try:
            await asyncio.wait_for(finished.wait(), 15)
        finally:
            if errors:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise errors[0]
        # Every script ends with close code 4000, which ends the bridge (exit 0).
        code = await asyncio.wait_for(task, 5)
        return bridge, code


async def test_hello_carries_config_refresh_and_scores(four):
    async def script(peer: Peer) -> None:
        hello = await peer.recv("hello")
        assert hello["protocol"] == 2 and hello["name"] == "fake-brainsync-fbcca"
        assert hello["refresh_hz"] == 60.0 and hello["waveform"] == "sine"
        assert [(s["slot"], s["freq_hz"]) for s in hello["slots"]] == [
            ("s1", 13.0), ("s2", 15.0), ("s3", 17.0), ("s4", 11.0)]
        assert hello["decoder"] == StubEngine.decoder_params
        assert hello["score_kind"] == "stub_minus_baseline"
        status = await peer.recv("status")
        assert status == {"type": "status", "ok": True, "board_ok": True, "display_ok": True,
                          "message": status["message"]}
        await peer.ws.close(4000, "replaced")

    bridge, code = await with_server(script, lambda url: make_bridge(url, four))
    assert code == 0  # close code 4000: stop, exit 0
    assert bridge.replaced


async def test_stim_start_it_cannot_carry_out_is_not_ready(four):
    engine = StubEngine(commit_to=None)

    async def script(peer: Peer) -> None:
        await peer.recv("hello")
        await peer.send(type="stim_start", trial_id=1, active_slots=SLOTS)  # before hello_ack
        assert await peer.recv("no_selection") == {"type": "no_selection", "trial_id": 1,
                                                   "reason": "not_ready"}
        await peer.handshake(baseline=False, hello=False)
        await peer.send(type="stim_start", trial_id=2, active_slots=SLOTS)  # acked, no baseline yet
        assert (await peer.recv("no_selection"))["reason"] == "not_ready"
        await peer.send(type="baseline", baseline_id=1, seconds=0.5)
        await peer.send(type="stim_start", trial_id=3, active_slots=SLOTS)  # during the baseline
        assert (await peer.recv("no_selection"))["trial_id"] == 3
        assert (await peer.recv("baseline_done", timeout=3))["ok"] is True
        await peer.send(type="stim_start", trial_id=4, active_slots=SLOTS)  # ready: it flickers
        assert (await peer.recv("stim_onset"))["trial_id"] == 4
        await peer.send(type="stim_stop", trial_id=4, reason="paused")
        await peer.send(type="pause", reason="stop_button")
        await peer.send(type="stim_start", trial_id=5, active_slots=SLOTS)  # after a pause
        assert await peer.recv("no_selection") == {"type": "no_selection", "trial_id": 5,
                                                   "reason": "not_ready"}
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, engine))
    assert [c for c in engine.calls if c[0] == "trial"] == [("trial", 4, [0, 1, 2, 3])]


async def test_baseline_done_never_early_and_interrupted_by_pause(four):
    class SlowEngine(StubEngine):
        def start_baseline(self, baseline_id, seconds):  # the data never completes
            self.calls.append(("baseline", baseline_id))

    async def script(peer: Peer) -> None:
        await peer.handshake(baseline=False)
        started = time.monotonic()
        await peer.send(type="baseline", baseline_id=7, seconds=0.4)
        await asyncio.sleep(0.1)
        await peer.send(type="pause", reason="stop_button")
        done = await peer.recv("baseline_done")
        assert done == {"type": "baseline_done", "baseline_id": 7, "ok": False, "message": "interrupted"}
        assert time.monotonic() - started >= 0.38
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, SlowEngine()))


async def test_trial_flickers_only_its_active_slots_and_selection_follows_onset(four):
    display = vb.HeadlessDisplay(60.0, auto_onset=False)  # the onset comes when the test says

    async def script(peer: Peer) -> None:
        await peer.handshake()
        await peer.send(type="display", text="", highlight=None,
                        labels={"s1": "Yes.", "s2": "", "s3": "", "s4": "Back"})
        await peer.send(type="stim_start", trial_id=9, active_slots=["s1", "s4"])
        # The stub decoder committed at once, but nothing goes out before the onset.
        assert await peer.quiet(0.4) is not None
        assert display.lines[-2:] == ['{"active": [0, 3]}', ON]
        before = time.time()
        display.fire("flicker_on")
        onset = await peer.recv("stim_onset")
        sel = await peer.recv("selection")
        assert onset["trial_id"] == 9 and onset["t_unix"] >= before
        assert sel["trial_id"] == 9 and sel["slot"] == "s1" and sel["t_unix"] >= onset["t_unix"]
        assert sel["score"] == 0.5 and set(sel["scores"]) == set(SLOTS)
        # At most one answer per trial, and the flicker goes off on stim_stop.
        await peer.send(type="stim_stop", trial_id=9, reason="selected")
        await peer.quiet(0.2)
        assert display.lines[-1] == OFF
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, StubEngine(), display))
    # A JSON line first (an integer line would end --hold-still), then the screens.
    assert display.lines[:4] == [OFF, '{"labels": ["One", "Two", "Three", "More options"]}',
                                 '{"labels": ["Yes.", "", "", "Back"]}', '{"text": ""}']


async def test_unknown_slot_and_display_during_a_trial(four):
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await peer.handshake()
        await peer.send(type="stim_start", trial_id=1, active_slots=["s1", "s7"])
        assert await peer.recv("no_selection") == {"type": "no_selection", "trial_id": 1,
                                                   "reason": "unknown_slot"}
        await peer.send(type="stim_start", trial_id=2, active_slots=SLOTS)
        await peer.recv("stim_onset")
        n = len(display.lines)
        await peer.send(type="display", text="changed mid-trial")  # rule 5: never drawn
        await peer.quiet(0.2)
        assert '{"text": "changed mid-trial"}' not in display.lines[n:]
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, StubEngine(commit_to=None), display))


async def test_flicker_off_when_the_server_goes_and_reconnects(four):
    display = vb.HeadlessDisplay(60.0)
    engine = StubEngine(commit_to=None)

    async def first(peer: Peer) -> None:
        await peer.handshake()
        await peer.send(type="stim_start", trial_id=1, active_slots=SLOTS)
        await peer.recv("stim_onset")
        assert display.lines[-1] == ON
        await peer.ws.close()  # the server goes away mid-trial

    async def second(peer: Peer) -> None:
        # Reconnected: the flicker went off at once, and stays off until a new ack and stim_start.
        assert display.lines[-1] == OFF
        await peer.recv("hello")
        await peer.send(type="stim_start", trial_id=2, active_slots=SLOTS)  # not acked again yet
        assert (await peer.recv("no_selection"))["reason"] == "not_ready"
        assert display.lines[-1] == OFF
        await peer.ws.close(4000)

    bridge, code = await with_server([first, second], lambda url: make_bridge(url, four, engine, display))
    assert code == 0
    assert ("stop",) in engine.calls


async def test_pause_turns_the_flicker_off_at_once(four):
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await peer.handshake()
        await peer.send(type="stim_start", trial_id=1, active_slots=SLOTS)
        await peer.recv("stim_onset")
        await peer.send(type="pause", reason="stop_button")  # no stim_stop first
        await peer.quiet(0.2)
        assert display.lines[-1] == OFF
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, StubEngine(commit_to=None), display))


async def test_error_keeps_the_bridge_quiet(four):
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await peer.recv("hello")
        await peer.send(type="error", message="display refresh is 120 Hz, expected 60 Hz")
        await peer.send(type="stim_start", trial_id=1, active_slots=SLOTS)
        await peer.quiet(0.15)  # a status already on its way before the error is fine
        assert await peer.quiet(0.6, allow=()) == []  # then not even status
        assert ON not in display.lines
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, StubEngine(), display))


async def test_display_exit_is_reported_and_relaunched(four):
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await peer.handshake()
        await peer.send(type="display", labels={"s1": "Kept"})
        await peer.send(type="stim_start", trial_id=1, active_slots=SLOTS)
        await peer.recv("stim_onset")
        display.refresh = 120.0  # the relaunched display measures a different rate
        display.simulate_exit()  # Esc
        seen = []
        while True:  # statuses sent before the exit still say display_ok
            msg = await peer.recv("status", "hello", timeout=5)
            seen.append(msg)
            if msg["type"] == "status" and not msg["display_ok"]:
                assert msg["ok"] is False
                break
        hello = await peer.recv("hello", timeout=5)  # refresh changed: a new handshake
        assert hello["refresh_hz"] == 120.0
        while not (await peer.recv("status", timeout=5))["display_ok"]:
            pass
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, StubEngine(commit_to=None), display))
    assert display.starts == 2
    # The relaunched display gets the last screen again, held still; then the disconnect's OFF.
    restore = [OFF, '{"labels": ["Kept", "Two", "Three", "More options"]}', '{"text": ""}', "-1"]
    after = display.lines[display.lines.index(ON) + 1:]
    assert after[:4] == restore and set(after[4:]) == {OFF}


# ---------------------------------------------------------------------------
# PsychoPyDisplay through launch_stimulus_live, with a stand-in display process
# ---------------------------------------------------------------------------

STAND_IN = r'''
import os, sys, time
log = open(sys.argv[1], "a", encoding="utf-8")
print("measured refresh: 59.94 Hz", file=sys.stderr, flush=True)
for line in sys.stdin:
    line = line.rstrip("\n")
    log.write(line + "\n"); log.flush()
    if line == '{"flicker": true}':
        print(f"[marker {time.time():.6f}] flicker_on", file=sys.stderr, flush=True)
    elif line == '{"flicker": false}':
        print(f"[marker {time.time():.6f}] flicker_off", file=sys.stderr, flush=True)
    elif line == '{"text": "esc"}' and not os.path.exists(sys.argv[1] + ".esc"):
        open(sys.argv[1] + ".esc", "w").close()  # only the first process quits
        sys.exit(0)  # like Esc: the window closes
'''


def test_launch_stimulus_live_streams_lines(tmp_path):
    import sys

    from brainsync.launch import launch_stimulus_live

    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    seen: list[str] = []
    ended: list[int] = []
    stim = launch_stimulus_live([], seen.append, command=[sys.executable, str(script), str(tmp_path / "in.log")],
                                log_dir=tmp_path, on_end=ended.append, timeout=20)
    assert seen == ["measured refresh: 59.94 Hz"]
    stim.proc.stdin.write('{"flicker": true}\n')
    stim.proc.stdin.flush()
    deadline = time.monotonic() + 10
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert vb.parse_marker(seen[1])[1] == "flicker_on"
    stim.proc.stdin.close()
    assert stim.proc.wait(timeout=10) == 0
    deadline = time.monotonic() + 5
    while not ended and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ended == [0]
    assert "flicker_on" in stim.text()  # still mirrored to the log file


def test_launch_stimulus_live_reports_an_early_exit(tmp_path):
    import sys

    from brainsync.launch import launch_stimulus_live

    with pytest.raises(RuntimeError, match="exited with code 2.*refresh is off"):
        launch_stimulus_live([], lambda line: None, log_dir=tmp_path, timeout=20, command=[
            sys.executable, "-c", "import sys; print('error: refresh is off', file=sys.stderr); sys.exit(2)"])


async def test_psychopy_display_markers_exit_and_relaunch(four, tmp_path):
    import sys

    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    received = tmp_path / "in.log"
    display = vb.PsychoPyDisplay([], 60.0, timeout=20,
                                 command=[sys.executable, str(script), str(received)])

    async def script_(peer: Peer) -> None:
        hello = await peer.handshake()
        assert hello["refresh_hz"] == 59.94  # the display's measured refresh line
        sent = time.time()
        await peer.send(type="stim_start", trial_id=1, active_slots=SLOTS)
        onset = await peer.recv("stim_onset", timeout=5)
        assert onset["trial_id"] == 1 and onset["t_unix"] >= sent - 0.01
        await peer.send(type="stim_stop", trial_id=1, reason="timeout")
        await peer.send(type="display", text="esc")  # the stand-in quits, like Esc
        while (await peer.recv("status", timeout=5))["display_ok"]:
            pass
        # Relaunched with the same refresh: display_ok again, and no new hello.
        while not (await peer.recv("status", timeout=20))["display_ok"]:
            pass
        await peer.quiet(0.3)
        await peer.ws.close(4000)

    bridge, code = await with_server(script_, lambda url: make_bridge(url, four, StubEngine(commit_to=None), display))
    assert code == 0
    lines = received.read_text(encoding="utf-8").splitlines()
    assert lines[:4] == [OFF, '{"labels": ["One", "Two", "Three", "More options"]}',
                         '{"active": [0, 1, 2, 3]}', ON]
    assert lines[4:6] == [OFF, '{"text": "esc"}']
    # The relaunched process got the last screen, held still.
    assert lines[6:10] == [OFF, '{"labels": ["One", "Two", "Three", "More options"]}',
                           '{"text": "esc"}', "-1"]
    display.close()


# ---------------------------------------------------------------------------
# Review fixes: answers stay on their connection, hello waits for the display, shutdown leaves
# no window, a local flicker limit, a stdin that never blocks the loop
# ---------------------------------------------------------------------------


class SlowBaselineEngine(StubEngine):
    def start_baseline(self, baseline_id, seconds):  # the data never completes
        self.calls.append(("baseline", baseline_id))


async def test_an_interrupted_baseline_never_answers_on_the_next_connection(four):
    async def first(peer: Peer) -> None:
        await peer.handshake(baseline=False)
        await peer.send(type="baseline", baseline_id=7, seconds=0.6)
        await asyncio.sleep(0.05)
        await peer.send(type="pause", reason="stop_button")  # interrupted, still sleeping
        await asyncio.sleep(0.05)
        await peer.ws.close()  # then the connection drops

    async def second(peer: Peer) -> None:
        await peer.recv("hello")
        await peer.quiet(1.0)  # only status: baseline 7 belongs to the old connection
        await peer.ws.close(4000)

    await with_server([first, second], lambda url: make_bridge(url, four, SlowBaselineEngine()))


class GatedDisplay(vb.HeadlessDisplay):
    """A headless display whose (re)launch waits until ``gate`` is set."""

    def __init__(self) -> None:
        super().__init__(60.0)
        self.gate = threading.Event()
        self.gate.set()

    def start(self) -> float:
        if not self.gate.wait(10):
            raise RuntimeError("gate never opened")
        return super().start()


async def test_no_hello_while_the_display_is_down_and_no_early_baseline_done(four):
    display = GatedDisplay()

    async def first(peer: Peer) -> None:
        await peer.handshake()
        display.gate.clear()
        display.simulate_exit()  # Esc; the relaunch waits at the gate
        while (await peer.recv("status", timeout=5))["display_ok"]:
            pass
        asked = time.monotonic()
        await peer.send(type="baseline", baseline_id=2, seconds=0.4)
        done = await peer.recv("baseline_done")
        assert done == {"type": "baseline_done", "baseline_id": 2, "ok": False,
                        "message": "display_down"}
        assert time.monotonic() - asked >= 0.38  # rule 12: never before the seconds asked for
        await peer.ws.close()

    async def second(peer: Peer) -> None:
        n = len(display.lines)
        statuses = await peer.quiet(0.5)  # rule 9: status only, no hello while it is down
        assert statuses and not any(s["display_ok"] for s in statuses)
        display.gate.set()
        hello = await peer.recv("hello", timeout=5)
        assert hello["refresh_hz"] == 60.0
        assert display.lines[n] == OFF  # the relaunch's first line
        await peer.ws.close(4000)

    await with_server([first, second], lambda url: make_bridge(url, four, StubEngine(), display))
    assert display.starts == 2


async def test_display_writes_wait_while_it_is_down(four):
    display = GatedDisplay()

    async def script(peer: Peer) -> None:
        await peer.handshake()
        display.gate.clear()
        display.simulate_exit()
        while (await peer.recv("status", timeout=5))["display_ok"]:
            pass
        n = len(display.lines)
        await peer.send(type="display", highlight="s2", labels={"s1": "Later"})
        await peer.quiet(0.2)
        assert len(display.lines) == n  # nothing reaches a display that is down
        display.gate.set()
        while not (await peer.recv("status", timeout=5))["display_ok"]:
            pass
        # Relaunched: a JSON line first, then the kept screen.
        assert display.lines[n:n + 4] == [OFF, '{"labels": ["Later", "Two", "Three", "More options"]}',
                                          '{"text": ""}', "1"]
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, StubEngine(), display))


async def test_a_relaunch_retries_after_any_error(four):
    class FlakyDisplay(vb.HeadlessDisplay):
        failures = 1

        def start(self) -> float:
            if self.starts >= 1 and self.failures:
                self.failures -= 1
                raise OSError("too many open files")
            return super().start()

    display = FlakyDisplay(60.0)

    async def script(peer: Peer) -> None:
        await peer.handshake()
        display.simulate_exit()
        while (await peer.recv("status", timeout=5))["display_ok"]:
            pass
        while not (await peer.recv("status", timeout=10))["display_ok"]:  # after RELAUNCH_S
            pass
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(url, four, StubEngine(), display))
    assert display.starts == 2 and display.failures == 0


async def test_the_flicker_goes_off_by_itself_after_max_flicker_s(four):
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await peer.handshake()
        await peer.send(type="stim_start", trial_id=1, active_slots=SLOTS)
        await peer.recv("stim_onset")
        assert display.lines[-1] == ON
        started = time.monotonic()
        # The server never sends stim_stop (wedged): the bridge stops the flicker itself.
        assert await peer.recv("no_selection", timeout=3) == {
            "type": "no_selection", "trial_id": 1, "reason": "flicker_limit"}
        assert time.monotonic() - started >= 0.25
        assert display.lines[-1] == OFF
        await peer.ws.close(4000)

    await with_server(script, lambda url: make_bridge(
        url, four, StubEngine(commit_to=None), display, "--max-flicker-s", "0.3"))


async def test_no_reconnect_that_never_connects_exits_1(four):
    import socket

    with socket.socket() as sock:  # a port nobody listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    bridge = make_bridge(f"ws://127.0.0.1:{port}/ws/decoder", four, StubEngine(), None,
                         "--no-reconnect")
    assert await asyncio.wait_for(bridge.run(), 10) == 1
    assert bridge.finished


def test_line_writer_never_blocks_on_a_full_pipe():
    import os

    r, w = os.pipe()
    pipe = os.fdopen(w, "w")
    writer = vb.LineWriter(pipe, max_lines=4)
    big = "x" * 200_000  # larger than any pipe buffer: the writer thread blocks on the first
    started = time.monotonic()
    results = [writer.put(big) for _ in range(8)]
    assert time.monotonic() - started < 1.0  # put only queues
    assert results[:4] == [True] * 4 and results[-1] is False  # the queue is full: not reading
    os.close(r)  # the reader goes: the blocked write fails and the writer stops
    writer.join(5)
    assert writer.broken and writer.put("1") is False


STAND_IN_PIDS = r'''
import os, sys, time
pids = sys.argv[1]
first = not os.path.exists(pids)
with open(pids, "a") as f:
    f.write(f"{os.getpid()}\n")
if not first:
    time.sleep(1.5)  # a relaunch takes a while to come up
print("measured refresh: 60.00 Hz", file=sys.stderr, flush=True)
for line in sys.stdin:
    pass
'''


def _running(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():  # Linux: a zombie has exited
        return stat.read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    return True


async def test_stopping_during_a_relaunch_leaves_no_display_running(four, tmp_path):
    import signal
    import sys

    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN_PIDS, encoding="utf-8")
    pids = tmp_path / "pids"
    display = vb.PsychoPyDisplay([], 60.0, timeout=20, command=[sys.executable, str(script), str(pids)])

    async def script_(peer: Peer) -> None:
        await peer.handshake(baseline=False)
        display.stim.proc.send_signal(signal.SIGINT)  # Ctrl-C reaches the display's group too
        deadline = time.monotonic() + 10
        while len(pids.read_text().split()) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert len(pids.read_text().split()) == 2  # the relaunch is coming up...
        await peer.ws.close(4000)  # ...when the bridge stops

    bridge, code = await with_server(script_, lambda url: make_bridge(
        url, four, StubEngine(commit_to=None), display))
    assert code == 0 and bridge.finished
    await asyncio.to_thread(display.close)  # what serve() does next
    with pytest.raises(RuntimeError, match="closing"):
        display.start()  # close is final
    await asyncio.sleep(2.0)  # the relaunch thread's launch has ended by now
    deadline = time.monotonic() + 5
    while any(_running(int(p)) for p in pids.read_text().split()) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not any(_running(int(p)) for p in pids.read_text().split())
    assert len(pids.read_text().split()) == 2  # and nothing launched a third


async def test_a_display_exit_after_stopping_is_not_relaunched(four):
    display = vb.HeadlessDisplay(60.0)

    async def script(peer: Peer) -> None:
        await peer.handshake(baseline=False)
        await peer.ws.close(4000)

    bridge, _ = await with_server(script, lambda url: make_bridge(url, four, StubEngine(), display))
    display.simulate_exit(-2)  # Ctrl-C killed it after the bridge stopped
    await asyncio.sleep(0.2)
    assert display.starts == 1 and bridge.relaunching is None
