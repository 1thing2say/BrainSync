"""Server-side state machine (PLAN.md section 7).

Page mode (default): 3 reply options, each on its own flickering target, plus NEXT on a fourth
(a diamond on the user's screen). Looking at an option opens a confirm screen with only two
targets, "Say it" and "Back"; only "Say it" speaks. List mode (the 2-target fallback): all
options listed with one highlighted, SAY speaks it and NEXT moves the highlight. Halves mode
(Look to Speak on the phone, c-VEP, draft section 9): two cards, left and right; each look keeps
one half of the phrases still in play until one is left, then "Say it" / "Back".

The orchestrator owns all state. It is driven by events (decoder and browser messages, plus
``tick()``), reads time from an injectable clock and sends messages through two callbacks, so tests
run it without timers, sockets or threads. Pages of options come from the LLM (V3: a
``request_page`` hook, answered by ``on_page_ready``) after each partner line and for "More
options"; the next MENU_CUE waits for it in PREPARING for up to ``llm.timeout_s``, then uses the
fallback lists in ``config/intents.yaml``. The decoder side speaks protocol 2
(INTERFACE-v2-draft.md). In paradigm code the phone draws the flicker: the orchestrator forwards
the bridge's code to it (``stimulus``), relays its cycle markers to the bridge (``stim_markers``)
and runs a calibration trial when the bridge has no template.
"""

from __future__ import annotations

import itertools
import math
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.config import AppConfig, IntentsConfig
from app.context import Conversation, PageRequest
from app.log import EventLog
from app.schemas import (
    PROTOCOL,
    Baseline,
    BaselineDone,
    Calibrate,
    ClockPing,
    CodeSpec,
    CycleMarkers,
    DecoderHello,
    DecoderStatus,
    Display,
    DisplayReport,
    HelloAck,
    Highlight,
    Judgment,
    Key,
    Listen,
    MicReport,
    MicSet,
    Message,
    NoSelection,
    Option,
    PartnerText,
    Pause,
    ProtocolError,
    Resume,
    Selection,
    ShowPage,
    Speak,
    State,
    StimOnset,
    StimMarkers,
    StimStart,
    StimStop,
    Stimulus,
    Stop,
    Train,
    TrainDone,
    Transcript,
    Trial,
    TTSDone,
    UISlot,
    UIStatus,
    hello_problems,
    is_fake_name,
)

Send = Callable[[Message], None]

DIDNT_CATCH = "Didn't catch that"
NOT_CONFIRMED = "Not confirmed: nothing was said"
HOME_LIST = "home"
MORE_LIST = "more"  # the first "More options" page from home, if intents.yaml has it
LLM_LIST = "llm"  # name of a page the LLM wrote
# How a spoken line starts when it is a question (speech-to-text adds no "?").
QUESTION_STARTS = {
    "who", "what", "when", "where", "why", "how", "which", "whose", "whom", "do", "does", "did",
    "are", "is", "was", "were", "am", "can", "could", "would", "will", "should", "shall", "have",
    "has", "had", "may", "might", "must", "want", "wanna", "you", "any", "anything", "shouldn't",
    "don't", "doesn't", "didn't", "aren't", "isn't", "won't", "wouldn't", "can't", "couldn't",
}
FILLER = {"so", "um", "uh", "hey", "hi", "ok", "okay", "well", "oh", "and", "but"}
FINDING_REPLIES = "Finding replies"
NEXT_PAGE_LABEL = "More options"
SAY_IT, BACK = "Say it", "Back"
LIST_LABELS = {"say": "SAY", "next": "NEXT"}
LIST_MORE_LABEL = "MORE"  # list mode: NEXT on the last option asks for new options
# Halves mode: the last item of every page; chosen alone it asks for a new page (nothing spoken).
MORE_ITEM = Option(label=NEXT_PAGE_LABEL, utterance="", intent_id="more")
SIDE_LABELS = {"left": "Left", "right": "Right"}
MISSES_TO_RESET = 2  # halves mode: misses in a row on a narrowed screen go back to the whole page
CALIBRATE_LIST = "calibrate"
CALIBRATE_TEXT = "Look at the left card"
CALIBRATING_DETAIL = "Calibrating: keep looking at the left card"
LOW_CALIBRATION = 0.7  # below this the session still runs, but the state says the score is low


class St(str, Enum):
    IDLE = "IDLE"
    BASELINE = "BASELINE"
    PREPARING = "PREPARING"  # waiting for the LLM's page (up to llm.timeout_s), screen still
    MENU_CUE = "MENU_CUE"
    STIMULATING = "STIMULATING"  # a page or list trial
    CONFIRMING = "CONFIRMING"  # the confirm trial: Say it / Back
    CALIBRATING = "CALIBRATING"  # paradigm code: a still cue, then the trial that trains the template
    SELECTED = "SELECTED"
    SPEAKING = "SPEAKING"
    REST = "REST"
    NOT_CAUGHT = "NOT_CAUGHT"
    PAUSED = "PAUSED"


TRIAL_STATES = (St.STIMULATING, St.CONFIRMING)


@dataclass
class OptionList:
    list_id: str
    name: str  # fallback list name, or "llm"
    options: list[Option]


@dataclass
class Screen:
    """What the user's screen shows for the next trial, and what each active target does."""

    show: ShowPage  # for the partner page's mirror
    display: Display  # for the bridge's PsychoPy display
    roles: dict[str, str]  # active slot -> role; only these flicker in the trial


