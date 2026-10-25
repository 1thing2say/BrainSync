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

Code mode (a hello with ``paradigm: "code"``, INTERFACE-v2-draft.md section 9, proposed): the
baselines become trainings (``train`` 1 with retrain, ``train`` 2 without), each checked for
``train_progress`` (its id, cycles counting up to ``of``) and ``trained`` (its id, ok, the
accuracy); every ``decision`` must be for the trial in progress, after its onset, with scores
for the active slots and raw / committed among them, and a selection must follow at least one
decision. The config follows the hello: a code hello is checked against config/default.yaml, a
freq hello against config/fvep_page.yaml (``--config`` picks one for both).

``--page-cycles`` (code mode): play the session page that draws the code (``display.flicker:
browser``, section 9.7) as well. During each training and each trial (from its stim_onset) it
sends the bridge ``cycle`` {train_id or trial_id, n, t_unix} once per cycle (1.05 s), with
``t_unix`` this computer's clock, as our server relays the page's flips. A headless bridge on
the headset needs them to train and decode; one with its own display (or a fake board) must
ignore them, and the check fails if anything it sends breaks the rules above.

    python tools/contract_check.py --config config/fvep_list.yaml   # a 2-target list-mode bridge
    python tools/contract_check.py --page-cycles                    # bridge --no-stimulus
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

from app.config import DEFAULT_CONFIG_PATH, FVEP_PAGE_CONFIG_PATH, AppConfig, load_config
from app.schemas import (
    PROTOCOL,
    Baseline,
    Cycle,
    Decision,
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
    Train,
    Trained,
    TrainProgress,
    hello_problems,
    parse_decoder_to_server,
    to_json,
)

