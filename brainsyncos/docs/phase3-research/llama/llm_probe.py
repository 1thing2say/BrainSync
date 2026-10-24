"""Drive app.menus.MenuMaker against a local llama-server: Anthropic /v1/messages and OpenAI /v1/chat/completions."""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, "/work")
from app.config import load_config, load_intents, load_profile
from app.context import PageRequest, Turn
from app.menus import MenuMaker, TOOL_NAME
C = Path("/work/config")
cfg = load_config(C / "default.yaml")
intents = load_intents(C / "intents.yaml", option_count=cfg.menus.option_count, label_max_chars=cfg.menus.label_max_chars,
                       utterance_max_words=cfg.menus.utterance_max_words, label_max_words=cfg.menus.label_max_words)
profile = load_profile(C / "profile.yaml")
llm = cfg.llm.model_copy(update={"timeout_s": 120.0, "model": "local"})
req = PageRequest(request_id=1, reason="partner", partner_last="Are you hungry? Want some lunch?",
                  history=(Turn("partner", "Are you hungry? Want some lunch?"),), shown=(), time_of_day="Saturday, 12:40 PM (afternoon)")

async def anthropic_path():
    import anthropic
    client = anthropic.AsyncAnthropic(api_key="local", base_url="http://127.0.0.1:8080", max_retries=0, timeout=120)
    mm = MenuMaker(cfg.menus, llm, intents, profile, api_key=None, create=client.messages.create)
    params = mm.params(req)
    print("params keys:", sorted(params), "tool strict:", params["tools"][0].get("strict"), "output_config:", params["output_config"])
    t = time.monotonic()
    try:
        msg = await client.messages.create(**params)
    except Exception as e:
        print("ANTHROPIC raw call failed:", type(e).__name__, getattr(e, "status_code", None), str(e)[:400]); return
    print(f"ANTHROPIC raw ok in {time.monotonic()-t:.1f}s stop_reason={msg.stop_reason}")
    for b in msg.content:
        print("  block", b.type, getattr(b, "name", None), json.dumps(getattr(b, "input", getattr(b, "text", None)))[:600])
    opts, problems, short = mm.parse(msg, req)
    print("  parse ->", [o.utterance for o in opts] if opts else None, problems, short)
    t = time.monotonic(); out = await mm.make_page(req); print(f"  make_page -> {[o.model_dump() for o in out] if out else None} ({time.monotonic()-t:.1f}s)")

async def openai_path():
    import openai
    client = openai.AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:8080/v1", max_retries=0, timeout=120)
    mm = MenuMaker(cfg.menus, llm, intents, profile, api_key=None, create=lambda **k: None)
    p = mm.params(req)
    tool = p["tools"][0]
    t = time.monotonic()
    try:
        r = await client.chat.completions.create(model="local", max_tokens=p["max_tokens"],
            messages=[{"role": "system", "content": p["system"]}] + p["messages"],
            tools=[{"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                     "parameters": tool["input_schema"], "strict": True}}],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}})
    except Exception as e:
        print("OPENAI tools call failed:", type(e).__name__, getattr(e, "status_code", None), str(e)[:400]); return
    m = r.choices[0]
    print(f"OPENAI tools ok in {time.monotonic()-t:.1f}s finish={m.finish_reason}")
    for tc in (m.message.tool_calls or []):
        print("  tool_call", tc.function.name, tc.function.arguments[:600])
    t = time.monotonic()
    r = await client.chat.completions.create(model="local", max_tokens=p["max_tokens"],
        messages=[{"role": "system", "content": p["system"]}] + p["messages"],
        response_format={"type": "json_schema", "json_schema": {"name": TOOL_NAME, "strict": True, "schema": tool["input_schema"]}})
    print(f"OPENAI json_schema ok in {time.monotonic()-t:.1f}s:", r.choices[0].message.content[:600])

asyncio.run(anthropic_path()); asyncio.run(openai_path())
