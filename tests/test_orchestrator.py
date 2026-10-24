"""Orchestrator transitions with a fake clock: no timers, sockets or threads (PLAN.md section 9)."""

from __future__ import annotations

import random

import pytest

from app.config import load_config, load_intents
from app.orchestrator import DIDNT_CATCH, MORE_LABEL, Orchestrator, St
from app.schemas import (
    DecoderHello,
    DecoderStatus,
    Highlight,
    Key,
    NoSelection,
    PartnerText,
    Resume,
    Selection,
    ServerHello,
    ShowOption,
    Speak,
    State,
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
SAY, NEXT = "s1", "s2"  # roles in config/default.yaml


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


class Rig:
    """An orchestrator plus everything it sent, and helpers to move time forward."""

    def __init__(self, cfg=CFG, keyboard_mode: bool = False) -> None:
        self.clock = Clock()
        self.ui: list = []
        self.decoder: list = []
        self.o = Orchestrator(cfg, INTENTS, self.ui.append, self.decoder.append, clock=self.clock,
                              keyboard_mode=keyboard_mode)

    def advance(self, seconds: float) -> None:
        self.clock.t += seconds
        self.o.tick()

    def run_out(self) -> None:
        """Advance exactly to the pending deadline."""
        assert self.o.deadline is not None, f"no deadline in {self.o.state}"
        self.clock.t = self.o.deadline
        self.o.tick()

    def to_trial(self) -> StimStart:
        """From IDLE (or after a rest) to the next STIMULATING; returns its stim_start."""
        if self.o.state in (St.IDLE, St.PAUSED):
            self.o.on_ui_message(Resume())
        while self.o.state is not St.STIMULATING:
            self.run_out()
        return self.last(self.decoder, StimStart)

    def select(self, slot: str) -> None:
        start = self.last(self.decoder, StimStart)
        self.o.on_decoder_message(Selection(trial_id=start.trial_id, slot=slot, t_unix=self.clock.t))

    @staticmethod
    def last(msgs: list, kind: type):
        found = [m for m in msgs if isinstance(m, kind)]
        assert found, f"no {kind.__name__} sent"
        return found[-1]


def test_hello_is_answered_with_config_slots():
    r = Rig()
    r.o.on_decoder_connected()
    r.o.on_decoder_message(DecoderHello(protocol=1, name="test"))
    hello = r.last(r.decoder, ServerHello)
    assert [s.slot for s in hello.slots] == ["s1", "s2"]
    assert [s.freq_hz for s in hello.slots] == [15.0, 17.0]
    assert hello.waveform == "sine" and hello.refresh_hz == 60
    assert r.last(r.ui, UIStatus).decoder_ok


def test_start_goes_through_baseline_and_cue():
    r = Rig()
    r.o.on_ui_message(Resume())
    assert r.o.state is St.BASELINE
    r.advance(CFG.baseline.seconds - 0.01)
    assert r.o.state is St.BASELINE
    r.advance(0.01)
    assert r.o.state is St.MENU_CUE
    shown = r.last(r.ui, ShowOption)
    assert shown.index == 0 and shown.utterance == INTENTS.fallback_lists["home"][0].utterance
    assert [(s.slot, s.label) for s in shown.slots] == [("s1", "SAY"), ("s2", "NEXT")]
    r.advance(CFG.stim.cue_s)
    assert r.o.state is St.STIMULATING
    start = r.last(r.decoder, StimStart)
    assert start.trial_id == 1 and start.active_slots == ["s1", "s2"] and start.mode == "live"
    assert r.last(r.ui, Trial).phase == "flicker"


def test_say_speaks_exactly_the_shown_utterance():
    r = Rig()
    r.to_trial()
    shown = r.last(r.ui, ShowOption)
    r.select(SAY)
    assert r.o.state is St.SELECTED
    assert r.last(r.decoder, StimStop).reason == "selected"
    assert r.last(r.ui, Highlight).slot == SAY
    r.run_out()
    speak = r.last(r.ui, Speak)
    assert r.o.state is St.SPEAKING and speak.text == shown.utterance
    assert r.last(r.ui, Transcript) == Transcript(who="user", text=shown.utterance)
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    assert r.o.state is St.REST


def test_wrong_tts_done_is_ignored_and_speaking_times_out():
    r = Rig()
    r.to_trial()
    r.select(SAY)
    r.run_out()
    r.o.on_ui_message(TTSDone(utterance_id="nope"))
    assert r.o.state is St.SPEAKING
    r.advance(CFG.speech.max_speak_s)
    assert r.o.state is St.REST


def test_next_moves_through_the_list_then_more_options():
    r = Rig()
    count = len(INTENTS.fallback_lists["home"])
    for i in range(count - 1):
        r.to_trial()
        assert r.last(r.ui, ShowOption).index == i
        r.select(NEXT)
        r.run_out()  # SELECTED -> REST
        assert r.o.state is St.REST
    r.to_trial()
    last = r.last(r.ui, ShowOption)
    assert last.index == count - 1
    assert [s.label for s in last.slots] == ["SAY", MORE_LABEL]
    first_list = last.list_id
    r.select(NEXT)  # past the end: "More options"
    r.run_out()
    r.to_trial()
    shown = r.last(r.ui, ShowOption)
    assert shown.list_id != first_list and shown.index == 0
    assert shown.utterance == INTENTS.fallback_lists["yes_no"][0].utterance


def test_more_options_twice_in_a_row_goes_home():
    r = Rig()
    count = len(INTENTS.fallback_lists["home"])
    for _ in range(2 * count):
        r.to_trial()
        r.select(NEXT)
        r.run_out()
    r.to_trial()
    shown = r.last(r.ui, ShowOption)
    assert shown.index == 0 and shown.utterance == INTENTS.fallback_lists["home"][0].utterance


def test_timeout_is_not_caught_and_repeats_the_same_option():
    r = Rig()
    r.to_trial()
    r.select(NEXT)
    r.run_out()
    r.to_trial()
    before = r.last(r.ui, ShowOption)
    r.advance(CFG.stim.max_trial_s)
    assert r.o.state is St.NOT_CAUGHT
    assert r.last(r.decoder, StimStop).reason == "timeout"
    assert r.last(r.ui, State).detail == DIDNT_CATCH
    r.to_trial()
    after = r.last(r.ui, ShowOption)
    assert (after.list_id, after.index) == (before.list_id, before.index)


def test_no_selection_is_not_caught():
    r = Rig()
    start = r.to_trial()
    r.o.on_decoder_message(NoSelection(trial_id=start.trial_id, reason="low_signal"))
    assert r.o.state is St.NOT_CAUGHT
    assert r.last(r.decoder, StimStop).reason == "timeout"  # v1 has no "no_selection" reason


def test_stale_and_unknown_selections_are_ignored():
    r = Rig()
    start = r.to_trial()
    r.o.on_decoder_message(Selection(trial_id=start.trial_id - 1, slot=SAY, t_unix=0))
    r.o.on_decoder_message(Selection(trial_id=start.trial_id + 1, slot=SAY, t_unix=0))
    r.o.on_decoder_message(Selection(trial_id=start.trial_id, slot="s9", t_unix=0))
    assert r.o.state is St.STIMULATING
    r.select(SAY)
    r.o.on_decoder_message(Selection(trial_id=start.trial_id, slot=NEXT, t_unix=0))  # second answer
    assert r.o.state is St.SELECTED and r.o.selected_slot == SAY
    assert len([m for m in r.decoder if isinstance(m, StimStop)]) == 1


def test_selection_outside_a_trial_is_ignored():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.o.on_decoder_message(Selection(trial_id=0, slot=SAY, t_unix=0))
    r.o.on_decoder_message(Selection(trial_id=1, slot=SAY, t_unix=0))
    assert r.o.state is St.BASELINE


def test_partner_question_during_trial_stops_it_and_shows_yes_no():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="Do you want to go outside?", source="typed"))
    assert r.last(r.decoder, StimStop).reason == "context_changed"
    assert r.o.state is St.MENU_CUE
    assert r.last(r.ui, ShowOption).utterance == INTENTS.fallback_lists["yes_no"][0].utterance
    assert Transcript(who="partner", text="Do you want to go outside?") in r.ui


