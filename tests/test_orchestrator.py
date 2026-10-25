"""Orchestrator transitions with a fake clock: no timers, sockets or threads (PLAN.md section 9).

f-VEP page mode (config/fvep_page.yaml): 3 options on s1-s3 and NEXT on s4, a confirm screen with
"Say it" on s1 and "Back" on s4. List mode (the 2-target fallback): SAY on s1, NEXT on s2. Code
mode (the default config, c-VEP) is in test_orchestrator_code.py.
"""

from __future__ import annotations

import random

import pytest

from app.config import FVEP_PAGE_CONFIG_PATH, load_config, load_intents
from app.context import PageRequest, Turn
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
    Option,
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

CFG = load_config(FVEP_PAGE_CONFIG_PATH)
INTENTS = load_intents(option_count=CFG.menus.option_count, label_max_chars=CFG.menus.label_max_chars,
                       utterance_max_words=CFG.menus.utterance_max_words,
                       label_max_words=CFG.menus.label_max_words)
HOME = INTENTS.fallback_lists["home"]
YES_NO = INTENTS.fallback_lists["yes_no"]
MORE = INTENTS.fallback_lists["more"]
OPTION_SLOTS, NEXT = ["s1", "s2", "s3"], "s4"
SAY_IT, BACK = CFG.stim.confirm.say, CFG.stim.confirm.back  # s1, s4

# List mode: two slots, SAY / NEXT, no confirm step.
# The PsychoPy window draws the flicker and the page is a still mirror: a page may come and go.
CFG_PSYCHOPY = CFG.model_copy(update={"display": CFG.display.model_copy(update={"flicker": "psychopy"})})
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

    def __init__(self, cfg=CFG, keyboard_mode: bool = False, bridge: bool = True, **hooks) -> None:
        self.cfg = cfg
        self.clock = Clock()
        self.ui: list = []
        self.decoder: list = []
        self.o = Orchestrator(cfg, INTENTS, self.ui.append, self.decoder.append, clock=self.clock,
                              keyboard_mode=keyboard_mode, **hooks)
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


def test_closing_the_flickering_page_pauses_the_session():
    r = Rig()  # display.flicker: browser, the default
    r.to_trial()
    r.o.on_ui_disconnected()
    assert r.o.state is St.PAUSED and r.last(r.ui, State).detail == "Screen closed"
    assert r.last(r.decoder, StimStop).reason == "paused"
    assert r.last(r.decoder, Pause).reason == "screen_closed"


def test_a_new_page_mid_session_pauses_until_its_own_start():
    r = Rig()
    r.to_trial()
    r.o.on_ui_connected()  # a reload or a second tab: it flickers only after its start button
    assert r.o.state is St.PAUSED and r.last(r.ui, State).detail == "New screen: press start"
    r.o.on_ui_message(Resume(start=True))
    assert r.o.state is St.BASELINE


def test_a_page_coming_and_going_while_idle_or_paused_changes_nothing():
    r = Rig()
    r.o.on_ui_disconnected()
    r.o.on_ui_connected()
    assert r.o.state is St.IDLE
    r.to_trial()
    r.o.on_ui_message(Stop())
    r.o.on_ui_disconnected()
    assert r.o.state is St.PAUSED and r.last(r.ui, State).detail == "Stopped"


def test_with_psychopy_drawing_the_flicker_the_page_may_close_mid_trial():
    r = Rig(CFG_PSYCHOPY)
    r.to_trial()
    r.o.on_ui_disconnected()
    assert r.o.state is St.STIMULATING


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


@pytest.mark.parametrize("name, flag, fake", [
    ("fake-decoder", False, True),  # tools/fake_decoder.py started by hand
    ("Fake-random", False, True),
    ("ssvep-fbcca-fake", False, True),  # "fake" anywhere in the name, not only at the start
    ("FakeSource", False, True),
    ("ssvep-fbcca", False, False),  # the teammate's bridge
    ("ssvep-fbcca", True, True),  # --fake-decoder
])
def test_the_acked_hello_name_says_whether_the_decoder_is_fake(name, flag, fake):
    r = Rig(fake_decoder=flag, bridge=False)
    r.connect(name=name)
    status = r.last(r.ui, UIStatus)
    assert (status.fake_decoder, status.decoder_name) == (fake, name)


def test_only_an_acked_hello_names_the_decoder_and_a_disconnect_clears_it():
    r = Rig(bridge=False)
    r.connect(name="fake-decoder")
    r.o.on_decoder_message(bridge_hello(name="fake-decoder", refresh_hz=120.0))  # rejected
    assert (r.o.decoder_name, r.last(r.ui, UIStatus).fake_decoder) == ("", False)
    r.connect(name="fake-decoder")
    r.o.on_decoder_disconnected()
    status = r.last(r.ui, UIStatus)
    assert (status.decoder_name, status.fake_decoder) == ("", False)


def test_the_log_says_which_decoder_was_acked_and_whether_it_is_fake(tmp_path):
    from app.log import EventLog, read_events

    log = EventLog(tmp_path)
    r = Rig(bridge=False, log=log)
    r.connect(name="fake-decoder")
    r.connect(name="ssvep-fbcca")
    acked = [(e["name"], e["fake"]) for e in read_events(log.path) if e["event"] == "decoder_acked"]
    assert acked == [("fake-decoder", True), ("ssvep-fbcca", False)]


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


def _pause_by_stop(r):
    r.o.on_ui_message(Stop())


def _pause_by_esc(r):
    r.status(display_ok=False, message="Esc pressed")


def _pause_by_decoder_loss(r):
    r.o.on_decoder_disconnected()


def _pause_by_rejected_hello(r):
    r.o.on_decoder_message(bridge_hello(refresh_hz=120.0))


