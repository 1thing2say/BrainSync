"""Orchestrator with c-VEP on the phone (config/phone_cvep.yaml, draft section 9), fake clock.

Halves mode (Look to Speak): two cards, left (s1) and right (s2); each look keeps one half of the
phrases still in play, then Say it (s1) / Back (s2). The phone draws the flicker and sends cycle
markers, which the orchestrator relays to the bridge; a calibration trial trains the template.
"""

from __future__ import annotations

import random

import pytest

from app.config import load_config, load_intents
from app.log import EventLog, read_events
from app.orchestrator import CALIBRATING_DETAIL, MORE_ITEM, Orchestrator, St
from app.schemas import (
    PROTOCOL,
    BaselineDone,
    Calibrate,
    ClockPing,
    CodeSpec,
    CycleMarker,
    CycleMarkers,
    DecoderHello,
    DecoderStatus,
    Display,
    DisplayReport,
    HelloAck,
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
    StimMarkers,
    StimStart,
    StimStop,
    Stimulus,
    Stop,
    Train,
    TrainDone,
    Trial,
    TTSDone,
    UIStatus,
)

CFG = load_config("config/phone_cvep.yaml")
INTENTS = load_intents("config/intents_halves.yaml", option_count=6, label_max_chars=18,
                       utterance_max_words=14, label_max_words=3)
HOME = INTENTS.fallback_lists["home"]
MORE = INTENTS.fallback_lists["more"]
LEFT, RIGHT = "s1", "s2"
SAY_IT, BACK = CFG.stim.confirm.say, CFG.stim.confirm.back  # s1, s2
BITS = ("1000001" * 9)[:63]
OK_REPORT = DisplayReport(refresh_hz=60.0, ok=True)
CALIBRATION_S = 20 * 63 / 60 + CFG.stim.max_trial_s  # train_cycles cycles + max_trial_s


def code_hello(**changes) -> DecoderHello:
    """A hello that matches phone_cvep.yaml, as brainsync.voice_bridge -c cvep_2 sends it."""
    fields = dict(protocol=PROTOCOL, name="bridge", refresh_hz=60.0, waveform="square",
                  paradigm="code", code=CodeSpec(length=63, frames_per_bit=1, bits=BITS),
                  slots=[SlotSpec(slot="s1", lag_frames=0), SlotSpec(slot="s2", lag_frames=31)])
    fields.update(changes)
    return DecoderHello(**fields)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


class Rig:
    """An orchestrator on phone_cvep.yaml plus everything it sent.

    By default a bridge is connected, acked and trained (no calibration), and the phone has
    reported an ok display; ``select`` sends the phone's cycle 0 first, as the phone would.
    """

    def __init__(self, cfg=CFG, keyboard_mode: bool = False, bridge: bool = True,
                 trained: bool = True, report: DisplayReport | None = OK_REPORT, **hooks) -> None:
        self.cfg = cfg
        self.clock = Clock()
        self.ui: list = []
        self.decoder: list = []
        self.o = Orchestrator(cfg, INTENTS, self.ui.append, self.decoder.append, clock=self.clock,
                              keyboard_mode=keyboard_mode, **hooks)
        if bridge:
            self.o.on_decoder_connected()
            self.o.on_decoder_message(code_hello())
            self.status(trained=trained)
        if report is not None:
            self.o.on_ui_message(report)

    def status(self, **flags) -> None:
        fields = dict(ok=True, board_ok=True, display_ok=True, message="")
        fields.update(flags)
        self.o.on_decoder_message(DecoderStatus(**fields))

    def finish_baseline(self) -> None:
        self.o.on_decoder_message(BaselineDone(baseline_id=self.o.expected_baseline, ok=True))

    def run_out(self) -> None:
        assert self.o.deadline is not None, f"no deadline in {self.o.state}"
        self.clock.t = self.o.deadline
        self.o.tick()

    def to_trial(self) -> None:
        """From IDLE / PAUSED / any wait to the next page or confirm trial."""
        if self.o.state in (St.IDLE, St.PAUSED):
            self.o.on_ui_message(Resume())
        while self.o.state not in (St.STIMULATING, St.CONFIRMING):
            if self.o.state is St.BASELINE and self.o.expected_baseline is not None:
                self.finish_baseline()
            else:
                self.run_out()

    def to_calibration_trial(self) -> Train:
        if self.o.state in (St.IDLE, St.PAUSED):
            self.o.on_ui_message(Resume())
        if self.o.state is St.BASELINE:
            self.finish_baseline()
        assert self.o.state is St.CALIBRATING and self.o.active_trial is None
        self.run_out()  # the cue is over
        return self.last(self.decoder, Train)

    def onset(self, trial_id: int | None = None) -> None:
        trial_id = self.o.active_trial if trial_id is None else trial_id
        self.o.on_ui_message(CycleMarkers(trial_id=trial_id,
                                          markers=[CycleMarker(cycle=0, t_unix=self.clock.t)]))

    def select(self, slot: str, onset: bool = True) -> None:
        if onset and not self.o.onset_seen:
            self.onset()
        self.o.on_decoder_message(Selection(trial_id=self.o.active_trial or -1, slot=slot,
                                            t_unix=self.clock.t))

    def look(self, slot: str) -> None:
        """One halves or confirm trial: select ``slot`` and let the highlight run out."""
        self.to_trial()
        self.select(slot)
        self.run_out()

    def items(self) -> list[str]:
        """The phrases on the halves screen shown last ("" for More options)."""
        show = self.last(self.ui, ShowPage)
        assert show.mode == "halves"
        return [o.utterance for o in show.options]

    @staticmethod
    def last(msgs: list, kind: type):
        found = [m for m in msgs if isinstance(m, kind)]
        assert found, f"no {kind.__name__} sent"
        return found[-1]

    def spoken(self) -> list[str]:
        return [m.text for m in self.ui if isinstance(m, Speak)]


