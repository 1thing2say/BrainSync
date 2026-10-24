"""Orchestrator transitions with a fake clock: no timers, sockets or threads (PLAN.md section 9).

Page mode (the default config): 3 options on s1-s3 and NEXT on s4, a confirm screen with "Say it"
on s1 and "Back" on s4. List mode (the 2-target fallback): SAY on s1, NEXT on s2.
"""

from __future__ import annotations

import random

import pytest

from app.config import load_config, load_intents
from app.orchestrator import DIDNT_CATCH, NOT_CONFIRMED, Orchestrator, St
from app.schemas import (
    PROTOCOL,
    Baseline,
    BaselineDone,
    DecoderHello,
    DecoderStatus,
    Display,
    HelloAck,
    Highlight,
    Key,
    NoSelection,
    PartnerText,
    Pause,
    ProtocolError,
    Resume,
    Selection,
    ShowPage,
    SlotSpec,
    Speak,
    State,
    StimOnset,
    StimStart,
    StimStop,
    Stop,
    Transcript,
    Trial,
    TTSDone,
    UIStatus,
)

CFG = load_config()
INTENTS = load_intents()
HOME = INTENTS.fallback_lists["home"]
YES_NO = INTENTS.fallback_lists["yes_no"]
MORE = INTENTS.fallback_lists["more"]
OPTION_SLOTS, NEXT = ["s1", "s2", "s3"], "s4"
SAY_IT, BACK = CFG.stim.confirm.say, CFG.stim.confirm.back  # s1, s4

# List mode: two slots, SAY / NEXT, no confirm step.
CFG_LIST = CFG.model_copy(update={
    "stim": CFG.stim.model_copy(update={
        "slots": CFG.stim.slots[:2], "roles": {"s1": "say", "s2": "next"}}),
    "menus": CFG.menus.model_copy(update={"mode": "list"}),
    "selection": CFG.selection.model_copy(update={"confirm_mode": "never"}),
})
CFG_NO_CONFIRM = CFG.model_copy(update={
    "selection": CFG.selection.model_copy(update={"confirm_mode": "never"})})


def bridge_hello(cfg=CFG, **changes) -> DecoderHello:
    """A hello that matches ``cfg``, as the teammate's bridge would send it."""
    fields = dict(protocol=PROTOCOL, name="bridge", refresh_hz=59.94, waveform="sine",
                  slots=[SlotSpec(slot=s.slot, freq_hz=s.freq_hz) for s in cfg.stim.slots])
    fields.update(changes)
    return DecoderHello(**fields)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


