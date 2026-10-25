"""c-VEP on the phone (INTERFACE-v2-draft.md section 9), the server's part: clock_ping answered
at once on /ws/ui, the fallback lists from menus.intents, /api/config and /api/health for both
paradigms, the startup line, and the commands --bridge and --fake-decoder run for paradigm code."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import REPO_ROOT, load_config, load_intents
from app.log import read_events
from app.schemas import DisplayReport
from app.server import (
    CODE_STIMULUS,
    bridge_command,
    code_bits,
    create_app,
    fake_decoder_command,
    main,
    page_slots,
    phone_health,
    stimulus_line,
)
from brainsync.vep.codes import m_sequence
from tests.test_server import fast_config, recv_until

PHONE_PATH = REPO_ROOT / "config" / "phone_cvep.yaml"
PHONE = load_config(PHONE_PATH)
URL = "ws://127.0.0.1:8000/ws/decoder"
T_SERVER = 1_800_000_000.25  # the server's clock in the ping tests


def write_intents(path: Path, count: int) -> Path:
    """A fallback-list file with ``count`` phrases per list (config/intents_halves.yaml comes
    with the orchestrator's halves mode)."""
    words = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"][:count]
    lists = {name: [{"label": f"{w} {name}", "utterance": f"{w} for the {name} list.",
                     "intent_id": "need"} for w in words]
             for name in ("home", "yes_no", "more")}
    path.write_text(yaml.safe_dump({"intents": [{"id": "need", "description": "Ask for something"}],
                                    "fallback_lists": lists}), encoding="utf-8")
    return path


def phone_intents(tmp_path: Path):
    m = PHONE.menus
    return load_intents(write_intents(tmp_path / "halves.yaml", m.option_count),
                        option_count=m.option_count, label_max_chars=m.label_max_chars,
                        utterance_max_words=m.utterance_max_words, label_max_words=m.label_max_words)


# -- clock_ping / clock_pong ---------------------------------------------------------------


@pytest.fixture
def ping_client(tmp_path):
    """Keyboard mode with a fixed server clock; ``seen`` records what reaches the orchestrator."""
    app = create_app(fast_config(), keyboard_mode=True, log_dir=tmp_path, clock=lambda: T_SERVER)
    seen = []
    on_ui = app.state.orch.on_ui_message
    app.state.orch.on_ui_message = lambda msg: (seen.append(msg), on_ui(msg))
    with TestClient(app) as c:
        c.log_dir, c.seen = tmp_path, seen
        yield c


def test_clock_ping_is_answered_at_once_and_never_reaches_the_orchestrator(ping_client):
    with ping_client.websocket_connect("/ws/ui?client=ios") as ui:
        for k in range(30):
            ui.send_text(json.dumps({"type": "clock_ping", "id": k, "t_phone": 1000.5 + k}))
            assert recv_until(ui, "clock_pong") == {"type": "clock_pong", "id": k,
                                                    "t_phone": 1000.5 + k, "t_server": T_SERVER}
        # Everything else still goes to the orchestrator.
        ui.send_text(json.dumps({"type": "mic_report", "state": "listening"}))
        while not recv_until(ui, "status")["mic"]:
            pass
    assert [m.type for m in ping_client.seen] == ["mic_report"]
    events = read_events(next(ping_client.log_dir.glob("*.jsonl")))
    # Not one log line per ping or pong: one count when the page goes.
    clock_events = [e for e in events if "clock" in json.dumps(e)]
    assert [(e["event"], e["client"], e["count"]) for e in clock_events] == [
        ("clock_pings", "ios", 30)]


def test_an_invalid_clock_ping_is_logged_and_the_next_one_answered(ping_client):
    with ping_client.websocket_connect("/ws/ui") as ui:
        ui.send_text(json.dumps({"type": "clock_ping", "id": 1}))  # no t_phone
        ui.send_text(json.dumps({"type": "clock_ping", "id": 2, "t_phone": 5.0}))
        assert recv_until(ui, "clock_pong")["id"] == 2
    events = read_events(next(ping_client.log_dir.glob("*.jsonl")))
    assert any(e["event"] == "invalid_from_ui" for e in events)
    assert ping_client.seen == []


# -- fallback lists from menus.intents -------------------------------------------------------


def test_fallback_lists_come_from_menus_intents(tmp_path):
    path = write_intents(tmp_path / "three.yaml", 3)
    base = fast_config()
    cfg = base.model_copy(update={"menus": base.menus.model_copy(update={"intents": str(path)})})
    app = create_app(cfg, keyboard_mode=True, log_dir=tmp_path)
    assert app.state.intents.fallback_lists["home"][0].utterance == "Alpha for the home list."


def test_a_relative_intents_path_is_read_from_the_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # anywhere but the repo root
    app = create_app(fast_config(), keyboard_mode=True, log_dir=tmp_path)
    assert fast_config().menus.intents == "config/intents.yaml"
    assert app.state.intents == load_intents()


def test_intents_with_the_wrong_count_are_refused(tmp_path):
    from app.config import ConfigError

    path = write_intents(tmp_path / "three.yaml", 3)
    cfg = PHONE.model_copy(update={"menus": PHONE.menus.model_copy(update={"intents": str(path)})})
    with pytest.raises(ConfigError, match="needs exactly 6"):
        create_app(cfg, log_dir=tmp_path)


# -- /api/config and /api/health ------------------------------------------------------------


def test_page_config_for_c_vep_on_the_phone(tmp_path):
    with TestClient(create_app(PHONE, phone_intents(tmp_path), log_dir=tmp_path)) as c:
        conf = c.get("/api/config").json()
    # flicker: whether this browser page draws it; the phone does here.
    assert (conf["paradigm"], conf["display"], conf["flicker"]) == ("code", "phone", False)
    assert conf["slots"] == {"s1": {"lag_frames": 0}, "s2": {"lag_frames": 31}}
    assert (conf["waveform"], conf["refresh_hz"]) == ("square", 60)
    assert conf["max_trial_s"] == PHONE.stim.max_trial_s and conf["phone_url"] == ""


def test_page_slots_for_each_paradigm_and_display():
    base = load_config()
    assert page_slots(base) == {s.slot: {"freq_hz": s.freq_hz, "phase_deg": s.phase_deg}
                                for s in base.stim.slots}
    assert page_slots(PHONE) == {"s1": {"lag_frames": 0}, "s2": {"lag_frames": 31}}


def test_page_config_with_the_psychopy_window(tmp_path):
    base = fast_config()
    cfg = base.model_copy(update={"display": base.display.model_copy(update={"flicker": "psychopy"})})
    with TestClient(create_app(cfg, keyboard_mode=True, log_dir=tmp_path)) as c:
        conf = c.get("/api/config").json()
    assert (conf["paradigm"], conf["display"], conf["flicker"]) == ("freq", "psychopy", False)
    assert conf["slots"]["s1"] == {"freq_hz": 13.0, "phase_deg": 0}


def test_health_names_the_paradigm_and_who_draws(tmp_path):
    with TestClient(create_app(fast_config(), keyboard_mode=True, log_dir=tmp_path)) as c:
        h = c.get("/api/health").json()
    assert (h["paradigm"], h["display"]) == ("freq", "browser")
    assert h["phone"] == {"display": None, "trained": False, "calibration_accuracy": None}


def test_health_for_c_vep_on_the_phone(tmp_path):
    with TestClient(create_app(PHONE, phone_intents(tmp_path), log_dir=tmp_path)) as c:
        h = c.get("/api/health").json()
    assert (h["paradigm"], h["display"]) == ("code", "phone")
    assert h["phone"] == {"display": None, "trained": False, "calibration_accuracy": None}


def test_phone_health_reads_the_orchestrator():
    orch = SimpleNamespace(phone_display=DisplayReport(refresh_hz=59.98, ok=True, message="even"),
                           bridge_trained=True, calibration_accuracy=0.85)
    assert phone_health(orch) == {"display": {"refresh_hz": 59.98, "ok": True, "message": "even"},
                                  "trained": True, "calibration_accuracy": 0.85}
    # An orchestrator without the attributes (before c-VEP) reports nothing.
    assert phone_health(object()) == {"display": None, "trained": False,
                                      "calibration_accuracy": None}


# -- startup line ---------------------------------------------------------------------------


def test_stimulus_line_says_what_flickers_and_who_draws_it():
    assert stimulus_line(PHONE, PHONE_PATH) == (
        "stimulus: c-VEP (63-bit m-sequence, lags s1 0, s2 31 frames), drawn by the iPhone app "
        "(config/phone_cvep.yaml)")
    assert stimulus_line(load_config()) == (
        "stimulus: SSVEP 13/15/17/11 Hz sine, drawn by this computer's page in Chrome "
        "(config/default.yaml)")


# -- --bridge and --fake-decoder with paradigm code -----------------------------------------


def test_bridge_command_for_c_vep():
    head = [sys.executable, "-m", "brainsync.voice_bridge", "--url", URL, "--parent-pid", "42"]
    assert CODE_STIMULUS == "cvep_2"
    # The phone draws: no PsychoPy window, whatever --bridge-headless says.
    assert bridge_command("cerelog", URL, 42, paradigm="code") == head + [
        "--paradigm", "code", "-c", "cvep_2", "--no-stimulus"]
    assert bridge_command("fake", URL, 42, headless=True, paradigm="code",
                          template=Path("/tmp/me.npz")) == head + [
        "--fake", "--paradigm", "code", "-c", "cvep_2", "--no-stimulus", "--template", "/tmp/me.npz"]
    # Paradigm freq is as before.
    assert bridge_command("cerelog", URL, 42, paradigm="freq") == head
    with pytest.raises(ValueError):
        bridge_command("cerelog", URL, 42, paradigm="ssvep")


@pytest.mark.parametrize("argv, fragment", [
    (["--template", "t.npz"], "--template needs --bridge"),
    (["--bridge", "fake", "--template", "t.npz"], "stim.paradigm code"),
    (["--config", str(PHONE_PATH), "--bridge", "fake", "--template", "/no/such/template.npz"],
     "no file"),
])
def test_template_argument_errors_exit_2(argv, fragment, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert fragment in capsys.readouterr().err


def test_code_bits_are_one_period_of_the_m_sequence():
    bits = code_bits(63)
    assert bits == "".join(str(int(b)) for b in m_sequence(63)) and len(bits) == 63
    assert bits.count("1") == 32  # an m-sequence of 2^6 - 1 bits has 2^5 ones


def test_fake_decoder_command_for_c_vep():
    assert fake_decoder_command(PHONE, URL, 42) == [
        sys.executable, "-m", "tools.fake_decoder", "--random", "--parent-pid", "42", "--url", URL,
        "--paradigm", "code", "--lags", "s1=0,s2=31", "--code-bits", code_bits(63),
        "--frames-per-bit", "1", "--waveform", "square", "--refresh-hz", "60"]


def test_fake_decoder_command_for_ssvep_is_unchanged():
    assert fake_decoder_command(load_config(), URL, 42) == [
        sys.executable, "-m", "tools.fake_decoder", "--random", "--parent-pid", "42", "--url", URL,
        "--slots", "s1=13,s2=15,s3=17,s4=11", "--waveform", "sine", "--refresh-hz", "60"]
