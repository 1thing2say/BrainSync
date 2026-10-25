"""Demo go/no-go checks (PLAN.md V6, item 1).

Each check prints PASS, WARN or FAIL (SKIP when it was not run) with a one-line reason and, unless
it passed, a fix. Exits 0 only if nothing FAILed; a WARN is a fallback the demo survives (the
browser voice, keyboard mode) or something to look at.

1. config/default.yaml is valid.
2. ANTHROPIC_API_KEY and CARTESIA_API_KEY are set (environment or .env; names only, never values).
3. Network: DNS, TCP connect (about one ping), TLS and HTTP round trips to api.anthropic.com and
   api.cartesia.ai. A congested link (1-4 s ping on 2026-10-24) timed out every Claude page.
4. One free Claude call (GET /v1/models/{llm.model}, no tokens): the key and the model work.
5. One short Cartesia synthesis in the configured voice and format; WARN if it fails, since the
   browser voice then speaks every reply. Then how many fixed-list clips are already on disk
   (speech.cache_dir); WARN if some are missing (python tools/prerender_tts.py fetches them).
6. The running server (GET /api/health and /api/config at --url): decoder, display, partner page,
   mic, Claude keep-alive, Cartesia.
Then the headset checklist is printed as a reminder (manual, not checked).

    python tools/preflight.py                        # server at http://127.0.0.1:8000
    python tools/preflight.py --url http://127.0.0.1:8013 --skip-network
    python tools/preflight.py --json                 # one JSON object instead of the table
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import statistics
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import (  # noqa: E402
    REPO_ROOT,
    AppConfig,
    ConfigError,
    load_config,
    load_env,
    load_intents,
)
from app.tts import TTS_URL, CartesiaTTS  # noqa: E402

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
ANTHROPIC_HOST = "api.anthropic.com"
CARTESIA_HOST = httpx.URL(TTS_URL).host
ANTHROPIC_VERSION = "2023-06-01"  # the Messages API version header
TEST_SENTENCE = "Preflight check."  # two words: a small clip, a real synthesis
KEYS = ("ANTHROPIC_API_KEY", "CARTESIA_API_KEY")
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+|sk_car_[A-Za-z0-9_\-]+")

HEADSET_CHECKLIST = [
    "Headset on at least 20 minutes before recording or demoing (unsettled contacts ruin the "
    "first trials).",
    "One eyes-closed alpha check (spectrum_check -t 10): a posterior 9-10 Hz peak and RMS under "
    "20 uV on O1 and O2.",
    "Nothing else reads the board's serial port (a live plot splits the stream).",
    "Allow about 16 s for the board to connect; its first \"Handshake verification failed\" line "
    "is harmless.",
    "Display at a fixed 60 Hz, ProMotion / variable refresh off (with it on there is no SSVEP).",
    "Nobody with photosensitive epilepsy or flicker sensitivity uses it (11-17 Hz flicker).",
]


@dataclass
class Result:
    """One line of the report."""

    name: str
    level: str  # PASS, WARN, FAIL or SKIP
    reason: str
    fix: str = ""
    data: dict[str, Any] = field(default_factory=dict)  # numbers for --json


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove API keys from ``text``: the given values, and anything shaped like one."""
    for s in secrets or []:
        if s:
            text = text.replace(s, "[redacted]")
    return SECRET.sub("[redacted]", text)


# ---------------------------------------------------------------------------
# 1-2. Config and keys
# ---------------------------------------------------------------------------


def check_config(path: str | Path | None = None) -> tuple[Result, AppConfig | None]:
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        return Result("config", FAIL, f"{exc.path.name}: {'; '.join(exc.problems)[:300]}",
                      "fix the listed keys (PLAN.md section 5)"), None
    # code slots (the c-VEP default) carry a lag in frames, frequency slots a frequency
    slots = ", ".join(f"{s.slot} {s.freq_hz:g} Hz" if s.freq_hz is not None
                      else f"{s.slot} lag {s.lag_frames}" for s in cfg.stim.slots)
    stimulus = "c-VEP code" if cfg.stim.paradigm == "code" else cfg.stim.waveform
    return Result("config", PASS, f"{cfg.menus.mode} mode, {slots}, {stimulus}"), cfg


