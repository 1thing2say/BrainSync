"""The server in code mode (the default config, c-VEP) over real websockets: a code hello is
acked with three roles, the session trains instead of taking a baseline, decisions reach the page
as confidence, and an option is spoken only after its confirm trial on s1 / s3."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.server import create_app, screen_layout
from app.schemas import (
    PROTOCOL,
    CodeParams,
    Decision,
    DecoderHello,
    Selection,
    SlotSpec,
    StimOnset,
    Trained,
    TrainProgress,
    to_json,
)
from tests.test_server import recv_until

BASE = load_config()
HELLO = DecoderHello(protocol=PROTOCOL, name="brainsync-cvep", paradigm="code", refresh_hz=60.0,
                     code=CodeParams(length=63, cycle_frames=63),
                     slots=[SlotSpec(slot=s.slot, lag_frames=s.lag_frames) for s in BASE.stim.slots])


def fast_config():
    return BASE.model_copy(update={
        "stim": BASE.stim.model_copy(update={"cue_s": 0.05, "rest_s": 0.05, "max_trial_s": 2.0}),
        "selection": BASE.selection.model_copy(update={"chosen_highlight_s": 0.05}),
        "speech": BASE.speech.model_copy(update={"max_speak_s": 2.0}),
    })


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(fast_config(), log_dir=tmp_path)) as c:
        yield c


def test_the_page_config_says_code_and_its_three_slots(client):
    c = client.get("/api/config").json()
    assert (c["paradigm"], c["layout"], c["slots"]) == ("code", "cvep", ["s1", "s2", "s3"])


def test_screen_layouts():
    from app.config import FVEP_LIST_CONFIG_PATH, FVEP_PAGE_CONFIG_PATH

    assert screen_layout(BASE) == "cvep"
    assert screen_layout(load_config(FVEP_PAGE_CONFIG_PATH)) == "diamond"
    assert screen_layout(load_config(FVEP_LIST_CONFIG_PATH)) == "row"


def test_a_code_session_trains_shows_confidence_and_speaks_after_confirm(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/decoder") as dec:
        recv_until(ui, "status")
        dec.send_text(to_json(HELLO))
        ack = recv_until(dec, "hello_ack")
        assert ack["roles"] == {"s1": "option", "s2": "option", "s3": "next"}
        ui.send_text(json.dumps({"type": "resume"}))
        train = recv_until(dec, "train")
        assert (train["slot"], train["label"], train["cycles"], train["retrain"]) == ("s1", "left", 30, True)
        assert recv_until(ui, "trial")["phase"] == "train"
        assert recv_until(ui, "training")["cycle"] == 0
        dec.send_text(to_json(TrainProgress(train_id=train["train_id"], cycle=7, of=30)))
        assert recv_until(ui, "training")["cycle"] == 7
        dec.send_text(to_json(Trained(train_id=train["train_id"], ok=True, accuracy=0.8,
                                      chance=0.333, cycles=30)))
        assert recv_until(ui, "training")["accuracy"] == 0.8
        shown = recv_until(ui, "show_page")
        assert [s["slot"] for s in shown["slots"]] == ["s1", "s2", "s3"]
        start = recv_until(dec, "stim_start")
        assert start["active_slots"] == ["s1", "s2", "s3"]
        trial = start["trial_id"]
        dec.send_text(to_json(StimOnset(trial_id=trial, t_unix=0.0)))
        scores = {"s1": 0.1, "s2": 0.45, "s3": 0.0}
        dec.send_text(to_json(Decision(trial_id=trial, scores=scores, raw="s2", lead=0.35)))
        conf = recv_until(ui, "confidence")
        assert conf == {"type": "confidence", "trial_id": trial, "scores": scores, "raw": "s2",
                        "committed": None, "lead": 0.35}
        dec.send_text(to_json(Selection(trial_id=trial, slot="s2", t_unix=0.0, score=0.45,
                                        scores=scores)))
        confirm = recv_until(ui, "show_page")
        assert confirm["mode"] == "confirm" and confirm["text"] == shown["slots"][1]["utterance"]
        start = recv_until(dec, "stim_start")
        assert start["active_slots"] == ["s1", "s3"]
        dec.send_text(to_json(Selection(trial_id=start["trial_id"], slot="s1", t_unix=0.0)))
        speak = recv_until(ui, "speak")
        assert speak["text"] == shown["slots"][1]["utterance"]
