"""Spoken prompts with Cartesia text-to-speech, for every brainsync app (no macOS ``say``).

``speak(text)`` returns at once; the clip is synthesised over Cartesia's REST ``/tts/bytes``
(the same voice, model and API version as the voice app's config/default.yaml) and played
by the system's audio player, one utterance at a time: a new prompt waits for the one
playing instead of talking over it, and when prompts pile up the oldest waiting ones are
dropped. Up to two clips are synthesised ahead while one plays (the account allows two
requests at once). Clips are cached on disk by voice, model and text, so a prompt heard
once plays instantly the next time; ``prefetch`` warms the cache for prompts that are
known in advance.

The key is ``CARTESIA_API_KEY`` from the environment, else from the first ``.env`` found
in the working directory or above it, or above this package (the repo root's ``.env``
serves every worktree under it). It is never printed. Without a key, without a player,
or when a request fails, the prompt is only printed (there is no other voice to fall back
to). ``BRAINSYNC_TTS=off`` silences everything, which the test suite uses;
``CARTESIA_VOICE_ID`` and ``BRAINSYNC_TTS_CACHE`` override the voice and the cache folder.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

__all__ = ["CartesiaSpeaker", "TTSError", "speak", "prefetch", "find_api_key",
           "player_command", "default_speaker", "DEFAULT_VOICE_ID"]

TTS_URL = "https://api.cartesia.ai/tts/bytes"
DEFAULT_VOICE_ID = "47c38ca4-5f35-497b-b1a3-415245fb35e1"   # config/default.yaml speech.voice_id
MODEL = "sonic-3.5"              # pinned, as in the voice app; never sonic-latest
API_VERSION = "2026-03-01"       # Cartesia-Version header
LANGUAGE = "en"
SAMPLE_RATE = 22050
TIMEOUT_S = 8.0                  # per request, once sent
ATTEMPTS = 3                     # for timeouts, network errors, 429 and 5xx
RETRY_S = 0.5                    # pause before retry n is n * RETRY_S
MAX_PENDING = 3                  # prompts waiting to be played; older ones are dropped
MAX_AHEAD = 2                    # clips synthesised at once (Cartesia plan limit)
DRAIN_S = 4.0                    # at exit, how long the prompts still queued may play
KEY_NAME = "CARTESIA_API_KEY"
# players that take a WAV file and block until it has played, in order of preference
PLAYERS = (("afplay",), ("paplay",), ("aplay", "-q"),
           ("ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"))
# Windows has none of those; PowerShell's SoundPlayer plays a WAV and blocks the same way
WINDOWS_PLAYER = ("powershell", "-NoProfile", "-NonInteractive", "-Command")

Post = Callable[[dict, dict], bytes]
Play = Callable[[Path], None]


def _stderr_note(msg: str) -> None:
    print(f"voice: {msg}", file=sys.stderr, flush=True)


def read_dotenv(path: Path, name: str) -> str | None:
    """The value of ``name`` in a ``.env`` file (KEY=value lines, optional ``export`` and
    quotes), or None when the file or the key is absent."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value or None
    return None


def find_api_key(env: Mapping[str, str] = os.environ,
                 starts: Iterable[Path] | None = None) -> str | None:
    """CARTESIA_API_KEY from ``env``, else from the nearest ``.env`` above any of ``starts``
    (default: the working directory, then this package)."""
    if env.get(KEY_NAME):
        return env[KEY_NAME]
    starts = [Path.cwd(), Path(__file__).resolve().parent] if starts is None else starts
    for start in starts:
        for folder in (start, *start.parents):
            key = read_dotenv(folder / ".env", KEY_NAME)
            if key:
                return key
    return None


def player_command(path: Path, which: Callable[[str], str | None] = shutil.which
                   ) -> list[str] | None:
    """The command that plays the WAV at ``path`` and returns when done, or None."""
    for cmd in PLAYERS:
        if which(cmd[0]):
            return [*cmd, str(path)]
    if which(WINDOWS_PLAYER[0]):
        # a PowerShell single-quoted string: doubling a quote escapes it, and PowerShell
        # also ends the string at the typographic single quotes U+2018 to U+201B
        quoted = "".join(c * 2 if c in "'‘’‚‛" else c for c in str(path))
        return [*WINDOWS_PLAYER, f"(New-Object Media.SoundPlayer '{quoted}').PlaySync()"]
    return None


