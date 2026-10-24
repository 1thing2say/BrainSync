"""Tests for app/schemas.py: round trips, the v2 draft's examples, validation, the handshake check
and validate_option."""

from __future__ import annotations

import json
import re

import pytest
from pydantic import BaseModel, ValidationError

from app import schemas as s
from app.config import REPO_ROOT

SLOTS = [s.SlotSpec(slot="s1", freq_hz=15.0), s.SlotSpec(slot="s2", freq_hz=17.0)]


def hello(**changes) -> s.DecoderHello:
    fields = dict(protocol=2, name="ssvep-fbcca", refresh_hz=59.94, waveform="sine", slots=SLOTS)
    fields.update(changes)
    return s.DecoderHello(**fields)


# (message, parse helper) for every message type in every direction.
ROUND_TRIP_CASES: list[tuple[BaseModel, object]] = [
    # decoder -> server
    (hello(), s.parse_decoder_to_server),
    (
        hello(decoder=s.DecoderParams(window_s=4.0, step_s=0.5, votes=3), score_kind="fbcca_minus_baseline"),
        s.parse_decoder_to_server,
    ),
    (s.StimOnset(trial_id=4, t_unix=12.25), s.parse_decoder_to_server),
    (s.Selection(trial_id=1, slot="s2", t_unix=1792809206.53), s.parse_decoder_to_server),
    (
        s.Selection(trial_id=1, slot="s1", t_unix=1.5, score=0.041, scores={"s1": 0.041, "s2": 0.008}),
        s.parse_decoder_to_server,
    ),
    (s.NoSelection(trial_id=3, reason="low_signal"), s.parse_decoder_to_server),
    (s.BaselineDone(baseline_id=3, ok=False, message="O1 off"), s.parse_decoder_to_server),
    (s.DecoderStatus(ok=False, board_ok=True, display_ok=False, message="Esc"), s.parse_decoder_to_server),
    # server -> decoder
    (
        s.HelloAck(protocol=2, roles={"s1": "say", "s2": "next"}, labels={"s1": "SAY", "s2": "NEXT"}),
        s.parse_server_to_decoder,
    ),
    (s.ProtocolError(message="display refresh is 120 Hz"), s.parse_server_to_decoder),
    (s.Baseline(baseline_id=1, seconds=10.0), s.parse_server_to_decoder),
    (s.Display(text="I'm thirsty", labels={"s1": "SAY", "s2": "NEXT"}, highlight=None), s.parse_server_to_decoder),
    (s.Display(highlight="s1"), s.parse_server_to_decoder),
    (s.Display(), s.parse_server_to_decoder),
    (s.StimStart(trial_id=5, active_slots=["s1", "s2"]), s.parse_server_to_decoder),
    (s.StimStop(trial_id=5, reason="no_selection"), s.parse_server_to_decoder),
    (s.Pause(reason="stop_button"), s.parse_server_to_decoder),
    # server -> browser
    (
        s.ShowPage(
            list_id="l1",
            page=1,
            mode="page",
            slots=[
                s.UISlot(slot="s1", role="option", label="Go outside", utterance="Yes, let's go outside."),
                s.UISlot(slot="s2", role="option", label="Not now", utterance="Not right now."),
                s.UISlot(slot="s3", role="option", label="After lunch", utterance="Maybe after lunch."),
                s.UISlot(slot="s4", role="next", label="More options"),
            ],
        ),
        s.parse_server_to_ui,
    ),
    (
        s.ShowPage(
            list_id="l1",
            page=1,
            mode="confirm",
            text="Yes, let's go outside.",
            slots=[s.UISlot(slot="s1", role="say", label="Say it"), s.UISlot(slot="s4", role="back", label="Back")],
        ),
        s.parse_server_to_ui,
    ),
    (
        s.ShowPage(
            list_id="l1",
            page=2,
            mode="list",
            text="Not right now.",
            options=[s.Option(label="Go outside", utterance="Yes.", intent_id="yes"), s.Option(label="Not now", utterance="Not right now.", intent_id="no")],
            highlight_index=1,
            slots=[s.UISlot(slot="s1", role="say", label="SAY"), s.UISlot(slot="s2", role="next", label="NEXT")],
        ),
        s.parse_server_to_ui,
    ),
    (s.Trial(trial_id=7, phase="flicker"), s.parse_server_to_ui),
    (s.Highlight(slot="s2"), s.parse_server_to_ui),
    (s.Speak(text="Yes, let's go outside.", utterance_id="u1"), s.parse_server_to_ui),
    (s.Listen(seconds=10.0, delay_s=0.5, reason="turn"), s.parse_server_to_ui),
    (s.Listen(seconds=0.0, reason="speaking"), s.parse_server_to_ui),
    (s.State(name="MENU_CUE"), s.parse_server_to_ui),
    (s.Transcript(who="partner", text="Do you want to go outside?"), s.parse_server_to_ui),
    (
        s.UIStatus(
            decoder_connected=True,
            decoder_ok=True,
            display_ok=False,
            llm_ok=True,
            fake_decoder=True,
            keyboard_mode=False,
        ),
        s.parse_server_to_ui,
    ),
    (
        s.UIStatus(
            decoder_connected=True,
            decoder_ok=True,
            display_ok=True,
            llm_ok=False,
            fake_decoder=True,
            keyboard_mode=False,
            decoder_name="fake-decoder",
        ),
        s.parse_server_to_ui,
    ),
    # browser -> server
    (s.TTSDone(utterance_id="u1"), s.parse_ui_to_server),
    (s.PartnerText(text="Hungry?", source="typed"), s.parse_ui_to_server),
    (s.Key(key="1"), s.parse_ui_to_server),
    (s.Stop(), s.parse_ui_to_server),
    (s.Resume(), s.parse_ui_to_server),
    (s.Judgment(utterance_id="u1", correct=True), s.parse_ui_to_server),
]


