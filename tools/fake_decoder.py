"""Fake SSVEP decoder that speaks INTERFACE.md v1, so the UI side never blocks on the real decoder.

It connects as a websocket client to the server's ``/ws/decoder``, sends ``hello``, and answers each
``stim_start`` with a ``selection`` (or ``no_selection``) for that trial after ``--latency`` seconds.
A ``stim_stop`` or a newer ``stim_start`` cancels the pending answer, so a stopped or stale trial is
never answered (INTERFACE.md section 5, rules 1-2).

Modes (pick one):
  --random          pick a random active slot (default); --fail-rate sends no_selection sometimes
  --keyboard        during a trial, type a number + Enter: 1 = active_slots[0], 2 = active_slots[1],
                    ...; "n" + Enter sends no_selection
  --script FILE     follow a YAML list of steps, one per trial (see tools/scripts/demo.yaml).
                    The script LOOPS back to the first step after the last one.

Run:
  python tools/fake_decoder.py --random
  python -m tools.fake_decoder --script tools/scripts/demo.yaml --latency 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

if __package__ in (None, ""):
    # Run as a plain script: make the repo root importable so `app` resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from app.config import ConfigError, load_config
from app.schemas import (
    PROTOCOL_V1,
    Baseline,
    DecoderHello,
    DecoderStatus,
    Display,
    Message,
    NoSelection,
    Selection,
    ServerHello,
    SlotId,
    StimStart,
    StimStop,
    parse_server_to_decoder,
    to_json,
)

log = logging.getLogger("fake_decoder")



def default_url() -> str:
    """The server's /ws/decoder URL from config/default.yaml (server.host / server.port)."""
    try:
        server = load_config().server
    except ConfigError as exc:
        log.warning("%s; falling back to 127.0.0.1:8000", exc)
        return "ws://127.0.0.1:8000/ws/decoder"
    # A server listening on all interfaces is still reached locally on 127.0.0.1.
    host = "127.0.0.1" if server.host == "0.0.0.0" else server.host
    return f"ws://{host}:{server.port}/ws/decoder"


# ---------------------------------------------------------------------------
# Script mode
# ---------------------------------------------------------------------------


class ScriptStep(BaseModel):
    """One scripted trial: exactly one of slot / no_selection / timeout, plus optional latency."""

    model_config = ConfigDict(extra="forbid")

    slot: SlotId | None = None
    no_selection: str | None = None
    timeout: bool = False
    latency: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _exactly_one_action(self) -> ScriptStep:
        actions = [self.slot is not None, self.no_selection is not None, self.timeout]
        if sum(actions) != 1:
            raise ValueError("each step needs exactly one of: slot, no_selection, timeout: true")
        return self


def load_script(path: str | Path) -> list[ScriptStep]:
    """Load and validate a script file. Raises ValueError if it is unreadable, empty or invalid."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: cannot read script: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path}: script must be a non-empty YAML list of steps")
    steps = []
    for i, raw in enumerate(data):
        try:
            steps.append(ScriptStep.model_validate(raw))
        except ValidationError as exc:
            raise ValueError(f"{path}: step {i + 1} is invalid: {exc}") from exc
    return steps


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _fraction(text: str) -> float:
    value = float(text)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fake_decoder", description="Fake SSVEP decoder speaking INTERFACE.md v1."
    )
    url = default_url()
    parser.add_argument("--url", default=url, help=f"server websocket (default {url}, from config)")
    parser.add_argument("--name", default="fake-decoder", help="name sent in hello")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--random", dest="mode", action="store_const", const="random",
                      help="pick a random active slot (default)")
    mode.add_argument("--keyboard", dest="mode", action="store_const", const="keyboard",
                      help="type 1, 2, ... + Enter during a trial; n = no_selection")
    mode.add_argument("--script", metavar="FILE", help="YAML list of steps; loops at the end")
    parser.add_argument("--latency", type=float, default=5.0,
                        help="seconds from stim_start to the answer (default 5.0)")
    parser.add_argument("--fail-rate", type=_fraction, default=0.0,
                        help="random mode: probability of no_selection (default 0)")
    parser.add_argument("--scores", action="store_true", help="attach fake score/scores")
    parser.add_argument("--seed", type=int, default=None, help="random seed")
    parser.add_argument("--once", action="store_true", help="exit after the first answered trial")
    parser.add_argument("--max-trials", type=int, default=None, metavar="N",
                        help="exit after N answered trials")
    parser.add_argument("--status-interval", type=float, default=1.0,
                        help="seconds between status messages (default 1.0)")
    parser.add_argument("--reconnect", action=argparse.BooleanOptionalAction, default=True,
                        help="retry if the connection drops (default on)")
    parser.add_argument("--reconnect-s", type=float, default=1.0,
                        help="seconds between reconnect attempts (default 1.0)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse arguments and fill in derived fields (mode, max_trials)."""
    args = build_parser().parse_args(argv)
    if args.script:
        args.mode = "script"
    elif args.mode is None:
        args.mode = "random"
    if args.once:
        args.max_trials = 1
    return args


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


