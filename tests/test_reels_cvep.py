"""Tests for BrainScroll in code mode (the c-VEP standard, the default of brainsync.reels):
headless on the standard's fake board in virtual time, with a real Bridge and a websockets
client playing the extension, and with a stand-in board whose cycle times come from the
"browser" (the path a real headset takes)."""
from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import threading

import numpy as np
import pytest

from brainsync import reels
from brainsync.vep.live import standard_fake_board

CODE = ["--fake", "--fast", "--no-browser", "--no-voice"]
ACTION = re.compile(r"^\s*([\d.]+) s  (?:scroll (up|down)|play/(pause)) #(\d+)$")


def actions_from(out: str) -> list[tuple[float, str, int]]:
    return [(float(m[1]), m[2] or m[3], int(m[4])) for m in map(ACTION.match, out.splitlines())
            if m]


def _has_websockets() -> bool:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


needs_ws = pytest.mark.skipif(not _has_websockets(), reason="needs the reels extra (websockets)")


# -- headless -------------------------------------------------------------------------------

def test_code_is_the_default_paradigm():
    args = reels.build_parser().parse_args([])
    reels._resolve(args)
    assert args.paradigm == "code" and args.config == "reels_cvep"
    assert args.votes == 2 and args.margin == 0.0 and args.pad == 0.5
    assert args.train_cycles == 30 and args.cycles == 2 and args.cooldown == 1.5


def test_headless_run_trains_then_scrolls_both_ways_and_pauses_once(capsys):
    assert reels.main([*CODE, "--seconds", "30"]) == 0
    cap = capsys.readouterr()
    assert "training: cycle 30/30" in cap.err
    assert "template ready: 100 % on held-out training cycles (chance 33 %)" in cap.err
    got = actions_from(cap.out)
    dirs = [d for _, d, _ in got]
    # the fake viewer looks at down, up, then pause, 10 s each
    assert dirs[0] == "down" and {"up", "down", "pause"} == set(dirs)
    assert dirs.count("pause") == 1                      # once per look, never on dwell
    assert dirs.index("up") > dirs.index("down") and dirs.index("pause") > dirs.index("up")
    times = [t for t, _, _ in got]
    assert min(b - a for a, b in zip(times, times[1:])) >= 1.5
    assert [i for _, _, i in got] == list(range(1, len(got) + 1))
    up, down = dirs.count("up"), dirs.count("down")
    assert f"{up + down} scrolls (up {up}, down {down}), 1 play/pause; ended: time up" in cap.out
    decisions = [ln for ln in cap.err.splitlines() if "  raw " in ln]
    assert len(decisions) == pytest.approx(30 / 1.05, abs=2)   # one per 1.05 s cycle
    assert "template: 100 % on held-out" in cap.out
    assert "browser cycles" not in cap.out                # no bridge


def test_dwelling_on_down_scrolls_every_four_cycles(capsys):
    assert reels.main([*CODE, "--seconds", "30", "--switch", "1000"]) == 0
    got = actions_from(capsys.readouterr().out)
    assert {d for _, d, _ in got} == {"down"} and len(got) >= 6
    np.testing.assert_allclose(np.diff([t for t, _, _ in got]), 4 * 63 / 60, atol=0.01)


def test_log_and_template_reuse(tmp_path, capsys):
    log, tpl = tmp_path / "run.jsonl", tmp_path / "tpl.npz"
    assert reels.main([*CODE, "--seconds", "12", "--train-cycles", "10", "--log", str(log),
                       "--save-template", str(tpl)]) == 0
    out = capsys.readouterr().out
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "start" and kinds[-1] == "end"
    assert {"settings", "trained", "template", "decision", "scroll", "rearm"} <= set(kinds)
    settings = next(r for r in rows if r["kind"] == "settings")
    assert settings["paradigm"] == "code" and settings["labels"] == ["up", "pause", "down"]
    assert settings["train_cycles"] == 10 and settings["votes"] == 2
    trained = next(r for r in rows if r["kind"] == "trained")
    assert trained["cycles"] == 10 and trained["accuracy"] == 1.0
    decisions = [r for r in rows if r["kind"] == "decision"]
    assert set(decisions[0]) >= {"t", "time", "scores", "raw", "committed", "changed", "lead",
                                 "truth", "stale", "hidden"}
    assert len(decisions[0]["scores"]) == 3
    assert int(re.search(r"(\d+) decisions", out)[1]) == len(decisions)
    assert tpl.is_file()
    # the saved template skips training
    assert reels.main([*CODE, "--seconds", "8", "--template", str(tpl)]) == 0
    cap = capsys.readouterr()
    assert "training:" not in cap.err and f"template loaded from {tpl}" in cap.err
    assert actions_from(cap.out)[0][1] == "down"
    assert f"template: loaded from {tpl}" in cap.out


