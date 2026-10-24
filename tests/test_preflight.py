"""tools/preflight.py: every check on fakes (httpx.MockTransport, a fake probe); no network."""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from app.config import load_config, load_intents
from app.tts import CartesiaTTS
from tools import preflight
from tools.preflight import (
    FAIL, PASS, SKIP, WARN, Result, Timing, check_cartesia, check_claude, check_clips,
    check_health, check_keys, check_network, fetch_server, probe_host, redact, report, run_checks,
)

CFG = load_config()
ANT_KEY = "sk-ant-test-not-a-real-key"
CAR_KEY = "sk_car_test_not_a_real_key"
ENV = {"ANTHROPIC_API_KEY": ANT_KEY, "CARTESIA_API_KEY": CAR_KEY}
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xf3" * 100


def health(**over) -> dict:
    """A healthy /api/health answer; ``over`` replaces whole sections or top-level keys."""
    h = {"state": "IDLE", "resume_ready": True,
         "decoder": {"connected": True, "ok": True, "display_ok": True, "board_ok": True,
                     "message": "", "keyboard_mode": False, "fake_decoder": False,
                     "name": "ssvep-fbcca", "fake_flag": False},
         "partner_page": {"connected": True, "mic": "listening", "mic_muted": False,
                          "stt_mode": "always"},
         "llm": {"enabled": True, "why_disabled": "", "dynamic": True, "ok": False,
                 "model": "claude-haiku-5-5",
                 "keepalive_s": 20.0, "warm_ok": True, "keepalive_running": True},
         "tts": {"enabled": True, "why_disabled": "", "provider": "cartesia", "model": "sonic-3.5",
                 "voice_id": "47c38ca4", "format": "mp3"}}
    for key, value in over.items():
        h[key] = {**h[key], **value} if isinstance(value, dict) else value
    return h


def levels(results: list[Result]) -> dict[str, str]:
    return {r.name: r.level for r in results}


class FakeNet:
    """One MockTransport for every host: the APIs, the server and the probe's round trips."""

    def __init__(self, health_body: dict | None = None, claude: int = 200, cartesia=None) -> None:
        self.health = health_body if health_body is not None else health()
        self.claude = claude
        self.cartesia = cartesia or (lambda: httpx.Response(200, content=MP3,
                                                            headers={"content-type": "audio/mpeg"}))
        self.requests: list[httpx.Request] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host == "api.anthropic.com" and req.url.path.startswith("/v1/models/"):
            body = ({"id": req.url.path.rsplit("/", 1)[1]} if self.claude == 200 else
                    {"type": "error", "error": {"type": "not_found_error", "message": "model: x"}})
            return httpx.Response(self.claude, json=body)
        if req.url.host == "api.cartesia.ai" and req.url.path == "/tts/bytes":
            return self.cartesia()
        if req.url.path == "/api/health":
            return httpx.Response(200, json=self.health)
        if req.url.path == "/api/config":
            return httpx.Response(200, json={"keyboard_mode": False, "stt_mode": "always"})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def fast_probe(host: str) -> Timing:
    return Timing(host, dns_ms=5, tcp_ms=30, tls_ms=60, http_ms=[90, 100, 120], status=404)


def ticking(step_s: float):
    """A clock that moves ``step_s`` seconds per call."""
    t = [0.0]

    def clock() -> float:
        t[0] += step_s
        return t[0]
    return clock


# -- keys ------------------------------------------------------------------------------------


@pytest.mark.parametrize("env, want", [
    (ENV, {"ANTHROPIC_API_KEY": PASS, "CARTESIA_API_KEY": PASS}),
    ({}, {"ANTHROPIC_API_KEY": FAIL, "CARTESIA_API_KEY": WARN}),
    ({"ANTHROPIC_API_KEY": ANT_KEY, "CARTESIA_API_KEY": ""}, {"ANTHROPIC_API_KEY": PASS,
                                                             "CARTESIA_API_KEY": WARN}),
])
def test_keys_are_reported_by_name_only(env, want):
    results = check_keys(CFG, env)
    assert levels(results) == want
    assert ANT_KEY not in repr(results) and CAR_KEY not in repr(results)


