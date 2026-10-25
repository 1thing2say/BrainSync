"""BrainSwipe on the c-VEP standard (docs/CVEP-STANDARD.md): Pass / Super like / Like on
targets 0, 1, 2, headless (the standard's synthetic board, a stand-in stimulus)."""
from __future__ import annotations

import json
import math
import sys

import numpy as np
import pytest

from brainsync import demo, launch, swipe
from brainsync.metrics import ABSTAIN
from brainsync.online import Decision
from brainsync.profiles import Profile, load_deck
from brainsync.stimulus import load_config
from brainsync.stimulus.config import card_overlap
from brainsync.stimulus.display import text_boxes, text_overlaps
from brainsync.swipe import LIKE, PASS, SUPER_LIKE, CodeSwiper, Feed, Outcome
from brainsync.vep import live

HEADLESS = ["--fake", "--fast", "--no-stimulus", "--no-voice"]

FAKE_STIMULUS = """
import sys
print("measured refresh: 60.00 Hz", flush=True)
with open(sys.argv[1], "w") as out:
    for line in sys.stdin:
        out.write(line)
        out.flush()
"""


@pytest.fixture
def fake_stimulus(monkeypatch, tmp_path):
    calls: list[dict] = []
    feedback = tmp_path / "feedback.txt"

    def fake_launch(args, stdin=None, **kw):
        cmd = [sys.executable, "-c", FAKE_STIMULUS, str(feedback)]
        stim = launch.launch_stimulus(args, stdin=stdin, command=cmd, log_dir=tmp_path)
        calls.append({"args": list(args), "stim": stim})
        return stim

    monkeypatch.setattr(demo, "launch_stimulus", fake_launch)
    return calls, feedback


def spoken(err: str) -> list[str]:
    return [ln[8:] for ln in err.splitlines() if ln.startswith("[speak] ")]


def swipes_from(out: str) -> list[tuple[float, str]]:
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("swipes:"))
    out_ = []
    for item in line.strip()[len("swipes:"):].strip().split("  "):   # names have spaces
        t, _, name = item.strip().partition("->")
        out_.append((float(t.rstrip("s")), name))
    return out_


# -- the standard -------------------------------------------------------------------------

def test_targets_follow_the_standard():
    assert (PASS, SUPER_LIKE, LIKE) == (0, 1, 2)
    assert swipe.ACTIONS == ("Pass", "Super like", "Like")
    cfg = load_config(swipe.CODE_CONFIG)
    assert cfg.labels == swipe.ACTIONS and demo.is_standard_code(cfg)
    assert cfg.fill == (1.0, 1.0, 1.0) and cfg.display.background == 0.0
    assert cfg.refresh == 60.0


def test_swipe_cvep_leaves_room_for_the_card_and_the_text():
    cfg = load_config("swipe_cvep")
    assert card_overlap(cfg) is None                 # a 2:3 card at the centre
    for aspect in (1.5, 1.6, 16 / 9):
        assert text_overlaps(cfg, aspect) == [], aspect
    slots, centre = text_boxes(cfg)
    assert [b.side for b in slots] == ["above", "right", "below"]
    # the centre line (profile + status) goes under the card, never over it
    half = cfg.card_spec.height / 2
    assert centre.rect[3] < -half


def test_super_like_always_matches_and_counts_as_a_like():
    feed = Feed(5, match_rate=0.0, seed=1)
    out = feed.swipe(liked=False, super_like=True)
    assert out == Outcome(out.card, liked=True, match=True, super_like=True)
    assert (feed.likes, feed.super_likes, feed.matches, feed.passes) == (1, 1, 1, 0)
    assert feed.swipe(liked=True).match is False    # match rate 0: a plain like never does


def test_super_like_draws_like_any_swipe():
    # one match draw per swipe whatever it was, so later matches do not depend on it
    a, b = Feed(6, match_rate=0.5, seed=3), Feed(6, match_rate=0.5, seed=3)
    a.swipe(liked=True, super_like=True)
    b.swipe(liked=False)
    assert [a.swipe(True).match for _ in range(20)] == [b.swipe(True).match for _ in range(20)]