class FakeDecoder:
    """Decoder client. Create with parsed args, then ``await run()``.

    In keyboard mode, lines from ``stdin`` feed ``self.keys``; pass ``stdin=None`` to feed the
    queue yourself (tests).
    """

    def __init__(self, args: argparse.Namespace, stdin: TextIO | None = sys.stdin) -> None:
        self.args = args
        self.stdin = stdin
        self.rng = random.Random(args.seed)
        self.steps = load_script(args.script) if args.mode == "script" else []
        self.step_index = 0
        self.keys: asyncio.Queue[str] = asyncio.Queue()
        self.server_hello: ServerHello | None = None
        self.current_trial: int | None = None
        self.pending: asyncio.Task[None] | None = None
        self.answered = 0
        self.finished = False

    # -- connection -------------------------------------------------------

    async def run(self) -> None:
        """Connect, serve trials, and reconnect on drops until finished (or reconnect is off)."""
        if self.args.mode == "keyboard" and self.stdin is not None:
            self._start_stdin_reader(self.stdin)
        while not self.finished:
            try:
                async with connect(self.args.url) as ws:
                    log.info("connected to %s", self.args.url)
                    await self._session(ws)
            except (OSError, ConnectionClosed, InvalidHandshake, TimeoutError) as exc:
                log.warning("connection problem: %s", exc)
            self._cancel_pending()
            if self.finished or not self.args.reconnect:
                break
            log.info("reconnecting in %.1f s", self.args.reconnect_s)
            await asyncio.sleep(self.args.reconnect_s)

    async def _session(self, ws: ClientConnection) -> None:
        await ws.send(to_json(DecoderHello(protocol=PROTOCOL_V1, name=self.args.name)))
        status_task = asyncio.create_task(self._send_status(ws))
        try:
            async for raw in ws:
                await self._handle(ws, raw)
        finally:
            status_task.cancel()
            self._cancel_pending()

    async def _send_status(self, ws: ClientConnection) -> None:
        status = to_json(DecoderStatus(ok=True, message="fake decoder"))
        try:
            while True:
                await ws.send(status)
                await asyncio.sleep(self.args.status_interval)
        except ConnectionClosed:
            pass

    # -- incoming messages ------------------------------------------------

    async def _handle(self, ws: ClientConnection, raw: str | bytes) -> None:
        try:
            msg = parse_server_to_decoder(raw)
        except ValidationError as exc:
            log.warning("ignored invalid or unknown message (%s): %.200s",
                        _type_of(raw), exc.errors()[0].get("msg", exc))
            return

        if isinstance(msg, ServerHello):
            self.server_hello = msg
            slots = ", ".join(f"{s.slot}={s.freq_hz:g}Hz/{s.phase_deg:g}deg" for s in msg.slots)
            log.info("server hello: protocol %d, %.1f Hz %s, %s",
                     msg.protocol, msg.refresh_hz, msg.waveform, slots)
            if msg.protocol != PROTOCOL_V1:
                log.warning("server speaks protocol %d, this fake decoder speaks %d",
                            msg.protocol, PROTOCOL_V1)
        elif isinstance(msg, StimStart):
            self._on_stim_start(ws, msg)
        elif isinstance(msg, StimStop):
            self._on_stim_stop(msg)
        elif isinstance(msg, (Display, Baseline)):
            # Protocol 2 proposals: a v1 decoder only logs them, so the commands can be checked.
            log.info("display command (not acted on): %s", to_json(msg))

    def _on_stim_start(self, ws: ClientConnection, msg: StimStart) -> None:
        if self.pending is not None and not self.pending.done():
            log.info("trial %s superseded by trial %d; its answer is dropped",
                     self.current_trial, msg.trial_id)
        self._cancel_pending()
        self.current_trial = msg.trial_id
        cue = f", cued {msg.cued_slot}" if msg.cued_slot else ""
        log.info("trial %d started (%s, active %s%s)",
                 msg.trial_id, msg.mode, ",".join(msg.active_slots), cue)
        self.pending = asyncio.create_task(self._answer(ws, msg))

    def _on_stim_stop(self, msg: StimStop) -> None:
        if msg.trial_id != self.current_trial:
            log.info("stim_stop for trial %d (not current), ignored", msg.trial_id)
            return
        log.info("trial %d stopped (%s)", msg.trial_id, msg.reason)
        self._cancel_pending()
        self.current_trial = None

    def _cancel_pending(self) -> None:
        if self.pending is not None and not self.pending.done():
            self.pending.cancel()
        self.pending = None

    # -- answering --------------------------------------------------------

    async def _answer(self, ws: ClientConnection, start: StimStart) -> None:
        answer = await self._decide(start)
        if answer is None:
            log.info("trial %d: sending nothing (timeout step)", start.trial_id)
            return
        if self.current_trial != start.trial_id:
            return  # trial ended while deciding
        self.current_trial = None  # at most one answer per trial
        try:
            await ws.send(to_json(answer))
        except ConnectionClosed:
            return
        log.info("trial %d -> %s", start.trial_id,
                 getattr(answer, "slot", None) or f"no_selection ({answer.reason})")
        self.answered += 1
        if self.args.max_trials is not None and self.answered >= self.args.max_trials:
            self.finished = True
            await ws.close()

    async def _decide(self, start: StimStart) -> Message | None:
        """Wait for the answer according to the mode. None means send nothing."""
        active = start.active_slots
        if self.args.mode == "keyboard":
            return await self._decide_keyboard(start)

        if self.args.mode == "script":
            step = self.steps[self.step_index % len(self.steps)]
            self.step_index += 1
            await asyncio.sleep(step.latency if step.latency is not None else self.args.latency)
            if step.timeout:
                return None
            if step.no_selection is not None:
                return NoSelection(trial_id=start.trial_id, reason=step.no_selection)
            if step.slot not in active:
                log.warning("trial %d: script slot %s is not active %s, sending no_selection",
                            start.trial_id, step.slot, active)
                return NoSelection(trial_id=start.trial_id, reason="slot_not_active")
            return self._selection(start, step.slot)

        await asyncio.sleep(self.args.latency)
        if self.rng.random() < self.args.fail_rate:
            return NoSelection(trial_id=start.trial_id, reason="low_signal")
        return self._selection(start, self.rng.choice(active))

    async def _decide_keyboard(self, start: StimStart) -> Message:
        active = start.active_slots
        while not self.keys.empty():  # drop keys typed between trials
            self.keys.get_nowait()
        choices = ", ".join(f"{i + 1}={slot}" for i, slot in enumerate(active))
        print(f"trial {start.trial_id}: type {choices} or n, then Enter", file=sys.stderr)
        while True:
            key = (await self.keys.get()).strip().lower()
            if key == "n":
                return NoSelection(trial_id=start.trial_id, reason="keyboard")
            if key.isdigit() and 1 <= int(key) <= len(active):
                return self._selection(start, active[int(key) - 1])
            print(f"  not a choice: {key!r} ({choices} or n)", file=sys.stderr)

    def _selection(self, start: StimStart, slot: str) -> Selection:
        extra: dict[str, Any] = {}
        if self.args.scores:
            # Fake CCA-like correlations: the chosen slot clearly highest.
            scores = {s: round(self.rng.uniform(0.1, 0.4), 3) for s in start.active_slots}
            scores[slot] = round(self.rng.uniform(0.5, 0.9), 3)
            extra = {"score": scores[slot], "scores": scores}
        return Selection(trial_id=start.trial_id, slot=slot, t_unix=time.time(), **extra)

    def _start_stdin_reader(self, stream: TextIO) -> None:
        # A daemon thread, so a blocked readline never keeps the process alive on exit.
        loop = asyncio.get_running_loop()

        def read() -> None:
            try:
                for line in stream:
                    loop.call_soon_threadsafe(self.keys.put_nowait, line)
            except (OSError, ValueError, RuntimeError) as exc:  # closed stream or loop
                log.warning("stdin reader stopped: %s", exc)

        threading.Thread(target=read, name="stdin-reader", daemon=True).start()


def _type_of(raw: str | bytes) -> str:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "not JSON"
    return f"type={data.get('type')!r}" if isinstance(data, dict) else "not an object"


async def run(args: argparse.Namespace) -> FakeDecoder:
    """Importable entry point: run a fake decoder until it finishes. Returns it for inspection."""
    decoder = FakeDecoder(args)
    await decoder.run()
    return decoder


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except ValueError as exc:  # bad script file
        sys.exit(f"fake_decoder: {exc}")


if __name__ == "__main__":
    main()
