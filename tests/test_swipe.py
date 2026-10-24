"""Tests for the BrainSwipe logic: fresh-window swiping and the endless profile feed."""
from __future__ import annotations

import numpy as np
import pytest

from brainsync import swipe
from brainsync.board import FakeBoard
from brainsync.classify import ABSTAIN, make
from brainsync.online import TUNED, Decision, Decoder, calibrate
from brainsync.swipe import LIKE, PASS, Feed, Outcome, Swiper

FS = 250.0
FREQS = (15.0, 17.0)


def small_decoder(votes: int = 3) -> Decoder:
    """Window 250 samples, step 50, so stream positions in the tests are round numbers."""
    clf = make("cca", freqs=FREQS, fs=FS)
    return Decoder(clf, FS, window_s=1.0, step_s=0.2, votes=votes, pad_s=0.5)


def decision(end: int, raw: int, committed: int = ABSTAIN, changed: bool = False) -> Decision:
    return Decision(time=end / FS, scores=np.zeros(2), raw=raw, committed=committed,
                    changed=changed, end=end)


def push_all(swiper: Swiper, ends, raws) -> list[int]:
    return [swiper.push(decision(e, r)) for e, r in zip(ends, raws)]


# -- Swiper: unit behaviour on hand-made decisions ----------------------------------------

def test_targets_are_pass_left_like_right():
    assert (PASS, LIKE) == (0, 1)
    assert swipe.ABSTAIN == ABSTAIN == -1


def test_votes_default_to_the_decoder_and_cooldown_is_in_samples():
    dec = small_decoder(votes=4)
    s = Swiper(dec)
    assert s.votes == 4 and Swiper(dec, votes=2).votes == 2
    assert s.cooldown == 125                       # round(0.5 s * 250 Hz)
    assert s.switch == 0 and s.fresh_from == 125   # the card on screen counts from n_seen
    assert (s.pending, s.streak, s.stale, s.fired) == (ABSTAIN, 0, 0, 0)


def test_fires_after_votes_fresh_agreeing_windows():
    s = Swiper(small_decoder(votes=3))
    # window starts end - 250: 150, 200, 250 are all >= the 125 fresh limit
    assert push_all(s, [400, 450, 500], [LIKE] * 3) == [ABSTAIN, ABSTAIN, LIKE]
    assert s.fired == 1 and s.stale == 0


def test_stale_windows_never_vote():
    s = Swiper(small_decoder(votes=3))
    # windows starting before 125 (ends < 375) carry EEG from before the card settled
    out = push_all(s, [300, 350, 400, 450], [PASS] * 4)
    assert out == [ABSTAIN] * 4
    assert s.stale == 2 and (s.pending, s.streak) == (PASS, 2)
    assert s.push(decision(500, PASS)) == PASS


def test_window_starting_exactly_at_the_limit_is_fresh():
    s = Swiper(small_decoder(votes=1))
    assert s.is_stale(decision(374, LIKE)) and not s.is_stale(decision(375, LIKE))
    assert s.push(decision(374, LIKE)) == ABSTAIN
    assert s.push(decision(375, LIKE)) == LIKE


def test_after_a_fire_windows_are_stale_until_a_fully_fresh_one():
    dec = small_decoder(votes=3)
    s = Swiper(dec)
    assert push_all(s, [400, 450, 500], [LIKE] * 3)[-1] == LIKE
    assert s.switch == 500 and s.fresh_from == 625
    # every window ending before 625 + 250 started before the swipe settled
    stale_ends = list(range(550, 875, 50))
    assert push_all(s, stale_ends, [LIKE] * len(stale_ends)) == [ABSTAIN] * len(stale_ends)
    assert s.stale == len(stale_ends) and (s.pending, s.streak) == (ABSTAIN, 0)
    assert push_all(s, [900, 950, 1000], [LIKE] * 3) == [ABSTAIN, ABSTAIN, LIKE]
    assert s.fired == 2


def test_committed_field_of_the_decision_is_ignored():
    s = Swiper(small_decoder(votes=2))
    # the decoder's own voter may say "committed, changed" from stale windows: not a swipe
    for end in (400, 450, 500):
        assert s.push(decision(end, ABSTAIN, committed=LIKE, changed=True)) == ABSTAIN
    assert s.fired == 0


