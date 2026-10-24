"""app/menus.py and app/context.py with a fake Claude: nothing here calls the API."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from app.config import load_config, load_intents, load_profile
from app.context import Conversation, PageRequest, Turn, time_of_day
from app.log import EventLog, read_events
from app.menus import TOOL_NAME, MenuMaker, rank_keys, reply_tool, shorten_label, user_message

CFG = load_config()
INTENTS = load_intents()
PROFILE = load_profile()

GOOD = {
    "option_1": {"label": "Let's go", "utterance": "Yes, let's go outside.", "intent_id": "yes"},
    "option_2": {"label": "Not now", "utterance": "Not right now, thanks.", "intent_id": "later"},
    "option_3": {"label": "After lunch", "utterance": "Maybe after lunch.", "intent_id": "maybe"},
}


def reply(options: dict | None = None, stop_reason: str = "tool_use", name: str = TOOL_NAME):
    """A Messages API response carrying one propose_replies call."""
    content = [] if options is None else [SimpleNamespace(type="tool_use", name=name, input=options)]
    return SimpleNamespace(stop_reason=stop_reason, content=content,
                           usage=SimpleNamespace(input_tokens=900, output_tokens=80))


class FakeClaude:
    """Stands in for ``AsyncAnthropic().messages.create``: answers in order, the last one repeats."""

    def __init__(self, *answers, delay: float = 0.0) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.delay = delay

    async def __call__(self, **params):
        self.calls.append(params)
        if self.delay:
            await asyncio.sleep(self.delay)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class APIStatusError(Exception):
    """Shaped like the SDK's errors (``.status_code``), without importing the slow SDK."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def request(**changes) -> PageRequest:
    fields = dict(request_id=7, reason="partner", partner_last="Do you want to go outside?",
                  history=(Turn("partner", "Do you want to go outside?"),), shown=(),
                  time_of_day="Saturday, 3:40 PM (afternoon)")
    fields.update(changes)
    return PageRequest(**fields)


@pytest.fixture
def event_log(tmp_path):
    log = EventLog(tmp_path)
    yield log
    log.close()


def maker(fake, event_log=None, timeout_s: float = 4.0, **menus) -> MenuMaker:
    return MenuMaker(CFG.menus.model_copy(update=menus),
                     CFG.llm.model_copy(update={"timeout_s": timeout_s}),
                     INTENTS, PROFILE, api_key=None, log=event_log, create=fake)


def llm_events(event_log) -> list[dict]:
    return [e for e in read_events(event_log.path) if e["event"].startswith("llm_")]


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