class Rig:
    """An orchestrator plus everything it sent, and helpers to move time forward.

    By default a bridge is connected and acked, and ``to_trial`` answers its baselines; pass
    ``bridge=False`` for a session without one.
    """

    def __init__(self, cfg=CFG, keyboard_mode: bool = False, bridge: bool = True) -> None:
        self.cfg = cfg
        self.clock = Clock()
        self.ui: list = []
        self.decoder: list = []
        self.o = Orchestrator(cfg, INTENTS, self.ui.append, self.decoder.append, clock=self.clock,
                              keyboard_mode=keyboard_mode)
        if bridge:
            self.connect()

    def connect(self, **hello_changes) -> None:
        self.o.on_decoder_connected()
        self.o.on_decoder_message(bridge_hello(self.cfg, **hello_changes))

    def finish_baseline(self, ok: bool = True, message: str = "") -> None:
        assert self.o.state is St.BASELINE and self.o.expected_baseline is not None
        self.o.on_decoder_message(BaselineDone(baseline_id=self.o.expected_baseline, ok=ok,
                                               message=message))

    def status(self, **flags) -> None:
        fields = dict(ok=True, board_ok=True, display_ok=True, message="")
        fields.update(flags)
        self.o.on_decoder_message(DecoderStatus(**fields))

    def advance(self, seconds: float) -> None:
        self.clock.t += seconds
        self.o.tick()

    def run_out(self) -> None:
        """Advance exactly to the pending deadline."""
        assert self.o.deadline is not None, f"no deadline in {self.o.state}"
        self.clock.t = self.o.deadline
        self.o.tick()

    def to_trial(self) -> StimStart | None:
        """From IDLE / PAUSED / any wait to the next trial; returns its stim_start (None if no bridge)."""
        if self.o.state in (St.IDLE, St.PAUSED):
            self.o.on_ui_message(Resume())
        while self.o.state not in (St.STIMULATING, St.CONFIRMING):
            if self.o.state is St.BASELINE and self.o.expected_baseline is not None:
                self.finish_baseline()
            else:
                self.run_out()
        return next((m for m in reversed(self.decoder) if isinstance(m, StimStart)), None)

    def select(self, slot: str) -> None:
        self.o.on_decoder_message(Selection(trial_id=self.o.active_trial or -1, slot=slot,
                                            t_unix=self.clock.t))

    def pick_and_confirm_screen(self, slot: str) -> ShowPage:
        """Page trial -> pick ``slot`` -> the confirm screen's cue; returns the confirm show_page."""
        self.to_trial()
        self.select(slot)
        self.run_out()  # SELECTED -> MENU_CUE with the confirm screen
        assert self.o.state is St.MENU_CUE
        return self.last(self.ui, ShowPage)

    @staticmethod
    def last(msgs: list, kind: type):
        found = [m for m in msgs if isinstance(m, kind)]
        assert found, f"no {kind.__name__} sent"
        return found[-1]

    def spoken(self) -> list[str]:
        return [m.text for m in self.ui if isinstance(m, Speak)]


# ---------------------------------------------------------------------------
# Handshake, baseline, status (mode-independent)
# ---------------------------------------------------------------------------


def test_matching_hello_gets_hello_ack_with_roles_and_labels():
    r = Rig()
    ack = r.last(r.decoder, HelloAck)
    assert ack.protocol == PROTOCOL
    assert ack.roles == {"s1": "option", "s2": "option", "s3": "option", "s4": "next"}
    # The initial labels are the first page: each option's sentence, and "More options".
    assert ack.labels == {"s1": HOME[0].utterance, "s2": HOME[1].utterance,
                          "s3": HOME[2].utterance, "s4": "More options"}
    assert r.last(r.ui, UIStatus).decoder_ok


@pytest.mark.parametrize("changes, fragment", [
    ({"refresh_hz": 120.0}, "ProMotion"),
    ({"waveform": "square"}, "waveform square"),
    ({"slots": [SlotSpec(slot="s1", freq_hz=15.0), SlotSpec(slot="s2", freq_hz=17.0)]}, "slots"),
    ({"protocol": 1}, "protocol 1"),
])
def test_mismatched_hello_gets_error_and_pauses(changes, fragment):
    r = Rig()
    r.to_trial()
    r.o.on_decoder_message(bridge_hello(**changes))  # the bridge says hello again, now wrong
    assert fragment in r.last(r.decoder, ProtocolError).message
    assert len([m for m in r.decoder if isinstance(m, HelloAck)]) == 1  # only the first hello
    assert r.o.state is St.PAUSED
    assert fragment in r.last(r.ui, State).detail
    assert not r.last(r.ui, UIStatus).decoder_ok


def test_status_cannot_override_a_rejected_hello():
    r = Rig(bridge=False)
    r.connect(refresh_hz=120.0)
    r.status()
    assert not r.last(r.ui, UIStatus).decoder_ok


def test_nothing_is_sent_to_the_bridge_before_hello_ack():
    r = Rig(bridge=False)
    r.connect(refresh_hz=120.0)
    r.o.on_ui_message(Resume())
    r.o.on_ui_message(Stop())
    assert [type(m) for m in r.decoder] == [ProtocolError]
    assert r.o.state is St.IDLE
    assert "Decoder rejected" in r.last(r.ui, State).detail
    assert not r.last(r.ui, UIStatus).resume_ready