@pytest.mark.parametrize("pause", [_pause_by_stop, _pause_by_esc, _pause_by_decoder_loss,
                                   _pause_by_rejected_hello])
@pytest.mark.parametrize("when", ["cue", "flicker", "confirm"])
def test_every_pause_tells_the_mirror_the_trial_phase_is_paused(pause, when):
    """The partner page's selection timer runs from phase "flicker" until any other phase; a
    pause sent only a state, so the timer kept counting after STOP, Esc or a lost decoder."""
    r = Rig()
    if when == "confirm":
        r.pick_and_confirm_screen("s1")
        r.run_out()
    else:
        r.to_trial()
    if when == "cue":
        r.o.on_decoder_message(NoSelection(trial_id=r.o.active_trial, reason="low_signal"))
        r.run_out()  # NOT_CAUGHT -> REST
        r.run_out()  # REST -> MENU_CUE
        assert r.o.state is St.MENU_CUE
    else:
        r.o.on_decoder_message(StimOnset(trial_id=r.o.active_trial, t_unix=r.clock.t))
        assert r.last(r.ui, Trial).phase == "flicker"
    pause(r)
    assert r.o.state is St.PAUSED
    assert r.last(r.ui, Trial) == Trial(trial_id=r.o.trial_id, phase="paused")


@pytest.mark.parametrize("fail", ["baseline_done", "timeout"])
def test_a_failed_baseline_tells_the_mirror_the_phase_is_paused(fail):
    r = Rig()
    r.o.on_ui_message(Resume())
    assert r.last(r.ui, Trial).phase == "baseline"
    if fail == "baseline_done":
        r.finish_baseline(ok=False, message="O1 contact lost")
    else:
        r.advance(CFG.baseline.timeout_s)
    assert r.o.state is St.PAUSED and r.last(r.ui, Trial).phase == "paused"


def test_keyboard_mode_stop_during_a_trial_ends_the_mirror_flicker():
    r = Rig(keyboard_mode=True, bridge=False)
    r.to_trial()
    assert r.last(r.ui, Trial).phase == "flicker"  # no bridge: shown at stim_start
    r.o.on_ui_message(Stop())
    assert r.last(r.ui, Trial).phase == "paused"


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


def test_each_new_page_is_handed_to_tts_and_speak_carries_its_clip():
    pages: list[list[str]] = []
    ready = {HOME[0].utterance: "/audio/abc"}
    r = Rig(on_page=pages.append, audio_url=ready.get)
    assert pages == [[o.utterance for o in HOME]]  # the first page, set at start-up
    r.pick_and_confirm_screen("s1")
    r.to_trial()
    r.select(SAY_IT)
    r.run_out()
    speak = r.last(r.ui, Speak)
    assert speak.text == HOME[0].utterance and speak.audio_url == "/audio/abc"
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    r.to_trial()
    assert pages[-1] == [o.utterance for o in HOME]  # after speaking, a new page (home again; cached)
    r.select(NEXT)  # a new page of 3 -> prefetched as well
    r.run_out()
    assert pages[-1] == [o.utterance for o in MORE] == [o.utterance for o in r.o.options.options]


def test_speak_without_a_clip_has_no_audio_url():
    r = Rig(audio_url=lambda text: None)
    r.pick_and_confirm_screen("s2")
    r.to_trial()
    r.select(SAY_IT)
    r.run_out()
    assert r.last(r.ui, Speak).audio_url is None


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
    r = Rig(CFG_PSYCHOPY)
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


# ---------------------------------------------------------------------------
# LLM pages (V3): request_page / on_page_ready
# ---------------------------------------------------------------------------

LLM_OPTIONS = [
    Option(label="Let's go", utterance="Yes, let's go outside.", intent_id="yes"),
    Option(label="Not now", utterance="Not right now, thanks.", intent_id="later"),
    Option(label="After lunch", utterance="Maybe after lunch.", intent_id="maybe"),
]
QUESTION = "Do you want to go outside?"


class LLMRig(Rig):
    """A Rig whose pages come from a fake LLM: ``requests`` collects what it was asked."""

    def __init__(self, cfg=CFG, **kwargs) -> None:
        self.requests: list[PageRequest] = []
        super().__init__(cfg, request_page=self.requests.append, **kwargs)

    def partner(self, text: str = QUESTION) -> PageRequest:
        self.o.on_ui_message(PartnerText(text=text, source="typed"))
        return self.requests[-1]

    def answer(self, options=LLM_OPTIONS, req: PageRequest | None = None) -> None:
        self.o.on_page_ready((req or self.requests[-1]).request_id, options)

    def page_utterances(self) -> list[str]:
        return [s.utterance for s in self.last(self.ui, ShowPage).slots if s.role == "option"]


def test_partner_line_asks_the_llm_and_waits_for_its_page():
    r = LLMRig()
    r.to_trial()
    req = r.partner()
    assert req.reason == "partner" and req.partner_last == QUESTION
    assert req.shown == () and req.history[-1] == Turn("partner", QUESTION)
    assert r.last(r.decoder, StimStop).reason == "context_changed"
    assert r.o.state is St.PREPARING  # still screen, nothing flickers while waiting
    r.answer()
    assert r.o.state is St.MENU_CUE
    assert r.page_utterances() == [o.utterance for o in LLM_OPTIONS]  # ranked: option 1 on s1
    assert r.last(r.decoder, Display).labels["s1"] == LLM_OPTIONS[0].utterance
    assert r.last(r.ui, UIStatus).llm_ok


def test_start_from_a_new_page_begins_at_home_with_an_empty_conversation():
    r = LLMRig()
    r.to_trial()
    req = r.partner()  # the room's talk made a page of its own
    r.answer(req=req)
    r.o.on_ui_message(Stop())
    r.o.on_ui_message(Resume(start=True))
    assert r.o.conversation.turns == []
    while r.o.state is not St.MENU_CUE:
        r.finish_baseline() if r.o.state is St.BASELINE else r.run_out()
    assert r.page_utterances() == [o.utterance for o in HOME]