async def test_valid_answer_gives_ranked_options_and_sends_a_forced_strict_call(event_log):
    fake = FakeClaude(reply(GOOD))
    options = await maker(fake, event_log).make_page(request())
    assert [o.utterance for o in options] == [GOOD[k]["utterance"] for k in rank_keys(3)]
    (params,) = fake.calls
    assert params["model"] == "claude-haiku-5-5"
    assert params["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert params["tools"][0]["strict"] is True
    assert not {"temperature", "top_p", "top_k"} & set(params)  # a 400 on Claude Haiku 5.5
    assert "Do you want to go outside?" in params["messages"][0]["content"]
    assert "You write reply options for Sam," in params["system"] and "medical" in params["system"]
    ok = llm_events(event_log)[-1]
    assert ok["event"] == "llm_ok" and ok["options"][0] == "Yes, let's go outside."


def test_tool_schema_is_strict_with_one_field_per_rank():
    schema = reply_tool(INTENTS, CFG.menus)["input_schema"]
    assert schema["required"] == ["option_1", "option_2", "option_3"]
    assert schema["additionalProperties"] is False
    option = schema["properties"]["option_1"]
    assert option["additionalProperties"] is False
    assert option["properties"]["intent_id"]["enum"] == INTENTS.intent_ids


def test_user_message_lists_history_partner_line_and_shown():
    text = user_message(request(shown=("Yes, please.",), history=(
        Turn("partner", "Hi Sam"), Turn("user", "Hi, good to see you."),
        Turn("partner", "Do you want to go outside?"))), PROFILE.name)
    assert "Partner: Hi Sam" in text and f"{PROFILE.name}: Hi, good to see you." in text
    assert "<partner>Do you want to go outside?</partner>" in text
    assert "- Yes, please." in text and "Saturday" in text
    assert "has not said anything" in user_message(request(partner_last="", history=()), PROFILE.name)


def test_user_message_after_a_spoken_reply_asks_for_something_new():
    text = user_message(request(reason="more", history=(
        Turn("partner", "Do you want to go outside?"), Turn("user", "Yes, let's go outside."))),
        PROFILE.name)
    assert f"{PROFILE.name} has already replied" in text and "not another answer" in text
    assert "<partner>" not in text  # the question is only in the history now


# ---------------------------------------------------------------------------
# Validation, retry and fallback
# ---------------------------------------------------------------------------


def _with(rank: str, **fields) -> dict:
    return {**GOOD, rank: {**GOOD[rank], **fields}}


@pytest.mark.parametrize("bad, problem", [
    (_with("option_3", utterance="word " * 15), "utterance over"),
    (_with("option_1", intent_id="teleport"), "unknown intent"),
    (_with("option_3", utterance=GOOD["option_1"]["utterance"]), "duplicate"),
    ({k: v for k, v in GOOD.items() if k != "option_3"}, "option_3"),
], ids=["long utterance", "unknown intent", "duplicate", "missing option"])
async def test_an_invalid_answer_is_retried_once(event_log, bad, problem):
    fake = FakeClaude(reply(bad), reply(GOOD))
    options = await maker(fake, event_log).make_page(request())
    assert options is not None and len(fake.calls) == 2
    invalid = [e for e in llm_events(event_log) if e["event"] == "llm_invalid"]
    assert len(invalid) == 1 and any(problem in p for p in invalid[0]["problems"])


async def test_the_retry_names_what_was_wrong():
    fake = FakeClaude(reply(_with("option_3", utterance="word " * 15)), reply(GOOD))
    await maker(fake).make_page(request())
    first, retry = (c["messages"][0]["content"] for c in fake.calls)
    assert "could not be used" not in first
    assert "could not be used" in retry and "option_3: utterance over 14 words" in retry


@pytest.mark.parametrize("label, short", [
    ("Not sure about dinner", "Not sure"),  # 4 words: the first 3, then no dangling "about"
    ("Something comfortable tonight", "Something"),  # 3 words but 29 chars: cut at a word
    ("Unbelievablyfantastic", "Unbelievablyfantas"),  # one long word: cut to 18 chars
    ("Let's go", "Let's go"),
    ("Walk to the lake", "Walk"),  # no dangling "to the"
    ("A walk by the lake", "A walk"),
    ("Not yet, too early", "Not yet"),
    ("Up to you", "Up to you"),  # fits already: never changed
    ("No one, I think", "No one"),
    ("So-so. How about you?", "So-so"),
])
def test_shorten_label(label, short):
    assert shorten_label(label, 3, 18) == short


async def test_a_long_label_is_shortened_and_the_page_kept(event_log):
    fake = FakeClaude(reply(_with("option_2", label="Not sure about dinner")))
    options = await maker(fake, event_log).make_page(request())
    assert len(fake.calls) == 1 and options[1].label == "Not sure"
    assert options[1].utterance == GOOD["option_2"]["utterance"]  # what is spoken never changes
    ok = llm_events(event_log)[-1]
    assert ok["event"] == "llm_ok" and ok["labels_shortened"] == ["'Not sure about dinner' -> 'Not sure'"]


async def test_two_invalid_answers_give_none(event_log):
    fake = FakeClaude(reply(_with("option_1", label="")))
    assert await maker(fake, event_log).make_page(request()) is None
    assert len(fake.calls) == 2
    assert [e["event"] for e in llm_events(event_log)] == ["llm_request", "llm_invalid", "llm_invalid"]


async def test_options_already_shown_are_rejected():
    fresh = {"label": "Sounds good", "utterance": "That sounds good to me.", "intent_id": "yes"}
    fake = FakeClaude(reply(GOOD), reply({**GOOD, "option_1": fresh}))
    options = await maker(fake).make_page(request(reason="more", shown=("Yes, let's go outside.",)))
    assert len(fake.calls) == 2 and options[0].utterance == "That sounds good to me."


@pytest.mark.parametrize("answer", [reply(None), reply(GOOD, stop_reason="refusal"),
                                    reply(GOOD, name="something_else")],
                         ids=["no tool call", "refusal", "wrong tool"])
async def test_answers_without_a_usable_call_fall_back(answer):
    assert await maker(FakeClaude(answer)).make_page(request()) is None


async def test_a_slow_answer_times_out_within_llm_timeout(event_log):
    fake = FakeClaude(reply(GOOD), delay=1.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await maker(fake, event_log, timeout_s=0.2).make_page(request()) is None
    assert loop.time() - started < 0.6
    assert llm_events(event_log)[-1]["reason"] == "timeout"


async def test_a_connection_error_is_retried():
    fake = FakeClaude(ConnectionError("offline"), reply(GOOD))
    assert await maker(fake).make_page(request()) is not None
    assert len(fake.calls) == 2


async def test_a_transient_error_is_retried():
    fake = FakeClaude(APIStatusError(500), reply(GOOD))
    assert await maker(fake).make_page(request()) is not None
    assert len(fake.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_a_permanent_error_is_not_retried(event_log, status):
    fake = FakeClaude(APIStatusError(status))
    assert await maker(fake, event_log).make_page(request()) is None
    assert len(fake.calls) == 1
    failed = llm_events(event_log)[-1]
    assert failed["event"] == "llm_failed" and failed["status"] == status


async def test_an_sdk_too_old_for_the_parameters_is_not_retried(event_log):
    fake = FakeClaude(TypeError("create() got an unexpected keyword argument 'output_config'"))
    assert await maker(fake, event_log).make_page(request()) is None
    assert len(fake.calls) == 1
    assert llm_events(event_log)[-1]["reason"] == "TypeError"


async def test_request_calls_done_with_the_request_id():
    got = []
    m = maker(FakeClaude(reply(GOOD)))
    m.request(request(request_id=42), lambda rid, options: got.append((rid, options)))
    await asyncio.sleep(0.05)
    assert got and got[0][0] == 42 and len(got[0][1]) == 3
    await m.stop()


def test_disabled_without_a_key_or_with_dynamic_off():
    no_key = MenuMaker(CFG.menus, CFG.llm, INTENTS, PROFILE, api_key=None)
    assert not no_key.enabled and no_key.why_disabled == "ANTHROPIC_API_KEY is not set"
    off = maker(FakeClaude(reply(GOOD)), dynamic=False)
    assert not off.enabled and off.why_disabled == "menus.dynamic is false"


# ---------------------------------------------------------------------------
# Conversation (app/context.py)
# ---------------------------------------------------------------------------


def test_conversation_keeps_recent_turns_and_shown_since_the_partner_spoke():
    c = Conversation(history_turns=2)
    c.showed(["Hello."])
    c.partner_said("Hi Sam")  # a new partner line: earlier options may fit again
    c.showed(["Hi!", "Hello."])
    c.user_said("Hi!")
    c.partner_said("Coffee?")
    c.showed(["Yes, please."])
    c.showed(["Yes, please."])
    req = c.page_request(3, "more", 0.0)
    assert req.partner_last == "Coffee?"
    assert req.history == (Turn("user", "Hi!"), Turn("partner", "Coffee?"))
    assert req.shown == ("Yes, please.",)


def test_time_of_day_names_the_part_of_the_day():
    afternoon = dt.datetime(2026, 10, 24, 15, 40).timestamp()
    assert time_of_day(afternoon) == "Saturday, 3:40 PM (afternoon)"


# ---------------------------------------------------------------------------
# Plain replies and the personality map in the prompt
# ---------------------------------------------------------------------------

from app.config import Person, Personality, Profile  # noqa: E402
from app.menus import system_prompt, voice_sample  # noqa: E402

ANA = Profile(name="Ana Lopez", about="Ana lives in Fresno.", likes=["salsa music"],
              people=[Person(name="Rosa", relation="mother")], avoid_topics=["her old job"],
              personality=Personality(formality="formal", length="medium", humour="playful",
                                      warmth="reserved", directness="direct",
                                      phrases=["No worries"], avoid_phrases=["awesome"],
                                      notes="Dry wit, short sentences."))


def test_prompt_keeps_replies_basic_and_facts_for_when_asked():
    text = system_prompt(PROFILE, INTENTS, CFG.menus)
    assert "keep what the replies say basic" in text
    # Sam is casual, light humour, warm: the weekend answers in that voice.
    assert '"Pretty good, thanks!" / "Not great, honestly." / "Alright. No complaints! How about you?"' in text
    assert "only when the partner asks about exactly that thing" in text
    assert "(a fictional demo persona)" in text and "iced coffee" in text


def test_prompt_carries_the_personality_map():
    text = system_prompt(ANA, INTENTS, CFG.menus)
    assert "You write reply options for Ana," in text
    assert "polite and a little formal" in text and "reserved" in text and "playful" in text
    assert "usually 5 to 10 words" in text and 'says no plainly ("No.")' in text
    assert '"No worries"' in text and '"awesome"' in text and "Dry wit, short sentences." in text
    assert "Never bring up: her old job." in text
    assert "Rosa (mother)" in text and "fictional" not in text


@pytest.mark.parametrize("personality, sample", [
    (Personality(formality="neutral", humour="none", warmth="reserved"),
     ("It was good, thanks.", "Not great, honestly.", "It was okay.")),
    (Personality(formality="formal", humour="none", warmth="reserved", directness="direct"),
     ("It was very pleasant, thank you.", "It was not good.", "Quite ordinary, thank you.")),
    (Personality(formality="casual", humour="playful", warmth="warm", directness="direct",
                 length="chatty", phrases=["no cap"]),
     ("Honestly? Pretty awesome, no cap.", "Rough one, not gonna lie.",
      "Meh. Could've been worse! How about you?")),
], ids=["plain", "formal and direct", "playful with a phrase"])
def test_voice_sample_follows_the_personality(personality, sample):
    assert voice_sample(Profile(name="X", personality=personality)) == sample


def test_the_voice_sample_is_in_the_prompt():
    text = system_prompt(ANA, INTENTS, CFG.menus)
    good, bad, middle = voice_sample(ANA)
    assert f'"{good}" / "{bad}" / "{middle}"' in text


def test_chatty_length_names_the_word_limit():
    chatty = ANA.model_copy(update={"personality": Personality(length="chatty")})
    assert f"up to {CFG.menus.utterance_max_words} words" in system_prompt(chatty, INTENTS, CFG.menus)


async def test_set_profile_changes_the_next_request():
    fake = FakeClaude(reply(GOOD))
    m = maker(fake)
    m.set_profile(ANA)
    await m.make_page(request())
    assert "You write reply options for Ana," in fake.calls[0]["system"]
    assert "Never bring up: her old job." in fake.calls[0]["system"]


async def test_preview_uses_the_given_profile_without_keeping_it():
    fake = FakeClaude(reply(GOOD))
    m = maker(fake)
    options = await m.preview(ANA, "How was your weekend?", 0.0)
    assert len(options) == 3
    assert "for Ana," in fake.calls[0]["system"]
    assert "<partner>How was your weekend?</partner>" in fake.calls[0]["messages"][0]["content"]
    assert m.profile is PROFILE and "for Sam," in m.system  # the active profile is unchanged
