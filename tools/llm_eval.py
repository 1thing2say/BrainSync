"""Live check of Claude's reply options (PLAN.md section 9, the RUN_LLM_TESTS smoke test).

Runs a fixed set of partner lines through the real app/menus.py and flags options that break the
"keep it basic" rules: a profile detail the partner did not ask about, a claim that something
happened, a sentence over the personality's length target, or a question whose options are not
different answers. Prints a table; exits 1 if anything was flagged or no valid page came back.

Needs ANTHROPIC_API_KEY (environment or .env); makes one Claude call per case, never logs the key.

    python tools/llm_eval.py                         # the demo persona (config/profile.yaml)
    python tools/llm_eval.py --profile profiles/user.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from app.config import Profile, load_config, load_env, load_intents, load_profile  # noqa: E402
from app.context import Conversation  # noqa: E402
from app.schemas import Option  # noqa: E402

CASES = [
    "How was your weekend?",
    "What did you do yesterday?",
    "Did you sleep well last night?",
    "Are you hungry?",
    "Do you want to go outside?",
    "Are you cold? I can grab a blanket.",
    "How are you feeling today?",
    "Hey, good to see you!",
    "What should we have for dinner tonight?",
    "What music do you like?",  # asks about a profile fact: using the likes is right here
    "Who is coming to visit this week?",  # nobody said: no invented names
    "Thanks for having me over.",
]

# Flag only clearly long sentences: the prompt asks for "usually 3 to 6" (brief) or "5 to 10"
# (medium) words, so a 7-word answer is fine. Chatty: the config limit.
LENGTH_TARGET = {"brief": 8, "medium": 12}
PAST = re.compile(r"\b(i|we)\s+(went|had|did(?!\s+not)|saw|visited|ate|watched|spent|played|met|"
                  r"called|made|got|took|read|heard)\b", re.I)  # "I did not sleep well" answers
# "I had a quiet day" is a general answer Sam picks if it is true, not an invented event.
GENERAL = re.compile(r"\b(i|we)\s+had\s+an?\s+(\w+\s+)?(day|night|week|weekend|time|evening|"
                     r"morning)\b", re.I)
STOP = {"the", "and", "with", "when", "after", "before", "about", "from", "into", "nice", "good",
        "lives", "uses", "talk", "weather", "morning", "evening", "evenings", "outside",
        # everyday words that also appear in likes and dislikes ("early mornings", "loud rooms")
        "early", "late", "quiet", "loud", "cold", "warm", "rooms", "music"}


def words_in(text: str) -> set[str]:
    return set(re.findall(r"[a-z']+", text.lower()))


def detail_words(profile: Profile) -> set[str]:
    """Words that mark a profile detail (likes, dislikes, routines, people), lower case."""
    text = " ".join([*profile.likes, *profile.dislikes, *profile.routines,
                     *(p.name for p in profile.people)])
    return {w for w in words_in(text) if len(w) >= 4 and w not in STOP}


def check_page(partner: str, options: list[Option], profile: Profile, max_words: int) -> list[str]:
    """Rule breaks in one page of options (empty list = fine)."""
    flags = []
    asked = words_in(partner)
    details = detail_words(profile) - asked
    asks_about_likes = bool(asked & {"like", "likes", "favourite", "favorite", "music", "enjoy"})
    limit = LENGTH_TARGET.get(profile.personality.length, max_words)
    for i, o in enumerate(options, 1):
        used = sorted(words_in(o.utterance) & details)
        if used and not asks_about_likes:
            flags.append(f"{i}: profile detail not asked about ({', '.join(used)})")
        past = PAST.search(o.utterance)
        if past and not GENERAL.search(o.utterance):
            flags.append(f"{i}: claims something happened ({past.group(0)!r})")
        n = len(o.utterance.split())
        if n > limit:
            flags.append(f"{i}: {n} words (target {limit})")
    # Three "feeling" options can be different answers (good / not great / so-so); three "yes"
    # options cannot.
    intents = {o.intent_id for o in options}
    if partner.rstrip().endswith("?") and len(intents) == 1 and intents <= {"yes", "no", "maybe", "later"}:
        flags.append(f"question answered without different answers (all {intents.pop()})")
    return flags


async def run(profile: Profile, cases: list[str]) -> int:
    from app.menus import MenuMaker  # imports the SDK (slow from a /mnt/c venv)

    cfg, intents = load_config(), load_intents()
    maker = MenuMaker(cfg.menus, cfg.llm, intents, profile, os.environ.get("ANTHROPIC_API_KEY"))
    if not maker.enabled:
        print(f"Claude is off: {maker.why_disabled}", file=sys.stderr)
        return 2
    bad = 0
    print(f"profile: {profile.name} (length {profile.personality.length}, "
          f"formality {profile.personality.formality})\n")
    for n, partner in enumerate(cases, 1):
        conv = Conversation(cfg.context.history_turns)
        conv.partner_said(partner)
        started = time.monotonic()
        options = await maker.make_page(conv.page_request(n, "partner", time.time()))
        took = time.monotonic() - started
        if options is None:
            bad += 1
            print(f"FAIL  {partner}  ({took:.1f} s): no valid page\n")
            continue
        flags = check_page(partner, options, profile, cfg.menus.utterance_max_words)
        bad += bool(flags)
        print(f"{'FLAG' if flags else 'ok  '}  {partner}  ({took:.1f} s)")
        for i, o in enumerate(options, 1):
            print(f"      {i}. {o.utterance}  [{o.label}]")
        for f in flags:
            print(f"      ! {f}")
        print()
    print(f"{len(cases) - bad}/{len(cases)} pages clean")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="llm_eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", type=Path, help="a profile YAML (default: the demo persona)")
    args = p.parse_args(argv)
    load_env()
    if args.profile:
        profile = Profile.model_validate(yaml.safe_load(args.profile.read_text(encoding="utf-8")))
    else:
        profile = load_profile()
    sys.exit(asyncio.run(run(profile, CASES)))


if __name__ == "__main__":
    main()
