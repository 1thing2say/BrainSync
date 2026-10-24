"""Message schemas: the INTERFACE.md decoder contract plus the internal browser <-> server messages.

Every message is one JSON object with a ``type`` field. Unknown fields are ignored (INTERFACE.md
section 1), so every model uses ``extra="ignore"``. NaN and Infinity are rejected in float fields:
JSON has no way to write them, so they would turn into null and break the round trip.

Sections:
- INTERFACE.md v1 (agreed): decoder <-> server.
- Protocol 2 proposals (PLAN.md section 13 item 5, NOT agreed yet): opt-in extras.
- Internal browser <-> server messages (PLAN.md section 6).
- Shared menu ``Option`` model and its config-driven validator.
- Discriminated unions and ``parse_*`` / ``to_json`` helpers, one union per direction
  ("hello" and "status" exist in more than one direction).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

PROTOCOL_V1 = 1
PROTOCOL_V2_PROPOSED = 2

SlotId = Annotated[str, StringConstraints(pattern=r"^s[0-9]+$")]
Waveform = Literal["sine", "square"]


class Message(BaseModel):
    """Base for all messages: unknown fields are ignored, NaN/Infinity floats are rejected."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class SlotSpec(Message):
    """One stimulus target: slot id, flicker frequency and phase."""

    slot: SlotId
    freq_hz: float = Field(gt=0)
    phase_deg: float = 0


# ---------------------------------------------------------------------------
# INTERFACE.md v1: decoder -> server
# ---------------------------------------------------------------------------


class DecoderHello(Message):
    """Sent by the decoder right after connecting.

    ``refresh_hz``, ``waveform``, ``slots`` and ``score_kind`` are protocol 2 proposals (the decoder
    owns the display and announces its stimulus config). They are optional, so v1 hellos still parse.
    """

    type: Literal["hello"] = "hello"
    protocol: int
    name: str
    refresh_hz: float | None = Field(default=None, gt=0)
    waveform: Waveform | None = None
    slots: list[SlotSpec] | None = None
    score_kind: str | None = None


class Selection(Message):
    """The decoder's answer for a trial; ``score``/``scores`` are optional."""

    type: Literal["selection"] = "selection"
    trial_id: int
    slot: SlotId
    t_unix: float
    score: float | None = None
    scores: dict[SlotId, float] | None = None


class NoSelection(Message):
    """No clear answer for this trial."""

    type: Literal["no_selection"] = "no_selection"
    trial_id: int
    reason: str


class DecoderStatus(Message):
    """Signal health from the decoder, any time."""

    type: Literal["status"] = "status"
    ok: bool
    message: str = ""


# ---------------------------------------------------------------------------
# INTERFACE.md v1: server -> decoder
# ---------------------------------------------------------------------------


class ServerHello(Message):
    """Server reply to the decoder's hello, with the display and stimulus settings."""

    type: Literal["hello"] = "hello"
    protocol: int
    refresh_hz: float = Field(gt=0)
    waveform: Waveform
    slots: list[SlotSpec]


class StimStart(Message):
    """Flicker began (v1), or "start flickering now" (protocol 2 proposal)."""

    type: Literal["stim_start"] = "stim_start"
    trial_id: int
    menu_id: str
    t_unix: float
    active_slots: list[SlotId] = Field(min_length=1)
    cued_slot: SlotId | None = None
    mode: Literal["live", "calibrate"] = "live"


# "no_selection" is a protocol 2 proposal (PLAN.md section 13 item 5), not part of v1.
StimStopReason = Literal[
    "selected", "timeout", "context_changed", "paused", "calibration_done", "no_selection"
]


class StimStop(Message):
    """Flicker ended."""

    type: Literal["stim_stop"] = "stim_stop"
    trial_id: int
    t_unix: float
    reason: StimStopReason


# ---------------------------------------------------------------------------
# Protocol 2 PROPOSALS (PLAN.md section 13 item 5). NOT agreed with the decoder owner yet.
# Only send these when config enables protocol 2; a v1 decoder will not understand them.
# ---------------------------------------------------------------------------