def check_keys(cfg: AppConfig, env: Mapping[str, str]) -> list[Result]:
    """Whether each key is set. Only names are reported, never values."""
    needed = {
        "ANTHROPIC_API_KEY": (cfg.menus.dynamic, FAIL, "pages are fixed lists, not Claude's",
                              "menus.dynamic is false"),
        "CARTESIA_API_KEY": (cfg.speech.provider == "cartesia", WARN,
                             "the browser voice speaks every reply",
                             f"speech.provider is {cfg.speech.provider}"),
    }
    results = []
    for name, (used, level, without, unused) in needed.items():
        if env.get(name):
            results.append(Result(name, PASS, "set"))
        elif not used:
            results.append(Result(name, SKIP, f"not needed: {unused}"))
        else:
            results.append(Result(name, level, f"not set: {without}",
                                  f"add {name}=... to .env at the repo root"))
    return results


# ---------------------------------------------------------------------------
# 3. Network
# ---------------------------------------------------------------------------


@dataclass
class Timing:
    """One host's connection times in ms; ``error`` says which step failed."""

    host: str
    dns_ms: float | None = None
    tcp_ms: float | None = None  # about one round trip: the ping-like number
    tls_ms: float | None = None
    http_ms: list[float] = field(default_factory=list)  # round trips on an open connection
    status: int | None = None  # any HTTP answer proves the server is reached
    error: str = ""


def _tls_connect(host: str, addr: str, port: int, timeout: float,
                 clock: Callable[[], float]) -> tuple[float, float]:
    """Open TCP then TLS to ``addr``; returns (tcp_ms, tls_ms) and closes the socket."""
    started = clock()
    with socket.create_connection((addr, port), timeout=timeout) as sock:
        tcp = clock()
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
            tls = clock()
    return (tcp - started) * 1000, (tls - tcp) * 1000


def probe_host(host: str, *, timeout: float = 10.0, rounds: int = 3,
               transport: httpx.BaseTransport | None = None,
               resolve: Callable[..., Any] = socket.getaddrinfo,
               connect: Callable[..., tuple[float, float]] = _tls_connect,
               clock: Callable[[], float] = time.perf_counter) -> Timing:
    """DNS, TCP and TLS once, then ``rounds`` HTTP round trips on one open connection (the first
    request, which opens its own connection, is not counted)."""
    t = Timing(host)
    try:
        started = clock()
        infos = resolve(host, 443, type=socket.SOCK_STREAM)
        t.dns_ms = (clock() - started) * 1000
        t.tcp_ms, t.tls_ms = connect(host, infos[0][4][0], 443, timeout, clock)
    except (OSError, IndexError) as exc:  # gaierror, timeout, refused, TLS failure
        t.error = f"{'connect' if t.dns_ms is not None else 'DNS'}: {type(exc).__name__}"
        return t
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            for n in range(rounds + 1):
                started = clock()
                t.status = client.get(f"https://{host}/").status_code
                if n:
                    t.http_ms.append((clock() - started) * 1000)
    except httpx.HTTPError as exc:  # its message holds only the URL; the type says enough
        t.error = f"HTTP: {type(exc).__name__}"
    return t