PAGE = [o.utterance for o in HOME] + [""]  # the home page's 7 items, More options last


# ---------------------------------------------------------------------------
# Handshake and the phone's stimulus
# ---------------------------------------------------------------------------


def test_code_hello_is_acked_and_the_phone_gets_the_code():
    r = Rig()
    ack = r.last(r.decoder, HelloAck)
    assert ack.roles == {"s1": "left", "s2": "right"}
    assert ack.labels == {"s1": " / ".join(o.label for o in HOME[:4]),
                          "s2": " / ".join([o.label for o in HOME[4:]] + ["More options"])}
    stim = r.last(r.ui, Stimulus)
    assert stim == Stimulus(refresh_hz=60.0, frames_per_bit=1, bits=BITS,
                            lags={"s1": 0, "s2": 31}, depth=1.0)
    assert r.o.code == code_hello().code
    assert r.o.stimulus() == stim


def test_a_page_that_connects_and_a_bridge_that_says_hello_again_resend_the_stimulus():
    r = Rig()
    r.ui.clear()
    r.o.on_ui_connected()
    assert r.last(r.ui, Stimulus).bits == BITS
    r.ui.clear()
    r.o.on_decoder_disconnected()
    r.o.on_decoder_connected()
    r.o.on_decoder_message(code_hello())
    assert r.last(r.ui, Stimulus).lags == {"s1": 0, "s2": 31}


def test_no_stimulus_before_a_bridge_brought_the_code():
    r = Rig(bridge=False)
    r.o.on_ui_connected()
    assert not [m for m in r.ui if isinstance(m, Stimulus)]
    assert r.o.code is None


@pytest.mark.parametrize("changes, fragment", [
    ({"slots": [SlotSpec(slot="s1", lag_frames=0), SlotSpec(slot="s2", lag_frames=30)]},
     "s2 lags 30 frames, expected 31"),
    ({"code": CodeSpec(length=31, frames_per_bit=1, bits=BITS[:31])}, "code length 31"),
    ({"code": CodeSpec(length=63, frames_per_bit=2, bits=BITS)}, "frames_per_bit 2"),
    ({"paradigm": "freq", "code": None,
      "slots": [SlotSpec(slot="s1", freq_hz=15.0), SlotSpec(slot="s2", freq_hz=17.0)]},
     "paradigm freq"),
    ({"refresh_hz": 120.0}, "ProMotion"),
])
def test_a_code_hello_that_does_not_match_is_rejected(changes, fragment):
    r = Rig(bridge=False)
    r.o.on_decoder_connected()
    r.o.on_decoder_message(code_hello(**changes))
    assert fragment in r.last(r.decoder, ProtocolError).message
    assert not [m for m in r.decoder if isinstance(m, HelloAck)]
    assert not [m for m in r.ui if isinstance(m, Stimulus)]
    assert r.o.code is None
    assert r.o.resume_problem().startswith("Decoder rejected")


