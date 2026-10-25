"""app/remote.py and app/tunnel.py: a phone through the tunnel needs the access key; the decoder
socket stays local; the laptop itself needs nothing."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.remote import COOKIE, FORBIDDEN, is_remote
from app.server import create_app
from app.tunnel import tunnel_url

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


def test_no_key_means_no_gate(tmp_path):
    with TestClient(create_app(log_dir=tmp_path), headers=TUNNEL) as c:
        assert c.get("/").status_code == 200


def test_the_laptop_page_gets_the_phone_link(app):
    app.state.phone_url = f"https://abc.trycloudflare.com/?key={KEY}"
    with TestClient(app, client=LOCAL) as c:
        assert c.get("/api/config").json()["phone_url"] == app.state.phone_url


def test_shutdown_hooks_run_when_the_server_stops(app):
    stopped = []
    app.state.on_shutdown.append(lambda: stopped.append(True))
    with TestClient(app, client=LOCAL):
        assert not stopped
    assert stopped == [True]


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
    ({"client": ("127.0.0.1", 1), "headers": [(b"X-Forwarded-For", b"1.2.3.4")]}, True),
    ({"client": ("10.0.0.5", 1), "headers": []}, True),
    ({"headers": []}, True),  # no client address: not provably local
])
def test_is_remote(scope, remote):
    assert is_remote(scope) is remote


def test_the_tunnel_address_is_read_from_cloudflared_output():
    line = ("2026-10-24T21:03:11Z INF |  https://ripe-banana-quiet-lake.trycloudflare.com"
            "                     |")
    assert tunnel_url(line) == "https://ripe-banana-quiet-lake.trycloudflare.com"
    assert tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None
