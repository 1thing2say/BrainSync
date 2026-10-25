"""app/menus.py with several LLM backends: a local llama-server first, then Claude, then the fixed
lists. Fakes only, plus a tiny HTTP server that speaks /v1/messages the way llama-server does;
nothing here reaches the internet."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import LLMConfig, load_config, load_intents, load_profile
from app.log import EventLog, read_events
from app.menus import TOOL_NAME, MenuMaker, PageResult, rank_keys
from tests.test_menus import GOOD, SPARE, APIStatusError, FakeClaude, reply, request

CFG = load_config()
INTENTS = load_intents()
PROFILE = load_profile()

LOCAL_GOOD = {
    "option_1": {"label": "Sure", "utterance": "Sure, let's go outside.", "intent_id": "yes"},
    "option_2": {"label": "Not now", "utterance": "Not right now.", "intent_id": "later"},
    "option_3": {"label": "Maybe", "utterance": "Maybe in a bit.", "intent_id": "maybe"},
    "option_4": SPARE,
}
BAD = {**GOOD, "option_1": {**GOOD["option_1"], "intent_id": "teleport"},
       "option_2": {**GOOD["option_2"], "utterance": "word " * 15}}  # two bad, one spare: too few


def llm(local: dict | None = None, claude: dict | None = None) -> LLMConfig:
    """The default backends with some fields changed (short budgets for tests)."""
    changes = {"local": local or {}, "claude": claude or {}}
    return CFG.llm.model_copy(update={"backends": [
        b.model_copy(update=changes[b.name]) for b in CFG.llm.backends]})


@pytest.fixture
def event_log(tmp_path):
    log = EventLog(tmp_path)
    yield log
    log.close()


def events(event_log, prefix: str = "llm_") -> list[dict]:
    return [e for e in read_events(event_log.path) if e["event"].startswith(prefix)]


def both(local, claude, event_log=None, local_timeout: float = 0.3, claude_timeout: float = 0.5,
         **changes) -> MenuMaker:
    """Both backends on, each with its own fake ``messages.create``."""
    return MenuMaker(CFG.menus, llm({"timeout_s": local_timeout, **changes},
                                    {"timeout_s": claude_timeout, "hedge_after_s": 0}),
                     INTENTS, PROFILE, log=event_log, create={"local": local, "claude": claude})


# ---------------------------------------------------------------------------
# Order, hand-over and fallback
# ---------------------------------------------------------------------------


async def test_the_local_backend_answers_and_claude_is_not_asked(event_log):
    local, claude = FakeClaude(reply(LOCAL_GOOD)), FakeClaude(reply(GOOD))
    result = await both(local, claude, event_log).page(request())
    assert result == PageResult(result.options, "local", "")
    assert [o.utterance for o in result.options] == [LOCAL_GOOD[k]["utterance"] for k in rank_keys(3)]
    assert claude.calls == []
    (params,) = local.calls
    assert params["model"] == "qwen3.5-2b"
    assert params["tool_choice"] == {"type": "tool", "name": TOOL_NAME}  # llama-server: "required"
    assert params["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "output_config" not in params  # llama-server ignores it; only Claude gets the effort
    ok = events(event_log)[-1]
    assert ok["event"] == "llm_ok" and ok["backend"] == "local"


async def test_claude_s_request_is_unchanged_by_the_new_backends():
    claude = FakeClaude(reply(GOOD))
    m = MenuMaker(CFG.menus, CFG.llm, INTENTS, PROFILE, create=claude)  # local has no key: off
    assert [b.name for b in m.active] == ["claude"]
    assert (await m.page(request())).backend == "claude"
    (params,) = claude.calls
    assert set(params) == {"model", "max_tokens", "system", "tools", "tool_choice", "messages",
                           "output_config"}
    assert params["output_config"] == {"effort": "low"} and params["tools"][0]["strict"] is True


async def test_local_down_hands_over_to_claude_in_milliseconds(event_log):
    local, claude = FakeClaude(ConnectionRefusedError(111, "Connection refused")), FakeClaude(reply(GOOD))
    started = time.monotonic()
    result = await both(local, claude, event_log, local_timeout=5.0).page(request())
    assert result.backend == "claude" and result.options is not None
    assert time.monotonic() - started < 0.2  # the local budget (5 s) is not spent
    assert len(local.calls) == 1  # no retry against a server that is not there
    failed = [e for e in events(event_log) if e["event"] == "llm_failed"]
    assert len(failed) == 1 and failed[0]["backend"] == "local" and failed[0]["next"] == "claude"
    assert failed[0]["reason"] == "ConnectionRefusedError"


async def test_an_sdk_connection_error_also_hands_over_at_once():
    class APIConnectionError(Exception):  # the SDK's name, without importing the slow SDK
        pass

    local, claude = FakeClaude(APIConnectionError("Connection error.")), FakeClaude(reply(GOOD))
    assert (await both(local, claude, local_timeout=5.0).page(request())).backend == "claude"
    assert len(local.calls) == 1


async def test_the_last_backend_still_retries_a_connection_error():
    claude = FakeClaude(ConnectionError("offline"), reply(GOOD))
    m = MenuMaker(CFG.menus, CFG.llm, INTENTS, PROFILE, create=claude)  # Claude alone, as before
    assert await m.make_page(request()) is not None and len(claude.calls) == 2


async def test_local_slow_past_its_budget_then_claude_gets_its_full_time(event_log):
    local = FakeClaude(reply(LOCAL_GOOD), delay=5.0)
    claude = FakeClaude(reply(GOOD), delay=0.35)  # longer than what local left of 0.5 s
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await both(local, claude, event_log, local_timeout=0.3, claude_timeout=0.5).page(request())
    took = loop.time() - started
    assert result.backend == "claude" and result.options is not None
    assert 0.6 <= took < 1.0  # 0.3 s local, then 0.35 s of Claude's own 0.5 s
    timeout = next(e for e in events(event_log) if e["event"] == "llm_failed")
    assert timeout["reason"] == "timeout" and timeout["backend"] == "local"
    assert timeout["next"] == "claude"
    ok = events(event_log)[-1]
    assert ok["backend"] == "claude" and 300 <= ok["ms"] < 500 and ok["page_ms"] >= 600


async def test_local_invalid_output_is_retried_then_claude_answers(event_log):
    local, claude = FakeClaude(reply(BAD)), FakeClaude(reply(GOOD))
    result = await both(local, claude, event_log, local_timeout=2.0).page(request())
    assert result.backend == "claude"
    assert len(local.calls) == 2  # one retry with the problems named, as for Claude
    assert "could not be used" in local.calls[1]["messages"][0]["content"]
    invalid = [e for e in events(event_log) if e["event"] == "llm_invalid"]
    assert [e["backend"] for e in invalid] == ["local", "local"]
    assert "next" not in invalid[0] and invalid[1]["next"] == "claude"


async def test_local_without_a_tool_call_falls_through_to_claude():
    # A small model may answer in prose: no propose_replies call is invalid, never trusted.
    local, claude = FakeClaude(reply(None, stop_reason="end_turn")), FakeClaude(reply(GOOD))
    assert (await both(local, claude, local_timeout=2.0).page(request())).backend == "claude"


async def test_a_permanent_error_from_local_hands_over_instead_of_giving_up(event_log):
    local, claude = FakeClaude(APIStatusError(401)), FakeClaude(reply(GOOD))
    result = await both(local, claude, event_log, local_timeout=2.0).page(request())
    assert result.backend == "claude" and len(local.calls) == 1
    failed = events(event_log)[1]
    assert failed["status"] == 401 and failed["next"] == "claude"


async def test_both_failing_gives_the_fixed_list_and_says_why(event_log):
    local = FakeClaude(reply(LOCAL_GOOD), delay=5.0)
    claude = FakeClaude(APIStatusError(529))
    started = time.monotonic()
    result = await both(local, claude, event_log, local_timeout=0.2, claude_timeout=0.3).page(request())
    assert result == PageResult(None, "", "local: timeout; claude: HTTP 529")
    assert time.monotonic() - started < 0.8  # within the sum of the budgets
    final = [e for e in events(event_log) if e["event"] == "llm_failed"][-1]
    assert final["backend"] == "claude" and "next" not in final  # the page's own end


async def test_done_gets_the_backend_or_the_reasons():
    got = []
    m = both(FakeClaude(ConnectionRefusedError()), FakeClaude(APIStatusError(401)))
    m.request(request(request_id=5), lambda rid, options, **info: got.append((rid, options, info)))
    await asyncio.sleep(0.1)
    assert got == [(5, None, {"backend": "",
                              "error": "local: ConnectionRefusedError; claude: HTTP 401"})]
    await m.stop()


async def test_each_backend_hedges_with_its_own_setting(event_log):
    # local: no hedge (one slot); claude: hedges after its own hedge_after_s.
    local = FakeClaude(reply(LOCAL_GOOD), delay=5.0)
    claude = FakeClaude(reply(GOOD), delay=0.25)
    m = MenuMaker(CFG.menus, llm({"timeout_s": 0.3}, {"timeout_s": 0.6, "hedge_after_s": 0.1}),
                  INTENTS, PROFILE, log=event_log, create={"local": local, "claude": claude})
    assert (await m.page(request())).backend == "claude"
    assert len(local.calls) == 1 and len(claude.calls) == 2
    hedges = [e for e in events(event_log) if e["event"] == "llm_hedge"]
    assert [e["backend"] for e in hedges] == ["claude"]
    assert m.busy == 0


# ---------------------------------------------------------------------------
# Keys, status and the page budget
# ---------------------------------------------------------------------------


def test_a_backend_without_its_key_is_off_and_the_page_budget_is_the_active_ones():
    claude = FakeClaude(reply(GOOD))
    laptop = MenuMaker(CFG.menus, CFG.llm, INTENTS, PROFILE, create=claude)
    assert [b.name for b in laptop.active] == ["claude"]
    assert laptop.page_timeout_s == 4.0  # as before: nothing waits for a local server
    local = next(d for d in laptop.describe() if d["name"] == "local")
    assert not local["enabled"] and local["why_disabled"] == "BRAINSYNC_LLAMA_API_KEY is not set"
    jetson = both(FakeClaude(reply(LOCAL_GOOD)), claude, local_timeout=10.0, claude_timeout=4.0)
    assert jetson.page_timeout_s == 14.0 and jetson.enabled


async def test_describe_never_carries_a_key():
    m = MenuMaker(CFG.menus, CFG.llm, INTENTS, PROFILE, keys={"BRAINSYNC_LLAMA_API_KEY": "s3cret-key"})
    try:
        assert "s3cret-key" not in json.dumps(m.describe())
        assert [d["enabled"] for d in m.describe()] == [True, False]
        assert m.why_disabled == ""  # local alone writes the pages
    finally:
        await m.stop()


async def test_keep_alive_runs_for_claude_only():
    class Warm:
        calls = 0

        async def __call__(self):
            Warm.calls += 1

    local_warm = Warm()
    m = MenuMaker(CFG.menus, llm(claude={"keepalive_s": 0.05}), INTENTS, PROFILE,
                  create={"local": FakeClaude(reply(LOCAL_GOOD)), "claude": FakeClaude(reply(GOOD))},
                  warm={"local": local_warm, "claude": Warm()})
    await m.start()
    await asyncio.sleep(0.12)
    await m.stop()
    assert Warm.calls >= 2  # Claude's keep-alive ran
    assert m.warm_ok is True and [d["keepalive_running"] for d in m.describe()] == [False, False]


async def test_preview_uses_the_same_backends():
    local, claude = FakeClaude(reply(LOCAL_GOOD)), FakeClaude(reply(GOOD))
    m = both(local, claude)
    options = await m.preview(PROFILE, "Do you want to go outside?", time.time())
    assert options[0].utterance == LOCAL_GOOD["option_1"]["utterance"] and not claude.calls


# ---------------------------------------------------------------------------
# The real SDK against a stand-in llama-server
# ---------------------------------------------------------------------------


class FakeLlamaServer:
    """Speaks POST /v1/messages the way llama-server does (tools/server/server-chat.cpp): the key
    from --api-key-file as "Authorization: Bearer", ``strict`` and ``output_config`` dropped, the
    forced tool_choice turned into "required", and an Anthropic-shaped tool_use reply."""

    KEY = "local-test-key"

    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                        "body": body})
                if self.headers.get("Authorization") != f"Bearer {server.KEY}":
                    return self._send(401, {"error": {"code": 401, "message": "Invalid API Key",
                                                      "type": "authentication_error"}})
                # What llama-server keeps: tools as functions (no strict), tool_choice "required".
                assert body["tool_choice"]["type"] in ("tool", "any")
                self._send(200, {
                    "id": "msg_local", "type": "message", "role": "assistant",
                    "model": body["model"], "stop_reason": "tool_use", "stop_sequence": None,
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": TOOL_NAME,
                                 "input": server.answer}],
                    "usage": {"input_tokens": 1200, "output_tokens": 160}})

            def _send(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def llama():
    server = FakeLlamaServer(LOCAL_GOOD)
    yield server
    server.close()


def free_port() -> int:
    """A port nothing listens on (bound, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_the_sdk_talks_to_a_llama_server(llama, event_log):
    claude = FakeClaude(reply(GOOD))
    m = MenuMaker(CFG.menus, llm({"base_url": llama.url, "timeout_s": 5.0}), INTENTS, PROFILE,
                  log=event_log, keys={"BRAINSYNC_LLAMA_API_KEY": FakeLlamaServer.KEY},
                  create={"claude": claude})
    try:
        result = await m.page(request())
    finally:
        await m.stop()
    assert result.backend == "local" and not claude.calls
    assert result.options[0].utterance == LOCAL_GOOD["option_1"]["utterance"]
    (sent,) = llama.requests
    assert sent["path"] == "/v1/messages" and sent["auth"] == f"Bearer {FakeLlamaServer.KEY}"
    body = sent["body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}  # extra_body, top level
    assert body["model"] == "qwen3.5-2b" and "output_config" not in body
    assert body["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    ok = events(event_log)[-1]
    assert ok["backend"] == "local" and ok["input_tokens"] == 1200
    assert FakeLlamaServer.KEY not in event_log.path.read_text()  # keys are never logged


async def test_a_wrong_local_key_hands_over_to_claude(llama):
    claude = FakeClaude(reply(GOOD))
    m = MenuMaker(CFG.menus, llm({"base_url": llama.url, "timeout_s": 5.0}), INTENTS, PROFILE,
                  keys={"BRAINSYNC_LLAMA_API_KEY": "wrong"}, create={"claude": claude})
    try:
        result = await m.page(request())
    finally:
        await m.stop()
    assert result.backend == "claude" and result.error == "" and len(llama.requests) == 1


async def test_the_sdk_fails_fast_when_no_llama_server_listens(event_log):
    claude = FakeClaude(reply(GOOD))
    m = MenuMaker(CFG.menus, llm({"base_url": f"http://127.0.0.1:{free_port()}", "timeout_s": 10.0}),
                  INTENTS, PROFILE, log=event_log,
                  keys={"BRAINSYNC_LLAMA_API_KEY": "k"}, create={"claude": claude})
    started = time.monotonic()
    try:
        result = await m.page(request())
    finally:
        await m.stop()
    assert result.backend == "claude"
    assert time.monotonic() - started < 1.0  # milliseconds, not the 10 s local budget
    failed = next(e for e in events(event_log) if e["event"] == "llm_failed")
    assert failed["reason"] == "APIConnectionError" and failed["next"] == "claude"
    assert failed["ms"] < 1000