def check_network(hosts: list[str], probe: Callable[[str], Timing],
                  slow_ms: float = 1000.0) -> list[Result]:
    """One result per host: FAIL if it cannot be reached, WARN if the link is slow. Cartesia
    unreachable is a WARN: the browser voice takes over, as everywhere else in the preflight."""
    results = []
    for host in hosts:
        t = probe(host)
        data = asdict(t)
        if t.error and not t.http_ms:
            if "cartesia" in host:
                results.append(Result(f"net {host}", WARN, f"unreachable ({t.error})",
                                      "replies will use the browser voice; check the Wi-Fi", data))
            else:
                results.append(Result(f"net {host}", FAIL, f"unreachable ({t.error})",
                                      "check the Wi-Fi or switch to a phone hotspot", data))
            continue
        median = statistics.median(t.http_ms)
        setup = (t.dns_ms or 0) + (t.tcp_ms or 0) + (t.tls_ms or 0)
        reason = (f"DNS {t.dns_ms:.0f} ms, TCP {t.tcp_ms:.0f} ms (~ping), TLS {t.tls_ms:.0f} ms, "
                  f"HTTP {min(t.http_ms):.0f}/{median:.0f}/{max(t.http_ms):.0f} ms "
                  f"(min/median/max of {len(t.http_ms)})")
        if median > slow_ms or setup > 2 * slow_ms:
            results.append(Result(
                f"net {host}", WARN, "congested link: " + reason,
                "a Claude page must fit in llm.timeout_s; at 1-4 s ping every page timed out "
                "(2026-10-24). Use a phone hotspot or a wired network", data))
        else:
            results.append(Result(f"net {host}", PASS, reason, data=data))
    return results


# ---------------------------------------------------------------------------
# 4-5. Claude and Cartesia
# ---------------------------------------------------------------------------


def _error_detail(response: httpx.Response, secrets: list[str]) -> str:
    """The API's own error type and message (short, keys removed)."""
    try:
        body = response.json()
        err = body.get("error") or {}
        if isinstance(err, dict):
            text = f"{err.get('type', '')}: {err.get('message', '')}".strip(": ")
        else:
            text = str(err)
        if not text:  # Cartesia: {"error_code": ..., "message": "Invalid API key.", "title": ...}
            text = f"{body.get('error_code') or ''}: {body.get('message') or body.get('title') or ''}"
            text = text.strip(": ")
    except (ValueError, AttributeError):
        text = response.text
    return redact(text[:160], secrets)


def check_claude(cfg: AppConfig, api_key: str | None, *, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.perf_counter) -> Result:
    """GET /v1/models/{model}: free (no tokens), proves the key and the model name."""
    model = cfg.llm.model
    if not cfg.menus.dynamic:
        return Result("claude", SKIP, "menus.dynamic is false: fixed lists only")
    if not api_key:
        return Result("claude", SKIP, "no ANTHROPIC_API_KEY (see above)")
    started = clock()
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            r = client.get(f"https://{ANTHROPIC_HOST}/v1/models/{model}",
                           headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION})
    except httpx.HTTPError as exc:
        return Result("claude", FAIL, f"no answer ({type(exc).__name__}): pages would be fixed lists",
                      "check the network (above)")
    ms = (clock() - started) * 1000
    data = {"ms": round(ms), "status": r.status_code, "model": model}
    if r.status_code == 200:
        return Result("claude", PASS, f"key and model {model} work ({ms:.0f} ms, cold connection)",
                      data=data)
    fix = {401: "ANTHROPIC_API_KEY is wrong or revoked: replace it in .env",
           403: "the key has no access to this model or API",
           404: f"no model named {model}: check llm.model in config/default.yaml"}.get(
               r.status_code, "try again in a minute; pages fall back to fixed lists meanwhile")
    return Result("claude", FAIL, f"HTTP {r.status_code}: {_error_detail(r, [api_key])}", fix, data)