def test_start_from_a_reloaded_page_while_running_goes_home_at_once():
    r = LLMRig()
    r.to_trial()
    req = r.partner()
    assert r.o.state is St.PREPARING  # waiting for Claude's page
    r.o.on_ui_message(Resume(start=True))
    assert r.o.state is St.MENU_CUE
    assert r.page_utterances() == [o.utterance for o in HOME]
    r.answer(req=req)  # the old conversation's page arrives late: dropped
    assert r.page_utterances() == [o.utterance for o in HOME]
    r.to_trial()
    r.o.on_ui_message(Resume(start=True))  # mid-trial: the trial ends, home again
    assert r.last(r.decoder, StimStop).reason == "context_changed"
    assert r.page_utterances() == [o.utterance for o in HOME]


def test_plain_resume_keeps_the_conversation():
    r = LLMRig()
    r.to_trial()
    r.answer(req=r.partner())
    r.o.on_ui_message(Stop())
    r.o.on_ui_message(Resume())
    assert r.o.conversation.partner_last == QUESTION


def cue_length(r) -> float:
    """How long the MENU_CUE just entered lasts."""
    assert r.o.state is St.MENU_CUE
    return r.o.deadline - r.clock.t


def words_on(show) -> int:
    return len(" ".join(s.utterance or s.label for s in show.slots).split())


def test_a_new_page_stays_still_long_enough_to_read_it():
    r = Rig()
    r.o.on_ui_message(Resume())
    r.finish_baseline()
    stim = CFG.stim
    page = r.last(r.ui, ShowPage)
    expected = min(stim.cue_s + stim.read_s_per_word * words_on(page), stim.cue_max_s)
    assert cue_length(r) == pytest.approx(expected)
    assert cue_length(r) > stim.cue_s  # more than the bare minimum for three sentences


def test_the_reading_time_has_a_cap():
    long = CFG.model_copy(update={"stim": CFG.stim.model_copy(update={"read_s_per_word": 5.0})})
    r = Rig(long)
    r.o.on_ui_message(Resume())
    r.finish_baseline()
    assert cue_length(r) == pytest.approx(CFG.stim.cue_max_s)


def test_the_confirm_screen_and_a_page_seen_again_get_short_cues():
    r = Rig()
    r.pick_and_confirm_screen("s1")
    assert cue_length(r) == pytest.approx(CFG.stim.confirm_cue_s)
    r.to_trial()
    r.select(BACK)  # the same page again: already read
    while r.o.state is not St.MENU_CUE:
        r.run_out()
    assert r.last(r.ui, ShowPage).mode == "page"
    assert cue_length(r) == pytest.approx(CFG.stim.cue_s)


def test_llm_timeout_shows_the_fallback_and_ignores_the_late_page():
    r = LLMRig()
    r.to_trial()
    req = r.partner()
    r.advance(CFG.llm.timeout_s)
    assert r.o.state is St.MENU_CUE
    assert r.page_utterances() == [o.utterance for o in YES_NO]  # the question fallback
    assert not r.last(r.ui, UIStatus).llm_ok
    r.answer(req=req)  # too late: the user is already reading the fallback
    assert r.page_utterances() == [o.utterance for o in YES_NO]


def test_llm_failure_shows_the_fallback_at_once():
    r = LLMRig()
    r.to_trial()
    r.partner()
    r.answer(None)
    assert r.o.state is St.MENU_CUE and r.page_utterances() == [o.utterance for o in YES_NO]


def test_a_newer_partner_line_replaces_the_pending_request():
    r = LLMRig()
    r.to_trial()
    first = r.partner("Are you hungry?")
    second = r.partner()
    assert second.request_id != first.request_id and r.o.state is St.PREPARING
    r.answer(req=first)  # answers the old question: ignored
    assert r.o.state is St.PREPARING
    r.answer(req=second)
    assert r.page_utterances() == [o.utterance for o in LLM_OPTIONS]


def test_more_options_asks_the_llm_for_different_replies():
    r = LLMRig()
    r.to_trial()
    r.partner()
    r.answer()
    r.to_trial()
    r.select(NEXT)
    r.run_out()  # SELECTED -> REST
    req = r.requests[-1]
    assert req.reason == "more" and req.shown == tuple(o.utterance for o in LLM_OPTIONS)
    r.run_out()  # REST -> PREPARING
    assert r.o.state is St.PREPARING
    more = [Option(label=f"New {i}", utterance=f"Something new {i}.", intent_id="social")
            for i in range(3)]
    r.answer(more)
    assert r.page_utterances() == [o.utterance for o in more]


def test_the_confirm_screen_never_waits_for_the_llm():
    r = LLMRig()
    r.to_trial()
    r.partner()
    r.answer()
    r.pick_and_confirm_screen("s1")
    assert r.last(r.ui, ShowPage).mode == "confirm" and r.o.state is St.MENU_CUE


def test_spoken_reply_joins_the_conversation_and_logs_its_rank(tmp_path):
    from app.log import EventLog, read_events

    log = EventLog(tmp_path)
    r = LLMRig()
    r.o.log = log
    r.to_trial()
    r.partner()
    r.answer()
    r.pick_and_confirm_screen("s2")
    r.to_trial()
    r.select(SAY_IT)
    r.run_out()
    assert r.spoken() == [LLM_OPTIONS[1].utterance]
    spoken = [e for e in read_events(log.path) if e["event"] == "spoken"]
    assert spoken[-1]["rank"] == 2 and spoken[-1]["list"] == "llm"
    r.o.on_ui_message(TTSDone(utterance_id=r.last(r.ui, Speak).utterance_id))
    asked = len(r.requests)
    r.run_out()  # REST -> MENU_CUE: after a spoken reply the home page, without asking Claude
    assert r.o.state is St.MENU_CUE and len(r.requests) == asked
    assert r.page_utterances() == [o.utterance for o in HOME]
    next_req = r.partner("Great, where to?")
    assert [t.text for t in next_req.history][-3:] == [QUESTION, LLM_OPTIONS[1].utterance,
                                                      "Great, where to?"]
    log.close()