class Display(Message):
    """Proposed server -> decoder: text, labels and highlight for the decoder's display."""

    type: Literal["display"] = "display"
    text: str | None = None
    labels: list[str] | None = None
    highlight: SlotId | None = None


class Baseline(Message):
    """Proposed server -> decoder: hold the flicker off and record a resting baseline."""

    type: Literal["baseline"] = "baseline"
    seconds: float = Field(gt=0)
    t_unix: float | None = None


class StimOnset(Message):
    """Proposed decoder -> server: Unix time of the display's first flickering frame."""

    type: Literal["stim_onset"] = "stim_onset"
    trial_id: int
    t_unix: float


# ---------------------------------------------------------------------------
# Internal: server -> browser (PLAN.md section 6)
# ---------------------------------------------------------------------------


class UISlot(Message):
    """One on-screen box in the mirror: which slot, what it does, what it says."""

    slot: SlotId
    role: Literal["say", "next"]
    label: str


class ShowOption(Message):
    """The option currently offered (index of count in list list_id)."""

    type: Literal["show_option"] = "show_option"
    list_id: str
    index: int = Field(ge=0)
    count: int = Field(ge=1)
    label: str
    utterance: str
    slots: list[UISlot] = Field(min_length=1)

    @model_validator(mode="after")
    def _index_in_range(self) -> ShowOption:
        if self.index >= self.count:
            raise ValueError(f"index {self.index} out of range for count {self.count}")
        return self


class Trial(Message):
    """Trial phase change, for the mirror."""

    type: Literal["trial"] = "trial"
    trial_id: int
    phase: Literal["cue", "flicker", "rest", "baseline"]


class Highlight(Message):
    """Highlight the picked box."""

    type: Literal["highlight"] = "highlight"
    slot: SlotId


class Speak(Message):
    """Speak this exact text; reply with tts_done."""

    type: Literal["speak"] = "speak"
    text: str
    utterance_id: str


class State(Message):
    """Orchestrator state name for display."""

    type: Literal["state"] = "state"
    name: str
    detail: str = ""


class Transcript(Message):
    """One line for the conversation panel."""

    type: Literal["transcript"] = "transcript"
    who: Literal["partner", "user"]
    text: str


class UIStatus(Message):
    """Status badges for the partner page."""

    type: Literal["status"] = "status"
    decoder_connected: bool
    decoder_ok: bool
    display_ok: bool
    llm_ok: bool
    fake_decoder: bool
    keyboard_mode: bool


# ---------------------------------------------------------------------------
# Internal: browser -> server (PLAN.md section 6)
# ---------------------------------------------------------------------------


class TTSDone(Message):
    """The browser finished speaking an utterance."""

    type: Literal["tts_done"] = "tts_done"
    utterance_id: str


class PartnerText(Message):
    """What the partner said (speech-to-text) or typed."""

    type: Literal["partner_text"] = "partner_text"
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    source: Literal["stt", "typed"]


class Key(Message):
    """A key press (keyboard mode 1-2, C/W judgments, etc.)."""

    type: Literal["key"] = "key"
    key: str


class Stop(Message):
    """STOP button: pause everything, flicker off."""

    type: Literal["stop"] = "stop"


class Resume(Message):
    """Resume after STOP."""

    type: Literal["resume"] = "resume"


class Judgment(Message):
    """Partner's correct/wrong mark for a spoken utterance."""

    type: Literal["judgment"] = "judgment"
    utterance_id: str
    correct: bool


# ---------------------------------------------------------------------------
# Shared menu option
# ---------------------------------------------------------------------------


class Option(BaseModel):
    """One menu option. Length limits come from config; check them with validate_option."""

    model_config = ConfigDict(extra="ignore")

    label: str
    utterance: str
    intent_id: str


