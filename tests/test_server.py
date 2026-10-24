"""Server over real websockets (FastAPI TestClient) with very short timings."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.config import REPO_ROOT, load_config
from app.log import read_events
from app.server import create_app
from app.schemas import PROTOCOL, BaselineDone, DecoderHello, Selection, SlotSpec, to_json
from tests.test_menus import GOOD, FakeClaude, reply
from tests.test_tts import CLIP, KEY, MP3, FakeCartesia

BASE = load_config()
HELLO = DecoderHello(protocol=PROTOCOL, name="test", refresh_hz=60.0, waveform="sine",
                     slots=[SlotSpec(slot=s.slot, freq_hz=s.freq_hz) for s in BASE.stim.slots])


def fast_config():
    """The default config with every wait shortened so a full trial takes well under a second."""
    return BASE.model_copy(update={
        "stim": BASE.stim.model_copy(update={"cue_s": 0.05, "rest_s": 0.05, "max_trial_s": 2.0}),
        "baseline": BASE.baseline.model_copy(update={"seconds": 0.05, "timeout_s": 1.0}),
        "selection": BASE.selection.model_copy(update={"chosen_highlight_s": 0.05}),
        "speech": BASE.speech.model_copy(update={"max_speak_s": 2.0}),
    })


@pytest.fixture
def client(tmp_path):
    app = create_app(fast_config(), log_dir=tmp_path)
    with TestClient(app) as c:
        c.log_dir = tmp_path
        yield c


@pytest.fixture
def kb_client(tmp_path):
    """Keyboard mode: sessions run without a bridge (the baseline is just timed)."""
    app = create_app(fast_config(), keyboard_mode=True, log_dir=tmp_path)
    with TestClient(app) as c:
        c.log_dir = tmp_path
        yield c


def answer_baseline(dec) -> None:
    """Play the bridge: wait for the server's baseline and report it recorded."""
    baseline = recv_until(dec, "baseline")
    dec.send_text(to_json(BaselineDone(baseline_id=baseline["baseline_id"], ok=True)))


def recv_until(ws, kind: str, limit: int = 200) -> dict:
    """Read messages until one of type ``kind`` arrives; return it."""
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind} within {limit} messages")


def test_partner_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "BCI Voice" in r.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/api/config").json() == {
        "tts_rate": BASE.speech.tts_rate, "keyboard_mode": False,
        "stt_mode": "always", "listen_window_s": BASE.speech.listen_window_s,
        "stt_gap_s": BASE.speech.stt_gap_s, "stt_max_line_s": BASE.speech.stt_max_line_s,
        "stt_echo_s": BASE.speech.stt_echo_s}


def test_a_connecting_page_gets_the_mic_state(kb_client):
    with kb_client.websocket_connect("/ws/ui") as ui:
        listen = recv_until(ui, "listen")
    assert listen["reason"] == "reconnect" and listen["until_stopped"] is False  # idle: mic off


def test_decoder_hello_exchange(client):
    with client.websocket_connect("/ws/decoder") as dec:
        dec.send_text(to_json(HELLO))
        ack = recv_until(dec, "hello_ack")
    assert ack["roles"] == {"s1": "option", "s2": "option", "s3": "option", "s4": "next"}
    assert ack["labels"]["s4"] == "More options"
    assert set(ack["labels"]) == {"s1", "s2", "s3", "s4"}


