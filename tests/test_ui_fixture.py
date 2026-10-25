"""tools/ui_fixture.py: the /ws/ui fixture the iPhone app's tests read (ios/BrainSyncKit) is current,
covers every message type, and every message in it is one the server sends or accepts."""

from __future__ import annotations

import json

from app.schemas import ServerToUI, UIToServer, parse_server_to_ui, parse_ui_to_server
from tools import ui_fixture
from tools.ui_fixture import FIXTURE, literal_values, message_type, union_models


def committed() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_committed_fixture_is_the_generated_one():
    assert FIXTURE.read_text(encoding="utf-8") == ui_fixture.render(), \
        "stale fixture: run python tools/ui_fixture.py"


def test_check_passes_on_the_committed_fixture_and_fails_on_a_stale_one(tmp_path):
    assert ui_fixture.main(["--check"]) == 0
    stale = tmp_path / "ui_messages.json"
    stale.write_text("{}\n")
    assert ui_fixture.main(["--check", "--out", str(stale)]) == 1
    assert ui_fixture.main(["--check", "--out", str(tmp_path / "missing.json")]) == 1
    assert ui_fixture.main(["--out", str(stale)]) == 0 and stale.read_text() == ui_fixture.render()


def test_every_server_to_ui_message_type_appears_and_validates():
    models = {message_type(m): m for m in union_models(ServerToUI)}
    msgs = committed()["server_to_ui"]
    assert {m["type"] for m in msgs} == set(models)
    for raw in msgs:
        msg = parse_server_to_ui(raw)
        assert isinstance(msg, models[raw["type"]])
        assert msg.model_dump(mode="json") == raw  # exactly as the server sends it


def test_every_ui_to_server_message_type_appears_and_parses():
    models = {message_type(m): m for m in union_models(UIToServer)}
    msgs = committed()["ui_to_server"]
    assert {m["type"] for m in msgs} == set(models)
    for raw in msgs:
        msg = parse_ui_to_server(raw)
        assert isinstance(msg, models[raw["type"]]) and msg.model_dump(mode="json") == raw


def test_every_variant_the_app_handles_is_there():
    by_type: dict[str, list[dict]] = {}
    for raw in committed()["server_to_ui"] + committed()["ui_to_server"]:
        by_type.setdefault(raw["type"], []).append(raw)
    assert {p["mode"] for p in by_type["show_page"]} == {"page", "confirm", "list"}
    listing = next(p for p in by_type["show_page"] if p["mode"] == "list")
    assert listing["options"] and listing["highlight_index"] is not None
    assert {s["audio_url"] is None for s in by_type["speak"]} == {True, False}
    listens = by_type["listen"]
    assert any(m["until_stopped"] for m in listens) and any(m["seconds"] > 0 for m in listens)
    assert any(not m["until_stopped"] and m["seconds"] == 0 for m in listens)  # stop
    assert {m["reason"] for m in listens} == set(literal_values(next(m for m in union_models(ServerToUI) if message_type(m) == "listen"), "reason"))
    trial_model = next(m for m in union_models(ServerToUI) if message_type(m) == "trial")
    assert {m["phase"] for m in by_type["trial"]} == set(literal_values(trial_model, "phase"))
    assert {m["who"] for m in by_type["transcript"]} == {"partner", "user"}
    assert {m["llm"] for m in by_type["status"]} == {"off", "waiting", "ok", "failed"}
    mic_model = next(m for m in union_models(UIToServer) if message_type(m) == "mic_report")
    assert {m["state"] for m in by_type["mic_report"]} == set(literal_values(mic_model, "state"))
    assert {m["source"] for m in by_type["partner_text"]} == {"stt", "typed"}
