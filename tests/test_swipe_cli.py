"""Tests for the BrainSwipe CLI, headless: FakeBoard and a stand-in stimulus process (the
pattern of tests/test_demo.py)."""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from brainsync import demo, launch, swipe
from brainsync.board import FakeBoard
from brainsync.classify import ABSTAIN
from brainsync.profiles import CARD_SIZE, Profile, load_deck
from brainsync.swipe import LIKE, PASS, Feed, Outcome

HEADLESS = ["--fake", "--fast", "--no-stimulus", "--no-voice"]
LIVE = ["--fake", "--fast", "--no-voice"]

# Stand-in for brainsync.stimulus --feedback stdin: reports its window, then copies each
# feedback line to a file until EOF (or exits at once with "exit", or crashes with code 1
# right after the second line with "crash").
FAKE_STIMULUS = """
import sys
print("measured refresh: 60.00 Hz", flush=True)
if sys.argv[2] == "exit":
    sys.exit(0)
with open(sys.argv[1], "w") as out:
    for i, line in enumerate(sys.stdin):
        out.write(line)
        out.flush()
        if sys.argv[2] == "crash" and i == 1:
            sys.exit(1)
"""


def png_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


@pytest.fixture
def fake_stimulus(monkeypatch, tmp_path):
    """Route demo.launch_stimulus to FAKE_STIMULUS; returns (calls, feedback file, mode).

    Each call records its arguments and the PNGs in its --cards folder at launch time
    (the folder is temporary and gone once the run ends)."""
    calls: list[dict] = []
    feedback = tmp_path / "feedback.txt"
    mode = {"value": "copy"}

    def fake_launch(args, stdin=None, **kw):
        args = list(args)
        cards = Path(args[args.index("--cards") + 1]) if "--cards" in args else None
        pngs = sorted(cards.glob("*.png")) if cards is not None else []
        cmd = [sys.executable, "-c", FAKE_STIMULUS, str(feedback), mode["value"]]
        stim = launch.launch_stimulus(args, stdin=stdin, command=cmd, log_dir=tmp_path)
        calls.append({"args": args, "stim": stim, "cards": cards,
                      "pngs": [(p.name, png_size(p)) for p in pngs]})
        return stim

    monkeypatch.setattr(demo, "launch_stimulus", fake_launch)
    return calls, feedback, mode


def summary_value(out: str, key: str) -> int:
    """The integer before ``key`` in the summary's first line, e.g. "swipes"."""
    words = out.splitlines()[0].replace(",", " ").replace(":", " ").replace(";", " ").split()
    return int(words[words.index(key) - 1])


def spoken(err: str) -> list[str]:
    return [ln[8:] for ln in err.splitlines() if ln.startswith("[speak] ")]


# -- headless runs ------------------------------------------------------------------------

def test_headless_run_swipes_and_summarises(capsys):
    assert swipe.main([*HEADLESS, "--seconds", "60"]) == 0
    cap = capsys.readouterr()
    first = cap.out.splitlines()[0]
    assert first.startswith("swipe: ") and "ended: time up" in first
    assert float(first.split()[1]) == pytest.approx(60.0, abs=0.3)   # to the last window
    n = summary_value(cap.out, "swipes")
    # held gaze swipes at most every cooldown + window + 3 steps (~3 s), and FakeBoard
    # only changes target every 10 s, so a minute gives well over a dozen swipes
    assert 12 <= n <= 22
    assert summary_value(cap.out, "likes") + summary_value(cap.out, "passes") == n
    assert "per minute" in first and "stale windows ignored" in first
    hit = int(cap.out.split("true target")[0].split("(")[-1].split("%")[0])
    assert hit >= 70
    said = spoken(cap.err)
    assert said[:2] == list(swipe.PROMPTS)
    assert len(said[2:]) == n                       # one voice line per swipe
    status = [ln for ln in cap.err.splitlines() if " lead " in ln and " window " in ln]
    assert len(status) == pytest.approx((60 - 3.5) / 0.25 + 1, abs=2)
    assert status[0].strip().startswith("3.50 s")
    assert sum(" swipe " in ln for ln in status) == n
    assert any(" stale" in ln for ln in status) and any(" streak " in ln for ln in status)