def test_voice_and_card_text():
    p = Profile("Ana", 30, (("My simple pleasures", "Toast " * 30),), "#405060", "Baker")
    assert swipe.voice_line(p, Outcome(0, True, True, True)) == "Super liked Ana. It's a match!"
    assert swipe.voice_line(p, Outcome(0, True, True)) == "Ana liked you back. It's a match!"
    head, answer = swipe.card_text(p).split("\n")
    assert head == "Ana, 30 · Baker"
    assert answer.startswith("“Toast") and answer.endswith("…”")
    assert len(answer) <= swipe.ANSWER_CHARS + 2
    q = Profile("Bo", 41, (("Q", "Short."),), "#405060")
    assert swipe.card_text(q) == "Bo, 41\n“Short.”"


# -- CodeSwiper ---------------------------------------------------------------------------

def trained(seed: int = 0):
    """A standard fake board looking at Pass and a decoder trained on it (virtual time)."""
    board = live.standard_fake_board(0, seed=seed)
    dec = live.CodeVepDecoder(board.fs, train_cycles=10)
    board.start_flicker()
    while dec.training:
        board.advance(0.1)
        X, t = board.read()
        dec.feed(X, t, board.read_events())
    return board, dec


def run(board, swiper: CodeSwiper, seconds: float, gaze=None):
    """(board time, decision, fired) for every decision handed out in ``seconds``."""
    out = []
    for step in range(int(round(seconds / 0.1))):
        if gaze is not None:
            board.set_target(gaze(step * 0.1))
        board.advance(0.1)
        X, t = board.read()
        for d in swiper.feed(X, t, board.read_events()):
            out.append((float(t[-1]), d, swiper.fires(d)))
    return out


def test_code_swiper_swipes_once_per_commit_and_resets_the_decoder():
    board, dec = trained()
    sw = CodeSwiper(dec, cooldown_s=0.5)
    board.set_target(LIKE)
    sw.card_changed()
    seen = run(board, sw, 30.0)
    fires = [(t, k) for t, d, k in seen if k != ABSTAIN]
    assert sw.fired == len(fires) >= 5 and {k for _, k in fires} == {LIKE}
    # each swipe takes two fresh cycles plus the pad after the settle delay
    gaps = np.diff([t for t, _ in fires])
    assert gaps.min() >= 0.5 + 2 * 1.05 + 0.5 - 0.05
    assert sw.stale > 0 and dec.committed in (ABSTAIN, LIKE)


def test_a_fire_drops_the_rest_of_its_read():
    board, dec = trained()
    sw = CodeSwiper(dec, cooldown_s=0.0)
    board.set_target(SUPER_LIKE)
    board.advance(20.0)                         # a backlog of many cycles in one read
    X, t = board.read()
    out = sw.feed(X, t, board.read_events())
    assert [sw.fires(d) for d in out][-1] == SUPER_LIKE
    assert sum(sw.fires(d) != ABSTAIN for d in out) == 1 and sw.fired == 1
    assert sw.fresh_from == pytest.approx(float(t[-1]))
    assert dec.committed == ABSTAIN             # reset: the next card starts from nothing


def test_cycles_before_the_card_settled_are_stale():
    board, dec = trained()
    sw = CodeSwiper(dec, cooldown_s=2.0)
    sw.card_changed(at=board.now)
    evs = [(board.now + 0.5, "cycle", 1), (board.now + 2.5, "cycle", 2)]
    sw.feed(np.zeros((8, 0)), np.zeros(0), evs)
    assert sw.stale == 1


def test_training_takes_every_cycle_and_never_fires():
    board = live.standard_fake_board(0)
    dec = live.CodeVepDecoder(board.fs, train_cycles=5)
    sw = CodeSwiper(dec)
    sw.fresh_from = math.inf                    # would drop everything once trained
    board.start_flicker()
    for _ in range(80):
        board.advance(0.1)
        X, t = board.read()
        assert sw.feed(X, t, board.read_events()) == []
        if not dec.training:
            break
    assert not dec.training and sw.stale == 0 and sw.fired == 0


def test_bad_cooldown_raises():
    _, dec = trained()
    for bad in (-0.1, math.nan, math.inf):
        with pytest.raises(ValueError):
            CodeSwiper(dec, cooldown_s=bad)


def test_fires_only_on_a_new_commit():
    def d(committed, changed):
        return Decision(time=0.0, scores=np.zeros(3), raw=committed, committed=committed,
                        changed=changed)
    assert CodeSwiper.fires(d(LIKE, True)) == LIKE
    assert CodeSwiper.fires(d(LIKE, False)) == ABSTAIN
    assert CodeSwiper.fires(d(ABSTAIN, True)) == ABSTAIN


