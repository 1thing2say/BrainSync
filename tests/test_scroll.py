"""Tests for the scroll gate: commits -> scrolls, cooldown, voter-only re-arm."""
from __future__ import annotations

import math

import numpy as np
import pytest

from brainsync.board import FakeBoard
from brainsync.classify import ABSTAIN, make
from brainsync.online import Decision, Decoder, Voter
from brainsync.scroll import COOLDOWN_S, Action, ScrollGate

STEP = 0.25   # decoder step, s


def dec(committed: int, changed: bool, raw: int | None = None) -> Decision:
    return Decision(time=0.0, scores=np.zeros(2), raw=committed if raw is None else raw,
                    committed=committed, changed=changed)


def simulate(raws: list[int], cooldown_s: float = COOLDOWN_S, votes: int = 4,
             gate_votes: int | None = None) -> tuple[list[Action], ScrollGate, list[float]]:
    """Feed raw window predictions through a real Voter (one per STEP) and the gate, the
    way brainsync.reels wires them; returns the actions, the gate and the re-arm times."""
    voter = Voter(votes)
    rearmed: list[float] = []
    now = [0.0]

    def rearm() -> None:
        voter.reset()
        rearmed.append(now[0])

    gate = ScrollGate(cooldown_s=cooldown_s, votes=votes if gate_votes is None else gate_votes,
                      rearm=rearm)
    actions = []
    for i, raw in enumerate(raws):
        now[0] = i * STEP
        changed = voter.push(raw)
        a = gate.push(Decision(time=now[0], scores=np.zeros(2), raw=raw,
                               committed=voter.committed, changed=changed), now[0])
        if a is not None:
            actions.append(a)
    return actions, gate, rearmed


def test_a_new_commit_fires_one_scroll_in_its_direction():
    gate = ScrollGate()
    assert gate.push(dec(ABSTAIN, False), 0.0) is None
    a = gate.push(dec(1, True), 0.25)
    assert a == Action(dir="down", target=1, id=1, time=0.25)
    assert gate.cooling and gate.fired == 1


def test_unchanged_commit_does_not_fire_again():
    gate = ScrollGate(cooldown_s=0.0)
    assert gate.push(dec(0, True), 0.0) is not None
    assert gate.push(dec(0, False), 0.25) is None      # re-arms (cooldown 0)
    for k in range(10):
        assert gate.push(dec(0, False), 0.5 + k * STEP) is None
    assert gate.fired == 1


def test_cooldown_ignores_every_decision_then_rearms_once():
    calls = []
    gate = ScrollGate(cooldown_s=1.5, rearm=lambda: calls.append(1))
    assert gate.push(dec(0, True), 10.0).dir == "up"
    for t in (10.25, 10.5, 11.0, 11.49):
        assert gate.push(dec(1, True), t) is None      # a different target, still ignored
    assert calls == [] and gate.cooling and gate.ignored == 4
    assert gate.push(dec(1, True), 11.5) is None       # cooldown over: re-arm, dropped
    assert calls == [1] and not gate.cooling and gate.rearms == 1
    assert gate.push(dec(1, True), 11.75).dir == "down"
    assert gate.ignored == 5


def test_dwelling_on_one_square_scrolls_about_every_two_and_a_half_seconds():
    # 4 abstentions, then 12 s staring at "down"
    actions, gate, rearmed = simulate([ABSTAIN] * 4 + [1] * 48)
    times = [a.time for a in actions]
    assert all(a.dir == "down" for a in actions)
    assert times[0] == pytest.approx(7 * STEP)          # 4th agreeing window
    gaps = np.diff(times)
    # 1.5 s cooldown, re-arm at its end, then 4 more agreeing windows (1.0 s)
    np.testing.assert_allclose(gaps, 2.5)
    assert len(actions) == 1 + (48 * STEP - 1.0) // 2.5
    assert [a.id for a in actions] == list(range(1, len(actions) + 1))
    assert rearmed == pytest.approx([t + 1.5 for t in times[:len(rearmed)]])


def test_watching_the_video_never_scrolls():
    actions, gate, rearmed = simulate([ABSTAIN] * 200)
    assert actions == [] and rearmed == [] and gate.fired == 0
    gate = ScrollGate(cooldown_s=0.0)
    for k in range(20):   # even a commit change to ABSTAIN (a withdrawal) never fires
        assert gate.push(dec(ABSTAIN, k % 2 == 0), k * STEP) is None


def test_switching_target_scrolls_the_other_way():
    actions, _, _ = simulate([0] * 12 + [1] * 12)
    assert [a.dir for a in actions] == ["up", "down"]
    # "down" commits at the 4th window after the switch (index 15) unless still cooling;
    # the up scroll at 0.75 s re-armed at 2.25 s (index 9), well before
    assert actions[1].time == pytest.approx(15 * STEP)
    assert actions[1].time - actions[0].time >= COOLDOWN_S


