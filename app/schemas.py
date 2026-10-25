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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_serializer,
    model_validator,
)

PROTOCOL = 2
# Handshake tolerances (draft section 2): contract values, not tunables.
FREQ_TOLERANCE_HZ = 0.01
REFRESH_TOLERANCE_HZ = 1.0

SlotId = Annotated[str, StringConstraints(pattern=r"^s[0-9]+$")]
Waveform = Literal["sine", "square"]
# freq: SSVEP, each slot flickers at its own frequency (freq_hz). code: c-VEP (Bin et al. 2009),
# every slot shows one m-sequence delayed by its own lag_frames (draft section 9).
Paradigm = Literal["freq", "code"]
# option: one reply (page mode); next: new page or move the highlight; say / back: the confirm
# screen in page mode, or SAY in list mode; left / right: the two halves in halves mode (Look to
# Speak: each look keeps one half of the remaining phrases).
Role = Literal["option", "next", "say", "back", "left", "right"]


class Message(BaseModel):
    """Base for all messages: unknown fields are ignored, NaN/Infinity floats are rejected."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    #: True for messages whose left-out fields mean "unchanged": to_json then writes only the
    #: fields that were set (an explicit None stays as null).
    partial: ClassVar[bool] = False


class SlotSpec(Message):
    """One stimulus target: slot id plus either a flicker frequency and phase (SSVEP) or a code
    lag in frames (c-VEP), never both."""

    slot: SlotId
    freq_hz: float | None = Field(default=None, gt=0)
    phase_deg: float = 0
    lag_frames: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _freq_or_lag(self) -> SlotSpec:
        if (self.freq_hz is None) == (self.lag_frames is None):
            raise ValueError(f"slot {self.slot}: give freq_hz (SSVEP) or lag_frames (c-VEP), "
                             "exactly one")
        return self

    @model_serializer(mode="wrap")
    def _without_the_other_paradigm(self, handler: Any) -> dict[str, Any]:
        """An SSVEP slot is written without lag_frames, a c-VEP slot without freq_hz and phase."""
        data = handler(self)
        if self.lag_frames is None:
            data.pop("lag_frames", None)
        else:
            data.pop("freq_hz", None)
            data.pop("phase_deg", None)
        return data


class CodeSpec(Message):
    """The c-VEP code: one m-sequence period as a string of 0/1 bits (bit 1 bright), each held for
    ``frames_per_bit`` frames. Slot k shows frame n of a trial (n = 0 at the onset) as
    ``bits[((n - lag_k) mod cycle_frames) // frames_per_bit]``; a cycle starts every
    ``cycle_frames`` frames (brainsync.vep.codes.lagged_codes)."""

    length: int = Field(ge=3)
    frames_per_bit: int = Field(default=1, ge=1)
    bits: Annotated[str, StringConstraints(pattern=r"^[01]+$")]

    @property
    def cycle_frames(self) -> int:
        return self.length * self.frames_per_bit

    @model_validator(mode="after")
    def _bits_length(self) -> CodeSpec:
        if len(self.bits) != self.length:
            raise ValueError(f"bits has {len(self.bits)} bits, length says {self.length}")
        return self


class CycleMarker(Message):
    """The first frame of c-VEP cycle ``cycle`` (0 = the trial's onset frame), in Unix seconds on
    the server's clock (the phone converts with its clock offset, draft section 9)."""

    cycle: int = Field(ge=0)
    t_unix: float


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
    paradigm: Paradigm = "freq"
    code: CodeSpec | None = None  # paradigm code only: the m-sequence the display must show

    @model_validator(mode="after")
    def _code_for_code(self) -> DecoderHello:
        if (self.paradigm == "code") != (self.code is not None):
            raise ValueError("code is required with paradigm code, and only then")
        return self


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
    trained: bool | None = None  # paradigm code: a c-VEP template is loaded (None: no template needed)


class TrainDone(Message):
    """Paradigm code: the template from ``train`` is fitted (or not). ``accuracy``: the
    leave-one-cycle-out score of the cycles it used (brainsync.vep.cvep.cross_validate), 0..1."""

    type: Literal["train_done"] = "train_done"
    train_id: int
    ok: bool
    accuracy: float | None = Field(default=None, ge=0, le=1)
    cycles: int = Field(default=0, ge=0)
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


class StimMarkers(Message):
    """Paradigm code: cycle onsets of trial ``trial_id`` reported by the phone that draws the
    flicker, relayed in order (server clock). The bridge cuts its epochs at these times; a trial's
    onset is its cycle 0."""

    type: Literal["stim_markers"] = "stim_markers"
    trial_id: int
    markers: list[CycleMarker] = Field(min_length=1)


class Train(Message):
    """Paradigm code: fit the c-VEP template from trial ``trial_id``, in which the user looks at
    ``slot`` (every active slot flickers), using its first ``cycles`` cycles; answered by
    train_done. The trial is started and stopped as usual (stim_start / stim_stop)."""

    type: Literal["train"] = "train"
    train_id: int
    trial_id: int
    slot: SlotId
    cycles: int = Field(ge=2)


# ---------------------------------------------------------------------------
# Handshake check (draft section 2)
# ---------------------------------------------------------------------------


def is_fake_name(name: str) -> bool:
    """A bridge whose hello name contains "fake" (any case) is not EEG: "fake-decoder"
    (tools/fake_decoder.py), "ssvep-fbcca-fake", "FakeSource". Shared by the orchestrator and
    tools/log_summary.py."""
    return "fake" in name.lower()


def hello_problems(
    hello: DecoderHello,
    slots: list[SlotSpec],
    waveform: str,
    refresh_hz: float,
    paradigm: str = "freq",
    code_length: int | None = None,
    frames_per_bit: int | None = None,
) -> list[str]:
    """Why the bridge's hello does not match our config; an empty list means hello_ack.

    Paradigm code compares each slot's lag_frames exactly and the code's length and
    frames_per_bit with ``code_length`` / ``frames_per_bit`` (the bits themselves come from the
    bridge: the server forwards them to the phone)."""
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
    if hello.paradigm != paradigm:
        problems.append(f"paradigm {hello.paradigm}, expected {paradigm}")
        return problems  # slots of another paradigm cannot be compared
    if paradigm == "code" and hello.code is not None:
        if code_length is not None and hello.code.length != code_length:
            problems.append(f"code length {hello.code.length}, expected {code_length}")
        if frames_per_bit is not None and hello.code.frames_per_bit != frames_per_bit:
            problems.append(f"frames_per_bit {hello.code.frames_per_bit}, expected {frames_per_bit}")
    theirs = {s.slot: s for s in hello.slots}
    ours = {s.slot: s for s in slots}
    if len(theirs) != len(hello.slots):
        problems.append("duplicate slot ids in hello")
    if set(theirs) != set(ours):
        problems.append(f"slots {sorted(theirs)}, expected {sorted(ours)}")
    for slot_id in sorted(set(theirs) & set(ours)):
        if paradigm == "code":
            got_lag, want_lag = theirs[slot_id].lag_frames, ours[slot_id].lag_frames
            if got_lag != want_lag:
                problems.append(f"{slot_id} lags {got_lag} frames, expected {want_lag}")
            continue
        got, want = theirs[slot_id].freq_hz, ours[slot_id].freq_hz
        if got is None or want is None:
            problems.append(f"{slot_id} has no frequency")
        elif abs(got - want) > FREQ_TOLERANCE_HZ:
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
    mode: Literal["page", "confirm", "list", "halves", "calibrate"]
    slots: list[UISlot] = Field(min_length=1)
    text: str = ""
    options: list[Option] = Field(default_factory=list)
    highlight_index: int | None = None
    # halves mode: the phrases still in play are ``options``; the first ``split`` of them sit on
    # the "left" slot's card, the rest on the "right" slot's. An option with an empty utterance is
    # "More options" (choosing it alone asks for a new page; nothing is spoken).
    split: int | None = None
    # calibrate mode: the slot the user looks at while the c-VEP template is trained.
    cue: SlotId | None = None

    @model_validator(mode="after")
    def _highlight_in_range(self) -> ShowPage:
        if self.highlight_index is not None and not 0 <= self.highlight_index < len(self.options):
            raise ValueError(f"highlight_index {self.highlight_index} out of range for "
                             f"{len(self.options)} options")
        if self.mode == "halves" and (self.split is None
                                      or not 1 <= self.split < len(self.options)):
            raise ValueError(f"halves needs 1 <= split < {len(self.options)} options, "
                             f"got {self.split}")
        if self.mode == "calibrate" and self.cue is None:
            raise ValueError("calibrate needs a cue slot")
        return self


class Trial(Message):
    """Trial phase change, for the mirror. ``paused``: the session paused (STOP, display Esc,
    decoder lost, baseline failed), so nothing flickers and a selection timer stops."""

    type: Literal["trial"] = "trial"
    trial_id: int
    phase: Literal["cue", "flicker", "rest", "baseline", "paused"]


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
    """What the partner page's mic should do. Three forms:

    - ``until_stopped``: listen from ``delay_s`` on until the next listen message (``always`` mode:
      the whole session except while a reply plays, paused, idle or muted);
    - ``seconds`` > 0: listen for that long, after ``delay_s`` (``turn`` mode: the partner's turn);
    - neither: stop now.

    ``muted`` carries the partner's Mute, which the server holds. Sent so nobody has to press
    anything and the app never hears itself.
    """

    type: Literal["listen"] = "listen"
    seconds: float = Field(ge=0)
    delay_s: float = Field(default=0.0, ge=0)
    until_stopped: bool = False
    muted: bool = False
    reason: Literal["start", "turn", "speaking", "paused", "muted", "idle", "reconnect"]

    @model_validator(mode="after")
    def _one_form(self) -> Listen:
        if self.until_stopped and self.seconds > 0:
            raise ValueError("until_stopped listens until told otherwise: seconds must be 0")
        return self


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
    start a session now. ``fake_decoder``: selections are not EEG (``--fake-decoder``, or the acked
    hello's name contains "fake", ``is_fake_name``). ``decoder_name``: the acked hello's name, ""
    before an ack.
    ``llm_ok``: Claude's last page came in time and valid. ``llm``: the same, telling "not asked
    yet" apart from "failed": ``off`` (no key, or ``menus.dynamic`` false), ``waiting`` (no page
    yet), ``ok``, ``failed`` (the last page fell back to the fixed list).
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
    decoder_name: str = ""
    llm: Literal["off", "waiting", "ok", "failed"] = "off"
    mic_muted: bool = False  # the partner's Mute
    mic: str = ""  # the page's last mic_report state ("listening", "offline", "denied", ...)
    resume_ready: bool = False
    # Paradigm code: the bridge holds a c-VEP template, and the last calibration's score (0..1).
    trained: bool = False
    calibration_accuracy: float | None = None


class Stimulus(Message):
    """Paradigm code, display.flicker phone: what the phone draws. Sent to a page that connects
    and again after every hello_ack. Slot ``s`` shows frame n of a trial (n = 0 at its first
    flicker frame) bright when ``bits[((n - lags[s]) mod (len(bits) * frames_per_bit)) //
    frames_per_bit]`` is "1" (CodeSpec). ``depth`` scales the contrast (1 = black / white)."""

    type: Literal["stimulus"] = "stimulus"
    paradigm: Paradigm = "code"
    refresh_hz: float = Field(gt=0)
    frames_per_bit: int = Field(ge=1)
    bits: Annotated[str, StringConstraints(pattern=r"^[01]+$")]
    lags: dict[SlotId, int]
    depth: float = Field(default=1.0, gt=0, le=1)


class ClockPong(Message):
    """The answer to clock_ping, sent at once: ``t_server`` is the server's time.time() when it
    answered. The phone keeps the offset t_server - (t_send + t_receive) / 2 of the pings with
    the shortest round trip (draft section 9)."""

    type: Literal["clock_pong"] = "clock_pong"
    id: int
    t_phone: float
    t_server: float


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
    """Resume after STOP. ``start``: the start button of a newly opened page (the flicker
    warning), a fresh session: an empty conversation and the home page. Left out (None) by the
    Resume button and by clients that do not send it."""

    type: Literal["resume"] = "resume"
    start: bool | None = None


class MicSet(Message):
    """The partner pressed Mute or Unmute (the server holds the setting)."""

    type: Literal["mic_set"] = "mic_set"
    muted: bool


class MicReport(Message):
    """What the page's speech recognition is doing, sent when it changes (no transcript text)."""

    type: Literal["mic_report"] = "mic_report"
    state: Literal["listening", "needs_tap", "denied", "nomic", "offline", "unsupported", "error",
                   "stopped", "hidden", "muted", "speaking", "paused"]
    restarts: int = Field(default=0, ge=0)


class Judgment(Message):
    """Partner's correct/wrong mark for a spoken utterance."""

    type: Literal["judgment"] = "judgment"
    utterance_id: str
    correct: bool


class ClockPing(Message):
    """The phone's clock sync (draft section 9): its own Unix time when sent."""

    type: Literal["clock_ping"] = "clock_ping"
    id: int
    t_phone: float


class CycleMarkers(Message):
    """The phone drew the first frame of these c-VEP cycles of trial ``trial_id`` (server clock).
    Sent once per cycle, in order, from cycle 0 (the onset). ``dropped_frames``: frames the phone
    missed in this trial so far."""

    type: Literal["cycle_markers"] = "cycle_markers"
    trial_id: int
    markers: list[CycleMarker] = Field(min_length=1)
    dropped_frames: int = Field(default=0, ge=0)


class DisplayReport(Message):
    """The phone's own refresh check: the frame rate it measured while locked to 60 Hz, and
    whether it will flicker (``ok``). Sent on connect and when it changes."""

    type: Literal["display_report"] = "display_report"
    refresh_hz: float = Field(ge=0)
    ok: bool
    message: str = ""


class Calibrate(Message):
    """Train the c-VEP template now (the setup step, or Recalibrate in Settings)."""

    type: Literal["calibrate"] = "calibrate"


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
    Union[DecoderHello, StimOnset, Selection, NoSelection, BaselineDone, DecoderStatus, TrainDone],
    Field(discriminator="type"),
]
ServerToDecoder = Annotated[
    Union[HelloAck, ProtocolError, Baseline, Display, StimStart, StimStop, Pause, StimMarkers,
          Train],
    Field(discriminator="type"),
]
ShowPage.model_rebuild()

ServerToUI = Annotated[
    Union[ShowPage, Trial, Highlight, Speak, Listen, State, Transcript, UIStatus, Stimulus,
          ClockPong],
    Field(discriminator="type"),
]
UIToServer = Annotated[
    Union[TTSDone, PartnerText, Key, Stop, Resume, Judgment, MicSet, MicReport, ClockPing,
          CycleMarkers, DisplayReport, Calibrate],
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
    "Paradigm",
    "Role",
    "Message",
    "SlotSpec",
    "CodeSpec",
    "CycleMarker",
    "TrainDone",
    "StimMarkers",
    "Train",
    "Stimulus",
    "ClockPong",
    "ClockPing",
    "CycleMarkers",
    "DisplayReport",
    "Calibrate",
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
    "MicSet",
    "MicReport",
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
