"""Server-side state machine (PLAN.md section 7).

Page mode (default): 3 reply options, each on its own flickering target, plus NEXT on a fourth
(a diamond on the user's screen). Looking at an option opens a confirm screen with only two
targets, "Say it" and "Back"; only "Say it" speaks. List mode (the 2-target fallback): all
options listed with one highlighted, SAY speaks it and NEXT moves the highlight.

The orchestrator owns all state. It is driven by events (decoder and browser messages, plus
``tick()``), reads time from an injectable clock and sends messages through two callbacks, so tests
run it without timers, sockets or threads. Pages of options come from the LLM (V3: a
``request_page`` hook, answered by ``on_page_ready``) after each partner line and for "More
options"; the next MENU_CUE waits for it in PREPARING for up to ``llm.timeout_s``, then uses the
fallback lists in ``config/intents.yaml``. The decoder side speaks protocol 2
(INTERFACE-v2-draft.md).
"""

from __future__ import annotations

import itertools
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
    DecoderHello,
    DecoderStatus,
    Display,
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
    StimStart,
    StimStop,
    Stop,
    Transcript,
    Trial,
    TTSDone,
    UISlot,
    UIStatus,
    hello_problems,
)

Send = Callable[[Message], None]

DIDNT_CATCH = "Didn't catch that"
NOT_CONFIRMED = "Not confirmed: nothing was said"
HOME_LIST = "home"
QUESTION_LIST = "yes_no"
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


