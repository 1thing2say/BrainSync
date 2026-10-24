"""Tests for app/schemas.py: round trips, INTERFACE.md examples, validation and validate_option."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from app import schemas as s

# (message, parse helper) for every message type in every direction.
ROUND_TRIP_CASES: list[tuple[BaseModel, object]] = [
    # decoder -> server
    (s.DecoderHello(protocol=1, name="ssvep-cca"), s.parse_decoder_to_server),
    (
        s.DecoderHello(
            protocol=2,
            name="ssvep-fbcca",
            refresh_hz=60.0,
            waveform="sine",
            slots=[s.SlotSpec(slot="s1", freq_hz=15.0), s.SlotSpec(slot="s2", freq_hz=17.0)],
            score_kind="fbcca_minus_baseline",
        ),
        s.parse_decoder_to_server,
    ),
    (s.Selection(trial_id=1, slot="s2", t_unix=1761268204.9), s.parse_decoder_to_server),
    (
        s.Selection(
            trial_id=1, slot="s1", t_unix=1.5, score=0.7, scores={"s1": 0.7, "s2": 0.2}
        ),
        s.parse_decoder_to_server,
    ),
    (s.NoSelection(trial_id=3, reason="low_signal"), s.parse_decoder_to_server),
    (s.DecoderStatus(ok=False, message="O1 off"), s.parse_decoder_to_server),
    (s.StimOnset(trial_id=4, t_unix=12.25), s.parse_decoder_to_server),
    # server -> decoder
    (
        s.ServerHello(
            protocol=1,
            refresh_hz=60.0,
            waveform="square",
            slots=[s.SlotSpec(slot="s1", freq_hz=15.0, phase_deg=90)],
        ),
        s.parse_server_to_decoder,
    ),
    (
        s.StimStart(trial_id=5, menu_id="m1", t_unix=2.0, active_slots=["s1", "s2"]),
        s.parse_server_to_decoder,
    ),
    (
        s.StimStart(
            trial_id=6,
            menu_id="m1",
            t_unix=2.0,
            active_slots=["s1"],
            cued_slot="s1",
            mode="calibrate",
        ),
        s.parse_server_to_decoder,
    ),
    (s.StimStop(trial_id=5, t_unix=3.0, reason="timeout"), s.parse_server_to_decoder),
    (s.StimStop(trial_id=5, t_unix=3.0, reason="no_selection"), s.parse_server_to_decoder),
    (s.Display(text="I'm thirsty", labels=["SAY", "NEXT"], highlight="s1"), s.parse_server_to_decoder),
    (s.Display(), s.parse_server_to_decoder),
    (s.Baseline(seconds=10.0, t_unix=5.0), s.parse_server_to_decoder),
    # server -> browser
    (
        s.ShowOption(
            list_id="l1",
            index=0,
            count=4,
            label="Water",
            utterance="Could I have some water, please?",
            slots=[
                s.UISlot(slot="s1", role="say", label="SAY"),
                s.UISlot(slot="s2", role="next", label="NEXT"),
            ],
        ),
        s.parse_server_to_ui,
    ),
    (s.Trial(trial_id=7, phase="flicker"), s.parse_server_to_ui),
    (s.Highlight(slot="s2"), s.parse_server_to_ui),
    (s.Speak(text="Yes, let's go outside.", utterance_id="u1"), s.parse_server_to_ui),
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


# INTERFACE.md examples, copied verbatim.
INTERFACE_EXAMPLES: list[tuple[str, object, type]] = [
    ('{"type": "hello", "protocol": 1, "name": "ssvep-cca"}', s.parse_decoder_to_server, s.DecoderHello),
    (
        """{"type": "hello", "protocol": 1,
 "refresh_hz": 60.0,
 "waveform": "square",
 "slots": [
   {"slot": "s1", "freq_hz": 15.0, "phase_deg": 0},
   {"slot": "s2", "freq_hz": 12.0, "phase_deg": 0},
   {"slot": "s3", "freq_hz": 10.0, "phase_deg": 0},
   {"slot": "s4", "freq_hz": 8.57, "phase_deg": 0}]}""",
        s.parse_server_to_decoder,
        s.ServerHello,
    ),
    (
        """{"type": "stim_start", "trial_id": 42, "menu_id": "m17",
 "t_unix": 1761268201.512,
 "active_slots": ["s1", "s2", "s3", "s4"],
 "cued_slot": null, "mode": "live"}""",
        s.parse_server_to_decoder,
        s.StimStart,
    ),
    (
        """{"type": "stim_stop", "trial_id": 42, "t_unix": 1761268204.930,
 "reason": "selected"}""",
        s.parse_server_to_decoder,
        s.StimStop,
    ),
    (
        '{"type": "selection", "trial_id": 42, "slot": "s2", "t_unix": 1761268204.900}',
        s.parse_decoder_to_server,
        s.Selection,
    ),
    (
        '{"type": "no_selection", "trial_id": 42, "reason": "low_signal"}',
        s.parse_decoder_to_server,
        s.NoSelection,
    ),
    (
        '{"type": "status", "ok": true, "message": "8/8 channels ok"}',
        s.parse_decoder_to_server,
        s.DecoderStatus,
    ),
]


@pytest.mark.parametrize(("raw", "parse", "cls"), INTERFACE_EXAMPLES, ids=lambda v: getattr(v, "__name__", ""))
def test_interface_examples_parse(raw: str, parse, cls: type) -> None:
    msg = parse(raw)
    assert isinstance(msg, cls)
    # Every field in the example survives the parse with the same value.
    expected = json.loads(raw)
    assert msg.model_dump(include=set(expected)) == expected


def test_interface_example_values() -> None:
    start = s.parse_server_to_decoder(INTERFACE_EXAMPLES[2][0])
    assert start.trial_id == 42
    assert start.active_slots == ["s1", "s2", "s3", "s4"]
    assert start.cued_slot is None
    hello = s.parse_server_to_decoder(INTERFACE_EXAMPLES[1][0])
    assert [sl.freq_hz for sl in hello.slots] == [15.0, 12.0, 10.0, 8.57]


def test_selection_without_scores() -> None:
    sel = s.parse_decoder_to_server(
        {"type": "selection", "trial_id": 42, "slot": "s2", "t_unix": 1761268204.9}
    )
    assert sel.score is None and sel.scores is None


def test_selection_with_scores_from_interface_example() -> None:
    raw = {
        "type": "selection",
        "trial_id": 42,
        "slot": "s2",
        "t_unix": 1761268204.9,
        "score": 0.71,
        "scores": {"s1": 0.31, "s2": 0.71, "s3": 0.22, "s4": 0.18},
    }
    sel = s.parse_decoder_to_server(raw)
    assert sel.score == 0.71
    assert sel.scores == {"s1": 0.31, "s2": 0.71, "s3": 0.22, "s4": 0.18}


def test_unknown_fields_are_ignored() -> None:
    sel = s.parse_decoder_to_server(
        {"type": "selection", "trial_id": 1, "slot": "s1", "t_unix": 1.0, "extra": [1, 2]}
    )
    assert isinstance(sel, s.Selection)
    assert "extra" not in sel.model_dump()
    slot = s.SlotSpec.model_validate({"slot": "s1", "freq_hz": 15.0, "color": "white"})
    assert slot == s.SlotSpec(slot="s1", freq_hz=15.0)
    assert isinstance(s.parse_ui_to_server({"type": "stop", "why": "x"}), s.Stop)


def test_hello_and_status_resolve_per_direction() -> None:
    assert isinstance(
        s.parse_decoder_to_server({"type": "hello", "protocol": 1, "name": "x"}), s.DecoderHello
    )
    assert isinstance(s.parse_decoder_to_server({"type": "status", "ok": True}), s.DecoderStatus)
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
    # wrong types
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": "abc", "slot": "s1", "t_unix": 1.0}),
    (s.parse_decoder_to_server, {"type": "status", "ok": "maybe"}),
    (s.parse_ui_to_server, {"type": "judgment", "utterance_id": "u1", "correct": "dunno"}),
    # missing required fields
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": 1, "slot": "s1"}),
    (s.parse_decoder_to_server, {"type": "hello", "protocol": 1}),
    (s.parse_server_to_decoder, {"type": "stim_stop", "trial_id": 1, "t_unix": 1.0}),
    (s.parse_server_to_decoder, {"type": "hello", "protocol": 1, "refresh_hz": 60.0, "waveform": "square"}),
    # empty active_slots
    (
        s.parse_server_to_decoder,
        {"type": "stim_start", "trial_id": 1, "menu_id": "m", "t_unix": 1.0, "active_slots": []},
    ),
    # bad enum values
    (s.parse_server_to_decoder, {"type": "stim_stop", "trial_id": 1, "t_unix": 1.0, "reason": "bored"}),
    (
        s.parse_server_to_decoder,
        {"type": "hello", "protocol": 1, "refresh_hz": 60.0, "waveform": "sampled_sinusoid", "slots": []},
    ),
    (
        s.parse_server_to_decoder,
        {"type": "stim_start", "trial_id": 1, "menu_id": "m", "t_unix": 1.0, "active_slots": ["s1"], "mode": "x"},
    ),
    (s.parse_ui_to_server, {"type": "partner_text", "text": "hi", "source": "telepathy"}),
    # constraints
    (s.parse_server_to_decoder, {"type": "baseline", "seconds": 0}),
    (
        s.parse_server_to_decoder,
        {"type": "hello", "protocol": 1, "refresh_hz": 60.0, "waveform": "sine", "slots": [{"slot": "s1", "freq_hz": 0}]},
    ),
    (s.parse_ui_to_server, {"type": "partner_text", "text": "", "source": "typed"}),
    (s.parse_ui_to_server, {"type": "partner_text", "text": "   ", "source": "stt"}),
    # NaN / Infinity floats (JSON cannot carry them; they would serialize as null)
    (s.parse_decoder_to_server, '{"type":"selection","trial_id":1,"slot":"s1","t_unix":NaN}'),
    (s.parse_decoder_to_server, {"type": "selection", "trial_id": 1, "slot": "s1", "t_unix": float("inf")}),
    (
        s.parse_decoder_to_server,
        {"type": "selection", "trial_id": 1, "slot": "s1", "t_unix": 1.0, "scores": {"s1": float("nan")}},
    ),
    (
        s.parse_server_to_decoder,
        '{"type":"hello","protocol":1,"refresh_hz":60.0,"waveform":"square","slots":[{"slot":"s1","freq_hz":Infinity}]}',
    ),
    (s.parse_server_to_decoder, {"type": "hello", "protocol": 1, "refresh_hz": float("inf"), "waveform": "square", "slots": []}),
    (s.parse_server_to_decoder, {"type": "baseline", "seconds": float("inf")}),
    # show_option: index must be < count, slots must not be empty
    (
        s.parse_server_to_ui,
        {
            "type": "show_option", "list_id": "l1", "index": 5, "count": 1, "label": "a", "utterance": "b",
            "slots": [{"slot": "s1", "role": "say", "label": "SAY"}],
        },
    ),
    (
        s.parse_server_to_ui,
        {
            "type": "show_option", "list_id": "l1", "index": 1, "count": 1, "label": "a", "utterance": "b",
            "slots": [{"slot": "s1", "role": "say", "label": "SAY"}],
        },
    ),
    (
        s.parse_server_to_ui,
        {"type": "show_option", "list_id": "l1", "index": 0, "count": 1, "label": "a", "utterance": "b", "slots": []},
    ),
    # wrong direction / unknown type / no type
    (s.parse_ui_to_server, {"type": "speak", "text": "hi", "utterance_id": "u1"}),
    (s.parse_decoder_to_server, {"type": "stim_start", "trial_id": 1, "menu_id": "m", "t_unix": 1.0, "active_slots": ["s1"]}),
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
    assert s.PROTOCOL_V1 == 1
    assert s.PROTOCOL_V2_PROPOSED == 2


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