@pytest.mark.parametrize(("msg", "parse"), ROUND_TRIP_CASES, ids=lambda v: getattr(v, "type", ""))
def test_round_trip_json_and_dict(msg: BaseModel, parse) -> None:
    text = s.to_json(msg)
    assert parse(text) == msg
    assert parse(json.loads(text)) == msg
    assert parse(text.encode()) == msg


def test_to_json_is_compact() -> None:
    assert " " not in s.to_json(s.Key(key="1"))


def test_display_sends_only_the_fields_that_were_set() -> None:
    assert json.loads(s.to_json(s.Display(highlight=None))) == {"type": "display", "highlight": None}
    assert json.loads(s.to_json(s.Display(text="Hi"))) == {"type": "display", "text": "Hi"}
    assert json.loads(s.to_json(s.Display())) == {"type": "display"}
    # A parsed display keeps exactly the fields it arrived with.
    raw = '{"type": "display", "labels": {"s1": "SAY", "s2": "MORE"}}'
    assert json.loads(s.to_json(s.parse_server_to_decoder(raw))) == json.loads(raw)


# Every JSON example in the v2 draft, read from the document itself so the tests follow it.
DRAFT = (REPO_ROOT / "INTERFACE-v2-draft.md").read_text()
DRAFT_EXAMPLES = re.findall(r"```json\n(.*?)```", DRAFT, re.S)
DECODER_TYPES = {"hello", "stim_onset", "selection", "no_selection", "baseline_done", "status"}


def test_draft_has_all_its_examples() -> None:
    types = [json.loads(raw)["type"] for raw in DRAFT_EXAMPLES]
    assert len(types) == 13  # two display examples: a page and the confirm screen
    assert set(types) == DECODER_TYPES | {
        "hello_ack", "baseline", "display", "stim_start", "stim_stop", "pause"
    }


@pytest.mark.parametrize("raw", DRAFT_EXAMPLES, ids=lambda raw: json.loads(raw)["type"])
def test_draft_examples_parse(raw: str) -> None:
    expected = json.loads(raw)
    direction = expected["type"] in DECODER_TYPES
    msg = (s.parse_decoder_to_server if direction else s.parse_server_to_decoder)(raw)
    # Every field in the example survives the parse with the same value.
    assert msg.model_dump(mode="json", include=set(expected)) == expected


def test_inline_error_example_parses() -> None:
    raw = re.search(r'`(\{"type": "error"[^`]*)`', DRAFT).group(1)
    assert isinstance(s.parse_server_to_decoder(raw), s.ProtocolError)


def test_selection_without_scores() -> None:
    sel = s.parse_decoder_to_server({"type": "selection", "trial_id": 42, "slot": "s2", "t_unix": 1.0})
    assert sel.score is None and sel.scores is None


