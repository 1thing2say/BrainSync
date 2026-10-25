"""The orchestrator in code mode (the default config, c-VEP; docs/CVEP-STANDARD.md section 5).

Three slots: option 1 on s1, option 2 on s2, NEXT on s3; the confirm screen flickers only s1
("Say it") and s3 ("Back"). The BASELINE step trains instead: ``train`` to the bridge, then
``trained``; decisions during a trial reach the partner page as ``confidence``.
"""

from __future__ import annotations

import pytest

from app.config import FVEP_PAGE_CONFIG_PATH, load_config, load_intents
from app.orchestrator import Orchestrator, PageClockOffset, St
from app.schemas import (
    PROTOCOL,
    Baseline,
    BaselineDone,
    CodeParams,
    Confidence,
    Cycle,
    Decision,
    DecoderHello,
    Display,
    HelloAck,
    PageClock,
    PageCycle,
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
    Stop,
    Train,
    Trained,
    Training,
    TrainProgress,
    Trial,
    TTSDone,
)

CFG = load_config()
INTENTS = load_intents()
HOME = INTENTS.fallback_lists["home"]


def code_hello(**changes) -> DecoderHello:
    fields = dict(protocol=PROTOCOL, name="brainsync-cvep", paradigm="code", refresh_hz=59.94,
                  code=CodeParams(length=63, cycle_frames=63, train_cycles=30),
                  slots=[SlotSpec(slot=s.slot, lag_frames=s.lag_frames) for s in CFG.stim.slots])
    fields.update(changes)
    return DecoderHello(**fields)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


class Rig:
    def __init__(self, cfg=CFG, bridge: bool = True, **hooks) -> None:
        self.clock = Clock()
        self.ui: list = []
        self.decoder: list = []
        self.o = Orchestrator(cfg, INTENTS, self.ui.append, self.decoder.append, clock=self.clock,
                              **hooks)
        if bridge:
            self.o.on_decoder_connected()
            self.o.on_decoder_message(code_hello())

    def sent(self, kind):
        return [m for m in self.decoder if isinstance(m, kind)]

    def shown(self, kind):
        return [m for m in self.ui if isinstance(m, kind)]

    def advance(self, seconds: float) -> None:
        self.clock.t += seconds
        self.o.tick()

    def run_out(self) -> None:
        assert self.o.deadline is not None
        self.clock.t = self.o.deadline
        self.o.tick()

    def train(self, accuracy: float = 0.83) -> Train:
        """Resume from IDLE and answer the train request."""
        self.o.on_ui_message(Resume())
        train = self.sent(Train)[-1]
        for k in (1, 15, 30):
            self.o.on_decoder_message(TrainProgress(train_id=train.train_id, cycle=k, of=30))
        self.o.on_decoder_message(Trained(train_id=train.train_id, ok=True, accuracy=accuracy,
                                          chance=1 / 3, cycles=30))
        return train

    def to_trial(self) -> StimStart:
        """From MENU_CUE: run out the cue, report the onset; the trial's stim_start."""
        assert self.o.state is St.MENU_CUE, self.o.state
        self.run_out()
        start = self.sent(StimStart)[-1]
        self.o.on_decoder_message(StimOnset(trial_id=start.trial_id, t_unix=self.clock.t))
        return start

    def select(self, slot: str) -> None:
        start = self.sent(StimStart)[-1]
        self.o.on_decoder_message(Selection(trial_id=start.trial_id, slot=slot, t_unix=self.clock.t,
                                            score=0.4, scores={s: 0.1 for s in start.active_slots}
                                            | {slot: 0.4}))
        self.run_out()  # SELECTED -> the next step


def test_matching_code_hello_is_acked_with_three_roles():
    r = Rig()
    (ack,) = r.sent(HelloAck)
    assert ack.roles == {"s1": "option", "s2": "option", "s3": "next"}
    assert ack.labels == {"s1": HOME[0].utterance, "s2": HOME[1].utterance, "s3": "More options"}
    assert r.o.acked and r.o.decoder_name == "brainsync-cvep"


@pytest.mark.parametrize("hello, fragment", [
    (code_hello(paradigm="freq", waveform="sine",
                slots=[SlotSpec(slot=s, freq_hz=f) for s, f in (("s1", 13.0), ("s2", 15.0),
                                                                ("s3", 17.0))]),
     "paradigm freq, this server runs code (start it with --config config/fvep_page.yaml)"),
    (code_hello(slots=[SlotSpec(slot="s1", lag_frames=0), SlotSpec(slot="s2", lag_frames=21),
                       SlotSpec(slot="s3", lag_frames=40)]), "s3 lags 40 frames, expected 42"),
    (code_hello(code=CodeParams(length=31, cycle_frames=31)), "code length 31, expected 63"),
    (code_hello(slots=[SlotSpec(slot=f"s{k + 1}", lag_frames=k * 21) for k in range(2)]),
     "slots ['s1', 's2'], expected ['s1', 's2', 's3']"),
    (code_hello(refresh_hz=120.0), "display refresh is 120 Hz"),
], ids=["freq hello", "wrong lag", "wrong code", "two slots", "ProMotion"])
def test_a_code_hello_that_does_not_match_is_rejected(hello, fragment):
    r = Rig(bridge=False)
    r.o.on_decoder_connected()
    r.o.on_decoder_message(hello)
    (err,) = r.sent(ProtocolError)
    assert fragment in err.message
    assert not r.o.acked