# ---------------------------------------------------------------------------
# Trials on the phone: markers, onset, selections
# ---------------------------------------------------------------------------


def test_the_phone_is_told_to_flicker_at_stim_start():
    r = Rig()
    r.to_trial()
    start = r.last(r.decoder, StimStart)
    assert start.active_slots == [LEFT, RIGHT]
    assert r.last(r.ui, Trial) == Trial(trial_id=start.trial_id, phase="flicker")


def test_cycle_markers_are_relayed_only_for_the_current_trial():
    r = Rig()
    r.to_trial()
    trial = r.o.active_trial
    markers = [CycleMarker(cycle=0, t_unix=1.0), CycleMarker(cycle=1, t_unix=2.05)]
    r.o.on_ui_message(CycleMarkers(trial_id=trial, markers=markers, dropped_frames=1))
    assert r.last(r.decoder, StimMarkers) == StimMarkers(trial_id=trial, markers=markers)
    assert r.o.onset_seen
    sent = len(r.decoder)
    r.o.on_ui_message(CycleMarkers(trial_id=trial - 1, markers=markers))  # an older trial's
    r.o.on_ui_message(CycleMarkers(trial_id=trial + 1, markers=markers))  # not started yet
    assert len(r.decoder) == sent
    r.select(LEFT)  # the trial ends: its late markers are dropped too
    r.o.on_ui_message(CycleMarkers(trial_id=trial, markers=[CycleMarker(cycle=2, t_unix=3.1)]))
    assert len([m for m in r.decoder if isinstance(m, StimMarkers)]) == 1


def test_cycle_zero_is_logged_as_the_onset(tmp_path):
    log = EventLog(tmp_path)
    r = Rig(log=log)
    r.to_trial()
    r.o.on_ui_message(CycleMarkers(trial_id=r.o.active_trial,
                                   markers=[CycleMarker(cycle=0, t_unix=1234.5)]))
    r.o.on_ui_message(CycleMarkers(trial_id=r.o.active_trial,
                                   markers=[CycleMarker(cycle=1, t_unix=1235.55)]))
    onsets = [e for e in read_events(log.path) if e["event"] == "stim_onset"]
    assert [(e["trial_id"], e["onset_t_unix"]) for e in onsets] == [(r.o.active_trial, 1234.5)]
    log.close()


def test_a_selection_before_the_phones_first_cycle_is_ignored():
    r = Rig()
    r.to_trial()
    r.select(LEFT, onset=False)
    assert r.o.state is St.STIMULATING and r.o.active_trial is not None
    r.onset()
    r.select(LEFT)
    assert r.o.state is St.SELECTED


def test_keyboard_selections_need_no_onset():
    r = Rig(keyboard_mode=True)
    r.to_trial()
    r.o.on_ui_message(Key(key="2"))
    assert r.o.state is St.SELECTED and r.o.selected_slot == RIGHT


def test_a_clock_ping_that_reaches_the_orchestrator_does_nothing():
    r = Rig()
    sent = (len(r.ui), len(r.decoder))
    r.o.on_ui_message(ClockPing(id=1, t_phone=5.0))
    assert (len(r.ui), len(r.decoder)) == sent


# ---------------------------------------------------------------------------
# Halves mode
# ---------------------------------------------------------------------------


def test_the_page_is_split_into_two_halves_with_more_options_last():
    r = Rig()
    r.to_trial()
    show = r.last(r.ui, ShowPage)
    assert show.mode == "halves" and show.split == 4
    assert show.options[-1] == MORE_ITEM
    assert r.items() == PAGE
    assert [(s.slot, s.role, s.label) for s in show.slots] == [(LEFT, "left", "Left"),
                                                                (RIGHT, "right", "Right")]
    display = r.last(r.decoder, Display)
    assert display.labels == {LEFT: " / ".join(o.label for o in HOME[:4]),
                              RIGHT: f"{HOME[4].label} / {HOME[5].label} / More options"}


