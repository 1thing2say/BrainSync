"""tools/log_summary.py: statistics from small hand-written event lists, plus a real log if any."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.log import DEFAULT_LOG_DIR, EventLog, read_events
from tools.log_summary import (collect, find_logs, format_report, format_texts, main, merge,
                               percentile, redact, summarize)

OPTS = ["Yes, let's go outside.", "Not right now.", "Maybe after lunch."]
OTHER = ["I'm doing well, thanks for asking.", "Could you help me with something, please?",
         "Could I have something to drink, please?"]


# -- event builders (the shapes app/orchestrator.py, menus.py and tts.py log) -----------------


def ev(t: float, event: str, **fields) -> dict:
    return {"t_unix": t, "event": event, **fields}


def ui(t: float, type_: str, state: str = "MENU_CUE", **msg) -> dict:
    return ev(t, "to_ui", state=state, message={"type": type_, **msg})


def from_ui(t: float, type_: str, state: str = "MENU_CUE", **msg) -> dict:
    return ev(t, "from_ui", state=state, message={"type": type_, **msg})


def page(t: float, utterances: list[str], list_id: str = "m1") -> dict:
    slots = [{"slot": f"s{i}", "role": "option", "label": u[:5], "utterance": u}
             for i, u in enumerate(utterances, 1)]
    slots.append({"slot": "s4", "role": "next", "label": "More options", "utterance": ""})
    return ui(t, "show_page", list_id=list_id, page=1, mode="page", slots=slots)


def confirm(t: float, text: str, list_id: str = "m1") -> dict:
    return ui(t, "show_page", list_id=list_id, page=1, mode="confirm", text=text,
              slots=[{"slot": "s1", "role": "say", "label": "Say it"},
                     {"slot": "s4", "role": "back", "label": "Back"}])


def trial(t: float, trial_id: int, state: str = "STIMULATING", event: str = "to_decoder") -> dict:
    return ev(t, event, state=state,
              message={"type": "stim_start", "trial_id": trial_id, "active_slots": ["s1", "s4"]})


def stop(t: float, trial_id: int, reason: str) -> dict:
    return ev(t, "to_decoder", state="STIMULATING",
              message={"type": "stim_stop", "trial_id": trial_id, "reason": reason})


def selected(t: float, trial_id: int, role: str, source: str = "decoder") -> dict:
    return ev(t, "selected", state="STIMULATING", trial_id=trial_id, slot="s1", role=role,
              source=source, screen="page")


def speak(t: float, uid: str, text: str, audio: bool = True) -> dict:
    return ui(t, "speak", state="SPEAKING", text=text, utterance_id=uid,
              audio_url=f"/audio/{uid}" if audio else None)


def pick_and_say(t: float, trial_id: int, text: str, uid: str, rank: int | None,
                 list_: str = "llm", audio: bool = True) -> list[dict]:
    """Page trial -> option -> confirm trial -> Say it -> spoken -> speak -> tts_done (6.5 s)."""
    return [trial(t, trial_id), selected(t + 2, trial_id, "option"), stop(t + 2, trial_id, "selected"),
            confirm(t + 3, text), trial(t + 4, trial_id + 1, "CONFIRMING"),
            selected(t + 6, trial_id + 1, "say"), stop(t + 6, trial_id + 1, "selected"),
            ev(t + 6.5, "spoken", state="SELECTED", utterance_id=uid, text=text, list=list_, rank=rank),
            speak(t + 6.5, uid, text, audio),
            from_ui(t + 8, "tts_done", state="SPEAKING", utterance_id=uid)]


def llm_ok(t: float, rid: int, options: list[str], ms: int = 1500) -> dict:
    return ev(t, "llm_ok", request_id=rid, attempt=1, ms=ms, options=options,
              labels_shortened=[], input_tokens=2000, output_tokens=150)


# -- helpers -------------------------------------------------------------------------------------


@pytest.mark.parametrize("values, q, expected", [
    ([], 50, None),
    ([5.0], 95, 5.0),
    ([1.0, 2.0, 3.0, 4.0], 50, 2.5),
    ([3.0, 1.0, 2.0], 50, 2.0),  # unsorted input
    ([float(i) for i in range(1, 21)], 95, 19.05),
    ([0.0, 10.0], 100, 10.0),
])
def test_percentile(values, q, expected):
    assert percentile(values, q) == (pytest.approx(expected) if expected is not None else None)


def test_redact_hides_keys():
    assert redact("key sk-ant-api03-abc_DEF-1 and sk_car_XYZ9 ok") == "key <redacted> and <redacted> ok"


# -- one session -------------------------------------------------------------------------------


def test_an_empty_log_summarizes_to_zeros():
    summary = summarize(collect([]))
    assert summary["trials"]["started"] == 0 and summary["replies"]["spoken"] == 0
    assert summary["llm"]["fallback_rate"] is None and summary["warnings"] == []
    assert "no trials, replies or Claude pages" in format_report("empty", summary)


def test_replies_time_selections_rank_list_and_audio():
    events = [
        ev(0, "server_start", keyboard_mode=False, fake_decoder=False),
        from_ui(1, "hello", state="IDLE"),  # unknown from_ui types are ignored
        ui(1, "state", state="BASELINE", name="BASELINE", detail=""),
        from_ui(10, "partner_text", state="BASELINE", text="Do you want to go outside?", source="typed"),
        ev(10, "llm_request", request_id=1, reason="partner", partner_last="..."),
        llm_ok(11.5, 1, OPTS),
        page(12, OPTS),
        *pick_and_say(13, 1, OPTS[0], "u1", rank=1),  # spoken at 19.5, done at 21
        # No partner line: the second reply counts from the end of the first (21 s).
        page(22, OTHER, list_id="m2"),
        trial(23, 3), selected(25, 3, "next"), stop(25, 3, "selected"),
        *pick_and_say(27, 4, OTHER[1], "u2", rank=2, list_="home", audio=False),  # spoken at 33.5
        ev(34, "judgment", state="REST", utterance_id="u2", correct=True),
    ]
    summary = summarize(collect(events))
    rp = summary["replies"]
    assert rp["spoken"] == 2 and rp["with_audio"] == 1
    assert rp["seconds"]["n"] == 2
    assert rp["seconds"]["median"] == pytest.approx((9.5 + 12.5) / 2)  # 19.5-10 and 33.5-21
    assert rp["mean_selections"] == pytest.approx((2 + 3) / 2)
    assert rp["mean_trials"] == pytest.approx((2 + 3) / 2)
    assert (rp["option1"], rp["ranked"], rp["option1_rate"]) == (1, 2, 0.5)
    assert rp["ranks"] == {"1": 1, "2": 1} and rp["lists"] == {"llm": 1, "home": 1}
    tr = summary["trials"]
    assert tr["started"] == 5 and tr["confirm"] == 2 and tr["selections"] == 5
    assert tr["by_role"] == {"option": 2, "say": 2, "next": 1} and tr["pick_rate"] == 1.0
    assert summary["marks"] == {"correct": 1, "wrong": 0, "correct_rate": 1.0}
    assert summary["session"]["pages"] == 2 and summary["session"]["screens"] == 4


def test_the_first_reply_counts_from_the_first_page_after_a_baseline():
    events = [ui(0, "state", state="BASELINE", name="BASELINE", detail=""),
              page(10, OPTS), *pick_and_say(11, 1, OPTS[0], "u1", rank=1)]
    assert summarize(collect(events))["replies"]["seconds"]["median"] == pytest.approx(7.5)


def test_a_pause_does_not_count_toward_the_reply():
    events = [from_ui(5, "partner_text", text="Tea?", source="typed"),
              ui(6, "state", state="PAUSED", name="PAUSED", detail="Stopped"),
              ui(50, "state", state="BASELINE", name="BASELINE", detail=""),
              page(60, OPTS), *pick_and_say(61, 1, OPTS[0], "u1", rank=1)]
    assert summarize(collect(events))["replies"]["seconds"]["median"] == pytest.approx(7.5)


@pytest.mark.parametrize("stopped, partner_t, partner_state, expected", [
    ("PAUSED", 20, "PAUSED", 7.5),  # said while paused: from the first page after the baseline
    ("IDLE", 20, "IDLE", 7.5),  # said before the flicker warning was accepted: the same
    ("IDLE", 55, "BASELINE", 12.5),  # said during the baseline: the line starts the clock
])
def test_a_partner_line_while_the_session_is_not_running(stopped, partner_t, partner_state, expected):
    events = sorted([ui(6, "state", state=stopped, name=stopped, detail=""),
                     from_ui(partner_t, "partner_text", state=partner_state, text="Tea?", source="typed"),
                     ui(50, "state", state="BASELINE", name="BASELINE", detail=""),
                     page(60, OPTS), *pick_and_say(61, 1, OPTS[0], "u1", rank=1)],
                    key=lambda e: e["t_unix"])  # stable: equal times keep their order
    assert summarize(collect(events))["replies"]["seconds"]["median"] == pytest.approx(expected)


def test_a_speak_without_a_time_is_no_anchor():
    events = [{"event": "to_ui", "message": {"type": "speak", "text": OPTS[0], "utterance_id": "u1"}},
              speak(110, "u2", OPTS[1])]  # no tts_done for u1: it must not count from t = 0
    rp = summarize(collect(events))["replies"]
    assert rp["spoken"] == 2 and rp["seconds"]["n"] == 0


def test_an_event_named_like_a_message_type_is_not_a_message():
    events = [ev(1, "speak", text=OPTS[0], utterance_id="u1"), ev(2, "show_page", mode="page")]
    summary = summarize(collect(events))
    assert summary["replies"]["spoken"] == 0 and summary["session"]["screens"] == 0


def test_option_1_is_also_counted_on_claudes_pages_alone():
    events = [llm_ok(1, 1, OPTS), page(2, OPTS),
              *pick_and_say(3, 1, OPTS[1], "u1", rank=2),
              page(12, OTHER, "m2"), *pick_and_say(13, 3, OTHER[0], "u2", rank=1, list_="home")]
    rp = summarize(collect(events))["replies"]
    assert (rp["option1"], rp["ranked"]) == (1, 2)
    assert (rp["option1_llm"], rp["ranked_llm"], rp["option1_llm_rate"]) == (0, 1, 0.0)
    assert "(on Claude's pages 0/1 (0 %))" in format_report("s", summarize(collect(events)))


def test_rank_and_list_are_found_on_the_page_in_logs_without_a_spoken_event():
    events = [ev(1, "llm_ok", request_id=1, attempt=1, ms=900, options=OPTS),
              page(2, OPTS), trial(3, 1), selected(5, 1, "say"),
              speak(6, "u1", OPTS[2]),  # an old log: no spoken event
              page(8, OTHER, "m2"), speak(9, "u2", OTHER[0]),
              speak(10, "u3", "Something typed that was never on a page.")]
    rp = summarize(collect(events))["replies"]
    assert rp["ranks"] == {"3": 1, "1": 1} and rp["ranked"] == 2
    assert rp["lists"] == {"llm": 1, "fallback": 2}


def test_a_muted_stt_line_does_not_start_the_reply_clock():
    events = [from_ui(1, "partner_text", text="Hello", source="typed"),
              from_ui(5, "partner_text", text="noise", source="stt"),
              ev(5, "stt_ignored_muted", state="MENU_CUE"),
              speak(7, "u1", OPTS[0])]
    summary = summarize(collect(events))
    assert summary["replies"]["seconds"]["median"] == pytest.approx(6.0)
    assert summary["partner"]["ignored_muted"] == 1


@pytest.mark.parametrize("events, outcome", [
    # shown: the page with these options reached the screen
    ([ev(0, "llm_request", request_id=1, reason="partner"), llm_ok(2, 1, OPTS), page(3, OPTS)],
     "shown"),
    # retried after a connection error, then shown
    ([ev(0, "llm_request", request_id=1, reason="more"),
      ev(1, "llm_failed", request_id=1, reason="APIConnectionError", status=None, attempt=1, ms=900),
      llm_ok(2.5, 1, OPTS), page(3, OPTS)], "shown"),
    # timed out: menus' deadline and the orchestrator giving up
    ([ev(0, "llm_request", request_id=1, reason="partner"),
      ev(4, "llm_failed", request_id=1, reason="timeout", attempt=1, ms=4000),
      ev(4, "llm_page_timeout", state="PREPARING", request_id=1), page(4, OTHER)], "timed out"),
    # the orchestrator gave up, and Claude's page came later anyway
    ([ev(0, "llm_request", request_id=1, reason="partner"),
      ev(4, "llm_page_timeout", state="PREPARING", request_id=1), page(4, OTHER),
      llm_ok(5, 1, OPTS), ev(5, "llm_page_ignored", state="MENU_CUE", request_id=1, why="late"),
      page(9, OPTS)], "timed out"),
    # a permanent error (old log: no ms)
    ([ev(0, "llm_request", request_id=1, reason="partner"),
      ev(1, "llm_failed", request_id=1, reason="AuthenticationError", status=401, attempt=1)],
     "failed"),
    # two invalid answers
    ([ev(0, "llm_request", request_id=1, reason="partner"),
      ev(1, "llm_invalid", request_id=1, attempt=1, ms=900, problems=["x"]),
      ev(2, "llm_invalid", request_id=1, attempt=2, ms=900, problems=["x"])], "invalid"),
    # answered, but the session ended before the next cue
    ([ev(0, "llm_request", request_id=1, reason="partner"), llm_ok(2, 1, OPTS)], "not shown"),
    # menus' own deadline while still awaited (the orchestrator logs no llm_page_timeout then)
    ([ev(0, "llm_request", request_id=1, reason="partner"),
      ev(4, "llm_failed", request_id=1, reason="timeout", attempt=1, ms=4000)], "timed out"),
])
def test_how_one_claude_request_ended(events, outcome):
    assert summarize(collect(events))["llm"]["outcomes"] == {outcome: 1}


def test_a_newer_partner_line_replaces_a_page_not_on_screen_yet():
    events = [ev(0, "llm_request", request_id=1, reason="partner"), llm_ok(2, 1, OPTS),
              ev(3, "llm_request", request_id=2, reason="partner"), llm_ok(4, 2, OTHER),
              page(5, OTHER, "m2"),
              ev(6, "llm_request", request_id=3, reason="partner"),
              llm_ok(9, 3, OPTS), ev(9, "llm_page_ignored", state="MENU_CUE", request_id=3, why="late"),
              ev(10, "llm_request", request_id=4, reason="partner")]
    llm = summarize(collect(events))["llm"]
    assert llm["outcomes"] == {"replaced": 2, "shown": 1, "no answer": 1}
    assert llm["fallback_rate"] == 0.0  # nothing replaced by a fixed list


def test_a_replaced_request_that_times_out_later_is_no_fallback():
    events = [ev(0, "llm_request", request_id=1, reason="partner"),
              ev(1, "llm_request", request_id=2, reason="partner"),  # a newer line
              llm_ok(2.5, 2, OPTS), page(3, OPTS),
              ev(4, "llm_failed", request_id=1, reason="timeout", attempt=1, ms=4000),
              ev(4, "llm_page_ignored", state="MENU_CUE", request_id=1, why="late failure")]
    llm = summarize(collect(events))["llm"]
    assert llm["outcomes"] == {"replaced": 1, "shown": 1} and llm["fallback_rate"] == 0.0


def test_printed_output_is_redacted(tmp_path, capsys):
    log = _write_log(tmp_path, 1_000_000.0, [
        ev(0, "from_decoder", state="IDLE", message={"type": "hello", "name": "bridge sk-ant-abc123"}),
        ev(1, "tts_disabled", reason="bad key sk_car_XYZ9")])
    for args in ([str(log)], ["--json", str(log)]):
        assert main(args) == 0
        out = capsys.readouterr().out
        assert "abc123" not in out and "XYZ9" not in out and "<redacted>" in out


def test_claude_latencies_fallback_rate_and_tokens():
    events = [ev(0, "llm_request", request_id=1, reason="partner"), llm_ok(2, 1, OPTS, ms=2000),
              page(3, OPTS),  # 3 s to the screen
              ev(10, "llm_request", request_id=2, reason="more"), llm_ok(11, 2, OTHER, ms=1000),
              page(15, OTHER, "m2"),  # waited for the trial: 5 s
              ev(20, "llm_request", request_id=3, reason="partner"),
              ev(24, "llm_failed", request_id=3, reason="timeout", attempt=1, ms=4000),
              ev(24, "llm_page_timeout", state="PREPARING", request_id=3)]
    llm = summarize(collect(events))["llm"]
    assert llm["ok_ms"]["median"] == 1500 and llm["ok_ms"]["n"] == 2
    assert llm["answer_s"]["n"] == 3 and llm["answer_s"]["median"] == pytest.approx(2.0)
    assert (llm["screen_s"]["median"], llm["screen_s"]["max"]) == (4.0, 5.0)  # 3, 5 and 4 s
    assert llm["fallback_rate"] == pytest.approx(1 / 3)
    assert llm["by_reason"] == {"partner": 2, "more": 1}
    assert (llm["tokens_in"], llm["tokens_out"]) == (4000, 300)
    assert llm["attempt_failures"] == {"timeout": 1}


def test_cartesia_counts():
    events = [ev(1, "tts_ok", audio_id="a", chars=10, bytes=1000, ms=400),
              ev(2, "tts_ok", audio_id="b", chars=10, bytes=1000, ms=600),
              ev(3, "tts_retry", audio_id="c", reason="timeout", status=None, ms=8000, attempt=1),
              ev(4, "tts_failed", audio_id="c", reason="bad_response", status=429),
              ev(5, "tts_timeout", state="SPEAKING", utterance_id="u1")]
    tts = summarize(collect(events))["tts"]
    assert tts["ok"] == 2 and tts["ms"]["median"] == 500 and tts["retries"] == 1
    assert tts["failed"] == {"bad_response (429)": 1} and tts["speak_timeouts"] == 1
    assert tts["failed_rate"] == pytest.approx(1 / 3)


def test_partner_input_and_mic_states():
    events = [from_ui(1, "partner_text", text="a", source="typed"),
              from_ui(2, "partner_text", state="STIMULATING", text="b", source="stt"),
              ev(2, "partner_text_queued", state="STIMULATING", trial_id=3, question=False),
              from_ui(3, "partner_text", state="STIMULATING", text="are you cold", source="stt"),
              stop(3, 3, "context_changed"),
              *(ev(t, "from_ui", state="MENU_CUE", message={"type": "mic_report", "state": mic})
                for t, mic in ((4, "listening"), (5, "offline"), (6, "listening")))]
    summary = summarize(collect(events))
    pt = summary["partner"]
    assert (pt["typed"], pt["stt"], pt["queued"], pt["interrupted_trials"]) == (1, 2, 1, 1)
    assert pt["mic_reports"] == {"listening": 2, "offline": 1}
    assert any("speech recognition reported offline" in w for w in summary["warnings"])


def test_older_log_shapes_do_not_crash():
    events = [
        ev(0, "server_start", keyboard_mode=False, fake_decoder=False),
        ev(0, "from_decoder", state="IDLE", message={"type": "hello", "protocol": 1,
                                                     "name": "fake-decoder"}),
        ev(0, "from_decoder", state="IDLE", message={"type": "status", "ok": True}),  # no board_ok
        ui(1, "show_option", list_id="m1", index=0, count=4, label="Yes", utterance=OPTS[0]),
        ui(2, "show_option", list_id="m1", index=1, count=4, label="No", utterance=OPTS[1]),
        trial(3, 1), ev(4, "selected", state="STIMULATING", trial_id=1, slot="s1", role="say",
                        source="decoder"),  # no screen field
        speak(5, "u1", OPTS[1]),
        ev(6, "llm_failed", request_id=9, reason="timeout", attempt=1),  # no request, no ms
        {"event": "not_caught"},  # no time, no fields
        "not a dict",
        ev(7, "to_ui", state="IDLE", message="not a dict"),
        ev(8, "invalid_from_ui", raw="{", error="bad"),
    ]
    summary = summarize(collect(events))
    assert summary["session"]["screen_modes"] == {"scan": 2}
    assert summary["replies"]["ranks"] == {"2": 1}
    assert summary["session"]["decoder_names"] == {"fake-decoder": 1}
    assert summary["health"]["invalid messages"] == 1
    format_report("old", summary)


# -- warnings ------------------------------------------------------------------------------------


def _not_caught_session(keyboard: bool, decoder: bool) -> list[dict]:
    events = [ev(0, "server_start", keyboard_mode=keyboard, fake_decoder=False)]
    if decoder:
        events.append(ev(0, "from_decoder", state="IDLE", message={"type": "hello", "protocol": 2,
                                                                   "name": "ssvep-fbcca"}))
    for i in range(10):
        events.append(trial(10 + i, i + 1, event="to_decoder" if decoder else "not_sent_before_ack"))
        if i < 2:
            events.append(selected(10.5 + i, i + 1, "next", "decoder" if decoder else "keyboard"))
        else:
            events.append(ev(10.9 + i, "not_caught", state="STIMULATING", trial_id=i + 1,
                             why="timeout" if keyboard else "no_selection", confirming=False))
    return events


@pytest.mark.parametrize("keyboard, decoder, expected, absent", [
    (False, True, "the decoder gave no answer on 8 of 10 trials", "no key pressed"),
    (True, False, "8 trials ended with no key pressed", "decoder gave no answer"),
])
def test_not_caught_warning_names_the_right_cause(keyboard, decoder, expected, absent):
    found = summarize(collect(_not_caught_session(keyboard, decoder)))["warnings"]
    assert any(expected in w for w in found) and not any(absent in w for w in found)


def test_a_local_backend_handing_over_to_claude_is_not_a_timed_out_page():
    # Request 1: local times out after 10 s, Claude answers 1.5 s later, the page is shown.
    # Request 2: local answers itself in 6 s. Request 3: both fail (the last one ends it).
    events = [ev(0, "llm_request", request_id=1, reason="partner"),
              ev(10, "llm_failed", request_id=1, backend="local", reason="timeout", attempt=1,
                 ms=10000, next="claude"),
              {**llm_ok(11.5, 1, OPTS, ms=1500), "backend": "claude", "page_ms": 11500},
              page(12, OPTS),
              ev(20, "llm_request", request_id=2, reason="more"),
              {**llm_ok(26, 2, OTHER, ms=6000), "backend": "local", "page_ms": 6000},
              page(26.5, OTHER, "m2"),
              ev(30, "llm_request", request_id=3, reason="partner"),
              ev(30, "llm_failed", request_id=3, backend="local", reason="APIConnectionError",
                 status=None, attempt=1, ms=2, next="claude"),
              ev(34, "llm_failed", request_id=3, backend="claude", reason="timeout", attempt=2,
                 ms=4000),
              ev(34, "llm_page_timeout", state="PREPARING", request_id=3)]
    summary = summarize(collect(events))
    llm = summary["llm"]
    assert llm["outcomes"] == {"shown": 2, "timed out": 1}
    assert llm["answer_s"]["n"] == 3 and llm["answer_s"]["max"] == pytest.approx(11.5)
    assert llm["answered_by"] == {"claude": 1, "local": 1}
    assert llm["ok_ms_by_backend"]["local"]["median"] == 6000
    assert llm["ok_ms_by_backend"]["claude"]["median"] == 1500
    assert llm["handovers"] == {"local -> claude": 2}
    assert llm["attempt_failures"] == {"local: timeout": 1, "local: APIConnectionError": 1,
                                       "claude: timeout": 1}
    text = format_report("s", summary)
    assert "answered by claude 1, local 1" in text and "handed over local -> claude 2" in text


def test_a_rejected_local_key_names_the_backend():
    events = [ev(0, "llm_request", request_id=1, reason="partner"),
              ev(1, "llm_failed", request_id=1, backend="local", reason="AuthenticationError",
                 status=401, attempt=1, ms=5, next="claude")]
    assert any(w.startswith("local rejected the request")
               for w in summarize(collect(events))["warnings"])


def test_a_total_does_not_blame_the_decoder_for_idle_keyboard_sessions():
    total = merge([collect(_not_caught_session(True, False)), collect(_not_caught_session(False, True))])
    found = summarize(total)["warnings"]
    assert any("8 trials ended with no key pressed" in w for w in found)
    assert any("the decoder gave no answer on 8 of 10 trials" in w for w in found)


@pytest.mark.parametrize("events, expected", [
    ([e for i in range(4) for e in (ev(i, "llm_request", request_id=i, reason="partner"),
                                     ev(i + 4, "llm_page_timeout", request_id=i))],
     "4 of 4 Claude pages timed out"),
    ([ev(0, "llm_request", request_id=1, reason="partner"),
      ev(1, "llm_failed", request_id=1, reason="AuthenticationError", status=401, attempt=1, ms=50)],
     "Claude rejected the request"),
    ([ev(0, "llm_warm_failed", ms=20000, reason="APITimeoutError", status=None)],
     "keep-alive failed 1 time"),
    ([ev(0, "tts_disabled", reason="CARTESIA_API_KEY is not set")], "Cartesia was off"),
    ([ev(0, "tts_failed", audio_id="a", reason="bad_response", status=429)], "HTTP 429"),
    ([ev(i, "tts_failed", audio_id="a", reason="timeout") for i in range(3)], "3 of 3 Cartesia clips"),
    ([ev(0, "judgment", utterance_id="u1", correct=False)] * 2
     + [ev(0, "judgment", utterance_id="u1", correct=True)], "2 of 3 spoken replies were marked wrong"),
    ([ev(0, "decoder_disconnected", state="STIMULATING")], "the bridge disconnected"),
    ([ev(0, "baseline_timeout", state="BASELINE", baseline_id=1)], "did not answer baseline"),
])
def test_warnings(events, expected):
    found = summarize(collect(events))["warnings"]
    assert any(expected in w for w in found), found


def test_unconfirmed_picks_and_missing_marks_with_a_real_decoder():
    events = [ev(0, "from_decoder", state="IDLE", message={"type": "hello", "name": "ssvep-fbcca"})]
    for i in range(4):
        events += [selected(i, 2 * i, "option"), ev(i + 0.5, "confirm_back", utterance="x")]
    events += pick_and_say(10, 20, OPTS[0], "u1", rank=1)
    found = summarize(collect(events))["warnings"]
    assert any("4 of 5 picks were not confirmed" in w for w in found)
    assert any("no C/W marks" in w for w in found)
    assert not any("no C/W marks" in w for w in summarize(collect(
        events + [ev(30, "judgment", utterance_id="u1", correct=True)]))["warnings"])


# -- several sessions, text and the CLI ------------------------------------------------------------


def test_merge_adds_counts_and_pools_samples():
    one = collect([ev(0, "llm_request", request_id=1, reason="partner"), llm_ok(1, 1, OPTS, ms=1000),
                   page(2, OPTS), *pick_and_say(3, 1, OPTS[0], "u1", rank=1)])
    two = collect([ev(100, "llm_request", request_id=1, reason="partner"), llm_ok(103, 1, OTHER, ms=3000),
                   page(104, OTHER), *pick_and_say(105, 1, OTHER[2], "u1", rank=3)])
    summary = summarize(merge([one, two]))
    assert summary["session"]["sessions"] == 2
    assert summary["replies"]["spoken"] == 2 and summary["replies"]["option1_rate"] == 0.5
    assert summary["llm"]["ok_ms"]["median"] == 2000 and summary["llm"]["requests"] == 2
    assert summary["trials"]["by_role"] == {"option": 2, "say": 2}
    assert one.replies and len(one.replies) == 1  # the parts are unchanged


def test_conversation_text_only_on_request():
    stats = collect([from_ui(1, "partner_text", text="Do you want tea?", source="typed"),
                     speak(3, "u1", "Yes please, sk-ant-secret123")])
    report = format_report("s", summarize(stats))
    assert "Do you want tea" not in report and "Yes please" not in report
    texts = format_texts(stats)
    assert "partner (typed): Do you want tea?" in texts
    assert "user: Yes please, <redacted>" in texts and "secret123" not in texts
    assert "texts" not in json.dumps(summarize(stats))


def _write_log(log_dir: Path, clock: float, events: list[dict]) -> Path:
    with EventLog(log_dir, clock=lambda: clock) as log:
        for e in events:
            fields = {k: v for k, v in e.items() if k not in ("t_unix", "event")}
            log.log(e["event"], **fields)
        return log.path


def test_find_logs_picks_the_newest_started_session(tmp_path):
    old = _write_log(tmp_path, 1_000_000.0, [ev(0, "server_start", keyboard_mode=True, fake_decoder=False)])
    new = _write_log(tmp_path, 2_000_000.0, [ev(0, "server_start", keyboard_mode=True, fake_decoder=False)])
    empty = tmp_path / "9999-01-01_000000_ffffff.jsonl"
    empty.write_text("")
    assert find_logs([], tmp_path, every=False) == [new]
    assert find_logs([], tmp_path, every=True) == [old, new, empty]
    assert find_logs([tmp_path], tmp_path, every=False) == [old, new, empty]


def test_main_prints_text_and_json(tmp_path, capsys):
    events = [ev(0, "server_start", keyboard_mode=True, fake_decoder=False),
              from_ui(1, "partner_text", text="Do you want to go outside?", source="typed"),
              ev(1, "llm_request", request_id=1, reason="partner"), llm_ok(2, 1, OPTS), page(3, OPTS),
              *pick_and_say(4, 1, OPTS[0], "u1", rank=1)]
    one = _write_log(tmp_path, 1_000_000.0, events)
    two = _write_log(tmp_path, 2_000_000.0, events)

    assert main(["--log-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert two.stem in out and one.stem not in out and "option 1: 1/1 (100 %)" in out
    assert "go outside" not in out

    assert main(["--all", "--json", "--show-text", "--log-dir", str(tmp_path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["sessions"]) == 2 and data["total"]["replies"]["spoken"] == 2
    assert data["sessions"][0]["texts"][0]["who"] == "partner (typed)"

    assert main(["--show-text", str(one)]) == 0
    assert "Conversation" in capsys.readouterr().out


def test_main_exit_codes(tmp_path, capsys):
    assert main(["--log-dir", str(tmp_path / "none")]) == 2
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"t_unix": 1, "event": "x"\n{"t_unix": 2, "event": "y"}\n')  # corrupt first line
    assert main([str(bad)]) == 1  # unreadable
    good = _write_log(tmp_path, 1_000_000.0, [ev(0, "server_start", keyboard_mode=False, fake_decoder=False)])
    assert main([str(bad), str(good)]) == 1
    assert "could not read" in capsys.readouterr().err


def test_every_real_log_summarizes():
    logs = sorted(DEFAULT_LOG_DIR.glob("*.jsonl"))
    if not logs:
        pytest.skip("no session logs in logs/")
    parts = [collect(read_events(p)) for p in logs]
    for path, stats in zip(logs, parts):
        format_report(path.stem, summarize(stats))
    total = summarize(merge(parts))
    assert total["session"]["sessions"] == len(logs)
    json.dumps(total)  # plain JSON