class St(str, Enum):
    IDLE = "IDLE"
    BASELINE = "BASELINE"
    PREPARING = "PREPARING"  # waiting for the LLM's page (up to llm.timeout_s), screen still
    MENU_CUE = "MENU_CUE"
    STIMULATING = "STIMULATING"  # a page or list trial
    CONFIRMING = "CONFIRMING"  # the confirm trial: Say it / Back
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
        # The last LLM page arrived in time and valid; False until one has (no badge before proof).
        self.llm_ok = False

        self.mode = cfg.menus.mode
        self.slots = [s.slot for s in cfg.stim.slots]
        self.roles = dict(cfg.stim.roles)
        self.option_slots = [slot for slot in self.slots if self.roles[slot] == "option"]
        self.state = St.IDLE
        self.deadline: float | None = None

        self.trial_id = 0
        self.active_trial: int | None = None
        self.selected_slot: str | None = None
        self.screen: Screen | None = None  # fixed from MENU_CUE until the trial ends

        self._list_ids = itertools.count(1)
        self._utterance_ids = itertools.count(1)
        self.page_no = 0  # counts pages shown; _set_list makes the first one page 1
        self.index = 0  # list mode: the highlighted option
        self._set_list(self._fallback(HOME_LIST))
        self.picked: Option | None = None  # page mode: the option waiting for "Say it"
        self.pending_list: OptionList | None = None
        self.more_in_a_row = 0

        self.speaking_id: str | None = None
        self.last_spoken_id: str | None = None

        self.decoder_connected = False
        self.acked = False  # the bridge's hello passed the check (hello_ack sent)
        self.decoder_error = ""  # why the last hello was rejected
        self.paused_by_decoder = False
        # Last bridge status; assumed fine until a status says otherwise.
        self.bridge_ok = self.board_ok = self.display_ok = True
        self.bridge_message = ""
        self.baseline_id = 0
        self.expected_baseline: int | None = None  # baseline_id we wait for, if a bridge records it

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
                self._enter_menu_cue()  # no bridge recording it: the still screen was the baseline
            else:
                self._record("baseline_timeout", baseline_id=self.expected_baseline)
                self._pause_session(f"Baseline failed: no baseline_done within "
                                    f"{self.cfg.baseline.timeout_s:g} s", "baseline_failed")
        elif self.state is St.PREPARING:
            self._give_up_on_llm()
            self._enter_menu_cue()  # with the fallback list
        elif self.state is St.MENU_CUE:
            self._start_trial()
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
        self.bridge_ok = self.board_ok = self.display_ok = True
        self.bridge_message = ""
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
            if self._is_current(msg.trial_id):
                self._send_ui(Trial(trial_id=msg.trial_id, phase="flicker"))

    def _on_hello(self, hello: DecoderHello) -> None:
        """Check the bridge's hello against config: hello_ack, or error and stay paused."""
        problems = hello_problems(hello, self.cfg.stim.slots, self.cfg.stim.waveform,
                                  self.cfg.display.expected_refresh_hz)
        if problems:
            self.acked = False
            self.decoder_error = "; ".join(problems)
            self._send_decoder(ProtocolError(message=self.decoder_error))
            self._send_status()
            if self.state not in (St.IDLE, St.PAUSED):
                self._pause(f"Decoder rejected: {self.decoder_error}")
            return
        self.acked = True
        self.decoder_error = ""
        screen = self.screen or self._build_screen()
        self._send_decoder(HelloAck(protocol=PROTOCOL, roles=self.roles,
                                    labels=screen.display.labels or {}))
        self._send_status()
        if self.state is St.PAUSED and self.paused_by_decoder and self.display_ok:
            self._enter_baseline()

    def _on_bridge_status(self, msg: DecoderStatus) -> None:
        display_lost = self.display_ok and not msg.display_ok
        changed = (msg.ok, msg.board_ok, msg.display_ok, msg.message) != (
            self.bridge_ok, self.board_ok, self.display_ok, self.bridge_message)
        self.bridge_ok, self.board_ok, self.display_ok = msg.ok, msg.board_ok, msg.display_ok
        self.bridge_message = msg.message
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
            self._enter_menu_cue()
        else:
            self._pause_session(f"Baseline failed: {msg.message or 'no reason given'}",
                                "baseline_failed")

    def resume_problem(self) -> str:
        """Why Resume would not start a session now; empty if it would."""
        if self.keyboard_mode and not self.acked:
            return ""  # keyboard mode runs without a bridge
        if self.decoder_error:
            return f"Decoder rejected: {self.decoder_error}"
        if not self.acked:
            return "Waiting for the decoder"
        if not self.display_ok:
            return "Display closed: restart it, then Resume"
        return ""

    # ---------------------------------------------------------------- events: browser

    def on_ui_connected(self) -> None:
        """A partner page connected: bring it up to date."""
        self._record("ui_connected")
        self._send_status()
        if self.shown is not None:
            self._send_ui(self.shown)
        self._send_state()
        # A reloaded or reconnected page gets the mic state again and resumes by itself.
        if self.cfg.speech.stt_mode == "always":
            self._sync_mic(force=True, reason="reconnect")
        else:
            self._send_ui(Listen(seconds=0.0, muted=self.mic_muted, reason="reconnect"))

    def on_ui_message(self, msg: Message) -> None:
        self._record("from_ui", message=msg)
        if isinstance(msg, Resume):
            if self.state in (St.IDLE, St.PAUSED):
                problem = self.resume_problem()
                if problem:
                    self._send_state(problem)  # stay put; the page shows why
                else:
                    self._enter_baseline()
        elif isinstance(msg, Stop):
            if self.state not in (St.IDLE, St.PAUSED):
                self._pause("Stopped")
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
        name = QUESTION_LIST if question else HOME_LIST
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

    def _enter_menu_cue(self) -> None:
        """Show the next screen (page, confirm or list), still, for cue_s before its trial.

        If an LLM page is on its way, wait for it first in PREPARING (until llm.timeout_s after
        the request); the confirm screen never waits.
        """
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
        self._enter(St.MENU_CUE, self.cfg.stim.cue_s)
        self._send_ui(self.screen.show)
        self._send_ui(Trial(trial_id=self.trial_id + 1, phase="cue"))
        self._send_decoder(self.screen.display)

    def _start_trial(self) -> None:
        self.trial_id += 1
        self.active_trial = self.trial_id
        self.selected_slot = None
        state = St.CONFIRMING if self.screen.show.mode == "confirm" else St.STIMULATING
        self._enter(state, self.cfg.stim.max_trial_s)
        self._send_decoder(StimStart(trial_id=self.trial_id, active_slots=list(self.screen.roles)))
        if not self.acked:
            # No bridge (keyboard mode): nothing will report an onset, so show the trial now.
            self._send_ui(Trial(trial_id=self.trial_id, phase="flicker"))

    def _stop_trial(self, reason: str) -> None:
        if self.active_trial is None:
            return
        self._send_decoder(StimStop(trial_id=self.active_trial, reason=reason))
        self.active_trial = None

    def _on_selection(self, trial_id: int | None, slot: str, source: str) -> None:
        if not self._is_current(trial_id):
            self._record("stale_selection", trial_id=trial_id, slot=slot, source=source)
            return
        if slot not in self.screen.roles:
            self._record("inactive_slot", trial_id=trial_id, slot=slot, source=source)
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
                self._enter_rest()  # back to the same page
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
        self._enter(St.PAUSED, None, detail)
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
        return self._list_screen()

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

    def _set_list(self, options: OptionList) -> None:
        self.options, self.index = options, 0
        self.page_no += 1
        self.conversation.showed([o.utterance for o in options.options])
        if self.on_page is not None:
            self.on_page([o.utterance for o in options.options])

    def status(self) -> UIStatus:
        return UIStatus(
            decoder_connected=self.decoder_connected,
            decoder_ok=self.acked and self.bridge_ok,
            display_ok=self.acked and self.display_ok,
            board_ok=self.acked and self.board_ok,
            decoder_message=(f"Rejected: {self.decoder_error}" if self.decoder_error
                             else self.bridge_message),
            resume_ready=self.state in (St.IDLE, St.PAUSED) and not self.resume_problem(),
            llm_ok=self.request_page is not None and self.llm_ok,
            fake_decoder=self.fake_decoder,
            keyboard_mode=self.keyboard_mode,
            mic_muted=self.mic_muted,
            mic=self.mic_report,
        )

    def _send_status(self) -> None:
        self._send_ui(self.status())

    def _send_state(self, detail: str = "") -> None:
        self._send_ui(State(name=self.state.value, detail=detail))

    def _send_ui(self, msg: Message) -> None:
        # PLAN rule 3: the shown options never change during a trial.
        if isinstance(msg, ShowPage) and self.state in TRIAL_STATES:
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