def test_looks_narrow_to_one_phrase_then_say_it_speaks_exactly_it():
    r = Rig()
    r.look(LEFT)
    assert r.items() == PAGE[:4] and r.last(r.ui, ShowPage).split == 2
    assert r.o.state is St.MENU_CUE
    r.look(RIGHT)
    assert r.items() == PAGE[2:4] and r.last(r.ui, ShowPage).split == 1
    r.look(LEFT)
    confirm = r.last(r.ui, ShowPage)
    assert confirm.mode == "confirm" and confirm.text == HOME[2].utterance
    assert [(s.slot, s.role) for s in confirm.slots] == [(SAY_IT, "say"), (BACK, "back")]
    assert r.spoken() == []  # nothing before Say it (rule 2)
    r.to_trial()
    assert r.o.state is St.CONFIRMING
    assert r.last(r.decoder, StimStart).active_slots == [SAY_IT, BACK]
    r.select(SAY_IT)
    r.run_out()
    assert r.spoken() == [HOME[2].utterance]


def test_the_spoken_phrase_logs_its_rank(tmp_path):
    log = EventLog(tmp_path)
    r = Rig(log=log)
    for side in (RIGHT, LEFT, LEFT):  # items 4-6 -> 4, 5 -> 4 (the fifth phrase)
        r.look(side)
    r.look(SAY_IT)
    assert r.spoken() == [HOME[4].utterance]
    spoken = [e for e in read_events(log.path) if e["event"] == "spoken"]
    assert spoken[-1]["rank"] == 5 and spoken[-1]["list"] == "home"
    log.close()


def test_more_options_alone_asks_for_a_new_page_without_speaking():
    r = Rig()
    r.look(RIGHT)
    assert r.items() == PAGE[4:]
    r.look(RIGHT)  # the right half of the last three is More options alone
    assert r.o.state is St.REST and r.spoken() == []
    r.run_out()
    assert r.items() == [o.utterance for o in MORE] + [""]
    assert not [m for m in r.ui if isinstance(m, ShowPage) and m.mode == "confirm"]


def test_more_options_twice_goes_home():
    r = Rig()
    for _ in range(2):
        r.look(RIGHT)
        r.look(RIGHT)
        r.run_out()
    assert r.items() == PAGE


@pytest.mark.parametrize("ending", ["back", "timeout", "no_selection"])
def test_back_or_no_answer_on_the_confirm_screen_shows_the_whole_page(ending):
    r = Rig()
    r.look(LEFT)
    r.look(LEFT)
    r.look(LEFT)
    assert r.last(r.ui, ShowPage).mode == "confirm"
    r.to_trial()
    if ending == "back":
        r.select(BACK)
    elif ending == "timeout":
        r.run_out()
    else:
        r.o.on_decoder_message(NoSelection(trial_id=r.o.active_trial, reason="low_signal"))
    r.to_trial()
    assert r.items() == PAGE
    assert r.spoken() == []


def test_one_miss_repeats_the_screen_and_two_go_back_to_the_whole_page():
    r = Rig()
    r.look(LEFT)
    r.to_trial()
    r.o.on_decoder_message(NoSelection(trial_id=r.o.active_trial, reason="low_signal"))
    r.to_trial()
    assert r.items() == PAGE[:4]
    r.run_out()  # timeout: the second miss in a row
    r.to_trial()
    assert r.items() == PAGE


def test_misses_on_the_whole_page_just_repeat_it():
    r = Rig()
    for _ in range(3):
        r.to_trial()
        r.run_out()
    r.to_trial()
    assert r.items() == PAGE


def test_a_selection_between_misses_starts_the_count_again():
    r = Rig()
    r.look(LEFT)
    r.to_trial()
    r.run_out()  # miss 1 on [0..3]
    r.look(LEFT)  # -> [0, 1]
    r.to_trial()
    r.run_out()  # miss 1 on [0, 1]
    r.to_trial()
    assert r.items() == PAGE[:2]


