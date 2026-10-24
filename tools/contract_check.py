"""Stand-in server for testing the bridge against protocol 2 (INTERFACE-v2-draft.md).

It listens where our server would (``ws://127.0.0.1:8000/ws/decoder`` by default, from
config/default.yaml), waits for the bridge to connect, and plays one short session:

    hello -> hello_ack, baseline 1, N trials, a trial stopped by STOP (pause), baseline 2

In page mode (config/default.yaml) every second trial is a confirm trial: only the "Say it" and
"Back" slots flicker (stim_start.active_slots has 2 of the 4), as after a real pick.

Every message the bridge sends is checked against the v2 schemas, and the session checks the
rules: hello matches our config, stim_onset before the answer, at most one answer per trial and
only for the current trial, a selection only for a slot active in that trial, baseline_done echoes its id and does not come early, score_kind when
scores are sent, a status about once a second. Each check prints PASS or FAIL; the exit status is
0 only if nothing failed.

    python tools/contract_check.py                 # then start the bridge
    python tools/contract_check.py --trials 5 --port 8765

A trial the decoder does not answer within max_trial_s is not a failure (the server would end it
as not caught); it is reported as INFO.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

if __package__ in (None, ""):
    # Run as a plain script: make the repo root importable so `app` resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from app.config import AppConfig, load_config
from app.schemas import (
    PROTOCOL,
    Baseline,
    DecoderHello,
    DecoderStatus,
    Display,
    HelloAck,
    Message,
    NoSelection,
    Pause,
    ProtocolError,
    Selection,
    StimOnset,
    StimStart,
    StimStop,
    hello_problems,
    parse_decoder_to_server,
    to_json,
)

ROLE_LABELS = {"say": "SAY", "next": "NEXT"}  # list mode
NEXT_PAGE_LABEL, SAY_IT, BACK = "More options", "Say it", "Back"  # page mode, as app/orchestrator.py
REPLACED = 4000  # close code for a second bridge while a check is running


@dataclass
class Report:
    """PASS / FAIL / INFO lines, printed as they happen."""

    out: TextIO = sys.stdout
    failures: list[str] = field(default_factory=list)
    passes: int = 0

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        if ok:
            self.passes += 1
        else:
            self.failures.append(what if not detail else f"{what}: {detail}")
        self._line("PASS" if ok else "FAIL", what, detail)
        return ok

    def fail(self, what: str, detail: str = "") -> None:
        self.check(False, what, detail)

    def info(self, what: str, detail: str = "") -> None:
        self._line("INFO", what, detail)

    def _line(self, tag: str, what: str, detail: str) -> None:
        print(f"{tag}  {what}" + (f": {detail}" if detail else ""), file=self.out, flush=True)

    @property
    def ok(self) -> bool:
        return not self.failures


class Inbox:
    """Reads the bridge's messages, checks each one, and queues the ones the session waits for.

    Answers (stim_onset, selection, no_selection) are checked against the trial in progress
    here, so a late, early or duplicate answer fails even when nothing is waiting for it.
    """

    def __init__(self, ws: ServerConnection, report: Report) -> None:
        self.ws = ws
        self.report = report
        self.queue: asyncio.Queue[Message | None] = asyncio.Queue()
        self.statuses: list[tuple[float, DecoderStatus]] = []
        self.active: int | None = None  # trial between stim_start and stim_stop
        self.onsets: set[int] = set()
        self.answered: set[int] = set()
        self.hello: DecoderHello | None = None
        self.closed = False

    async def read(self) -> None:
        try:
            async for raw in self.ws:
                self._on_raw(raw)
        except ConnectionClosed:
            pass
        finally:
            self.closed = True
            self.queue.put_nowait(None)

    def _on_raw(self, raw: str | bytes) -> None:
        try:
            msg = parse_decoder_to_server(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            self.report.fail("message matches the v2 schema",
                             f"{first.get('msg')} at {where or 'top level'}: {str(raw)[:160]}")
            return
        if isinstance(msg, DecoderStatus):
            self.statuses.append((time.monotonic(), msg))
            return
        if isinstance(msg, DecoderHello):
            self.hello = msg
        elif isinstance(msg, (StimOnset, Selection, NoSelection)) and not self._answer_ok(msg):
            return
        self.queue.put_nowait(msg)

    def _answer_ok(self, msg: StimOnset | Selection | NoSelection) -> bool:
        if msg.trial_id != self.active:
            when = "outside a trial" if self.active is None else f"while trial {self.active} runs"
            self.report.fail(f"{msg.type} only for the current trial (rule 1)",
                             f"got trial {msg.trial_id} {when}")
            return False
        seen = self.onsets if isinstance(msg, StimOnset) else self.answered
        if msg.trial_id in seen:
            what = "one stim_onset per trial" if seen is self.onsets else "at most one answer per trial"
            self.report.fail(what, f"trial {msg.trial_id}")
            return False
        seen.add(msg.trial_id)
        return True

    def drop_answers(self, trial_id: int) -> list[Message]:
        """Remove answers still queued for ``trial_id`` (they came while it ran, so they were legal)."""
        kept, dropped = [], []
        while not self.queue.empty():
            msg = self.queue.get_nowait()
            is_answer = isinstance(msg, (StimOnset, Selection, NoSelection))
            (dropped if is_answer and msg.trial_id == trial_id else kept).append(msg)
        for msg in kept:
            self.queue.put_nowait(msg)
        return dropped

    async def expect(self, types: set[str], timeout: float) -> Message | None:
        """The next queued message of one of ``types`` within ``timeout`` s; others are reported."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0 or (self.closed and self.queue.empty()):
                return None
            try:
                msg = await asyncio.wait_for(self.queue.get(), remaining)
            except TimeoutError:
                return None
            if msg is None:
                return None
            if msg.type in types:
                return msg
            self.report.fail(f"expected {' or '.join(sorted(types))}", f"got {msg.type}")