def check_cartesia(cfg: AppConfig, api_key: str | None, *, transport: httpx.BaseTransport | None = None,
                   clock: Callable[[], float] = time.perf_counter) -> Result:
    """One short synthesis exactly as app/tts.py asks for it. WARN, never FAIL: without Cartesia
    the browser voice speaks."""
    speech = cfg.speech
    tts = CartesiaTTS(speech, api_key)
    if not tts.enabled:
        level = SKIP if speech.provider != "cartesia" else WARN
        return Result("cartesia", level, f"off: {tts.why_disabled}",
                      "" if level == SKIP else "the browser voice speaks every reply")
    headers = {"Authorization": f"Bearer {api_key}", "Cartesia-Version": speech.api_version,
               "Content-Type": "application/json"}
    fallback = "replies use the browser voice until this passes"
    started = clock()
    try:
        with httpx.Client(transport=transport, timeout=speech.tts_timeout_s) as client:
            r = client.post(TTS_URL, headers=headers, json=tts.body(TEST_SENTENCE))
    except httpx.HTTPError as exc:
        return Result("cartesia", WARN, f"no answer in {speech.tts_timeout_s:g} s "
                      f"({type(exc).__name__})", fallback)
    ms = (clock() - started) * 1000
    kind = r.headers.get("content-type", "")
    data = {"ms": round(ms), "bytes": len(r.content), "status": r.status_code}
    if r.status_code == 200 and kind.startswith("audio/") and r.content:
        reason = (f"{speech.model}, voice {speech.voice_id[:8]}: {ms:.0f} ms, "
                  f"{len(r.content) / 1000:.1f} KB {speech.format}")
        if ms > speech.tts_timeout_s * 500:  # over half the per-clip time limit
            return Result("cartesia", WARN, "slow: " + reason,
                          "some clips may time out (speech.tts_timeout_s); the browser speaks those",
                          data)
        return Result("cartesia", PASS, reason, data=data)
    fix = {401: "CARTESIA_API_KEY is wrong or revoked: replace it in .env",
           400: f"check speech.voice_id and speech.api_version ({speech.api_version}; "
                "the API reference lists 2026-08-14)",
           404: "check speech.voice_id and speech.model",
           429: "plan limit reached (a running server may be fetching clips): rerun in a moment",
           }.get(r.status_code, fallback)
    detail = _error_detail(r, [api_key or ""]) if r.status_code != 200 else f"not audio ({kind})"
    return Result("cartesia", WARN, f"HTTP {r.status_code}: {detail}", fix, data)


def check_clips(cfg: AppConfig, clips_dir: str | Path | None = None) -> Result:
    """How many fixed-list sentences (intents.yaml) already have a Cartesia clip on disk. Missing
    ones are fetched when the server starts, which is slow on a busy network: WARN."""
    speech = cfg.speech
    if speech.provider != "cartesia":
        return Result("clips", SKIP, "speech.provider is browser")
    if clips_dir is None and not speech.cache_dir:
        return Result("clips", SKIP, "speech.cache_dir is empty: clips live in memory only")
    clips_dir = Path(clips_dir) if clips_dir is not None else REPO_ROOT / speech.cache_dir
    m = cfg.menus
    try:
        intents = load_intents(option_count=m.option_count, label_max_chars=m.label_max_chars,
                               utterance_max_words=m.utterance_max_words,
                               label_max_words=m.label_max_words)
    except ConfigError as exc:
        return Result("clips", FAIL, f"config/intents.yaml is invalid: {exc}",
                      "fix the problems listed above")
    ready, total = CartesiaTTS(speech, None, cache_dir=clips_dir).cached_count(intents.utterances)
    reason = f"{ready}/{total} fixed-list clips on disk ({speech.model}, voice {speech.voice_id[:8]})"
    data = {"cached": ready, "total": total}
    if ready < total:
        return Result("clips", WARN, reason,
                      "python tools/prerender_tts.py (else the server fetches them at startup)", data)
    return Result("clips", PASS, reason, data=data)


# ---------------------------------------------------------------------------
# 6. The running server
# ---------------------------------------------------------------------------


HEALTH_SECTIONS = ("decoder", "partner_page", "llm", "tts")