class Orchestrator:
    """Event-driven state machine. Call ``tick()`` regularly; every other method is an event."""

    def __init__(
        self,
        cfg: AppConfig,
        intents: IntentsConfig,
        send_ui: Send,
        send_decoder: Send,
        clock: Callable[[], float] = time.time,
        log: EventLog | None = None,
        keyboard_mode: bool = False,
        fake_decoder: bool = False,
        on_page: Callable[[list[str]], None] | None = None,
        audio_url: Callable[[str], str | None] | None = None,
        request_page: Callable[[PageRequest], None] | None = None,
    ) -> None:
        """``on_page(utterances)`` runs whenever a new page of options is set (TTS prefetch);
        ``audio_url(text)`` returns a ready clip for ``text`` or None (the browser speaks it);
        ``request_page(req)`` asks the LLM for a page, answered later by ``on_page_ready``. Without
        it, every page comes from the fallback lists."""
        self.cfg = cfg
        self.intents = intents
        self._send_ui_cb = send_ui
        self._send_decoder_cb = send_decoder
        self.clock = clock
        self.log = log
        self.keyboard_mode = keyboard_mode
        self.fake_decoder = fake_decoder
        self.on_page = on_page
        self.audio_url = audio_url
        self.request_page = request_page
        self.conversation = Conversation(cfg.context.history_turns)
        self._request_ids = itertools.count(1)
        self.awaiting: int | None = None  # the LLM page request the next MENU_CUE waits for
        self.awaiting_until = 0.0  # when that wait ends (llm.timeout_s after the request)
        # The partner's mic (PLAN V4). In always mode it follows the state (see _mic_wanted).
        self.mic_muted = cfg.speech.start_muted
        self.mic_key: tuple[bool, bool] | None = None  # last (on, muted) sent in always mode
        self.mic_tail_until = 0.0  # the mic reopens after this, once a reply has ended
        self.mic_report = ""  # what the page says its recognition is doing
        self.last_stt_interrupt = float("-inf")
        # The last LLM page arrived in time and valid; None until one was asked for and answered
        # (no OK badge before proof, and no warning either).
        self.llm_ok: bool | None = None

        self.mode = cfg.menus.mode
        self.slots = [s.slot for s in cfg.stim.slots]
        self.roles = dict(cfg.stim.roles)
        self.option_slots = [slot for slot in self.slots if self.roles[slot] == "option"]
        self.state = St.IDLE
        self.state_detail = ""
        self.deadline: float | None = None

        self.trial_id = 0
        self.active_trial: int | None = None
        self.selected_slot: str | None = None
        self.screen: Screen | None = None  # fixed from MENU_CUE until the trial ends
        self.read_page: tuple | None = None  # the last page or list item given its reading time
        # Paradigm code: the phone sends cycle markers; a selection counts only after cycle 0.
        self.onset_seen = False

        self._list_ids = itertools.count(1)
        self._utterance_ids = itertools.count(1)
        self.page_no = 0  # counts pages shown; _set_list makes the first one page 1
        self.index = 0  # list mode: the highlighted option
        self.candidates: list[Option] = []  # halves mode: the items still in play
        self.misses = 0  # halves mode: misses in a row on the current screen
        self._set_list(self._fallback(HOME_LIST))
        self.picked: Option | None = None  # page mode: the option waiting for "Say it"
        self.pending_list: OptionList | None = None
        self.more_in_a_row = 0

        self.speaking_id: str | None = None
        self.last_spoken_id: str | None = None

        self.decoder_connected = False
        self.acked = False  # the bridge's hello passed the check (hello_ack sent)
        self.decoder_error = ""  # why the last hello was rejected
        self.decoder_name = ""  # the acked hello's name; "" before an ack
        self.paused_by_decoder = False
        # Last bridge status; assumed fine until a status says otherwise.
        self.bridge_ok = self.board_ok = self.display_ok = True
        self.bridge_message = ""
        self.baseline_id = 0
        self.expected_baseline: int | None = None  # baseline_id we wait for, if a bridge records it

        # Paradigm code (draft section 9). The code from the acked hello, forwarded to the phone.
        self.code: CodeSpec | None = None
        # The phone's own refresh check (display.flicker: phone); None until it reports.
        self.phone_display: DisplayReport | None = None
        self.bridge_trained = False  # the bridge holds a c-VEP template (its status says so)
        self.calibration_accuracy: float | None = None  # the last calibration's score, 0..1
        self.calibrate_requested = False  # the page asked to calibrate (again) at the next chance
        self.train_id = 0
        self.expected_train: int | None = None  # train_id whose train_done the calibration waits for

    @property
    def decoder_fake(self) -> bool:
        """Selections are not EEG: started with --fake-decoder, or the acked bridge says it is a
        fake: its name contains "fake" (tools/fake_decoder.py names itself "fake-decoder" unless
        --name says otherwise)."""
        return self.fake_decoder or is_fake_name(self.decoder_name)

    @property
    def shown(self) -> ShowPage | None:
        return self.screen.show if self.screen is not None else None

    # ------------------------------------------------------------------ events: time

    def tick(self) -> None:
        """Fire the pending deadline if it has passed."""
        if self.deadline is not None and self.clock() >= self.deadline:
            self.deadline = None
            self._on_deadline()

    def _on_deadline(self) -> None:
        if self.state is St.BASELINE:
            if self.expected_baseline is None:
                self._after_baseline()  # no bridge recording it: the still screen was the baseline
            else:
                self._record("baseline_timeout", baseline_id=self.expected_baseline)
                self._pause_session(f"Baseline failed: no baseline_done within "
                                    f"{self.cfg.baseline.timeout_s:g} s", "baseline_failed")
        elif self.state is St.PREPARING:
            self._give_up_on_llm()
            self._enter_menu_cue()  # with the fallback list
        elif self.state is St.MENU_CUE:
            self._start_trial()
        elif self.state is St.CALIBRATING:
            if self.active_trial is None:
                self._start_trial()  # the cue is over: the calibration trial
            else:
                self._record("train_timeout", train_id=self.expected_train)
                self._pause_session(f"Calibration failed: no train_done within "
                                    f"{self._calibration_s():g} s", "calibration_failed")
        elif self.state in TRIAL_STATES:
            self._not_caught("timeout")
        elif self.state is St.SELECTED:
            self._after_selected()
        elif self.state is St.SPEAKING:
            self._record("tts_timeout", utterance_id=self.speaking_id)
            self._finish_speaking()
        elif self.state is St.NOT_CAUGHT:
            self._enter_rest()
        elif self.state is St.REST:
            self._enter_menu_cue()

    # --------------------------------------------------------------- events: decoder

    def on_decoder_connected(self) -> None:
        self.decoder_connected = True
        self._record("decoder_connected")
        self._send_status()
        # A session paused by a lost decoder resumes in _on_hello, once the bridge's hello passes:
        # baseline comes after hello_ack (INTERFACE-v2-draft.md section 3).

    def on_decoder_disconnected(self) -> None:
        self.decoder_connected = False
        self.acked = False
        self.decoder_error = ""
        self.decoder_name = ""
        self.bridge_ok = self.board_ok = self.display_ok = True
        self.bridge_message = ""
        self.bridge_trained = False  # a bridge that comes back says again whether it has a template
        self._record("decoder_disconnected")
        self._send_status()
        if self.keyboard_mode or self.state in (St.IDLE, St.PAUSED):
            return
        self._pause("Decoder disconnected")
        self.paused_by_decoder = True

    def on_decoder_message(self, msg: Message) -> None:
        self._record("from_decoder", message=msg)
        if isinstance(msg, DecoderHello):
            self._on_hello(msg)
        elif isinstance(msg, DecoderStatus):
            self._on_bridge_status(msg)
        elif isinstance(msg, BaselineDone):
            self._on_baseline_done(msg)
        elif isinstance(msg, Selection):
            self._on_selection(msg.trial_id, msg.slot, source="decoder")
        elif isinstance(msg, NoSelection):
            if self._is_current(msg.trial_id):
                self._not_caught("no_selection")
            else:
                self._record("stale_no_selection", trial_id=msg.trial_id)
        elif isinstance(msg, StimOnset):
            # Logged above (his offline analysis uses it); the mirror shows the flicker from here.
            # With the phone drawing, the phone was told at stim_start and its markers are the onset.
            if self._is_current(msg.trial_id) and self.cfg.display.flicker != "phone":
                self._send_ui(Trial(trial_id=msg.trial_id, phase="flicker"))
        elif isinstance(msg, TrainDone):
            self._on_train_done(msg)

    def _on_hello(self, hello: DecoderHello) -> None:
        """Check the bridge's hello against config: hello_ack, or error and stay paused."""
        stim = self.cfg.stim
        problems = hello_problems(
            hello, stim.slots, stim.waveform, self.cfg.display.expected_refresh_hz,
            paradigm=stim.paradigm,
            code_length=stim.code.length if stim.code is not None else None,
            frames_per_bit=stim.code.frames_per_bit if stim.code is not None else None)
        self.decoder_name = ""  # a new hello: only an acked one names the decoder
        if problems:
            self.acked = False
            self.code = None
            self.decoder_error = "; ".join(problems)
            self._send_decoder(ProtocolError(message=self.decoder_error))
            self._send_status()
            if self.state not in (St.IDLE, St.PAUSED):
                self._pause(f"Decoder rejected: {self.decoder_error}")
            return
        self.acked = True
        self.decoder_error = ""
        self.decoder_name = hello.name
        # The log says whether this session's selections came from EEG.
        self._record("decoder_acked", name=hello.name, fake=self.decoder_fake)
        screen = self.screen or self._build_screen()
        self._send_decoder(HelloAck(protocol=PROTOCOL, roles=self.roles,
                                    labels=screen.display.labels or {}))
        if stim.paradigm == "code":
            self.code = hello.code
            self._send_stimulus()  # the phone draws the bridge's code
        self._send_status()
        if self.state is St.PAUSED and self.paused_by_decoder and self.display_ok:
            self._enter_baseline()

    def _on_bridge_status(self, msg: DecoderStatus) -> None:
        display_lost = self.display_ok and not msg.display_ok
        trained = self.bridge_trained if msg.trained is None else msg.trained
        changed = (msg.ok, msg.board_ok, msg.display_ok, msg.message, trained) != (
            self.bridge_ok, self.board_ok, self.display_ok, self.bridge_message,
            self.bridge_trained)
        self.bridge_ok, self.board_ok, self.display_ok = msg.ok, msg.board_ok, msg.display_ok
        self.bridge_message = msg.message
        self.bridge_trained = trained
        if display_lost and self.state not in (St.IDLE, St.PAUSED):
            # The flicker is already off (Esc or the display exited). No automatic resume:
            # after Esc the partner presses Resume once the display is back.
            self._pause_session("Display closed (Esc or exit)", "display_closed")
            self.paused_by_decoder = False
        elif changed:
            self._send_status()

    def _on_baseline_done(self, msg: BaselineDone) -> None:
        if self.state is not St.BASELINE or msg.baseline_id != self.expected_baseline:
            self._record("stale_baseline_done", baseline_id=msg.baseline_id)
            return
        self.expected_baseline = None
        if msg.ok:
            self._after_baseline()
        else:
            self._pause_session(f"Baseline failed: {msg.message or 'no reason given'}",
                                "baseline_failed")

    def _on_train_done(self, msg: TrainDone) -> None:
        """The calibration trial's answer: the first page with the new template, or pause."""
        if self.state is not St.CALIBRATING or msg.train_id != self.expected_train:
            self._record("stale_train_done", train_id=msg.train_id)
            return
        self.expected_train = None
        if not msg.ok:
            self._pause_session(f"Calibration failed: {msg.message or 'no reason given'}",
                                "calibration_failed")
            return
        self._stop_trial("selected")
        self.bridge_trained = True
        self.calibration_accuracy = msg.accuracy
        self.calibrate_requested = False
        self._record("calibrated", train_id=msg.train_id, accuracy=msg.accuracy, cycles=msg.cycles)
        self._send_status()
        detail = "Calibrated"
        if msg.accuracy is not None:
            detail += f": {round(msg.accuracy * 100)} %"
            if msg.accuracy < LOW_CALIBRATION:
                detail += " (low: selections may be wrong; calibrate again from Settings)"
        self._enter_menu_cue(detail)

    def resume_problem(self) -> str:
        """Why Resume would not start a session now; empty if it would."""
        if not (self.keyboard_mode and not self.acked):  # keyboard mode runs without a bridge
            if self.decoder_error:
                return f"Decoder rejected: {self.decoder_error}"
            if not self.acked:
                return "Waiting for the decoder"
            if not self.display_ok:
                return "Display closed: restart it, then Resume"
        return self._phone_problem()

    def _phone_problem(self) -> str:
        """With the phone drawing the flicker, why it may not flicker now; empty if it may."""
        if self.cfg.display.flicker != "phone":
            return ""
        if self.phone_display is None:
            return "Waiting for the phone's refresh check"
        if not self.phone_display.ok:
            return f"Phone display: {self.phone_display.message or 'refresh check failed'}"
        return ""

    # ---------------------------------------------------------------- events: browser

    def on_ui_disconnected(self) -> None:
        """The page closed or lost its connection. When the page draws the flicker
        (display.flicker: browser) nothing flickers any more: pause, as for a lost display, so the
        decoder never picks from a still screen."""
        self._record("ui_disconnected")
        if self.cfg.display.flicker == "phone":
            self.phone_display = None  # the next page (maybe another phone) checks its refresh again
        if self._page_flickers() and self.state not in (St.IDLE, St.PAUSED):
            self._pause_session("Screen closed", "screen_closed")

    def on_ui_connected(self) -> None:
        """A page connected: bring it up to date."""
        self._record("ui_connected")
        if self._page_flickers() and self.state not in (St.IDLE, St.PAUSED):
            # A new page (one replacing the old, a reload) flickers only after its own start
            # button: until then nothing flickers, so the session waits paused.
            self._pause_session("New screen: press start", "screen_replaced")
        self._send_status()
        self._send_stimulus()
        if self.shown is not None:
            self._send_ui(self.shown)
        self._send_state(self.state_detail)
        # A reloaded or reconnected page gets the mic state again and resumes by itself.
        if self.cfg.speech.stt_mode == "always":
            self._sync_mic(force=True, reason="reconnect")
        else:
            self._send_ui(Listen(seconds=0.0, muted=self.mic_muted, reason="reconnect"))

    def on_ui_message(self, msg: Message) -> None:
        if isinstance(msg, ClockPing):
            return  # the server answers it at once; it never reaches here (and is not logged)
        self._record("from_ui", message=msg)
        if isinstance(msg, Resume):
            if self.state in (St.IDLE, St.PAUSED):
                problem = self.resume_problem()
                if problem:
                    self._send_state(problem)  # stay put; the page shows why
                else:
                    if msg.start:
                        self._fresh_start()
                    self._enter_baseline()
            elif msg.start:
                # A newly opened page while the session runs (a reload): start over from the
                # home page; a trial in progress ends first, so the screen may change.
                in_trial = self.state in TRIAL_STATES
                if in_trial:
                    self._stop_trial("context_changed")
                self._fresh_start()
                if in_trial or self.state in (St.MENU_CUE, St.PREPARING):
                    self._enter_menu_cue()
        elif isinstance(msg, Stop):
            if self.state not in (St.IDLE, St.PAUSED):
                self._pause("Stopped")
            # STOP means stopped until Resume, even if a lost decoder paused us first: without
            # this the flicker came back by itself when the bridge reconnected (rule 10).
            self.paused_by_decoder = False
            self._send_decoder(Pause(reason="stop_button"))
        elif isinstance(msg, PartnerText):
            self._on_partner_text(msg.text, msg.source)
        elif isinstance(msg, TTSDone):
            if self.state is St.SPEAKING and msg.utterance_id == self.speaking_id:
                self._finish_speaking()
        elif isinstance(msg, Key):
            self._on_key(msg.key)
        elif isinstance(msg, Judgment):
            pass  # logged above; read by the log summary
        elif isinstance(msg, MicSet):
            if msg.muted != self.mic_muted:
                self._record("mic_muted", muted=msg.muted)
                self.mic_muted = msg.muted
                if self.cfg.speech.stt_mode == "always":
                    self._sync_mic(force=True)
                elif msg.muted:
                    self._listen(False, "muted")
                else:
                    self._send_ui(Listen(seconds=0.0, muted=False, reason="muted"))
            self._send_status()
        elif isinstance(msg, MicReport):
            if msg.state != self.mic_report:
                self.mic_report = msg.state
                self._send_status()
        elif isinstance(msg, CycleMarkers):
            self._on_cycle_markers(msg)
        elif isinstance(msg, DisplayReport):
            self._on_display_report(msg)
        elif isinstance(msg, Calibrate):
            self._on_calibrate()

    def _on_cycle_markers(self, msg: CycleMarkers) -> None:
        """The phone drew these cycle starts: relay them to the bridge, which cuts its epochs at
        them. Cycle 0 is the trial's onset (rule 8: no selection before it)."""
        if self.cfg.display.flicker != "phone":
            self._record("cycle_markers_ignored", trial_id=msg.trial_id)
            return
        if msg.trial_id != self.active_trial:
            self._record("stale_cycle_markers", trial_id=msg.trial_id)
            return
        if not self.onset_seen and any(m.cycle == 0 for m in msg.markers):
            self.onset_seen = True
            onset = next(m for m in msg.markers if m.cycle == 0)
            # (t_unix is the log line's own time, so the onset goes in onset_t_unix)
            self._record("stim_onset", trial_id=msg.trial_id, onset_t_unix=onset.t_unix)
        self._send_decoder(StimMarkers(trial_id=msg.trial_id, markers=msg.markers))

    def _on_display_report(self, msg: DisplayReport) -> None:
        """The phone's refresh check. Not ok: its flicker is off, so a session pauses, and Resume
        waits for an ok report (like display_ok: false from the bridge)."""
        changed = msg != self.phone_display
        self.phone_display = msg
        if self.cfg.display.flicker != "phone":
            return
        if not msg.ok and self.state not in (St.IDLE, St.PAUSED):
            self._pause_session(f"Phone display: {msg.message or 'refresh check failed'}",
                                "display_closed")
        elif changed:
            self._send_status()

    def _on_calibrate(self) -> None:
        """Train the c-VEP template (the setup step, or Recalibrate in Settings): at the next
        start when idle or paused, now during a session (after a reply that is playing)."""
        if self.cfg.stim.paradigm != "code":
            self._record("calibrate_ignored", why="paradigm freq")
            return
        self.calibrate_requested = True
        if self.state in (St.IDLE, St.PAUSED, St.BASELINE, St.CALIBRATING, St.SPEAKING):
            return  # the next start, the end of this baseline or the next cue calibrates
        if not self.acked:
            self._record("calibrate_ignored", why="no bridge")
            self.calibrate_requested = False
            return
        if self.state in TRIAL_STATES:
            self._stop_trial("context_changed")
        self.picked = None
        self._calibrate()

    def _on_key(self, key: str) -> None:
        """Keyboard mode: digit n selects slot n (1 = top, 4 = bottom) if it flickers now."""
        key = key.lower()
        if key.isdigit() and self.keyboard_mode:
            n = int(key)
            if 1 <= n <= len(self.slots) and self.state in TRIAL_STATES:
                self._on_selection(self.active_trial, self.slots[n - 1], source="keyboard")
        elif key in ("c", "w") and self.last_spoken_id is not None:
            self._record("judgment", utterance_id=self.last_spoken_id, correct=key == "c")

    def _on_partner_text(self, text: str, source: str = "typed") -> None:
        if source == "stt" and self.mic_muted:
            self._record("stt_ignored_muted")  # heard just before Mute took effect
            return
        self._send_ui(Transcript(who="partner", text=text))
        self.conversation.partner_said(text)
        question = is_question(text)
        name = self.intents.fallback_for(text, question)
        self.pending_list = self._fallback(name)  # shown if the LLM's page does not come in time
        self.more_in_a_row = 0
        self._ask_llm("partner")
        if source == "stt" and self._queue_spoken(question):
            # Spoken lines never throw away the BCI user's work: the replies are prepared now and
            # shown at the next cue. Typed text is deliberate, so it always interrupts.
            self._record("partner_text_queued", trial_id=self.active_trial, question=question)
            return
        if self.state in TRIAL_STATES:
            self._stop_trial("context_changed")
            self.picked = None
            self._enter_menu_cue()
        elif self.state in (St.MENU_CUE, St.PREPARING):
            self.picked = None
            self._enter_menu_cue()  # not in a trial yet, so the screen may still change
        # In every other state the new page is shown at the next MENU_CUE.

    def _queue_spoken(self, question: bool) -> bool:
        """Whether a spoken line waits for the next cue instead of changing the screen now."""
        if self.picked is not None:
            return True  # a pick in progress (the Say it / Back screen) is never interrupted
        if self.state not in (*TRIAL_STATES, St.MENU_CUE):
            return False  # nothing on screen to protect (PREPARING takes the newest line)
        speech = self.cfg.speech
        now = self.clock()
        interrupts = (question and speech.stt_interrupts == "question"
                      and now - self.last_stt_interrupt >= speech.stt_interrupt_cooldown_s)
        if interrupts:
            self.last_stt_interrupt = now  # at most one spoken interrupt per cooldown
        return not interrupts

    # -------------------------------------------------------------- events: LLM pages

    def on_page_ready(self, request_id: int, options: list[Option] | None) -> None:
        """The LLM's answer to ``request_page``: ranked options, or None (it failed)."""
        if request_id != self.awaiting:
            self._record("llm_page_ignored", request_id=request_id,
                         why="late" if options is not None else "late failure")
            return
        if self.clock() >= self.awaiting_until:
            # Still awaited only because the confirm trial does not wait: too late all the same.
            self._give_up_on_llm()  # keep the fallback list already pending
            options = None
        self.awaiting = None
        if options is None:
            self._set_llm_ok(False)  # keep the fallback list already pending
        else:
            self.pending_list = OptionList(list_id=f"m{next(self._list_ids)}", name=LLM_LIST,
                                           options=list(options))
            self._set_llm_ok(True)
        if self.state is St.PREPARING:
            self._enter_menu_cue()

    def _ask_llm(self, reason: str) -> None:
        """Ask for an LLM page; the next MENU_CUE waits for it (up to llm.timeout_s)."""
        if self.request_page is None:
            return
        self.awaiting = next(self._request_ids)
        self.awaiting_until = self.clock() + self.cfg.llm.timeout_s
        self.request_page(self.conversation.page_request(self.awaiting, reason, self.clock()))

    def _give_up_on_llm(self) -> None:
        self._record("llm_page_timeout", request_id=self.awaiting)
        self.awaiting = None
        self._set_llm_ok(False)

    def _set_llm_ok(self, ok: bool) -> None:
        if ok != self.llm_ok:
            self.llm_ok = ok
            self._send_status()

    # ------------------------------------------------------------------ transitions

    def _enter(self, state: St, seconds: float | None, detail: str = "") -> None:
        self.state = state
        self.state_detail = detail  # sent again to a page that connects later
        self.deadline = None if seconds is None else self.clock() + seconds
        self._send_state(detail)
        self._sync_mic()  # every way into SPEAKING or PAUSED closes the mic, every way out opens it

    def _enter_baseline(self) -> None:
        """Still screen while the bridge records the resting baseline (draft section 3).

        With an acked bridge, wait for its baseline_done (up to baseline.timeout_s); without one
        (keyboard mode) the still screen simply lasts baseline.seconds.
        """
        self.paused_by_decoder = False
        self.baseline_id += 1
        self.expected_baseline = self.baseline_id if self.acked else None
        wait = self.cfg.baseline.timeout_s if self.acked else self.cfg.baseline.seconds
        self._enter(St.BASELINE, wait, "Keep still and look at the screen")
        self._send_ui(Trial(trial_id=self.trial_id, phase="baseline"))
        self._send_decoder(Baseline(baseline_id=self.baseline_id, seconds=self.cfg.baseline.seconds))
        self._send_status()  # resume_ready changes
        self._listen(True, "start")  # turn mode: the partner may speak first (also after a reconnect)

    def _after_baseline(self) -> None:
        """Paradigm code: calibrate first if the bridge has no template (or the page asked)."""
        if self._calibration_due():
            self._calibrate()
        else:
            self._enter_menu_cue()

    def _calibration_due(self) -> bool:
        # Calibration needs an acked bridge: keyboard mode without one goes straight to the page.
        return (self.cfg.stim.paradigm == "code" and self.acked
                and (not self.bridge_trained or self.calibrate_requested))

    def _calibrate(self) -> None:
        """The calibration screen (the left card cued), still for cue_s; then its trial, in which
        the bridge trains the template from the first train_cycles cycles (draft section 9)."""
        self.picked = None
        self._reset_halves()  # after it the page starts whole again
        self.screen = self._calibrate_screen()
        self._enter(St.CALIBRATING, self.cfg.stim.cue_s, CALIBRATING_DETAIL)
        self._send_ui(self.screen.show)
        self._send_ui(Trial(trial_id=self.trial_id + 1, phase="cue"))
        self._send_decoder(self.screen.display)

    def _calibration_s(self) -> float:
        """The calibration trial's deadline: its cycles at the expected refresh, plus max_trial_s."""
        code = self.cfg.stim.code
        return (code.train_cycles * code.cycle_frames / self.cfg.display.expected_refresh_hz
                + self.cfg.stim.max_trial_s)

    def _enter_menu_cue(self, detail: str = "") -> None:
        """Show the next screen (page, confirm, list or halves), still, for its reading time
        before its trial. ``detail``: the state line meanwhile (the calibration's score).

        If an LLM page is on its way, wait for it first in PREPARING (until llm.timeout_s after
        the request); the confirm screen never waits. A calibration the page asked for during the
        session runs first.
        """
        if self.picked is None and self.calibrate_requested and self._calibration_due():
            self._calibrate()
            return
        if self.awaiting is not None and self.picked is None:
            remaining = self.awaiting_until - self.clock()
            if remaining > 0:
                self._enter(St.PREPARING, remaining, FINDING_REPLIES)
                return
            self._give_up_on_llm()
        if self.pending_list is not None and self.picked is None:
            self._set_list(self.pending_list)
            self.pending_list = None
        self.screen = self._build_screen()
        self._enter(St.MENU_CUE, self._cue_seconds(self.screen.show), detail)
        self._send_ui(self.screen.show)
        self._send_ui(Trial(trial_id=self.trial_id + 1, phase="cue"))
        self._send_decoder(self.screen.display)

    def _cue_seconds(self, show: ShowPage) -> float:
        """How long a screen stays still before its trial, so the user can read it before anything
        flickers (gaze that rests on an option while reading would pick it). A new page: cue_s
        plus read_s_per_word for each word on it, up to cue_max_s. The same page again (after
        Didn't catch that or Back), or a narrowed halves screen of it: cue_s. The confirm
        screen: confirm_cue_s."""
        stim = self.cfg.stim
        if show.mode == "confirm":
            return stim.confirm_cue_s
        key = (show.list_id, show.page, show.highlight_index if show.mode == "list" else None)
        if key == self.read_page:
            return stim.cue_s
        self.read_page = key
        if show.mode == "list":
            text = show.text
        elif show.mode == "halves":
            text = " ".join(o.utterance or o.label for o in show.options)
        else:
            text = " ".join(s.utterance or s.label for s in show.slots)
        return min(stim.cue_s + stim.read_s_per_word * len(text.split()), max(stim.cue_s, stim.cue_max_s))

    def _start_trial(self) -> None:
        self.trial_id += 1
        self.active_trial = self.trial_id
        self.selected_slot = None
        self.onset_seen = False
        mode = self.screen.show.mode
        if mode == "calibrate":
            self._enter(St.CALIBRATING, self._calibration_s(), CALIBRATING_DETAIL)
        else:
            state = St.CONFIRMING if mode == "confirm" else St.STIMULATING
            self._enter(state, self.cfg.stim.max_trial_s)
        self._send_decoder(StimStart(trial_id=self.trial_id, active_slots=list(self.screen.roles)))
        if not self.acked or self.cfg.display.flicker == "phone":
            # No bridge (keyboard mode): nothing will report an onset, so show the trial now.
            # The phone starts drawing on this message; its cycle markers are the onset.
            self._send_ui(Trial(trial_id=self.trial_id, phase="flicker"))
        if mode == "calibrate":
            self.train_id += 1
            self.expected_train = self.train_id
            self._send_decoder(Train(train_id=self.train_id, trial_id=self.trial_id,
                                     slot=self.screen.show.cue, cycles=self.cfg.stim.code.train_cycles))

    def _stop_trial(self, reason: str) -> None:
        if self.active_trial is None:
            return
        self._send_decoder(StimStop(trial_id=self.active_trial, reason=reason))
        self.active_trial = None

    def _on_selection(self, trial_id: int | None, slot: str, source: str) -> None:
        if self.state is St.CALIBRATING:
            self._record("calibration_selection", trial_id=trial_id, slot=slot, source=source)
            return  # the calibration trial ends on train_done, never on a selection
        if not self._is_current(trial_id):
            self._record("stale_selection", trial_id=trial_id, slot=slot, source=source)
            return
        if slot not in self.screen.roles:
            self._record("inactive_slot", trial_id=trial_id, slot=slot, source=source)
            return
        if source == "decoder" and self.cfg.stim.paradigm == "code" and not self.onset_seen:
            # Rule 8: nothing flickered yet as far as we know (no cycle 0 from the phone).
            self._record("selection_before_onset", trial_id=trial_id, slot=slot)
            return
        self._stop_trial("selected")
        self.selected_slot = slot
        role = self.screen.roles[slot]
        self._record("selected", trial_id=trial_id, slot=slot, role=role, source=source,
                     screen=self.screen.show.mode)
        self._enter(St.SELECTED, self.cfg.selection.chosen_highlight_s, role)
        self._send_ui(Highlight(slot=slot))
        self._send_decoder(Display(highlight=slot))

    def _after_selected(self) -> None:
        mode = self.screen.show.mode
        role = self.screen.roles[self.selected_slot]
        if mode == "confirm":
            option, self.picked = self.picked, None
            if role == "say":
                self._speak(option.utterance)
            else:
                self._record("confirm_back", utterance=option.utterance)
                self._reset_halves()  # halves mode: back to the whole page
                self._enter_rest()  # back to the same page
        elif mode == "halves":
            self._after_half(role)
        elif mode == "page":
            if role == "option":
                option = self.options.options[self.option_slots.index(self.selected_slot)]
                if self.cfg.selection.confirm_mode == "never":
                    self._speak(option.utterance)
                else:
                    # low_score needs decoder scores and a threshold; until then it confirms too.
                    self.picked = option
                    self._enter_menu_cue()  # the confirm screen
            else:
                self._more_options()
                self._enter_rest()
        else:  # list mode
            if role == "say":
                self._speak(self.options.options[self.index].utterance)
                return
            self.index += 1
            if self.index >= len(self.options.options):
                self._more_options()
            self._enter_rest()

    def _after_half(self, role: str) -> None:
        """Halves mode: keep the chosen half. Several items left: show them (still, cue_s). One
        left: a phrase goes to the confirm screen, "More options" asks for a new page."""
        split = self.screen.show.split
        kept = self.candidates[:split] if role == "left" else self.candidates[split:]
        self.misses = 0
        if len(kept) > 1:
            self.candidates = kept
            self._enter_menu_cue()
            return
        item = kept[0]
        if not item.utterance:  # "More options": nothing to say, so nothing to confirm
            self._more_options()
            self._enter_rest()
        elif self.cfg.selection.confirm_mode == "never":
            self._speak(item.utterance)
        else:
            self.picked = item
            self._enter_menu_cue()  # the confirm screen

    def _halves_items(self) -> list[Option]:
        """Halves mode: every item of the current page, "More options" last."""
        return [*self.options.options, MORE_ITEM]

    def _reset_halves(self) -> None:
        """Halves mode: the whole page is in play again."""
        if self.mode == "halves":
            self.candidates = self._halves_items()
            self.misses = 0

    def _more_options(self) -> None:
        """NEXT for a new page: the "more" page first, then others; twice in a row goes home."""
        if self.pending_list is not None:
            return  # a partner line came in after the pick: its page (or fallback) comes next
        self.more_in_a_row += 1
        if self.more_in_a_row >= 2:
            self.more_in_a_row = 0
            self._set_list(self._fallback(HOME_LIST))
            return
        names = list(self.intents.fallback_lists)
        if MORE_LIST in names and self.options.name != MORE_LIST:
            name = MORE_LIST
        else:
            others = [n for n in names if n != self.options.name]
            name = others[0] if others else HOME_LIST
        if self.request_page is None:
            self._set_list(self._fallback(name))
            return
        self.pending_list = self._fallback(name)  # if the LLM's new page does not come in time
        self._ask_llm("more")

    def _listen(self, on: bool, reason: str) -> None:
        """Tell the partner page to listen (the partner's turn) or to stop (rule: never hear
        itself). Only in ``turn`` mode; in ``push_to_talk`` the partner opens the mic by hand."""
        speech = self.cfg.speech
        if speech.stt_mode == "always" or (on and (speech.stt_mode != "turn" or self.mic_muted)):
            return  # always mode: _sync_mic owns the mic
        self._send_ui(Listen(seconds=speech.listen_window_s if on else 0.0, muted=self.mic_muted,
                             delay_s=speech.stt_tail_s if reason == "turn" else 0.0, reason=reason))

    def _mic_wanted(self) -> bool:
        return not self.mic_muted and self.state not in (St.IDLE, St.PAUSED, St.SPEAKING)

    def _sync_mic(self, force: bool = False, reason: str | None = None) -> None:
        """Always mode: send the mic state when it changes (or to a reconnected page)."""
        if self.cfg.speech.stt_mode != "always":
            return
        on = self._mic_wanted()
        key = (on, self.mic_muted)
        if key == self.mic_key and not force:
            return
        self.mic_key = key
        delay = max(0.0, self.mic_tail_until - self.clock()) if on else 0.0
        if reason is None:
            if on:
                reason = "turn" if delay > 0 else "start"
            elif self.mic_muted:
                reason = "muted"
            else:
                reason = {St.SPEAKING: "speaking", St.PAUSED: "paused"}.get(self.state, "idle")
        self._send_ui(Listen(seconds=0.0, until_stopped=on, delay_s=round(delay, 3),
                             muted=self.mic_muted, reason=reason))

    def _speak(self, text: str) -> None:
        self._listen(False, "speaking")
        self.speaking_id = f"u{next(self._utterance_ids)}"
        utterances = [o.utterance for o in self.options.options]
        # Rank 1 = the option the LLM (or the fallback list) put first; for the log summary.
        self._record("spoken", utterance_id=self.speaking_id, text=text, list=self.options.name,
                     rank=utterances.index(text) + 1 if text in utterances else None)
        self.conversation.user_said(text)
        self._enter(St.SPEAKING, self.cfg.speech.max_speak_s, text)
        url = self.audio_url(text) if self.audio_url is not None else None
        self._send_ui(Speak(text=text, utterance_id=self.speaking_id, audio_url=url))
        self._send_ui(Transcript(who="user", text=text))

    def _finish_speaking(self) -> None:
        self.last_spoken_id, self.speaking_id = self.speaking_id, None
        self.more_in_a_row = 0
        if self.pending_list is None:
            self.pending_list = self._fallback(HOME_LIST)  # V3: the LLM's next page
        self.mic_tail_until = self.clock() + self.cfg.speech.stt_tail_s  # never hear the reply
        self._enter_rest()
        self._listen(True, "turn")  # the partner usually answers now

    def _not_caught(self, why: str) -> None:
        confirming = self.state is St.CONFIRMING
        self._stop_trial(why)  # "no_selection" or "timeout"
        self._record("not_caught", trial_id=self.trial_id, why=why, confirming=confirming)
        if confirming:
            self.picked = None  # a confirm timeout counts as Back: nothing is spoken by default
            self._reset_halves()
        elif self.mode == "halves":
            # One miss repeats the same screen; two in a row on a narrowed one show the whole page.
            self.misses += 1
            if self.misses >= MISSES_TO_RESET and len(self.candidates) < len(self._halves_items()):
                self._record("halves_reset", misses=self.misses)
                self._reset_halves()
        self._enter(St.NOT_CAUGHT, self.cfg.selection.chosen_highlight_s,
                    NOT_CONFIRMED if confirming else DIDNT_CATCH)

    def _enter_rest(self) -> None:
        self._enter(St.REST, self.cfg.stim.rest_s)
        self._send_ui(Trial(trial_id=self.trial_id, phase="rest"))

    def _pause(self, detail: str) -> None:
        self._listen(False, "paused")
        self._stop_trial("paused")
        self.picked = None
        self.expected_baseline = None
        self.expected_train = None  # a train_done that comes later is stale
        self._reset_halves()  # after a pause the page starts whole again
        self._enter(St.PAUSED, None, detail)
        # The mirror's phase (and its selection timer) follows Trial messages, not the state.
        self._send_ui(Trial(trial_id=self.trial_id, phase="paused"))
        self._send_status()  # resume_ready changes

    def _pause_session(self, detail: str, reason: str) -> None:
        """Pause and tell the bridge to keep the flicker off until the next baseline."""
        self._pause(detail)
        self._send_decoder(Pause(reason=reason))

    # ------------------------------------------------------------------- screens

    def _build_screen(self) -> Screen:
        if self.picked is not None:
            return self._confirm_screen(self.picked)
        if self.mode == "page":
            return self._page_screen()
        if self.mode == "halves":
            return self._halves_screen()
        return self._list_screen()

    def _side_slots(self) -> list[UISlot]:
        """Halves mode's two cards, left then right (slots from stim.roles)."""
        stim = self.cfg.stim
        return [UISlot(slot=stim.slot_for(role), role=role, label=SIDE_LABELS[role])
                for role in ("left", "right")]

    def _halves_screen(self) -> Screen:
        """The items still in play: the first ceil(n / 2) on the left card, the rest on the right."""
        items = self.candidates
        split = math.ceil(len(items) / 2)
        left, right = self._side_slots()
        show = ShowPage(list_id=self.options.list_id, page=self.page_no, mode="halves",
                        slots=[left, right], options=list(items), split=split)
        labels = {left.slot: " / ".join(o.label for o in items[:split]),
                  right.slot: " / ".join(o.label for o in items[split:])}
        return Screen(show, Display(text="", labels=labels, highlight=None),
                      {left.slot: "left", right.slot: "right"})

    def _calibrate_screen(self) -> Screen:
        """Paradigm code's calibration: every card flickers, the user looks at the cued one (the
        left card in halves mode, else the first slot)."""
        if self.mode == "halves":
            slots = self._side_slots()
        else:
            slots = [UISlot(slot=s, role=self.roles[s], label="") for s in self.slots]
        cue = slots[0].slot
        show = ShowPage(list_id=CALIBRATE_LIST, page=max(1, self.page_no), mode="calibrate",
                        slots=slots, cue=cue, text=CALIBRATE_TEXT)
        labels = {s.slot: s.label for s in slots}
        return Screen(show, Display(text=CALIBRATE_TEXT, labels=labels, highlight=None),
                      {s.slot: s.role for s in slots})

    def _page_screen(self) -> Screen:
        """An option beside each option slot (in rank order) and NEXT on the next slot."""
        next_slot = self.cfg.stim.slot_for("next")
        slots = [UISlot(slot=slot, role="option", label=opt.label, utterance=opt.utterance)
                 for slot, opt in zip(self.option_slots, self.options.options)]
        slots.append(UISlot(slot=next_slot, role="next", label=NEXT_PAGE_LABEL))
        show = ShowPage(list_id=self.options.list_id, page=self.page_no, mode="page", slots=slots)
        labels = {s.slot: s.utterance or s.label for s in slots}
        return Screen(show, Display(text="", labels=labels, highlight=None),
                      {s.slot: s.role for s in slots})

    def _confirm_screen(self, option: Option) -> Screen:
        """The picked sentence, with only "Say it" and "Back" flickering."""
        say, back = self.cfg.stim.confirm.say, self.cfg.stim.confirm.back
        slots = [UISlot(slot=say, role="say", label=SAY_IT), UISlot(slot=back, role="back", label=BACK)]
        show = ShowPage(list_id=self.options.list_id, page=self.page_no, mode="confirm",
                        slots=slots, text=option.utterance)
        # Clear the labels of the targets that stay still.
        labels = {slot: "" for slot in self.slots} | {say: SAY_IT, back: BACK}
        return Screen(show, Display(text=option.utterance, labels=labels, highlight=None),
                      {say: "say", back: "back"})

    def _list_screen(self) -> Screen:
        """All options with one highlighted; SAY speaks it, NEXT moves the highlight."""
        last = self.index == len(self.options.options) - 1
        slots = [UISlot(slot=slot, role=role,
                        label=LIST_MORE_LABEL if role == "next" and last else LIST_LABELS[role])
                 for slot, role in self.roles.items()]
        option = self.options.options[self.index]
        show = ShowPage(list_id=self.options.list_id, page=self.page_no, mode="list", slots=slots,
                        text=option.utterance, options=self.options.options,
                        highlight_index=self.index)
        return Screen(show, Display(text=option.utterance, labels={s.slot: s.label for s in slots},
                                    highlight=None),
                      {s.slot: s.role for s in slots})

    # ---------------------------------------------------------------------- helpers

    def _is_current(self, trial_id: int | None) -> bool:
        return self.state in TRIAL_STATES and trial_id is not None and trial_id == self.active_trial

    def _fallback(self, name: str) -> OptionList:
        options = [Option(**o.model_dump()) for o in self.intents.fallback_lists[name]]
        return OptionList(list_id=f"m{next(self._list_ids)}", name=name, options=options)

    def _fresh_start(self) -> None:
        """The start button of a newly opened page: a new conversation, from the home page. Lines
        heard before (the room, the last session) no longer shape Claude's pages."""
        self.conversation = Conversation(self.cfg.context.history_turns)
        self.awaiting = None  # a page still on its way for the old conversation is dropped
        self.pending_list = None
        self.picked = None
        self.more_in_a_row = 0
        self._set_list(self._fallback(HOME_LIST))
        self._record("fresh_start")

    def _set_list(self, options: OptionList) -> None:
        self.options, self.index = options, 0
        self._reset_halves()
        self.page_no += 1
        self.conversation.showed([o.utterance for o in options.options])
        if self.on_page is not None:
            self.on_page([o.utterance for o in options.options])

    def status(self) -> UIStatus:
        return UIStatus(
            decoder_connected=self.decoder_connected,
            decoder_ok=self.acked and self.bridge_ok,
            # With the phone drawing, its refresh check counts as the display's too.
            display_ok=self.acked and self.display_ok and not self._phone_problem(),
            board_ok=self.acked and self.board_ok,
            decoder_message=(f"Rejected: {self.decoder_error}" if self.decoder_error
                             else self.bridge_message),
            resume_ready=self.state in (St.IDLE, St.PAUSED) and not self.resume_problem(),
            llm_ok=self.request_page is not None and self.llm_ok is True,
            llm=("off" if self.request_page is None else "waiting" if self.llm_ok is None
                 else "ok" if self.llm_ok else "failed"),
            fake_decoder=self.decoder_fake,
            decoder_name=self.decoder_name,
            keyboard_mode=self.keyboard_mode,
            mic_muted=self.mic_muted,
            mic=self.mic_report,
            trained=self.bridge_trained,
            calibration_accuracy=self.calibration_accuracy,
        )

    def _send_status(self) -> None:
        self._send_ui(self.status())

    def stimulus(self) -> Stimulus | None:
        """What the phone draws (paradigm code), once an acked hello brought the code."""
        if self.cfg.stim.paradigm != "code" or self.code is None:
            return None
        return Stimulus(refresh_hz=self.cfg.display.expected_refresh_hz,
                        frames_per_bit=self.code.frames_per_bit, bits=self.code.bits,
                        lags={s.slot: s.lag_frames for s in self.cfg.stim.slots},
                        depth=self.cfg.display.depth)

    def _send_stimulus(self) -> None:
        stimulus = self.stimulus()
        if stimulus is not None:
            self._send_ui(stimulus)

    def _page_flickers(self) -> bool:
        """The connected page (Chrome or the phone) draws the flicker the user looks at."""
        return self.cfg.display.flicker in ("browser", "phone")

    def _send_state(self, detail: str = "") -> None:
        self._send_ui(State(name=self.state.value, detail=detail))

    def _send_ui(self, msg: Message) -> None:
        # PLAN rule 3: the shown options never change during a trial.
        if isinstance(msg, ShowPage) and (self.state in TRIAL_STATES or (
                self.state is St.CALIBRATING and self.active_trial is not None)):
            assert msg == self.shown, "show_page changed during a trial"
        # The app never hears itself, and Mute means muted.
        if isinstance(msg, Listen) and (msg.until_stopped or msg.seconds > 0):
            assert self.state is not St.SPEAKING and not self.mic_muted, "mic opened while speaking or muted"
        self._record("to_ui", message=msg)
        self._send_ui_cb(msg)

    def _send_decoder(self, msg: Message) -> None:
        # Draft rule 3: the bridge acts on nothing before hello_ack, so nothing is sent before it.
        if not self.acked and not isinstance(msg, (HelloAck, ProtocolError)):
            self._record("not_sent_before_ack", message=msg)
            return
        # Draft rule 5: the display never changes during a trial (a highlight comes after stim_stop).
        if isinstance(msg, Display) and self.active_trial is not None:
            raise AssertionError("display sent during a trial")
        self._record("to_decoder", message=msg)
        self._send_decoder_cb(msg)

    def _record(self, event: str, **fields: object) -> None:
        if self.log is not None:
            self.log.log(event, state=self.state.value, **fields)


def is_question(text: str) -> bool:
    """A question, typed or spoken: any sentence that is one ("It might rain. Do you want tea")."""
    if "?" in text:
        return True
    return any(_sentence_is_question(s) for s in re.split(r"(?<=[.!;])\s+", text))


def _sentence_is_question(text: str) -> bool:
    """Speech-to-text adds no "?", so the first word decides."""
    raw = text.split()
    if len(raw) > 1 and raw[0].endswith(","):  # a name first: "Ana, do you want tea"
        raw = raw[1:]
    words = [w for w in (r.strip(".,!;:").lower() for r in raw) if w]
    while words and words[0] in FILLER:  # "So, are you hungry"
        words.pop(0)
    return bool(words) and words[0] in QUESTION_STARTS