def test_abstains_and_switches_break_the_streak():
    s = Swiper(small_decoder(votes=3))
    seen = []
    for end, raw in zip(range(400, 800, 50), [PASS, PASS, ABSTAIN, PASS, PASS, LIKE, LIKE, LIKE]):
        fired = s.push(decision(end, raw))
        seen.append((fired, s.pending, s.streak))
    assert seen == [(ABSTAIN, PASS, 1), (ABSTAIN, PASS, 2), (ABSTAIN, ABSTAIN, 0),
                    (ABSTAIN, PASS, 1), (ABSTAIN, PASS, 2), (ABSTAIN, LIKE, 1),
                    (ABSTAIN, LIKE, 2), (LIKE, ABSTAIN, 0)]


def test_runs_of_abstains_never_fire():
    s = Swiper(small_decoder(votes=2))
    assert push_all(s, range(400, 1000, 50), [ABSTAIN] * 12) == [ABSTAIN] * 12
    assert s.fired == 0 and s.stale == 0


def test_card_changed_resets_the_votes():
    dec = small_decoder(votes=3)
    s = Swiper(dec)
    push_all(s, [400, 450], [PASS, PASS])
    s.card_changed(at=450)
    assert (s.switch, s.fresh_from, s.pending, s.streak) == (450, 575, ABSTAIN, 0)
    # the next fresh window ends at 575 + 250
    assert push_all(s, [500, 800, 850, 900, 950], [PASS] * 5) == [ABSTAIN] * 4 + [PASS]
    assert s.stale == 2


def test_card_changed_defaults_to_samples_seen():
    dec = small_decoder()
    dec.feed(np.zeros((8, 100)))             # fewer than needed: no decisions yet
    s = Swiper(dec)
    assert s.switch == 100
    dec.feed(np.zeros((8, 40)))
    s.card_changed()
    assert s.switch == 140


def test_zero_cooldown_needs_only_a_window_after_the_change():
    s = Swiper(small_decoder(votes=1), cooldown_s=0.0)
    s.card_changed(at=1000)
    assert s.push(decision(1249, LIKE)) == ABSTAIN
    assert s.push(decision(1250, LIKE)) == LIKE


@pytest.mark.parametrize("cooldown", [-0.1, float("nan"), float("inf")])
def test_bad_cooldown_raises(cooldown):
    with pytest.raises(ValueError, match="cooldown_s"):
        Swiper(small_decoder(), cooldown_s=cooldown)


@pytest.mark.parametrize("votes", [0, -1, 1.5])
def test_bad_votes_raise(votes):
    with pytest.raises(ValueError, match="votes"):
        Swiper(small_decoder(), votes=votes)


# -- Swiper on a FakeBoard stream ---------------------------------------------------------

def tuned_decoder() -> Decoder:
    clf = make(TUNED.classifier, freqs=FREQS, fs=FS, margin=TUNED.margin,
               threshold=TUNED.threshold)
    return Decoder(clf, FS, window_s=TUNED.window_s, step_s=TUNED.step_s, votes=TUNED.votes,
                   pad_s=2.0)


def calibrated(board: FakeBoard) -> Decoder:
    """Tuned decoder with its rest baseline fit on 30 s of still screen, like the demo."""
    dec = tuned_decoder()
    board.set_target(ABSTAIN)
    calibrate(dec, board, 30.0)
    board.set_target(None)
    board.read()
    return dec


def stream(board: FakeBoard, dec: Decoder, swiper: Swiper, seconds: float):
    """Feed ``seconds`` in decoder steps; yields (decision, fired, true target)."""
    for _ in range(int(round(seconds / dec.step_s))):
        board.advance(dec.step_s)
        truth = board.active_target()
        for d in dec.feed(*board.read()):
            yield d, swiper.push(d), truth


def min_interval(dec: Decoder, swiper: Swiper) -> int:
    """Fewest samples between swipes: settle, one fresh window, then votes - 1 more steps."""
    return swiper.cooldown + dec.n_window + (swiper.votes - 1) * dec.n_step


def test_holding_gaze_keeps_swiping_at_the_minimum_interval():
    board = FakeBoard(FREQS, seed=3)
    dec = calibrated(board)
    board.set_target(LIKE)
    swiper = Swiper(dec)
    swiper.card_changed()
    fires = [d.end for d, fired, _ in stream(board, dec, swiper, 40.0) if fired == LIKE]
    gaps = np.diff([0, *fires])
    assert swiper.fired == len(fires) >= 8
    assert gaps.min() >= min_interval(dec, swiper)
    # a strong steady response fires as soon as the rule allows, give or take a step
    assert np.median(gaps[1:]) <= min_interval(dec, swiper) + dec.n_step


