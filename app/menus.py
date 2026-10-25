"""LLM option ranking: one page of reply options per request, from the first LLM backend that
answers in time, validated, with the fixed lists as fallback (PLAN.md V3, items 2-4).

``MenuMaker.request(page_request, done)`` starts the page in the background and later calls
``done(request_id, options, backend=..., error=...)`` with exactly ``menus.option_count`` options
ranked most likely first and the name of the backend that wrote them, or ``None`` and why each
backend failed (the orchestrator then keeps the fixed list from config/intents.yaml). The LLM only
writes options: it never picks or changes a selection (rule 1), and an option is spoken exactly as
shown (rule 2).

The backends (``llm.backends``) are tried in order: on BrainSyncOS a local llama-server first, then
Claude. A backend whose key (``api_key_env``) is not set is off, so a laptop without llama-server
behaves as before. Each backend gets its own ``timeout_s`` for its whole turn, starting when its
turn starts; one that fails early (a refused connection, a bad key, an error a retry cannot fix)
hands over at once, so the next one still gets its full time and a page takes at most the sum
(``page_timeout_s``, which the orchestrator waits). A refused connection costs milliseconds, not
the budget: a backend with another one after it is not retried after a connection error.

The call forces one strict tool call, ``propose_replies``, whose input has one field per rank
(``option_1`` .. ``option_4`` with the defaults), so the output is schema-valid JSON with the
ranking explicit. llama-server drops ``strict`` (and ``output_config``) and turns the forced
tool_choice into "required", so nothing here trusts the schema: lengths, intents, duplicates and
repeats of options already shown are checked here, for each candidate on its own, for every
backend. The page is the first ``menus.option_count`` usable candidates in rank order, so one bad
candidate costs a spare (``menus.spare_options``), not the page. Duplicates and repeats are
compared without case, punctuation or extra spaces ("Yes, please." repeats "yes please"). A label
that is too long is shortened rather than rejected: it is only the short tag beside a square, and
the utterance (what is spoken) is never changed.

Within its turn a backend gets at most three calls: the first; a second, identical one (the hedge)
if the first has not answered after its ``hedge_after_s``, since a call stuck on a congested
network rarely recovers in time; and one retry after an answer with too few usable candidates (the
problems named) or a transient error. The first usable answer wins and the other calls are
cancelled. A permanent error (bad request, key, model) ends that backend's turn at once.

Keys come from the environment (``ANTHROPIC_API_KEY``, ``BRAINSYNC_LLAMA_API_KEY``, loaded from the
env file by the server's ``main``) and are never logged.

``start()`` (server lifespan) opens the connection to the Claude API at once and keeps it open with
a cheap call (``models.retrieve``, no tokens) every ``keepalive_s`` while idle, and right after a
failed turn (a call cut off by the time limit closes its connection). A page then skips DNS, TCP
and TLS: three round trips, 1-2 s on a good network and 3-9 s on the congested venue Wi-Fi of
2026-10-24, all of it inside the backend's ``timeout_s``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from pydantic import ValidationError

from app.config import IntentsConfig, LLMBackend, LLMConfig, MenusConfig, Profile
from app.context import Conversation, PageRequest
from app.log import EventLog
from app.schemas import Option, validate_option

TOOL_NAME = "propose_replies"
MAX_TOKENS = 1024  # a few short options; a forced tool call skips thinking on Claude Haiku 5.5
# HTTP statuses a retry cannot fix: bad request, key, permissions, model name.
PERMANENT_STATUS = {400, 401, 403, 404}

Done = Callable[..., None]  # done(request_id, options | None, backend=str, error=str)
Create = Callable[..., Awaitable[Any]]
Warm = Callable[[], Awaitable[Any]]


class Call(NamedTuple):
    """One call of a backend's turn: ``first``, ``hedge`` or ``retry``, numbered in start order."""

    name: str
    attempt: int
    started: float  # time.monotonic()


class PageResult(NamedTuple):
    options: list[Option] | None
    backend: str  # the backend that wrote the page; "" when it fell back
    error: str  # why each backend failed ("local: timeout; claude: APIConnectionError"), or ""


