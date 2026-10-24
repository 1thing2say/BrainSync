"""End-to-end tests of python -m brainsync.server: real sockets, a real-time FakeBoard.

One server (``app.serve`` on a thread, port 0, a token file, calibrate_s 4) serves the slots
mode, point mode, auth and HTTP tests. Point mode runs the real Detector with a stub
``create``, so the request and answer handling are real and no test reaches the API. The CLI
tests run ``python -m brainsync.server`` as a subprocess. The JavaScript tests drive the real
SDK, reference page and extension code (tests/server_e2e_client.mjs) with Node's built-in
WebSocket, and are skipped without node.

The decoder needs about 2.5 s of gaze per selection, so the module takes about half a minute.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("websockets")

from brainsync.screen.detect import Detector  # noqa: E402
from brainsync.server import app  # noqa: E402
from brainsync.server.client import Client, ServerError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
NODE = shutil.which("node") or next(
    (p for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if Path(p).exists()), None)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
VIEWPORT = {"w": 1000, "h": 600, "dpr": 1}
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF").decode()
CALIBRATE_S = 4.0
RUN_TIMEOUT = CALIBRATE_S + 8.0      # calibration plus slack, for the first "run"
SELECT_TIMEOUT = 8.0                 # about 2.5 s per selection on the fake board

# What the stub model "sees": a safe button and a risky one, in image pixels (1000 x 600).
ELEMENTS = [
    {"label": "Sign in", "role": "button", "x": 100, "y": 100, "w": 120, "h": 40, "hint_id": 0,
     "p_button": 0.95, "p_next": 0.9, "risky": False, "enabled": True},
    {"label": "Delete account", "role": "button", "x": 600, "y": 100, "w": 150, "h": 40,
     "hint_id": -1, "p_button": 0.9, "p_next": 0.8, "risky": True, "enabled": True},
]
HINTS = [{"id": 0, "x": 100, "y": 100, "w": 120, "h": 40, "role": "button", "text": "Sign in",
          "tag": "button"}]


class StubCreate:
    """Stands in for anthropic's messages.create: records each request, answers ELEMENTS."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, **params):
        with self.lock:
            self.calls.append(params)
        text = json.dumps({"summary": "Login page", "elements": ELEMENTS})
        return SimpleNamespace(stop_reason="end_turn", model="stub-haiku",
                               content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=200))


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("server_e2e")
    token_file = tmp / "config" / "token"
    p = app.build_parser()
    args = p.parse_args(["--board", "fake", "--port", "0", "--token-file", str(token_file),
                         "--calibrate-s", str(CALIBRATE_S), "--log", str(tmp / "log.jsonl"),
                         "--log-scans", str(tmp / "scans")])
    app.check_arguments(p, args)
    stub = StubCreate()
    ready: queue.Queue = queue.Queue()
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(app.serve(args, stop, ready.put,
                                               detector=Detector(create=stub))),
        name="e2e-server", daemon=True)
    thread.start()
    transport = ready.get(timeout=30)
    token = token_file.read_text().strip()
    yield SimpleNamespace(url=transport.url, port=transport.port, token=token, stub=stub,
                          tmp=tmp, token_file=token_file, transport=transport)
    stop.set()
    thread.join(15)
    assert not thread.is_alive() and result == [0]


def controller(server, **kw) -> Client:
    """A connected controller that took over from whatever test came before."""
    kw.setdefault("takeover", True)
    c = Client(server.url, server.token, **kw)
    c.connect(timeout=10)
    return c


def wait_run(c: Client) -> None:
    if not (c.phase and c.phase["phase"] == "run"):
        c.until("phase", timeout=RUN_TIMEOUT, pred=lambda m: m["phase"] == "run")


def selections_until(c: Client, slot: str, timeout: float) -> list[str]:
    """The slots of every selection up to and including the first of ``slot``."""
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"no selection of {slot!r} within {timeout:g} s; saw {seen}")
        msg = c.until("selection", timeout=left)
        seen.append(msg["slot"])
        if msg["slot"] == slot:
            return seen


def scan(scan_id: int) -> dict:
    return {"type": "scan", "scan_id": scan_id,
            "image": {"media_type": "image/jpeg", "data": JPEG, "w": 1000, "h": 600},
            "viewport": dict(VIEWPORT), "url": "https://example.com/login",
            "title": "Example login", "hints": HINTS}


