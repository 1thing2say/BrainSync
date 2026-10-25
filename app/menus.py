"""LLM option ranking: one Claude call per page, validated, with the fixed lists as fallback
(PLAN.md V3, items 2-4).

``MenuMaker.request(page_request, done)`` starts the call in the background and later calls
``done(request_id, options)`` with exactly ``menus.option_count`` options ranked most likely first,
or ``None`` when the call failed, timed out or returned nothing valid (the orchestrator then keeps
the fixed list from config/intents.yaml). The LLM only writes options: it never picks or changes a
selection (rule 1), and an option is spoken exactly as shown (rule 2).

The call forces one strict tool call, ``propose_replies``, whose input has one field per rank
(``option_1`` .. ``option_4`` with the defaults), so the output is schema-valid JSON with the
ranking explicit. It asks for ``menus.spare_options`` more candidates than a page shows, ranked
last. Lengths, intents, duplicates and repeats of options already shown are checked here, for
each candidate on its own (strict schemas cannot express them): the page is the first
``menus.option_count`` usable candidates in rank order, so one bad candidate costs a spare, not the
page. Duplicates and repeats are compared without case, punctuation or extra spaces ("Yes,
please." repeats "yes please"), and on the sentence: two sentences whose labels match (often only
after shortening) both stay unless a spare with its own label can take the second one's place. A
label that is too long is shortened rather than rejected: it is only the short tag beside a
square, and the utterance (what is spoken) is never changed.

Each page gets at most three calls, all inside ``llm.timeout_s``: the first; a second, identical
one (the hedge) if the first has not answered after ``llm.hedge_after_s``, since a call stuck on a
congested network rarely recovers in time; and one retry after an answer with too few usable
candidates (the problems named) or a transient error. The first usable answer wins and the other
calls are cancelled. A permanent error (bad request, key, model) gives up at once.

The API key comes from the environment (``ANTHROPIC_API_KEY``, loaded from ``.env`` by the
server's ``main``) and is never logged.

``start()`` (server lifespan) opens the connection to the API at once and keeps it open with a
cheap call (``models.retrieve``, no tokens) every ``llm.keepalive_s`` while idle, and right after a
failed page (a call cut off by the time limit closes its connection). A page then skips DNS, TCP
and TLS: three round trips, 1-2 s on a good network and 3-9 s on the congested venue Wi-Fi of
2026-10-24, all of it inside ``llm.timeout_s``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from pydantic import ValidationError

from app.config import IntentsConfig, LLMConfig, MenusConfig, Profile
from app.context import Conversation, PageRequest
from app.log import EventLog
from app.schemas import Option, validate_option

TOOL_NAME = "propose_replies"
MAX_TOKENS = 1024  # a few short options; a forced tool call skips thinking on Claude Haiku 5.5
# HTTP statuses a retry cannot fix: bad request, key, permissions, model name.
PERMANENT_STATUS = {400, 401, 403, 404}

Done = Callable[[int, "list[Option] | None"], None]
Create = Callable[..., Awaitable[Any]]
Warm = Callable[[], Awaitable[Any]]


class Call(NamedTuple):
    """One Claude call of a page: ``first``, ``hedge`` or ``retry``, numbered in start order."""

    name: str
    attempt: int
    started: float  # time.monotonic()


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
        warm: Warm | None = None,
    ) -> None:
        """``create`` replaces ``AsyncAnthropic().messages.create`` and ``warm`` the keep-alive
        call (tests). Without ``warm`` nothing is kept open."""
        self.menus = menus
        self.llm = llm
        self.intents = intents
        self.profile = profile
        self.log = log
        self._create = create
        self._warm = warm
        self._client: Any = None
        if create is None and api_key:
            # Imported only here: the SDK has thousands of files, and from a venv on /mnt/c (WSL)
            # importing it takes about 2 minutes, which tests and keyless runs should not pay.
            import anthropic
            import httpx2  # the SDK's HTTP library, installed with it

            # httpx closes a connection after 5 s idle; keep it open between keep-alive calls.
            keep = llm.keepalive_s
            http = anthropic.DefaultAsyncHttpxClient(limits=httpx2.Limits(
                max_connections=10, max_keepalive_connections=2,
                keepalive_expiry=2 * keep if keep else 5.0))
            # Our own retry and time limit apply, so the SDK's are turned off.
            client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=llm.timeout_s,
                                              http_client=http)
            self._client = client
            self._create = client.messages.create
            # More time than a page gets: on a slow network the connection still opens, for the next.
            warm_timeout = max(keep, llm.timeout_s)
            self._warm = lambda: client.models.retrieve(llm.model, timeout=warm_timeout)
        self.system = system_prompt(profile, intents, menus)
        self.tool = reply_tool(intents, menus)
        self._tasks: set[asyncio.Task[None]] = set()
        self._keeper: asyncio.Task[None] | None = None
        self._rewarm = asyncio.Event()  # set after a failed page: open a new connection now
        self._busy = 0  # page calls in flight (a keep-alive call then would open a second connection)
        self._warm_ok: bool | None = None

    @property
    def enabled(self) -> bool:
        return not self.why_disabled

    @property
    def warm_ok(self) -> bool | None:
        """Whether the last keep-alive call answered; None before the first one (or keep-alive off)."""
        return self._warm_ok

    @property
    def keepalive_running(self) -> bool:
        return self._keeper is not None

    @property
    def why_disabled(self) -> str:
        """Empty when Claude writes the options; otherwise why only the fixed lists are used."""
        if not self.menus.dynamic:
            return "menus.dynamic is false"
        if self._create is None:
            return "ANTHROPIC_API_KEY is not set"
        return ""

    def set_profile(self, profile: Profile) -> None:
        """Use ``profile`` from the next page on (the profile page saved a new one)."""
        self.profile = profile
        self.system = system_prompt(profile, self.intents, self.menus)

    async def preview(self, profile: Profile, partner_text: str, now_unix: float) -> list[Option] | None:
        """One page for ``partner_text`` written with ``profile`` (not saved), for the profile page."""
        trial = MenuMaker(self.menus, self.llm, self.intents, profile, api_key=None, log=self.log,
                          create=self._create)
        conv = Conversation(history_turns=1)
        conv.partner_said(partner_text)
        return await trial.make_page(conv.page_request(0, "partner", now_unix))

    # -- requests -----------------------------------------------------------

    def request(self, req: PageRequest, done: Done) -> None:
        """Start writing a page in the background (returns at once; for the orchestrator)."""
        task = asyncio.get_running_loop().create_task(self._run(req, done))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self) -> None:
        """Open the API connection now and keep it open (server lifespan; needs a running loop)."""
        if self.enabled and self._warm is not None and self.llm.keepalive_s > 0 and not self._keeper:
            self._keeper = asyncio.get_running_loop().create_task(self._keep_warm())

    async def stop(self) -> None:
        tasks = list(self._tasks) + ([self._keeper] if self._keeper else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._keeper = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _keep_warm(self) -> None:
        while True:
            # Cleared before the call, so a page that fails during it still asks for a rewarm.
            self._rewarm.clear()
            if not self._busy:
                await self.warm()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._rewarm.wait(), self.llm.keepalive_s)

    async def warm(self) -> bool:
        """One keep-alive call. Logs the first result and every change, not each call."""
        started = time.monotonic()
        fields: dict[str, object] = {}
        try:
            await self._warm()
        except Exception as exc:  # never fatal: the page calls report their own failures
            fields = {"reason": type(exc).__name__, "status": getattr(exc, "status_code", None)}
        ok = not fields
        if ok != self._warm_ok:
            self._log("llm_warm" if ok else "llm_warm_failed",
                      ms=round((time.monotonic() - started) * 1000), **fields)
        self._warm_ok = ok
        return ok

    async def _run(self, req: PageRequest, done: Done) -> None:
        done(req.request_id, await self.make_page(req))

    async def make_page(self, req: PageRequest) -> list[Option] | None:
        """The ranked options for ``req``, or None (fall back to the fixed list)."""
        self._busy += 1
        try:
            options = await self._make_page(req)
        finally:
            self._busy -= 1
        if options is None:
            self._rewarm.set()  # a call cut off by the time limit closed its connection
        return options

    async def _make_page(self, req: PageRequest) -> list[Option] | None:
        """Up to three calls (first, hedge, retry) inside ``llm.timeout_s``; the first usable
        answer wins. Calls still running when it returns are cancelled and awaited."""
        self._log("llm_request", request_id=req.request_id, reason=req.reason,
                  partner_last=req.partner_last)
        loop = asyncio.get_running_loop()
        page_started = time.monotonic()
        deadline = loop.time() + self.llm.timeout_s
        hedge_s = self.llm.hedge_after_s
        hedge_at = loop.time() + hedge_s if 0 < hedge_s < self.llm.timeout_s else None
        calls: dict[asyncio.Task[Any], Call] = {}  # the calls still running
        attempts = 0  # calls started (at most 3: first, hedge, retry)
        retried = False  # the one retry is used (after an invalid answer or a transient error)

        def ms_since(started: float) -> int:
            return round((time.monotonic() - started) * 1000)

        def start(name: str, feedback: list[str]) -> asyncio.Task[Any]:
            nonlocal attempts
            attempts += 1

            # Called inside the task, so an SDK that rejects the parameters (TypeError when the
            # call is made) fails that call like any other error.
            async def send() -> Any:
                return await self._create(**self.params(req, feedback))

            task = loop.create_task(send())
            calls[task] = Call(name, attempts, time.monotonic())
            return task

        try:
            first = start("first", [])
            while calls:
                wake = deadline if hedge_at is None else min(deadline, hedge_at)
                done, _ = await asyncio.wait(calls, timeout=max(0.0, wake - loop.time()),
                                             return_when=asyncio.FIRST_COMPLETED)
                for task in sorted(done, key=lambda t: calls[t].attempt):
                    call = calls.pop(task)
                    ms = ms_since(call.started)
                    try:
                        msg = task.result()
                    except Exception as exc:  # the SDK's APIStatusError carries .status_code
                        status = getattr(exc, "status_code", None)
                        self._log("llm_failed", request_id=req.request_id, reason=type(exc).__name__,
                                  status=status, attempt=call.attempt, call=call.name, ms=ms)
                        # A TypeError is an SDK too old for these parameters: a retry cannot fix it.
                        if status in PERMANENT_STATUS or isinstance(exc, TypeError):
                            return None
                        # Connection errors, 429, 5xx: retry if time allows.
                        if not retried and loop.time() < deadline:
                            retried = True
                            start("retry", [])
                        continue
                    options, problems, shortened = self.parse(msg, req)
                    usage = getattr(msg, "usage", None)
                    tokens = {"input_tokens": getattr(usage, "input_tokens", None),
                              "output_tokens": getattr(usage, "output_tokens", None)}
                    if options is not None:
                        self._log("llm_ok", request_id=req.request_id, attempt=call.attempt,
                                  call=call.name, ms=ms, page_ms=ms_since(page_started),
                                  options=[o.utterance for o in options], dropped=problems,
                                  labels_shortened=shortened,
                                  cancelled=[c.name for c in calls.values()], **tokens)
                        return options
                    self._log("llm_invalid", request_id=req.request_id, attempt=call.attempt,
                              call=call.name, ms=ms, problems=problems, **tokens)
                    if not retried and loop.time() < deadline:
                        retried = True
                        start("retry", problems)  # the problems named, so it can fix them
                if hedge_at is not None and loop.time() >= hedge_at:
                    hedge_at = None
                    if first in calls:  # the first call has not answered: send the same one again
                        start("hedge", [])
                        self._log("llm_hedge", request_id=req.request_id, attempt=attempts,
                                  after_ms=ms_since(page_started))
                if calls and loop.time() >= deadline:
                    running = sorted(calls.values(), key=lambda c: c.attempt)
                    self._log("llm_failed", request_id=req.request_id, reason="timeout",
                              attempt=running[-1].attempt, calls=[c.name for c in running],
                              ms=ms_since(page_started))
                    return None
            return None
        finally:
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, return_exceptions=True)

    def params(self, req: PageRequest, feedback: list[str] | None = None) -> dict[str, Any]:
        """The Messages API request for one page; ``feedback`` names what a rejected answer broke."""
        content = user_message(req, first_name(self.profile))
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
        """The page from the response, the problems found, and the labels shortened.

        Each candidate (``option_1`` .. ``option_N``, N = option_count + spare_options) is checked
        on its own; the page is the first ``option_count`` usable ones in rank order. A candidate
        is dropped if it breaks a length or intent rule, repeats an earlier kept candidate's
        sentence, or repeats an option already shown. Duplicates are judged on the sentence (the
        big text on the user's screen): a candidate whose (shortened) label matches an earlier one
        gives way to a spare with its own label, but is kept when no such spare is left. With too
        few usable candidates the page is None and the problems are what the retry is told;
        otherwise the problems name the candidates dropped, and the label clashes kept (logged).
        Only the label is ever shortened, never the utterance.
        """
        if getattr(msg, "stop_reason", None) == "refusal":
            return None, ["refused"], []
        block = next((b for b in getattr(msg, "content", []) or []
                      if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == TOOL_NAME),
                     None)
        if block is None or not isinstance(block.input, dict):
            return None, [f"no {TOOL_NAME} call (stop_reason {getattr(msg, 'stop_reason', None)})"], []
        problems: list[str] = []
        shortened: list[str] = []
        options: list[tuple[int, Option]] = []  # (rank, option), each with its own label
        clashing: list[tuple[int, Option, str, str]] = []  # a label already kept: (rank, option,
        # note, shortened note), kept only if no spare with its own label fills the page
        known = set(self.intents.intent_ids)
        shown = {normalized(u) for u in req.shown}
        kept_utterances: dict[str, str] = {}  # normalized utterance -> the rank key that has it
        kept_labels: dict[str, str] = {}
        for rank, key in enumerate(rank_keys(self.menus.option_count + self.menus.spare_options)):
            if len(options) == self.menus.option_count:
                break  # the page is full: later spares are not needed
            try:
                option = Option.model_validate(block.input.get(key))
            except ValidationError as exc:
                problems.append(f"{key}: {exc.errors()[0].get('msg')}")
                continue
            written = option.label.strip()
            label = shorten_label(written, self.menus.label_max_words, self.menus.label_max_chars)
            option = Option(label=label, utterance=option.utterance.strip(),
                            intent_id=option.intent_id.strip())
            found = validate_option(option, self.menus.label_max_chars,
                                    self.menus.utterance_max_words, self.menus.label_max_words)
            if option.intent_id not in known:
                found.append(f"unknown intent {option.intent_id!r}")
            utterance, short = normalized(option.utterance), normalized(label)
            if utterance in shown:
                found.append(f"repeats an option already shown ({option.utterance!r})")
            elif utterance in kept_utterances:
                found.append(f"duplicate of {kept_utterances[utterance]}")
            if found:
                problems += [f"{key}: {p}" for p in found]
                continue
            short_note = f"{written!r} -> {label!r}" if label != written else ""
            kept_utterances[utterance] = key
            if short in kept_labels:
                # A different sentence with the same label: a spare with its own label may take
                # its place; if none does, it is kept below (the sentence is what the user reads).
                clashing.append((rank, option, f"{key}: same label as {kept_labels[short]} "
                                               f"({label!r})", short_note))
                continue
            kept_labels[short] = key
            options.append((rank, option))
            shortened += [short_note] if short_note else []
        missing = self.menus.option_count - len(options)
        for rank, option, note, short_note in clashing:
            if missing > 0:
                options.append((rank, option))
                problems.append(f"{note}, kept: no spare with its own label")
                shortened += [short_note] if short_note else []
                missing -= 1
            else:
                problems.append(f"{note}, dropped for a spare")
        if missing > 0:
            return None, problems, shortened
        return [option for _, option in sorted(options, key=lambda ro: ro[0])], problems, shortened

    def _log(self, event: str, **fields: object) -> None:
        if self.log is not None:
            self.log.log(event, **fields)


# ---------------------------------------------------------------------------
# Prompt and tool
# ---------------------------------------------------------------------------


# Words a shortened label should not end on ("Walk to the" -> "Walk").
DANGLING = {"a", "an", "the", "to", "by", "of", "for", "at", "in", "on", "with", "and", "or", "but",
            "too", "my", "your", "our", "is", "are", "be", "so", "if", "i", "about", "how", "what",
            "that"}


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


def normalized(text: str) -> str:
    """``text`` for comparing options: casefolded, without punctuation, single spaces, so
    "Yes, please." and "yes  please" are the same option."""
    return " ".join(re.sub(r"[^\w\s]", "", text.casefold()).split())


def rank_keys(count: int) -> list[str]:
    return [f"option_{i}" for i in range(1, count + 1)]


def reply_tool(intents: IntentsConfig, menus: MenusConfig) -> dict[str, Any]:
    """The strict ``propose_replies`` tool: one field per rank, each a label, sentence and intent.
    It asks for ``option_count + spare_options`` candidates, all required; the spares rank last."""
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
    keys = rank_keys(menus.option_count + menus.spare_options)
    description = (f"Give the {len(keys)} replies the user is most likely to want to say next, "
                   "ranked: option_1 is the most likely.")
    if menus.spare_options:
        spares = keys[menus.option_count:]
        description += (f" {' and '.join(spares)} {'is a' if len(spares) == 1 else 'are'} "
                        f"lower-ranked alternative{'s' if len(spares) > 1 else ''}, shown only if "
                        "one above cannot be used. All replies are different from each other and "
                        "from the options already shown.")
    return {
        "name": TOOL_NAME,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {key: option for key in keys},
            "required": keys,
            "additionalProperties": False,
        },
    }


# The personality map, as instructions (app/config.py Personality).
FORMALITY = {"casual": "casual and relaxed, like talking to a friend",
             "neutral": "plain, everyday language",
             "formal": "polite and a little formal"}
LENGTH = {"brief": "usually 3 to 6 words", "medium": "usually 5 to 10 words"}
CHATTY = ("a little chatty, up to {max} words; the extra words are friendly ones (\"thanks for "
          "asking\", a question back), never extra details")
HUMOUR = {"none": "no jokes",
          "light": "a light touch of humour now and then, never in serious moments",
          "playful": "playful and teasing when the moment allows, never in serious moments"}
WARMTH = {"reserved": "reserved; does not gush", "warm": "warm and friendly"}
DIRECTNESS = {"gentle": 'softens a no ("Not right now, thanks.")', "direct": 'says no plainly ("No.")'}


# The weekend answers (good / bad / so-so) in each voice: the model sees how this person sounds
# without being given a fact to repeat. Keyed by (formality, humour).
VOICE = {
    ("casual", "none"): ("Pretty good, thanks.", "Not great, honestly.", "It was alright."),
    ("casual", "light"): ("Pretty good, thanks!", "Not great, honestly.", "Alright. No complaints!"),
    ("casual", "playful"): ("Honestly? Pretty awesome.", "Rough one, not gonna lie.",
                            "Meh. Could've been worse!"),
    ("neutral", "none"): ("It was good, thanks.", "Not great, honestly.", "It was okay."),
    ("neutral", "light"): ("It was good, thanks!", "Not great, honestly.", "Okay. Nothing exciting!"),
    ("neutral", "playful"): ("It was great, thanks!", "Not my best one, honestly.",
                             "Fine. Nothing to write home about!"),
    ("formal", "none"): ("It was very pleasant, thank you.", "Not particularly good, I'm afraid.",
                         "Quite ordinary, thank you."),
    ("formal", "light"): ("Very pleasant, thank you.", "Not the best, I'm afraid.",
                          "Quite ordinary, though I can't complain."),
    ("formal", "playful"): ("Delightful, thank you for asking.", "Rather dreadful, I'm afraid.",
                            "Uneventful, which suits me perfectly."),
}
DIRECT_NO = {"casual": "Not good.", "neutral": "It wasn't good.", "formal": "It was not good."}
ASK_BACK = {"casual": "How about you?", "neutral": "How was yours?", "formal": "And yours?"}


def voice_sample(profile: Profile) -> tuple[str, str, str]:
    """"How was your weekend?" answered in this person's voice: good, bad and in between."""
    p = profile.personality
    good, bad, middle = VOICE[(p.formality, p.humour)]
    if p.directness == "direct" and p.humour == "none":  # a playful voice keeps its own "no"
        bad = DIRECT_NO[p.formality]
    if p.warmth == "warm":
        middle = f"{middle} {ASK_BACK[p.formality]}"
    if p.phrases:
        good = f"{good.rstrip('.!')}, {p.phrases[0]}." if p.length != "brief" else good
    return good, bad, middle


def first_name(profile: Profile) -> str:
    return profile.name.split()[0]


def style_lines(profile: Profile, max_words: int) -> list[str]:
    """How the user talks, from the personality map: tone and length only, never facts."""
    p, name = profile.personality, first_name(profile)
    length = LENGTH.get(p.length) or CHATTY.format(max=max_words)
    lines = [f"- Tone: {FORMALITY[p.formality]}; {WARMTH[p.warmth]}; {HUMOUR[p.humour]}.",
             f"- Length: {length} (never more than {max_words}).",
             f"- Saying no: {DIRECTNESS[p.directness]}."]
    if p.phrases:
        lines.append("- Phrases " + name + " uses a lot: " + "; ".join(f'"{x}"' for x in p.phrases)
                     + ". Use at most one of them per page, and only where its meaning fits (a "
                     "word that means yes only in a yes); never in place of an answer.")
    if p.avoid_phrases:
        lines.append("- Words and phrases " + name + " never uses: "
                     + "; ".join(f'"{x}"' for x in p.avoid_phrases) + ".")
    if p.notes:
        lines.append(f"- In {name}'s own words: {p.notes}")
    return lines


def facts_lines(profile: Profile) -> list[str]:
    """What is known about the user; used only when the partner asks about it."""
    lines = []
    if profile.about:
        lines.append(f"- About: {profile.about}")
    for title, items in (("Likes", profile.likes), ("Dislikes", profile.dislikes),
                         ("Routines (habits, not things that happened)", profile.routines)):
        if items:
            lines.append(f"- {title}: {'; '.join(items)}.")
    if profile.people:
        people = ", ".join(f"{p.name} ({p.relation})" if p.relation else p.name for p in profile.people)
        lines.append(f"- People: {people}.")
    return lines or ["- (nothing yet)"]


def system_prompt(profile: Profile, intents: IntentsConfig, menus: MenusConfig) -> str:
    """Static instructions: the rules, the user's profile and the intents (the same for every call)."""
    name, n = first_name(profile), menus.option_count
    intent_lines = "\n".join(f"- {i.id}: {i.description}" for i in intents.intents)
    persona = " (a fictional demo persona)" if profile.fictional else ""
    voice = voice_sample(profile)
    avoid = (f"\nNever bring up: {'; '.join(profile.avoid_topics)}. If the partner raises one, keep "
             "the replies short and neutral.\n" if profile.avoid_topics else "")
    spares = rank_keys(n + menus.spare_options)[n:]
    spare = (f" After those {n}, write {len(spares)} spare "
             f"option{'s' if len(spares) > 1 else ''} ({', '.join(spares)}), ranked lower: {name} "
             f"sees a spare only when one of the first {n} cannot be used. Every option is "
             "different from every other one and from the options already shown." if spares else "")
    return f"""\
You write reply options for {name}, who talks through a brain-computer interface. {name} cannot \
speak or type: they choose one of {n} short replies by looking at it, then confirm it, and the \
laptop says the sentence aloud. Choosing takes several seconds, so the right reply should be among \
the first {n} you write, and most often it should be option_1.{spare}

{name} says these sentences aloud to a real person, so any detail you make up becomes a false \
statement in {name}'s voice. So keep what the replies say basic, and let how they say it sound \
like {name}.

What to say:
- Write each sentence in the first person, as {name} would say it, natural and spoken.
- When the partner asks a question, give the main answers to it, each option a different one: \
positive, negative and in between. None changes the subject. {name} picks the one that is true.
  "Did you sleep well?" -> a yes, a no and a so-so.
  "Do you want some tea?" -> a yes, a no and a later.
  "Do you want to go outside?" -> a yes, a no and a later, with no extra details such as "after \
coffee".
- For an open question ("What should we have for dinner?"), give simple possible answers, not \
details nobody mentioned: something simple, "not sure, any ideas?", or "you choose".
  "Who is coming to visit?" -> not sure / nobody I know of / why, is someone coming? Never say \
that someone is coming, visiting or doing something unless the conversation says so, even \
someone from the profile.
- Do not use a like or habit to explain an answer ("Not great, it's too early") unless the partner \
asked about it.
- When the partner did not ask anything ("Good to see you!"), reply simply and warmly, or ask \
something back.
- For a yes/no question, put the answer {name} is most likely to give first, and include a "no" or \
"not now" answer in option_1 or option_2, so declining is always quick.
- State no facts that are not in the conversation or in {name}'s profile below. Never invent \
names, plans, places, events, feelings or what {name} did, ate or saw.
- Use a profile fact only when the partner asks about exactly that thing ("What music do you \
like?" -> {name}'s likes). Otherwise leave the profile out: do not bring up likes, routines or \
people by yourself. The profile says what {name} usually likes and does, never what happened.
- Greetings and time words match the time given in the message: no "good morning" in the \
afternoon, evening or at night.
- Never give medical advice or make medical claims.
- Each label is a short tag beside its square, at most {menus.label_max_words} words and \
{menus.label_max_chars} characters, so {name} recognises the sentence at a glance: for example \
"Good, thanks", "Not great", "Maybe later". The full sentence goes in the utterance.

How {name} talks (the words of every option; never new facts):
{chr(10).join(style_lines(profile, menus.utterance_max_words))}
- For example, {name} answers "How was your weekend?" like this: "{voice[0]}" / "{voice[1]}" / \
"{voice[2]}". Every option should sound like {name} in the same way: someone reading the three \
options should be able to tell how {name} talks.
- Personality changes the wording, never the content: it never adds a reason, a feeling or a \
detail ("long week", "still waking up", "too early for me", "coffee?") that the conversation did \
not give.

{name}'s profile{persona}, to use only when asked about:
{chr(10).join(facts_lines(profile))}
{avoid}
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
