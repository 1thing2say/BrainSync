"""Server-side state machine (PLAN.md section 7).

Page mode (default): 3 reply options, each on its own flickering target, plus NEXT on a fourth
(a diamond on the user's screen). Looking at an option opens a confirm screen with only two
targets, "Say it" and "Back"; only "Say it" speaks. List mode (the 2-target fallback): all
options listed with one highlighted, SAY speaks it and NEXT moves the highlight.

The orchestrator owns all state. It is driven by events (decoder and browser messages, plus
``tick()``), reads time from an injectable clock and sends messages through two callbacks, so tests
run it without timers, sockets or threads. Option lists come from the fallback lists in
``config/intents.yaml`` until the LLM menus land (V3). The decoder side speaks protocol 2
(INTERFACE-v2-draft.md).
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.config import AppConfig, IntentsConfig
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
NEXT_PAGE_LABEL = "More options"
SAY_IT, BACK = "Say it", "Back"
LIST_LABELS = {"say": "SAY", "next": "NEXT"}
LIST_MORE_LABEL = "MORE"  # list mode: NEXT on the last option asks for new options


class St(str, Enum):
    IDLE = "IDLE"
    BASELINE = "BASELINE"
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
    name: str  # fallback list name, later "llm"
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
    ) -> None:
        """``on_page(utterances)`` runs whenever a new page of options is set (TTS prefetch);
        ``audio_url(text)`` returns a ready clip for ``text`` or None (the browser speaks it)."""
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
            self._on_partner_text(msg.text)
        elif isinstance(msg, TTSDone):
            if self.state is St.SPEAKING and msg.utterance_id == self.speaking_id:
                self._finish_speaking()
        elif isinstance(msg, Key):
            self._on_key(msg.key)
        elif isinstance(msg, Judgment):
            pass  # logged above; read by the log summary

    def _on_key(self, key: str) -> None:
        """Keyboard mode: digit n selects slot n (1 = top, 4 = bottom) if it flickers now."""
        key = key.lower()
        if key.isdigit() and self.keyboard_mode:
            n = int(key)
            if 1 <= n <= len(self.slots) and self.state in TRIAL_STATES:
                self._on_selection(self.active_trial, self.slots[n - 1], source="keyboard")
        elif key in ("c", "w") and self.last_spoken_id is not None:
            self._record("judgment", utterance_id=self.last_spoken_id, correct=key == "c")

    def _on_partner_text(self, text: str) -> None:
        self._send_ui(Transcript(who="partner", text=text))
        name = QUESTION_LIST if text.rstrip().endswith("?") else HOME_LIST
        self.pending_list = self._fallback(name)
        self.more_in_a_row = 0
        if self.state in TRIAL_STATES:
            self._stop_trial("context_changed")
            self.picked = None
            self._enter_menu_cue()
        elif self.state is St.MENU_CUE:
            self.picked = None
            self._enter_menu_cue()  # not in a trial yet, so the screen may still change
        # In every other state the new page is shown at the next MENU_CUE.

    # ------------------------------------------------------------------ transitions

    def _enter(self, state: St, seconds: float | None, detail: str = "") -> None:
        self.state = state
        self.deadline = None if seconds is None else self.clock() + seconds
        self._send_state(detail)

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

    def _enter_menu_cue(self) -> None:
        """Show the next screen (page, confirm or list), still, for cue_s before its trial."""
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
        self._set_list(self._fallback(name))

    def _speak(self, text: str) -> None:
        self.speaking_id = f"u{next(self._utterance_ids)}"
        self._enter(St.SPEAKING, self.cfg.speech.max_speak_s, text)
        url = self.audio_url(text) if self.audio_url is not None else None
        self._send_ui(Speak(text=text, utterance_id=self.speaking_id, audio_url=url))
        self._send_ui(Transcript(who="user", text=text))

    def _finish_speaking(self) -> None:
        self.last_spoken_id, self.speaking_id = self.speaking_id, None
        self.more_in_a_row = 0
        if self.pending_list is None:
            self.pending_list = self._fallback(HOME_LIST)  # V3: the LLM's next page
        self._enter_rest()

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
            llm_ok=False,  # no LLM until V3
            fake_decoder=self.fake_decoder,
            keyboard_mode=self.keyboard_mode,
        )

    def _send_status(self) -> None:
        self._send_ui(self.status())

    def _send_state(self, detail: str = "") -> None:
        self._send_ui(State(name=self.state.value, detail=detail))

    def _send_ui(self, msg: Message) -> None:
        # PLAN rule 3: the shown options never change during a trial.
        if isinstance(msg, ShowPage) and self.state in TRIAL_STATES:
            assert msg == self.shown, "show_page changed during a trial"
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