def test_partner_text_while_speaking_waits_for_the_next_cue():
    r = Rig()
    r.to_trial()
    r.select(SAY)
    r.run_out()
    speak = r.last(r.ui, Speak)
    shows_before = len([m for m in r.ui if isinstance(m, ShowOption)])
    r.o.on_ui_message(PartnerText(text="Are you hungry?", source="typed"))
    assert r.o.state is St.SPEAKING
    assert len([m for m in r.ui if isinstance(m, ShowOption)]) == shows_before
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    r.run_out()  # REST -> MENU_CUE
    assert r.last(r.ui, ShowOption).utterance == INTENTS.fallback_lists["yes_no"][0].utterance


def test_stop_pauses_and_resume_goes_through_baseline():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(Stop())
    assert r.o.state is St.PAUSED and r.o.deadline is None
    assert r.last(r.decoder, StimStop).reason == "paused"
    r.advance(100)
    assert r.o.state is St.PAUSED
    r.o.on_ui_message(Resume())
    assert r.o.state is St.BASELINE


def test_decoder_loss_pauses_and_reconnect_resumes():
    r = Rig()
    r.o.on_decoder_connected()
    r.to_trial()
    r.o.on_decoder_disconnected()
    assert r.o.state is St.PAUSED
    assert r.last(r.ui, State).detail == "Decoder disconnected"
    r.o.on_decoder_connected()
    assert r.o.state is St.BASELINE