def test_resume_waits_for_a_bridge_unless_keyboard_mode():
    r = Rig(bridge=False)
    r.o.on_ui_message(Resume())
    assert r.o.state is St.IDLE and r.last(r.ui, State).detail == "Waiting for the decoder"
    k = Rig(bridge=False, keyboard_mode=True)
    k.o.on_ui_message(Resume())
    assert k.o.state is St.BASELINE and k.o.expected_baseline is None
    k.advance(CFG.baseline.seconds)  # no bridge: the still screen is timed
    assert k.o.state is St.MENU_CUE


def test_baseline_waits_for_baseline_done_with_the_right_id():
    r = Rig()
    r.o.on_ui_message(Resume())
    sent = r.last(r.decoder, Baseline)
    r.advance(CFG.baseline.seconds + 1)
    assert r.o.state is St.BASELINE  # the timer alone does not end it
    r.o.on_decoder_message(BaselineDone(baseline_id=sent.baseline_id - 1, ok=True))
    assert r.o.state is St.BASELINE  # stale id ignored
    r.o.on_decoder_message(BaselineDone(baseline_id=sent.baseline_id, ok=True))
    assert r.o.state is St.MENU_CUE


def test_failed_baseline_pauses_with_the_message():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.finish_baseline(ok=False, message="O1 contact lost")
    assert r.o.state is St.PAUSED
    assert r.last(r.ui, State).detail == "Baseline failed: O1 contact lost"
    assert isinstance(r.decoder[-1], Pause)
    assert r.last(r.ui, UIStatus).resume_ready
    r.o.on_ui_message(Resume())
    assert r.o.state is St.BASELINE and r.o.expected_baseline == 2


def test_missing_baseline_done_times_out_and_pauses():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.advance(CFG.baseline.timeout_s)
    assert r.o.state is St.PAUSED
    assert "no baseline_done" in r.last(r.ui, State).detail
    r.o.on_decoder_message(BaselineDone(baseline_id=1, ok=True))  # too late: ignored
    assert r.o.state is St.PAUSED


def test_stop_during_baseline_forgets_it():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.o.on_ui_message(Stop())
    r.o.on_decoder_message(BaselineDone(baseline_id=1, ok=True))
    assert r.o.state is St.PAUSED


def test_display_lost_pauses_and_only_resumes_by_hand():
    r = Rig()
    r.to_trial()
    r.status(display_ok=False, message="Esc pressed")
    assert r.o.state is St.PAUSED
    assert [type(m) for m in r.decoder[-2:]] == [StimStop, Pause]
    assert r.last(r.decoder, StimStop).reason == "paused"
    status = r.last(r.ui, UIStatus)
    assert not status.display_ok and not status.resume_ready
    r.o.on_ui_message(Resume())
    assert r.o.state is St.PAUSED and "Display closed" in r.last(r.ui, State).detail
    r.status(display_ok=True)
    assert r.o.state is St.PAUSED  # no automatic restart after Esc
    assert r.last(r.ui, UIStatus).resume_ready
    r.o.on_ui_message(Resume())
    assert r.o.state is St.BASELINE


def test_board_not_ok_is_a_badge_only():
    r = Rig()
    r.to_trial()
    r.status(board_ok=False, message="posterior RMS 250 uV")
    assert r.o.state is St.STIMULATING
    status = r.last(r.ui, UIStatus)
    assert not status.board_ok and status.display_ok
    assert status.decoder_message == "posterior RMS 250 uV"


def test_decoder_loss_pauses_and_reconnect_resumes():
    r = Rig()
    r.to_trial()
    r.o.on_decoder_disconnected()
    assert r.o.state is St.PAUSED
    assert r.last(r.ui, State).detail == "Decoder disconnected"
    r.o.on_decoder_connected()
    assert r.o.state is St.PAUSED  # not before the bridge's hello passes
    r.o.on_decoder_message(bridge_hello())
    assert r.o.state is St.BASELINE