class ContractCheck:
    """One scripted session against a connected bridge."""

    def __init__(self, cfg: AppConfig, args: argparse.Namespace, report: Report) -> None:
        self.cfg = cfg
        self.args = args
        self.report = report
        self.slots = [s.slot for s in cfg.stim.slots]
        self.roles = dict(cfg.stim.roles)
        self.page_mode = cfg.menus.mode == "page"
        if self.page_mode:
            options = iter(range(1, len(self.slots) + 1))
            self.labels = {slot: NEXT_PAGE_LABEL if role == "next" else f"Option {next(options)}"
                           for slot, role in self.roles.items()}
        else:
            self.labels = {slot: ROLE_LABELS[role] for slot, role in self.roles.items()}
        self.scores_seen = False

    async def run(self, ws: ServerConnection) -> None:
        inbox = Inbox(ws, self.report)
        reader = asyncio.create_task(inbox.read())
        try:
            if await self._handshake(ws, inbox):
                await self._session(ws, inbox)
                self._final_checks(inbox)
        finally:
            reader.cancel()
            with contextlib.suppress(Exception):
                await ws.close()

    async def _send(self, ws: ServerConnection, msg: Message) -> None:
        with contextlib.suppress(ConnectionClosed):
            await ws.send(to_json(msg))

    async def _handshake(self, ws: ServerConnection, inbox: Inbox) -> bool:
        hello = await inbox.expect({"hello"}, self.args.hello_timeout)
        if hello is None:
            self.report.fail("bridge sends hello after connecting",
                             f"none within {self.args.hello_timeout:g} s")
            return False
        self.report.check(True, "hello is valid v2",
                          f"{hello.name}, {hello.refresh_hz:g} Hz {hello.waveform}, "
                          + ", ".join(f"{s.slot}={s.freq_hz:g} Hz" for s in hello.slots))
        if hello.decoder is not None:
            d = hello.decoder
            self.report.info("decoder", f"window {d.window_s:g} s, step {d.step_s:g} s, {d.votes} votes")
        problems = hello_problems(hello, self.cfg.stim.slots, self.cfg.stim.waveform,
                                  self.cfg.display.expected_refresh_hz)
        if not self.report.check(not problems, "hello matches config/default.yaml", "; ".join(problems)):
            await self._send(ws, ProtocolError(message="; ".join(problems)))
            return False
        await self._send(ws, HelloAck(protocol=PROTOCOL, roles=self.roles, labels=self.labels))
        return True

    async def _session(self, ws: ServerConnection, inbox: Inbox) -> None:
        if not await self._baseline(ws, inbox, 1):
            return
        for trial_id in range(1, self.args.trials + 1):
            if inbox.closed:
                break
            await self._trial(ws, inbox, trial_id)
        await self._paused_trial(ws, inbox, self.args.trials + 1)
        await self._baseline(ws, inbox, 2)
        if inbox.closed:
            self.report.fail("bridge stays connected for the whole check")

    async def _baseline(self, ws: ServerConnection, inbox: Inbox, baseline_id: int) -> bool:
        seconds = self.args.baseline_s
        started = time.monotonic()
        await self._send(ws, Baseline(baseline_id=baseline_id, seconds=seconds))
        while True:
            done = await inbox.expect({"baseline_done"}, self.args.baseline_timeout)
            if done is None:
                self.report.fail(f"baseline_done for baseline {baseline_id}",
                                 f"none within {self.args.baseline_timeout:g} s")
                return False
            if done.baseline_id == baseline_id:
                break
            self.report.fail("baseline_done echoes the baseline_id",
                             f"sent {baseline_id}, got {done.baseline_id}")
        elapsed = time.monotonic() - started
        self.report.check(done.ok, f"baseline {baseline_id} recorded", done.message or f"{elapsed:.1f} s")
        # A small allowance for clock and message jitter.
        self.report.check(elapsed >= seconds * 0.9, "baseline_done not before the requested seconds",
                          f"{elapsed:.2f} s for {seconds:g} s")
        return done.ok

    def _screen(self, trial_id: int, confirm: bool) -> tuple[list[str], Display]:
        """Active slots and display for a trial: every slot, or the confirm screen's two."""
        if not confirm:
            return self.slots, Display(text=f"Contract check, trial {trial_id}", labels=self.labels,
                                       highlight=None)
        say, back = self.cfg.stim.confirm.say, self.cfg.stim.confirm.back
        labels = {slot: "" for slot in self.slots} | {say: SAY_IT, back: BACK}
        return [say, back], Display(text=f"Contract check, trial {trial_id}: say this?",
                                    labels=labels, highlight=None)

    async def _start(self, ws: ServerConnection, inbox: Inbox, trial_id: int,
                     active: list[str], display: Display) -> float:
        await self._send(ws, display)
        await asyncio.sleep(self.cfg.stim.cue_s)
        inbox.active = trial_id
        await self._send(ws, StimStart(trial_id=trial_id, active_slots=active))
        return time.time()

    async def _stop(self, ws: ServerConnection, inbox: Inbox, trial_id: int, reason: str) -> list[Message]:
        """End the trial; returns answers that arrived during it but were never waited for."""
        inbox.active = None
        await self._send(ws, StimStop(trial_id=trial_id, reason=reason))
        return inbox.drop_answers(trial_id)

    async def _trial(self, ws: ServerConnection, inbox: Inbox, trial_id: int) -> None:
        confirm = self.page_mode and trial_id % 2 == 0
        active, display = self._screen(trial_id, confirm)
        if confirm:
            self.report.info(f"trial {trial_id}: confirm trial", f"only {', '.join(active)} flicker")
        started = await self._start(ws, inbox, trial_id, active, display)
        limit = self.cfg.stim.max_trial_s
        first = await inbox.expect({"stim_onset", "selection", "no_selection"}, limit)
        if first is None:
            self.report.fail(f"trial {trial_id}: stim_onset after stim_start",
                             f"no onset within {limit:g} s")
            await self._stop(ws, inbox, trial_id, "timeout")
            return
        if not isinstance(first, StimOnset):
            self.report.fail(f"trial {trial_id}: stim_onset first", f"{first.type} came before any stim_onset")
            await self._stop(ws, inbox, trial_id, "selected" if isinstance(first, Selection) else "no_selection")
            return
        delay = first.t_unix - started
        self.report.check(delay < self.args.onset_timeout,
                          f"trial {trial_id}: stim_onset first, within {self.args.onset_timeout:g} s",
                          f"{delay * 1000:.0f} ms after stim_start")
        remaining = limit - (time.time() - started)
        answer = await inbox.expect({"selection", "no_selection"}, max(remaining, 0))
        if answer is None:
            self.report.info(f"trial {trial_id}: no answer within {limit:g} s (the server times it out)")
            reason = "timeout"
        elif isinstance(answer, Selection):
            self._check_selection(answer, first, trial_id, active)
            reason = "selected"
        else:
            self.report.check(True, f"trial {trial_id}: no_selection", answer.reason)
            reason = "no_selection"
        await self._stop(ws, inbox, trial_id, reason)
        await asyncio.sleep(self.cfg.stim.rest_s)

    def _check_selection(self, sel: Selection, onset: StimOnset, trial_id: int, active: list[str]) -> None:
        self.report.check(sel.slot in active, f"trial {trial_id}: selection is an active slot",
                          f"{sel.slot} of {', '.join(active)}, {sel.t_unix - onset.t_unix:.2f} s after the onset")
        self.report.check(sel.t_unix >= onset.t_unix, f"trial {trial_id}: selection time after the onset")
        if sel.scores is not None:
            self.report.check(set(active) <= set(sel.scores) <= set(self.slots),
                              f"trial {trial_id}: scores cover the active slots", str(sorted(sel.scores)))
        if sel.score is not None or sel.scores is not None:
            self.scores_seen = True

    async def _paused_trial(self, ws: ServerConnection, inbox: Inbox, trial_id: int) -> None:
        """STOP during a trial: stim_stop (paused) then pause; nothing may answer it afterwards."""
        await self._start(ws, inbox, trial_id, *self._screen(trial_id, confirm=False))
        onset = await inbox.expect({"stim_onset"}, self.args.onset_timeout)
        self.report.check(onset is not None, f"trial {trial_id}: stim_onset before the pause")
        early = await self._stop(ws, inbox, trial_id, "paused")
        if any(isinstance(m, (Selection, NoSelection)) for m in early):
            self.report.info(f"trial {trial_id}: answered before the pause (allowed)")
        await self._send(ws, Pause(reason="contract_check"))
        failures = len(self.report.failures)
        await asyncio.sleep(self.args.quiet_s)
        self.report.check(len(self.report.failures) == failures,
                          f"nothing answers the paused trial within {self.args.quiet_s:g} s")

    def _final_checks(self, inbox: Inbox) -> None:
        if self.scores_seen:
            self.report.check(inbox.hello is not None and inbox.hello.score_kind is not None,
                              "score_kind in hello when selections carry scores")
        statuses = inbox.statuses
        if not self.report.check(bool(statuses), "bridge sends status"):
            return
        times = [t for t, _ in statuses]
        gap = max((b - a for a, b in zip(times, times[1:])), default=0.0)
        self.report.check(gap <= self.args.status_gap, "status about once a second",
                          f"{len(statuses)} messages, longest gap {gap:.1f} s")
        last = statuses[-1][1]
        self.report.check(last.display_ok, "display_ok during the check", last.message)
        if not last.board_ok:
            self.report.info("board_ok is false", last.message)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser(cfg: AppConfig) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="contract_check", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=cfg.server.host)
    p.add_argument("--port", type=int, default=cfg.server.port)
    p.add_argument("--trials", type=int, default=3, help="normal trials before the paused one")
    p.add_argument("--baseline-s", type=float, default=cfg.baseline.seconds,
                   help=f"seconds asked for in baseline (default {cfg.baseline.seconds:g})")
    p.add_argument("--baseline-timeout", type=float, default=cfg.baseline.timeout_s,
                   help=f"wait for baseline_done (default {cfg.baseline.timeout_s:g})")
    p.add_argument("--hello-timeout", type=float, default=60.0,
                   help="wait for hello after connecting (default 60; the board connect takes ~16 s)")
    p.add_argument("--onset-timeout", type=float, default=1.0,
                   help="stim_onset must come within this many seconds of stim_start (default 1)")
    p.add_argument("--status-gap", type=float, default=3.0,
                   help="longest allowed gap between status messages (default 3)")
    p.add_argument("--quiet-s", type=float, default=1.5,
                   help="how long to watch for answers after a pause (default 1.5)")
    p.add_argument("--wait", type=float, default=None,
                   help="give up if no bridge connects within this many seconds (default: wait)")
    return p


