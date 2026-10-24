"""Tests for the Reels CLI, headless: FakeBoard in virtual time, and a real Bridge talking
to a websockets test client that plays the extension."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from brainsync import reels
from brainsync.board import FakeBoard
from brainsync.bridge import Bridge

HEADLESS = ["--fake", "--fast", "--no-browser", "--no-voice"]
ROOT = Path(__file__).resolve().parents[1]


def _has_websockets() -> bool:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


# The browser half needs the reels extra; the headless tests run without it.
needs_ws = pytest.mark.skipif(not _has_websockets(), reason="needs the reels extra (websockets)")
SCROLL = re.compile(r"^\s*([\d.]+) s  scroll (up|down) #(\d+)$")


def scrolls_from(out: str) -> list[tuple[float, str, int]]:
    return [(float(m[1]), m[2], int(m[3])) for m in map(SCROLL.match, out.splitlines()) if m]


# -- headless -------------------------------------------------------------------------------

def test_headless_fake_run_scrolls_both_ways_with_cooldown(capsys):
    assert reels.main([*HEADLESS, "--seconds", "30"]) == 0
    cap = capsys.readouterr()
    assert "ended: time up" in cap.out
    got = scrolls_from(cap.out)
    dirs = [d for _, d, _ in got]
    assert {"up", "down"} <= set(dirs)
    # FakeBoard starts the run on target 1 ("down") and switches every 10 s
    assert dirs[0] == "down" and "up" in dirs[1:]
    times = [t for t, _, _ in got]
    assert min(b - a for a, b in zip(times, times[1:])) >= 1.5
    assert [i for _, _, i in got] == list(range(1, len(got) + 1))
    up, down = dirs.count("up"), dirs.count("down")
    assert f"{len(got)} scrolls (up {up}, down {down})" in cap.out
    status = [ln for ln in cap.err.splitlines() if " committed " in ln]
    assert len(status) == pytest.approx((30 - 3.5) / 0.25 + 1, abs=2)
    assert "squares flickering" in cap.err            # --calibrate-on watch by default
    assert "browser acks" not in cap.out               # no bridge


def test_dwelling_scrolls_every_two_and_a_half_to_three_seconds(capsys):
    assert reels.main([*HEADLESS, "--seconds", "30", "--switch", "1000"]) == 0
    times = [t for t, _, _ in scrolls_from(capsys.readouterr().out)]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= 8 and all(2.4 <= g <= 3.2 for g in gaps)


def test_longer_cooldown_means_fewer_scrolls(capsys):
    assert reels.main([*HEADLESS, "--seconds", "30"]) == 0
    n_default = len(scrolls_from(capsys.readouterr().out))
    assert reels.main([*HEADLESS, "--seconds", "30", "--cooldown", "5"]) == 0
    got = scrolls_from(capsys.readouterr().out)
    assert 0 < len(got) < n_default
    assert min(b[0] - a[0] for a, b in zip(got, got[1:])) >= 5.0


def test_calibrate_on_still(capsys):
    assert reels.main([*HEADLESS, "--seconds", "5", "--calibrate-on", "still"]) == 0
    err = capsys.readouterr().err
    assert "squares still" in err and "[say] Keep still and look at the middle" in err


def test_log_writes_json_lines(tmp_path, capsys):
    log = tmp_path / "run.jsonl"
    assert reels.main([*HEADLESS, "--seconds", "20", "--log", str(log)]) == 0
    out = capsys.readouterr().out
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "start" and kinds[-1] == "end"
    assert {"settings", "calibration", "decision", "scroll", "rearm"} <= set(kinds)
    decisions = [r for r in rows if r["kind"] == "decision"]
    windows = int(re.search(r"(\d+) windows", out)[1])
    assert len(decisions) == windows
    assert set(decisions[0]) >= {"t", "time", "scores", "raw", "committed", "changed", "gap",
                                 "level", "truth"}
    scrolls = [r for r in rows if r["kind"] == "scroll"]
    assert [(round(r["t"], 2), r["dir"], r["id"]) for r in scrolls] == [
        (t, d, i) for t, d, i in scrolls_from(out)]
    settings = next(r for r in rows if r["kind"] == "settings")
    assert settings["cooldown_s"] == 1.5 and settings["labels"] == ["up", "down"]
    assert rows[-1]["ended"] == "time up" and rows[-1]["scrolls"] == len(scrolls)


def test_fake_calibration_has_no_tone(monkeypatch, capsys):
    seen = []
    real = reels.calibrate

    def spy(decoder, board, seconds, **kw):
        seen.append(board.active_target())
        return real(decoder, board, seconds, **kw)

    monkeypatch.setattr(reels, "calibrate", spy)
    assert reels.main([*HEADLESS, "--seconds", "5", "--calibrate", "8"]) == 0
    assert seen == [-1]
    assert "baseline from 19 windows" in capsys.readouterr().err


class SilentBoard(FakeBoard):
    """FakeBoard that stops delivering after calibration."""

    def read(self):
        X, t = super().read()
        assert self.elapsed < 300.0, "the run should have ended on the stall"
        if self.elapsed > 40.0:
            return X[:, :0], t[:0]
        return X, t


def test_board_stall_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(reels, "FakeBoard", SilentBoard)
    assert reels.main([*HEADLESS, "--seconds", "60"]) == 1
    cap = capsys.readouterr()
    assert "ended: board stalled" in cap.out and "sent nothing for 5 s" in cap.err


class InterruptingBoard(FakeBoard):
    reads = 120

    def read(self):
        type(self).reads -= 1
        if type(self).reads < 0:
            raise KeyboardInterrupt
        return super().read()


def test_ctrl_c_during_calibration_exits_130(monkeypatch, capsys):
    InterruptingBoard.reads = 120
    monkeypatch.setattr(reels, "FakeBoard", InterruptingBoard)
    assert reels.main([*HEADLESS, "--seconds", "100"]) == 130
    cap = capsys.readouterr()
    assert "interrupted before decoding started" in cap.err and cap.out == ""


def test_ctrl_c_during_the_run_exits_0(monkeypatch, capsys):
    InterruptingBoard.reads = 900                 # calibration takes 600 reads (0.05 s)
    monkeypatch.setattr(reels, "FakeBoard", InterruptingBoard)
    assert reels.main([*HEADLESS, "--seconds", "100"]) == 0
    assert "ended: interrupted" in capsys.readouterr().out


class BatchBoard(FakeBoard):
    """FakeBoard whose every read also brings 1.4 s more: about 6 decisions per read, as
    after a USB stall."""

    def read(self):
        self.advance(1.4)
        return super().read()


def test_decisions_scored_before_a_rearm_are_dropped(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(reels, "FakeBoard", BatchBoard)
    log = tmp_path / "run.jsonl"
    assert reels.main([*HEADLESS, "--seconds", "60", "--log", str(log)]) == 0
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert any(r["kind"] == "decision" and r["stale"] for r in rows)
    # after each re-arm a scroll needs `votes` (4) decisions voted by the reset voter
    fresh, n_scrolls = None, 0
    for r in rows:
        if r["kind"] == "rearm":
            fresh = 0
        elif r["kind"] == "decision" and not r["stale"] and fresh is not None:
            fresh += 1
        elif r["kind"] == "scroll":
            n_scrolls += 1
            assert fresh is None or fresh >= 4, (r, fresh)
            fresh = None
    assert n_scrolls >= 5
    assert "ended: time up" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["--fast", "--no-browser"],
    [*HEADLESS, "--seconds", "0"],
    [*HEADLESS, "--cooldown", "-1"],
    [*HEADLESS, "--cooldown", "nan"],
    [*HEADLESS, "--wait-browser", "0"],
    [*HEADLESS, "--ws-port", "70000"],
    [*HEADLESS, "--calibrate-on", "closed"],
    [*HEADLESS, "--votes", "0"],
    [*HEADLESS, "--names", "a,b"],
    ["--manual", "--no-browser"],
    ["--manual", "--fake"],
])
def test_bad_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        reels.main(argv)
    assert exc.value.code == 2


def test_bad_config_channel_or_log_returns_2(capsys, tmp_path: Path):
    assert reels.main([*HEADLESS, "-c", "two_targets"]) == 2     # labels 15/17, not up/down
    assert reels.main([*HEADLESS, "-c", str(tmp_path / "nope.toml")]) == 2
    assert reels.main([*HEADLESS, "--channels", "Oz"]) == 2
    assert reels.main([*HEADLESS, "--log", str(tmp_path / "no" / "dir" / "x.jsonl")]) == 2
    err = capsys.readouterr().err
    assert "up or down" in err and "nope.toml" in err and "Oz" in err and "--log" in err


def test_defaults():
    args = reels.build_parser().parse_args([])
    assert args.config == "reels" and args.ws_port == 8765 and args.host == "127.0.0.1"
    assert args.calibrate == 30.0 and args.calibrate_on == "watch"
    assert args.cooldown == 1.5 and args.seconds == 600.0 and args.wait_browser == 120.0
    assert args.threshold == 0.5 and args.margin == 0.5 and args.votes == 4


def test_sigterm_and_sighup_raise_keyboard_interrupt_until_restored():
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    restore = reels._stop_on_signals()
    try:
        assert all(signal.getsignal(s) is reels._interrupt for s in before)
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGTERM)
    finally:
        restore()
    assert {s: signal.getsignal(s) for s in before} == before
    assert reels.main([*HEADLESS, "--seconds", "2"]) == 0      # main restores them too
    assert {s: signal.getsignal(s) for s in before} == before


class FakeBridge:
    def __init__(self, events):
        self._events = list(events)
        self.n_clients = 1

    def events(self):
        out, self._events = self._events, []
        return out

    def send(self, msg):
        pass


def test_acks_count_once_per_scroll_id(capsys):
    link = reels.Link(FakeBridge([]), reels.JsonLog(None))
    for i in (1, 2, 3):
        link.scroll(reels.Action(dir="down", target=1, id=i, time=float(i)))
    # two tabs: the hidden one refuses at once, the visible one moves the feed
    link.bridge._events = [
        {"type": "scrolled", "id": 1, "ok": False, "method": "none"},
        {"type": "scrolled", "id": 1, "ok": True, "method": "container"},
        {"type": "scrolled", "id": 2, "ok": True, "method": "container"},
        {"type": "scrolled", "id": 2, "ok": False, "method": "none"},
        {"type": "scrolled", "id": 2, "ok": True, "method": "container"},
        {"type": "scrolled", "id": 9, "ok": True, "method": "container"},
        {"type": "scrolled", "id": True, "ok": True, "method": "container"},
    ]
    assert link.poll() is None
    assert (link.acks_ok, link.acks_failed, link.unanswered) == (2, 0, 1)
    err = capsys.readouterr().err
    assert err.count("never sent") == 2 and "could not scroll #1" in err
    link.bridge._events = [{"type": "scrolled", "id": 3, "ok": False, "method": "none"}] * 2
    link.poll()
    assert (link.acks_ok, link.acks_failed, link.unanswered) == (2, 1, 0)


@pytest.mark.parametrize("ignore", [False, True])
def test_a_later_hello_gets_its_refresh_checked(ignore, capsys):
    hello = {"type": "hello", "agent": "extension", "refresh_hz": 59.97}
    link = reels.Link(FakeBridge([hello, {**hello, "refresh_hz": 120.0}]), reels.JsonLog(None),
                      cfg=reels.load_config("reels"), ignore_refresh=ignore)
    assert link.poll() is None and link.hello["refresh_hz"] == 59.97
    err = capsys.readouterr().err
    assert "hello again" in err and "cannot flicker: the browser measured a 120.00 Hz" in err
    assert ("(ignored)" in err) is ignore


class WaitingBridge(FakeBridge):
    url = "ws://fake"

    def start(self):
        return self

    def wait_hello(self, timeout):
        return None


def test_a_bye_queued_with_the_hello_stops_before_calibration(capsys):
    hello = {"type": "hello", "agent": "extension", "refresh_hz": 60.0}
    link = reels.Link(WaitingBridge([hello, {"type": "bye"}]), reels.JsonLog(None))
    args = reels.build_parser().parse_args([])
    with pytest.raises(reels.ViewerQuit):
        reels._await_browser(link, args, reels.load_config("reels"))
    assert link.quit and link.hello == hello


def test_refresh_problem():
    cfg = reels.load_config("reels")
    assert reels.refresh_problem({"refresh_hz": 59.97}, cfg) is None
    assert reels.refresh_problem({"refresh_hz": 60.9}, cfg) is None
    assert "120.00 Hz" in reels.refresh_problem({"refresh_hz": 120.0}, cfg)
    assert "did not report" in reels.refresh_problem({}, cfg)
    assert "did not report" in reels.refresh_problem({"refresh_hz": True}, cfg)


# -- with a real bridge and a test client playing the extension -----------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def patient_acks(monkeypatch):
    # the ack counts below must not depend on machine load; the wait ends at the last ack
    monkeypatch.setattr(reels, "ACK_GRACE_S", 10.0)


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


async def extension(port: int, refresh_hz: float = 59.97, bye_after: int | None = 2,
                    ack: bool = True, timeout: float = 60.0) -> list[dict]:
    """Connect, say hello, ack every scroll and say bye after ``bye_after`` scrolls; the
    messages received until the stop phase (or the server closing)."""
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
    scrolls = 0
    async with ws:
        await ws.send(json.dumps({"type": "hello", "agent": "extension",
                                  "url": "https://www.instagram.com/reels/",
                                  "refresh_hz": refresh_hz, "dropped": 0, "accepted": True}))
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                got.append(msg)
                if msg["type"] == "scroll":
                    scrolls += 1
                    if ack:
                        await ws.send(json.dumps({"type": "scrolled", "id": msg["id"],
                                                  "ok": msg["id"] != 2,
                                                  "method": "container"}))
                    if scrolls == bye_after:
                        await ws.send(json.dumps({"type": "frames", "refresh_hz": 59.98,
                                                  "dropped": 3}))
                        await ws.send("this is not json")
                        await ws.send(json.dumps({"type": "bye"}))
                if msg["type"] == "phase" and msg["phase"] == "stop":
                    break
        except ConnectionClosed:
            pass
    return got


def phases(got: list[dict]) -> list[tuple[str, bool]]:
    return [(m["phase"], m["flicker"]) for m in got if m["type"] == "phase"]


@needs_ws
def test_browser_run_config_phases_scrolls_and_bye(capsys, tmp_path):
    port = free_port()
    log = tmp_path / "run.jsonl"
    th, result = start_main(["--fake", "--fast", "--no-voice", "--seconds", "600",
                             "--ws-port", str(port), "--wait-browser", "20", "--log", str(log)])
    got = asyncio.run(extension(port))
    th.join(60)
    assert not th.is_alive() and result.get("code") == 0, result
    cap = capsys.readouterr()
    assert got[0]["type"] == "config" and got[0]["name"] == "reels"
    assert [t["label"] for t in got[0]["targets"]] == ["up", "down"]
    seq = phases(got)
    assert seq[0] == ("wait", False)
    assert [p for p in seq if p[0] != "wait"] == [("calibrate", True), ("run", True),
                                                   ("stop", False)]
    scrolls = [m for m in got if m["type"] == "scroll"]
    assert len(scrolls) >= 2 and scrolls[0] == {"type": "scroll", "dir": "down", "id": 1}
    assert [m["id"] for m in scrolls] == list(range(1, len(scrolls) + 1))
    cues = [m["target"] for m in got if m["type"] == "cue"]
    assert 1 in cues and -1 in cues                     # commit box, cleared on re-arm
    assert got.index(next(m for m in got if m.get("phase") == "run")) < got.index(scrolls[0])
    assert "ended: viewer quit" in cap.out
    assert f"browser acks: {len(scrolls) - 1} ok, 1 failed, 0 unanswered" in cap.out
    assert "browser frames: refresh 59.98 Hz, 3 dropped" in cap.out
    assert "could not scroll #2" in cap.err and "not JSON" in cap.err
    assert "browser connected: extension" in cap.err and "refresh 59.97 Hz" in cap.err
    kinds = [json.loads(ln)["kind"] for ln in log.read_text().splitlines()]
    assert {"hello", "ack", "frames", "bye"} <= set(kinds)


@needs_ws
def test_refresh_mismatch_stops_the_overlay_and_exits_1(capsys):
    port = free_port()
    th, result = start_main(["--fake", "--fast", "--no-voice", "--ws-port", str(port),
                             "--wait-browser", "20"])
    got = asyncio.run(extension(port, refresh_hz=120.0))
    th.join(30)
    assert result.get("code") == 1, result
    cap = capsys.readouterr()
    assert "error: the browser measured a 120.00 Hz refresh" in cap.err
    assert "--ignore-refresh" in cap.err
    stop = [m for m in got if m.get("phase") == "stop"]
    assert stop and "120.00 Hz" in stop[0]["text"] and stop[0]["flicker"] is False
    assert not any(m.get("phase") == "calibrate" for m in got)
    assert not any(m["type"] == "scroll" for m in got)


@needs_ws
def test_ignore_refresh_proceeds_and_calibrates_still(capsys):
    port = free_port()
    th, result = start_main(["--fake", "--fast", "--no-voice", "--ws-port", str(port),
                             "--wait-browser", "20", "--ignore-refresh",
                             "--calibrate-on", "still"])
    got = asyncio.run(extension(port, refresh_hz=120.0, bye_after=1))
    th.join(60)
    assert result.get("code") == 0, result
    cap = capsys.readouterr()
    assert "120.00 Hz refresh" in cap.err and "(ignored)" in cap.err
    assert ("calibrate", False) in phases(got) and ("run", True) in phases(got)
    assert any(m["type"] == "scroll" for m in got)
    assert "ended: viewer quit" in cap.out


@needs_ws
def test_no_hello_in_time_exits_1(capsys):
    port = free_port()
    assert reels.main(["--fake", "--fast", "--no-voice", "--ws-port", str(port),
                       "--wait-browser", "0.3"]) == 1
    assert "no browser said hello within 0.3 s" in capsys.readouterr().err


@needs_ws
def test_port_taken_exits_1(capsys):
    with Bridge(port=0) as other:
        assert reels.main(["--fake", "--fast", "--no-voice", "--ws-port", str(other.port),
                           "--wait-browser", "1"]) == 1
    assert "cannot listen on" in capsys.readouterr().err


@needs_ws
def test_manual_mode_reads_stdin(monkeypatch, capsys):
    port = free_port()
    monkeypatch.setattr(sys, "stdin", io.StringIO("u\n\nd\nsideways\nUP\nq\nd\n"))
    th, result = start_main(["--manual", "--no-voice", "--ws-port", str(port),
                             "--wait-browser", "20"])
    got = asyncio.run(extension(port, bye_after=None))
    th.join(30)
    assert result.get("code") == 0, result
    cap = capsys.readouterr()
    assert [(m["dir"], m["id"]) for m in got if m["type"] == "scroll"] == [
        ("up", 1), ("down", 2), ("up", 3)]                 # stops at q: the last d is unread
    assert ("run", True) in phases(got) and phases(got)[-1][0] == "stop"
    assert "ignored 'sideways'" in cap.err
    assert "3 scrolls (up 2, down 1); ended: quit" in cap.out
    assert "browser acks: 2 ok, 1 failed, 0 unanswered" in cap.out
    assert "windows" not in cap.out                        # nothing decoded


@needs_ws
def test_manual_mode_ends_at_end_of_input(monkeypatch, capsys):
    port = free_port()
    monkeypatch.setattr(sys, "stdin", io.StringIO("d\n"))
    th, result = start_main(["--manual", "--no-voice", "--ws-port", str(port),
                             "--wait-browser", "20"])
    got = asyncio.run(extension(port, bye_after=None))
    th.join(30)
    assert result.get("code") == 0, result
    assert [m["dir"] for m in got if m["type"] == "scroll"] == ["down"]
    assert "ended: end of input" in capsys.readouterr().out


async def escape_first(port: int) -> list[dict]:
    """Press Escape right after Start, before the refresh was measured: a bye and no hello;
    the messages received until the stop phase."""
    from websockets.asyncio.client import connect

    ws = await connect_retry(connect, f"ws://127.0.0.1:{port}/")
    got: list[dict] = []
    async with ws:
        await ws.send(json.dumps({"type": "bye"}))
        async for text in ws:
            got.append(json.loads(text))
            if got[-1].get("phase") == "stop":
                break
    return got


async def connect_retry(connect, url: str):
    for _ in range(1500):
        try:
            return await connect(url)
        except OSError:
            await asyncio.sleep(0.02)
    raise AssertionError(f"no server on {url}")


@needs_ws
def test_escape_before_the_hello_ends_the_run_at_once(capsys):
    port = free_port()
    th, result = start_main(["--fake", "--fast", "--no-voice", "--ws-port", str(port),
                             "--wait-browser", "20"])
    got = asyncio.run(escape_first(port))
    th.join(30)
    assert result.get("code") == 0, result
    assert [p for p, _ in phases(got)] == ["wait", "stop"]       # never calibrated
    assert "viewer quit" in got[-1]["text"]
    assert "ended: viewer quit" in capsys.readouterr().out


@needs_ws
@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX signals")
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_sigterm_or_sighup_still_tells_the_overlay_to_stop(signame):
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "brainsync.reels", "--manual", "--no-voice",
                             "--ws-port", str(port), "--wait-browser", "30"],
                            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})

    async def client() -> list[dict]:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        ws = await connect_retry(connect, f"ws://127.0.0.1:{port}/")
        got: list[dict] = []
        async with ws:
            await ws.send(json.dumps({"type": "hello", "agent": "extension", "url": "x",
                                      "refresh_hz": 60.0, "dropped": 0, "accepted": True}))
            try:
                while True:
                    got.append(json.loads(await asyncio.wait_for(ws.recv(), 20)))
                    if got[-1].get("phase") == "run":
                        proc.send_signal(getattr(signal, signame))
                    if got[-1].get("phase") == "stop":
                        break
            except ConnectionClosed:
                pass
        return got

    try:
        got = asyncio.run(client())
        out, err = proc.communicate(timeout=30)
    finally:
        proc.kill()
    assert phases(got)[-2:] == [("run", True), ("stop", False)], (got, err)
    assert "interrupted" in got[-1]["text"]
    assert proc.returncode == 0, err
    assert "ended: interrupted" in out