def test_headless_run_needs_no_rendering(monkeypatch, capsys):
    def no_render(*a, **kw):
        raise AssertionError("--no-stimulus must not render cards")

    monkeypatch.setattr(swipe, "render_deck", no_render)
    assert swipe.main([*HEADLESS, "--seconds", "10"]) == 0
    assert "ended: time up" in capsys.readouterr().out


def test_voice_lines_name_the_swiped_profile(capsys):
    assert swipe.main([*HEADLESS, "--seconds", "60", "--match-rate", "1"]) == 0
    cap = capsys.readouterr()
    names = {p.name for p in load_deck()}
    lines = spoken(cap.err)[2:]
    assert "Pass" in lines
    likes = [ln for ln in lines if ln != "Pass"]
    assert likes and all(ln.endswith(" liked you back. It's a match!") for ln in likes)
    assert {ln.split(" liked you")[0] for ln in likes} <= names
    assert summary_value(cap.out, "matches") == summary_value(cap.out, "likes") == len(likes)


def test_no_matches_at_rate_zero(capsys):
    assert swipe.main([*HEADLESS, "--seconds", "40", "--match-rate", "0"]) == 0
    cap = capsys.readouterr()
    assert summary_value(cap.out, "matches") == 0
    likes = [ln for ln in spoken(cap.err)[2:] if ln != "Pass"]
    assert likes and all(ln.startswith("Liked ") for ln in likes)


def test_longer_cooldown_swipes_less(capsys):
    assert swipe.main([*HEADLESS, "--seconds", "60"]) == 0
    fast = summary_value(capsys.readouterr().out, "swipes")
    assert swipe.main([*HEADLESS, "--seconds", "60", "--cooldown", "2"]) == 0
    assert summary_value(capsys.readouterr().out, "swipes") < fast


# -- with a stand-in stimulus ------------------------------------------------------------------

def _parse_feedback(text: str) -> list[tuple[str, int]]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        out.append(("int", int(parts[0])) if len(parts) == 1 else (parts[0], int(parts[1])))
    return out


@pytest.fixture
def rendered(monkeypatch) -> list[str]:
    """Names of the profiles in the order swipe.main renders them (image 0 first)."""
    names: list[str] = []
    real = swipe.render_deck

    def spy(profiles, out_dir):
        names.extend(p.name for p in profiles)
        return real(profiles, out_dir)

    monkeypatch.setattr(swipe, "render_deck", spy)
    return names


def check_stream(text: str, rendered: list[str], err: str) -> list[tuple[int, str]]:
    """Check the feedback invariants; returns (target, name on screen) for every swipe."""
    sent = _parse_feedback(text)
    assert sent[0] == ("int", -1)                       # the first line starts the flicker
    shown = 0                                           # image 0 is up from the first frame
    cue = -1
    fires = []
    i = 1
    while i < len(sent):
        kind, k = sent[i]
        if kind == "int":
            assert k != cue                             # cue lines only on a change
            cue = k
            i += 1
            continue
        assert kind == "flash" and k in (PASS, LIKE)    # cards only change on a swipe
        fires.append((k, rendered[shown]))
        i += 1
        if sent[i] == ("int", -1):                      # the cue box clears with the swipe
            cue = -1
            i += 1
        kind, nxt = sent[i]
        assert kind == "card" and 0 <= nxt < len(rendered) and nxt != shown
        shown = nxt
        i += 1
    # every voice line is about the card that was on screen when it fired (match rate 0)
    said = spoken(err)[2:]
    assert said == ["Pass" if k == PASS else f"Liked {name}" for k, name in fires]
    return fires