def clip_key(voice_id: str, text: str) -> str:
    """Cache file stem for one utterance in one voice, model and format."""
    blob = f"{MODEL}\n{API_VERSION}\n{voice_id}\n{SAMPLE_RATE}\n{text}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def request_body(voice_id: str, text: str) -> dict:
    """The /tts/bytes request: the exact text, as 16-bit WAV every player can open."""
    return {"model_id": MODEL, "transcript": text, "voice": {"id": voice_id},
            "language": LANGUAGE,
            "output_format": {"container": "wav", "encoding": "pcm_s16le",
                              "sample_rate": SAMPLE_RATE}}


class TTSError(Exception):
    """A request that failed; ``retry`` when trying again may help (timeout, 429, 5xx)."""

    def __init__(self, message: str, retry: bool) -> None:
        super().__init__(message)
        self.retry = retry


def _httpx_post(body: dict, headers: dict) -> bytes:
    import httpx   # lazy: only a process that speaks pays for the import

    try:
        r = httpx.post(TTS_URL, json=body, headers=headers, timeout=TIMEOUT_S)
    except httpx.TimeoutException:
        raise TTSError("timed out", retry=True) from None
    except httpx.HTTPError as exc:
        raise TTSError(type(exc).__name__, retry=True) from None
    kind = r.headers.get("content-type", "")
    if r.status_code == 200 and kind.startswith("audio/") and r.content:
        return r.content
    raise TTSError(f"HTTP {r.status_code}", retry=r.status_code == 429 or r.status_code >= 500)


