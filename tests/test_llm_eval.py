"""tools/llm_eval.py's checks, on hand-written pages (the live run itself needs a key)."""

from __future__ import annotations

from app.config import Personality, load_profile
from app.schemas import Option
from tools.llm_eval import check_page, detail_words

SAM = load_profile()


def page(*utterances: str, intents: tuple[str, ...] = ("yes", "no", "maybe")) -> list[Option]:
    return [Option(label=f"Opt {i}", utterance=u, intent_id=intents[i])
            for i, u in enumerate(utterances)]


def test_a_plain_page_passes():
    good = page("It was good, thanks.", "Not great, honestly.", "It was okay. How was yours?",
                intents=("feeling", "feeling", "social"))
    assert check_page("How was your weekend?", good, SAM, 14) == []


def test_an_unasked_profile_detail_and_a_past_event_are_flagged():
    bad = page("I went for a walk by the lake.", "Not great.", "Okay.",
               intents=("activity", "feeling", "feeling"))
    flags = check_page("How was your weekend?", bad, SAM, 14)
    assert any("profile detail" in f and "lake" in f for f in flags)
    assert any("claims something happened" in f for f in flags)


def test_a_profile_detail_is_fine_when_asked_about():
    asked = page("I love jazz records.", "Mostly jazz.", "Not much music lately.",
                 intents=("social", "social", "feeling"))
    assert check_page("What music do you like?", asked, SAM, 14) == []


def test_length_follows_the_personality():
    long_one = page("Yes please, that would be really lovely for me right now.", "No.", "Later.",
                    intents=("yes", "no", "later"))
    assert any("words (target 8)" in f for f in check_page("Tea?", long_one, SAM, 14))
    chatty = SAM.model_copy(update={"personality": Personality(length="chatty")})
    assert check_page("Tea?", long_one, chatty, 14) == []


def test_a_general_answer_is_not_an_invented_event():
    general = page("I had a quiet day, thanks.", "Not much, honestly.", "Sorry, say that again?",
                   intents=("social", "social", "repeat"))
    assert check_page("What did you do yesterday?", general, SAM, 14) == []


def test_a_question_needs_different_answers():
    same = page("Yes.", "Sure.", "Yes, please.", intents=("yes", "yes", "yes"))
    assert any("different answers" in f for f in check_page("Tea?", same, SAM, 14))


def test_a_negative_answer_and_a_time_word_are_not_flagged():
    page_ = page("Yes, I slept well.", "No, I did not sleep well.", "Not sure, it's still early.",
                 intents=("yes", "no", "maybe"))
    assert check_page("Did you sleep well last night?", page_, SAM, 14) == []


def test_three_different_feeling_answers_are_fine():
    feelings = page("Pretty good, thanks.", "Not great today.", "So-so.",
                    intents=("feeling", "feeling", "feeling"))
    assert check_page("How are you feeling today?", feelings, SAM, 14) == []


def test_detail_words_come_from_the_profile():
    words = detail_words(SAM)
    assert {"jazz", "coffee", "lake", "maya"} <= words and "the" not in words