async def serve_once(cfg: AppConfig, args: argparse.Namespace, report: Report,
                     on_listening=None) -> None:
    """Serve /ws/decoder until one bridge has been checked (or ``args.wait`` runs out).

    ``on_listening(port)`` is called once the socket is bound (tests pass port 0).
    """
    done = asyncio.Event()
    busy = False

    async def handler(ws: ServerConnection) -> None:
        nonlocal busy
        if busy:
            await ws.close(REPLACED, "a contract check is already running")
            return
        busy = True
        path = ws.request.path if ws.request else ""
        report.check(path == "/ws/decoder", "bridge connects to /ws/decoder", path or "?")
        try:
            await ContractCheck(cfg, args, report).run(ws)
        finally:
            done.set()

    async with serve(handler, args.host, args.port) as server:
        port = server.sockets[0].getsockname()[1]
        report.info("listening", f"ws://{args.host}:{port}/ws/decoder (start the bridge now)")
        if on_listening is not None:
            on_listening(port)
        try:
            await asyncio.wait_for(done.wait(), args.wait)
        except TimeoutError:
            report.fail("a bridge connects", f"none within {args.wait:g} s")


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    args = build_parser(cfg).parse_args(argv)
    report = Report()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_once(cfg, args, report))
    total = report.passes + len(report.failures)
    print(f"\n{report.passes}/{total} checks passed", flush=True)
    for failure in report.failures:
        print(f"  FAIL {failure}", flush=True)
    return 0 if report.ok and total else 1


if __name__ == "__main__":
    sys.exit(main())