def test_reading_time_new_page_counts_its_words_a_narrowed_screen_gets_cue_s():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.finish_baseline()
    words = sum(len(o.utterance.split()) for o in HOME) + 2  # + "More options"
    stim = CFG.stim
    assert r.o.deadline - r.clock.t == pytest.approx(
        min(stim.cue_s + stim.read_s_per_word * words, stim.cue_max_s))
    r.run_out()
    r.select(LEFT)
    r.run_out()
    assert r.o.state is St.MENU_CUE and r.o.deadline - r.clock.t == pytest.approx(stim.cue_s)


def test_keyboard_digits_pick_the_left_and_right_cards():
    r = Rig(keyboard_mode=True, bridge=False)
    r.to_trial()
    r.o.on_ui_message(Key(key="1"))
    r.run_out()
    assert r.items() == PAGE[:4]
    r.to_trial()
    r.o.on_ui_message(Key(key="2"))
    r.run_out()
    assert r.items() == PAGE[2:4]


def test_a_typed_partner_line_shows_a_new_whole_page():
    r = Rig()
    r.look(LEFT)
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="Do you want tea?", source="typed"))
    assert r.last(r.decoder, StimStop).reason == "context_changed"
    yes_no = INTENTS.fallback_lists["yes_no"]
    assert r.items() == [o.utterance for o in yes_no] + [""]


@pytest.mark.parametrize("calibrating", [False, True])
def test_the_shown_screen_never_changes_during_a_trial(calibrating):
    r = Rig(trained=not calibrating)
    if calibrating:
        r.to_calibration_trial()
    else:
        r.look(LEFT)
        r.to_trial()
    show = r.o.shown
    r.o._send_ui(show)  # the same screen again (a reconnecting page): allowed
    changed = ShowPage(list_id=show.list_id, page=show.page, mode="halves", slots=show.slots,
                       options=list(HOME[:3]), split=2)
    with pytest.raises(AssertionError, match="show_page changed during a trial"):
        r.o._send_ui(changed)


def test_twenty_trials_unattended_speak_only_confirmed_phrases():
    rng = random.Random(7)
    r = Rig()
    utterances = {o.utterance for lst in INTENTS.fallback_lists.values() for o in lst}
    for _ in range(20):
        r.to_trial()
        if rng.random() < 0.2:
            r.run_out()
        else:
            r.select(rng.choice([LEFT, RIGHT]))
            r.run_out()
        if r.o.state is St.SPEAKING:
            r.o.on_ui_message(TTSDone(utterance_id=r.last(r.ui, Speak).utterance_id))
    assert set(r.spoken()) <= utterances
    confirms = [m for m in r.ui if isinstance(m, ShowPage) and m.mode == "confirm"]
    assert len(r.spoken()) <= len(confirms)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_an_untrained_bridge_is_calibrated_after_the_baseline():
    r = Rig(trained=False)
    r.o.on_ui_message(Resume())
    r.finish_baseline()
    assert r.o.state is St.CALIBRATING
    show = r.last(r.ui, ShowPage)
    assert show.mode == "calibrate" and show.cue == LEFT and show.text == "Look at the left card"
    assert show.list_id == "calibrate" and show.page >= 1
    assert [(s.slot, s.role) for s in show.slots] == [(LEFT, "left"), (RIGHT, "right")]
    assert r.o.deadline - r.clock.t == pytest.approx(CFG.stim.cue_s)  # still first
    assert r.last(r.ui, State).detail == CALIBRATING_DETAIL
    r.run_out()
    start = r.last(r.decoder, StimStart)
    assert start.active_slots == [LEFT, RIGHT]
    train = r.last(r.decoder, Train)
    assert r.decoder.index(train) == r.decoder.index(start) + 1  # right after the stim_start
    assert train == Train(train_id=train.train_id, trial_id=start.trial_id, slot=LEFT, cycles=20)
    assert r.last(r.ui, Trial) == Trial(trial_id=start.trial_id, phase="flicker")
    assert r.o.deadline - r.clock.t == pytest.approx(CALIBRATION_S)