def fetch_server(url: str, *, timeout: float = 5.0,
                 transport: httpx.BaseTransport | None = None) -> tuple[dict | None, dict | None, Result]:
    """(health, page config, result): GET /api/health and /api/config."""
    base = url.rstrip("/")
    start = "start it with python -m app.server (add --keyboard or --fake-decoder without a bridge)"
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            health = client.get(f"{base}/api/health")
            page_cfg = client.get(f"{base}/api/config")
    except httpx.HTTPError as exc:
        return None, None, Result("server", FAIL, f"not running at {base} ({type(exc).__name__})",
                                  start)
    if health.status_code == 404:
        return None, None, Result("server", FAIL, f"{base} has no /api/health",
                                  "an older server: restart it from this branch")
    if health.status_code != 200 or page_cfg.status_code != 200:
        return None, None, Result("server", FAIL, f"HTTP {health.status_code} / {page_cfg.status_code} "
                                  "from /api/health and /api/config", start)
    try:
        h, c = health.json(), page_cfg.json()
        if not (isinstance(h, dict) and isinstance(c, dict)
                and all(isinstance(h.get(k), dict) for k in HEALTH_SECTIONS)):
            raise ValueError("unexpected shape")
    except ValueError:  # another program on this port, or a different server version
        return None, None, Result("server", FAIL, f"{base} answers, but not like a BrainSync server",
                                  "check --url, or restart the server from this branch")
    return h, c, Result("server", PASS, f"running at {base}, state {h.get('state')}")


MIC_FIX = {
    "": "open the partner page in Chrome and allow the microphone",
    "muted": "the partner page's Mute is on: press Unmute",
    "denied": "allow the microphone for this page in Chrome's site settings, then reload",
    "nomic": "no microphone found: plug one in or pick one in Chrome",
    "offline": "Chrome's speech recognition needs internet; the typed box still works",
    "unsupported": "use Chrome (the Web Speech API); the typed box still works",
    "needs_tap": "tap the partner page once so Chrome turns the mic on",
    "hidden": "bring the partner page to the front (Chrome stops recognition in a hidden tab)",
    "error": "reload the partner page",
}


