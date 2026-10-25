"""app/tts.py with a mocked Cartesia (httpx.MockTransport): nothing here touches the network."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from app.config import load_config, load_env
from app.log import EventLog, read_events
from app.tts import ATTEMPTS, MAX_CLIPS, TTS_URL, CartesiaTTS, audio_id

SPEECH = load_config().speech
KEY = "sk_car_test_not_a_real_key"
CLIP = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xf3"  # enough to stand in for an MP3 clip
MP3 = "audio/mpeg"


class FakeCartesia:
    """Records requests; answers each with ``reply(request)`` after ``delay`` seconds."""

    def __init__(self, reply=None, delay: float = 0.0) -> None:
        self.requests: list[httpx.Request] = []
        self.reply = reply or (lambda req: httpx.Response(200, content=CLIP,
                                                          headers={"content-type": MP3}))
        self.delay = delay

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr("app.tts.RETRY_S", 0.01)


@pytest.fixture
def event_log(tmp_path):
    log = EventLog(tmp_path)
    yield log
    log.close()


async def make_tts(fake: FakeCartesia, event_log=None, key: str | None = KEY, cache_dir=None,
                   **speech) -> CartesiaTTS:
    tts = CartesiaTTS(SPEECH.model_copy(update=speech), key, log=event_log, transport=fake.transport,
                      cache_dir=cache_dir)
    await tts.start()
    return tts


async def test_success_caches_the_clip_and_sends_the_documented_request(event_log):
    fake = FakeCartesia()
    tts = await make_tts(fake, event_log)
    text = "Yes, let's go outside."
    assert tts.url_for(text) is None
    await tts.prefetch([text])
    clip_id = tts.id_for(text)
    assert tts.url_for(text) == f"/audio/{clip_id}"
    assert tts.get(clip_id) == (CLIP, MP3)

    (req,) = fake.requests
    assert str(req.url) == TTS_URL and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["cartesia-version"] == SPEECH.api_version
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {
        "model_id": "sonic-3.5",
        "transcript": text,  # the exact utterance (rule 2)
        "voice": {"id": SPEECH.voice_id},
        "language": "en",
        "output_format": {"container": "mp3", "sample_rate": 22050, "bit_rate": 64000},
    }
    assert any(e["event"] == "tts_ok" for e in read_events(event_log.path))
    await tts.stop()


class CountingCartesia(FakeCartesia):
    """Also tracks how many requests are in flight at once."""

    def __init__(self, delay: float) -> None:
        super().__init__(delay=delay)
        self.now = self.peak = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            return await super().handle(request)
        finally:
            self.now -= 1


async def test_a_page_is_fetched_concurrently_up_to_the_account_limit():
    fake = CountingCartesia(delay=0.2)
    tts = await make_tts(fake, max_concurrent=2, tts_timeout_s=0.3)  # a queued clip does not time out
    texts = ["One.", "Two.", "Three."]
    loop = asyncio.get_running_loop()
    started = loop.time()
    await tts.prefetch(texts)
    assert fake.peak == 2  # Cartesia answers 429 concurrency_limited above the plan's limit
    assert loop.time() - started < 0.55  # 2 rounds of 0.2 s, not 3
    assert all(tts.url_for(t) for t in texts)
    await tts.stop()


def _limited_then_ok():
    calls = []

    def reply(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, json={"error_code": "concurrency_limited"})
        return httpx.Response(200, content=CLIP, headers={"content-type": MP3})

    return reply


async def test_a_concurrency_limited_request_is_retried(event_log):
    fake = FakeCartesia(_limited_then_ok())
    tts = await make_tts(fake, event_log)
    await tts.prefetch(["Hello."])
    assert len(fake.requests) == 2 and tts.url_for("Hello.")
    assert [e["event"] for e in read_events(event_log.path) if e["event"].startswith("tts_")][-2:] == [
        "tts_retry", "tts_ok"]
    await tts.stop()


async def test_429_gives_up_after_all_attempts(event_log):
    fake = FakeCartesia(lambda req: httpx.Response(429, json={"error_code": "concurrency_limited"}))
    tts = await make_tts(fake, event_log)
    await tts.prefetch(["Hello."])
    assert len(fake.requests) == ATTEMPTS and tts.url_for("Hello.") is None
    events = [e["event"] for e in read_events(event_log.path) if e["event"] in ("tts_retry", "tts_failed")]
    assert events == ["tts_retry"] * (ATTEMPTS - 1) + ["tts_failed"]
    await tts.stop()


@pytest.mark.parametrize("status", [400, 401])
async def test_permanent_errors_are_not_retried(event_log, status):
    fake = FakeCartesia(lambda req: httpx.Response(status, json={"error": "nope"}))
    tts = await make_tts(fake, event_log)
    await tts.prefetch(["Hello."])
    assert len(fake.requests) == 1 and tts.url_for("Hello.") is None
    await tts.stop()


async def test_a_timeout_then_success_still_gets_the_clip(event_log):
    calls = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            await asyncio.sleep(1.0)  # the slow Wi-Fi spell
        return httpx.Response(200, content=CLIP, headers={"content-type": MP3})

    tts = CartesiaTTS(SPEECH.model_copy(update={"tts_timeout_s": 0.1}), KEY, log=event_log,
                      transport=httpx.MockTransport(handle))
    await tts.start()
    await tts.prefetch(["Hello."])
    assert len(calls) == 2 and tts.url_for("Hello.")
    await tts.stop()


async def test_wav_format_sends_the_wav_output_format():
    fake = FakeCartesia()
    tts = await make_tts(fake, format="wav", sample_rate=44100)
    await tts.prefetch(["Hello."])
    assert json.loads(fake.requests[0].content)["output_format"] == {
        "container": "wav", "encoding": "pcm_s16le", "sample_rate": 44100}
    await tts.stop()


async def test_identical_utterances_reuse_the_cache():
    fake = FakeCartesia()
    tts = await make_tts(fake)
    await tts.prefetch(["Same.", "Same."])
    await tts.prefetch(["Same."])
    tts.request(["Same."])
    assert len(fake.requests) == 1
    await tts.stop()


async def test_a_request_already_in_flight_is_not_sent_twice():
    fake = FakeCartesia(delay=0.1)
    tts = await make_tts(fake)
    tts.request(["Hello."])
    await tts.prefetch(["Hello."])
    assert len(fake.requests) == 1 and tts.url_for("Hello.")
    await tts.stop()


async def test_a_slow_clip_times_out_and_leaves_no_audio(event_log):
    tts = await make_tts(FakeCartesia(delay=1.0), event_log, tts_timeout_s=0.1)
    await tts.prefetch(["Too slow."])
    assert tts.url_for("Too slow.") is None
    failed = [e for e in read_events(event_log.path) if e["event"] == "tts_failed"]
    assert failed and failed[0]["reason"] == "timeout" and failed[0]["attempt"] == ATTEMPTS
    await tts.stop()


def _offline(req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("offline", request=req)


@pytest.mark.parametrize("reply, reason", [
    (lambda req: httpx.Response(401, json={"error": f"bad key {KEY}"}), "bad_response"),
    (lambda req: httpx.Response(200, json={"not": "audio"}), "bad_response"),
    (lambda req: httpx.Response(200, content=b"", headers={"content-type": MP3}), "bad_response"),
    (_offline, "ConnectError"),
], ids=["401 echoing the key", "json instead of audio", "empty audio", "no network"])
async def test_errors_leave_no_audio_and_never_log_the_key(event_log, reply, reason):
    tts = await make_tts(FakeCartesia(reply), event_log)
    await tts.prefetch(["Hello."])
    assert tts.url_for("Hello.") is None
    events = read_events(event_log.path)
    assert any(e["event"] == "tts_failed" and e["reason"] == reason for e in events)
    assert KEY not in event_log.path.read_text()
    await tts.stop()


@pytest.mark.parametrize("key, speech, why", [
    (None, {}, "CARTESIA_API_KEY is not set"),
    (KEY, {"voice_id": ""}, "speech.voice_id is empty"),
    (KEY, {"provider": "browser"}, "speech.provider is browser"),
], ids=["no key", "no voice", "browser provider"])
async def test_disabled_tts_sends_nothing(key, speech, why):
    fake = FakeCartesia()
    tts = await make_tts(fake, key=key, **speech)
    assert not tts.enabled and tts.why_disabled == why
    await tts.prefetch(["Hello."])
    assert fake.requests == [] and tts.url_for("Hello.") is None
    await tts.stop()


async def test_requests_before_start_are_fetched_on_start():
    fake = FakeCartesia()
    tts = CartesiaTTS(SPEECH, KEY, transport=fake.transport)
    tts.request(["Early."])  # the orchestrator sets its first page before the event loop runs
    assert fake.requests == []
    await tts.start()
    await tts.prefetch(["Early."])
    assert len(fake.requests) == 1 and tts.url_for("Early.")
    await tts.stop()


async def test_the_cache_is_bounded():
    tts = await make_tts(FakeCartesia())
    await tts.prefetch([f"Sentence {i}." for i in range(MAX_CLIPS + 5)])
    assert tts.url_for("Sentence 0.") is None and tts.url_for(f"Sentence {MAX_CLIPS + 4}.")
    await tts.stop()


def test_cache_key_depends_on_voice_and_text():
    assert audio_id("v1", "Hi.") == audio_id("v1", "Hi.")
    assert audio_id("v1", "Hi.") != audio_id("v2", "Hi.")
    assert audio_id("v1", "Hi.") != audio_id("v1", "Hi!")
    assert audio_id("v1", "Hi.", "sonic-3.5|mp3") != audio_id("v1", "Hi.", "sonic-3.5|wav")


# -- the disk cache --------------------------------------------------------------------------


async def test_a_clip_on_disk_survives_a_restart(tmp_path):
    fake = FakeCartesia()
    tts = await make_tts(fake, cache_dir=tmp_path)
    await tts.prefetch(["Kept."])
    await tts.stop()
    clip_id = tts.id_for("Kept.")
    assert (tmp_path / f"{clip_id}.mp3").read_bytes() == CLIP
    assert not list(tmp_path.glob("*.tmp"))

    again = await make_tts(fake, cache_dir=tmp_path)  # a new process: empty memory
    assert again.url_for("Kept.") == f"/audio/{clip_id}"
    await again.prefetch(["Kept."])
    assert len(fake.requests) == 1  # never fetched twice
    assert again.get(clip_id) == (CLIP, MP3)
    assert again.cached_count(["Kept.", "Kept.", "New."]) == (1, 2)
    await again.stop()


async def test_a_disk_clip_plays_without_a_key_or_network(tmp_path):
    await (await make_tts(FakeCartesia(), cache_dir=tmp_path)).prefetch(["Offline."])
    fake = FakeCartesia(_offline)
    tts = await make_tts(fake, key=None, cache_dir=tmp_path)
    assert tts.url_for("Offline.") and tts.get(tts.id_for("Offline."))[0] == CLIP
    assert fake.requests == []
    await tts.stop()


@pytest.mark.parametrize("change", [{"voice_id": "another-voice"}, {"model": "sonic-3"},
                                    {"format": "wav"}, {"bit_rate": 128000},
                                    {"provider": "browser"}],
                         ids=["voice", "model", "format", "bit rate", "browser provider"])
async def test_a_changed_voice_or_format_never_replays_an_old_clip(tmp_path, change):
    await (await make_tts(FakeCartesia(), cache_dir=tmp_path)).prefetch(["Hello."])
    tts = await make_tts(FakeCartesia(), key=None, cache_dir=tmp_path, **change)
    assert tts.url_for("Hello.") is None
    await tts.stop()


@pytest.mark.parametrize("clip_id", ["../../etc/passwd", "a" * 31, "A" * 32, ""])
async def test_only_clip_ids_reach_the_disk(tmp_path, clip_id):
    tts = await make_tts(FakeCartesia(), cache_dir=tmp_path)
    assert tts.get(clip_id) is None
    await tts.stop()


async def test_a_disk_that_cannot_be_written_still_keeps_the_clip_in_memory(tmp_path, event_log):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    tts = await make_tts(FakeCartesia(), event_log, cache_dir=blocked / "tts")
    await tts.prefetch(["Memory."])
    assert tts.url_for("Memory.")
    assert any(e["event"] == "tts_cache_write_failed" for e in read_events(event_log.path))
    await tts.stop()


async def test_prerender_fetches_only_missing_clips(tmp_path):
    from tools.prerender_tts import prerender

    fake = FakeCartesia()
    await (await make_tts(fake, cache_dir=tmp_path)).prefetch(["One."])
    tts = CartesiaTTS(SPEECH, KEY, transport=fake.transport, cache_dir=tmp_path)
    assert await prerender(tts, ["One.", "Two.", "Three."]) == (3, 3)
    assert len(fake.requests) == 3  # "One." once, then the two missing ones


def test_load_env_sets_missing_names_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# secrets\nBS_TEST_A=one\nexport BS_TEST_B='two'\nBS_TEST_C=x\n\nnot a line\n")
    monkeypatch.delenv("BS_TEST_A", raising=False)
    monkeypatch.delenv("BS_TEST_B", raising=False)
    monkeypatch.setenv("BS_TEST_C", "already set")
    try:
        assert load_env(env) == ["BS_TEST_A", "BS_TEST_B"]  # names only, never values
        assert os.environ["BS_TEST_A"] == "one" and os.environ["BS_TEST_B"] == "two"
        assert os.environ["BS_TEST_C"] == "already set"
    finally:
        os.environ.pop("BS_TEST_A", None)
        os.environ.pop("BS_TEST_B", None)
    assert load_env(tmp_path / "missing.env") == []


def test_load_env_unreadable_file_is_a_note_not_a_crash(tmp_path, monkeypatch, capsys):
    # BrainSyncOS: /etc/brainsync/secrets.env is root:brainsync 0640; another account gets
    # PermissionError and must still start, with the keys from the environment only.
    env = tmp_path / "secrets.env"
    env.write_text("BS_TEST_SECRET=sk-never-shown\n")

    def denied(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", denied)
    monkeypatch.delenv("BS_TEST_SECRET", raising=False)
    assert load_env(env) == []
    assert "BS_TEST_SECRET" not in os.environ
    err = capsys.readouterr().err
    assert f"cannot read {env} (PermissionError)" in err and "sk-never-shown" not in err