def test_keys_not_used_by_the_config_are_skipped():
    cfg = CFG.model_copy(update={"menus": CFG.menus.model_copy(update={"dynamic": False}),
                                 "speech": CFG.speech.model_copy(update={"provider": "browser"})})
    assert set(levels(check_keys(cfg, {})).values()) == {SKIP}


# -- network ---------------------------------------------------------------------------------


@pytest.mark.parametrize("timing, want", [
    (Timing("h", 5, 30, 60, [90, 100, 120]), PASS),
    (Timing("h", 5, 900, 1500, [1500, 2500, 4000]), WARN),  # the venue Wi-Fi of 2026-10-24
    (Timing("h", 5, 40, 3000, [100, 120, 130]), WARN),  # slow setup alone
    (Timing("h", error="DNS: gaierror"), FAIL),
    (Timing("h", 5, error="connect: TimeoutError"), FAIL),
])
def test_network_levels(timing, want):
    [result] = check_network(["h"], lambda host: timing)
    assert result.level == want
    if want == PASS:
        assert "TCP 30 ms (~ping)" in result.reason and "90/100/120 ms" in result.reason
    if want == WARN:
        assert result.reason.startswith("congested link") and "hotspot" in result.fix


def test_probe_times_dns_tls_and_round_trips_on_one_connection():
    net = FakeNet()
    t = probe_host("api.anthropic.com", rounds=3, transport=net.transport,
                   resolve=lambda host, port, type: [(0, 0, 0, "", ("192.0.2.1", 443))],
                   connect=lambda host, addr, port, timeout, clock: (25.0, 50.0))
    assert (t.tcp_ms, t.tls_ms, t.status, t.error) == (25.0, 50.0, 404, "")
    assert len(t.http_ms) == 3 and len(net.requests) == 4  # the cold first request is not counted


def test_probe_reports_a_dns_failure():
    def no_dns(*args, **kwargs):
        raise socket.gaierror("no such host")
    t = probe_host("api.example.invalid", resolve=no_dns)
    assert t.error == "DNS: gaierror" and t.http_ms == []


# -- Claude and Cartesia ---------------------------------------------------------------------


def test_claude_model_lookup_is_free_and_sends_the_key_header_only():
    net = FakeNet()
    result = check_claude(CFG, ANT_KEY, transport=net.transport)
    assert result.level == PASS and CFG.llm.model in result.reason
    [req] = net.requests
    assert req.method == "GET" and req.url.path == f"/v1/models/{CFG.llm.model}"
    assert req.headers["x-api-key"] == ANT_KEY and ANT_KEY not in str(req.url)


@pytest.mark.parametrize("status, fix", [(401, "wrong or revoked"), (404, "llm.model"),
                                         (529, "fall back")])
def test_claude_errors_fail_with_a_fix(status, fix):
    result = check_claude(CFG, ANT_KEY, transport=FakeNet(claude=status).transport)
    assert result.level == FAIL and fix in result.fix and f"HTTP {status}" in result.reason


def test_claude_without_a_key_is_skipped():
    assert check_claude(CFG, None).level == SKIP


def test_cartesia_synthesis_reports_time_and_size():
    net = FakeNet()
    result = check_cartesia(CFG, CAR_KEY, transport=net.transport, clock=ticking(0.2))
    assert result.level == PASS and result.data == {"ms": 200, "bytes": len(MP3), "status": 200}
    body = json.loads(net.requests[0].content)
    assert body["transcript"] == preflight.TEST_SENTENCE and len(body["transcript"].split()) == 2
    assert body["output_format"]["container"] == CFG.speech.format
    assert body["voice"]["id"] == CFG.speech.voice_id
    assert net.requests[0].headers["Cartesia-Version"] == CFG.speech.api_version