def validate_option(
    option: Option,
    label_max_chars: int,
    utterance_max_words: int,
    label_max_words: int,
) -> list[str]:
    """Return a list of problems with ``option`` (empty list means it is fine)."""
    problems: list[str] = []
    label = option.label.strip()
    utterance = option.utterance.strip()
    if not label:
        problems.append("label is empty")
    if not utterance:
        problems.append("utterance is empty")
    if not option.intent_id.strip():
        problems.append("intent_id is empty")
    if len(label) > label_max_chars:
        problems.append(f"label over {label_max_chars} chars ({len(label)})")
    label_words = len(label.split())
    if label_words > label_max_words:
        problems.append(f"label over {label_max_words} words ({label_words})")
    utterance_words = len(utterance.split())
    if utterance_words > utterance_max_words:
        problems.append(f"utterance over {utterance_max_words} words ({utterance_words})")
    return problems


# ---------------------------------------------------------------------------
# Unions and helpers
# ---------------------------------------------------------------------------

DecoderToServer = Annotated[
    Union[DecoderHello, Selection, NoSelection, DecoderStatus, StimOnset],
    Field(discriminator="type"),
]
ServerToDecoder = Annotated[
    Union[ServerHello, StimStart, StimStop, Display, Baseline],
    Field(discriminator="type"),
]
ServerToUI = Annotated[
    Union[ShowOption, Trial, Highlight, Speak, State, Transcript, UIStatus],
    Field(discriminator="type"),
]
UIToServer = Annotated[
    Union[TTSDone, PartnerText, Key, Stop, Resume, Judgment],
    Field(discriminator="type"),
]

_decoder_to_server: TypeAdapter[Any] = TypeAdapter(DecoderToServer)
_server_to_decoder: TypeAdapter[Any] = TypeAdapter(ServerToDecoder)
_server_to_ui: TypeAdapter[Any] = TypeAdapter(ServerToUI)
_ui_to_server: TypeAdapter[Any] = TypeAdapter(UIToServer)


def _parse(adapter: TypeAdapter[Any], raw: dict[str, Any] | str | bytes) -> Message:
    if isinstance(raw, (str, bytes, bytearray)):
        return adapter.validate_json(raw)
    return adapter.validate_python(raw)


def parse_decoder_to_server(raw: dict[str, Any] | str | bytes) -> Message:
    """Parse a decoder -> server message. Raises pydantic.ValidationError if invalid."""
    return _parse(_decoder_to_server, raw)


def parse_server_to_decoder(raw: dict[str, Any] | str | bytes) -> Message:
    """Parse a server -> decoder message. Raises pydantic.ValidationError if invalid."""
    return _parse(_server_to_decoder, raw)


def parse_server_to_ui(raw: dict[str, Any] | str | bytes) -> Message:
    """Parse a server -> browser message. Raises pydantic.ValidationError if invalid."""
    return _parse(_server_to_ui, raw)


def parse_ui_to_server(raw: dict[str, Any] | str | bytes) -> Message:
    """Parse a browser -> server message. Raises pydantic.ValidationError if invalid."""
    return _parse(_ui_to_server, raw)


def to_json(model: BaseModel) -> str:
    """Serialize a message as compact JSON."""
    return model.model_dump_json()


__all__ = [
    "PROTOCOL_V1",
    "PROTOCOL_V2_PROPOSED",
    "SlotId",
    "Waveform",
    "Message",
    "SlotSpec",
    "DecoderHello",
    "Selection",
    "NoSelection",
    "DecoderStatus",
    "ServerHello",
    "StimStart",
    "StimStopReason",
    "StimStop",
    "Display",
    "Baseline",
    "StimOnset",
    "UISlot",
    "ShowOption",
    "Trial",
    "Highlight",
    "Speak",
    "State",
    "Transcript",
    "UIStatus",
    "TTSDone",
    "PartnerText",
    "Key",
    "Stop",
    "Resume",
    "Judgment",
    "Option",
    "validate_option",
    "DecoderToServer",
    "ServerToDecoder",
    "ServerToUI",
    "UIToServer",
    "parse_decoder_to_server",
    "parse_server_to_decoder",
    "parse_server_to_ui",
    "parse_ui_to_server",
    "to_json",
]