def test_a_code_hello_is_rejected_by_an_fvep_server():
    fvep = load_config(FVEP_PAGE_CONFIG_PATH)
    intents = load_intents(option_count=3, label_max_chars=18, utterance_max_words=14,
                           label_max_words=3)
    decoder: list = []
    o = Orchestrator(fvep, intents, lambda m: None, decoder.append, clock=Clock())
    o.on_decoder_connected()
    o.on_decoder_message(code_hello())
    (err,) = [m for m in decoder if isinstance(m, ProtocolError)]
    assert "paradigm code, this server runs freq (start it with --config config/default.yaml)" \
        in err.message


def test_session_start_trains_on_the_first_slot_instead_of_a_baseline():
    r = Rig()
    r.o.on_ui_message(Resume())
    assert r.o.state is St.BASELINE
    (train,) = r.sent(Train)
    assert (train.slot, train.label, train.cycles, train.retrain) == ("s1", "left", 30, True)
    assert not r.sent(Baseline)  # no baseline in code mode
    assert r.shown(Trial)[-1].phase == "train"
    assert r.shown(Training)[-1] == Training(slot="s1", label="left", cycle=0, of=30)
    assert r.shown(State)[-1].detail == "Training: look at the left square, cycle 0/30"
    # Progress reaches the partner page, and the state line counts the cycles.
    r.o.on_decoder_message(TrainProgress(train_id=train.train_id, cycle=12, of=30))
    assert r.shown(Training)[-1].cycle == 12
    assert r.shown(State)[-1].detail == "Training: look at the left square, cycle 12/30"
    # trained: the accuracy is shown and the first page comes up.
    r.o.on_decoder_message(Trained(train_id=train.train_id, ok=True, accuracy=0.83, chance=1 / 3,
                                   cycles=30))
    done = r.shown(Training)[-1]
    assert done.accuracy == 0.83 and done.cycle == 30 and done.chance == pytest.approx(1 / 3)
    assert r.o.state is St.MENU_CUE and r.o.train_accuracy == 0.83
    page = r.shown(ShowPage)[-1]
    assert page.mode == "page"
    assert [(s.slot, s.role, s.utterance) for s in page.slots] == [
        ("s1", "option", HOME[0].utterance), ("s2", "option", HOME[1].utterance),
        ("s3", "next", "")]
    display = r.sent(Display)[-1]
    assert display.labels == {"s1": HOME[0].utterance, "s2": HOME[1].utterance,
                              "s3": "More options"}


def test_a_late_or_foreign_trained_changes_nothing():
    r = Rig()
    r.o.on_ui_message(Resume())
    train = r.sent(Train)[-1]
    r.o.on_decoder_message(Trained(train_id=train.train_id + 5, ok=True, accuracy=0.9, cycles=30))
    r.o.on_decoder_message(BaselineDone(baseline_id=train.train_id, ok=True))  # not a baseline
    assert r.o.state is St.BASELINE


def test_a_failed_training_pauses_and_resume_trains_again_without_retrain():
    r = Rig()
    r.o.on_ui_message(Resume())
    train = r.sent(Train)[-1]
    r.o.on_decoder_message(Trained(train_id=train.train_id, ok=False, message="interrupted"))
    assert r.o.state is St.PAUSED
    assert r.shown(State)[-1].detail == "Training failed: interrupted"
    assert r.sent(Pause)[-1].reason == "train_failed"
    assert r.shown(Training)[-1].message == "interrupted"
    r.o.on_ui_message(Resume())
    again = r.sent(Train)[-1]
    assert again.train_id > train.train_id and again.retrain is False


def test_no_trained_within_the_timeout_pauses():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.advance(CFG.train.timeout_s - 0.1)
    assert r.o.state is St.BASELINE
    r.advance(0.2)
    assert r.o.state is St.PAUSED
    assert "Training failed: no result within 60 s" in r.shown(State)[-1].detail


def test_keyboard_mode_without_a_bridge_keeps_the_still_screen():
    r = Rig(bridge=False, keyboard_mode=True)
    r.o.on_ui_message(Resume())
    assert r.o.state is St.BASELINE and not r.sent(Train)
    r.advance(CFG.baseline.seconds)
    assert r.o.state is St.MENU_CUE


