"""app/remote.py and app/tunnel.py: a phone through the tunnel needs the access key; the decoder
socket stays local; the laptop itself needs nothing."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.server as server
from app.remote import COOKIE, FORBIDDEN, is_plainly_remote, is_remote, key_problem, new_key
from app.server import app_link, create_app, phone_link, run_shutdown, watch_tunnel
from app.tunnel import stop_process, tunnel_url

KEY = "test-key-not-secret"
LOCAL = ("127.0.0.1", 50000)
TUNNEL = {"cf-connecting-ip": "203.0.113.7", "x-forwarded-proto": "https"}  # what cloudflared adds


@pytest.fixture
def app(tmp_path):
    return create_app(log_dir=tmp_path, access_key=KEY)


def test_the_laptop_needs_no_key(app):
    with TestClient(app, client=LOCAL) as c:
        assert c.get("/").status_code == 200
        with c.websocket_connect("/ws/ui") as ws:
            assert ws.receive_json()["type"]


def test_the_tunnel_without_the_key_gets_nothing(app):
    with TestClient(app, client=LOCAL, headers=TUNNEL) as c:  # cloudflared connects from 127.0.0.1
        for path in ("/", "/app.js", "/api/config", "/api/health", "/api/profile"):
            assert c.get(path).status_code == 403, path
        assert c.get("/?key=wrong").status_code == 403
        with pytest.raises(WebSocketDisconnect) as closed, c.websocket_connect("/ws/ui") as ws:
            ws.receive_json()
        assert closed.value.code == FORBIDDEN


def test_the_phone_link_sets_a_cookie_that_opens_the_rest(app):
    with TestClient(app, client=LOCAL, headers=TUNNEL) as c:
        first = c.get(f"/?key={KEY}")
        assert first.status_code == 200
        cookie = first.headers["set-cookie"]
        assert f"{COOKIE}={KEY}" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert "Secure" in cookie  # the tunnel is https
        c.cookies.set(COOKIE, KEY)
        assert c.get("/app.js").status_code == 200
        assert c.get("/api/config").json()["phone_url"] == ""  # the link stays on the laptop
        with c.websocket_connect("/ws/ui") as ws:
            assert ws.receive_json()["type"]


def test_the_decoder_socket_is_never_remote(app):
    with TestClient(app, client=LOCAL, headers=TUNNEL, cookies={COOKIE: KEY}) as c:
        with pytest.raises(WebSocketDisconnect) as closed, c.websocket_connect(f"/ws/decoder?key={KEY}") as ws:
            ws.receive_json()
        assert closed.value.code == FORBIDDEN


def test_the_local_bridge_connects_as_before(app):
    with TestClient(app, client=LOCAL) as c, c.websocket_connect("/ws/decoder"):
        pass


def test_another_address_on_the_network_is_remote_too(app):
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        assert c.get("/").status_code == 403
        assert c.get(f"/?key={KEY}").status_code == 200


def test_without_a_key_this_computer_gets_in(tmp_path):
    with TestClient(create_app(log_dir=tmp_path), client=LOCAL) as c, c.websocket_connect("/ws/ui") as ws:
        assert c.get("/").status_code == 200
        assert ws.receive_json()["type"]
    with TestClient(create_app(log_dir=tmp_path)) as c:  # Starlette's "testclient" is no address
        assert c.get("/").status_code == 200


def test_without_a_key_nothing_remote_gets_in(tmp_path):
    """An orphaned tunnel (or a proxy) in front of a plain local server reaches nothing."""
    with TestClient(create_app(log_dir=tmp_path), client=LOCAL, headers=TUNNEL) as c:
        assert c.get("/").status_code == 403
        assert c.get("/api/health?key=anything").status_code == 403
        for path in ("/ws/ui", "/ws/decoder"):
            with pytest.raises(WebSocketDisconnect) as closed, c.websocket_connect(path) as ws:
                ws.receive_json()
            assert closed.value.code == FORBIDDEN, path
    with TestClient(create_app(log_dir=tmp_path), client=("192.168.1.20", 50000)) as c:
        assert c.get("/").status_code == 403


def test_the_laptop_page_gets_the_phone_link(app):
    app.state.phone_url = f"https://abc.trycloudflare.com/?key={KEY}"
    with TestClient(app, client=LOCAL) as c:
        assert c.get("/api/config").json()["phone_url"] == app.state.phone_url


def test_shutdown_hooks_run_once_off_the_event_loop(app):
    stopped = []

    def hook() -> None:
        try:
            asyncio.get_running_loop()
            stopped.append("on the loop")  # would block every connection while a child exits
        except RuntimeError:
            stopped.append("in a thread")

    app.state.on_shutdown.append(hook)
    with TestClient(app, client=LOCAL):
        assert not stopped
    assert stopped == ["in a thread"]
    run_shutdown(app)  # main's finally after the lifespan: nothing runs twice
    assert stopped == ["in a thread"]


def test_the_iphone_app_connects_with_the_key_in_its_socket_url(app):
    with TestClient(app, client=LOCAL, headers=TUNNEL) as c:  # no cookie: the app sends ?key=
        with c.websocket_connect(f"/ws/ui?key={KEY}&client=ios") as ws:
            assert ws.receive_json()["type"]
            assert c.get(f"/api/health?key={KEY}").json()["ui_client"] == "ios"


def test_the_iphone_app_without_the_key_is_closed_with_4403(app):
    with TestClient(app, client=LOCAL, headers=TUNNEL) as c:
        with pytest.raises(WebSocketDisconnect) as closed, c.websocket_connect("/ws/ui?client=ios") as ws:
            ws.receive_json()
        assert closed.value.code == FORBIDDEN
        assert c.get("/api/config?key=wrong").status_code == 403


def test_the_iphone_app_fetches_audio_and_config_with_the_key(app):
    app.state.tts._remember("abc123", (b"ID3fake-mp3", "audio/mpeg"))
    app.state.phone_url = f"https://abc.trycloudflare.com/?key={KEY}"
    with TestClient(app, client=LOCAL, headers=TUNNEL) as c:
        assert c.get("/audio/abc123").status_code == 403
        r = c.get(f"/audio/abc123?key={KEY}")
        assert r.status_code == 200 and r.content == b"ID3fake-mp3"
        config = c.get(f"/api/config?key={KEY}").json()
        assert config["phone_url"] == "" and config["app_link"] == ""  # never to a remote request


def test_the_laptop_page_gets_the_iphone_app_link(app):
    app.state.phone_url = f"https://abc.trycloudflare.com/?key={KEY}"
    with TestClient(app, client=LOCAL) as c:
        assert c.get("/api/config").json()["app_link"] == (
            f"brainsync://pair?url=https%3A%2F%2Fabc.trycloudflare.com&key={KEY}")
        app.state.phone_url = ""
        assert c.get("/api/config").json()["app_link"] == ""


def test_a_lan_phone_link_gets_an_app_link_too(app):
    app.state.phone_url = f"http://192.168.1.5:8000/?key={KEY}"
    with TestClient(app, client=LOCAL) as c:
        assert c.get("/api/config").json()["app_link"] == (
            f"brainsync://pair?url=http%3A%2F%2F192.168.1.5%3A8000&key={KEY}")


@pytest.mark.parametrize("scope, remote", [
    ({"client": ("127.0.0.1", 1), "headers": []}, False),
    ({"client": ("::1", 1), "headers": []}, False),
    ({"client": ("::ffff:127.0.0.1", 1), "headers": []}, False),  # server.host "::" (dual stack)
    ({"client": ("127.0.0.2", 1), "headers": []}, False),
    ({"client": ("localhost", 1), "headers": []}, False),
    ({"client": ("127.0.0.1", 1), "headers": [(b"X-Forwarded-For", b"1.2.3.4")]}, True),
    ({"client": ("10.0.0.5", 1), "headers": []}, True),
    ({"client": ("::ffff:10.0.0.5", 1), "headers": []}, True),
    ({"client": ("testclient", 1), "headers": []}, True),  # with a key: not provably local
    ({"headers": []}, True),  # no client address: not provably local
])
def test_is_remote(scope, remote):
    assert is_remote(scope) is remote


@pytest.mark.parametrize("scope, refused", [
    ({"client": ("127.0.0.1", 1), "headers": []}, False),
    ({"client": ("::ffff:127.0.0.1", 1), "headers": []}, False),
    ({"client": ("testclient", 1), "headers": []}, False),
    ({"headers": []}, False),
    ({"client": ("127.0.0.1", 1), "headers": [(b"cf-ray", b"abc")]}, True),
    ({"client": ("127.0.0.1", 1), "headers": [(b"forwarded", b"for=1.2.3.4")]}, True),
    ({"client": ("testclient", 1), "headers": [(b"x-forwarded-for", b"1.2.3.4")]}, True),
    ({"client": ("192.168.1.20", 1), "headers": []}, True),
    ({"client": ("::ffff:10.0.0.5", 1), "headers": []}, True),
])
def test_what_a_server_without_a_key_refuses(scope, refused):
    assert is_plainly_remote(scope) is refused


def test_a_key_with_url_characters_round_trips_through_the_links(tmp_path):
    key = "a+b&c=d/e%f"
    assert key_problem(key) is None  # all cookie characters
    link = phone_link("https://abc.trycloudflare.com", key)
    assert parse_qs(urlsplit(link).query) == {"key": [key]}
    assert parse_qs(urlsplit(app_link(link)).query)["key"] == [key]
    app = create_app(log_dir=tmp_path, access_key=key)
    with TestClient(app, client=LOCAL, headers=TUNNEL) as c:
        assert c.get(f"/?key={key}").status_code == 403  # unencoded, "+" reads as a space
        first = c.get(f"/?{urlsplit(link).query}")
        assert first.status_code == 200
        assert f"{COOKIE}={key};" in first.headers["set-cookie"]
        c.cookies.set(COOKIE, key)
        assert c.get("/app.js").status_code == 200


@pytest.mark.parametrize("key", ["a b", "a;b", 'a"b', "a,b", "a\\b", "caf\u00e9", "a\nb", ""])
def test_a_key_that_cannot_be_a_cookie_is_rejected(key):
    assert key_problem(key)


def test_generated_keys_are_valid():
    assert all(key_problem(new_key()) is None for _ in range(50))


# -- main(): the tunnel and the fake decoder (no real cloudflared, no real uvicorn) -------------

TUNNEL_URL = "https://ripe-banana-quiet-lake.trycloudflare.com"


class FakeTunnel:
    def __init__(self, events: list, port: int, binary: str) -> None:
        self.events = events
        self.address = threading.Event()  # set by the test: cloudflared printed its address
        self.stopped = False
        events.append("tunnel_start")

    def wait(self, timeout: float) -> str | None:
        got = self.address.wait(min(timeout, 5.0))
        return TUNNEL_URL if got and not self.stopped else None

    def stop(self) -> None:
        self.events.append("tunnel_stop")
        self.stopped = True
        self.address.set()  # ends watch_tunnel's wait, so no thread outlives the test


class FakeProc:
    def __init__(self, events: list, *args, **kwargs) -> None:
        self.events = events
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.events.append("fake_terminate")

    def kill(self) -> None:
        self.events.append("fake_kill")

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("fake_wait")
        self.returncode = 0
        return 0


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    """main() with a fake Tunnel and a recording create_app; returns (events, tunnels, built)."""
    events: list = []
    tunnels: list[FakeTunnel] = []
    built: list = []
    real_create_app = server.create_app

    def make_tunnel(port, binary):
        tunnels.append(FakeTunnel(events, port, binary))
        return tunnels[-1]

    def recording_create_app(*args, **kwargs):
        events.append("create_app")
        app = real_create_app(*args, **{**kwargs, "log_dir": tmp_path, "profile_path": None,
                                         "tts_cache_dir": None})
        built.append(app)
        return app

    monkeypatch.setattr(server, "load_env", lambda: [])
    for name in ("CARTESIA_API_KEY", "ANTHROPIC_API_KEY", "BRAINSYNC_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server, "find_cloudflared", lambda: "/fake/cloudflared")
    monkeypatch.setattr(server, "Tunnel", make_tunnel)
    monkeypatch.setattr(server, "create_app", recording_create_app)
    return events, tunnels, built


def test_main_listens_at_once_and_stops_the_tunnel_once(run_main, monkeypatch):
    events, tunnels, built = run_main

    def serve(app, **kwargs):
        assert events == ["create_app", "tunnel_start"]  # the tunnel only once the app exists
        assert app.state.phone_url == ""  # listening before cloudflared has its address
        with TestClient(app, client=LOCAL):
            tunnels[0].address.set()
            deadline = time.monotonic() + 5.0
            while not app.state.phone_url and time.monotonic() < deadline:
                time.sleep(0.01)
            assert app.state.phone_url.startswith(f"{TUNNEL_URL}/?key=")
        # The lifespan stopped the tunnel; main's finally must not stop it again.

    monkeypatch.setattr("uvicorn.run", serve)
    server.main(["--tunnel"])
    assert events == ["create_app", "tunnel_start", "tunnel_stop"]
    key = parse_qs(urlsplit(built[0].state.phone_url).query)["key"][0]
    assert key_problem(key) is None


def test_main_starts_no_tunnel_when_the_app_cannot_be_built(run_main, monkeypatch):
    events, tunnels, _ = run_main

    def broken(*args, **kwargs):
        events.append("create_app")
        raise RuntimeError("bad profile")

    monkeypatch.setattr(server, "create_app", broken)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: pytest.fail("must not serve"))
    with pytest.raises(RuntimeError):
        server.main(["--tunnel"])
    assert events == ["create_app"] and not tunnels


def test_main_stops_every_child_when_the_server_cannot_start(run_main, monkeypatch):
    events, _, _ = run_main
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: FakeProc(events, *a, **kw))

    def address_in_use(app, **kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr("uvicorn.run", address_in_use)
    with pytest.raises(OSError):
        server.main(["--tunnel", "--fake-decoder"])
    assert events == ["create_app", "tunnel_start", "tunnel_stop", "fake_terminate", "fake_wait"]


def test_main_rejects_a_key_that_cannot_be_a_cookie(run_main, monkeypatch):
    events, _, _ = run_main
    monkeypatch.setenv("BRAINSYNC_ACCESS_KEY", "open sesame;x")
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("served despite a bad key"))
    with pytest.raises(SystemExit) as stopped:
        server.main(["--tunnel"])
    assert "BRAINSYNC_ACCESS_KEY" in str(stopped.value.code)
    assert "open sesame" not in str(stopped.value.code)  # never shows the key
    assert events == []  # nothing built, nothing started


def test_a_tunnel_without_an_address_leaves_no_phone_link(app, capsys):
    tunnel = FakeTunnel([], 8000, "cloudflared")  # address never set
    watch_tunnel(app, tunnel, KEY, wait_s=0.01)
    assert app.state.phone_url == ""
    assert "no address within 0.01 s" in capsys.readouterr().err


def test_stop_process_waits_and_kills_a_child_that_hangs():
    events: list = []

    class Hangs(FakeProc):
        def wait(self, timeout=None):
            events.append("fake_wait")
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    stop_process(Hangs(events), timeout_s=0.01)
    assert events == ["fake_terminate", "fake_wait", "fake_kill", "fake_wait"]
    exited: list = []
    done = FakeProc(exited)
    done.returncode = 0  # already exited: no terminate, still reaped
    stop_process(done)
    assert exited == ["fake_wait"]


def test_the_tunnel_address_is_read_from_cloudflared_output():
    line = ("2026-10-24T21:03:11Z INF |  https://ripe-banana-quiet-lake.trycloudflare.com"
            "                     |")
    assert tunnel_url(line) == "https://ripe-banana-quiet-lake.trycloudflare.com"
    assert tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None


@pytest.mark.parametrize("origin, allowed", [
    (None, True),  # the bridge and the fake decoder send no Origin
    ("http://127.0.0.1:8000", True),  # the server's own page
    ("https://evil.example", False),  # any other site open in the laptop's browser
    ("http://127.0.0.1:9999", False),  # another local port is another origin
])
def test_a_browser_socket_must_come_from_the_servers_own_page(tmp_path, origin, allowed):
    app = create_app(log_dir=tmp_path)
    headers = {"host": "127.0.0.1:8000", **({"origin": origin} if origin else {})}
    with TestClient(app, client=LOCAL, headers=headers) as c:
        if allowed:
            with c.websocket_connect("/ws/ui") as ws:
                assert ws.receive_json()["type"]
        else:
            with pytest.raises(WebSocketDisconnect) as closed, c.websocket_connect("/ws/ui") as ws:
                ws.receive_json()
            assert closed.value.code == FORBIDDEN


def test_the_phones_page_through_the_tunnel_passes_the_origin_check(app):
    headers = {**TUNNEL, "host": "abc.trycloudflare.com", "origin": "https://abc.trycloudflare.com"}
    with TestClient(app, client=LOCAL, headers=headers, cookies={COOKIE: KEY}) as c:
        with c.websocket_connect("/ws/ui") as ws:
            assert ws.receive_json()["type"]