def test_feedback_stream_drives_cue_flash_and_cards(fake_stimulus, rendered, capsys):
    calls, feedback, _ = fake_stimulus
    assert swipe.main([*LIVE, "--seconds", "60", "--match-rate", "0"]) == 0
    cap = capsys.readouterr()
    deck = load_deck()
    n = len(deck)

    # the stimulus gets the swipe config, the usual live flags and the rendered cards
    assert len(calls) == 1
    args = calls[0]["args"]
    assert args[args.index("-c") + 1] == "swipe"
    for flag in ("--free", "--feedback", "--hold-still"):
        assert flag in args
    assert calls[0]["pngs"] == [(f"card_{i:03d}.png", CARD_SIZE) for i in range(n)]
    assert sorted(rendered) == sorted(p.name for p in deck)
    assert not calls[0]["cards"].exists()               # the temporary folder is cleaned up
    assert calls[0]["stim"].proc.poll() == 0

    # image 0 (shown from the first frame, during calibration) is the feed's first card
    assert rendered[0] == deck[Feed(n, seed=0).current].name

    fires = check_stream(feedback.read_text(), rendered, cap.err)
    assert summary_value(cap.out, "swipes") == len(fires) >= 12
    assert any(k == LIKE for k, _ in fires) and any(k == PASS for k, _ in fires)


def test_small_deck_names_the_card_on_screen(fake_stimulus, rendered, capsys, tmp_path):
    # 3 cards over ~18 swipes: every image, the swapped first one included, gets swiped
    _, feedback, _ = fake_stimulus
    deck = tmp_path / "deck.toml"
    deck.write_text("".join(f'[[profile]]\nname = "P{i}"\nage = 30\ncolor = "#405060"\n'
                            'prompts = [["My simple pleasures", "Toast."]]\n' for i in range(3)))
    seed = next(s for s in range(50) if Feed(3, seed=s).current != 0)
    assert swipe.main([*LIVE, "--deck", str(deck), "--seconds", "60", "--match-rate", "0",
                       "--seed", str(seed)]) == 0
    assert rendered[0] == f"P{Feed(3, seed=seed).current}"
    fires = check_stream(feedback.read_text(), rendered, capsys.readouterr().err)
    assert {name for k, name in fires if k == LIKE} == {"P0", "P1", "P2"}


def test_cue_box_follows_the_pending_streak(fake_stimulus):
    _, feedback, _ = fake_stimulus
    assert swipe.main([*LIVE, "--seconds", "30"]) == 0
    sent = _parse_feedback(feedback.read_text())
    flashes = [j for j, (kind, _) in enumerate(sent) if kind == "flash"]
    assert flashes
    for j in flashes:
        # held gaze: the box sits on the target before its swipe fires
        before = [k for kind, k in sent[:j] if kind == "int"]
        assert before[-1] == sent[j][1]


def test_run_ends_when_the_stimulus_exits(fake_stimulus, capsys):
    calls, _, mode = fake_stimulus
    mode["value"] = "exit"
    assert swipe.main([*LIVE, "--seconds", "3000"]) == 0
    out = capsys.readouterr().out
    assert "ended: stimulus" in out and "before the flicker started" in out
    assert summary_value(out, "swipes") == 0
    assert calls[0]["stim"].proc.poll() is not None


def test_stimulus_crash_mid_run_exits_1(fake_stimulus, capsys):
    _, _, mode = fake_stimulus
    mode["value"] = "crash"
    assert swipe.main([*LIVE, "--seconds", "3000"]) == 1
    cap = capsys.readouterr()
    assert "ended: stimulus" in cap.out and "error: the stimulus exited with code 1" in cap.err


class InterruptingBoard(FakeBoard):
    """FakeBoard whose read raises KeyboardInterrupt (Ctrl-C) after ``reads`` reads."""

    reads = 0

    def read(self):
        type(self).reads -= 1
        if type(self).reads < 0:
            raise KeyboardInterrupt
        return super().read()


def test_ctrl_c_mid_run_prints_the_summary(fake_stimulus, monkeypatch, capsys):
    calls, _, _ = fake_stimulus
    InterruptingBoard.reads = 900                # past calibration (600 reads), into the run
    monkeypatch.setattr(demo, "FakeBoard", InterruptingBoard)
    assert swipe.main([*LIVE, "--seconds", "1000"]) == 0
    assert "ended: interrupted" in capsys.readouterr().out
    assert calls[0]["stim"].proc.poll() == 0