class CartesiaSpeaker:
    """Speaks one prompt at a time in one Cartesia voice (see the module docstring).

    ``post(body, headers)`` sends one request and returns the audio (or raises TTSError);
    ``play(path)`` plays a WAV and returns when it is done. Both default to the real thing
    and are replaced in tests.
    """

    def __init__(self, api_key: str | None, voice_id: str = DEFAULT_VOICE_ID,
                 cache_dir: Path | None = None, *, post: Post | None = None,
                 play: Play | None = None, note: Callable[[str], None] = _stderr_note,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.voice_id = voice_id
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(
            os.environ.get("BRAINSYNC_TTS_CACHE", Path.home() / ".cache" / "brainsync" / "tts"))
        self._key = api_key or ""
        self._post = post if post is not None else _httpx_post
        self._player = play
        self._note = note
        self._sleep = sleep
        self._noted: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue: deque[str] = deque()
        self._futures: dict[str, Future] = {}
        self._pool = ThreadPoolExecutor(max_workers=MAX_AHEAD, thread_name_prefix="cartesia")
        self._worker: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._playing = False
        self._closed = False
        self.spoken = 0
        self.dropped = 0
        if not self._key:
            self._note_once(f"{KEY_NAME} is not set (environment or .env); prompts are "
                            "printed only")

    @property
    def enabled(self) -> bool:
        return bool(self._key) and not self._closed

    def _note_once(self, msg: str) -> None:
        if msg not in self._noted:
            self._noted.add(msg)
            self._note(msg)

    # -- synthesis -----------------------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}", "Cartesia-Version": API_VERSION,
                "Content-Type": "application/json"}

    def _synth(self, text: str) -> Path | None:
        path = self.cache_dir / f"{clip_key(self.voice_id, text)}.wav"
        if path.is_file() and path.stat().st_size > 44:      # more than a WAV header
            return path
        for attempt in range(1, ATTEMPTS + 1):
            try:
                audio = self._post(request_body(self.voice_id, text), self._headers())
            except TTSError as exc:
                if exc.retry and attempt < ATTEMPTS:
                    self._sleep(attempt * RETRY_S)
                    continue
                self._note_once(f"Cartesia request failed ({exc}); that prompt is printed only")
                return None
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
                tmp.write_bytes(audio)
                os.replace(tmp, path)
            except OSError as exc:
                self._note_once(f"could not cache a clip in {self.cache_dir}: {exc}")
                return None
            return path
        return None

    def _future(self, text: str) -> Future:
        with self._lock:
            f = self._futures.get(text)
            if f is None or (f.done() and f.result() is None):   # failed before: try again
                f = self._pool.submit(self._synth, text)
                self._futures[text] = f
            return f

    def clip(self, text: str, timeout: float | None = None) -> Path | None:
        """The WAV of ``text`` (synthesised or from the cache), or None when it failed."""
        text = " ".join(str(text).split())
        if not self.enabled or not text:
            return None
        return self._future(text).result(timeout)

    def prefetch(self, texts: Iterable[str]) -> None:
        """Synthesise these prompts in the background, so they play at once later."""
        if self.enabled:
            for text in texts:
                text = " ".join(str(text).split())
                if text:
                    self._future(text)

    # -- playback ------------------------------------------------------------------------

    def say(self, text: str) -> None:
        """Queue ``text`` to be spoken after whatever is playing; returns at once."""
        text = " ".join(str(text).split())
        if not self.enabled or not text:
            return
        self._future(text)
        with self._wake:
            self._queue.append(text)
            while len(self._queue) > MAX_PENDING:
                self._queue.popleft()
                self.dropped += 1
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="cartesia-player",
                                                daemon=True)
                self._worker.start()
            self._wake.notify_all()

    def _run(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._closed:
                    self._wake.wait()
                if self._closed:
                    return
                text = self._queue.popleft()
                self._playing = True
            try:
                path = self._future(text).result()
                if path is not None and not self._closed:
                    self._play(path)
                    self.spoken += 1
            except Exception as exc:  # noqa: BLE001 - one bad clip must not end the voice
                self._note_once(f"could not play a prompt: {exc}")
            finally:
                with self._wake:
                    self._playing = False
                    self._wake.notify_all()

    def _play(self, path: Path) -> None:
        if self._player is not None:
            self._player(path)
            return
        cmd = player_command(path)
        if cmd is None:
            self._note_once("no audio player found (afplay, paplay, aplay, ffplay or "
                            "PowerShell); "
                            "prompts are printed only")
            return
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._proc = proc
        proc.wait()
        self._proc = None

    def wait(self, timeout: float) -> bool:
        """Block until nothing is playing or waiting, or ``timeout``; True when idle."""
        end = time.monotonic() + timeout
        with self._wake:
            while self._queue or self._playing:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._wake.wait(left)
        return True

    def close(self, drain_s: float = 0.0) -> None:
        """Let the prompts already queued play for up to ``drain_s``, then stop."""
        if drain_s > 0 and self.enabled:
            self.wait(drain_s)
        with self._wake:
            self._closed = True
            self._queue.clear()
            self._wake.notify_all()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
        self._pool.shutdown(wait=False, cancel_futures=True)


# -- the process-wide voice -----------------------------------------------------------------

_speakers: dict[str, CartesiaSpeaker] = {}
_speakers_lock = threading.Lock()


def _silenced() -> bool:
    return os.environ.get("BRAINSYNC_TTS", "").strip().lower() in ("off", "0", "false", "no")


def default_speaker(voice_id: str | None = None) -> CartesiaSpeaker | None:
    """The shared speaker for ``voice_id`` (default: CARTESIA_VOICE_ID or the voice app's
    voice), made on first use; None when BRAINSYNC_TTS=off."""
    if _silenced():
        return None
    vid = voice_id or os.environ.get("CARTESIA_VOICE_ID") or DEFAULT_VOICE_ID
    with _speakers_lock:
        if vid not in _speakers:
            if not _speakers:
                atexit.register(_shutdown)
            _speakers[vid] = CartesiaSpeaker(find_api_key(), vid)
        return _speakers[vid]


def speak(text: str, voice_id: str | None = None) -> None:
    """Say ``text`` with Cartesia after any prompt still playing; returns at once."""
    speaker = default_speaker(voice_id)
    if speaker is not None:
        speaker.say(text)


def prefetch(texts: Iterable[str], voice_id: str | None = None) -> None:
    """Warm the cache for prompts the app will say later."""
    speaker = default_speaker(voice_id)
    if speaker is not None:
        speaker.prefetch(texts)


def _shutdown() -> None:
    with _speakers_lock:
        speakers = list(_speakers.values())
    for s in speakers:
        s.close(drain_s=DRAIN_S)