def test_glance_away_and_back_during_cooldown_waits_for_rearm():
    # up at index 3; the viewer looks at "down" at once: committed by index 7, inside the
    # cooldown, so ignored; re-arm at 0.75 + 1.5 = 2.25 s, then 4 fresh windows
    actions, gate, _ = simulate([0] * 4 + [1] * 10)
    assert [a.dir for a in actions] == ["up", "down"]
    assert gate.ignored >= 1
    assert actions[1].time == pytest.approx(2.25 + 4 * STEP)


def test_stale_commit_right_after_rearm_is_ignored():
    # several decisions scored before the caller saw the re-arming one: the voter state
    # in the 2nd and 3rd predates the reset
    gate = ScrollGate(cooldown_s=1.0, votes=4)
    assert gate.push(dec(0, True), 0.0) is not None
    assert gate.push(dec(ABSTAIN, False), 1.0) is None      # re-arm
    assert gate.push(dec(1, True), 1.25) is None            # stale: 1 decision since
    assert gate.push(dec(1, False), 1.5) is None
    assert gate.push(dec(1, False), 1.75) is None
    assert gate.push(dec(1, True), 2.0).dir == "down"        # 4th: a fresh vote can commit
    assert gate.ignored == 1


def test_votes_one_lets_the_first_decision_after_rearm_fire():
    gate = ScrollGate(cooldown_s=1.0, votes=1)
    gate.push(dec(0, True), 0.0)
    gate.push(dec(ABSTAIN, False), 1.0)
    assert gate.push(dec(0, True), 1.25).id == 2


def test_ids_increase_and_reset_restarts_them():
    gate = ScrollGate(cooldown_s=0.0)
    ids = []
    t = 0.0
    for k in range(6):
        ids.append(gate.push(dec(k % 2, True), t).id)
        gate.push(dec(ABSTAIN, False), t + 0.25)   # re-arm
        t += 0.5
    assert ids == [1, 2, 3, 4, 5, 6]
    gate.reset()
    assert not gate.cooling and gate.fired == 0
    assert gate.push(dec(0, True), 0.0).id == 1


def test_custom_directions_map_targets():
    gate = ScrollGate(("down", "up"))
    assert gate.push(dec(0, True), 0.0).dir == "down"
    one = ScrollGate(("down",))
    assert one.push(dec(0, True), 0.0).dir == "down"
    with pytest.raises(ValueError, match="maps only 1 target"):
        ScrollGate(("down",)).push(dec(1, True), 0.0)


@pytest.mark.parametrize("kwargs, match", [
    ({"directions": ()}, "at least one"),
    ({"directions": ("up", "left")}, "'left'"),
    ({"directions": ("15", "17")}, "up, down or pause"),
    ({"cooldown_s": -1.0}, "cooldown_s"),
    ({"cooldown_s": math.nan}, "cooldown_s"),
    ({"cooldown_s": math.inf}, "cooldown_s"),
    ({"votes": 0}, "votes"),
    ({"votes": 2.5}, "votes"),
    ({"votes": True}, "votes"),
])
def test_bad_arguments(kwargs, match):
    with pytest.raises(ValueError, match=match):
        ScrollGate(**kwargs)


def test_time_must_not_go_backwards_or_be_nan():
    gate = ScrollGate()
    gate.push(dec(ABSTAIN, False), 5.0)
    gate.push(dec(ABSTAIN, False), 5.0)          # equal is fine
    with pytest.raises(ValueError, match="backwards"):
        gate.push(dec(ABSTAIN, False), 4.0)
    with pytest.raises(ValueError, match="finite"):
        gate.push(dec(ABSTAIN, False), math.nan)


def test_rearm_resets_only_the_voter_so_the_buffer_keeps_deciding():
    # the real decoder on a FakeBoard locked on "down": scrolls repeat ~2.5 s apart and
    # decisions never pause for a buffer refill (that would be a 3.5 s gap)
    board = FakeBoard([15.0, 17.0], switch_s=math.inf, seed=3)
    board.set_target(1)
    clf = make("fbcca", freqs=[15.0, 17.0], fs=board.fs, margin=0.0, threshold=None)
    decoder = Decoder(clf, board.fs, window_s=1.5, step_s=STEP, votes=4)
    gate = ScrollGate(cooldown_s=1.5, votes=4, rearm=decoder.voter.reset)
    ends, actions = [], []
    for _ in range(int(20 / 0.1)):
        board.advance(0.1)
        for d in decoder.feed(*board.read()):
            t = d.end / decoder.fs
            ends.append(t)
            a = gate.push(d, t)
            if a is not None:
                actions.append(a)
    assert np.diff(ends).max() == pytest.approx(STEP, abs=0.005)   # 62 samples
    assert len(actions) >= 6 and {a.dir for a in actions} == {"down"}
    # the step is 62 samples (0.248 s), so the first decision past the 1.5 s cooldown is the
    # 7th (1.736 s) and four more make 2.728 s
    assert np.median(np.diff([a.time for a in actions])) == pytest.approx(2.728, abs=0.01)
    assert gate.rearms >= len(actions) - 1