def http_get(port: int, path: str) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


# -- slots mode ---------------------------------------------------------------------------

@pytest.mark.slow
def test_slots_mode_selects_the_slot_the_user_looks_at(server):
    c = controller(server, slots=("yes", "no"))
    v = Client(server.url, server.token, role="viewer", mode=None, slots=None, refresh_hz=None)
    try:
        w = c.welcome
        assert (w["role"], w["mode"], w["fake"], w["detect"]) == ("controller", "slots", True,
                                                                  True)
        assert w["max_slots"] == 4 and w["freqs"] == [15.0, 17.0, 12.0, 20.0]
        v.connect(timeout=10)
        assert v.welcome["role"] == "viewer"
        wait_run(c)
        assert c.layout is not None and c.layout["stage"] == "slots"
        assert [t["slot"] for t in c.layout["targets"]] == ["yes", "no"]
        assert [t["freq"] for t in c.layout["targets"]] == [15.0, 17.0]
        assert all(t["rects"] == [] for t in c.layout["targets"])

        t0 = time.monotonic()
        c.gaze("yes")
        first = c.until("selection", timeout=SELECT_TIMEOUT)
        assert first["slot"] == "yes", first
        assert time.monotonic() - t0 < 6.5
        assert first["layout_id"] == c.layout["layout_id"]
        assert set(first["scores"]) == {"yes", "no"} and first["score"] == first["scores"]["yes"]
        assert c.onsets >= 1

        c.gaze("no")
        seen = selections_until(c, "no", timeout=2 * SELECT_TIMEOUT)
        assert set(seen) <= {"yes", "no"} and seen[-1] == "no"
        # the viewer saw the same broadcasts
        assert v.until("selection", timeout=5)["slot"] in ("yes", "no")
        c.gaze(None)
    finally:
        v.close()
        c.close()


# -- point mode ---------------------------------------------------------------------------

@pytest.mark.slow
def test_point_mode_scans_shows_top_k_and_clicks(server):
    c = controller(server, mode="point", slots=None)
    try:
        assert c.welcome["mode"] == "point" and c.welcome["detect"] is True
        req = c.until("scan_request", timeout=RUN_TIMEOUT)
        assert req["settle_ms"] >= 0
        c.send(scan(req["scan_id"]))
        res = c.until("scan_result", timeout=10)
        assert res["ok"] is True and res["scan_id"] == req["scan_id"]
        assert res["n_elements"] == 2 and res["n_candidates"] == 2
        assert res["summary"] == "Login page"
        pick = c.until("layout", timeout=5, pred=lambda m: m["stage"] == "pick")
        assert [t["slot"] for t in pick["targets"]] == ["b1", "b2", "more"]
        assert [t["label"] for t in pick["targets"]][:2] == ["Sign in", "Delete account"]
        assert [t["freq"] for t in pick["targets"]] == [15.0, 17.0, 20.0]
        assert all(t["rects"] for t in pick["targets"])
        wait_run(c)

        # a safe button: one pick clicks it at the center of its own box
        c.gaze("b1")
        click = c.until("click", timeout=SELECT_TIMEOUT)
        c.gaze(None)
        assert click["element"]["label"] == "Sign in" and click["element"]["hint_id"] == 0
        assert click["element"]["risky"] is False
        assert click["x"] == pytest.approx(0.16, abs=1e-3)
        assert click["y"] == pytest.approx(0.2, abs=1e-3)
        assert c.phase["phase"] == "pause"         # held until clicked
        c.send({"type": "clicked", "click_id": click["click_id"], "ok": True,
                "method": "element.click", "detail": ""})
        req2 = c.until("scan_request", timeout=5)
        assert req2["scan_id"] != req["scan_id"]
        c.until("phase", timeout=5, pred=lambda m: m["phase"] == "run")

        # a risky button: the pick shows the confirm stage, and "Do it" clicks
        c.send(scan(req2["scan_id"]))
        assert c.until("scan_result", timeout=10)["ok"] is True
        c.until("layout", timeout=5, pred=lambda m: m["stage"] == "pick")
        wait_run(c)
        c.gaze("b2")
        confirm = c.until("layout", timeout=SELECT_TIMEOUT,
                          pred=lambda m: m["stage"] == "confirm")
        assert [(t["slot"], t["kind"]) for t in confirm["targets"]] == [("confirm", "confirm"),
                                                                       ("back", "back")]
        c.gaze("confirm")
        click2 = c.until("click", timeout=SELECT_TIMEOUT)
        c.gaze(None)
        assert click2["element"]["label"] == "Delete account" and click2["element"]["risky"]
        assert click2["x"] == pytest.approx(0.675, abs=1e-3)
        c.send({"type": "clicked", "click_id": click2["click_id"], "ok": True,
                "method": "element.click", "detail": ""})
        c.until("scan_request", timeout=5)
    finally:
        c.close()

    # The detector got the screenshot, the hints and, after the first click, the history.
    calls = server.stub.calls
    assert len(calls) >= 2
    image = calls[0]["messages"][0]["content"][0]
    assert image["source"]["data"] == JPEG and image["source"]["media_type"] == "image/jpeg"
    assert "Sign in" in calls[-1]["messages"][0]["content"][1]["text"]
    assert all("temperature" not in call for call in calls)
    # --log-scans kept the screenshots in the screen_eval format
    kept = sorted((server.tmp / "scans").glob("*.json"))
    assert len(kept) >= 2
    page = json.loads(kept[0].read_text())
    assert page["image"] == {"w": 1000, "h": 600} and page["viewport"]["w"] == 1000
    assert kept[0].with_suffix(".jpg").read_bytes().startswith(b"\xff\xd8\xff")
    # the JSON-lines log has the session's records and never the token
    log = (server.tmp / "log.jsonl").read_text()
    kinds = {json.loads(line)["kind"] for line in log.splitlines()}
    assert {"connect", "layout", "selection", "click", "scan_result", "point.scan",
            "point.click"} <= kinds
    assert server.token not in log