def test_option_confirm_say_speaks_with_three_targets_and_two_on_confirm():
    r = Rig()
    r.train()
    start = r.to_trial()
    assert start.active_slots == ["s1", "s2", "s3"]
    r.select("s2")
    assert r.o.state is St.MENU_CUE and r.o.picked.utterance == HOME[1].utterance
    confirm = r.shown(ShowPage)[-1]
    assert confirm.mode == "confirm" and confirm.text == HOME[1].utterance
    assert [(s.slot, s.role) for s in confirm.slots] == [("s1", "say"), ("s3", "back")]
    assert r.sent(Display)[-1].labels == {"s1": "Say it", "s2": "", "s3": "Back"}
    start = r.to_trial()
    assert start.active_slots == ["s1", "s3"]
    r.select("s1")
    assert r.o.state is St.SPEAKING
    speak = r.shown(Speak)[-1]
    assert speak.text == HOME[1].utterance
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    assert r.o.state is St.REST


def test_back_on_s3_speaks_nothing():
    r = Rig()
    r.train()
    r.to_trial()
    r.select("s1")
    r.to_trial()
    r.select("s3")  # Back
    assert r.o.state is St.REST and not r.shown(Speak)


def test_next_on_s3_turns_the_page():
    r = Rig()
    r.train()
    first = r.shown(ShowPage)[-1]
    r.to_trial()
    r.select("s3")
    assert r.o.state is St.REST
    r.run_out()
    page = r.shown(ShowPage)[-1]
    assert page.mode == "page" and page.page == first.page + 1
    assert [s.utterance for s in page.slots if s.role == "option"] == [
        o.utterance for o in INTENTS.fallback_lists["more"][:2]]


def test_decisions_reach_the_partner_page_as_confidence_only_during_their_trial():
    r = Rig()
    r.train()
    start = r.to_trial()
    scores = {"s1": 0.42, "s2": 0.11, "s3": -0.03}
    r.o.on_decoder_message(Decision(trial_id=start.trial_id, scores=scores, raw="s1",
                                    committed=None, lead=0.31))
    (conf,) = r.shown(Confidence)
    assert conf == Confidence(trial_id=start.trial_id, scores=scores, raw="s1", committed=None,
                              lead=0.31)
    r.o.on_decoder_message(Decision(trial_id=start.trial_id - 1, scores=scores, raw="s1"))
    assert len(r.shown(Confidence)) == 1  # a stale trial's decision is dropped
    r.select("s1")
    r.o.on_decoder_message(Decision(trial_id=start.trial_id, scores=scores, raw="s1",
                                    committed="s1", changed=True))
    assert len(r.shown(Confidence)) == 1  # the trial is over


def test_stop_during_training_pauses_and_the_bridge_is_told():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.o.on_ui_message(Stop())
    assert r.o.state is St.PAUSED and r.o.expected_train is None
    assert r.sent(Pause)[-1].reason == "stop_button"
    train = r.sent(Train)[-1]
    r.o.on_decoder_message(Trained(train_id=train.train_id, ok=False, message="interrupted"))
    assert r.o.state is St.PAUSED  # the bridge's answer to the cut-short training is stale


def test_a_lost_bridge_resumes_with_a_training_that_may_reuse_the_template():
    r = Rig()
    r.train()
    r.to_trial()
    r.o.on_decoder_disconnected()
    assert r.o.state is St.PAUSED
    r.o.on_decoder_connected()
    r.o.on_decoder_message(code_hello())
    again = r.sent(Train)[-1]
    assert r.o.state is St.BASELINE and again.retrain is False


def test_fallback_pages_show_the_first_two_ranked_options():
    r = Rig()
    assert [o.utterance for o in r.o.options.options] == [o.utterance for o in HOME[:2]]


# ---------------------------------------------------------------------------
# The session page draws the code (display.flicker: browser; draft section 9.7)
# ---------------------------------------------------------------------------

BROWSER = CFG.model_copy(update={"display": CFG.display.model_copy(update={"flicker": "browser"})})
PSYCHOPY = CFG.model_copy(update={"display": CFG.display.model_copy(update={"flicker": "psychopy"})})


def test_the_page_clock_offset_is_the_median_and_never_after_receipt():
    c = PageClockOffset(size=3)
    assert c.offset is None and c.unix(5.0, 9.0) == 9.0  # no clock message yet: the receipt
    for t_client, t_recv in ((100.0, 100.002), (101.0, 101.010), (102.0, 102.003)):
        c.add(t_client, t_recv)
    assert c.offset == pytest.approx(0.003)
    assert c.unix(103.5, 104.0) == pytest.approx(103.503)
    assert c.unix(103.5, 103.501) == 103.501  # capped at the receipt
    c.add(103.0, 103.5)  # the oldest of 3 drops out: 0.010, 0.003, 0.5
    assert c.offset == pytest.approx(0.010)
    c.clear()
    assert c.offset is None