def test_without_an_llm_partner_text_shows_the_fallback_at_once():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(PartnerText(text=QUESTION, source="typed"))
    assert r.o.state is St.MENU_CUE and not r.last(r.ui, UIStatus).llm_ok


def fresh(tag: str) -> list[Option]:
    """Three LLM options no fallback list has."""
    return [Option(label=f"{tag} {i}", utterance=f"{tag} reply {i}.", intent_id="social")
            for i in range(3)]


def page_options(show: ShowPage) -> list[str]:
    """The option sentences on a page or list screen."""
    if show.mode == "list":
        return [o.utterance for o in show.options]
    return [s.utterance for s in show.slots if s.role == "option"]


def statuses(r: Rig) -> list[UIStatus]:
    return [m for m in r.ui if isinstance(m, UIStatus)]


@pytest.mark.parametrize("answer", ["llm", "timeout"])
def test_partner_line_right_after_next_wins_over_more_options(answer):
    r = LLMRig()
    r.to_trial()
    r.select(NEXT)
    assert r.o.state is St.SELECTED
    req = r.partner()
    r.run_out()  # SELECTED -> REST: no "more" request on top of the partner's
    assert r.requests == [req] and r.o.awaiting == req.request_id
    r.run_out()  # REST -> PREPARING
    assert r.o.state is St.PREPARING
    if answer == "llm":
        r.answer()
        expected = [o.utterance for o in LLM_OPTIONS]
    else:
        r.run_out()
        expected = [o.utterance for o in YES_NO]  # the question fallback, not the "more" page
    assert r.o.state is St.MENU_CUE and r.page_utterances() == expected
    r.to_trial()
    r.select(NEXT)  # the first NEXT on the answer page: more answers, not home
    r.run_out()
    assert r.requests[-1].reason == "more"


def test_partner_line_right_after_next_without_an_llm_shows_the_question_page():
    r = Rig()
    r.to_trial()
    r.select(NEXT)
    r.o.on_ui_message(PartnerText(text=QUESTION, source="typed"))
    r.to_trial()
    assert [s.utterance for s in r.last(r.ui, ShowPage).slots[:3]] == [o.utterance for o in YES_NO]
    r.select(NEXT)
    r.run_out()
    r.to_trial()
    assert [s.utterance for s in r.last(r.ui, ShowPage).slots[:3]] == [o.utterance for o in MORE]


def test_llm_ok_follows_the_last_page():
    r = LLMRig()
    r.to_trial()
    for text, answer, ok, change in [
        (QUESTION, LLM_OPTIONS, True, 1),
        ("Again?", None, False, 1),  # a failed page turns the badge off
        ("And now?", None, False, 0),  # still failing: no new status
        ("Once more?", LLM_OPTIONS, True, 1),
        ("Hello?", "timeout", False, 1),
    ]:
        req = r.partner(text)
        n = len(statuses(r))
        if answer == "timeout":
            r.advance(CFG.llm.timeout_s)
        else:
            r.answer(answer)
        assert len(statuses(r)) == n + change and statuses(r)[-1].llm_ok is ok
        r.to_trial()
    n = len(statuses(r))
    r.answer(LLM_OPTIONS, req)  # nobody waits for it any more: ignored, the badge stays off
    assert len(statuses(r)) == n and not r.o.llm_ok


def test_llm_status_tells_not_asked_yet_from_failed():
    # Before Claude's first page the partner page must not warn "fixed replies only".
    assert Rig().o.status().llm == "off"  # no Claude at all
    r = LLMRig()
    r.to_trial()
    assert r.o.status().llm == "waiting" and not r.o.status().llm_ok
    r.partner("Again?")
    r.answer(None)
    assert r.o.status().llm == "failed"
    r.to_trial()
    r.partner(QUESTION)
    r.answer(LLM_OPTIONS)
    assert r.o.status().llm == "ok" and r.o.status().llm_ok


def test_partner_line_during_the_confirm_step_waits_for_the_new_page():
    r = LLMRig()
    r.to_trial()
    r.partner()
    r.answer()
    r.pick_and_confirm_screen("s1")
    r.to_trial()
    assert r.o.state is St.CONFIRMING
    req = r.partner("Are you cold?")
    assert r.last(r.decoder, StimStop).reason == "context_changed"
    assert r.o.state is St.PREPARING and r.o.picked is None and r.spoken() == []
    new = fresh("Cold")
    r.answer(new, req)
    assert r.o.state is St.MENU_CUE and r.page_utterances() == [o.utterance for o in new]


@pytest.mark.parametrize("late", [False, True], ids=["in time", "late"])
def test_page_arriving_during_the_confirm_trial_is_held_until_the_next_cue(late):
    r = LLMRig()
    r.to_trial()
    r.select("s1")
    req = r.partner()  # while SELECTED on an option
    r.run_out()  # the confirm screen does not wait for Claude
    assert r.o.state is St.MENU_CUE and r.last(r.ui, ShowPage).mode == "confirm"
    r.to_trial()
    if late:
        r.advance(CFG.llm.timeout_s)  # still in the confirm trial, past llm.timeout_s
    assert r.o.state is St.CONFIRMING
    n_ui, n_decoder = len(r.ui), len(r.decoder)
    new = fresh("Out")
    r.answer(new, req)
    assert r.o.state is St.CONFIRMING  # rule 3: the screen does not change during the trial
    assert not [m for m in r.ui[n_ui:] if isinstance(m, ShowPage)]
    assert not [m for m in r.decoder[n_decoder:] if isinstance(m, Display)]
    r.select(SAY_IT)
    r.run_out()
    assert r.spoken() == [HOME[0].utterance]
    r.o.on_ui_message(TTSDone(utterance_id=r.o.speaking_id))
    r.to_trial()
    # A page after llm.timeout_s is dropped (the fixed list stays), even when it came mid-trial.
    expected = YES_NO if late else new
    assert r.page_utterances() == [o.utterance for o in expected]
    assert r.o.llm_ok is not late


