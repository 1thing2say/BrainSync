"""Cartesia text-to-speech: pre-generated audio for each page's options (PLAN.md V2, item 3).

As soon as a page is set, the orchestrator hands its option utterances to ``request``; each one is
synthesised concurrently over Cartesia's REST ``/tts/bytes`` (plain ``httpx``, not the SDK) and
cached in memory, keyed by a hash of voice id + utterance. By the time the user confirms "Say it"
the clip is usually there, so ``speak`` carries ``/audio/{id}`` and playback starts at once. A clip
that is still missing then (every attempt failed or took longer than ``speech.tts_timeout_s``) has
no URL, and the browser speaks the sentence with ``speechSynthesis`` instead.

Clips are MP3 by default (``speech.format``): about 10x smaller than 44.1 kHz WAV, which matters on
venue Wi-Fi. The account allows ``speech.max_concurrent`` requests at once (more gets HTTP 429
``concurrency_limited``), so the rest wait their turn in rank order; the timeout counts from when a
request is sent, not while it waits. Timeouts, network errors, 429 and 5xx are retried up to
``ATTEMPTS`` times in all; other errors (400, 401) are not.

The API key comes from the environment (``CARTESIA_API_KEY``, loaded from ``.env`` by the server's
``main``). It is never logged: log events carry only ids, sizes, timings and error types.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Iterable

import httpx

from app.config import SpeechConfig
from app.log import EventLog

TTS_URL = "https://api.cartesia.ai/tts/bytes"
AUDIO_ROUTE = "/audio"
MAX_CLIPS = 256  # oldest clips are dropped first; a page needs 3
ATTEMPTS = 3  # per clip, for timeouts, network errors, 429 and 5xx
RETRY_S = 0.5  # pause before retry n is n * RETRY_S
KEEPALIVE_S = 60.0  # keep connections open between pages; on slow Wi-Fi connect + TLS took 1-2 s


def audio_id(voice_id: str, text: str) -> str:
    """Cache key for one utterance in one voice."""
    return hashlib.sha256(f"{voice_id}\n{text}".encode()).hexdigest()[:32]


class CartesiaTTS:
    """Prefetches and caches Cartesia clips. Disabled (every ``url_for`` is None) without a key."""

    def __init__(
        self,
        cfg: SpeechConfig,
        api_key: str | None,
        log: EventLog | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self._api_key = api_key or ""
        self.log = log
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._clips: OrderedDict[str, tuple[bytes, str]] = OrderedDict()  # id -> (audio, media type)
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._waiting: list[str] = []  # asked for before start()
        self._slots = asyncio.Semaphore(cfg.max_concurrent)

    @property
    def enabled(self) -> bool:
        return not self.why_disabled

    @property
    def why_disabled(self) -> str:
        """Empty when Cartesia is in use; otherwise why the browser voice is used instead."""
        if self.cfg.provider != "cartesia":
            return f"speech.provider is {self.cfg.provider}"
        if not self._api_key:
            return "CARTESIA_API_KEY is not set"
        if not self.cfg.voice_id:
            return "speech.voice_id is empty"
        return ""

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Open the HTTP client and fetch anything requested before the event loop ran."""
        if not self.enabled:
            self._log("tts_disabled", reason=self.why_disabled)
            return
        limits = httpx.Limits(max_connections=self.cfg.max_concurrent, keepalive_expiry=KEEPALIVE_S)
        self._client = httpx.AsyncClient(transport=self._transport, limits=limits,
                                         timeout=httpx.Timeout(self.cfg.tts_timeout_s))
        self._log("tts_ready", model=self.cfg.model, voice_id=self.cfg.voice_id)
        waiting, self._waiting = self._waiting, []
        self.request(waiting)

    async def stop(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- cache --------------------------------------------------------------

    def id_for(self, text: str) -> str:
        return audio_id(self.cfg.voice_id, text)

    def get(self, clip_id: str) -> tuple[bytes, str] | None:
        """The cached clip and its media type (``audio/mpeg`` or ``audio/wav``), or None."""
        return self._clips.get(clip_id)

    def url_for(self, text: str) -> str | None:
        """``/audio/{id}`` if this utterance's clip is cached, else None (the browser speaks it)."""
        clip_id = self.id_for(text)
        return f"{AUDIO_ROUTE}/{clip_id}" if clip_id in self._clips else None

    # -- fetching -----------------------------------------------------------

    def request(self, texts: Iterable[str]) -> None:
        """Start fetching ``texts`` in the background (returns at once; for the orchestrator)."""
        if not self.enabled:
            return
        if self._client is None:
            self._waiting.extend(texts)
            return
        for text in dict.fromkeys(texts):
            clip_id = self.id_for(text)
            if clip_id in self._clips or clip_id in self._inflight:
                continue
            task = asyncio.get_running_loop().create_task(self._fetch(clip_id, text))
            self._inflight[clip_id] = task
            task.add_done_callback(lambda _t, cid=clip_id: self._inflight.pop(cid, None))

    async def prefetch(self, texts: Iterable[str]) -> None:
        """Fetch ``texts`` concurrently and wait until each one is cached or has failed."""
        texts = list(texts)
        self.request(texts)
        tasks = [self._inflight[cid] for cid in map(self.id_for, texts) if cid in self._inflight]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch(self, clip_id: str, text: str) -> None:
        for attempt in range(1, ATTEMPTS + 1):
            response: httpx.Response | None = None
            async with self._slots:
                started = time.monotonic()
                try:
                    response = await asyncio.wait_for(self._post(text), self.cfg.tts_timeout_s)
                    reason = "" if _is_audio(response) else "bad_response"
                except TimeoutError:
                    reason = "timeout"
                except httpx.HTTPError as exc:  # its message holds the URL only; log the type alone
                    reason = type(exc).__name__
            ms = round((time.monotonic() - started) * 1000)
            if not reason:
                assert response is not None
                kind = response.headers["content-type"].split(";")[0].strip()
                self._clips[clip_id] = (response.content, kind)
                while len(self._clips) > MAX_CLIPS:
                    self._clips.popitem(last=False)
                self._log("tts_ok", audio_id=clip_id, chars=len(text), bytes=len(response.content),
                          ms=ms, attempt=attempt)
                return
            status = response.status_code if response is not None else None
            transient = status is None or status == 429 or status >= 500
            retry = transient and attempt < ATTEMPTS
            fields: dict[str, object] = {"audio_id": clip_id, "reason": reason, "status": status,
                                         "ms": ms, "attempt": attempt}
            if response is not None and not _is_audio(response):
                fields["detail"] = self._redact(response.text[:200])
            self._log("tts_retry" if retry else "tts_failed", **fields)
            if not retry:
                return
            await asyncio.sleep(RETRY_S * attempt)

    async def _post(self, text: str) -> httpx.Response:
        assert self._client is not None
        return await self._client.post(TTS_URL, headers=self._headers(), json=self.body(text))

    def body(self, text: str) -> dict:
        """The /tts/bytes request body: the exact utterance (rule 2), in a format <audio> plays."""
        if self.cfg.format == "mp3":
            output = {"container": "mp3", "sample_rate": self.cfg.sample_rate,
                      "bit_rate": self.cfg.bit_rate}
        else:
            output = {"container": "wav", "encoding": "pcm_s16le", "sample_rate": self.cfg.sample_rate}
        return {
            "model_id": self.cfg.model,
            "transcript": text,
            "voice": {"id": self.cfg.voice_id},
            "language": self.cfg.language,
            "output_format": output,
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Cartesia-Version": self.cfg.api_version,
            "Content-Type": "application/json",
        }

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "[redacted]") if self._api_key else text

    def _log(self, event: str, **fields: object) -> None:
        if self.log is not None:
            self.log.log(event, **fields)


def _is_audio(response: httpx.Response) -> bool:
    kind = response.headers.get("content-type", "")
    return response.status_code == 200 and kind.startswith("audio/") and bool(response.content)