@pytest.mark.parametrize("argv", [
    ["--no-browser", "--no-voice"],                       # real board: cycles need a browser
    [*CODE, "--train-cycles", "1"],
    [*CODE, "--cycles", "0"],
    [*CODE, "--paradigm", "freq", "--template", "x.npz"],
    [*CODE, "--template", "a.npz", "--save-template", "b.npz"],
    [*CODE, "--paradigm", "time"],
])
def test_bad_code_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        reels.main(argv)
    assert exc.value.code == 2


def test_bad_code_configs_return_2(tmp_path, capsys):
    assert reels.main([*CODE, "-c", "reels"]) == 2               # an f-VEP config
    assert reels.main([*CODE, "--paradigm", "freq", "-c", "reels_cvep"]) == 2
    assert reels.main([*CODE, "-c", "cvep_3"]) == 2              # labels YES / NO / HELP
    wide = tmp_path / "wide.toml"
    wide.write_text(reels.load_config("reels_cvep").path.read_text()
                    .replace("lag_frames = 21", "lag_frames = 10"))
    assert reels.main([*CODE, "-c", str(wide)]) == 2             # --fake plays the standard
    assert reels.main([*CODE, "--template", str(tmp_path / "none.npz")]) == 2
    err = capsys.readouterr().err
    assert "is a freq config, not --paradigm code" in err
    assert "is a code config, not --paradigm freq" in err
    assert "up, down or pause" in err and "plays the c-VEP standard" in err
    assert "none.npz" in err


class HiddenSpellBoard:
    """The standard fake board, with the squares out of view from 8 to 16 s of run time."""

    current = None

    def __init__(self, *a, **kw):
        self.inner = standard_fake_board(*a, **kw)
        self.run_start = None
        type(self).current = self

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    @classmethod
    def in_view(cls) -> bool:
        b = cls.current
        if b is None or b.run_start is None:
            return True
        return not 8.0 <= b.inner.elapsed - b.run_start < 16.0


