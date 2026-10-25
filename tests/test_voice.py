"""brainsync.voice: Cartesia prompts without macOS say. A fake Cartesia and a fake player,
no network and no sound."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from brainsync import voice
from brainsync.voice import CartesiaSpeaker, TTSError

WAV = b"RIFF" + b"\0" * 100


class FakeCartesia:
    """Answers /tts/bytes; ``fail`` is a list of errors to raise first."""

    def __init__(self, fail: list[TTSError] | None = None, gate: threading.Event | None = None):
        self.calls: list[tuple[dict, dict]] = []
        self.fail = list(fail or [])
        self.gate = gate

    def __call__(self, body: dict, headers: dict) -> bytes:
        self.calls.append((body, headers))
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise self.fail.pop(0)
        return WAV + body["transcript"].encode()


class FakePlayer:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.played: list[bytes] = []
        self.gate = gate
        self.started = threading.Event()

    def __call__(self, path: Path) -> None:
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        self.played.append(path.read_bytes()[len(WAV):])


def speaker(tmp_path, post=None, play=None, notes=None, key="sk-test") -> CartesiaSpeaker:
    return CartesiaSpeaker(key, "voice-1", tmp_path, post=post or FakeCartesia(),
                           play=play or FakePlayer(),
                           note=(notes.append if notes is not None else lambda m: None),
                           sleep=lambda s: None)


def test_request_matches_the_voice_app_settings(tmp_path):
    post = FakeCartesia()
    s = speaker(tmp_path, post=post)
    path = s.clip("Look at the piece you want to move.")
    assert path is not None and path.parent == tmp_path and path.suffix == ".wav"
    body, headers = post.calls[0]
    assert body == {"model_id": "sonic-3.5", "transcript": "Look at the piece you want to move.",
                    "voice": {"id": "voice-1"}, "language": "en",
                    "output_format": {"container": "wav", "encoding": "pcm_s16le",
                                      "sample_rate": 22050}}
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["Cartesia-Version"] == "2026-03-01"
    s.close()


def test_clips_are_cached_on_disk(tmp_path):
    post = FakeCartesia()
    s = speaker(tmp_path, post=post)
    a = s.clip("Back")
    assert s.clip("  Back ") == a and len(post.calls) == 1    # same text, same clip
    s.close()
    again = speaker(tmp_path, post=FakeCartesia())             # a new run: from the disk
    assert again.clip("Back") == a and again._post.calls == []
    other = CartesiaSpeaker("sk-test", "voice-2", tmp_path, post=FakeCartesia(),
                            play=FakePlayer())
    assert other.clip("Back") != a                             # another voice, another clip
    again.close()
    other.close()


def test_prompts_play_one_at_a_time_in_order(tmp_path):
    gate = threading.Event()
    player = FakePlayer(gate)
    s = speaker(tmp_path, play=player)
    s.say("You play e4")
    assert player.started.wait(5)                     # the first is playing, held open
    s.say("Black plays e5")
    s.say("Look at the piece you want to move")
    assert player.played == []                        # nothing talks over the first
    gate.set()
    assert s.wait(5)
    assert player.played == [b"You play e4", b"Black plays e5",
                             b"Look at the piece you want to move"]
    assert s.spoken == 3 and s.dropped == 0
    s.close()


def test_a_backlog_drops_the_oldest_waiting_prompts(tmp_path):
    gate = threading.Event()
    player = FakePlayer(gate)
    s = speaker(tmp_path, play=player)
    s.say("first")
    assert player.started.wait(5)
    for k in range(5):
        s.say(f"waiting {k}")
    gate.set()
    assert s.wait(5)
    assert player.played == [b"first", b"waiting 2", b"waiting 3", b"waiting 4"]
    assert s.dropped == 2
    s.close()


def test_retries_then_gives_up_quietly(tmp_path):
    notes: list[str] = []
    post = FakeCartesia(fail=[TTSError("HTTP 429", retry=True), TTSError("timed out", retry=True)])
    s = speaker(tmp_path, post=post, notes=notes)
    assert s.clip("hello") is not None and len(post.calls) == 3   # third attempt worked
    bad = FakeCartesia(fail=[TTSError("HTTP 401", retry=False)])
    s2 = speaker(tmp_path, post=bad, notes=notes)
    assert s2.clip("other") is None and len(bad.calls) == 1       # 401: no retry
    assert any("HTTP 401" in n for n in notes)
    assert all("sk-test" not in n for n in notes)                 # the key never shows
    assert s2.clip("other") is not None                           # a later try may work
    s.close()
    s2.close()


def test_without_a_key_prompts_are_printed_only(tmp_path):
    notes: list[str] = []
    post = FakeCartesia()
    player = FakePlayer()
    s = speaker(tmp_path, post=post, play=player, notes=notes, key="")
    assert not s.enabled
    s.say("hello")
    s.prefetch(["a", "b"])
    assert s.clip("hello") is None and post.calls == [] and player.played == []
    assert notes == ["CARTESIA_API_KEY is not set (environment or .env); prompts are printed only"]


def test_prefetch_warms_the_cache(tmp_path):
    post = FakeCartesia()
    s = speaker(tmp_path, post=post)
    s.prefetch(["Back", "Only one move", "  "])
    assert s.clip("Back") is not None and s.clip("Only one move") is not None
    assert sorted(b["transcript"] for b, _ in post.calls) == ["Back", "Only one move"]
    s.close()


def test_close_stops_waiting_prompts(tmp_path):
    gate = threading.Event()
    player = FakePlayer(gate)
    s = speaker(tmp_path, play=player)
    s.say("one")
    assert player.started.wait(5)
    s.say("two")
    s.close()
    gate.set()
    assert player.played in ([], [b"one"]) and not s.enabled
    s.say("after")                                    # ignored once closed


def test_find_api_key(tmp_path, monkeypatch):
    assert voice.find_api_key({"CARTESIA_API_KEY": "from-env"}, [tmp_path]) == "from-env"
    nested = tmp_path / "repo" / ".dmux" / "worktrees" / "w"
    nested.mkdir(parents=True)
    assert voice.find_api_key({}, [nested]) is None
    (tmp_path / "repo" / ".env").write_text('OTHER=1\nexport CARTESIA_API_KEY="sk-file"\n')
    assert voice.find_api_key({}, [nested]) == "sk-file"      # the repo root's .env
    (nested / ".env").write_text("CARTESIA_API_KEY='sk-near'\n")
    assert voice.find_api_key({}, [nested]) == "sk-near"      # the nearest wins
    (nested / ".env").write_text("CARTESIA_API_KEY=\n")
    assert voice.find_api_key({}, [nested]) == "sk-file"      # empty: keep looking


def test_player_command_preference():
    wav = Path("/tmp/x.wav")
    have = {"aplay", "ffplay"}
    cmd = voice.player_command(wav, which=lambda n: n if n in have else None)
    assert cmd == ["aplay", "-q", str(wav)]                  # str: "\tmp\x.wav" on Windows
    assert voice.player_command(wav, which=lambda n: "/usr/bin/afplay"
                                if n == "afplay" else None) == ["afplay", str(wav)]
    assert voice.player_command(wav, which=lambda n: None) is None
    assert all(p[0] != "say" for p in voice.PLAYERS)


def test_player_command_powershell_only_on_windows(monkeypatch):
    only_ps = lambda n: n if n == "powershell" else None          # noqa: E731
    for platform in ("darwin", "linux"):                         # e.g. Linux's snap PowerShell
        monkeypatch.setattr(voice.sys, "platform", platform)
        assert voice.player_command(Path("/tmp/x.wav"), which=only_ps) is None


def test_player_command_windows_fallback(monkeypatch):
    monkeypatch.setattr(voice.sys, "platform", "win32")
    path = Path("C:/Users/o'brien/clip.wav")
    cmd = voice.player_command(path, which=lambda n: n if n == "powershell" else None)
    assert cmd[:4] == list(voice.WINDOWS_PLAYER)
    quoted = str(path).replace("'", "''")             # the quote cannot end the string
    assert cmd[4] == f"(New-Object Media.SoundPlayer '{quoted}').PlaySync()"
    curly = voice.player_command(Path("C:/x/o\u2019brien.wav"), which=lambda n: n)
    assert curly is not None and curly[0] == "afplay"   # afplay first when it exists
    curly = voice.player_command(Path("C:/x/o\u2019brien.wav"),
                                 which=lambda n: n if n == "powershell" else None)
    assert "o\u2019\u2019brien" in curly[4]             # typographic quotes end it too
    both = {"powershell", "ffplay"}                   # a POSIX player still wins
    assert voice.player_command(path, which=lambda n: n if n in both else None)[0] == "ffplay"


def test_silenced_by_environment(monkeypatch):
    monkeypatch.setenv("BRAINSYNC_TTS", "off")
    assert voice.default_speaker() is None
    voice.speak("nothing happens")                    # no speaker, no error
    voice.prefetch(["nothing"])
