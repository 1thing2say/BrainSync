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

BASE = load_config()
HELLO = DecoderHello(protocol=PROTOCOL, name="test", refresh_hz=60.0, waveform="sine",
                     slots=[SlotSpec(slot="s1", freq_hz=15.0), SlotSpec(slot="s2", freq_hz=17.0)])


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
    assert client.get("/api/config").json() == {"tts_rate": BASE.speech.tts_rate, "keyboard_mode": False}


def test_decoder_hello_exchange(client):
    with client.websocket_connect("/ws/decoder") as dec:
        dec.send_text(to_json(HELLO))
        ack = recv_until(dec, "hello_ack")
    assert ack["roles"] == {"s1": "say", "s2": "next"}
    assert ack["labels"] == {"s1": "SAY", "s2": "NEXT"}


def test_full_trial_from_page_and_decoder(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/decoder") as dec:
        recv_until(ui, "status")
        dec.send_text(to_json(HELLO))
        recv_until(dec, "hello_ack")
        ui.send_text(json.dumps({"type": "resume"}))
        answer_baseline(dec)
        shown = recv_until(ui, "show_option")
        start = recv_until(dec, "stim_start")
        dec.send_text(to_json(Selection(trial_id=start["trial_id"], slot="s1", t_unix=0.0)))
        assert recv_until(dec, "stim_stop")["reason"] == "selected"
        assert recv_until(ui, "highlight")["slot"] == "s1"
        speak = recv_until(ui, "speak")
        assert speak["text"] == shown["utterance"]
        ui.send_text(json.dumps({"type": "tts_done", "utterance_id": speak["utterance_id"]}))
        assert recv_until(ui, "trial")["phase"] in ("rest", "cue")
    events = read_events(next(client.log_dir.glob("*.jsonl")))
    names = {e["event"] for e in events}
    assert {"server_start", "from_decoder", "to_decoder", "from_ui", "to_ui", "selected"} <= names


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
            assert recv_until(second, "show_option")["index"] == 0
    events = read_events(next(client.log_dir.glob("*.jsonl")))
    assert any(e["event"] == "ui_replaced" for e in events)


def test_page_reconnect_after_stop_stays_paused(kb_client):
    """A reconnecting partner page gets the state again; it never restarts the flicker."""
    client = kb_client
    with client.websocket_connect("/ws/ui") as ui:
        ui.send_text(json.dumps({"type": "resume"}))
        recv_until(ui, "show_option")
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