@dataclass
class Backend:
    """One configured backend and its connection state."""

    cfg: LLMBackend
    create: Create | None = None
    warm: Warm | None = None
    why_disabled: str = ""
    client: Any = None
    warm_ok: bool | None = None  # the last keep-alive call answered; None before the first one
    keeper: asyncio.Task[None] | None = None
    rewarm: asyncio.Event = field(default_factory=asyncio.Event)  # set after a failed turn
    busy: int = 0  # page calls in flight (a keep-alive call then would open a second connection)

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def enabled(self) -> bool:
        return self.create is not None


def unreachable(exc: BaseException) -> bool:
    """A connection error (refused, DNS, the SDK's own time limit): the server is not there.
    Checked by class name so the slow SDK need not be imported."""
    names = {c.__name__ for c in type(exc).__mro__}
    return isinstance(exc, OSError) or "APIConnectionError" in names


def failure(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    return f"HTTP {status}" if status else type(exc).__name__


def joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class MenuMaker:
    """Writes pages of reply options with the configured backends. Disabled (``enabled`` False)
    when no backend has its key."""

    def __init__(
        self,
        menus: MenusConfig,
        llm: LLMConfig,
        intents: IntentsConfig,
        profile: Profile,
        api_key: str | None = None,
        log: EventLog | None = None,
        create: Create | Mapping[str, Create] | None = None,
        warm: Warm | Mapping[str, Warm] | None = None,
        *,
        keys: Mapping[str, str] | None = None,
    ) -> None:
        """``keys`` maps each backend's ``api_key_env`` to its key (``main`` passes the
        environment); ``api_key`` is Claude's key (``ANTHROPIC_API_KEY``) when ``keys`` lacks it.

        ``create`` replaces ``AsyncAnthropic().messages.create`` and ``warm`` the keep-alive call
        (tests): a mapping gives one per backend name, and a single callable stands in for the
        backends of kind ``anthropic`` (Claude). A backend with a fake needs no key. Without
        ``warm`` nothing is kept open."""
        self.menus = menus
        self.llm = llm
        self.intents = intents
        self.profile = profile
        self.log = log
        env = dict(keys or {})
        if api_key:
            env.setdefault("ANTHROPIC_API_KEY", api_key)

        def pick(given: Any, b: LLMBackend) -> Any:
            if isinstance(given, Mapping):
                return given.get(b.name)
            return given if b.kind == "anthropic" else None

        self.backends: list[Backend] = []
        for cfg in llm.backends:
            b = Backend(cfg, create=pick(create, cfg), warm=pick(warm, cfg))
            if b.create is None:
                key = env.get(cfg.api_key_env)
                if key:
                    self._connect(b, key)
                else:
                    b.why_disabled = f"{cfg.api_key_env} is not set"
            self.backends.append(b)
        self.system = system_prompt(profile, intents, menus)
        self.tool = reply_tool(intents, menus)
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _connect(b: Backend, key: str) -> None:
        # Imported only here: the SDK has thousands of files, and from a venv on /mnt/c (WSL)
        # importing it takes about 2 minutes, which tests and keyless runs should not pay.
        import anthropic
        import httpx2  # the SDK's HTTP library, installed with it

        cfg = b.cfg
        # httpx closes a connection after 5 s idle; keep it open between keep-alive calls.
        keep = cfg.keepalive_s
        http = anthropic.DefaultAsyncHttpxClient(limits=httpx2.Limits(
            max_connections=10, max_keepalive_connections=2,
            keepalive_expiry=2 * keep if keep else 5.0))
        # Our own retry and time limit apply, so the SDK's are turned off. The SDK sends the key
        # as "Authorization: Bearer", which llama-server's --api-key-file checks too.
        extra = {"base_url": cfg.base_url} if cfg.base_url else {}
        client = anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=cfg.timeout_s,
                                          http_client=http, **extra)
        b.client = client
        b.create = client.messages.create
        if cfg.kind == "anthropic":
            # More time than a page gets: on a slow network the connection still opens, for the next.
            warm_timeout = max(keep, cfg.timeout_s)
            b.warm = lambda: client.models.retrieve(cfg.model, timeout=warm_timeout)

    # -- state ----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return not self.why_disabled

    @property
    def active(self) -> list[Backend]:
        """The backends that write pages, in order (those with a key or a fake)."""
        return [b for b in self.backends if b.enabled]

    @property
    def why_disabled(self) -> str:
        """Empty when an LLM writes the options; otherwise why only the fixed lists are used."""
        if not self.menus.dynamic:
            return "menus.dynamic is false"
        if not self.active:
            missing = [b.cfg.api_key_env for b in self.backends]
            return f"{joined(missing)} {'is' if len(missing) == 1 else 'are'} not set"
        return ""

    @property
    def page_timeout_s(self) -> float:
        """The longest a page can take: the active backends' turns one after the other."""
        return sum(b.cfg.timeout_s for b in self.active) or self.llm.page_timeout_s

    def _warmable(self) -> list[Backend]:
        return [b for b in self.active if b.warm is not None and b.cfg.keepalive_s > 0]

    @property
    def warm_ok(self) -> bool | None:
        """Whether the first kept-open backend's last keep-alive call answered; None before the
        first one (or keep-alive off)."""
        warm = self._warmable()
        return warm[0].warm_ok if warm else None

    @property
    def busy(self) -> int:
        """Backend turns in flight (0 once every page has finished)."""
        return sum(b.busy for b in self.backends)

    @property
    def keepalive_running(self) -> bool:
        return any(b.keeper is not None for b in self.backends)

    def describe(self) -> list[dict[str, Any]]:
        """Each backend for /api/health: names, flags and reasons only, never keys."""
        return [{"name": b.name, "kind": b.cfg.kind, "model": b.cfg.model,
                 "base_url": b.cfg.base_url, "timeout_s": b.cfg.timeout_s,
                 "enabled": b.enabled, "why_disabled": b.why_disabled,
                 "keepalive_s": b.cfg.keepalive_s if b.warm is not None else 0.0,
                 "warm_ok": b.warm_ok, "keepalive_running": b.keeper is not None}
                for b in self.backends]

    def set_profile(self, profile: Profile) -> None:
        """Use ``profile`` from the next page on (the profile page saved a new one)."""
        self.profile = profile
        self.system = system_prompt(profile, self.intents, self.menus)

    async def preview(self, profile: Profile, partner_text: str, now_unix: float) -> list[Option] | None:
        """One page for ``partner_text`` written with ``profile`` (not saved), for the profile page."""
        trial = MenuMaker(self.menus, self.llm, self.intents, profile, log=self.log,
                          create={b.name: b.create for b in self.active})
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
        """Open the Claude connection now and keep it open (server lifespan; needs a running loop)."""
        if not self.enabled:
            return
        for b in self._warmable():
            if b.keeper is None:
                b.keeper = asyncio.get_running_loop().create_task(self._keep_warm(b))

    async def stop(self) -> None:
        keepers = [b.keeper for b in self.backends if b.keeper is not None]
        tasks = list(self._tasks) + keepers
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for b in self.backends:
            b.keeper = None
            if b.client is not None:
                await b.client.close()
                b.client = None

    async def _keep_warm(self, b: Backend) -> None:
        while True:
            # Cleared before the call, so a page that fails during it still asks for a rewarm.
            b.rewarm.clear()
            if not b.busy:
                await self.warm(b)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(b.rewarm.wait(), b.cfg.keepalive_s)

    async def warm(self, b: Backend | None = None) -> bool:
        """One keep-alive call (the first kept-open backend by default). Logs the first result and
        every change, not each call."""
        if b is None:
            warmable = self._warmable() or [x for x in self.active if x.warm is not None]
            if not warmable:
                return False
            b = warmable[0]
        started = time.monotonic()
        fields: dict[str, object] = {}
        try:
            await b.warm()
        except Exception as exc:  # never fatal: the page calls report their own failures
            fields = {"reason": type(exc).__name__, "status": getattr(exc, "status_code", None)}
        ok = not fields
        if ok != b.warm_ok:
            self._log("llm_warm" if ok else "llm_warm_failed", backend=b.name,
                      ms=round((time.monotonic() - started) * 1000), **fields)
        b.warm_ok = ok
        return ok

    async def _run(self, req: PageRequest, done: Done) -> None:
        result = await self.page(req)
        done(req.request_id, result.options, backend=result.backend, error=result.error)

    async def make_page(self, req: PageRequest) -> list[Option] | None:
        """The ranked options for ``req``, or None (fall back to the fixed list)."""
        return (await self.page(req)).options

    async def page(self, req: PageRequest) -> PageResult:
        """Each active backend in turn until one writes a usable page."""
        self._log("llm_request", request_id=req.request_id, reason=req.reason,
                  partner_last=req.partner_last)
        page_started = time.monotonic()
        failures: list[str] = []
        active = self.active
        for i, b in enumerate(active):
            after = active[i + 1].name if i + 1 < len(active) else None
            b.busy += 1
            try:
                options, why = await self._turn(b, req, page_started, after)
            finally:
                b.busy -= 1
            if options is not None:
                return PageResult(options, b.name, "")
            b.rewarm.set()  # a call cut off by the time limit closed its connection
            failures.append(f"{b.name}: {why}")
        return PageResult(None, "", "; ".join(failures))

    async def _turn(self, b: Backend, req: PageRequest, page_started: float,
                    after: str | None) -> tuple[list[Option] | None, str]:
        """Up to three calls (first, hedge, retry) inside ``b``'s ``timeout_s``; the first usable
        answer wins. Returns the options or None and why. ``after``: the backend that comes next
        (logged as ``next`` on the failure that ends this turn). Calls still running when it
        returns are cancelled and awaited."""
        loop = asyncio.get_running_loop()
        cfg = b.cfg
        deadline = loop.time() + cfg.timeout_s
        hedge_s = cfg.hedge_after_s
        hedge_at = loop.time() + hedge_s if 0 < hedge_s < cfg.timeout_s else None
        calls: dict[asyncio.Task[Any], Call] = {}  # the calls still running
        attempts = 0  # calls started (at most 3: first, hedge, retry)
        retried = False  # the one retry is used (after an invalid answer or a transient error)
        why = "no answer"
        handover = {"next": after} if after else {}

        def ms_since(started: float) -> int:
            return round((time.monotonic() - started) * 1000)

        def start(name: str, feedback: list[str]) -> asyncio.Task[Any]:
            nonlocal attempts
            attempts += 1

            # Called inside the task, so an SDK that rejects the parameters (TypeError when the
            # call is made) fails that call like any other error.
            async def send() -> Any:
                return await b.create(**self.params(req, feedback, cfg))

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
                        why = failure(exc)
                        # A TypeError is an SDK too old for these parameters: a retry cannot fix
                        # it. A refused connection with another backend waiting is not retried
                        # (the server is not there): hand over, unless the hedge still runs.
                        permanent = status in PERMANENT_STATUS or isinstance(exc, TypeError)
                        absent = after is not None and unreachable(exc)
                        # Connection errors, 429, 5xx: retry if time allows.
                        retry = (not permanent and not absent and not retried
                                 and loop.time() < deadline)
                        ends = permanent or (not calls and not retry)
                        self._log("llm_failed", request_id=req.request_id, backend=b.name,
                                  reason=type(exc).__name__, status=status, attempt=call.attempt,
                                  call=call.name, ms=ms, **(handover if ends else {}))
                        if permanent:
                            return None, why
                        if retry:
                            retried = True
                            start("retry", [])
                        continue
                    options, problems, shortened = self.parse(msg, req)
                    usage = getattr(msg, "usage", None)
                    tokens = {"input_tokens": getattr(usage, "input_tokens", None),
                              "output_tokens": getattr(usage, "output_tokens", None)}
                    if options is not None:
                        self._log("llm_ok", request_id=req.request_id, backend=b.name,
                                  attempt=call.attempt, call=call.name, ms=ms,
                                  page_ms=ms_since(page_started),
                                  options=[o.utterance for o in options], dropped=problems,
                                  labels_shortened=shortened,
                                  cancelled=[c.name for c in calls.values()], **tokens)
                        return options, ""
                    why = "invalid answer"
                    retry = not retried and loop.time() < deadline
                    self._log("llm_invalid", request_id=req.request_id, backend=b.name,
                              attempt=call.attempt, call=call.name, ms=ms, problems=problems,
                              **tokens, **(handover if not calls and not retry else {}))
                    if retry:
                        retried = True
                        start("retry", problems)  # the problems named, so it can fix them
                if hedge_at is not None and loop.time() >= hedge_at:
                    hedge_at = None
                    if first in calls:  # the first call has not answered: send the same one again
                        start("hedge", [])
                        self._log("llm_hedge", request_id=req.request_id, backend=b.name,
                                  attempt=attempts, after_ms=ms_since(page_started))
                if calls and loop.time() >= deadline:
                    running = sorted(calls.values(), key=lambda c: c.attempt)
                    self._log("llm_failed", request_id=req.request_id, backend=b.name,
                              reason="timeout", attempt=running[-1].attempt,
                              calls=[c.name for c in running], ms=ms_since(page_started),
                              **handover)
                    return None, "timeout"
            return None, why
        finally:
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, return_exceptions=True)

    def params(self, req: PageRequest, feedback: list[str] | None = None,
               backend: LLMBackend | None = None) -> dict[str, Any]:
        """The Messages API request for one page (for ``backend``, the first active one by
        default); ``feedback`` names what a rejected answer broke."""
        if backend is None:
            backend = self.active[0].cfg if self.active else self.llm.backends[-1]
        content = user_message(req, first_name(self.profile))
        if feedback:
            content += ("\n\nYour previous answer could not be used: " + "; ".join(feedback)
                        + f". Call {TOOL_NAME} again with every rule met.")
        params: dict[str, Any] = {
            "model": backend.model,
            "max_tokens": MAX_TOKENS,
            "system": self.system,
            "tools": [self.tool],
            # llama-server turns this into tool_choice "required" (one tool, so the same call)
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content": content}],
        }
        if backend.kind == "anthropic":
            params["output_config"] = {"effort": "low"}  # llama-server ignores it
        if backend.extra_body:
            params["extra_body"] = backend.extra_body  # the SDK merges it into the JSON body
        return params

    # -- validation ---------------------------------------------------------

    def parse(self, msg: Any, req: PageRequest) -> tuple[list[Option] | None, list[str], list[str]]:
        """The page from the response, the problems found, and the labels shortened.

        Each candidate (``option_1`` .. ``option_N``, N = option_count + spare_options) is checked
        on its own; the page is the first ``option_count`` usable ones in rank order. A candidate
        is dropped if it breaks a length or intent rule, repeats an earlier kept candidate's
        sentence or label, or repeats an option already shown. With too few usable candidates the
        page is None and the problems are what the retry is told; otherwise the problems name the
        candidates dropped (logged). Only the label is ever shortened, never the utterance.
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
        options: list[Option] = []
        known = set(self.intents.intent_ids)
        shown = {normalized(u) for u in req.shown}
        kept_utterances: dict[str, str] = {}  # normalized utterance -> the rank key that has it
        kept_labels: dict[str, str] = {}
        for key in rank_keys(self.menus.option_count + self.menus.spare_options):
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
            elif short in kept_labels:
                found.append(f"duplicate label of {kept_labels[short]} ({label!r})")
            if found:
                problems += [f"{key}: {p}" for p in found]
                continue
            if label != written:
                shortened.append(f"{written!r} -> {label!r}")
            kept_utterances[utterance], kept_labels[short] = key, key
            options.append(option)
        if len(options) < self.menus.option_count:
            return None, problems, shortened
        return options, problems, shortened

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