# -- the CLI --------------------------------------------------------------------------------

def test_defaults_are_the_cvep_standard():
    args = swipe.build_parser().parse_args([])
    cfg = demo.resolve(args)
    assert args.paradigm == "code" and cfg.name == "swipe_cvep"
    assert args.votes == live.VOTES and args.train_cycles == live.TRAIN_CYCLES


def test_headless_run_trains_on_pass_then_swipes(capsys):
    assert swipe.main([*HEADLESS, "--seconds", "60"]) == 0
    cap = capsys.readouterr()
    first = cap.out.splitlines()[0]
    assert first.startswith("swipe: ") and "super)" in first and "stale cycles" in first
    assert "template ready:" in cap.out
    said = spoken(cap.err)
    assert said[:2] == ["Look at the Pass square and keep looking at it.",
                        swipe.CODE_PROMPTS[1]]
    assert any(s.startswith("Super liked ") and s.endswith("It's a match!") for s in said)
    assert "Pass" in said and any(s.startswith("Liked ") or "liked you back" in s
                                  for s in said)
    hits = cap.out.split("fake board: ")[1].split(" swipes")[0]
    got, of = (int(x) for x in hits.split(" of "))
    assert of >= 10 and got / of >= 0.7
    times = [t for t, _ in swipes_from(cap.out)]
    assert min(np.diff(times)) >= 3.0           # a fresh card needs fresh cycles


def test_feedback_stream_follows_the_standard(fake_stimulus, capsys):
    calls, feedback = fake_stimulus
    assert swipe.main(["--fake", "--fast", "--no-voice", "--seconds", "30"]) == 0
    out = capsys.readouterr().out
    args = calls[0]["args"]
    assert "swipe_cvep" in args[1] and "--cards" in args and "--labels" in args
    lines = feedback.read_text().splitlines()
    assert json.loads(lines[0]) == {"flicker": True} and lines[1] == "0"
    msgs = [json.loads(ln) for ln in lines if ln.startswith("{")]
    texts = [m["text"] for m in msgs if "text" in m]
    assert texts[0] == "training: keep looking at Pass   cycle 0/30"
    ready = next(i for i, t in enumerate(texts) if t.startswith("template ready"))
    after = texts[ready + 1:]
    assert after and any(t == swipe.CODE_PROMPTS[1] for t in after)   # the card shows the profile
    assert any(t.splitlines()[-1].startswith("lead ") for t in after)
    labels = [m["labels"] for m in msgs if "labels" in m]
    assert labels and all(row[0].startswith("Pass ") and row[1].startswith("Super like ")
                          and row[2].startswith("Like ") for row in labels)
    # integer lines: the training cue, its clearing, then each swipe's box and its clearing
    ints = [int(ln) for ln in lines if ln.lstrip("-").isdigit()]
    assert ints[:2] == [0, ABSTAIN]
    shown = [k for k in ints[2:] if k != ABSTAIN]
    order = {"Pass": PASS, "Super like": SUPER_LIKE, "Like": LIKE}
    swiped = [order[n] for _, n in swipes_from(out)]
    assert shown == swiped
    # every swipe flashes its square and shows the next card, as in the f-VEP feed
    flashes = [int(ln.split()[1]) for ln in lines if ln.startswith("flash ")]
    cards = [ln for ln in lines if ln.startswith("card ")]
    assert flashes == swiped and len(cards) == len(swiped)


def test_freq_flag_keeps_the_two_square_feed(capsys):
    assert swipe.main([*HEADLESS, "--paradigm", "freq", "--seconds", "20"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert "stale windows" in first and "super" not in first


@pytest.mark.parametrize("argv, message", [
    (["--fake", "--fast", "--no-voice", "-c", "cvep_2", "--no-stimulus"], "needs 3 targets"),
    (["--no-stimulus", "--no-voice"], "--no-stimulus needs --fake"),
    ([*HEADLESS, "--paradigm", "code", "-c", "swipe"], "not --paradigm code"),
])
def test_bad_configs_exit_2(argv, message, capsys):
    assert swipe.main(argv) == 2
    assert message in capsys.readouterr().err
