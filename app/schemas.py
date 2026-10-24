"""Message schemas: the decoder contract (protocol 2, INTERFACE-v2-draft.md) plus the internal
browser <-> server messages.

Every message is one JSON object with a ``type`` field. Unknown fields are ignored (draft section 1),
so every model uses ``extra="ignore"``. NaN and Infinity are rejected in float fields: JSON has no
way to write them, so they would turn into null and break the round trip.

Protocol 1 (``INTERFACE.md``, browser-drawn flicker) is dropped: the bridge owns the display, and
the server speaks only protocol 2 (PLAN.md section 13, decisions of 2026-10-23).

Sections:
- Decoder (bridge) -> server.
- Server -> decoder (bridge).
- The handshake check (``hello_problems``).
- Internal browser <-> server messages (PLAN.md section 6).
- Shared menu ``Option`` model and its config-driven validator.
- Discriminated unions and ``parse_*`` / ``to_json`` helpers, one union per direction
  ("status" exists in more than one direction).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

PROTOCOL = 2
# Handshake tolerances (draft section 2): contract values, not tunables.
FREQ_TOLERANCE_HZ = 0.01
REFRESH_TOLERANCE_HZ = 1.0

SlotId = Annotated[str, StringConstraints(pattern=r"^s[0-9]+$")]
Waveform = Literal["sine", "square"]
# option: one reply (page mode); next: new page or move the highlight; say / back: the confirm
# screen in page mode, or SAY in list mode.
Role = Literal["option", "next", "say", "back"]


class Message(BaseModel):
    """Base for all messages: unknown fields are ignored, NaN/Infinity floats are rejected."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    #: True for messages whose left-out fields mean "unchanged": to_json then writes only the
    #: fields that were set (an explicit None stays as null).
    partial: ClassVar[bool] = False


class SlotSpec(Message):
    """One stimulus target: slot id, flicker frequency and phase."""

    slot: SlotId
    freq_hz: float = Field(gt=0)
    phase_deg: float = 0


# ---------------------------------------------------------------------------
# Decoder (bridge) -> server (draft sections 2 and 4)
# ---------------------------------------------------------------------------


class DecoderParams(Message):
    """The live decoder's settings, reported in hello for the log."""

    window_s: float = Field(gt=0)
    step_s: float = Field(gt=0)
    votes: int = Field(ge=1)


class DecoderHello(Message):
    """Sent by the bridge right after connecting, from its stimulus config and refresh check."""

    type: Literal["hello"] = "hello"
    protocol: int
    name: str
    refresh_hz: float = Field(gt=0)
    waveform: Waveform
    slots: list[SlotSpec] = Field(min_length=1)
    decoder: DecoderParams | None = None
    score_kind: str | None = None  # set only if selections carry scores


class StimOnset(Message):
    """Unix time of the display's first flickering frame after stim_start."""

    type: Literal["stim_onset"] = "stim_onset"
    trial_id: int
    t_unix: float


class Selection(Message):
    """The decoder's first commit after stim_start; ``score``/``scores`` are optional."""

    type: Literal["selection"] = "selection"
    trial_id: int
    slot: SlotId
    t_unix: float
    score: float | None = None
    scores: dict[SlotId, float] | None = None


class NoSelection(Message):
    """The decoder gave up on this trial."""

    type: Literal["no_selection"] = "no_selection"
    trial_id: int
    reason: str


class BaselineDone(Message):
    """The resting baseline for ``baseline_id`` was recorded (or failed, with a message)."""

    type: Literal["baseline_done"] = "baseline_done"
    baseline_id: int
    ok: bool
    message: str = ""


class DecoderStatus(Message):
    """Bridge health, about once a second and on every change.

    ``display_ok`` false means the display exited or the user pressed Esc (the flicker is off);
    ``board_ok`` false is shown as a badge only.
    """

    type: Literal["status"] = "status"
    ok: bool
    board_ok: bool
    display_ok: bool
    message: str = ""


# ---------------------------------------------------------------------------
# Server -> decoder (bridge) (draft sections 2 and 3)
# ---------------------------------------------------------------------------


class HelloAck(Message):
    """The bridge's hello passed the check: the roles and initial labels per slot."""

    type: Literal["hello_ack"] = "hello_ack"
    protocol: int
    roles: dict[SlotId, Role]
    labels: dict[SlotId, str]


class ProtocolError(Message):
    """The bridge's hello failed the check; the session stays paused."""

    type: Literal["error"] = "error"
    message: str


class Baseline(Message):
    """Hold the flicker off and record the decoder's resting baseline; answered by baseline_done."""

    type: Literal["baseline"] = "baseline"
    baseline_id: int
    seconds: float = Field(gt=0)


class Display(Message):
    """What the user's screen shows. Left-out fields are unchanged; ``highlight: null`` clears it.

    Never sent between stim_start and stim_stop.
    """

    partial: ClassVar[bool] = True

    type: Literal["display"] = "display"
    text: str | None = None
    labels: dict[SlotId, str] | None = None
    highlight: SlotId | None = None


class StimStart(Message):
    """Start flickering now and start decoding."""

    type: Literal["stim_start"] = "stim_start"
    trial_id: int
    active_slots: list[SlotId] = Field(min_length=1)


StimStopReason = Literal["selected", "no_selection", "timeout", "context_changed", "paused"]


class StimStop(Message):
    """Stop flickering and stop decoding."""

    type: Literal["stim_stop"] = "stim_stop"
    trial_id: int
    reason: StimStopReason


class Pause(Message):
    """STOP from the partner page: flicker and decoding off until the next baseline."""

    type: Literal["pause"] = "pause"
    reason: str


# ---------------------------------------------------------------------------
# Handshake check (draft section 2)
# ---------------------------------------------------------------------------