ROLE_LABELS = {"say": "SAY", "next": "NEXT"}  # list mode
NEXT_PAGE_LABEL, SAY_IT, BACK = "More options", "Say it", "Back"  # page mode, as app/orchestrator.py
REPLACED = 4000  # close code for a second bridge while a check is running
LATE_DECISION_S = 0.5  # a decision this soon after its trial's stim_stop crossed it in flight


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
        self.decisions: dict[int, int] = {}  # code mode: decisions per trial
        self.active_slots: list[str] = []  # the current trial's
        self.stopped: tuple[int, float] | None = None  # the last trial stopped, and when

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
        if isinstance(msg, Decision):
            self._decision_ok(msg)
            return  # checked here; nothing waits for decisions
        if isinstance(msg, DecoderHello):
            self.hello = msg
        elif isinstance(msg, (StimOnset, Selection, NoSelection)) and not self._answer_ok(msg):
            return
        self.queue.put_nowait(msg)

    def _decision_ok(self, msg: Decision) -> None:
        """Code mode: a decision belongs to the trial in progress, after its onset."""
        if msg.trial_id != self.active:
            late = (self.stopped is not None and self.stopped[0] == msg.trial_id
                    and time.monotonic() - self.stopped[1] < LATE_DECISION_S)
            if late:
                self.report.info(f"trial {msg.trial_id}: a decision crossed its stim_stop")
            else:
                self.report.fail("decision only for the current trial",
                                 f"got trial {msg.trial_id} while "
                                 + ("no trial runs" if self.active is None else f"trial {self.active} runs"))
            return
        if msg.trial_id not in self.onsets:
            self.report.fail(f"trial {msg.trial_id}: decision after stim_onset", "it came first")
            return
        active = set(self.active_slots)
        first = msg.trial_id not in self.decisions
        self.decisions[msg.trial_id] = self.decisions.get(msg.trial_id, 0) + 1
        problems = []
        if not active <= set(msg.scores):
            problems.append(f"scores {sorted(msg.scores)} miss active {sorted(active - set(msg.scores))}")
        for name in ("raw", "committed"):
            value = getattr(msg, name)
            if value is not None and value not in active:
                problems.append(f"{name} {value} is not active")
        if problems or first:  # one PASS per trial, every FAIL
            self.report.check(not problems, f"trial {msg.trial_id}: decision scores the active slots",
                              "; ".join(problems) or ", ".join(f"{s} {v:+.2f}" for s, v in sorted(msg.scores.items())))

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
        self._setup(cfg)

    def _setup(self, cfg: AppConfig) -> None:
        """The slots, roles and labels of ``cfg``, the config the hello is checked against."""
        self.cfg = cfg
        self.code = cfg.stim.paradigm == "code"
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
        self.inbox: Inbox | None = None
        self.cycler: asyncio.Task | None = None  # --page-cycles: the page's cycles, if running
        self.cycles_sent = 0
        given = getattr(self.args, "config", None)
        self.config_name = (str(given) if given else "config/default.yaml" if self.code
                            else "config/fvep_page.yaml")

    async def run(self, ws: ServerConnection) -> None:
        inbox = Inbox(ws, self.report)
        self.inbox = inbox
        reader = asyncio.create_task(inbox.read())
        try:
            if await self._handshake(ws, inbox):
                await self._session(ws, inbox)
                self._final_checks(inbox)
        finally:
            self._stop_cycles()
            reader.cancel()
            with contextlib.suppress(Exception):
                await ws.close()

    async def _send(self, ws: ServerConnection, msg: Message) -> None:
        with contextlib.suppress(ConnectionClosed):
            await ws.send(to_json(msg))

    def _start_cycles(self, ws: ServerConnection, **owner: int) -> None:
        """--page-cycles in code mode: a cycle message per cycle for ``train_id`` or
        ``trial_id``, as the server relays the page's flips, until ``_stop_cycles``."""
        self._stop_cycles()
        if not (self.code and getattr(self.args, "page_cycles", False)):
            return
        code = self.cfg.stim.code
        cycle_s = (code.cycle_frames if code is not None else 63) / self.cfg.display.expected_refresh_hz

        async def run() -> None:
            n = 0
            while True:
                await self._send(ws, Cycle(n=n, t_unix=time.time(), **owner))
                self.cycles_sent += 1
                n += 1
                await asyncio.sleep(cycle_s)

        self.cycler = asyncio.create_task(run())

    def _stop_cycles(self) -> None:
        if self.cycler is not None:
            self.cycler.cancel()
            self.cycler = None

    async def _handshake(self, ws: ServerConnection, inbox: Inbox) -> bool:
        hello = await inbox.expect({"hello"}, self.args.hello_timeout)
        if hello is None:
            self.report.fail("bridge sends hello after connecting",
                             f"none within {self.args.hello_timeout:g} s")
            return False
        if hello.paradigm == "code":
            what = (f"{hello.name}, {hello.refresh_hz:g} Hz c-VEP "
                    f"({hello.code.length} bits, {hello.code.cycle_frames}-frame cycle), "
                    + ", ".join(f"{s.slot} lag {s.lag_frames}" for s in hello.slots))
        else:
            what = (f"{hello.name}, {hello.refresh_hz:g} Hz {hello.waveform}, "
                    + ", ".join(f"{s.slot}={s.freq_hz:g} Hz" for s in hello.slots))
        self.report.check(True, "hello is valid v2", what)
        if hello.decoder is not None:
            d = hello.decoder
            self.report.info("decoder", f"window {d.window_s:g} s, step {d.step_s:g} s, {d.votes} votes")
        if hello.paradigm != self.cfg.stim.paradigm and getattr(self.args, "config", None) is None:
            path = DEFAULT_CONFIG_PATH if hello.paradigm == "code" else FVEP_PAGE_CONFIG_PATH
            other = load_config(path)
            if other.stim.paradigm == hello.paradigm:
                self.report.info(f"a {hello.paradigm} hello", f"checking against config/{path.name}")
                self._setup(other)
                self.config_name = f"config/{path.name}"
        problems = hello_problems(hello, self.cfg.stim.slots, self.cfg.stim.waveform,
                                  self.cfg.display.expected_refresh_hz,
                                  paradigm=self.cfg.stim.paradigm, code=self.cfg.stim.code)
        if not self.report.check(not problems, f"hello matches {self.config_name}", "; ".join(problems)):
            await self._send(ws, ProtocolError(message="; ".join(problems)))
            return False
        await self._send(ws, HelloAck(protocol=PROTOCOL, roles=self.roles, labels=self.labels))
        return True

    async def _session(self, ws: ServerConnection, inbox: Inbox) -> None:
        start = self._train if self.code else self._baseline
        if not await start(ws, inbox, 1):
            return
        for trial_id in range(1, self.args.trials + 1):
            if inbox.closed:
                break
            await self._trial(ws, inbox, trial_id)
        await self._paused_trial(ws, inbox, self.args.trials + 1)
        await start(ws, inbox, 2)
        if inbox.closed:
            self.report.fail("bridge stays connected for the whole check")

    async def _train(self, ws: ServerConnection, inbox: Inbox, train_id: int) -> bool:
        """Code mode: train ``train_id`` (the first with retrain, so it records), then trained."""
        retrain = train_id == 1
        cycles = self.args.train_cycles
        slot = self.slots[0]
        await self._send(ws, Train(train_id=train_id, slot=slot, label="left", cycles=cycles,
                                   retrain=retrain))
        self._start_cycles(ws, train_id=train_id)
        try:
            return await self._trained(inbox, train_id, retrain, cycles)
        finally:
            self._stop_cycles()

    async def _trained(self, inbox: Inbox, train_id: int, retrain: bool, cycles: int) -> bool:
        last, progress, started = -1, 0, time.monotonic()
        while True:
            msg = await inbox.expect({"train_progress", "trained"}, self.args.train_timeout)
            if msg is None:
                self.report.fail(f"trained for train {train_id}",
                                 f"none within {self.args.train_timeout:g} s")
                return False
            if msg.train_id != train_id:
                self.report.fail(f"{msg.type} echoes the train_id", f"sent {train_id}, got {msg.train_id}")
                continue
            if isinstance(msg, TrainProgress):
                progress += 1
                if msg.cycle < last or msg.cycle > msg.of or msg.of != cycles:
                    self.report.fail(f"train {train_id}: progress counts up to {cycles} cycles",
                                     f"cycle {msg.cycle} of {msg.of} after {last}")
                last = msg.cycle
                continue
            break
        elapsed = time.monotonic() - started
        detail = (f"{elapsed:.1f} s, {progress} train_progress"
                  + ("" if msg.accuracy is None else f", accuracy {100 * msg.accuracy:.0f} %")
                  + ("" if msg.chance is None else f" (chance {100 * msg.chance:.0f} %)"))
        self.report.check(msg.ok, f"train {train_id} trained", msg.message or detail)
        if msg.ok:
            self.report.check(msg.accuracy is not None, f"train {train_id}: trained carries the accuracy")
            if retrain:
                self.report.check(progress > 0, f"train {train_id}: train_progress while recording",
                                  f"{progress} messages")
        return msg.ok

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
        inbox.active_slots = list(active)
        await self._send(ws, StimStart(trial_id=trial_id, active_slots=active))
        return time.time()

    async def _stop(self, ws: ServerConnection, inbox: Inbox, trial_id: int, reason: str) -> list[Message]:
        """End the trial; returns answers that arrived during it but were never waited for."""
        self._stop_cycles()
        inbox.active = None
        inbox.stopped = (trial_id, time.monotonic())
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
        self._start_cycles(ws, trial_id=trial_id)  # the page flickers from the onset on
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
        if self.code:
            self.report.check(self.inbox_decisions(trial_id) > 0,
                              f"trial {trial_id}: a decision before the selection (code mode)")
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
        if onset is not None:
            self._start_cycles(ws, trial_id=trial_id)
            if self.cycler is not None:
                await asyncio.sleep(0.1)  # its first cycle goes out before the pause
        early = await self._stop(ws, inbox, trial_id, "paused")
        if any(isinstance(m, (Selection, NoSelection)) for m in early):
            self.report.info(f"trial {trial_id}: answered before the pause (allowed)")
        await self._send(ws, Pause(reason="contract_check"))
        failures = len(self.report.failures)
        await asyncio.sleep(self.args.quiet_s)
        self.report.check(len(self.report.failures) == failures,
                          f"nothing answers the paused trial within {self.args.quiet_s:g} s")

    def inbox_decisions(self, trial_id: int) -> int:
        return self.inbox.decisions.get(trial_id, 0) if self.inbox is not None else 0

    def _final_checks(self, inbox: Inbox) -> None:
        if self.code and getattr(self.args, "page_cycles", False):
            self.report.info("page cycles sent", f"{self.cycles_sent} cycle messages")
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
    p.add_argument("--config", type=Path, default=None,
                   help="the config to check the hello against (default: config/default.yaml for "
                        "a code hello, config/fvep_page.yaml for a freq hello)")
    cycles = cfg.stim.code.train_cycles if cfg.stim.code is not None else 30
    p.add_argument("--train-cycles", type=int, default=cycles,
                   help=f"code mode: cycles asked for in train (default {cycles})")
    p.add_argument("--train-timeout", type=float, default=cfg.train.timeout_s,
                   help=f"code mode: wait for trained (default {cfg.train.timeout_s:g})")
    p.add_argument("--page-cycles", action="store_true",
                   help="code mode: also send the cycle messages our server relays from the "
                        "session page (a bridge run with --no-stimulus needs them)")
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
    args = build_parser(load_config()).parse_args(argv)
    cfg = load_config(args.config)
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