def test_more_options_twice_goes_home_without_asking_again():
    r = LLMRig()
    r.to_trial()
    r.partner()
    r.answer()
    r.to_trial()
    r.select(NEXT)
    r.run_out()
    r.run_out()  # REST -> PREPARING
    r.answer(fresh("More"))
    r.to_trial()
    r.select(NEXT)  # the second NEXT in a row
    r.run_out()
    asked, n = len(r.requests), len(r.ui)
    r.to_trial()
    assert len(r.requests) == asked
    assert St.PREPARING.value not in [m.name for m in r.ui[n:] if isinstance(m, State)]
    assert r.page_utterances() == [o.utterance for o in HOME]


@pytest.mark.parametrize("more_page", ["llm", "timeout"])
def test_list_mode_uses_llm_pages_and_starts_each_at_the_top(more_page):
    r = LLMRig(CFG_LIST)
    r.to_trial()
    r.partner()
    assert r.o.state is St.PREPARING
    r.answer()
    shown = r.last(r.ui, ShowPage)
    assert shown.mode == "list" and shown.highlight_index == 0 and shown.options == LLM_OPTIONS
    for _ in LLM_OPTIONS:  # NEXT through the list; on the last option ("MORE") it asks Claude
        r.to_trial()
        r.select("s2")
        r.run_out()
    req = r.requests[-1]
    assert req.reason == "more" and req.shown == tuple(o.utterance for o in LLM_OPTIONS)
    r.run_out()  # REST -> PREPARING
    assert r.o.state is St.PREPARING
    if more_page == "llm":
        new = fresh("New")
        r.answer(new)
        expected = [o.utterance for o in new]
    else:
        r.run_out()
        expected = [o.utterance for o in MORE]
    shown = r.last(r.ui, ShowPage)
    assert r.o.state is St.MENU_CUE and shown.highlight_index == 0 and shown.text == expected[0]
    assert [o.utterance for o in shown.options] == expected


def run_unattended_llm(cfg, trials: int = 30, seed: int = 5):
    """run_unattended with a flaky fake Claude and partner lines at random moments.

    Each request gets one fate: a fresh page in time, a failure (None), a page after
    llm.timeout_s, or no answer at all. Returns the rig, the request reason behind each LLM
    sentence, the sentences that came too late, and the states partner lines arrived in.
    """
    rng = random.Random(seed)
    r = LLMRig(cfg)
    timeout = cfg.llm.timeout_s
    due: list[tuple[float, int, list[Option] | None]] = []  # (when, request_id, options)
    reason_of: dict[str, str] = {}
    late: set[str] = set()
    partner_states: set[St] = set()
    planned = 0

    def plan_answers() -> None:
        nonlocal planned
        for req in r.requests[planned:]:
            fate = rng.choice(["in time", "in time", "in time", "failed", "late", "never"])
            options = fresh(f"R{req.request_id}")
            if fate == "in time":
                reason_of.update({o.utterance: req.reason for o in options})
                due.append((r.clock.t + rng.uniform(0.0, 0.9 * timeout), req.request_id, options))
            elif fate == "failed":
                due.append((r.clock.t + rng.uniform(0.0, 0.9 * timeout), req.request_id, None))
            elif fate == "late":
                late.update(o.utterance for o in options)
                due.append((r.clock.t + timeout + rng.uniform(0.0, 3.0), req.request_id, options))
        planned = len(r.requests)

    def advance_to(t: float) -> None:
        """Move the clock to ``t``, delivering answers and firing deadlines in time order."""
        while True:
            plan_answers()
            pending = [d[0] for d in due] + ([r.o.deadline] if r.o.deadline is not None else [])
            r.clock.t = max(r.clock.t, min([t] + pending))
            for d in [d for d in due if d[0] <= r.clock.t]:
                due.remove(d)
                r.o.on_page_ready(d[1], d[2])
            r.o.tick()
            if r.clock.t >= t:
                plan_answers()
                return

    r.o.on_ui_message(Resume())
    for _ in range(20_000):
        if r.o.trial_id >= trials and r.o.state is St.REST:
            return r, reason_of, late, partner_states
        if rng.random() < 0.1:
            partner_states.add(r.o.state)
            r.partner(rng.choice([QUESTION, "Are you hungry?", "I'm back.", "Nice day today."]))
        if r.o.state in (St.STIMULATING, St.CONFIRMING):
            advance_to(r.clock.t + rng.uniform(0.1, 3.0))
            if r.o.state in (St.STIMULATING, St.CONFIRMING) and rng.random() < 0.85:
                r.select(rng.choice(list(r.o.screen.roles)))
        elif r.o.state is St.SPEAKING:
            if rng.random() < 0.5:
                advance_to(r.clock.t + rng.uniform(0.2, 1.0))  # still speaking
            else:
                r.o.on_ui_message(TTSDone(utterance_id=r.o.speaking_id))
        elif r.o.state is St.BASELINE:
            r.finish_baseline()
        else:  # in small steps, so partner lines also land in the middle of waits
            advance_to(min(r.o.deadline, r.clock.t + rng.uniform(0.2, 1.5)))
        assert r.o.deadline is not None, f"stuck in {r.o.state}"
    raise AssertionError("did not finish")