def test_unknown_fields_are_ignored() -> None:
    sel = s.parse_decoder_to_server(
        {"type": "selection", "trial_id": 1, "slot": "s1", "t_unix": 1.0, "extra": [1, 2]}
    )
    assert isinstance(sel, s.Selection)
    assert "extra" not in sel.model_dump()
    slot = s.SlotSpec.model_validate({"slot": "s1", "freq_hz": 15.0, "color": "white"})
    assert slot == s.SlotSpec(slot="s1", freq_hz=15.0)
    assert isinstance(s.parse_ui_to_server({"type": "stop", "why": "x"}), s.Stop)


def test_status_resolves_per_direction() -> None:
    bridge = {"type": "status", "ok": True, "board_ok": True, "display_ok": True}
    assert isinstance(s.parse_decoder_to_server(bridge), s.DecoderStatus)
    ui_status = {
        "type": "status",
        "decoder_connected": False,
        "decoder_ok": False,
        "display_ok": False,
        "llm_ok": False,
        "fake_decoder": False,
        "keyboard_mode": True,
    }
    assert isinstance(s.parse_server_to_ui(ui_status), s.UIStatus)
    assert s.parse_server_to_ui(ui_status).decoder_name == ""  # optional: "" before an ack


V2_HELLO = {"type": "hello", "protocol": 2, "name": "x", "refresh_hz": 60.0, "waveform": "sine",
            "slots": [{"slot": "s1", "freq_hz": 15.0}]}