def test_ctrl_c_during_calibration_ends_before_decoding(fake_stimulus, monkeypatch, capsys):
    calls, feedback, _ = fake_stimulus
    InterruptingBoard.reads = 120
    monkeypatch.setattr(demo, "FakeBoard", InterruptingBoard)
    assert swipe.main([*LIVE, "--seconds", "1000"]) == 130
    cap = capsys.readouterr()
    assert "interrupted before decoding started" in cap.err and cap.out == ""
    assert feedback.read_text() == ""            # still screen never released


class SilentBoard(FakeBoard):
    """FakeBoard that stops delivering after calibration."""

    def read(self):
        X, t = super().read()
        assert self.elapsed < 300.0, "the run should have ended on the stall"
        if self.elapsed > 40.0:
            return X[:, :0], t[:0]
        return X, t


def test_board_stall_ends_the_run(monkeypatch, capsys):
    monkeypatch.setattr(demo, "FakeBoard", SilentBoard)
    assert swipe.main([*HEADLESS, "--seconds", "60"]) == 1
    cap = capsys.readouterr()
    assert "ended: board stalled" in cap.out and "sent nothing for 5 s" in cap.err


class BurstBoard(FakeBoard):
    """FakeBoard that, once decoding runs, holds its data back for 4.5 s of every 8 and
    then delivers the backlog in one read (a board that lags, short of the 5 s stall)."""

    def read(self):
        if self.elapsed > 35.0 and (self.elapsed - 35.0) % 8.0 < 4.5:
            return np.empty((8, 0)), np.empty(0)
        return super().read()


class RecordingSwiper(swipe.Swiper):
    """Swiper that records (window end, samples fed so far, fired) for every decision."""

    log: list[tuple[int, int, int]] = []

    def push(self, d):
        fired = super().push(d)
        RecordingSwiper.log.append((d.end, self.decoder.n_seen, fired))
        return fired


def test_a_backlog_burst_swipes_at_most_once(monkeypatch, capsys):
    # every decision in one board read is decoded before the new card line can go out,
    # so after a swipe the rest of that read is EEG from the old card and must not vote
    monkeypatch.setattr(demo, "FakeBoard", BurstBoard)
    monkeypatch.setattr(swipe, "Swiper", RecordingSwiper)
    RecordingSwiper.log = []
    assert swipe.main([*HEADLESS, "--seconds", "60", "--switch", "1000"]) == 0
    assert "ended: time up" in capsys.readouterr().out
    log = RecordingSwiper.log
    reads: dict[int, list[int]] = {}
    for _, seen, fired in log:
        reads.setdefault(seen, []).append(fired)
    bursts = [f for f in reads.values() if len(f) >= 16]   # 4 s or more in one read
    assert len(bursts) >= 4
    assert all(sum(k != ABSTAIN for k in fired) <= 1 for fired in reads.values())
    assert sum(k != ABSTAIN for fired in bursts for k in fired) >= 2   # bursts do swipe
    # every swipe's oldest voting window started after the previous card line went out
    dec_window, dec_step = 375, 62
    sent = None
    for end, seen, fired in log:
        if fired == ABSTAIN:
            continue
        if sent is not None:
            assert end - dec_window - 3 * dec_step >= sent + 125
        sent = seen


# -- helpers -----------------------------------------------------------------------------------

def test_card_order_puts_the_first_card_on_image_0():
    order = swipe.card_order(5, 3)
    assert order == [3, 1, 2, 0, 4]
    assert [order[i] for i in order] == list(range(5))     # its own inverse
    assert swipe.card_order(4, 0) == [0, 1, 2, 3]


def test_voice_lines():
    p = Profile("Ana", 30, (("My simple pleasures", "Toast."),), "#405060")
    assert swipe.voice_line(p, Outcome(2, liked=False, match=False)) == "Pass"
    assert swipe.voice_line(p, Outcome(2, liked=True, match=False)) == "Liked Ana"
    assert swipe.voice_line(p, Outcome(2, liked=True, match=True)) == (
        "Ana liked you back. It's a match!")