def fixed_cue(cfg):
    """A 1 s cue on every screen, as before the reading time: run_unattended_llm sends a partner
    line with a fixed chance per step, so longer cues (more steps) would bury every NEXT under
    partner lines and no "More options" page would ever show."""
    return cfg.model_copy(update={"stim": cfg.stim.model_copy(
        update={"cue_s": 1.0, "read_s_per_word": 0.0, "confirm_cue_s": 1.0})})


@pytest.mark.parametrize("cfg", [CFG, CFG_LIST], ids=["page", "list"])
def test_unattended_with_a_flaky_llm_and_partner_lines_at_any_moment(cfg):
    r, reason_of, late, partner_states = run_unattended_llm(fixed_cue(cfg))
    assert r.spoken()
    # Every spoken sentence was on screen right before: the confirm text, or the highlighted option.
    for i, msg in enumerate(r.ui):
        if isinstance(msg, Speak):
            before = [m for m in r.ui[:i] if isinstance(m, ShowPage)][-1]
            assert before.mode == ("confirm" if cfg.menus.mode == "page" else "list")
            assert before.text == msg.text
    shown = {u for m in r.ui if isinstance(m, ShowPage) and m.mode != "confirm"
             for u in page_options(m)}
    assert not shown & late  # a page after llm.timeout_s is never shown
    assert {reason_of[u] for u in shown if u in reason_of} == {"partner", "more"}
    fallbacks = {o.utterance for name in ("yes_no", "more") for o in INTENTS.fallback_lists[name]}
    assert shown & fallbacks  # Claude failed or was too late at least once
    trial_states = {St.STIMULATING, St.CONFIRMING} if cfg.menus.mode == "page" else {St.STIMULATING}
    assert trial_states | {St.SELECTED, St.SPEAKING, St.PREPARING} <= partner_states
    starts = [m.trial_id for m in r.decoder if isinstance(m, StimStart)]
    stops = [m.trial_id for m in r.decoder if isinstance(m, StimStop)]
    assert starts == sorted(set(starts)) and stops == starts[: len(stops)]


# ---------------------------------------------------------------------------
# V4: hands-free listening on the partner's turn; only questions interrupt a selection
# ---------------------------------------------------------------------------

from app.orchestrator import is_question  # noqa: E402
from app.schemas import Listen  # noqa: E402


def listens(r: Rig) -> list[Listen]:
    return [m for m in r.ui if isinstance(m, Listen)]


CFG_TURN = CFG.model_copy(update={"speech": CFG.speech.model_copy(update={"stt_mode": "turn"})})


def test_the_mic_opens_at_the_start_and_after_each_spoken_reply_and_never_while_speaking():
    r = Rig(CFG_TURN)
    r.o.on_ui_message(Resume())
    assert listens(r)[-1] == Listen(seconds=CFG.speech.listen_window_s, delay_s=0.0, reason="start")
    r.pick_and_confirm_screen("s1")
    r.to_trial()
    r.select(SAY_IT)
    r.run_out()  # SELECTED -> SPEAKING
    assert r.o.state is St.SPEAKING
    off, speak = listens(r)[-1], r.last(r.ui, Speak)
    assert off.seconds == 0 and off.reason == "speaking"
    assert r.ui.index(off) < r.ui.index(speak)  # the mic is off before the reply starts
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    on = listens(r)[-1]
    assert on.reason == "turn" and on.seconds == CFG.speech.listen_window_s
    assert on.delay_s == CFG.speech.stt_tail_s  # a short tail, so it never hears itself


def test_pause_closes_the_mic():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(Stop())
    assert listens(r)[-1].seconds == 0 and listens(r)[-1].reason == "paused"


def test_push_to_talk_mode_never_opens_the_mic_by_itself():
    cfg = CFG.model_copy(update={"speech": CFG.speech.model_copy(update={"stt_mode": "push_to_talk"})})
    r = Rig(cfg)
    r.o.on_ui_message(Resume())
    assert all(m.seconds == 0 for m in listens(r))


def test_a_spoken_remark_during_a_selection_waits_for_the_next_cue():
    r = Rig()
    r.to_trial()
    trial = r.o.active_trial
    r.o.on_ui_message(PartnerText(text="I just got back from the store", source="stt"))
    assert r.o.state is St.STIMULATING and r.o.active_trial == trial  # the selection goes on
    assert not any(isinstance(m, StimStop) for m in r.decoder)
    assert r.last(r.ui, Transcript) == Transcript(who="partner", text="I just got back from the store")
    r.select("s2")  # the user finishes the pick on the old page
    r.run_out()
    assert r.last(r.ui, ShowPage).mode == "confirm"  # their pick is kept
    r.to_trial()
    r.select(BACK)
    r.run_out()  # back to a page: now the new replies (here the home fallback) are shown
    r.run_out()
    assert r.last(r.ui, ShowPage).mode == "page"


@pytest.mark.parametrize("text, source", [
    ("are you hungry", "stt"),  # a spoken question (no "?")
    ("Do you want some tea", "stt"),
    ("I just got back from the store", "typed"),  # typing is deliberate: always interrupts
])
def test_a_question_or_typed_text_interrupts_the_selection(text, source):
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(PartnerText(text=text, source=source))
    assert r.last(r.decoder, StimStop).reason == "context_changed"
    assert r.o.state is St.MENU_CUE


def test_a_spoken_question_gets_the_yes_no_fallback():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="do you want to go outside", source="stt"))
    shown = [s.utterance for s in r.last(r.ui, ShowPage).slots if s.role == "option"]
    assert shown == [o.utterance for o in YES_NO]


