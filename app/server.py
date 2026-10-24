"""FastAPI server: serves the partner page (ui/), its websocket /ws/ui, and the decoder websocket
/ws/decoder (INTERFACE.md).

One active partner page and one active decoder at a time; a new connection replaces the old one.
All state lives in the orchestrator, which runs in the event loop and is ticked every TICK_S.

    python -m app.server                    # real decoder connects to /ws/decoder
    python -m app.server --fake-decoder     # also start tools/fake_decoder.py --random
    python -m app.server --keyboard         # keys 1-2 on the partner page select
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.config import REPO_ROOT, AppConfig, IntentsConfig, load_config, load_intents
from app.log import DEFAULT_LOG_DIR, EventLog
from app.orchestrator import Orchestrator
from app.schemas import Message, parse_decoder_to_server, parse_ui_to_server, to_json

UI_DIR = REPO_ROOT / "ui"
TICK_S = 0.02  # how often deadlines are checked; well below the shortest timing in config
REPLACED = 4000  # websocket close code sent to a connection that a newer one replaced

log = logging.getLogger("app.server")


@dataclass
class Conn:
    """One websocket plus the queue its sender task drains."""

    ws: WebSocket
    queue: asyncio.Queue[Message] = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None

    def send(self, msg: Message) -> None:
        self.queue.put_nowait(msg)

    async def pump(self) -> None:
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            while True:
                msg = await self.queue.get()
                await self.ws.send_text(to_json(msg))


class Hub:
    """The active partner page and decoder. Messages to a missing peer are dropped (and logged)."""

    def __init__(self) -> None:
        self.ui: Conn | None = None
        self.decoder: Conn | None = None

    def send_ui(self, msg: Message) -> None:
        if self.ui is not None:
            self.ui.send(msg)

    def send_decoder(self, msg: Message) -> None:
        if self.decoder is not None:
            self.decoder.send(msg)


def create_app(
    cfg: AppConfig | None = None,
    intents: IntentsConfig | None = None,
    *,
    keyboard_mode: bool = False,
    fake_decoder: bool = False,
    clock: Callable[[], float] = time.time,
    log_dir: str | Path = DEFAULT_LOG_DIR,
) -> FastAPI:
    cfg = cfg or load_config()
    intents = intents or load_intents(
        option_count=cfg.menus.option_count,
        label_max_chars=cfg.menus.label_max_chars,
        utterance_max_words=cfg.menus.utterance_max_words,
        label_max_words=cfg.menus.label_max_words,
    )
    hub = Hub()
    event_log = EventLog(log_dir, clock=clock)
    orch = Orchestrator(cfg, intents, hub.send_ui, hub.send_decoder, clock=clock, log=event_log,
                        keyboard_mode=keyboard_mode, fake_decoder=fake_decoder)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async def ticker() -> None:
            while True:
                orch.tick()
                await asyncio.sleep(TICK_S)

        task = asyncio.create_task(ticker())
        event_log.log("server_start", keyboard_mode=keyboard_mode, fake_decoder=fake_decoder)
        try:
            yield
        finally:
            task.cancel()
            event_log.log("server_stop")
            event_log.close()

    app = FastAPI(title="BCI Voice", lifespan=lifespan)
    app.state.orch = orch
    app.state.hub = hub
    app.state.event_log = event_log

    @app.get("/api/config")
    def page_config() -> dict:
        """The few settings the partner page needs."""
        return {"tts_rate": cfg.speech.tts_rate, "keyboard_mode": keyboard_mode}

    async def attach(ws: WebSocket, role: str) -> Conn:
        await ws.accept()
        conn = Conn(ws)
        old = getattr(hub, role)
        setattr(hub, role, conn)
        if old is not None:
            event_log.log(f"{role}_replaced")
            with contextlib.suppress(Exception):
                await old.ws.close(code=REPLACED)
        conn.task = asyncio.create_task(conn.pump())
        return conn

    async def detach(conn: Conn, role: str) -> bool:
        """Stop the connection's sender; True if it was still the active one."""
        if conn.task is not None:
            conn.task.cancel()
        if getattr(hub, role) is conn:
            setattr(hub, role, None)
            return True
        return False

    async def receive(ws: WebSocket, parse, handle, role: str) -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                raw = await ws.receive_text()
                try:
                    msg = parse(raw)
                except ValidationError as exc:
                    event_log.log(f"invalid_from_{role}", raw=raw[:500], error=str(exc)[:500])
                    continue
                handle(msg)

    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket) -> None:
        conn = await attach(ws, "ui")
        orch.on_ui_connected()
        try:
            await receive(ws, parse_ui_to_server, orch.on_ui_message, "ui")
        finally:
            await detach(conn, "ui")

    @app.websocket("/ws/decoder")
    async def ws_decoder(ws: WebSocket) -> None:
        replacing = hub.decoder is not None
        conn = await attach(ws, "decoder")
        if replacing:
            # The old bridge is gone (its flicker is off): handle it as a disconnect, so a running
            # trial pauses and the new bridge resumes it after its hello passes.
            orch.on_decoder_disconnected()
        orch.on_decoder_connected()
        try:
            await receive(ws, parse_decoder_to_server, orch.on_decoder_message, "decoder")
        finally:
            if await detach(conn, "decoder"):
                orch.on_decoder_disconnected()

    # Mounted last so the API and websocket routes take precedence.
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m app.server", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, help="config file (default config/default.yaml)")
    p.add_argument("--keyboard", action="store_true", help="keys 1-2 on the partner page select")
    p.add_argument("--fake-decoder", action="store_true",
                   help="also start tools/fake_decoder.py --random against this server")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = load_config(args.config)
    app = create_app(cfg, keyboard_mode=args.keyboard, fake_decoder=args.fake_decoder)
    host = cfg.server.host
    url_host = "127.0.0.1" if host == "0.0.0.0" else host
    fake = None
    if args.fake_decoder:
        # It retries every second until the server is up, so starting it first is fine.
        # Its hello uses this server's config, so it passes the handshake check.
        slots = ",".join(f"{s.slot}={s.freq_hz:g}" for s in cfg.stim.slots)
        fake = subprocess.Popen([sys.executable, "-m", "tools.fake_decoder", "--random",
                                 "--url", f"ws://{url_host}:{cfg.server.port}/ws/decoder",
                                 "--slots", slots, "--waveform", cfg.stim.waveform,
                                 "--refresh-hz", f"{cfg.display.expected_refresh_hz:g}"],
                                cwd=REPO_ROOT)
    print(f"partner page: http://{url_host}:{cfg.server.port}", file=sys.stderr)
    try:
        import uvicorn

        uvicorn.run(app, host=host, port=cfg.server.port, log_level="warning")
    finally:
        if fake is not None:
            fake.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                fake.wait(timeout=5)


if __name__ == "__main__":
    main()