def test_reconnect_sends_baseline_only_after_hello_ack():
    r = Rig()
    r.to_trial()
    r.o.on_decoder_disconnected()
    r.decoder.clear()
    r.o.on_decoder_connected()
    assert r.decoder == []
    r.o.on_decoder_message(bridge_hello())
    assert [type(m) for m in r.decoder] == [HelloAck, Baseline]


def test_reconnect_with_rejected_hello_stays_paused_until_a_good_one():
    r = Rig()
    r.to_trial()
    r.o.on_decoder_disconnected()
    r.decoder.clear()
    r.o.on_decoder_connected()
    r.o.on_decoder_message(bridge_hello(refresh_hz=120.0))
    assert r.o.state is St.PAUSED
    assert not [m for m in r.decoder if isinstance(m, Baseline)]
    r.o.on_decoder_message(bridge_hello())
    assert r.o.state is St.BASELINE


def test_stop_pauses_and_resume_goes_through_baseline():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(Stop())
    assert r.o.state is St.PAUSED and r.o.deadline is None
    assert [type(m) for m in r.decoder[-2:]] == [StimStop, Pause]
    assert r.last(r.decoder, StimStop).reason == "paused"
    r.advance(100)
    assert r.o.state is St.PAUSED
    r.o.on_ui_message(Resume())
    assert r.o.state is St.BASELINE
    r.o.on_ui_message(Stop())
    assert isinstance(r.decoder[-1], Pause)  # outside a trial: pause only


def test_baselines_are_numbered():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.o.on_ui_message(Stop())
    r.o.on_ui_message(Resume())
    assert [m.baseline_id for m in r.decoder if isinstance(m, Baseline)] == [1, 2]


# ---------------------------------------------------------------------------
# Page mode: the diamond of 3 options + NEXT
# ---------------------------------------------------------------------------


def test_the_page_shows_three_options_and_next_in_the_diamond():
    r = Rig()
    start = r.to_trial()
    shown = r.last(r.ui, ShowPage)
    assert shown.mode == "page" and shown.page == 1
    assert [(s.slot, s.role, s.label, s.utterance) for s in shown.slots] == [
        ("s1", "option", HOME[0].label, HOME[0].utterance),
        ("s2", "option", HOME[1].label, HOME[1].utterance),
        ("s3", "option", HOME[2].label, HOME[2].utterance),
        ("s4", "next", "More options", ""),
    ]
    cue = [m for m in r.decoder if isinstance(m, Display)][-1]
    assert cue.text == "" and cue.highlight is None and "highlight" in cue.model_fields_set
    assert cue.labels == {"s1": HOME[0].utterance, "s2": HOME[1].utterance,
                          "s3": HOME[2].utterance, "s4": "More options"}
    assert start.active_slots == ["s1", "s2", "s3", "s4"]
    assert r.last(r.ui, Trial).phase == "cue"  # the mirror waits for the display's onset
    r.o.on_decoder_message(StimOnset(trial_id=start.trial_id, t_unix=r.clock.t))
    assert r.last(r.ui, Trial).phase == "flicker"


def test_option_opens_the_confirm_screen_with_only_say_it_and_back():
    r = Rig()
    confirm = r.pick_and_confirm_screen("s2")
    assert r.last(r.ui, Highlight).slot == "s2"
    assert r.last(r.decoder, StimStop).reason == "selected"
    assert confirm.mode == "confirm" and confirm.text == HOME[1].utterance
    assert [(s.slot, s.role, s.label) for s in confirm.slots] == [
        (SAY_IT, "say", "Say it"), (BACK, "back", "Back")]
    display = r.last(r.decoder, Display)
    assert display.text == HOME[1].utterance
    assert display.labels == {"s1": "Say it", "s2": "", "s3": "", "s4": "Back"}
    assert r.spoken() == []  # nothing yet
    r.run_out()
    assert r.o.state is St.CONFIRMING
    assert r.last(r.decoder, StimStart).active_slots == [SAY_IT, BACK]