def test_point_mode_without_a_detector_is_refused_but_slots_work(tmp_path):
    p = app.build_parser()
    args = p.parse_args(["--board", "fake", "--port", "0", "--no-auth", "--no-detect",
                         "--calibrate-s", str(CALIBRATE_S)])
    app.check_arguments(p, args)
    ready: queue.Queue = queue.Queue()
    stop = threading.Event()
    thread = threading.Thread(target=app.serve, args=(args, stop, ready.put), daemon=True)
    thread.start()
    try:
        transport = ready.get(timeout=30)
        c = Client(transport.url, None, mode="point", slots=None)
        try:
            assert c.connect(timeout=10)["detect"] is False
            err = c.until("error", timeout=5)
            assert err["code"] == "detect_unavailable" and err["fatal"] is False
            assert c.phase["phase"] == "wait"
        finally:
            c.close()
    finally:
        stop.set()
        thread.join(15)
    assert not thread.is_alive()


# -- auth ---------------------------------------------------------------------------------

def test_a_wrong_token_is_closed_with_4001(server):
    with pytest.raises(ServerError) as info:
        Client(server.url, "not-the-token").connect(timeout=10)
    assert info.value.code == "bad_token" and info.value.close_code == 4001
    assert "not-the-token" not in str(info.value)


def test_a_second_controller_is_busy_4002_unless_it_takes_over(server):
    first = controller(server)
    try:
        with pytest.raises(ServerError) as info:
            Client(server.url, server.token, takeover=False).connect(timeout=10)
        assert info.value.code == "busy" and info.value.close_code == 4002
        second = controller(server)          # takeover: the first one is replaced
        try:
            with pytest.raises(ServerError) as replaced:
                first.until("selection", timeout=5)
            assert replaced.value.close_code == 4000
        finally:
            second.close()
    finally:
        first.close()


def test_the_token_file_is_private_and_reused(server):
    assert server.token_file.stat().st_mode & 0o777 == 0o600
    assert server.token_file.parent.stat().st_mode & 0o077 == 0
    assert app.read_or_create_token(server.token_file) == (server.token, False)
    assert len(server.token) >= 32


# -- HTTP ---------------------------------------------------------------------------------

