"""Server-side state machine (PLAN.md section 7), scan mode: one target says the shown option,
the other moves to the next one.

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
    ShowOption,
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
HOME_LIST = "home"
QUESTION_LIST = "yes_no"
ROLE_LABELS = {"say": "SAY", "next": "NEXT"}
MORE_LABEL = "MORE"  # NEXT on the last option of a list asks for new options


class St(str, Enum):
    IDLE = "IDLE"
    BASELINE = "BASELINE"
    MENU_CUE = "MENU_CUE"
    STIMULATING = "STIMULATING"
    SELECTED = "SELECTED"
    SPEAKING = "SPEAKING"
    REST = "REST"
    NOT_CAUGHT = "NOT_CAUGHT"
    PAUSED = "PAUSED"


@dataclass
class OptionList:
    list_id: str
    name: str  # fallback list name, later "llm"
    options: list[Option]


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
    ) -> None:
        self.cfg = cfg
        self.intents = intents
        self._send_ui_cb = send_ui
        self._send_decoder_cb = send_decoder
        self.clock = clock
        self.log = log
        self.keyboard_mode = keyboard_mode
        self.fake_decoder = fake_decoder

        self.slots = [s.slot for s in cfg.stim.slots]
        self.roles = dict(cfg.stim.roles)
        self.state = St.IDLE
        self.deadline: float | None = None

        self.trial_id = 0
        self.active_trial: int | None = None
        self.selected_slot: str | None = None
        self.shown: ShowOption | None = None  # what the user sees; fixed for the whole trial

        self._list_ids = itertools.count(1)
        self._utterance_ids = itertools.count(1)
        self.options = self._fallback(HOME_LIST)
        self.index = 0
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
        elif self.state is St.STIMULATING:
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
        last = self.shown is not None and self.shown.index == self.shown.count - 1
        self._send_decoder(HelloAck(
            protocol=PROTOCOL,
            roles=self.roles,
            labels={slot: self._label(role, last) for slot, role in self.roles.items()},
        ))
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
        key = key.lower()
        if key.isdigit() and self.keyboard_mode:
            n = int(key)
            if 1 <= n <= len(self.slots) and self.state is St.STIMULATING:
                self._on_selection(self.active_trial, self.slots[n - 1], source="keyboard")
        elif key in ("c", "w") and self.last_spoken_id is not None:
            self._record("judgment", utterance_id=self.last_spoken_id, correct=key == "c")

    def _on_partner_text(self, text: str) -> None:
        self._send_ui(Transcript(who="partner", text=text))
        name = QUESTION_LIST if text.rstrip().endswith("?") else HOME_LIST
        self.pending_list = self._fallback(name)
        self.more_in_a_row = 0
        if self.state is St.STIMULATING:
            self._stop_trial("context_changed")
            self._enter_menu_cue()
        elif self.state is St.MENU_CUE:
            self._enter_menu_cue()  # not in a trial yet, so the option may still change
        # In every other state the new list is shown at the next MENU_CUE.

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
        if self.pending_list is not None:
            self.options, self.pending_list, self.index = self.pending_list, None, 0
        option = self.options.options[self.index]
        last = self.index == len(self.options.options) - 1
        slots = [
            UISlot(slot=slot, role=self.roles[slot], label=self._label(self.roles[slot], last))
            for slot in self.slots
        ]
        self.shown = ShowOption(
            list_id=self.options.list_id,
            index=self.index,
            count=len(self.options.options),
            label=option.label,
            utterance=option.utterance,
            slots=slots,
        )
        self._enter(St.MENU_CUE, self.cfg.stim.cue_s)
        self._send_ui(self.shown)
        self._send_ui(Trial(trial_id=self.trial_id + 1, phase="cue"))
        self._send_decoder(Display(text=option.utterance, labels={s.slot: s.label for s in slots},
                                   highlight=None))

    def _start_trial(self) -> None:
        self.trial_id += 1
        self.active_trial = self.trial_id
        self.selected_slot = None
        self._enter(St.STIMULATING, self.cfg.stim.max_trial_s)
        self._send_decoder(
            StimStart(trial_id=self.trial_id, active_slots=list(self.slots))
        )
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
        if slot not in self.slots:
            self._record("unknown_slot", trial_id=trial_id, slot=slot, source=source)
            return
        self._stop_trial("selected")
        self.selected_slot = slot
        self._record("selected", trial_id=trial_id, slot=slot, role=self.roles[slot], source=source)
        self._enter(St.SELECTED, self.cfg.selection.chosen_highlight_s, self.roles[slot])
        self._send_ui(Highlight(slot=slot))
        self._send_decoder(Display(highlight=slot))

    def _after_selected(self) -> None:
        if self.roles[self.selected_slot] == "say":
            self._speak(self.options.options[self.index].utterance)
            return
        self.index += 1
        if self.index >= len(self.options.options):
            self._more_options()
        self._enter_rest()

    def _more_options(self) -> None:
        """NEXT past the last option: a different list; twice in a row goes back home."""
        self.more_in_a_row += 1
        if self.more_in_a_row >= 2:
            self.more_in_a_row = 0
            self.options = self._fallback(HOME_LIST)
        else:
            others = [n for n in self.intents.fallback_lists if n != self.options.name]
            self.options = self._fallback(others[0] if others else HOME_LIST)
        self.index = 0

    def _speak(self, text: str) -> None:
        self.speaking_id = f"u{next(self._utterance_ids)}"
        self._enter(St.SPEAKING, self.cfg.speech.max_speak_s, text)
        self._send_ui(Speak(text=text, utterance_id=self.speaking_id))
        self._send_ui(Transcript(who="user", text=text))

    def _finish_speaking(self) -> None:
        self.last_spoken_id, self.speaking_id = self.speaking_id, None
        self.more_in_a_row = 0
        if self.pending_list is None:
            self.pending_list = self._fallback(HOME_LIST)  # V3: the LLM's next list
        self._enter_rest()

    def _not_caught(self, why: str) -> None:
        self._stop_trial(why)  # "no_selection" or "timeout"
        self._record("not_caught", trial_id=self.trial_id, why=why)
        self._enter(St.NOT_CAUGHT, self.cfg.selection.chosen_highlight_s, DIDNT_CATCH)

    def _enter_rest(self) -> None:
        self._enter(St.REST, self.cfg.stim.rest_s)
        self._send_ui(Trial(trial_id=self.trial_id, phase="rest"))

    def _pause(self, detail: str) -> None:
        self._stop_trial("paused")
        self.expected_baseline = None
        self._enter(St.PAUSED, None, detail)
        self._send_status()  # resume_ready changes

    def _pause_session(self, detail: str, reason: str) -> None:
        """Pause and tell the bridge to keep the flicker off until the next baseline."""
        self._pause(detail)
        self._send_decoder(Pause(reason=reason))

    # ---------------------------------------------------------------------- helpers

    def _is_current(self, trial_id: int | None) -> bool:
        return self.state is St.STIMULATING and trial_id is not None and trial_id == self.active_trial

    def _fallback(self, name: str) -> OptionList:
        options = [Option(**o.model_dump()) for o in self.intents.fallback_lists[name]]
        return OptionList(list_id=f"m{next(self._list_ids)}", name=name, options=options)

    @staticmethod
    def _label(role: str, last_option: bool) -> str:
        return MORE_LABEL if role == "next" and last_option else ROLE_LABELS[role]

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
        # PLAN rule 3: the shown option never changes during a trial.
        if isinstance(msg, ShowOption) and self.state is St.STIMULATING:
            assert msg == self.shown, "show_option changed during a trial"
        self._record("to_ui", message=msg)
        self._send_ui_cb(msg)

    def _send_decoder(self, msg: Message) -> None:
        # Draft rule 3: the bridge acts on nothing before hello_ack, so nothing is sent before it.
        if not self.acked and not isinstance(msg, (HelloAck, ProtocolError)):
            self._record("not_sent_before_ack", message=msg)
            return
        self._record("to_decoder", message=msg)
        self._send_decoder_cb(msg)

    def _record(self, event: str, **fields: object) -> None:
        if self.log is not None:
            self.log.log(event, state=self.state.value, **fields)