def hello_problems(
    hello: DecoderHello,
    slots: list[SlotSpec],
    waveform: str,
    refresh_hz: float,
) -> list[str]:
    """Why the bridge's hello does not match our config; an empty list means hello_ack."""
    problems: list[str] = []
    if hello.protocol != PROTOCOL:
        problems.append(f"protocol {hello.protocol}, server speaks {PROTOCOL}")
    if abs(hello.refresh_hz - refresh_hz) > REFRESH_TOLERANCE_HZ:
        problems.append(
            f"display refresh is {hello.refresh_hz:g} Hz, expected {refresh_hz:g} Hz "
            "(turn ProMotion / variable refresh off)"
        )
    if hello.waveform != waveform:
        problems.append(f"waveform {hello.waveform}, expected {waveform}")
    theirs = {s.slot: s for s in hello.slots}
    ours = {s.slot: s for s in slots}
    if len(theirs) != len(hello.slots):
        problems.append("duplicate slot ids in hello")
    if set(theirs) != set(ours):
        problems.append(f"slots {sorted(theirs)}, expected {sorted(ours)}")
    for slot_id in sorted(set(theirs) & set(ours)):
        got, want = theirs[slot_id].freq_hz, ours[slot_id].freq_hz
        if abs(got - want) > FREQ_TOLERANCE_HZ:
            problems.append(f"{slot_id} is {got:g} Hz, expected {want:g} Hz")
    return problems


# ---------------------------------------------------------------------------
# Internal: server -> browser (PLAN.md section 6)
# ---------------------------------------------------------------------------


class UISlot(Message):
    """One target on the user's screen: which slot, what it does, its label and full sentence."""

    slot: SlotId
    role: Role
    label: str
    utterance: str = ""  # the sentence beside an option's target; empty for NEXT / Say it / Back


class ShowPage(Message):
    """What the user's screen shows now, for the partner page's mirror.

    ``mode``: ``page`` (an option on each target plus NEXT), ``confirm`` (``text`` is the picked
    sentence; the targets are Say it / Back) or ``list`` (all ``options`` with ``highlight_index``;
    the targets are SAY / NEXT). ``slots`` lists only the targets that flicker in the next trial.
    """

    type: Literal["show_page"] = "show_page"
    list_id: str
    page: int = Field(ge=1)
    mode: Literal["page", "confirm", "list"]
    slots: list[UISlot] = Field(min_length=1)
    text: str = ""
    options: list[Option] = Field(default_factory=list)
    highlight_index: int | None = None

    @model_validator(mode="after")
    def _highlight_in_range(self) -> ShowPage:
        if self.highlight_index is not None and not 0 <= self.highlight_index < len(self.options):
            raise ValueError(f"highlight_index {self.highlight_index} out of range for "
                             f"{len(self.options)} options")
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
    """Speak this exact text; reply with tts_done.

    ``audio_url``: a pre-generated Cartesia clip of exactly ``text`` (``/audio/{id}``); without
    one, or if it fails to play, the page speaks ``text`` with speechSynthesis.
    """

    type: Literal["speak"] = "speak"
    text: str
    utterance_id: str
    audio_url: str | None = None


class Listen(Message):
    """Listen to the partner now (``seconds`` > 0), after ``delay_s``, or stop (``seconds`` 0).

    Sent when it is the partner's turn (the session starts, a reply was just spoken) and when the
    page must not listen (a reply is being spoken, the session paused), so nobody has to press
    anything and the app never hears itself.
    """

    type: Literal["listen"] = "listen"
    seconds: float = Field(ge=0)
    delay_s: float = Field(default=0.0, ge=0)
    reason: Literal["start", "turn", "speaking", "paused"]


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
    """Status badges for the partner page.

    ``decoder_ok``: the bridge's hello passed and its last status was ok. ``decoder_message``: why
    not (a rejected hello), or the bridge's own status message. ``resume_ready``: Resume would
    start a session now.
    """

    type: Literal["status"] = "status"
    decoder_connected: bool
    decoder_ok: bool
    display_ok: bool
    llm_ok: bool
    fake_decoder: bool
    keyboard_mode: bool
    board_ok: bool = False
    decoder_message: str = ""
    resume_ready: bool = False


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
    Union[DecoderHello, StimOnset, Selection, NoSelection, BaselineDone, DecoderStatus],
    Field(discriminator="type"),
]
ServerToDecoder = Annotated[
    Union[HelloAck, ProtocolError, Baseline, Display, StimStart, StimStop, Pause],
    Field(discriminator="type"),
]
ShowPage.model_rebuild()

ServerToUI = Annotated[
    Union[ShowPage, Trial, Highlight, Speak, Listen, State, Transcript, UIStatus],
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
    """Serialize a message as compact JSON.

    Partial messages (``Display``) carry only the fields that were set, so a left-out field
    means "unchanged" while an explicit ``None`` is sent as null.
    """
    if getattr(model, "partial", False):
        data = model.model_dump(mode="json", exclude_unset=True)
        data["type"] = model.type
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return model.model_dump_json()


__all__ = [
    "PROTOCOL",
    "FREQ_TOLERANCE_HZ",
    "REFRESH_TOLERANCE_HZ",
    "SlotId",
    "Waveform",
    "Role",
    "Message",
    "SlotSpec",
    "DecoderParams",
    "DecoderHello",
    "StimOnset",
    "Selection",
    "NoSelection",
    "BaselineDone",
    "DecoderStatus",
    "HelloAck",
    "ProtocolError",
    "Baseline",
    "Display",
    "StimStart",
    "StimStopReason",
    "StimStop",
    "Pause",
    "hello_problems",
    "UISlot",
    "ShowPage",
    "Trial",
    "Highlight",
    "Speak",
    "Listen",
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