def test_decisions_out_of_view_never_act(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(reels, "standard_fake_board", HiddenSpellBoard)
    monkeypatch.setattr(reels.Link, "showing", property(lambda self: HiddenSpellBoard.in_view()))
    real = reels.phase_message

    def spy(phase, *a, **kw):
        if phase == "run" and HiddenSpellBoard.current is not None:
            HiddenSpellBoard.current.run_start = HiddenSpellBoard.current.inner.elapsed
        return real(phase, *a, **kw)

    monkeypatch.setattr(reels, "phase_message", spy)
    log = tmp_path / "run.jsonl"
    assert reels.main([*CODE, "--seconds", "30", "--switch", "1000", "--log", str(log)]) == 0
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    pause = next(r["t"] for r in rows if r["kind"] == "pause")
    resume = next(r["t"] for r in rows if r["kind"] == "resume")
    assert resume - pause == pytest.approx(8.0, abs=0.3)
    hidden = [r for r in rows if r["kind"] == "decision" and r["hidden"]]
    assert len(hidden) == pytest.approx(8.0 / 1.05, abs=1.5)
    acts = [r["t"] for r in rows if r["kind"] == "scroll"]
    assert acts and not [t for t in acts if pause <= t <= resume]
    assert any(t > resume + 1 for t in acts)
    assert re.search(r"squares out of view 8\.\d s in 1 spell", capsys.readouterr().out)


class InterruptingBoard(HiddenSpellBoard):
    reads = 100

    def read(self):
        type(self).reads -= 1
        if type(self).reads < 0:
            raise KeyboardInterrupt
        return self.inner.read()


def test_ctrl_c_during_training_exits_130_and_during_the_run_0(monkeypatch, capsys):
    monkeypatch.setattr(reels, "standard_fake_board", InterruptingBoard)
    InterruptingBoard.reads = 100                # training takes about 330 reads (0.1 s)
    assert reels.main([*CODE, "--seconds", "100"]) == 130
    cap = capsys.readouterr()
    assert "interrupted before decoding started" in cap.err and cap.out == ""
    InterruptingBoard.reads = 500
    assert reels.main([*CODE, "--seconds", "100"]) == 0
    assert "ended: interrupted" in capsys.readouterr().out


class StallingBoard(HiddenSpellBoard):
    def read(self):
        X, t = self.inner.read()
        if self.inner.elapsed > 45.0:
            return X[:, :0], t[:0]
        return X, t


def test_board_stall_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(reels, "standard_fake_board", StallingBoard)
    assert reels.main([*CODE, "--seconds", "60"]) == 1
    cap = capsys.readouterr()
    assert "ended: board stalled" in cap.out and "sent nothing for 5 s" in cap.err


def test_prompts_name_every_square():
    p = reels.code_prompts(reels.load_config("reels_cvep"), 30)
    assert p["train"] == ("Training: look at the UP square and keep looking at it "
                          "(30 cycles, about 32 s).")
    assert p["run"] == ("Look at UP for the previous reel, PAUSE to pause or play, or DOWN "
                        "for the next reel. Escape quits.")
    assert "up to go back" in p["run_spoken"]


# -- cycle reports ----------------------------------------------------------------------------

class FakeBridge:
    def __init__(self, events=()):
        self._events = list(events)
        self.n_clients = 1
        self.showing = True
        self.sent: list[dict] = []

    def events(self):
        out, self._events = self._events, []
        return out

    def send(self, msg):
        self.sent.append(msg)


def cycle(n, t, client=1):
    return {"type": "cycle", "n": n, "t": t, "_client": client}


def test_link_follows_one_tab_and_drops_repeats(monkeypatch, capsys):
    clock = [100.0]
    monkeypatch.setattr(reels.time, "monotonic", lambda: clock[0])
    link = reels.Link(FakeBridge([cycle(0, 10.0), cycle(0, 10.3, client=2), cycle(1, 11.05),
                                  cycle(1, 11.05), cycle(2, 10.9)]), reels.JsonLog(None))
    link.poll()
    assert link.take_cycles() == [(10.0, "cycle", 0), (11.05, "cycle", 1)]
    assert link.take_cycles() == []
    assert (link.cycles_seen, link.cycles_other) == (5, 1)
    assert "two tabs report flicker cycles" in capsys.readouterr().err
    # the followed tab goes quiet (closed, hidden): after FOLLOW_S the other one is taken
    clock[0] += reels.FOLLOW_S
    link.bridge._events = [cycle(5, 15.0, client=2)]
    link.poll()
    assert link.take_cycles() == [(15.0, "cycle", 5)] and link.cycle_client == 2
    assert "following another tab" in capsys.readouterr().err


class _StubBoard:
    def __init__(self, reads):
        self.reads = list(reads)

    def read(self):
        n, t_last = self.reads.pop(0)
        return np.zeros((8, n)), np.linspace(t_last - n / 250, t_last, n)


def test_cycle_feed_drops_cycles_on_another_clock(capsys):
    bridge = FakeBridge([cycle(0, 1000.0), cycle(1, 1001.05), cycle(2, 5000.0)])
    link = reels.Link(bridge, reels.JsonLog(None))
    feed = reels.CycleFeed(_StubBoard([(25, 1001.2)]), link, False, lambda s: None,
                           lambda: 0.0)
    X, t, events = feed.step()
    assert X.shape == (8, 25) and events == [(1000.0, "cycle", 0), (1001.05, "cycle", 1)]
    assert feed.skewed == 1 and feed.cycles == 2 and feed.board_t == 1001.2
    assert "+3998.8 s from the board's clock" in capsys.readouterr().err


def test_cycle_feed_warns_once_when_the_browser_sends_no_cycles(capsys):
    now = [0.0]
    link = reels.Link(FakeBridge(), reels.JsonLog(None))
    feed = reels.CycleFeed(_StubBoard([(25, 1.0)] * 30), link, False, lambda s: None,
                           lambda: now[0])
    for _ in range(30):
        now[0] += 1.0
        feed.step()
    assert capsys.readouterr().err.count("no flicker cycles from the browser") == 1
    # a hidden tab sends none, and that is no surprise: no warning
    link.bridge.showing = False
    feed = reels.CycleFeed(_StubBoard([(25, 1.0)] * 30), link, False, lambda s: None,
                           lambda: now[0])
    for _ in range(30):
        now[0] += 1.0
        feed.step()
    assert "no flicker cycles" not in capsys.readouterr().err


# -- with a real bridge and a test client playing the extension -----------------------------

def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_main(argv: list[str]) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def target() -> None:
        try:
            result["code"] = reels.main(argv)
        except BaseException as exc:   # surfaced by the test
            result["error"] = exc

    th = threading.Thread(target=target, daemon=True)
    th.start()
    return th, result


@pytest.fixture(autouse=True)
def patient_acks(monkeypatch):
    monkeypatch.setattr(reels, "ACK_GRACE_S", 10.0)


async def extension(port: int, quit_after: int | None = 4, timeout: float = 60.0) -> list[dict]:
    """Say hello, ack every scroll and toggle, say bye after ``quit_after`` actions; the
    messages received until the stop phase."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    url = f"ws://127.0.0.1:{port}/"
    for _ in range(1500):
        try:
            ws = await connect(url)
            break
        except OSError:
            await asyncio.sleep(0.02)
    else:
        raise AssertionError(f"no server on {url}")
    got: list[dict] = []
    acts = 0
    async with ws:
        await ws.send(json.dumps({"type": "hello", "agent": "extension",
                                  "url": "https://www.instagram.com/reels/", "refresh_hz": 59.97,
                                  "dropped": 0, "accepted": True, "showing": True}))
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                got.append(msg)
                if msg["type"] == "scroll":
                    await ws.send(json.dumps({"type": "scrolled", "id": msg["id"], "ok": True,
                                              "method": "container"}))
                elif msg["type"] == "toggle":
                    await ws.send(json.dumps({"type": "toggled", "id": msg["id"], "ok": True,
                                              "playing": False, "method": "video"}))
                if msg["type"] in ("scroll", "toggle"):
                    acts += 1
                    if acts == quit_after:
                        # the browser's cycles are not used with --fake
                        await ws.send(json.dumps({"type": "cycle", "n": 0, "t": 1.0e9}))
                        await ws.send(json.dumps({"type": "bye"}))
                if msg["type"] == "phase" and msg["phase"] == "stop":
                    break
        except ConnectionClosed:
            pass
    return got


@needs_ws
def test_browser_run_trains_shows_confidence_and_acts(capsys, tmp_path):
    port = free_port()
    th, result = start_main(["--fake", "--fast", "--no-voice", "--seconds", "600",
                             "--switch", "6", "--ws-port", str(port), "--wait-browser", "20"])
    got = asyncio.run(extension(port, quit_after=6))
    th.join(60)
    assert not th.is_alive() and result.get("code") == 0, result
    cap = capsys.readouterr()
    config = got[0]
    assert config["type"] == "config" and config["paradigm"] == "code"
    assert [t["label"] for t in config["targets"]] == ["up", "pause", "down"]
    assert config["code"]["lag_frames"] == 21 and len(config["code"]["bits"]) == 63
    seq = [(m["phase"], m["flicker"]) for m in got if m["type"] == "phase"]
    assert [p for p in seq if p[0] != "wait"] == [("train", True), ("run", True),
                                                   ("stop", False)]
    train = next(m for m in got if m.get("phase") == "train")
    assert "look at the UP square" in train["text"]
    i_train, i_run = got.index(train), got.index(next(m for m in got if m.get("phase") == "run"))
    assert {"type": "cue", "target": 0} in got[:i_train]      # the box on the training square
    progress = [m["cycle"] for m in got if m["type"] == "train"]
    assert progress == sorted(progress) and progress[-1] == 30
    assert all(m["of"] == 30 for m in got if m["type"] == "train")
    trained = [m for m in got if m["type"] == "trained"]
    assert trained == [{"type": "trained", "accuracy": 1.0, "chance": 0.3333, "cycles": 30}]
    assert got.index(trained[0]) < i_run
    decisions = [m for m in got if m["type"] == "decision"]
    assert decisions and got.index(decisions[0]) > i_run
    assert all(set(m["scores"]) == {"up", "pause", "down"} for m in decisions)
    assert {m["raw"] for m in decisions} >= {"up", "down", "pause"}
    acts = [m for m in got if m["type"] in ("scroll", "toggle")]
    assert [m["id"] for m in acts] == list(range(1, len(acts) + 1))
    assert {"type": "toggle", "id": next(m["id"] for m in acts if m["type"] == "toggle")} in acts
    assert "ended: viewer quit" in cap.out
    assert f"browser acks: {len(acts)} ok, 0 failed, 0 unanswered" in cap.out
    assert "browser cycles: 1 reported (not used with --fake)" in cap.out


@needs_ws
def test_manual_code_mode_pauses_with_p(monkeypatch, capsys):
    port = free_port()
    monkeypatch.setattr(sys, "stdin", io.StringIO("p\nd\nq\n"))
    th, result = start_main(["--manual", "--no-voice", "--ws-port", str(port),
                             "--wait-browser", "20"])
    got = asyncio.run(extension(port, quit_after=None))
    th.join(30)
    assert result.get("code") == 0, result
    assert [(m["type"], m["id"]) for m in got if m["type"] in ("scroll", "toggle")] == [
        ("toggle", 1), ("scroll", 2)]
    cap = capsys.readouterr()
    assert "p (pause / play)" in cap.err
    assert "1 scrolls (up 0, down 1), 1 play/pause; ended: quit" in cap.out
    assert "browser acks: 2 ok, 0 failed, 0 unanswered" in cap.out


# -- the headset path: cycle times from the browser ------------------------------------------

class BrowserTimedBoard:
    """A stand-in Cerelog X8: the standard's fake board in virtual time (0.1 s per read),
    whose cycle markers reach the run only as browser "cycle" messages, ``OFFSET`` s late
    (a constant flip-to-rAF offset the template must absorb), from tab 1, plus a second
    tab whose cycles must be ignored. The fake viewer looks at the top square until the
    run phase, then at the bottom one."""

    OFFSET = 0.03
    bridge: StubBridge | None = None

    def __init__(self, serial_port: str, **_: object) -> None:
        self.inner = standard_fake_board(fixated=0, seed=5)
        self.fs = self.inner.fs
        self.started = False

    def open(self):
        return self

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def read(self):
        bridge = type(self).bridge
        if not self.started:
            self.inner.start_flicker()
            self.started = True
        if any(m.get("phase") == "run" for m in bridge.sent):
            self.inner.set_target(2)
        self.inner.advance(0.1)
        X, t = self.inner.read()
        for when, kind, n in self.inner.read_events():
            if kind == "cycle":
                bridge.queue(cycle(n, when + self.OFFSET, client=1))
                bridge.queue(cycle(n, when + 0.4, client=2))     # another tab, another grid
        return X, t


class StubBridge:
    """The Bridge surface brainsync.reels uses, with no socket."""

    instance: StubBridge | None = None

    def __init__(self, host, port, config):
        self.config = config
        self.sent: list[dict] = []
        self._events: list[dict] = [{"type": "hello", "agent": "extension", "url": "x",
                                     "refresh_hz": 60.0, "dropped": 0, "accepted": True,
                                     "showing": True}]
        self.running = False
        self.url = "ws://stub"
        self.n_clients = 1
        self.showing = True
        type(self).instance = self
        BrowserTimedBoard.bridge = self

    def start(self):
        self.running = True
        return self

    def stop(self):
        self.running = False

    def send(self, msg):
        self.sent.append(msg)
        if msg["type"] in ("scroll", "toggle"):
            kind = "scrolled" if msg["type"] == "scroll" else "toggled"
            self._events.append({"type": kind, "id": msg["id"], "ok": True,
                                 "method": "container"})

    def queue(self, ev):
        self._events.append(ev)

    def wait_hello(self, timeout):
        return self._events[0]

    def events(self):
        out, self._events = self._events, []
        return out


def test_cycle_times_from_the_browser_train_and_drive_the_decoder(monkeypatch, capsys):
    monkeypatch.setattr(reels, "Bridge", StubBridge)
    monkeypatch.setattr(reels, "CerelogBoard", BrowserTimedBoard)
    monkeypatch.setattr(reels, "POLL_S", 0.0005)
    assert reels.main(["--no-voice", "--port", "/dev/cu.usbserial-1", "--seconds", "15"]) == 0
    cap = capsys.readouterr()
    assert "template ready: 100 % on held-out training cycles" in cap.err
    assert "two tabs report flicker cycles; following the first" in cap.err
    got = actions_from(cap.out)
    assert got and {d for _, d, _ in got} == {"down"}
    sent = StubBridge.instance.sent
    assert sent[-1]["phase"] == "stop"
    assert any(m["type"] == "decision" and m["committed"] == "down" for m in sent)
    m = re.search(r"browser cycles: (\d+) reported, (\d+) from another tab ignored", cap.out)
    assert m and int(m[2]) >= int(m[1]) // 2 - 2
    assert "not used with --fake" not in cap.out