def check_health(h: dict, page_cfg: dict) -> list[Result]:
    """Go/no-go from the server's own view: decoder, display, page, mic, Claude, voice."""
    d, p, llm, tts = h["decoder"], h["partner_page"], h["llm"], h["tts"]
    keyboard, fake = d["keyboard_mode"] or page_cfg.get("keyboard_mode", False), d["fake_decoder"]
    # fake_decoder is also true for a tools/fake_decoder.py started by hand (its hello's name);
    # a server without fake_flag only knew the --fake-decoder flag.
    fake_flag, name = d.get("fake_flag", fake), d.get("name", "")
    msg = f" ({d['message']})" if d["message"] else ""
    results = []

    if keyboard:
        results.append(Result("decoder", WARN, "keyboard mode: keys 1-4 select, no EEG decoding"
                              + (", a bridge is connected" if d["connected"] else ""),
                              "restart without --keyboard for a live EEG run"))
    elif fake and d["connected"] and d["ok"]:
        results.append(Result("decoder", WARN, "fake decoder: selections are random, not EEG"
                              + ("" if fake_flag else f" ({name or 'fake'}, started by hand)"),
                              "restart without --fake-decoder and start the bridge" if fake_flag
                              else "stop tools/fake_decoder.py and start the bridge"))
    elif fake and not fake_flag:  # a hand-started fake whose status says ok false
        results.append(Result("decoder", FAIL, f"fake decoder ({name or 'fake'}) connected but "
                              "not ok" + msg, "stop tools/fake_decoder.py and start the bridge"))
    elif fake:  # nothing selects: the session would wait forever
        results.append(Result("decoder", FAIL, "fake decoder "
                              + ("connected but not ok" + msg if d["connected"] else "not connected"),
                              "restart the server with --fake-decoder (it starts the fake decoder)"))
    elif not d["connected"]:
        results.append(Result("decoder", FAIL, "no bridge connected",
                              "start the bridge: python -m ssvep.online.bridge (in ../ssvep)"))
    elif not d["ok"]:
        rejected = d["message"].startswith("Rejected")
        results.append(Result("decoder", FAIL, "bridge connected but not ok" + msg,
                              "a rejected hello: match the slots, waveform and 60 Hz refresh"
                              if rejected else "no hello yet, or the bridge reports ok false: "
                              "check the bridge's terminal"))
    else:
        board = "" if d["board_ok"] else "; board_ok is false (check the headset and USB)"
        bridge = f"bridge {name}" if name else "bridge"
        results.append(Result("decoder", WARN if board else PASS,
                              f"{bridge} connected and ok" + msg + board,
                              "reseat the headset or replug the board" if board else ""))

    if fake and not keyboard:
        results.append(Result("display", WARN, "fake decoder: no PsychoPy display, the mirror "
                              "stands in", "start the bridge for the real display"))
    elif d["connected"] and d["ok"]:
        results.append(Result("display", PASS if d["display_ok"] else FAIL,
                              "display up" if d["display_ok"] else "display down (Esc or it exited)",
                              "" if d["display_ok"] else "relaunch the display, then Resume"))
    elif keyboard:
        results.append(Result("display", WARN, "no display (keyboard mode): the mirror stands in",
                              "fine for a keyboard demo; start the bridge for the flicker"))
    else:
        results.append(Result("display", FAIL, "no display without a working bridge",
                              "start the bridge (it launches the display)"))

    if p["connected"]:
        # Which partner client: the iPhone app (/ws/ui?client=ios) or a browser; older servers
        # report nothing. Information only, never a failure.
        client = {"ios": "iPhone app", "browser": "browser"}.get(h.get("ui_client", ""), "")
        results.append(Result("partner page", PASS, f"connected ({client})" if client else "connected"))
    else:
        results.append(Result("partner page", FAIL, "not open",
                              "open the server's address in Chrome"))

    mic, mode = "muted" if p["mic_muted"] else p["mic"], p["stt_mode"]
    if not p["connected"]:
        results.append(Result("mic", WARN, "no partner page, so no mic report", MIC_FIX[""]))
    elif mic in ("listening", "speaking"):
        results.append(Result("mic", PASS, f"{mic} ({mode} mode)"))
    elif mic in ("paused", "stopped") or (mic == "" and mode == "push_to_talk"):
        results.append(Result("mic", WARN, f"{mic or 'no report yet'} ({mode} mode, state "
                              f"{h['state']}): not proven yet",
                              "start the session and check that the page says it is listening"))
    else:
        results.append(Result("mic", WARN, f"{mic or 'no report yet'} ({mode} mode)",
                              MIC_FIX.get(mic, MIC_FIX["error"])))

    if not llm.get("dynamic", True):
        results.append(Result("claude (server)", SKIP, "menus.dynamic is false: fixed lists only"))
    elif not llm["enabled"]:
        results.append(Result("claude (server)", FAIL, f"off: {llm['why_disabled']}",
                              "add ANTHROPIC_API_KEY to .env and restart the server"))
    else:
        page = "; last page in time" if llm["ok"] else ""
        warm = llm["warm_ok"]
        if llm["keepalive_s"] <= 0:
            results.append(Result("claude (server)", WARN, f"{llm['model']}, keep-alive off" + page,
                                  "set llm.keepalive_s (20) so pages skip the connection setup"))
        elif warm:
            results.append(Result("claude (server)", PASS,
                                  f"{llm['model']}, connection kept open" + page))
        else:
            why = "keep-alive failing" if warm is False else "keep-alive not answered yet"
            results.append(Result("claude (server)", WARN, f"{llm['model']}, {why}" + page,
                                  "check the network; rerun in 20 s"))

    if tts["provider"] != "cartesia":
        results.append(Result("voice (server)", SKIP,
                              f"speech.provider is {tts['provider']}: the browser voice speaks"))
    elif tts["enabled"]:
        results.append(Result("voice (server)", PASS, f"Cartesia {tts['model']} ({tts['format']})"))
    else:
        results.append(Result("voice (server)", WARN, f"browser voice only: {tts['why_disabled']}",
                              "add CARTESIA_API_KEY to .env and restart the server"))
    return results


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def run_checks(url: str, *, skip_network: bool = False, env: Mapping[str, str] | None = None,
               config_path: str | Path | None = None, transport: httpx.BaseTransport | None = None,
               probe: Callable[[str], Timing] | None = None, timeout: float = 10.0,
               slow_ms: float = 1000.0, clips_dir: str | Path | None = None) -> list[Result]:
    """Every check in order. ``transport`` replaces the network for all HTTP and ``probe`` the
    DNS/TCP/TLS probe (tests); ``clips_dir`` replaces ``speech.cache_dir``."""
    env = os.environ if env is None else env
    result, cfg = check_config(config_path)
    results = [result]
    if cfg is None:
        return results
    results += check_keys(cfg, env)
    anthropic_key, cartesia_key = env.get("ANTHROPIC_API_KEY"), env.get("CARTESIA_API_KEY")
    if skip_network:
        results.append(Result("network", SKIP, "--skip-network: no network, Claude or Cartesia check"))
    else:
        hosts = ([ANTHROPIC_HOST] if cfg.menus.dynamic else []) + (
            [CARTESIA_HOST] if cfg.speech.provider == "cartesia" else [])
        probe = probe or (lambda host: probe_host(host, timeout=timeout, transport=transport))
        results += check_network(hosts, probe, slow_ms)
        results.append(check_claude(cfg, anthropic_key, timeout=timeout, transport=transport))
        results.append(check_cartesia(cfg, cartesia_key, transport=transport))
    results.append(check_clips(cfg, clips_dir))
    health, page_cfg, result = fetch_server(url, transport=transport)
    results.append(result)
    if health is not None:
        results += check_health(health, page_cfg or {})
    return results