def test_http_serves_the_reference_page_the_sdk_and_health(server):
    status, headers, body = http_get(server.port, "/")
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert b"/static/brainsync-client.js" in body and b"/static/app.js" in body
    assert headers["cache-control"] == "no-store"
    status, headers, body = http_get(server.port, "/static/brainsync-client.js")
    assert status == 200 and headers["content-type"].startswith("text/javascript")
    assert body == (ROOT / "clients" / "js" / "brainsync-client.js").read_bytes()
    for name in ("flicker.js", "app.js", "style.css"):
        assert http_get(server.port, f"/static/{name}")[0] == 200
    status, headers, body = http_get(server.port, "/health")
    assert status == 200 and headers["content-type"].startswith("application/json")
    health = json.loads(body)
    assert health["ok"] is True and health["board"] == "streaming"
    assert isinstance(health["controller"], bool) and isinstance(health["viewers"], int)
    assert http_get(server.port, "/static/../app.py")[0] == 404
    assert http_get(server.port, "/nope")[0] == 404


# -- JavaScript clients against the server -------------------------------------------------

def run_node(server, scenario: str) -> list[dict]:
    proc = subprocess.run([NODE, str(ROOT / "tests" / "server_e2e_client.mjs"), str(ROOT),
                           server.url, server.token, scenario],
                          capture_output=True, text=True, timeout=60)
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    assert proc.returncode == 0, (events, proc.stderr[-2000:])
    assert server.token not in proc.stdout + proc.stderr
    return events


@pytest.mark.slow
@needs_node
def test_reference_page_sdk_drives_slots_mode(server):
    events = run_node(server, "slots")
    assert not [e for e in events if e["event"] == "server_error"], events
    assert next(e for e in events if e["event"] == "welcome")["fake"] is True
    assert any(e["event"] == "onset" for e in events)
    sel = [e["slot"] for e in events if e["event"] == "selection"]
    assert sel[0] == "yes" and sel[-1] == "no"


@pytest.mark.slow
@needs_node
def test_extension_agent_drives_point_mode(server):
    events = run_node(server, "point")
    assert not [e for e in events if e["event"] == "server_error"], events
    sent = [e["type"] for e in events if e["event"] == "sent"]
    assert sent[:2] == ["scan", "clicked"]
    click = next(e for e in events if e["event"] == "click")
    assert click["label"] == "Sign in"
    at = next(e for e in events if e["event"] == "clickAt")
    assert (at["x"], at["y"]) == (click["x"], click["y"])


@pytest.mark.slow
def test_python_client_cli_prints_selections(server):
    proc = subprocess.run([PY, "-m", "brainsync.server.client", "--url", server.url,
                           "--token-file", str(server.token_file), "--slots", "yes,no",
                           "--gaze", "yes", "--count", "1", "--takeover", "--json",
                           "--timeout", "10", "--seconds", "30"],
                          cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    sel = [json.loads(x) for x in proc.stdout.splitlines() if x.startswith("{")]
    assert sel and sel[0]["slot"] == "yes"
    assert server.token not in proc.stdout + proc.stderr


# -- command line -------------------------------------------------------------------------

def _env() -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)       # never reach the API from a test
    env["PYTHONUNBUFFERED"] = "1"
    return env