def test_switching_gaze_swipes_the_true_target_from_fresh_windows_only():
    board = FakeBoard(FREQS, switch_s=10.0, seed=1)
    dec = calibrated(board)
    swiper = Swiper(dec)
    swiper.card_changed()
    switch = swiper.switch
    hits, fires = 0, 0
    voted_from = dec.n_window + (swiper.votes - 1) * dec.n_step
    for d, fired, truth in stream(board, dec, swiper, 60.0):
        if fired == ABSTAIN:
            continue
        fires += 1
        hits += fired == truth
        # the oldest of the agreeing windows started after the last swipe had settled
        assert d.end - voted_from >= switch + swiper.cooldown
        switch = d.end
        assert swiper.switch == d.end
    assert fires == swiper.fired >= 12
    assert hits / fires >= 0.75
    assert swiper.stale > 0


# -- Feed ---------------------------------------------------------------------------------

def shown(feed: Feed, n: int) -> list[int]:
    out = []
    for i in range(n):
        out.append(feed.current)
        feed.swipe(liked=bool(i % 2))
    return out


def test_feed_is_endless_and_shows_every_card_each_round():
    feed = Feed(5, seed=2)
    order = shown(feed, 50)
    for k in range(0, 50, 5):
        assert sorted(order[k:k + 5]) == list(range(5))
    assert all(a != b for a, b in zip(order, order[1:]))


@pytest.mark.parametrize("seed", range(20))
def test_feed_never_repeats_a_card_across_reshuffles(seed):
    order = shown(Feed(2, seed=seed), 40)
    assert all(a != b for a, b in zip(order, order[1:]))


def test_feed_order_is_deterministic_per_seed_and_independent_of_choices():
    a, b, c = Feed(7, seed=4), Feed(7, seed=4), Feed(7, seed=5)
    order_a = [a.current] + [a.swipe(True).card for _ in range(30)]
    order_b = [b.current] + [b.swipe(False).card for _ in range(30)]
    order_c = [c.current] + [c.swipe(True).card for _ in range(30)]
    assert order_a == order_b
    assert order_a != order_c


def test_single_card_feed_repeats_it():
    feed = Feed(1)
    assert [feed.swipe(True).card for _ in range(3)] == [0, 0, 0] and feed.current == 0


def test_swipe_returns_the_card_just_swiped_and_counts():
    feed = Feed(6, match_rate=0.5, seed=0)
    outcomes = []
    for i in range(30):
        before = feed.current
        o = feed.swipe(liked=i % 3 != 0)
        assert isinstance(o, Outcome) and o.card == before and o.liked == (i % 3 != 0)
        outcomes.append(o)
    assert feed.swipes == 30
    assert feed.likes == sum(o.liked for o in outcomes) == 20
    assert feed.passes == 10
    assert feed.matches == sum(o.match for o in outcomes)
    assert 0 < feed.matches < feed.likes
    assert not any(o.match and not o.liked for o in outcomes)


def test_match_rate_edges():
    never, always = Feed(4, match_rate=0.0), Feed(4, match_rate=1.0)
    assert not any(never.swipe(True).match for _ in range(50))
    assert all(always.swipe(True).match for _ in range(50))
    assert not any(always.swipe(False).match for _ in range(50))
    assert (always.likes, always.passes, always.matches) == (50, 50, 50)


def test_matches_are_deterministic_per_seed():
    a, b = Feed(4, seed=9), Feed(4, seed=9)
    assert [a.swipe(True).match for _ in range(40)] == [b.swipe(True).match for _ in range(40)]


def test_match_of_a_swipe_does_not_depend_on_earlier_choices():
    # one draw per swipe, liked or not: an earlier Pass must not shift later matches
    likes, mixed = Feed(20, match_rate=0.5, seed=1), Feed(20, match_rate=0.5, seed=1)
    likes.swipe(True)
    mixed.swipe(False)
    for i in range(40):
        a = likes.swipe(True).match
        b = mixed.swipe(True).match if i % 3 else mixed.swipe(False).match
        if i % 3:
            assert a == b
        else:
            assert not b


@pytest.mark.parametrize("kw", [{"n_cards": 0},{"n_cards": -3}, {"n_cards": 2.5},
                                {"n_cards": 3, "match_rate": -0.1},
                                {"n_cards": 3, "match_rate": 1.01},
                                {"n_cards": 3, "match_rate": float("nan")}])
def test_bad_feed_arguments_raise(kw):
    with pytest.raises(ValueError):
        Feed(**kw)