def test_say_it_speaks_exactly_the_picked_sentence():
    r = Rig()
    r.pick_and_confirm_screen("s3")
    r.to_trial()
    r.select(SAY_IT)
    r.run_out()
    speak = r.last(r.ui, Speak)
    assert r.o.state is St.SPEAKING and speak.text == HOME[2].utterance
    assert r.last(r.ui, Transcript) == Transcript(who="user", text=HOME[2].utterance)
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    assert r.o.state is St.REST


@pytest.mark.parametrize("ending", ["back", "timeout", "no_selection"])
def test_back_or_no_answer_on_the_confirm_screen_speaks_nothing(ending):
    r = Rig()
    r.pick_and_confirm_screen("s1")
    first_page = [m for m in r.ui if isinstance(m, ShowPage) and m.mode == "page"][-1]
    r.to_trial()
    if ending == "back":
        r.select(BACK)
        r.run_out()
    elif ending == "timeout":
        r.advance(CFG.stim.max_trial_s)
        assert r.last(r.ui, State).detail == NOT_CONFIRMED
        r.run_out()
    else:
        r.o.on_decoder_message(NoSelection(trial_id=r.o.active_trial, reason="low_signal"))
        assert r.last(r.decoder, StimStop).reason == "no_selection"
        r.run_out()
    assert r.spoken() == []
    r.to_trial()
    again = r.last(r.ui, ShowPage)
    assert again.mode == "page" and again.list_id == first_page.list_id  # the same page


def test_confirm_screen_ignores_targets_that_are_not_flickering():
    r = Rig()
    r.pick_and_confirm_screen("s1")
    r.to_trial()
    r.select("s2")  # still on screen but not active on the confirm screen
    assert r.o.state is St.CONFIRMING


def test_without_confirm_an_option_speaks_at_once():
    r = Rig(CFG_NO_CONFIRM)
    r.to_trial()
    r.select("s2")
    r.run_out()
    assert r.o.state is St.SPEAKING and r.spoken() == [HOME[1].utterance]


def test_next_loads_more_options_and_twice_goes_home():
    r = Rig()
    r.to_trial()
    first = r.last(r.ui, ShowPage)
    r.select(NEXT)
    r.run_out()
    assert r.o.state is St.REST and r.spoken() == []
    r.to_trial()
    second = r.last(r.ui, ShowPage)
    assert second.page == first.page + 1 and second.list_id != first.list_id
    assert [s.utterance for s in second.slots[:3]] == [o.utterance for o in MORE]
    r.select(NEXT)
    r.run_out()
    r.to_trial()
    third = r.last(r.ui, ShowPage)
    assert [s.utterance for s in third.slots[:3]] == [o.utterance for o in HOME]


def test_page_timeout_is_not_caught_and_repeats_the_same_page():
    r = Rig()
    r.to_trial()
    before = r.last(r.ui, ShowPage)
    r.advance(CFG.stim.max_trial_s)
    assert r.o.state is St.NOT_CAUGHT
    assert r.last(r.decoder, StimStop).reason == "timeout"
    assert r.last(r.ui, State).detail == DIDNT_CATCH
    r.to_trial()
    assert r.last(r.ui, ShowPage).list_id == before.list_id


def test_stale_and_unknown_selections_are_ignored():
    r = Rig()
    start = r.to_trial()
    for trial_id, slot in [(start.trial_id - 1, "s1"), (start.trial_id + 1, "s1"),
                           (start.trial_id, "s9")]:
        r.o.on_decoder_message(Selection(trial_id=trial_id, slot=slot, t_unix=0))
    assert r.o.state is St.STIMULATING
    r.select("s1")
    r.o.on_decoder_message(Selection(trial_id=start.trial_id, slot="s2", t_unix=0))  # second answer
    assert r.o.state is St.SELECTED and r.o.selected_slot == "s1"
    assert len([m for m in r.decoder if isinstance(m, StimStop)]) == 1


def test_selection_outside_a_trial_is_ignored():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.o.on_decoder_message(Selection(trial_id=0, slot="s1", t_unix=0))
    r.o.on_decoder_message(Selection(trial_id=1, slot="s1", t_unix=0))
    assert r.o.state is St.BASELINE