def page_cycle(r: Rig, phase: str, n: int, t: float, trial_id: int | None = None) -> None:
    trial = r.shown(Trial)[-1].trial_id if trial_id is None else trial_id
    r.o.on_ui_message(PageCycle(phase=phase, trial_id=trial, n=n, t_client=t))


def test_the_pages_training_cycles_go_to_the_bridge_on_the_servers_clock():
    r = Rig(BROWSER)
    r.o.on_ui_connected()
    r.o.on_ui_message(PageClock(t_client=r.clock.t - 0.002, refresh_hz=60.0))
    r.o.on_ui_message(Resume(start=True))
    train = r.sent(Train)[-1]
    assert r.shown(Trial)[-1].phase == "train"
    r.clock.t += 0.5
    page_cycle(r, "train", 0, r.clock.t - 0.3)
    r.clock.t += 1.05
    page_cycle(r, "train", 1, r.clock.t - 0.3)
    page_cycle(r, "flicker", 2, r.clock.t)  # no trial runs
    cycles = r.sent(Cycle)
    assert [(c.train_id, c.trial_id, c.n) for c in cycles] == [(train.train_id, None, 0),
                                                               (train.train_id, None, 1)]
    assert cycles[1].t_unix == pytest.approx(r.clock.t - 0.298)  # + the 2 ms clock offset
    r.o.on_decoder_message(Trained(train_id=train.train_id, ok=True, accuracy=0.8, cycles=30))
    page_cycle(r, "train", 3, r.clock.t)  # the training is over
    assert len(r.sent(Cycle)) == 2


def test_the_pages_trial_cycles_go_to_the_bridge_for_the_trial_in_progress_only():
    r = Rig(BROWSER)
    r.train()
    start = r.to_trial()
    assert r.shown(Trial)[-1] == Trial(trial_id=start.trial_id, phase="flicker")
    r.clock.t += 0.1
    page_cycle(r, "flicker", 0, r.clock.t)
    page_cycle(r, "flicker", 0, r.clock.t, trial_id=start.trial_id - 1)  # an old trial
    page_cycle(r, "train", 0, r.clock.t)  # not training
    (cycle,) = r.sent(Cycle)
    assert (cycle.trial_id, cycle.train_id, cycle.n) == (start.trial_id, None, 0)
    assert cycle.t_unix == r.clock.t  # no clock message: the receipt time
    r.select("s1")  # the trial ends
    page_cycle(r, "flicker", 1, r.clock.t, trial_id=start.trial_id)
    assert len(r.sent(Cycle)) == 1


def test_a_cycle_stamped_before_its_flicker_was_asked_for_is_dropped():
    r = Rig(BROWSER)
    r.train()
    start = r.to_trial()
    page_cycle(r, "flicker", 0, r.clock.t - 1.0, trial_id=start.trial_id)
    r.o.on_ui_message(PageClock(t_client=r.clock.t))  # offset 0: the page's time is believed
    page_cycle(r, "flicker", 0, r.clock.t - 1.0, trial_id=start.trial_id)
    assert len(r.sent(Cycle)) == 1  # the first used the receipt time; the second is too early


def test_with_psychopy_drawing_the_code_page_cycles_are_ignored():
    r = Rig(PSYCHOPY)
    r.o.on_ui_message(Resume())
    page_cycle(r, "train", 0, r.clock.t)
    assert not r.sent(Cycle)


def test_a_page_at_the_wrong_refresh_cannot_start_and_a_new_page_measures_again():
    r = Rig(BROWSER)
    r.o.on_ui_connected()
    r.o.on_ui_message(PageClock(t_client=r.clock.t, refresh_hz=120.0))
    problem = "This screen runs at 120 Hz: set it to 60 Hz (ProMotion off), then reload the page"
    assert r.o.resume_problem() == problem and not r.o.status().resume_ready
    assert r.shown(State)[-1].detail == problem
    r.o.on_ui_message(Resume(start=True))
    assert r.o.state is St.IDLE and not r.sent(Train)
    r.o.on_ui_connected()  # a reload, on a 60 Hz screen
    assert r.o.resume_problem() == ""
    r.o.on_ui_message(PageClock(t_client=r.clock.t, refresh_hz=59.9))
    r.o.on_ui_message(Resume(start=True))
    assert r.o.state is St.BASELINE and r.sent(Train)


def test_with_psychopy_drawing_the_page_refresh_does_not_matter():
    r = Rig(PSYCHOPY)
    r.o.on_ui_message(PageClock(t_client=r.clock.t, refresh_hz=120.0))
    assert r.o.resume_problem() == ""