def report(results: list[Result], secrets: list[str]) -> str:
    width = max(len(r.name) for r in results)
    lines = []
    for r in results:
        lines.append(f"{r.level}  {r.name:<{width}}  {r.reason}")
        if r.fix and r.level != PASS:
            lines.append(f"{'':6}{'':<{width}}  fix: {r.fix}")
    counts = {lvl: sum(r.level == lvl for r in results) for lvl in (PASS, WARN, FAIL, SKIP)}
    verdict = "NO-GO" if counts[FAIL] else "GO"
    lines.append(f"\n{verdict}: {counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, "
                 f"{counts[SKIP]} skipped")
    lines.append("\nHeadset checklist (manual):")
    lines += [f"  [ ] {item}" for item in HEADSET_CHECKLIST]
    return redact("\n".join(lines), secrets)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="preflight", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8000", help="the running BrainSync server")
    p.add_argument("--skip-network", action="store_true",
                   help="skip the network, Claude and Cartesia checks (the server is still checked)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    p.add_argument("--config", type=Path, help="config file (default config/default.yaml)")
    p.add_argument("--timeout", type=float, default=10.0, help="seconds per network request")
    p.add_argument("--slow-ms", type=float, default=1000.0,
                   help="median HTTP round trip above this is a congested link (WARN)")
    args = p.parse_args(argv)
    load_env()  # names only; the values stay in this process
    secrets = [os.environ.get(k, "") for k in KEYS]
    results = run_checks(args.url, skip_network=args.skip_network, config_path=args.config,
                         timeout=args.timeout, slow_ms=args.slow_ms)
    ok = not any(r.level == FAIL for r in results)
    if args.json:
        out = {"ok": ok, "results": [asdict(r) for r in results], "checklist": HEADSET_CHECKLIST}
        print(redact(json.dumps(out, indent=2), secrets))
    else:
        print(report(results, secrets))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