def test_partner_question_during_a_trial_shows_the_yes_no_page():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="Do you want to go outside?", source="typed"))
    assert r.last(r.decoder, StimStop).reason == "context_changed"
    assert r.o.state is St.MENU_CUE
    assert [s.utterance for s in r.last(r.ui, ShowPage).slots[:3]] == [o.utterance for o in YES_NO]
    assert Transcript(who="partner", text="Do you want to go outside?") in r.ui


def test_partner_question_on_the_confirm_screen_drops_the_pick():
    r = Rig()
    r.pick_and_confirm_screen("s2")
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="Are you cold?", source="typed"))
    shown = r.last(r.ui, ShowPage)
    assert shown.mode == "page" and shown.slots[0].utterance == YES_NO[0].utterance
    assert r.o.picked is None


def test_partner_text_while_speaking_waits_for_the_next_cue():
    r = Rig()
    r.pick_and_confirm_screen("s1")
    r.to_trial()
    r.select(SAY_IT)
    r.run_out()
    speak = r.last(r.ui, Speak)
    pages_before = len([m for m in r.ui if isinstance(m, ShowPage)])
    r.o.on_ui_message(PartnerText(text="Are you hungry?", source="typed"))
    assert r.o.state is St.SPEAKING
    assert len([m for m in r.ui if isinstance(m, ShowPage)]) == pages_before
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    r.run_out()  # REST -> MENU_CUE
    assert r.last(r.ui, ShowPage).slots[0].utterance == YES_NO[0].utterance


def test_wrong_tts_done_is_ignored_and_speaking_times_out():
    r = Rig(CFG_NO_CONFIRM)
    r.to_trial()
    r.select("s1")
    r.run_out()
    r.o.on_ui_message(TTSDone(utterance_id="nope"))
    assert r.o.state is St.SPEAKING
    r.advance(CFG.speech.max_speak_s)
    assert r.o.state is St.REST


def test_keyboard_keys_select_the_slot_with_that_number_when_it_flickers():
    r = Rig(keyboard_mode=True)
    r.to_trial()
    r.o.on_ui_message(Key(key="3"))
    assert r.o.selected_slot == "s3"
    r.run_out()
    r.to_trial()  # the confirm trial
    r.o.on_ui_message(Key(key="2"))  # s2 is not flickering now
    assert r.o.state is St.CONFIRMING
    r.o.on_ui_message(Key(key="4"))  # Back
    assert r.o.selected_slot == BACK


def test_keys_do_nothing_outside_keyboard_mode():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(Key(key="2"))
    assert r.o.state is St.STIMULATING


def test_keyboard_loss_of_decoder_does_not_pause():
    r = Rig(keyboard_mode=True)
    r.to_trial()
    r.o.on_decoder_disconnected()
    assert r.o.state is St.STIMULATING


def test_without_a_bridge_the_mirror_flickers_at_stim_start():
    r = Rig(bridge=False, keyboard_mode=True)
    r.to_trial()
    assert r.last(r.ui, Trial).phase == "flicker"


def test_stale_onset_does_not_change_the_mirror():
    r = Rig()
    start = r.to_trial()
    r.o.on_decoder_message(StimOnset(trial_id=start.trial_id - 1, t_unix=0))
    assert r.last(r.ui, Trial).phase == "cue"


def test_shown_page_never_changes_during_a_trial():
    r = Rig()
    r.to_trial()
    shown = r.last(r.ui, ShowPage)
    r.o.on_ui_connected()  # a reconnecting page gets the same screen again: allowed
    assert r.last(r.ui, ShowPage) == shown
    with pytest.raises(AssertionError):
        r.o._send_ui(shown.model_copy(update={"page": shown.page + 1}))


def test_display_highlights_the_pick_after_stim_stop():
    r = Rig()
    r.to_trial()
    r.select("s2")
    assert [type(m) for m in r.decoder[-2:]] == [StimStop, Display]
    assert r.last(r.decoder, Display).model_fields_set == {"highlight"}
    assert r.last(r.decoder, Display).highlight == "s2"