INVALID_CASES: list[tuple[object, object]] = [
    # bad slot ids
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": 1, "slot": "x1", "t_unix": 1.0}),
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": 1, "slot": "s", "t_unix": 1.0}),
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": 1, "slot": "S1", "t_unix": 1.0}),
    (
        s.parse_decoder_to_server,
        {"type": "selection", "trial_id": 1, "slot": "s1", "t_unix": 1.0, "scores": {"bad": 0.1}},
    ),
    (s.parse_server_to_ui, {"type": "highlight", "slot": "left"}),
    (s.parse_server_to_decoder, {"type": "display", "labels": {"left": "SAY"}}),
    (s.parse_server_to_decoder, {"type": "hello_ack", "protocol": 2, "roles": {"s1": "talk"}, "labels": {}}),
    # wrong types
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": "abc", "slot": "s1", "t_unix": 1.0}),
    (s.parse_decoder_to_server, {"type": "status", "ok": "maybe", "board_ok": True, "display_ok": True}),
    (s.parse_ui_to_server, {"type": "judgment", "utterance_id": "u1", "correct": "dunno"}),
    (s.parse_server_to_decoder, {"type": "display", "labels": ["SAY", "NEXT"]}),  # labels are per slot in v2
    # missing required fields
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": 1, "slot": "s1"}),
    (s.parse_decoder_to_server, {"type": "hello", "protocol": 2, "name": "v1-style hello"}),
    (s.parse_decoder_to_server, {"type": "status", "ok": True}),  # v1 status: no board_ok/display_ok
    (s.parse_decoder_to_server, {"type": "baseline_done", "ok": True}),
    (s.parse_server_to_decoder, {"type": "stim_stop", "trial_id": 1}),
    (s.parse_server_to_decoder, {"type": "baseline", "seconds": 10.0}),
    (s.parse_server_to_decoder, {"type": "pause"}),
    # empty lists
    (s.parse_server_to_decoder, {"type": "stim_start", "trial_id": 1, "active_slots": []}),
    (s.parse_decoder_to_server, {**V2_HELLO, "slots": []}),
    # bad enum values
    (s.parse_server_to_decoder, {"type": "stim_stop", "trial_id": 1, "reason": "bored"}),
    (s.parse_server_to_decoder, {"type": "stim_stop", "trial_id": 1, "reason": "calibration_done"}),
    (s.parse_decoder_to_server, {**V2_HELLO, "waveform": "sampled_sinusoid"}),
    (s.parse_ui_to_server, {"type": "partner_text", "text": "hi", "source": "telepathy"}),
    # constraints
    (s.parse_server_to_decoder, {"type": "baseline", "baseline_id": 1, "seconds": 0}),
    (s.parse_decoder_to_server, {**V2_HELLO, "slots": [{"slot": "s1", "freq_hz": 0}]}),
    (s.parse_decoder_to_server, {**V2_HELLO, "decoder": {"window_s": 4.0, "step_s": 0.5, "votes": 0}}),
    (s.parse_ui_to_server, {"type": "partner_text", "text": "", "source": "typed"}),
    (s.parse_ui_to_server, {"type": "partner_text", "text": "   ", "source": "stt"}),
    # NaN / Infinity floats (JSON cannot carry them; they would serialize as null)
    (s.parse_decoder_to_server, '{"type":"selection","trial_id":1,"slot":"s1","t_unix":NaN}'),
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": 1, "slot": "s1", "t_unix": float("inf")}),
    (
        s.parse_decoder_to_server,
        {"type": "selection", "trial_id": 1, "slot": "s1", "t_unix": 1.0, "scores": {"s1": float("nan")}},
    ),
    (s.parse_decoder_to_server, {**V2_HELLO, "refresh_hz": float("inf")}),
    (s.parse_decoder_to_server, '{"type":"stim_onset","trial_id":1,"t_unix":Infinity}'),
    (s.parse_server_to_decoder, {"type": "baseline", "baseline_id": 1, "seconds": float("inf")}),
    # show_page: highlight_index must point at an option, slots must not be empty, page starts at 1
    (
        s.parse_server_to_ui,
        {
            "type": "show_page", "list_id": "l1", "page": 1, "mode": "list", "highlight_index": 1,
            "options": [{"label": "a", "utterance": "b", "intent_id": "i"}], "slots": [{"slot": "s1", "role": "say", "label": "SAY"}],
        },
    ),
    (
        s.parse_server_to_ui,
        {
            "type": "show_page", "list_id": "l1", "page": 1, "mode": "page", "highlight_index": 0,
            "slots": [{"slot": "s1", "role": "option", "label": "a"}],
        },
    ),
    (s.parse_server_to_ui, {"type": "show_page", "list_id": "l1", "page": 1, "mode": "page", "slots": []}),
    (
        s.parse_server_to_ui,
        {"type": "show_page", "list_id": "l1", "page": 0, "mode": "page", "slots": [{"slot": "s1", "role": "next", "label": "x"}]},
    ),
    (
        s.parse_server_to_ui,
        {"type": "show_page", "list_id": "l1", "page": 1, "mode": "grid", "slots": [{"slot": "s1", "role": "next", "label": "x"}]},
    ),
    # wrong direction / unknown type / no type
    (s.parse_ui_to_server, {"type": "speak", "text": "hi", "utterance_id": "u1"}),
    (s.parse_decoder_to_server, {"type": "stim_start", "trial_id": 1, "active_slots": ["s1"]}),
    (s.parse_server_to_decoder, {"type": "baseline_done", "baseline_id": 1, "ok": True}),
    (s.parse_server_to_decoder, V2_HELLO),
    (s.parse_decoder_to_server, {"type": "nope"}),
    (s.parse_decoder_to_server, {"trial_id": 1}),
    # not JSON
    (s.parse_decoder_to_server, "{not json"),
]


@pytest.mark.parametrize(("parse", "raw"), INVALID_CASES)
def test_invalid_messages_raise(parse, raw) -> None:
    with pytest.raises(ValidationError):
        parse(raw)


def test_partner_text_is_stripped() -> None:
    msg = s.parse_ui_to_server({"type": "partner_text", "text": "  hi there ", "source": "stt"})
    assert msg.text == "hi there"


def test_protocol_constants() -> None:
    assert s.PROTOCOL == 2
    assert not hasattr(s, "PROTOCOL_V1")
    assert not hasattr(s, "ServerHello")