def test_cli_help_exits_0():
    proc = subprocess.run([PY, "-m", "brainsync.server", "--help"], cwd=ROOT, env=_env(),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    for flag in ("--board", "--token-file", "--no-auth", "--freqs", "--calibrate-s", "--origin",
                 "--no-detect", "--env-file", "--min-p-button", "--log", "--log-scans"):
        assert flag in proc.stdout


@pytest.mark.parametrize("argv", [
    ["--freqs", "15"],
    ["--freqs", "15,15"],
    ["--freqs", "15,abc"],
    ["--port", "70000"],
    ["--no-auth", "--host", "0.0.0.0"],
    ["--min-p-button", "1.5"],
    ["--calibrate-s", "-1"],
    ["--board", "muse"],
    ["--board", "fake", "--serial-port", "/dev/x"],
    ["--no-auth", "--token-file", "x"],
    ["--board", "fake", "--no-auth", "--calibrate-s", "1"],     # shorter than two windows
])
def test_cli_bad_arguments_exit_2(argv, tmp_path):
    proc = subprocess.run([PY, "-m", "brainsync.server", "--port", "0", *argv], cwd=tmp_path,
                          env={**_env(), "PYTHONPATH": str(ROOT)}, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert "error" in proc.stderr


def test_cli_bad_env_file_exits_2(tmp_path):
    env_file = tmp_path / "bad.env"
    env_file.write_text("THIS IS NOT A LINE\n")
    proc = subprocess.run([PY, "-m", "brainsync.server", "--board", "fake", "--no-auth",
                           "--port", "0", "--env-file", str(env_file)], cwd=ROOT, env=_env(),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and "line 1" in proc.stderr
    assert "THIS IS NOT" not in proc.stderr


@pytest.mark.slow
def test_cli_runs_serves_and_stops_on_sigterm_with_phase_stop(tmp_path):
    proc = subprocess.Popen([PY, "-m", "brainsync.server", "--board", "fake", "--no-auth",
                             "--port", "0", "--log", str(tmp_path / "log.jsonl")],
                            cwd=ROOT, env=_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        m = re.search(r"ws://127\.0\.0\.1:(\d+)/ws", line)
        assert m, line
        port = int(m.group(1))
        assert proc.stdout.readline().strip() == f"open http://127.0.0.1:{port}/"
        assert json.loads(http_get(port, "/health")[2])["ok"] is True
        assert http_get(port, "/")[0] == 200
        v = Client(f"ws://127.0.0.1:{port}/ws", None, role="viewer", mode=None, slots=None,
                   refresh_hz=None)
        v.connect(timeout=10)
        proc.send_signal(signal.SIGTERM)
        stop = v.until("phase", timeout=10, pred=lambda m: m["phase"] == "stop")
        assert stop["flicker"] is False
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    records = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert records[-1]["kind"] == "phase" and records[-1]["phase"] == "stop"


def test_cli_port_in_use_exits_1(server):
    proc = subprocess.run([PY, "-m", "brainsync.server", "--board", "fake", "--no-auth",
                           "--port", str(server.port)], cwd=ROOT, env=_env(),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1 and "--port" in proc.stderr


# -- pieces -------------------------------------------------------------------------------

def test_token_file_is_created_0600_with_a_urlsafe_token(tmp_path):
    path = tmp_path / "a" / "b" / "token"
    token, created = app.read_or_create_token(path)
    assert created and re.fullmatch(r"[A-Za-z0-9_-]{32}", token)
    assert path.stat().st_mode & 0o777 == 0o600
    assert app.read_or_create_token(path) == (token, False)
    path.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        app.read_or_create_token(path)


def test_browser_url_puts_the_token_in_the_fragment():
    assert app.browser_url("127.0.0.1", 8765, "abc") == "http://127.0.0.1:8765/#token=abc"
    assert app.browser_url("0.0.0.0", 8765, None) == "http://127.0.0.1:8765/"
    assert app.browser_url("::1", 9, None) == "http://[::1]:9/"


def test_static_files_cover_the_reference_page():
    files = app.static_files()
    assert set(files) == {"index.html", "style.css", "app.js", "flicker.js",
                          "brainsync-client.js"}
    html = files["index.html"].read_text()
    for src in re.findall(r'(?:src|href)="/static/([^"]+)"', html):
        assert src in files


def test_json_log_writes_plain_json_lines(tmp_path):
    log = app.JsonLog(tmp_path / "x.jsonl")
    log("selection", {"t": 1.0, "score": float("nan"), "slots": ("a", "b")})
    log.prefixed("point.")("scan", {"n": 3})
    log.close()
    log("after_close", {})
    lines = [json.loads(x) for x in (tmp_path / "x.jsonl").read_text().splitlines()]
    assert lines == [{"kind": "selection", "t": 1.0, "score": None, "slots": ["a", "b"]},
                     {"kind": "point.scan", "n": 3, "t": lines[1]["t"]}]
    assert (tmp_path / "x.jsonl").stat().st_mode & 0o777 == 0o600


def test_parse_freqs():
    assert app.parse_freqs("15,17,12,20") == (15.0, 17.0, 12.0, 20.0)
    for bad in ("15", "15,15", "0,15", "15,200", "a,b", "15,nan"):
        with pytest.raises(ValueError, match="--freqs"):
            app.parse_freqs(bad)


def test_parse_refresh_rates():
    assert app.parse_refresh_rates("60,90,120,144") == (60.0, 90.0, 120.0, 144.0)
    assert app.parse_refresh_rates("120") == (120.0,)
    assert app.parse_refresh_rates(" 144 ,60") == (144.0, 60.0)
    for bad in ("", "60,", "fast", "60,60", "0", "-60", "60,nan", "inf", "5000"):
        with pytest.raises(ValueError, match="--refresh-rates"):
            app.parse_refresh_rates(bad)


def test_refresh_rates_flag_defaults_to_60_90_120_144_and_is_checked(capsys):
    p = app.build_parser()
    args = p.parse_args(["--board", "fake", "--no-auth"])
    app.check_arguments(p, args)
    assert args.refresh_rates == (60.0, 90.0, 120.0, 144.0) and not args.ignore_refresh
    args = p.parse_args(["--board", "fake", "--no-auth", "--refresh-rates", "120,144"])
    app.check_arguments(p, args)
    assert args.refresh_rates == (120.0, 144.0)
    assert "--refresh-rates" in p.format_help()
    for argv, why in [(["--refresh-rates", "60,abc"], "separated by commas"),
                      (["--refresh-rates", "60,60"], "must not repeat"),
                      (["--refresh-rates", "0,60"], "must lie in"),
                      (["--refresh-rates", "40,60"], "40 cannot show the 20 Hz"),
                      (["--freqs", "15,30", "--refresh-rates", "60,120"], "60 cannot show the 30")]:
        args = p.parse_args(["--board", "fake", "--no-auth", *argv])
        with pytest.raises(SystemExit) as info:
            app.check_arguments(p, args)
        assert info.value.code == 2
        err = capsys.readouterr().err
        assert "--refresh-rates" in err and why in err, err


def test_refresh_rates_reach_the_welcome(tmp_path):
    p = app.build_parser()
    args = p.parse_args(["--board", "fake", "--port", "0", "--no-auth", "--no-detect",
                         "--calibrate-s", str(CALIBRATE_S), "--refresh-rates", "120,144"])
    app.check_arguments(p, args)
    ready: queue.Queue = queue.Queue()
    stop = threading.Event()
    thread = threading.Thread(target=app.serve, args=(args, stop, ready.put), daemon=True)
    thread.start()
    try:
        transport = ready.get(timeout=30)
        c = Client(transport.url, None, slots=("yes", "no"), refresh_hz=60.0)
        try:
            w = c.connect(timeout=10)
            assert w["refresh_hz"] == 120.0 and w["refresh_rates"] == [120.0, 144.0]
            err = c.until("error", timeout=5)
            assert err["code"] == "refresh" and "one of 120 or 144 Hz" in err["message"]
        finally:
            c.close()
        c = Client(transport.url, None, slots=("yes", "no"), refresh_hz=143.8)
        try:
            c.connect(timeout=10)
            c.until("phase", timeout=5, pred=lambda m: m["phase"] == "calibrate")
        finally:
            c.close()
    finally:
        stop.set()
        thread.join(15)
    assert not thread.is_alive()


# -- deployment ---------------------------------------------------------------------------

def test_the_jetson_unit_runs_valid_arguments():
    import configparser
    import shlex

    text = (ROOT / "deploy" / "jetson" / "brainsync-server.service").read_text()
    unit = configparser.ConfigParser(interpolation=None, strict=True)
    unit.optionxform = str
    unit.read_string(text.replace("\\\n", " "))
    service = unit["Service"]
    argv = shlex.split(service["ExecStart"])
    assert argv[:3] == ["/opt/brainsync/.venv/bin/python", "-m", "brainsync.server"]
    p = app.build_parser()
    args = p.parse_args(argv[3:])
    app.check_arguments(p, args)              # exits 2 on a bad flag
    assert args.host == "127.0.0.1" and not args.no_auth
    assert args.token_file.startswith("/var/lib/brainsync/")
    assert service["KillSignal"] == "SIGTERM" and service["RestartPreventExitStatus"] == "2"
    assert "dialout" in service["SupplementaryGroups"]
    readme = (ROOT / "deploy" / "jetson" / "README.md").read_text()
    assert "tailscale serve --help" in readme and "aarch64" in readme