@pytest.mark.parametrize("reply, step, want", [
    (lambda: httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"}), 6.0, "slow"),
    (lambda: httpx.Response(401, json={"error": f"bad key {CAR_KEY}"}), 0.1, "HTTP 401"),
    (lambda: httpx.Response(200, json={"oops": 1}), 0.1, "not audio"),
])
def test_cartesia_problems_only_warn(reply, step, want):
    result = check_cartesia(CFG, CAR_KEY, transport=FakeNet(cartesia=reply).transport,
                            clock=ticking(step))
    assert result.level == WARN and want in result.reason
    assert CAR_KEY not in result.reason


def test_cartesia_error_message_is_shown():
    # Cartesia's own error shape (seen live with a wrong key), unlike Anthropic's {"error": {...}}.
    reply = lambda: httpx.Response(401, json={"error_code": None, "message": "Invalid API key.",  # noqa: E731
                                              "title": "Unauthorized", "request_id": "r1"})
    result = check_cartesia(CFG, CAR_KEY, transport=FakeNet(cartesia=reply).transport)
    assert result.level == WARN and result.reason == "HTTP 401: Invalid API key."
    limited = lambda: httpx.Response(429, json={"error_code": "concurrency_limited",  # noqa: E731
                                                "message": "Too many requests."})
    result = check_cartesia(CFG, CAR_KEY, transport=FakeNet(cartesia=limited).transport)
    assert "concurrency_limited: Too many requests." in result.reason and "plan limit" in result.fix


def test_cartesia_down_only_warns():
    def down(req):
        raise httpx.ConnectError("unreachable")
    result = check_cartesia(CFG, CAR_KEY, transport=httpx.MockTransport(down))
    assert result.level == WARN and "browser voice" in result.fix


# -- the running server ----------------------------------------------------------------------


def test_server_not_running_fails():
    def refused(req):
        raise httpx.ConnectError("refused")
    _, _, result = fetch_server("http://127.0.0.1:8013", transport=httpx.MockTransport(refused))
    assert result.level == FAIL and "not running" in result.reason and "app.server" in result.fix


def test_an_older_server_without_health_fails():
    old = httpx.MockTransport(lambda req: httpx.Response(404))
    assert "no /api/health" in fetch_server("http://x", transport=old)[2].reason


@pytest.mark.parametrize("reply", [
    lambda req: httpx.Response(200, text="<html>another app</html>"),
    lambda req: httpx.Response(200, json={"status": "ok"}),
])
def test_something_else_on_the_port_fails_without_a_crash(reply):
    h, c, result = fetch_server("http://x", transport=httpx.MockTransport(reply))
    assert (h, c, result.level) == (None, None, FAIL) and "not like a BrainSync server" in result.reason


def test_a_healthy_live_server_passes_everything():
    assert set(levels(check_health(health(), {})).values()) == {PASS}


@pytest.mark.parametrize("over, name, want, text", [
    ({"decoder": {"keyboard_mode": True, "connected": False, "ok": False}}, "decoder", WARN,
     "keyboard mode"),
    ({"decoder": {"keyboard_mode": True, "connected": False, "ok": False}}, "display", WARN,
     "keyboard mode"),
    ({"decoder": {"fake_decoder": True, "fake_flag": True}}, "decoder", WARN, "fake decoder"),
    ({"decoder": {"fake_decoder": True, "fake_flag": True}}, "display", WARN, "fake decoder"),
    # tools/fake_decoder.py started by hand: the server knows it only by its hello's name.
    ({"decoder": {"fake_decoder": True, "name": "fake-decoder"}}, "decoder", WARN,
     "selections are random, not EEG (fake-decoder, started by hand)"),
    ({"decoder": {"fake_decoder": True, "name": "fake-decoder"}}, "display", WARN, "fake decoder"),
    ({"decoder": {"fake_decoder": True, "name": "fake-decoder", "ok": False,
                  "message": "low signal"}}, "decoder", FAIL, "fake decoder (fake-decoder)"),
    ({}, "decoder", PASS, "bridge ssvep-fbcca connected and ok"),
    ({"decoder": {"connected": False, "ok": False}}, "decoder", FAIL, "no bridge"),
    ({"decoder": {"connected": False, "ok": False}}, "display", FAIL, "no display"),
    ({"decoder": {"fake_decoder": True, "fake_flag": True, "connected": False, "ok": False,
                  "name": ""}}, "decoder", FAIL, "fake decoder not connected"),
    ({"decoder": {"fake_decoder": True, "fake_flag": True, "ok": False, "name": "",
                  "message": "Rejected: slots"}}, "decoder", FAIL,
     "Rejected: slots"),
    ({"decoder": {"ok": False, "message": "Rejected: refresh 120 Hz"}}, "decoder", FAIL, "120 Hz"),
    ({"decoder": {"display_ok": False}}, "display", FAIL, "display down"),
    ({"decoder": {"board_ok": False}}, "decoder", WARN, "board_ok"),
    ({"partner_page": {"connected": False, "mic": ""}}, "partner page", FAIL, "not open"),
    ({"partner_page": {"connected": False, "mic": ""}}, "mic", WARN, "no partner page"),
    ({"partner_page": {"mic_muted": True}}, "mic", WARN, "muted"),
    ({"partner_page": {"mic": "denied"}}, "mic", WARN, "denied"),
    ({"partner_page": {"mic": "offline"}}, "mic", WARN, "offline"),
    ({"partner_page": {"mic": ""}}, "mic", WARN, "no report yet"),
    ({"partner_page": {"mic": "paused"}}, "mic", WARN, "not proven yet"),
    ({"partner_page": {"mic": "speaking"}}, "mic", PASS, "speaking"),
    ({"llm": {"enabled": False, "why_disabled": "ANTHROPIC_API_KEY is not set"}},
     "claude (server)", FAIL, "ANTHROPIC_API_KEY"),
    ({"llm": {"warm_ok": None}}, "claude (server)", WARN, "not answered yet"),
    ({"llm": {"warm_ok": False}}, "claude (server)", WARN, "keep-alive failing"),
    ({"llm": {"keepalive_s": 0.0, "warm_ok": None}}, "claude (server)", WARN, "keep-alive off"),
    ({"tts": {"enabled": False, "why_disabled": "CARTESIA_API_KEY is not set"}},
     "voice (server)", WARN, "browser voice"),
    # Chosen in the config, not a problem: like the key checks above.
    ({"llm": {"enabled": False, "dynamic": False, "why_disabled": "menus.dynamic is false"}},
     "claude (server)", SKIP, "fixed lists only"),
    ({"tts": {"enabled": False, "provider": "browser", "why_disabled": "speech.provider is browser"}},
     "voice (server)", SKIP, "speech.provider is browser"),
])
def test_health_problems(over, name, want, text):
    result = {r.name: r for r in check_health(health(**over), {})}[name]
    assert result.level == want and text in result.reason
    assert want in (PASS, SKIP) or result.fix


@pytest.mark.parametrize("message, fix", [("Rejected: refresh 120 Hz", "rejected hello"),
                                          ("", "bridge's terminal")])
def test_a_bridge_that_is_not_ok_gets_the_matching_fix(message, fix):
    result = check_health(health(decoder={"ok": False, "message": message}), {})[0]
    assert result.level == FAIL and fix in result.fix


@pytest.mark.parametrize("decoder, fix", [
    ({"fake_decoder": True, "name": "fake-decoder"}, "stop tools/fake_decoder.py"),
    ({"fake_decoder": True, "fake_flag": True}, "restart without --fake-decoder"),
])
def test_a_fake_decoder_gets_the_fix_for_how_it_was_started(decoder, fix):
    result = check_health(health(decoder=decoder), {})[0]
    assert result.level == WARN and fix in result.fix and "start the bridge" in result.fix


def test_a_server_without_decoder_names_still_counts_its_flag():
    h = health()
    h["decoder"] = {k: v for k, v in h["decoder"].items() if k not in ("name", "fake_flag")}
    h["decoder"]["fake_decoder"] = True
    result = check_health(h, {})[0]
    assert result.level == WARN and "--fake-decoder" in result.fix


# -- fixed-list clips on disk ----------------------------------------------------------------


def write_clips(folder, count: int | None = None) -> int:
    """Put a clip for the first ``count`` fixed sentences (all by default) in ``folder``."""
    texts = load_intents().utterances
    tts = CartesiaTTS(CFG.speech, None, cache_dir=folder)
    folder.mkdir(parents=True, exist_ok=True)
    for text in texts[:count]:
        (folder / f"{tts.id_for(text)}.mp3").write_bytes(MP3)
    return len(texts)


def test_clips_pass_when_every_fixed_sentence_is_on_disk(tmp_path):
    total = write_clips(tmp_path)
    result = check_clips(CFG, tmp_path)
    assert result.level == PASS and result.data == {"cached": total, "total": total}


def test_missing_clips_warn_with_the_prerender_command(tmp_path):
    total = write_clips(tmp_path, 2)
    result = check_clips(CFG, tmp_path)
    assert result.level == WARN and f"2/{total}" in result.reason
    assert "prerender_tts.py" in result.fix


@pytest.mark.parametrize("speech", [{"provider": "browser"}, {"cache_dir": ""}])
def test_clips_skip_without_cartesia_or_a_cache(speech):
    cfg = CFG.model_copy(update={"speech": CFG.speech.model_copy(update=speech)})
    assert check_clips(cfg).level == SKIP


# -- the whole run ---------------------------------------------------------------------------


def test_run_with_everything_fine_is_a_go(tmp_path):
    write_clips(tmp_path)
    results = run_checks("http://127.0.0.1:8013", env=ENV, transport=FakeNet().transport,
                         probe=fast_probe, clips_dir=tmp_path)
    assert {r.level for r in results} == {PASS}
    names = [r.name for r in results]
    assert names[:3] == ["config", "ANTHROPIC_API_KEY", "CARTESIA_API_KEY"]
    assert {"net api.anthropic.com", "net api.cartesia.ai", "claude", "cartesia", "clips",
            "server", "decoder", "mic"} <= set(names)


def test_skip_network_makes_no_api_calls():
    net = FakeNet()
    results = run_checks("http://127.0.0.1:8013", skip_network=True, env=ENV,
                         transport=net.transport, probe=lambda host: pytest.fail("probed"))
    assert levels(results)["network"] == SKIP
    assert {r.url.path for r in net.requests} == {"/api/health", "/api/config"}


def test_report_ends_with_the_verdict_and_the_headset_checklist():
    results = run_checks("http://127.0.0.1:8013", skip_network=True, env={},
                         transport=FakeNet(health(decoder={"connected": False, "ok": False})).transport)
    text = report(results, [])
    assert "NO-GO" in text and "fix: start the bridge" in text
    assert "Headset checklist" in text and "20 minutes" in text and "ProMotion" in text


def test_main_exit_code_and_json(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "load_env", lambda: [])  # never the real .env in tests
    for bad in (False, True):
        h = health(partner_page={"connected": not bad})
        monkeypatch.setattr(preflight, "run_checks", lambda url, **kw: run_checks(
            url, skip_network=True, env=ENV, transport=FakeNet(h).transport))
        with pytest.raises(SystemExit) as exit_:
            preflight.main(["--json", "--skip-network"])
        out = json.loads(capsys.readouterr().out)
        assert exit_.value.code == (1 if bad else 0) and out["ok"] is not bad
        assert out["checklist"] and all("level" in r for r in out["results"])


def test_secrets_are_redacted_from_any_output():
    text = f"key {ANT_KEY} and {CAR_KEY} and sk-ant-api03-abcDEF_12-x"
    assert redact(text) == "key [redacted] and [redacted] and [redacted]"
    assert redact("my-own-secret here", ["my-own-secret"]) == "[redacted] here"