@pytest.mark.parametrize("text, question", [
    ("Do you want to go outside?", True),
    ("are you hungry", True),
    ("how was your weekend", True),
    ("so, are you cold", True),  # filler first
    ("Sam, do you want tea", True),  # a name first
    ("Ana, are you cold", True),
    ("Ana is here", False),
    ("you okay", True),
    ("Didn't you like it", True),
    ("I brought you some tea", False),
    ("It's raining outside", False),
    ("Good to see you", False),
    ("um", False),
    ("", False),
])
def test_is_question(text, question):
    assert is_question(text) is question


# ---------------------------------------------------------------------------
# Active listening (stt_mode: always): the mic is on the whole session except while a reply
# plays, paused, idle or muted; spoken lines never throw away a pick
# ---------------------------------------------------------------------------

from app.orchestrator import TRIAL_STATES as TRIAL_STATES_T  # noqa: E402
from app.schemas import MicReport, MicSet  # noqa: E402


def mic_on(m: Listen) -> bool:
    return m.until_stopped or m.seconds > 0


def say_it(r: Rig, slot: str = "s1") -> Speak:
    r.pick_and_confirm_screen(slot)
    r.to_trial()
    r.select(SAY_IT)
    r.run_out()
    assert r.o.state is St.SPEAKING
    return r.last(r.ui, Speak)


def test_always_mode_listens_from_resume_to_stop_as_one_level():
    r = Rig()
    r.o.on_ui_message(Resume())
    assert listens(r)[-1] == Listen(seconds=0, until_stopped=True, delay_s=0.0, reason="start")
    sent = len(listens(r))
    for _ in range(5):  # baseline, cues, trials, not-caught, rest: no more listen messages
        r.to_trial()
        r.run_out()
    assert len(listens(r)) == sent and mic_on(listens(r)[-1])
    r.o.on_ui_message(Stop())
    assert listens(r)[-1] == Listen(seconds=0, reason="paused")


def test_always_mode_closes_the_mic_for_a_reply_and_reopens_after_the_tail():
    r = Rig()
    speak = say_it(r)
    off = listens(r)[-1]
    assert not mic_on(off) and off.reason == "speaking" and r.ui.index(off) < r.ui.index(speak)
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    on = listens(r)[-1]
    assert on.until_stopped and on.reason == "turn" and on.delay_s == CFG.speech.stt_tail_s


def test_always_mode_reopens_after_a_reply_that_never_reports_done():
    r = Rig()
    say_it(r)
    r.run_out()  # max_speak_s with no tts_done
    assert r.o.state is St.REST and listens(r)[-1].until_stopped


def test_no_mic_on_message_is_ever_sent_while_speaking():
    """Unattended run with spoken lines at random moments: the reply is never heard."""
    r = Rig()
    rng = random.Random(7)
    for n in range(40):
        r.to_trial()
        if n % 4 == 0:
            r.o.on_ui_message(PartnerText(text=rng.choice(["are you hungry", "nice day"]), source="stt"))
            r.to_trial()
        r.select(rng.choice(list(r.o.screen.roles)))
        r.run_out()
        if r.o.state is St.SPEAKING:
            r.o.on_ui_message(TTSDone(utterance_id=r.o.speaking_id))
    for i, m in enumerate(r.ui):
        if isinstance(m, Speak):
            last = [x for x in r.ui[:i] if isinstance(x, Listen)][-1]
            assert not mic_on(last), "the mic was on when a reply started"


def test_a_reconnected_page_gets_the_mic_state():
    r = Rig(CFG_PSYCHOPY)
    r.to_trial()
    r.o.on_ui_connected()
    assert listens(r)[-1] == Listen(seconds=0, until_stopped=True, reason="reconnect")
    say_it(r)
    r.o.on_ui_connected()
    assert listens(r)[-1] == Listen(seconds=0, until_stopped=False, reason="reconnect")


def test_the_mic_reopens_after_a_decoder_reconnect_resume():
    r = Rig()
    r.to_trial()
    r.o.on_decoder_disconnected()
    assert r.o.state is St.PAUSED and not mic_on(listens(r)[-1])
    r.connect()  # the bridge comes back and its hello passes: automatic resume
    assert r.o.state is St.BASELINE and listens(r)[-1].until_stopped


def test_the_mic_reopens_after_a_decoder_reconnect_in_turn_mode_too():
    r = Rig(CFG_TURN)
    r.to_trial()
    r.o.on_decoder_disconnected()
    r.connect()
    assert listens(r)[-1].reason == "start" and listens(r)[-1].seconds == CFG.speech.listen_window_s


def test_mute_is_held_by_the_server_across_stop_resume_and_reconnect():
    r = Rig(CFG_PSYCHOPY)
    r.to_trial()
    r.o.on_ui_message(MicSet(muted=True))
    assert listens(r)[-1] == Listen(seconds=0, muted=True, reason="muted")
    assert r.last(r.ui, UIStatus).mic_muted
    r.o.on_ui_message(Stop())
    r.o.on_ui_message(Resume())
    assert not mic_on(listens(r)[-1])  # Resume does not open a muted mic
    r.o.on_ui_connected()
    assert listens(r)[-1].muted and not mic_on(listens(r)[-1])
    r.o.on_ui_message(MicSet(muted=False))
    assert listens(r)[-1].until_stopped and not listens(r)[-1].muted


def test_a_line_heard_while_muted_is_ignored():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(MicSet(muted=True))
    turns = len(r.o.conversation.turns)
    r.o.on_ui_message(PartnerText(text="are you hungry", source="stt"))
    assert len(r.o.conversation.turns) == turns and r.o.state is St.STIMULATING
    r.o.on_ui_message(PartnerText(text="Typed still works", source="typed"))
    assert r.o.conversation.turns[-1].text == "Typed still works"


