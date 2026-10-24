"""LLM option ranking: one Claude call per page, validated, with the fixed lists as fallback
(PLAN.md V3, items 2-4).

``MenuMaker.request(page_request, done)`` starts the call in the background and later calls
``done(request_id, options)`` with exactly ``menus.option_count`` options ranked most likely first,
or ``None`` when the call failed, timed out or returned nothing valid (the orchestrator then keeps
the fixed list from config/intents.yaml). The LLM only writes options: it never picks or changes a
selection (rule 1), and an option is spoken exactly as shown (rule 2).

The call forces one strict tool call, ``propose_replies``, whose input has one field per rank
(``option_1`` .. ``option_3``), so the output is schema-valid JSON with the ranking explicit.
Lengths, intents and duplicates are checked here (strict schemas cannot express them). A label
that is too long is shortened rather than rejected: it is only the short tag beside a square, and
the utterance (what is spoken) is never changed. An invalid answer is retried once with the
problems named, and a transient error once, within ``llm.timeout_s`` in all.

The API key comes from the environment (``ANTHROPIC_API_KEY``, loaded from ``.env`` by the
server's ``main``) and is never logged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from app.config import IntentsConfig, LLMConfig, MenusConfig, Profile
from app.context import PageRequest
from app.log import EventLog
from app.schemas import Option, validate_option

TOOL_NAME = "propose_replies"
MAX_TOKENS = 1024  # three short options; a forced tool call skips thinking on Claude Haiku 5.5
ATTEMPTS = 2  # the first try and one retry
# HTTP statuses a retry cannot fix: bad request, key, permissions, model name.
PERMANENT_STATUS = {400, 401, 403, 404}

Done = Callable[[int, "list[Option] | None"], None]
Create = Callable[..., Awaitable[Any]]


class MenuMaker:
    """Writes pages of reply options with Claude. Disabled (``enabled`` False) without a key."""

    def __init__(
        self,
        menus: MenusConfig,
        llm: LLMConfig,
        intents: IntentsConfig,
        profile: Profile,
        api_key: str | None,
        log: EventLog | None = None,
        create: Create | None = None,
    ) -> None:
        """``create`` replaces ``AsyncAnthropic().messages.create`` (tests)."""
        self.menus = menus
        self.llm = llm
        self.intents = intents
        self.profile = profile
        self.log = log
        self._create = create
        if create is None and api_key:
            # Imported only here: the SDK has thousands of files, and from a venv on /mnt/c (WSL)
            # importing it takes about 2 minutes, which tests and keyless runs should not pay.
            import anthropic

            # Our own retry and time limit apply, so the SDK's are turned off.
            client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=llm.timeout_s)
            self._create = client.messages.create
        self.system = system_prompt(profile, intents, menus)
        self.tool = reply_tool(intents, menus)
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return not self.why_disabled

    @property
    def why_disabled(self) -> str:
        """Empty when Claude writes the options; otherwise why only the fixed lists are used."""
        if not self.menus.dynamic:
            return "menus.dynamic is false"
        if self._create is None:
            return "ANTHROPIC_API_KEY is not set"
        return ""

    # -- requests -----------------------------------------------------------

    def request(self, req: PageRequest, done: Done) -> None:
        """Start writing a page in the background (returns at once; for the orchestrator)."""
        task = asyncio.get_running_loop().create_task(self._run(req, done))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, req: PageRequest, done: Done) -> None:
        done(req.request_id, await self.make_page(req))

    async def make_page(self, req: PageRequest) -> list[Option] | None:
        """The ranked options for ``req``, or None (fall back to the fixed list)."""
        self._log("llm_request", request_id=req.request_id, reason=req.reason,
                  partner_last=req.partner_last)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.llm.timeout_s
        feedback: list[str] = []  # what was wrong with the last answer, for the retry
        for attempt in range(1, ATTEMPTS + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            started = time.monotonic()
            try:
                msg = await asyncio.wait_for(self._create(**self.params(req, feedback)), remaining)
            except TimeoutError:
                self._log("llm_failed", request_id=req.request_id, reason="timeout", attempt=attempt)
                return None  # no time left for a retry
            except Exception as exc:  # the SDK's APIStatusError carries .status_code
                status = getattr(exc, "status_code", None)
                self._log("llm_failed", request_id=req.request_id, reason=type(exc).__name__,
                          status=status, attempt=attempt)
                # A TypeError is an SDK too old for these parameters: a retry cannot fix it either.
                if status in PERMANENT_STATUS or isinstance(exc, TypeError):
                    return None
                continue  # connection errors, 429, 5xx: retry if time allows
            ms = round((time.monotonic() - started) * 1000)
            options, problems, shortened = self.parse(msg, req)
            usage = getattr(msg, "usage", None)
            tokens = {"input_tokens": getattr(usage, "input_tokens", None),
                      "output_tokens": getattr(usage, "output_tokens", None)}
            if options is not None:
                self._log("llm_ok", request_id=req.request_id, attempt=attempt, ms=ms,
                          options=[o.utterance for o in options], labels_shortened=shortened,
                          **tokens)
                return options
            self._log("llm_invalid", request_id=req.request_id, attempt=attempt, ms=ms,
                      problems=problems, **tokens)
            feedback = problems
        return None

    def params(self, req: PageRequest, feedback: list[str] | None = None) -> dict[str, Any]:
        """The Messages API request for one page; ``feedback`` names what a rejected answer broke."""
        content = user_message(req, self.profile.name)
        if feedback:
            content += ("\n\nYour previous answer could not be used: " + "; ".join(feedback)
                        + f". Call {TOOL_NAME} again with every rule met.")
        return {
            "model": self.llm.model,
            "max_tokens": MAX_TOKENS,
            "system": self.system,
            "tools": [self.tool],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": content}],
        }

    # -- validation ---------------------------------------------------------

    def parse(self, msg: Any, req: PageRequest) -> tuple[list[Option] | None, list[str], list[str]]:
        """Options from the response (or None), the problems found, and the labels shortened."""
        if getattr(msg, "stop_reason", None) == "refusal":
            return None, ["refused"], []
        block = next((b for b in getattr(msg, "content", []) or []
                      if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == TOOL_NAME),
                     None)
        if block is None or not isinstance(block.input, dict):
            return None, [f"no {TOOL_NAME} call (stop_reason {getattr(msg, 'stop_reason', None)})"], []
        problems: list[str] = []
        shortened: list[str] = []
        options: list[Option] = []
        known = set(self.intents.intent_ids)
        for key in rank_keys(self.menus.option_count):
            try:
                option = Option.model_validate(block.input.get(key))
            except ValidationError as exc:
                problems.append(f"{key}: {exc.errors()[0].get('msg')}")
                continue
            label = shorten_label(option.label, self.menus.label_max_words, self.menus.label_max_chars)
            if label != option.label.strip():
                shortened.append(f"{option.label.strip()!r} -> {label!r}")
            option = Option(label=label, utterance=option.utterance.strip(),
                            intent_id=option.intent_id.strip())
            problems += [f"{key}: {p}" for p in validate_option(
                option, self.menus.label_max_chars, self.menus.utterance_max_words,
                self.menus.label_max_words)]
            if option.intent_id not in known:
                problems.append(f"{key}: unknown intent {option.intent_id!r}")
            options.append(option)
        labels = [o.label.casefold() for o in options]
        utterances = [o.utterance.casefold() for o in options]
        if len(set(labels)) != len(labels) or len(set(utterances)) != len(utterances):
            problems.append("duplicate options")
        shown = {u.casefold() for u in req.shown}
        repeats = [o.utterance for o in options if o.utterance.casefold() in shown]
        if repeats:
            problems.append(f"repeats options already shown: {repeats}")
        if problems:
            return None, problems, shortened
        return options, [], shortened

    def _log(self, event: str, **fields: object) -> None:
        if self.log is not None:
            self.log.log(event, **fields)


# ---------------------------------------------------------------------------
# Prompt and tool
# ---------------------------------------------------------------------------


# Words a shortened label should not end on ("Walk to the" -> "Walk").
DANGLING = {"a", "an", "the", "to", "by", "of", "for", "at", "in", "on", "with", "and", "or", "but",
            "too", "my", "your", "our", "is", "are", "be", "so", "if"}


def shorten_label(label: str, max_words: int, max_chars: int) -> str:
    """The first ``max_words`` words of ``label``, cut at a word boundary to ``max_chars``, without
    a dangling last word. A label that already fits is returned unchanged.

    Only the label is shortened: it is the tag beside a square, never what is spoken.
    """
    label = label.strip()
    words = label.split()
    if len(words) <= max_words and len(label) <= max_chars:
        return label
    words = words[:max_words]
    while len(words) > 1 and len(" ".join(words)) > max_chars:
        words.pop()
    while len(words) > 1 and words[-1].strip(",.;:-!?").lower() in DANGLING:
        words.pop()
    return " ".join(words)[:max_chars].rstrip(" ,.;:-")


def rank_keys(count: int) -> list[str]:
    return [f"option_{i}" for i in range(1, count + 1)]


def reply_tool(intents: IntentsConfig, menus: MenusConfig) -> dict[str, Any]:
    """The strict ``propose_replies`` tool: one field per rank, each a label, sentence and intent."""
    option = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description":
                      f"Short label beside the target: at most {menus.label_max_words} words and "
                      f"{menus.label_max_chars} characters."},
            "utterance": {"type": "string", "description":
                          f"The exact sentence that will be spoken aloud, in the first person: at "
                          f"most {menus.utterance_max_words} words."},
            "intent_id": {"type": "string", "enum": intents.intent_ids,
                          "description": "What the reply does."},
        },
        "required": ["label", "utterance", "intent_id"],
        "additionalProperties": False,
    }
    keys = rank_keys(menus.option_count)
    return {
        "name": TOOL_NAME,
        "description": (f"Give the {len(keys)} replies the user is most likely to want to say next, "
                        "ranked: option_1 is the most likely."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {key: option for key in keys},
            "required": keys,
            "additionalProperties": False,
        },
    }


def system_prompt(profile: Profile, intents: IntentsConfig, menus: MenusConfig) -> str:
    """Static instructions: the rules, the persona and the intents (the same for every call)."""
    people = ", ".join(f"{p.name} ({p.relation})" for p in profile.people)
    intent_lines = "\n".join(f"- {i.id}: {i.description}" for i in intents.intents)
    n = menus.option_count
    return f"""\