def test_decoder_status_not_ok_is_reported():
    r = Rig()
    r.o.on_decoder_connected()
    r.o.on_decoder_message(DecoderHello(protocol=1, name="test"))
    r.o.on_decoder_message(DecoderStatus(ok=False, message="display closed"))
    assert not r.last(r.ui, UIStatus).decoder_ok


@pytest.mark.parametrize("keyboard_mode, expect", [(True, St.SELECTED), (False, St.STIMULATING)])
def test_keyboard_selects_only_in_keyboard_mode(keyboard_mode, expect):
    r = Rig(keyboard_mode=keyboard_mode)
    r.to_trial()
    r.o.on_ui_message(Key(key="2"))
    assert r.o.state is expect
    if keyboard_mode:
        assert r.o.selected_slot == NEXT


def test_keyboard_loss_of_decoder_does_not_pause():
    r = Rig(keyboard_mode=True)
    r.to_trial()
    r.o.on_decoder_disconnected()
    assert r.o.state is St.STIMULATING


def test_shown_option_never_changes_during_a_trial():
    r = Rig()
    r.to_trial()
    shown = r.last(r.ui, ShowOption)
    r.o.on_ui_connected()  # a reconnecting page gets the same option again: allowed
    assert r.last(r.ui, ShowOption) == shown
    r.o.shown = shown.model_copy(update={"utterance": "something else"})
    with pytest.raises(AssertionError):
        r.o._send_ui(shown)


def test_protocol_2_proposals_only_when_enabled():
    r = Rig()
    r.to_trial()
    assert not [m for m in r.decoder if m.type in ("display", "baseline")]
    r2 = Rig(cfg=CFG.model_copy(update={"protocol": 2}))
    start = r2.to_trial()
    assert {m.type for m in r2.decoder} >= {"baseline", "display"}
    r2.o.on_decoder_message(NoSelection(trial_id=start.trial_id, reason="low_signal"))
    assert r2.last(r2.decoder, StimStop).reason == "no_selection"


def test_twenty_trials_unattended_never_get_stuck():
    """V2 done-check, headless: random decoder answers (some missing), the page reports tts_done."""
    rng = random.Random(7)
    r = Rig()
    r.o.on_ui_message(Resume())
    spoken = 0
    for _ in range(10_000):
        if r.o.trial_id >= 20 and r.o.state is St.REST:
            break
        if r.o.state is St.STIMULATING:
            r.advance(rng.uniform(0.1, 3.0))
            if r.o.state is St.STIMULATING and rng.random() < 0.85:
                r.select(rng.choice([SAY, NEXT]))
        elif r.o.state is St.SPEAKING:
            r.o.on_ui_message(TTSDone(utterance_id=r.o.speaking_id))
            spoken += 1
        else:
            r.run_out()
        assert r.o.deadline is not None or r.o.state is St.SPEAKING, f"stuck in {r.o.state}"
    assert r.o.trial_id >= 20
    assert spoken > 0
    starts = [m.trial_id for m in r.decoder if isinstance(m, StimStart)]
    stops = [m.trial_id for m in r.decoder if isinstance(m, StimStop)]
    assert starts == sorted(set(starts)) and stops == starts[: len(stops)]