def run_unattended(cfg, trials: int = 20, seed: int = 7) -> Rig:
    """Random decoder answers (some missing); the page reports tts_done. Fails if anything sticks."""
    rng = random.Random(seed)
    r = Rig(cfg)
    r.o.on_ui_message(Resume())
    for _ in range(20_000):
        if r.o.trial_id >= trials and r.o.state is St.REST:
            return r
        if r.o.state in (St.STIMULATING, St.CONFIRMING):
            r.advance(rng.uniform(0.1, 3.0))
            if r.o.state in (St.STIMULATING, St.CONFIRMING) and rng.random() < 0.85:
                r.select(rng.choice(list(r.o.screen.roles)))
        elif r.o.state is St.SPEAKING:
            r.o.on_ui_message(TTSDone(utterance_id=r.o.speaking_id))
        elif r.o.state is St.BASELINE:
            r.finish_baseline()
        else:
            r.run_out()
        assert r.o.deadline is not None, f"stuck in {r.o.state}"
    raise AssertionError("did not finish")


def test_twenty_trials_unattended_in_page_mode_speak_only_confirmed_picks():
    r = run_unattended(CFG)
    assert r.spoken()
    # Every spoken sentence was the confirm screen's text right before it, picked with "Say it".
    for i, msg in enumerate(r.ui):
        if isinstance(msg, Speak):
            confirm = [m for m in r.ui[:i] if isinstance(m, ShowPage)][-1]
            assert confirm.mode == "confirm" and confirm.text == msg.text
    starts = [m.trial_id for m in r.decoder if isinstance(m, StimStart)]
    stops = [m.trial_id for m in r.decoder if isinstance(m, StimStop)]
    assert starts == sorted(set(starts)) and stops == starts[: len(stops)]


def test_no_display_between_stim_start_and_stim_stop():
    r = run_unattended(CFG, trials=12, seed=3)
    in_trial = False
    for m in r.decoder:
        if isinstance(m, StimStart):
            in_trial = True
        elif isinstance(m, StimStop):
            in_trial = False
        elif isinstance(m, Display):
            assert not in_trial, "display sent during a trial"


# ---------------------------------------------------------------------------
# List mode: the 2-target fallback (SAY / NEXT, all options listed)
# ---------------------------------------------------------------------------


def test_list_mode_shows_every_option_with_one_highlighted():
    r = Rig(CFG_LIST)
    start = r.to_trial()
    shown = r.last(r.ui, ShowPage)
    assert shown.mode == "list" and shown.highlight_index == 0
    assert [o.utterance for o in shown.options] == [o.utterance for o in HOME]
    assert [(s.slot, s.label) for s in shown.slots] == [("s1", "SAY"), ("s2", "NEXT")]
    assert start.active_slots == ["s1", "s2"]


def test_list_mode_next_moves_the_highlight_and_say_speaks_it():
    r = Rig(CFG_LIST)
    r.to_trial()
    r.select("s2")
    r.run_out()
    r.to_trial()
    shown = r.last(r.ui, ShowPage)
    assert shown.highlight_index == 1 and shown.text == HOME[1].utterance
    r.select("s1")
    r.run_out()
    assert r.spoken() == [HOME[1].utterance]  # no confirm step in list mode


def test_list_mode_next_on_the_last_option_says_more_then_loads_a_new_page():
    r = Rig(CFG_LIST)
    for _ in range(len(HOME) - 1):
        r.to_trial()
        r.select("s2")
        r.run_out()
    r.to_trial()
    assert [s.label for s in r.last(r.ui, ShowPage).slots] == ["SAY", "MORE"]
    r.select("s2")
    r.run_out()
    r.to_trial()
    shown = r.last(r.ui, ShowPage)
    assert shown.highlight_index == 0 and [o.utterance for o in shown.options] == [o.utterance for o in MORE]


def test_twenty_trials_unattended_in_list_mode():
    r = run_unattended(CFG_LIST)
    assert r.spoken()