# -- three actions (the c-VEP layout: up, pause, down) ---------------------------------------

CODE_STEP = 63 / 60.0   # one c-VEP decision per cycle


def simulate_code(raws: list[int], cooldown_s: float = COOLDOWN_S, votes: int = 2):
    """simulate() for the c-VEP reels layout: three targets, one decision per cycle."""
    voter = Voter(votes)
    gate = ScrollGate(("up", "pause", "down"), cooldown_s=cooldown_s, votes=votes,
                      rearm=voter.reset)
    actions = []
    for i, raw in enumerate(raws):
        changed = voter.push(raw)
        a = gate.push(Decision(time=i * CODE_STEP, scores=np.zeros(3), raw=raw,
                               committed=voter.committed, changed=changed), i * CODE_STEP)
        if a is not None:
            actions.append(a)
    return actions, gate


def test_three_targets_map_to_up_pause_down():
    gate = ScrollGate(("up", "pause", "down"), cooldown_s=0.0)
    assert gate.push(dec(0, True), 0.0).dir == "up"
    gate.push(dec(ABSTAIN, False), 0.5)
    assert gate.push(dec(2, True), 1.0).dir == "down"
    gate.push(dec(ABSTAIN, False), 1.5)
    assert gate.push(dec(1, True), 2.0) == Action(dir="pause", target=1, id=3, time=2.0)


def test_dwelling_on_down_scrolls_every_four_cycles():
    actions, gate = simulate_code([2] * 30)
    assert {a.dir for a in actions} == {"down"} and len(actions) >= 6
    np.testing.assert_allclose(np.diff([a.time for a in actions]), 4 * CODE_STEP)


def test_dwelling_on_pause_toggles_once_per_look():
    actions, gate = simulate_code([1] * 30)
    assert [a.dir for a in actions] == ["pause"]
    assert gate.ignored == 1            # the re-commit after the re-arm is dropped
    # one decision elsewhere (or abstaining) releases it: looking back toggles again
    actions, _ = simulate_code([1] * 8 + [ABSTAIN] + [1] * 8)
    assert [a.dir for a in actions] == ["pause", "pause"]
    actions, _ = simulate_code([1] * 8 + [0] + [1] * 8)
    assert [a.dir for a in actions] == ["pause", "pause"]


def test_once_can_be_changed():
    gate = ScrollGate(("up", "pause", "down"), cooldown_s=0.0, once=())
    assert gate.push(dec(1, True), 0.0).dir == "pause"
    gate.push(dec(ABSTAIN, False, raw=1), 0.5)
    assert gate.push(dec(1, True), 1.0).dir == "pause"       # repeats on dwell
    gate = ScrollGate(("up", "pause", "down"), cooldown_s=0.0, once=("up",))
    assert gate.push(dec(0, True), 0.0).dir == "up"
    gate.push(dec(ABSTAIN, False, raw=0), 0.5)
    assert gate.push(dec(0, True), 1.0) is None              # held
    gate.push(dec(ABSTAIN, False, raw=2), 1.5)              # released
    assert gate.push(dec(0, True), 2.0).dir == "up"


def test_every_target_needs_its_own_action():
    with pytest.raises(ValueError, match="its own action"):
        ScrollGate(("up", "up"))
    with pytest.raises(ValueError, match="'like'"):
        ScrollGate(("up", "like", "down"))


def test_rearm_resets_only_the_cvep_votes_so_cycles_keep_deciding():
    from brainsync.vep.live import CodeVepDecoder, standard_fake_board

    board = standard_fake_board(fixated=0, seed=4)
    decoder = CodeVepDecoder(board.fs, train_cycles=12)
    board.start_flicker()
    while decoder.training:
        board.advance(0.1)
        decoder.feed(*board.read(), board.read_events())
    board.set_target(2)
    gate = ScrollGate(("up", "pause", "down"), cooldown_s=1.5, votes=2,
                      rearm=decoder.decoder.voter.reset)
    times, actions = [], []
    for _ in range(int(25 / 0.1)):
        board.advance(0.1)
        for d in decoder.feed(*board.read(), board.read_events()):
            times.append(d.time)
            a = gate.push(d, d.time)
            if a is not None:
                actions.append(a)
    assert np.diff(times).max() == pytest.approx(CODE_STEP, abs=0.01)   # no blind spell
    assert len(actions) >= 4 and {a.dir for a in actions} == {"down"}
    assert np.median(np.diff([a.time for a in actions])) == pytest.approx(4 * CODE_STEP,
                                                                          abs=0.01)