# -- arguments ----------------------------------------------------------------------------------

def test_defaults():
    args = swipe.build_parser().parse_args([])
    assert args.config == "swipe" and args.seconds == 300.0
    assert args.cooldown == 0.5 and args.match_rate == 0.3 and args.deck is None
    assert args.seed is None and args.calibrate >= 30.0
    help_text = swipe.build_parser().format_help()
    for flag in ("--cooldown", "--deck", "--match-rate", "--seed", "--no-stimulus"):
        assert flag in help_text


@pytest.mark.parametrize("argv", [[*HEADLESS, "--cooldown", "-0.1"],
                                  [*HEADLESS, "--cooldown", "nan"],
                                  [*HEADLESS, "--match-rate", "1.5"],
                                  [*HEADLESS, "--match-rate", "-0.1"],
                                  [*HEADLESS, "--seconds", "0"],
                                  [*HEADLESS, "--seed", "-1"],
                                  ["--no-stimulus", "--seed", "-1"],
                                  ["--fast", "--no-stimulus"]])
def test_bad_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        swipe.main(argv)
    assert exc.value.code == 2


def test_bad_deck_or_config_returns_2(capsys, tmp_path: Path):
    bad = tmp_path / "deck.toml"
    bad.write_text('[[profile]]\nname = "Ana"\nage = 12\ncolor = "#123456"\n'
                   'prompts = [["a", "b"]]\n')
    assert swipe.main([*HEADLESS, "--deck", str(bad)]) == 2
    assert swipe.main([*HEADLESS, "--deck", str(tmp_path / "missing.toml")]) == 2
    assert swipe.main([*HEADLESS, "-c", "one_target_15"]) == 2
    err = capsys.readouterr().err
    assert "deck.toml: profile 0" in err and "missing.toml" in err
    assert "two targets" in err
    # a folder or an unreadable file is an error message, not a traceback
    assert swipe.main([*HEADLESS, "--deck", str(tmp_path)]) == 2
    assert capsys.readouterr().err.startswith("error: ")
    locked = tmp_path / "locked.toml"
    locked.write_text('[[profile]]\nname = "Ana"\nage = 30\ncolor = "#405060"\n'
                      'prompts = [["My simple pleasures", "Toast."]]\n')
    locked.chmod(0)
    try:
        if not os.access(locked, os.R_OK):   # root reads anything
            assert swipe.main([*HEADLESS, "--deck", str(locked)]) == 2
            assert capsys.readouterr().err.startswith("error: ")
    finally:
        locked.chmod(0o600)


def test_feed_seed_is_fresh_on_the_real_board_and_0_with_fake(monkeypatch):
    monkeypatch.setattr(swipe.secrets, "randbelow", lambda n: 12345)
    assert swipe.feed_seed(None, fake=False) == 12345
    assert swipe.feed_seed(None, fake=True) == 0      # fake runs stay reproducible
    assert swipe.feed_seed(7, fake=False) == 7 and swipe.feed_seed(7, fake=True) == 7


def test_deck_line_names_the_seed(capsys):
    assert swipe.main([*HEADLESS, "--seconds", "5", "--seed", "4"]) == 0
    assert "seed 4" in capsys.readouterr().err
    assert swipe.main([*HEADLESS, "--seconds", "5"]) == 0
    assert "seed 0" in capsys.readouterr().err


def test_a_small_custom_deck_cycles(capsys, tmp_path: Path):
    deck = tmp_path / "deck.toml"
    deck.write_text("".join(f'[[profile]]\nname = "P{i}"\nage = 30\ncolor = "#405060"\n'
                            'prompts = [["My simple pleasures", "Toast."]]\n' for i in range(3)))
    assert swipe.main([*HEADLESS, "--deck", str(deck), "--seconds", "40",
                       "--match-rate", "0"]) == 0
    cap = capsys.readouterr()
    assert "3 profiles" in cap.err
    assert {ln for ln in spoken(cap.err)[2:] if ln != "Pass"} <= {"Liked P0", "Liked P1",
                                                                   "Liked P2"}