# ---------------------------------------------------------------------------
# hello_problems: the server's handshake check (draft section 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expect"),
    [
        ({}, []),
        ({"refresh_hz": 60.9}, []),  # within 1 Hz
        ({"slots": [s.SlotSpec(slot="s1", freq_hz=15.009), s.SlotSpec(slot="s2", freq_hz=16.991)]}, []),
        ({"slots": [s.SlotSpec(slot="s2", freq_hz=17.0), s.SlotSpec(slot="s1", freq_hz=15.0)]}, []),  # order
        ({"protocol": 1}, ["protocol 1"]),
        ({"refresh_hz": 120.0}, ["refresh is 120 Hz, expected 60 Hz (turn ProMotion"]),
        ({"refresh_hz": 58.9}, ["refresh is 58.9 Hz"]),
        ({"waveform": "square"}, ["waveform square"]),
        ({"slots": [s.SlotSpec(slot="s1", freq_hz=15.0)]}, ["slots ['s1']"]),
        ({"slots": [*SLOTS, s.SlotSpec(slot="s3", freq_hz=12.0)]}, ["slots ['s1', 's2', 's3']"]),
        ({"slots": [s.SlotSpec(slot="s1", freq_hz=15.0), s.SlotSpec(slot="s2", freq_hz=12.0)]}, ["s2 is 12 Hz"]),
        ({"slots": [s.SlotSpec(slot="s1", freq_hz=15.02), s.SlotSpec(slot="s2", freq_hz=17.0)]}, ["s1 is 15.02 Hz"]),
        ({"slots": [SLOTS[0], SLOTS[0], SLOTS[1]]}, ["duplicate slot ids"]),
    ],
)
def test_hello_problems(changes: dict, expect: list[str]) -> None:
    problems = s.hello_problems(hello(**changes), SLOTS, "sine", 60.0)
    assert len(problems) == len(expect), problems
    for problem, fragment in zip(problems, expect):
        assert fragment in problem


def test_hello_problems_reports_everything_at_once() -> None:
    bad = hello(protocol=1, refresh_hz=120.0, waveform="square")
    assert len(s.hello_problems(bad, SLOTS, "sine", 60.0)) == 3


@pytest.mark.parametrize(
    ("label", "utterance", "intent_id", "expected"),
    [
        ("Water", "Could I have some water, please?", "drink", []),
        ("Go outside", "Yes, I'd love to go outside.", "yes", []),
        ("", "Hello there.", "greet", ["label is empty"]),
        ("Hi", "   ", "greet", ["utterance is empty"]),
        ("Hi", "Hello.", "", ["intent_id is empty"]),
        ("Extraordinarily-long", "Hello.", "x", ["label over 18 chars (20)"]),
        ("one two three four", "Hello.", "x", ["label over 3 words (4)"]),
        (
            "Story",
            "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen",
            "x",
            ["utterance over 14 words (15)"],
        ),
        (
            "Fourteen",
            "one two three four five six seven eight nine ten eleven twelve thirteen fourteen",
            "x",
            [],
        ),
        ("exactly eighteen c", "Hi.", "x", []),
    ],
)
def test_validate_option(label: str, utterance: str, intent_id: str, expected: list[str]) -> None:
    opt = s.Option(label=label, utterance=utterance, intent_id=intent_id)
    assert s.validate_option(
        opt, label_max_chars=18, utterance_max_words=14, label_max_words=3
    ) == expected


def test_validate_option_uses_given_limits() -> None:
    opt = s.Option(label="Water", utterance="I want water now.", intent_id="drink")
    problems = s.validate_option(opt, label_max_chars=3, utterance_max_words=2, label_max_words=0)
    assert problems == [
        "label over 3 chars (5)", "label over 0 words (1)", "utterance over 2 words (4)"
    ]


def test_option_has_no_baked_in_limits() -> None:
    long = s.Option(label="x" * 200, utterance=" ".join(["w"] * 100), intent_id="i")
    assert len(long.label) == 200


@pytest.mark.parametrize("raw", [
    {"type": "mic_set", "muted": True},
    {"type": "mic_report", "state": "offline", "restarts": 2},
])
def test_mic_messages_round_trip(raw):
    msg = s.parse_ui_to_server(raw)
    assert s.parse_ui_to_server(s.to_json(msg)) == msg


@pytest.mark.parametrize("parse, raw", [
    (s.parse_server_to_ui, {"type": "listen", "seconds": 5, "until_stopped": True, "reason": "start"}),
    (s.parse_ui_to_server, {"type": "mic_report", "state": "dancing"}),
    (s.parse_ui_to_server, {"type": "mic_report", "state": "listening", "text": "secret words"}),
], ids=["until_stopped with seconds", "unknown mic state", "mic report never carries text"])
def test_invalid_mic_messages(parse, raw):
    if "text" in raw:  # extra fields are ignored by design; the model simply has no text field
        assert not hasattr(parse(raw), "text")
        return
    with pytest.raises(ValidationError):
        parse(raw)