def test_mute_in_turn_mode_blocks_the_turn_window():
    r = Rig(CFG_TURN)
    r.o.on_ui_message(MicSet(muted=True))
    speak = say_it(r)
    r.o.on_ui_message(TTSDone(utterance_id=speak.utterance_id))
    assert not any(mic_on(m) for m in listens(r)[-2:])


def test_mic_report_shows_in_the_status():
    r = Rig()
    r.o.on_ui_message(MicReport(state="offline", restarts=3))
    assert r.last(r.ui, UIStatus).mic == "offline"


def test_a_spoken_question_on_the_confirm_screen_keeps_the_pick():
    r = Rig()
    r.pick_and_confirm_screen("s2")
    r.to_trial()
    assert r.o.state is St.CONFIRMING
    r.o.on_ui_message(PartnerText(text="are you hungry", source="stt"))
    assert r.o.state is St.CONFIRMING and r.o.picked is not None  # the pick goes on
    r.select(SAY_IT)
    r.run_out()
    assert r.spoken() == [HOME[1].utterance]


def test_a_spoken_remark_on_the_confirm_cue_keeps_the_pick():
    r = Rig()
    r.pick_and_confirm_screen("s3")  # MENU_CUE with the confirm screen
    r.o.on_ui_message(PartnerText(text="it's sunny today", source="stt"))
    assert r.o.state is St.MENU_CUE and r.last(r.ui, ShowPage).mode == "confirm"


def test_typed_text_on_the_confirm_screen_still_drops_the_pick():
    r = Rig()
    r.pick_and_confirm_screen("s2")
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="Are you cold?", source="typed"))
    assert r.o.picked is None and r.last(r.decoder, StimStop).reason == "context_changed"


def test_spoken_question_interrupts_once_per_cooldown():
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="are you hungry", source="stt"))
    assert r.last(r.decoder, StimStop).reason == "context_changed"
    stops = sum(isinstance(m, StimStop) for m in r.decoder)
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="do you want tea", source="stt"))  # within 20 s: queued
    assert r.o.state is St.STIMULATING and sum(isinstance(m, StimStop) for m in r.decoder) == stops
    r.clock.t += CFG.speech.stt_interrupt_cooldown_s
    r.o.on_ui_message(PartnerText(text="are you cold", source="stt"))
    assert r.last(r.decoder, StimStop).reason == "context_changed"


def test_never_means_spoken_questions_wait_too():
    cfg = CFG.model_copy(update={"speech": CFG.speech.model_copy(update={"stt_interrupts": "never"})})
    r = Rig(cfg)
    r.to_trial()
    r.o.on_ui_message(PartnerText(text="are you hungry", source="stt"))
    assert r.o.state is St.STIMULATING and not any(isinstance(m, StimStop) for m in r.decoder)


def test_chatter_cannot_starve_the_trials():
    """A spoken question every 3 s for 2 minutes: selections still happen."""
    r = Rig()
    r.o.on_ui_message(Resume())
    picks = 0
    end = r.clock.t + 120
    next_line = r.clock.t
    while r.clock.t < end:
        if r.clock.t >= next_line:
            r.o.on_ui_message(PartnerText(text="are you okay", source="stt"))
            next_line = r.clock.t + 3
        if r.o.state is St.BASELINE and r.o.expected_baseline is not None:
            r.finish_baseline()
        elif r.o.state in TRIAL_STATES_T and r.clock.t - r.o.deadline + CFG.stim.max_trial_s > 2:
            r.select(next(iter(r.o.screen.roles)))
            picks += 1
        elif r.o.state is St.SPEAKING:
            r.o.on_ui_message(TTSDone(utterance_id=r.o.speaking_id))
        r.advance(0.5)
    assert picks >= 8


@pytest.mark.parametrize("text", ["it might not work right now. how was your weekend",
                                  "I got tea. Do you want some"])
def test_is_question_checks_each_sentence(text):
    assert is_question(text)


def test_stop_after_a_decoder_loss_stays_stopped_when_the_bridge_returns():
    r = Rig()
    r.to_trial()
    r.o.on_decoder_disconnected()  # paused by the decoder: would resume by itself on reconnect
    r.o.on_ui_message(Stop())  # the partner presses STOP while paused
    r.connect()  # the bridge comes back and its hello passes
    assert r.o.state is St.PAUSED  # STOP means stopped until Resume (rule 10)


# -- keyword-matched fallback lists (config/intents.yaml fallback_match) ----------------------


@pytest.mark.parametrize("text, name", [
    ("How are you?", "feeling"),
    ("hi sam how are you doing", "feeling"),  # "how are you" beats the greeting
    ("Sam, are you okay", "feeling"),
    ("you okay", "feeling"),
    ("How was your weekend", "how_was"),
    ("how did the appointment go", "how_was"),
    ("Hello there", "greeting"),
    ("good morning", "greeting"),
    ("What would you like to drink", "open_question"),
    ("so what do you want for dinner", "open_question"),
    ("Ana, where should we go", "open_question"),
    ("Do you want to go outside?", "yes_no"),
    ("are you hungry", "yes_no"),
    ("I don't know what to do", "home"),  # "what" in the middle is not a question
    ("It's raining outside", "home"),
])
def test_a_partner_line_gets_its_matching_fallback_list(text, name):
    assert INTENTS.fallback_for(text, is_question(text)) == name
    r = Rig()
    r.to_trial()
    r.o.on_ui_message(PartnerText(text=text, source="typed"))
    shown = [s.utterance for s in r.last(r.ui, ShowPage).slots if s.role == "option"]
    assert shown == [o.utterance for o in INTENTS.fallback_lists[name]]


def test_every_fallback_rule_shows_a_list_of_known_options():
    assert INTENTS.fallback_match, "config/intents.yaml has no fallback_match rules"
    for rule in INTENTS.fallback_match:
        assert rule.show in INTENTS.fallback_lists