def test_calibration_relays_markers_ignores_selections_and_ends_on_train_done():
    r = Rig(trained=False)
    train = r.to_calibration_trial()
    r.onset()
    assert r.last(r.decoder, StimMarkers).trial_id == train.trial_id
    r.select(LEFT)
    assert r.o.state is St.CALIBRATING  # a selection never ends the calibration
    r.o.on_decoder_message(TrainDone(train_id=train.train_id, ok=True, accuracy=0.85, cycles=20))
    assert r.last(r.decoder, StimStop) == StimStop(trial_id=train.trial_id, reason="selected")
    status = r.last(r.ui, UIStatus)
    assert status.trained and status.calibration_accuracy == 0.85
    assert r.o.bridge_trained and r.o.calibration_accuracy == 0.85
    assert r.o.state is St.MENU_CUE and r.last(r.ui, State).detail == "Calibrated: 85 %"
    assert r.items() == PAGE


def test_a_low_calibration_score_runs_the_session_with_a_warning():
    r = Rig(trained=False)
    train = r.to_calibration_trial()
    r.o.on_decoder_message(TrainDone(train_id=train.train_id, ok=True, accuracy=0.55))
    detail = r.last(r.ui, State).detail
    assert r.o.state is St.MENU_CUE and detail.startswith("Calibrated: 55 %") and "low" in detail


@pytest.mark.parametrize("how", ["not_ok", "deadline"])
def test_a_failed_calibration_pauses(how):
    r = Rig(trained=False)
    train = r.to_calibration_trial()
    if how == "not_ok":
        r.o.on_decoder_message(TrainDone(train_id=train.train_id, ok=False,
                                         message="too few cycles"))
        assert r.last(r.ui, State).detail == "Calibration failed: too few cycles"
    else:
        r.run_out()
        assert r.last(r.ui, State).detail.startswith("Calibration failed")
    assert r.o.state is St.PAUSED
    assert r.last(r.decoder, StimStop).reason == "paused"
    assert r.last(r.decoder, Pause).reason == "calibration_failed"
    assert not r.o.bridge_trained
    # Resume tries again: baseline, then calibration.
    r.o.on_ui_message(Resume())
    r.finish_baseline()
    assert r.o.state is St.CALIBRATING


def test_a_stale_train_done_is_ignored():
    r = Rig(trained=False)
    train = r.to_calibration_trial()
    r.o.on_decoder_message(TrainDone(train_id=train.train_id + 1, ok=True, accuracy=0.9))
    assert r.o.state is St.CALIBRATING
    r.o.on_ui_message(Stop())
    r.o.on_decoder_message(TrainDone(train_id=train.train_id, ok=True, accuracy=0.9))
    assert r.o.state is St.PAUSED and not r.o.bridge_trained


def test_a_trained_bridge_skips_calibration():
    r = Rig(trained=True)
    r.to_trial()
    assert not [m for m in r.decoder if isinstance(m, Train)]


def test_keyboard_mode_without_a_bridge_skips_calibration():
    r = Rig(keyboard_mode=True, bridge=False)
    r.o.on_ui_message(Calibrate())
    r.to_trial()
    assert r.o.state is St.STIMULATING
    assert not [m for m in r.ui if isinstance(m, ShowPage) and m.mode == "calibrate"]


def test_calibrate_while_idle_calibrates_at_the_next_start():
    r = Rig(trained=True)
    r.o.on_ui_message(Calibrate())
    assert r.o.state is St.IDLE
    train = r.to_calibration_trial()
    r.o.on_decoder_message(TrainDone(train_id=train.train_id, ok=True, accuracy=0.9))
    assert r.o.state is St.MENU_CUE
    r.o.on_ui_message(Stop())
    r.to_trial()  # calibrated once: the next start goes straight to the page
    assert len([m for m in r.decoder if isinstance(m, Train)]) == 1


def test_calibrate_during_a_trial_stops_it_and_calibrates_now():
    r = Rig(trained=True)
    r.look(LEFT)
    r.to_trial()
    trial = r.o.active_trial
    r.o.on_ui_message(Calibrate())
    assert r.last(r.decoder, StimStop) == StimStop(trial_id=trial, reason="context_changed")
    assert r.o.state is St.CALIBRATING and r.last(r.ui, ShowPage).mode == "calibrate"
    train = r.to_calibration_trial()
    r.o.on_decoder_message(TrainDone(train_id=train.train_id, ok=True, accuracy=0.8))
    assert r.items() == PAGE  # the whole page again