def test_full_trial_from_page_and_decoder(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/decoder") as dec:
        recv_until(ui, "status")
        dec.send_text(to_json(HELLO))
        recv_until(dec, "hello_ack")
        ui.send_text(json.dumps({"type": "resume"}))
        answer_baseline(dec)
        shown = recv_until(ui, "show_page")
        assert shown["mode"] == "page"
        start = recv_until(dec, "stim_start")
        assert start["active_slots"] == ["s1", "s2", "s3", "s4"]
        dec.send_text(to_json(Selection(trial_id=start["trial_id"], slot="s1", t_unix=0.0)))
        assert recv_until(dec, "stim_stop")["reason"] == "selected"
        assert recv_until(ui, "highlight")["slot"] == "s1"
        # The confirm step: only Say it (s1) and Back (s4) flicker; nothing is spoken before it.
        confirm = recv_until(ui, "show_page")
        assert confirm["mode"] == "confirm" and confirm["text"] == shown["slots"][0]["utterance"]
        start = recv_until(dec, "stim_start")
        assert start["active_slots"] == ["s1", "s4"]
        dec.send_text(to_json(Selection(trial_id=start["trial_id"], slot="s1", t_unix=0.0)))
        speak = recv_until(ui, "speak")
        assert speak["text"] == shown["slots"][0]["utterance"]
        ui.send_text(json.dumps({"type": "tts_done", "utterance_id": speak["utterance_id"]}))
        assert recv_until(ui, "trial")["phase"] in ("rest", "cue")
    events = read_events(next(client.log_dir.glob("*.jsonl")))
    names = {e["event"] for e in events}
    assert {"server_start", "from_decoder", "to_decoder", "from_ui", "to_ui", "selected"} <= names


def test_without_a_key_the_browser_speaks_and_audio_is_404(client):
    assert not client.app.state.tts.enabled
    assert client.get("/audio/" + "0" * 32).status_code == 404


def test_cartesia_clip_is_prefetched_spoken_and_served(tmp_path):
    fake = FakeCartesia()
    app = create_app(fast_config(), log_dir=tmp_path, cartesia_api_key=KEY, tts_transport=fake.transport)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/decoder") as dec:
            dec.send_text(to_json(HELLO))
            recv_until(dec, "hello_ack")
            ui.send_text(json.dumps({"type": "resume"}))
            answer_baseline(dec)
            shown = recv_until(ui, "show_page")
            for slot in ("s1", "s1"):  # pick option 1, then Say it
                start = recv_until(dec, "stim_start")
                dec.send_text(to_json(Selection(trial_id=start["trial_id"], slot=slot, t_unix=0.0)))
            speak = recv_until(ui, "speak")
        assert speak["text"] == shown["slots"][0]["utterance"]
        assert speak["audio_url"] and speak["audio_url"].startswith("/audio/")
        r = client.get(speak["audio_url"])
        assert r.status_code == 200 and r.headers["content-type"] == MP3 and r.content == CLIP
    # The first page's 3 options were requested first, once each (NEXT needs no audio); the
    # other fixed sentences are pre-generated behind them.
    asked = [json.loads(q.content)["transcript"] for q in fake.requests]
    options = [s["utterance"] for s in shown["slots"] if s["role"] == "option"]
    assert sorted(asked[:3]) == sorted(options) and len(asked) == len(set(asked))
    assert set(asked) <= set(app.state.intents.utterances)
    assert KEY not in next(tmp_path.glob("*.jsonl")).read_text()


def test_typed_partner_question_gets_claudes_page(tmp_path):
    fake = FakeClaude(reply(GOOD))
    app = create_app(fast_config(), keyboard_mode=True, log_dir=tmp_path, llm_create=fake)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/ui") as ui:
            ui.send_text(json.dumps({"type": "resume"}))
            recv_until(ui, "show_page")  # the fixed home page first
            ui.send_text(json.dumps({"type": "partner_text", "text": "Do you want to go outside?",
                                     "source": "typed"}))
            page = status = None
            for _ in range(200):  # the llm_ok status comes just before the new page
                msg = json.loads(ui.receive_text())
                if msg["type"] == "status" and msg["llm_ok"]:
                    status = msg
                if msg["type"] == "show_page" and msg["slots"][0]["utterance"] == GOOD["option_1"]["utterance"]:
                    page = msg
                if page and status:
                    break
    assert [s["utterance"] for s in page["slots"] if s["role"] == "option"] == [
        GOOD[k]["utterance"] for k in ("option_1", "option_2", "option_3")]
    assert status["llm_ok"]
    assert "Do you want to go outside?" in fake.calls[0]["messages"][0]["content"]
    events = read_events(next(tmp_path.glob("*.jsonl")))
    assert any(e["event"] == "llm_ok" for e in events)


def test_without_a_key_pages_are_fixed_and_llm_is_off(client):
    assert not client.app.state.menus.enabled
    assert client.app.state.orch.request_page is None


def test_invalid_messages_are_logged_not_fatal(client):
    with client.websocket_connect("/ws/decoder") as dec:
        dec.send_text("not json")
        dec.send_text(json.dumps({"type": "selection", "trial_id": "x"}))
        dec.send_text(to_json(HELLO))
        assert recv_until(dec, "hello_ack")["type"] == "hello_ack"
    events = read_events(next(client.log_dir.glob("*.jsonl")))
    assert sum(e["event"] == "invalid_from_decoder" for e in events) == 2


def test_rejected_hello_gets_error_and_shows_why(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/decoder") as dec:
        recv_until(ui, "status")
        dec.send_text(to_json(HELLO.model_copy(update={"refresh_hz": 120.0})))
        assert "ProMotion" in recv_until(dec, "error")["message"]
        while not (status := recv_until(ui, "status"))["decoder_message"]:
            pass
        assert status["decoder_message"].startswith("Rejected:")
        assert not status["decoder_ok"] and not status["resume_ready"]


def test_new_partner_page_replaces_the_old_one(kb_client):
    client = kb_client
    with client.websocket_connect("/ws/ui") as first:
        recv_until(first, "status")
        with client.websocket_connect("/ws/ui") as second:
            recv_until(second, "status")
            assert client.app.state.hub.ui is not None
            second.send_text(json.dumps({"type": "resume"}))
            assert recv_until(second, "show_page")["page"] == 1
    events = read_events(next(client.log_dir.glob("*.jsonl")))
    assert any(e["event"] == "ui_replaced" for e in events)


def test_page_reconnect_after_stop_stays_paused(kb_client):
    """A reconnecting partner page gets the state again; it never restarts the flicker."""
    client = kb_client
    with client.websocket_connect("/ws/ui") as ui:
        ui.send_text(json.dumps({"type": "resume"}))
        recv_until(ui, "show_page")
        ui.send_text(json.dumps({"type": "stop"}))
        while recv_until(ui, "state")["name"] != "PAUSED":
            pass
    with client.websocket_connect("/ws/ui") as ui:
        assert recv_until(ui, "state")["name"] == "PAUSED"
    assert client.app.state.orch.state.value == "PAUSED"
    # The page side of the same rule: app.js must not send resume when the socket (re)opens.
    app_js = (REPO_ROOT / "ui" / "app.js").read_text()
    assert re.search(r"onopen\s*=[^;]*resume", app_js) is None


def test_decoder_disconnect_pauses(client):
    with client.websocket_connect("/ws/ui") as ui:
        with client.websocket_connect("/ws/decoder") as dec:
            dec.send_text(to_json(HELLO))
            recv_until(dec, "hello_ack")
            ui.send_text(json.dumps({"type": "resume"}))
            answer_baseline(dec)
            recv_until(dec, "stim_start")
        while True:
            state = recv_until(ui, "state")
            if state["name"] == "PAUSED":
                break
        assert state["detail"] == "Decoder disconnected"


def test_a_replacing_bridge_pauses_the_trial_then_resumes_after_its_hello(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/decoder") as old:
        old.send_text(to_json(HELLO))
        recv_until(old, "hello_ack")
        ui.send_text(json.dumps({"type": "resume"}))
        answer_baseline(old)
        recv_until(old, "stim_start")
        with client.websocket_connect("/ws/decoder") as new:
            while (state := recv_until(ui, "state"))["name"] != "PAUSED":
                pass
            assert state["detail"] == "Decoder disconnected"
            new.send_text(to_json(HELLO))
            recv_until(new, "hello_ack")
            answer_baseline(new)  # the new bridge gets a fresh baseline before any trial
            assert recv_until(new, "stim_start")["trial_id"] == 2


# ---------------------------------------------------------------------------
# The profile page's API (/api/profile)
# ---------------------------------------------------------------------------

ANA = {"name": "Ana", "about": "Ana lives in Fresno.", "likes": ["salsa music"],
       "personality": {"formality": "formal", "length": "medium"}}


@pytest.fixture
def profile_client(tmp_path):
    fake = FakeClaude(reply(GOOD))
    path = tmp_path / "profiles" / "user.yaml"
    app = create_app(fast_config(), keyboard_mode=True, log_dir=tmp_path, llm_create=fake,
                     profile_path=path)
    with TestClient(app) as c:
        c.profile_path, c.fake, c.log_dir = path, fake, tmp_path
        yield c


def test_profile_starts_as_the_demo_persona(profile_client):
    state = profile_client.get("/api/profile").json()
    assert state["source"] == "demo" and state["editable"] and state["llm"]
    assert state["profile"]["name"] == "Sam Rivera"


def test_saving_a_profile_writes_it_and_uses_it_for_the_next_page(profile_client):
    state = profile_client.put("/api/profile", json=ANA).json()
    assert state["source"] == "user" and state["profile"]["name"] == "Ana"
    assert state["profile"]["fictional"] is False
    assert "name: Ana" in profile_client.profile_path.read_text(encoding="utf-8")
    assert "for Ana," in profile_client.app.state.menus.system
    # Only which fields changed is logged, never what they say.
    log_text = next(profile_client.log_dir.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "profile_saved" in log_text and "Fresno" not in log_text and "salsa" not in log_text


def test_a_saved_profile_is_loaded_on_the_next_start(profile_client, tmp_path):
    profile_client.put("/api/profile", json=ANA)
    app = create_app(fast_config(), log_dir=tmp_path, profile_path=profile_client.profile_path)
    assert app.state.profile_source == "user" and app.state.menus.profile.name == "Ana"


def test_an_invalid_profile_is_rejected_with_the_problems(profile_client):
    r = profile_client.put("/api/profile", json={"name": "", "personality": {"humour": "sarcastic"}})
    assert r.status_code == 422
    problems = r.json()["detail"]["problems"]
    assert any(p.startswith("name") for p in problems)
    assert any("humour" in p for p in problems)
    assert not profile_client.profile_path.exists()


def test_reset_goes_back_to_the_demo_persona(profile_client):
    profile_client.put("/api/profile", json=ANA)
    state = profile_client.delete("/api/profile").json()
    assert state["source"] == "demo" and state["profile"]["name"] == "Sam Rivera"
    assert not profile_client.profile_path.exists()
    assert "for Sam," in profile_client.app.state.menus.system


def test_preview_writes_replies_with_the_unsaved_profile(profile_client):
    r = profile_client.post("/api/profile/preview",
                            json={"profile": ANA, "partner_text": "How was your weekend?"})
    assert r.status_code == 200 and len(r.json()["options"]) == 3
    assert "for Ana," in profile_client.fake.calls[-1]["system"]
    assert profile_client.get("/api/profile").json()["source"] == "demo"  # nothing saved


def test_an_invalid_saved_profile_falls_back_to_the_demo_persona(tmp_path):
    path = tmp_path / "user.yaml"
    path.write_text("name: ''\nabout: secret detail\n", encoding="utf-8")
    app = create_app(fast_config(), log_dir=tmp_path, profile_path=path)
    assert app.state.profile_source == "demo" and app.state.menus.profile.name == "Sam Rivera"
    log_text = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "profile_invalid" in log_text and "secret detail" not in log_text


def test_without_a_profile_path_the_profile_is_read_only(client):
    assert client.get("/api/profile").json()["editable"] is False
    assert client.put("/api/profile", json=ANA).status_code == 403
    assert client.delete("/api/profile").status_code == 403
    assert client.post("/api/profile/preview", json={"profile": ANA, "partner_text": "Hi"}).status_code == 409


def test_profile_page_is_served(client):
    r = client.get("/profile.html")
    assert r.status_code == 200 and "How they talk" in r.text
    assert client.get("/profile.js").status_code == 200
    assert 'href="/profile.html"' in client.get("/").text


# -- /api/health (tools/preflight.py) ------------------------------------------------------


def test_health_without_keys_bridge_or_page(client):
    h = client.get("/api/health").json()
    assert h["state"] == "IDLE"
    assert h["decoder"] == {"connected": False, "ok": False, "display_ok": False, "board_ok": False,
                            "message": "", "keyboard_mode": False, "fake_decoder": False,
                            "name": "", "fake_flag": False}
    assert h["partner_page"] == {"connected": False, "mic": "", "mic_muted": False,
                                 "stt_mode": BASE.speech.stt_mode}
    assert h["llm"]["enabled"] is False and "ANTHROPIC_API_KEY" in h["llm"]["why_disabled"]
    assert h["llm"]["model"] == BASE.llm.model and h["llm"]["warm_ok"] is None
    assert h["llm"]["dynamic"] is BASE.menus.dynamic
    assert h["tts"]["enabled"] is False and "CARTESIA_API_KEY" in h["tts"]["why_disabled"]
    assert h["tts"]["model"] == BASE.speech.model


def test_health_follows_the_bridge_page_and_mic(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/decoder") as dec:
        recv_until(ui, "status")
        dec.send_text(to_json(HELLO))
        recv_until(dec, "hello_ack")
        ui.send_text(json.dumps({"type": "mic_report", "state": "listening"}))
        while not recv_until(ui, "status")["mic"]:
            pass
        h = client.get("/api/health").json()
        assert h["decoder"]["connected"] and h["decoder"]["ok"] and h["decoder"]["display_ok"]
        assert (h["decoder"]["name"], h["decoder"]["fake_decoder"]) == ("test", False)
        assert h["partner_page"]["connected"] and h["partner_page"]["mic"] == "listening"
        assert h["resume_ready"]
    # The page is gone: its last mic report no longer counts.
    h = client.get("/api/health").json()
    assert h["partner_page"] == {**h["partner_page"], "connected": False, "mic": ""}


@pytest.mark.parametrize("flag, name, fake", [(False, "fake-decoder", True),
                                              (True, "ssvep-fbcca", True),
                                              (False, "ssvep-fbcca", False)])
def test_health_counts_a_hand_started_fake_decoder_as_fake(tmp_path, flag, name, fake):
    with TestClient(create_app(fast_config(), fake_decoder=flag, log_dir=tmp_path)) as client:
        with client.websocket_connect("/ws/decoder") as dec:
            dec.send_text(to_json(HELLO.model_copy(update={"name": name})))
            recv_until(dec, "hello_ack")
            d = client.get("/api/health").json()["decoder"]
            assert (d["fake_decoder"], d["name"], d["fake_flag"]) == (fake, name, flag)
        # The decoder is gone: only the flag is left.
        d = client.get("/api/health").json()["decoder"]
        assert (d["fake_decoder"], d["name"]) == (flag, "")


def test_health_with_keys_carries_no_secrets_or_conversation(tmp_path):
    app = create_app(fast_config(), keyboard_mode=True, log_dir=tmp_path, cartesia_api_key=KEY,
                     tts_transport=FakeCartesia().transport, llm_create=FakeClaude(reply(GOOD)))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/ui") as ui:
            ui.send_text(json.dumps({"type": "resume"}))
            ui.send_text(json.dumps({"type": "partner_text", "text": "Do you want to go outside?",
                                     "source": "typed"}))
            recv_until(ui, "show_page")
            r = client.get("/api/health")
    h = r.json()
    assert h["llm"]["enabled"] and h["tts"]["enabled"] and h["decoder"]["keyboard_mode"]
    assert KEY not in r.text and "outside" not in r.text