You write reply options for {profile.name}, who talks through a brain-computer interface. \
{profile.name} cannot speak or type: they choose one of {n} short replies by looking at it, then \
confirm it, and the laptop says the sentence aloud. Choosing takes several seconds, so the right \
reply should be among the {n} you write, and most often it should be option_1.

How to write the options:
- Write each sentence in the first person, as {profile.name} would say it to the person they are \
talking with. Keep it natural and spoken, at most {menus.utterance_max_words} words.
- When the partner asked a question, every option answers that question.
- For a yes/no question, put the answer {profile.name} is most likely to give first, and include a \
"no" or "not now" answer in option_1 or option_2, so declining is always quick.
- Make the {n} options clearly different from each other: different answers or different needs, \
not rewordings of one reply.
- Use only facts from the profile below or from the conversation. Never invent names, plans, \
places or events.
- Never give medical advice or make medical claims.
- Each label is a short tag beside its square, at most {menus.label_max_words} words and \
{menus.label_max_chars} characters, so {profile.name} recognises the sentence at a glance: for example \
"Let's go", "Not now", "After lunch", "Coffee first". The full sentence goes in the utterance.

About {profile.name} (a fictional demo persona): {profile.about}
Likes: {", ".join(profile.likes)}.
Dislikes: {", ".join(profile.dislikes)}.
People: {people}.
Routines: {"; ".join(profile.routines)}.

Intents (pick the one that fits each option):
{intent_lines}

Always answer by calling {TOOL_NAME}."""


def user_message(req: PageRequest, name: str) -> str:
    """The changing part: time, recent conversation, the partner's last line, what was shown."""
    lines = [f"Time: {req.time_of_day}", ""]
    if req.history:
        lines.append("Conversation so far, oldest first:")
        lines += [f"{'Partner' if t.who == 'partner' else name}: {t.text}" for t in req.history]
        lines.append("")
    if req.partner_last and req.history and req.history[-1].who == "user":
        # "More options" after a spoken reply: the partner's line is answered already.
        lines.append(f"{name} has already replied to the partner's last line. Offer what {name} "
                     "is most likely to want to say next: a follow-up, a request or a new topic, "
                     "not another answer to it.")
    elif req.partner_last:
        lines.append(f"The partner just said: <partner>{req.partner_last}</partner>")
    else:
        lines.append(f"The partner has not said anything yet. Offer what {name} is most likely to "
                     "want to say or ask for right now.")
    if req.shown:
        lines += ["", f"{name} has already seen these options and wants different ones:"]
        lines += [f"- {u}" for u in req.shown]
    lines += ["", f"Write the replies {name} is most likely to want to say next."]
    return "\n".join(lines)