def test_calibrate_while_a_reply_plays_waits_for_the_next_cue():
    r = Rig(trained=True)
    for slot in (LEFT, LEFT, LEFT, SAY_IT):
        r.look(slot)
    assert r.o.state is St.SPEAKING
    r.o.on_ui_message(Calibrate())
    assert r.o.state is St.SPEAKING
    r.o.on_ui_message(TTSDone(utterance_id=r.last(r.ui, Speak).utterance_id))
    r.run_out()  # REST -> the calibration instead of the next page
    assert r.o.state is St.CALIBRATING


def test_the_bridges_status_says_whether_it_is_trained():
    r = Rig(trained=False)
    assert not r.last(r.ui, UIStatus).trained
    r.status(trained=True)
    assert r.o.bridge_trained and r.last(r.ui, UIStatus).trained
    r.status()  # trained: None leaves it
    assert r.o.bridge_trained
    r.status(trained=False)
    assert not r.o.bridge_trained
    r.o.on_decoder_disconnected()
    assert not r.o.bridge_trained


# ---------------------------------------------------------------------------
# The phone's display report, and the phone coming and going
# ---------------------------------------------------------------------------


def test_resume_waits_for_the_phones_refresh_check():
    r = Rig(report=None)
    assert r.o.resume_problem() == "Waiting for the phone's refresh check"
    assert not r.last(r.ui, UIStatus).display_ok
    r.o.on_ui_message(Resume())
    assert r.o.state is St.IDLE
    r.o.on_ui_message(DisplayReport(refresh_hz=120.0, ok=False, message="120 Hz, not 60"))
    assert r.o.resume_problem() == "Phone display: 120 Hz, not 60"
    r.o.on_ui_message(Resume())
    assert r.o.state is St.IDLE
    r.o.on_ui_message(DisplayReport(refresh_hz=59.98, ok=True))
    status = r.last(r.ui, UIStatus)
    assert status.display_ok and status.resume_ready
    r.o.on_ui_message(Resume())
    assert r.o.state is St.BASELINE


def test_a_bad_display_report_during_a_session_pauses_it():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(DisplayReport(refresh_hz=48.0, ok=False, message="uneven frames"))
    assert r.o.state is St.PAUSED
    assert r.last(r.ui, State).detail == "Phone display: uneven frames"
    assert r.last(r.decoder, Pause).reason == "display_closed"
    assert r.last(r.decoder, StimStop).reason == "paused"


def test_a_phone_that_goes_away_pauses_and_must_report_again():
    r = Rig()
    r.to_trial()
    r.o.on_ui_disconnected()
    assert r.o.state is St.PAUSED and r.o.phone_display is None
    r.o.on_ui_connected()
    assert r.o.resume_problem() == "Waiting for the phone's refresh check"
    r.o.on_ui_message(OK_REPORT)
    r.to_trial()
    assert r.o.state is St.STIMULATING


def test_a_new_phone_mid_session_pauses_until_its_own_start():
    r = Rig()
    r.to_trial()
    r.o.on_ui_connected()
    assert r.o.state is St.PAUSED and r.last(r.ui, State).detail == "New screen: press start"


# ---------------------------------------------------------------------------
# config/default.yaml is untouched by all this
# ---------------------------------------------------------------------------


def test_default_config_sends_no_stimulus_train_or_markers():
    from tests.test_orchestrator import Rig as FreqRig

    r = FreqRig()
    r.o.on_ui_connected()
    r.to_trial()
    r.o.on_ui_message(CycleMarkers(trial_id=r.o.active_trial,
                                   markers=[CycleMarker(cycle=0, t_unix=1.0)]))
    r.o.on_ui_message(DisplayReport(refresh_hz=120.0, ok=False))
    r.o.on_ui_message(Calibrate())
    kinds = {type(m) for m in r.decoder} | {type(m) for m in r.ui}
    assert not kinds & {Stimulus, Train, StimMarkers}
    assert r.o.state is St.STIMULATING and r.o.resume_problem() == ""
    assert r.o.code is None
    r.select("s1")  # paradigm freq needs no onset from markers
    assert r.o.state is St.SELECTED

