"""brainsync.brainmap server and CLI: static files without traversal, the SSE order (hello,
latest status, then frames), fan-out, dropping a stalled client without blocking publish(),
the DNS-rebinding Host check (also when exposed, decided from the bound address), prompt
stop(), argument checks, frame coalescing, board errors and Ctrl-C, the end-of-run grace
period, the fake demo's rest-then-tones schedule, and (when stream.py exists) one short
end-to-end run on the fake board."""
from __future__ import annotations

import collections
import http.client
import json
import math
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from brainsync.brainmap import __main__ as cli
from brainsync.brainmap import server as srv
from brainsync.brainmap.__main__ import main
from brainsync.brainmap.server import MapServer, encode, host_name, is_ip_literal

REPO = Path(__file__).resolve().parent.parent
SECRET = "TOPSECRET-not-for-the-page"
HELLO = {"type": "hello", "format": 1, "source": "fake", "channels": ["O1", "O2"],
         "label": "Your EEG · 8 electrodes · scalp map, not a brain scan"}


def frame(seq: int, **extra) -> dict:
    return {"type": "frame", "seq": seq, "t": 1.0e9 + seq / 4, **extra}


def status(state: str) -> dict:
    return {"type": "status", "state": state, "text": state}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    page = tmp_path / "page"
    (page / "sub").mkdir(parents=True)
    (page / "index.html").write_text("<!doctype html><title>map</title>")
    (page / "map.js").write_text("var BrainSyncMap = {};")
    (page / "util.mjs").write_text("export const x = 1;")
    (page / "style.css").write_text("body{}")
    (page / "notes.md").write_text("not a served type")
    (page / ".hidden.js").write_text(SECRET)
    (page / "sub" / "extra.js").write_text("var extra = 1;")
    (tmp_path / "secret.txt").write_text(SECRET)
    (tmp_path / "secret.js").write_text(SECRET)
    (page / "link.js").symlink_to(tmp_path / "secret.js")
    return page


@pytest.fixture
def server(root: Path):
    with MapServer(port=0, root=root) as s:
        yield s


def get(s: MapServer, path: str, host: str | None = "127.0.0.1") -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type", ""), resp.read()
    finally:
        conn.close()


def raw(s: MapServer, request_line: str, host: str | None = "127.0.0.1") -> bytes:
    """Send a request byte for byte (no client-side normalisation) and return the reply."""
    with socket.create_connection(("127.0.0.1", s.port), timeout=5) as sock:
        head = f"{request_line}\r\n" + (f"Host: {host}\r\n" if host is not None else "")
        sock.sendall((head + "\r\n").encode("latin-1"))
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.005)


class Events:
    """A reading /events client (http.client), parsing one "data:" message at a time."""

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        self.conn.putrequest("GET", "/events", skip_host=True, skip_accept_encoding=True)
        self.conn.putheader("Host", host)
        self.conn.endheaders()
        self.resp = self.conn.getresponse()
        self.comments: list[str] = []

    def next(self) -> dict | None:
        """The next message (None at the end of the stream); comments go to .comments."""
        data = []
        while True:
            line = self.resp.readline()
            if not line:
                return None
            line = line.decode("utf-8").rstrip("\n")
            if line.startswith(":"):
                self.comments.append(line)
            elif line.startswith("data: "):
                data.append(line[len("data: "):])
            elif line == "" and data:
                return json.loads("\n".join(data))

    def take(self, n: int) -> list[dict]:
        out = [self.next() for _ in range(n)]
        assert None not in out, f"stream ended early: {out}"
        return out

    def close(self) -> None:
        self.resp.close()     # the response holds the socket open after conn.close() alone
        self.conn.close()


# ---------------------------------------------------------------- static files

def test_index_is_served_at_root(server: MapServer, root: Path) -> None:
    code, ctype, body = get(server, "/")
    assert code == 200
    assert ctype.startswith("text/html")
    assert body == (root / "index.html").read_bytes()
    assert get(server, "/index.html")[2] == body


@pytest.mark.parametrize("path, ctype", [("/map.js", "text/javascript"),
                                         ("/util.mjs", "text/javascript"),
                                         ("/style.css", "text/css"),
                                         ("/sub/extra.js", "text/javascript")])
def test_static_files_have_their_content_type(server: MapServer, path: str, ctype: str) -> None:
    code, got, body = get(server, path)
    assert code == 200
    assert got.split(";")[0] == ctype
    assert body


@pytest.mark.parametrize("path", ["/notes.md", "/sub", "/sub/", "/missing.js", "/.hidden.js",
                                  "/%2ehidden.js"])
def test_unknown_types_directories_and_hidden_files_are_not_found(server: MapServer,
                                                                  path: str) -> None:
    code, _, body = get(server, path)
    assert code == 404
    assert SECRET.encode() not in body


TRAVERSALS = ["/../secret.txt", "/../secret.js", "/%2e%2e/secret.js", "/%2E%2E/secret.js",
              "/sub/../../secret.js", "/%2e%2e%2fsecret.js", "/..%2fsecret.js",
              "/sub/%2e%2e/%2e%2e/secret.js", "/..\\secret.js", "/sub%5c..%5c..%5csecret.js",
              "//etc/passwd", "/%2fetc%2fpasswd", "/link.js", "/sub/./../map.js",
              "/map.js%00.js"]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_traversal_never_leaves_root(server: MapServer, path: str) -> None:
    reply = raw(server, f"GET {path} HTTP/1.0")
    code = int(reply.split(b" ", 2)[1])
    assert code in (400, 403, 404)
    assert SECRET.encode() not in reply


def test_absolute_paths_never_leave_root(server: MapServer, root: Path) -> None:
    secret = (root.parent / "secret.js").resolve()
    for target in (f"/{secret}", f"http://127.0.0.1:{server.port}/../secret.js",
                   f"http://127.0.0.1:{server.port}{secret}"):
        reply = raw(server, f"GET {target} HTTP/1.0")
        assert int(reply.split(b" ", 2)[1]) in (400, 403, 404), target
        assert SECRET.encode() not in reply


def test_missing_root_gives_404_not_an_error(tmp_path: Path) -> None:
    with MapServer(port=0, root=tmp_path / "nope") as s:
        assert get(s, "/")[0] == 404


# ---------------------------------------------------------------- Host check

@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8140", "localhost", "LOCALHOST:1",
                                  "[::1]", "[::1]:8140"])
def test_loopback_host_headers_are_accepted(server: MapServer, host: str) -> None:
    assert get(server, "/", host=host)[0] == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8140", "127.0.0.1.evil.example",
                                  "localhost.evil.example", "0.0.0.0", "192.168.1.5:8140", "",
                                  None])
def test_foreign_host_headers_are_refused(server: MapServer, host: str | None) -> None:
    assert get(server, "/", host=host)[0] == 403
    if host is not None:
        assert get(server, "/events", host=host)[0] == 403
    assert server.client_count == 0


def test_allowed_hosts_extend_the_loopback_names(root: Path) -> None:
    with MapServer(port=0, root=root, allowed_hosts=["jetson.local"]) as s:
        assert get(s, "/", host="jetson.local:8140")[0] == 200
        assert get(s, "/", host="localhost")[0] == 200
        assert get(s, "/", host="evil.example")[0] == 403


@pytest.mark.parametrize("alias", ["127.1", "localhost", "localhost.", "::1"])
def test_loopback_aliases_keep_the_strict_check(root: Path, alias: str) -> None:
    """The guard follows the bound address: "127.1" once turned it off, since it is not
    written as a loopback IP, though it binds to 127.0.0.1."""
    try:
        s = MapServer(alias, 0, root)
    except OSError as exc:
        pytest.skip(f"cannot bind {alias}: {exc}")
    with s:
        assert not s.exposed
        bound = s._httpd.server_address[0]
        for host, want in (("evil.example", 403), ("192.168.1.5", 403), ("localhost", 200),
                           (srv._url_name(bound), 200)):
            conn = http.client.HTTPConnection(bound, s.port, timeout=5)
            try:
                conn.putrequest("GET", "/", skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", host)
                conn.endheaders()
                assert conn.getresponse().status == want, host
            finally:
                conn.close()


def test_exposure_is_read_from_the_bound_address(root: Path, monkeypatch) -> None:
    seen = []
    real = srv._bound_loopback
    monkeypatch.setattr(srv, "_bound_loopback", lambda a: seen.append(a) or real(a))
    with MapServer("localhost", 0, root) as s:
        assert not s.exposed
    assert seen == ["127.0.0.1"]
    assert real("127.0.0.2") and real("::1") and real("127.255.0.9")
    assert not any(real(a) for a in ("0.0.0.0", "::", "", "192.168.1.5", "fe80::1%en0",
                                     "localhost"))


@pytest.fixture
def exposed(root: Path, monkeypatch):
    """A server that believes it is bound to the network (binding 0.0.0.0 for real would
    trip the macOS firewall prompt)."""
    monkeypatch.setattr(srv, "_bound_loopback", lambda a: False)
    with MapServer(port=0, root=root, allowed_hosts=["Jetson.local:8140", ""]) as s:
        yield s


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost:8140", "[::1]", "jetson.local",
                                  "JETSON.LOCAL:8140", "192.168.1.20", "192.168.1.20:8140",
                                  "10.0.0.7", "0.0.0.0", "[fe80::1]:8140", "[fd00::5]"])
def test_an_exposed_server_accepts_loopback_allowed_and_ip_hosts(exposed: MapServer,
                                                                 host: str) -> None:
    assert exposed.exposed
    assert get(exposed, "/", host=host)[0] == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8140", "192.168.1.20.evil.example",
                                  "jetson.local.evil.example", "1.2.3.4.nip.io", "fe80::1", "[evil]",
                                  "", None])
def test_an_exposed_server_still_refuses_dns_names(exposed: MapServer, host: str | None) -> None:
    code, _, body = get(exposed, "/", host=host)
    assert code == 403
    assert b"--allow-host" in body
    if host is not None:
        assert get(exposed, "/events", host=host)[0] == 403
    assert exposed.client_count == 0


def test_accepted_hosts_lists_the_names(exposed: MapServer, root: Path) -> None:
    assert exposed.accepted_hosts == ("127.0.0.1", "[::1]", "jetson.local", "localhost")


def test_is_ip_literal() -> None:
    for yes in ("1.2.3.4", "0.0.0.0", "[::1]", "[fe80::1%25en0]"):
        assert is_ip_literal(yes), yes
    for no in ("", "localhost", "1.2.3.4.evil", "::1", "[1.2.3.4]", "[evil]", "1.2.3", "01.2.3.4"):
        assert not is_ip_literal(no), no


def test_host_name_strips_the_port() -> None:
    assert host_name("LocalHost:8140") == "localhost"
    assert host_name("[::1]:8140") == "[::1]"
    assert host_name("[::1]") == "[::1]"
    assert host_name("::1") == "::1"
    assert host_name(None) == ""


# ---------------------------------------------------------------- events

def test_events_start_with_hello_then_latest_status_then_frames_in_order(
        server: MapServer) -> None:
    server.set_hello(HELLO)
    server.publish(status("starting"))
    server.publish(frame(0))              # before anyone listens: not replayed
    server.publish(status("streaming"))
    ev = Events(server.port)
    try:
        assert ev.resp.status == 200
        assert ev.resp.getheader("Content-Type").startswith("text/event-stream")
        assert ev.resp.getheader("Access-Control-Allow-Origin") is None
        wait_for(lambda: server.client_count == 1)
        for k in range(1, 6):
            server.publish(frame(k))
        got = ev.take(7)
    finally:
        ev.close()
    assert got[0] == HELLO
    assert got[1] == status("streaming")
    assert [m["seq"] for m in got[2:]] == [1, 2, 3, 4, 5]


def test_events_without_a_hello_yet_still_stream(server: MapServer) -> None:
    ev = Events(server.port)
    try:
        wait_for(lambda: server.client_count == 1)
        server.publish(frame(1))
        assert ev.next() == frame(1)
    finally:
        ev.close()


def test_a_new_hello_reaches_connected_clients_and_replaces_the_old(server: MapServer) -> None:
    server.set_hello(HELLO)
    ev = Events(server.port)
    try:
        assert ev.next() == HELLO
        server.set_hello({**HELLO, "source": "replay"})
        assert ev.next()["source"] == "replay"
    finally:
        ev.close()
    ev = Events(server.port)
    try:
        assert ev.next()["source"] == "replay"
    finally:
        ev.close()


def test_two_clients_both_receive_everything(server: MapServer) -> None:
    server.set_hello(HELLO)
    a, b = Events(server.port), Events(server.port)
    try:
        wait_for(lambda: server.client_count == 2)
        for k in range(10):
            server.publish(frame(k))
        got_a, got_b = a.take(11), b.take(11)
    finally:
        a.close()
        b.close()
    assert got_a == got_b
    assert [m.get("seq") for m in got_a[1:]] == list(range(10))


def test_closed_clients_are_forgotten(server: MapServer) -> None:
    ev = Events(server.port)
    wait_for(lambda: server.client_count == 1)
    ev.close()
    deadline = time.monotonic() + 5
    while server.client_count and time.monotonic() < deadline:
        server.publish(frame(0))          # the handler notices the closed socket on a write
        time.sleep(0.01)
    assert server.client_count == 0


def test_idle_stream_sends_ping_comments(root: Path) -> None:
    with MapServer(port=0, root=root, ping_s=0.05) as s:
        ev = Events(s.port)
        try:
            wait_for(lambda: s.client_count == 1)
            line = ev.resp.readline()
            assert line == b": ping\n"
        finally:
            ev.close()


def test_a_stalled_client_is_dropped_without_blocking_publish(root: Path) -> None:
    with MapServer(port=0, root=root, queue_size=4) as s:
        s.set_hello(HELLO)
        stalled = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stalled.connect(("127.0.0.1", s.port))
        stalled.sendall(b"GET /events HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")   # never reads
        good = Events(s.port)
        try:
            assert good.next() == HELLO
            wait_for(lambda: s.client_count == 2)
            pad = "x" * 65536
            slowest = 0.0
            sent = 0
            while s.dropped == 0 and sent < 2000:
                began = time.perf_counter()
                s.publish(frame(sent, pad=pad))
                slowest = max(slowest, time.perf_counter() - began)
                assert good.next()["seq"] == sent    # the healthy client keeps up in step
                sent += 1
            assert s.dropped == 1, f"the stalled client was never dropped after {sent} frames"
            assert slowest < 0.25, f"publish blocked for {slowest:.3f} s"
            wait_for(lambda: s.client_count == 1)
            s.publish(frame(-1))
            assert good.next()["seq"] == -1
        finally:
            good.close()
            stalled.close()


def test_stop_returns_promptly_with_clients_connected(root: Path) -> None:
    s = MapServer(port=0, root=root).start()
    s.set_hello(HELLO)
    reading = Events(s.port)
    idle = socket.create_connection(("127.0.0.1", s.port), timeout=5)
    idle.sendall(b"GET /events HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
    assert reading.next() == HELLO
    wait_for(lambda: s.client_count == 2)
    began = time.monotonic()
    s.stop()
    assert time.monotonic() - began < 1.5
    assert reading.next() is None          # the stream ends instead of hanging
    reading.close()
    idle.close()
    s.stop()                               # twice is fine
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", s.port), timeout=1).close()


def test_port_zero_binds_a_free_port(root: Path) -> None:
    with MapServer(port=0, root=root) as s:
        assert s.port > 0
        assert s.url == f"http://127.0.0.1:{s.port}/"
        assert not s.exposed
        assert get(s, "/")[0] == 200


def test_a_busy_port_raises(server: MapServer, root: Path) -> None:
    assert srv.port_in_use("127.0.0.1", server.port)
    with pytest.raises(OSError):   # the same address and port as a listening server
        MapServer("127.0.0.1", server.port, root)


def test_publish_from_many_threads(server: MapServer) -> None:
    ev = Events(server.port)
    try:
        wait_for(lambda: server.client_count == 1)
        threads = [threading.Thread(target=lambda k=k: [server.publish(frame(k * 100 + i))
                                                        for i in range(10)])
                   for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = sorted(m["seq"] for m in ev.take(40))
    finally:
        ev.close()
    assert seqs == sorted(k * 100 + i for k in range(4) for i in range(10))


# ---------------------------------------------------------------- encoding

def test_nan_and_numpy_values_are_sent_as_valid_json(server: MapServer) -> None:
    ev = Events(server.port)
    try:
        wait_for(lambda: server.client_count == 1)
        server.publish(frame(1, z=[1.5, math.nan, math.inf], db=np.array([1.0, np.nan]),
                             n=np.int64(3), f=np.float32(0.5)))
        got = ev.next()
    finally:
        ev.close()
    assert got["z"] == [1.5, None, None]
    assert got["db"] == [1.0, None]
    assert got["n"] == 3 and got["f"] == 0.5


def test_encode_is_one_sse_event_and_never_nan() -> None:
    data = encode({"type": "frame", "x": [math.nan], "s": "two\nlines"})
    assert data.startswith(b"data: ") and data.endswith(b"\n\n")
    assert data.count(b"\n") == 2
    assert b"NaN" not in data
    for bad in ({"no": "type"}, ["type"], {"type": 3}):
        with pytest.raises(ValueError):
            encode(bad)
    with pytest.raises(TypeError):
        encode({"type": "frame", "x": object()})


def test_set_hello_needs_a_hello(server: MapServer) -> None:
    with pytest.raises(ValueError):
        server.set_hello(status("starting"))


def test_bad_settings_are_rejected(root: Path) -> None:
    with pytest.raises(ValueError):
        MapServer(port=0, root=root, ping_s=0)
    with pytest.raises(ValueError):
        MapServer(port=0, root=root, queue_size=0)


# ---------------------------------------------------------------- command line

@pytest.mark.parametrize("argv", [["--board", "muse"],
                                  ["--board", "replay"],
                                  ["--recording", "x.npz"],
                                  ["--loop"],
                                  ["--speed", "2"],
                                  ["--serial-port", "/dev/ttyUSB0"],
                                  ["--board", "replay", "--recording", "x.npz", "--speed", "0"],
                                  ["--board", "replay", "--recording", "x.npz", "--speed", "nan"],
                                  ["--board", "replay", "--recording", "x.npz", "--speed", "17"],
                                  ["--board", "replay", "--recording", "x.npz", "--speed", "1e9"],
                                  ["--allow-host", ""],
                                  ["--allow-host", "jetson local"],
                                  ["--allow-host", "http://jetson.local/"],
                                  ["--port", "70000"],
                                  ["--port", "-1"],
                                  ["--baseline-s", "0"],
                                  ["--baseline-s", "inf"],
                                  ["--seconds", "0"],
                                  ["--seconds", "-3"]])
def test_bad_arguments_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "error" in capsys.readouterr().err


def test_speed_up_to_the_cap_is_accepted() -> None:
    p = cli.build_parser()
    args = p.parse_args(["--board", "replay", "--recording", "x.npz", "--speed", "16"])
    cli.check_arguments(p, args)
    assert args.speed == cli.MAX_SPEED == 16


def test_a_missing_recording_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--board", "replay", "--recording", str(tmp_path / "gone.npz"), "--port", "0"])
    assert code == 2
    assert "recording not found" in capsys.readouterr().err


def test_a_damaged_recording_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad_cued.npz"
    bad.write_bytes(b"not a zip file")
    assert main(["--board", "replay", "--recording", str(bad), "--port", "0"]) == 1
    assert f"cannot read the recording {bad}" in capsys.readouterr().err


def test_a_short_replay_plays_to_its_end_and_exits_0(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("brainsync.brainmap.stream")
    from brainsync.recordings import MONTAGE
    fs, n = 250.0, 750
    path = tmp_path / "short_trials.npz"
    np.savez(path, X=np.random.default_rng(0).normal(0, 8, (8, n)),
             t=1.7e9 + np.arange(n) / fs, fs=fs, ch_names=np.array(MONTAGE), markers="", f0=15.0)
    began = time.monotonic()
    code = main(["--board", "replay", "--recording", str(path), "--speed", "16", "--port", "0"])
    assert code == 0, capsys.readouterr().err
    assert time.monotonic() - began < 10
    assert "the replay ended" in capsys.readouterr().err


def test_a_busy_port_exits_1(server: MapServer, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--port", str(server.port), "--seconds", "1"]) == 1
    assert "in use" in capsys.readouterr().err


def test_the_cli_starts_without_heavy_imports() -> None:
    code = ("import sys, brainsync.brainmap, brainsync.brainmap.server, brainsync.brainmap.__main__;"
            "heavy = [m for m in ('scipy', 'matplotlib', 'brainflow', 'torch', "
            "'brainsync.brainmap.stream', 'brainsync.brainmap.features') if m in sys.modules];"
            "print(heavy)")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         timeout=60, check=True).stdout.strip()
    assert out == "[]"


def test_end_to_end_fake_board_streams_frames() -> None:
    pytest.importorskip("brainsync.brainmap.stream")
    proc = subprocess.Popen([sys.executable, "-m", "brainsync.brainmap", "--board", "fake",
                             "--seconds", "4", "--port", "0"],
                            cwd=REPO, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            text=True)
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(ln) for ln in proc.stderr], daemon=True).start()
    try:
        url = ""
        deadline = time.monotonic() + 30
        log = []
        while not url and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            log.append(line)
            for word in line.split():
                if word.startswith("http://127.0.0.1:"):
                    url = word
        assert url, f"no URL printed: {''.join(log)}"
        port = int(url.rstrip("/").rsplit(":", 1)[1])
        ev = Events(port, timeout=10)
        try:
            hello = ev.next()
            assert hello["type"] == "hello" and hello["format"] == 1
            assert hello["source"] == "fake"
            assert len(hello["channels"]) == 8
            assert set(hello["positions"]) == set(hello["channels"])
            assert "not a brain scan" in hello["label"]
            seen = [ev.next() for _ in range(3)]
            if "ssvep_keys" in hello:
                assert len(hello["ssvep_keys"]) == len(hello["ssvep_hz"])
        finally:
            ev.close()
        kinds = [m["type"] for m in seen if m]
        assert kinds[0] == "status"
        assert "frame" in kinds, kinds
        f = next(m for m in seen if m and m["type"] == "frame")
        assert set(f["db"]) == {"delta", "theta", "alpha", "beta"}
        assert all(len(v) == 8 for v in f["db"].values())
        assert len(f["quality"]) == 8
        if "ssvep_keys" in hello:
            assert list(f["ssvep"]) == hello["ssvep_keys"]
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------- CLI: exposure

def test_an_exposed_cli_prints_the_accepted_names(monkeypatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("brainsync.brainmap.stream")
    monkeypatch.setattr(srv, "_bound_loopback", lambda a: False)
    servers = spy_servers(monkeypatch)
    assert main(["--seconds", "0.3", "--port", "0", "--allow-host", "jetson.local",
                 "--allow-host", "pi.lan"]) == 0
    err = capsys.readouterr().err
    assert "accepted Host names: 127.0.0.1, [::1], jetson.local, localhost, pi.lan" in err
    assert "any IP address" in err
    assert servers[0].accepted_hosts == ("127.0.0.1", "[::1]", "jetson.local", "localhost",
                                         "pi.lan")


def test_a_loopback_cli_prints_no_exposure_lines(capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("brainsync.brainmap.stream")
    assert main(["--seconds", "0.3", "--port", "0"]) == 0
    err = capsys.readouterr().err
    assert "accepted Host names" not in err and "anyone on this network" not in err


# ---------------------------------------------------------------- CLI: coalescing

def test_coalesce_keeps_the_newest_frames_per_poll_and_statuses_in_place() -> None:
    sent: list[dict] = []
    out = cli.Coalesce(sent.append, keep=2)
    out.emit(status("streaming"))
    for k in range(5):
        out.emit(frame(k))
    assert [m["type"] for m in sent] == ["status"]      # frames wait for the poll to end
    out.flush()
    out.emit(frame(5))
    out.emit(status("ended"))                           # a status flushes what came before
    assert [m.get("seq", m.get("state")) for m in sent] == ["streaming", 3, 4, 5, "ended"]
    assert out.dropped == 3
    with pytest.raises(ValueError):
        cli.Coalesce(sent.append, keep=0)


def test_a_fast_replay_publishes_at_most_a_few_frames_per_poll() -> None:
    """At the 16x cap a 0.05 s poll makes three or four frames; the client sees the newest
    FRAMES_PER_POLL of each, ending on the replay's last frame."""
    stream_mod = pytest.importorskip("brainsync.brainmap.stream")
    from brainsync.recordings import MONTAGE
    fs, n = 250.0, 250 * 20
    rec = (np.random.default_rng(1).normal(0, 8, (8, n)), 1.7e9 + np.arange(n) / fs, fs, MONTAGE)

    def play(coalesced: bool) -> tuple[list[dict], list[int]]:
        now = [0.0]
        board = stream_mod.ReplayBoard(rec, speed=cli.MAX_SPEED, clock=lambda: now[0])
        polls = [0]
        read = board.read
        board.read = lambda: (polls.__setitem__(0, polls[0] + 1), read())[1]
        stream = stream_mod.MapStream(fs, MONTAGE, baseline_s=1.0, source="replay")
        got: list[tuple[int, dict]] = []
        publish = lambda m: got.append((polls[0], m))  # noqa: E731
        kw = dict(sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
        if coalesced:
            assert cli.stream_board(board, stream, publish, **kw) == "ended"
        else:
            assert stream_mod.run(board, stream, publish, **kw) == "ended"
        frames = [(p, m) for p, m in got if m["type"] == "frame"]
        return [m for _, m in frames], [p for p, _ in frames]

    every, every_polls = play(False)
    shown, shown_polls = play(True)
    assert max(collections.Counter(every_polls).values()) > cli.FRAMES_PER_POLL
    per_poll = collections.Counter(shown_polls)
    assert max(per_poll.values()) <= cli.FRAMES_PER_POLL, per_poll
    seqs = [m["seq"] for m in shown]
    assert seqs == sorted(set(seqs))
    assert len(shown) < len(every)
    assert shown[-1] == every[-1]          # dropping is display only: same numbers


# ---------------------------------------------------------------- CLI: board errors

class BoomError(Exception):
    """Stands in for brainflow's BrainFlowError: none of OSError, RuntimeError, ValueError."""


class FailingBoard:
    """Streams nothing, then raises ``read_error`` on the third read; ``close_error`` on exit."""

    fs = 250.0

    def __init__(self, read_error: BaseException, close_error: BaseException | None = None):
        from brainsync.recordings import MONTAGE
        self.ch_names = MONTAGE
        self.read_error, self.close_error = read_error, close_error
        self.reads = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def read(self):
        self.reads += 1
        if self.reads >= 3:
            raise self.read_error
        return np.empty((8, 0)), np.empty(0)


def spy_servers(monkeypatch) -> list:
    """Patch the CLI's MapServer to record each instance and every message it publishes."""
    made: list = []

    class Spy(MapServer):
        def __init__(self, *a, **k) -> None:
            self.sent: list[dict] = []
            super().__init__(*a, **k)
            made.append(self)

        def publish(self, msg: dict) -> None:
            self.sent.append(msg)
            super().publish(msg)

    monkeypatch.setattr(cli, "MapServer", Spy)
    return made


def test_a_board_error_mid_run_exits_1_without_a_traceback(
        monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("brainsync.brainmap.stream")
    board = FailingBoard(BoomError("BOARD_NOT_READY_ERROR:7 unable to read"))
    monkeypatch.setattr(cli, "make_board", lambda args: board)
    monkeypatch.setattr(cli, "END_GRACE_S", 0.0)
    assert main(["--board", "cerelog", "--port", "0"]) == 1
    err = capsys.readouterr().err
    assert "error: BOARD_NOT_READY_ERROR:7 unable to read" in err
    assert "Traceback" not in err
    assert board.closed


def test_a_board_that_never_sends_a_sample_exits_1(monkeypatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    # the Cerelog X8 switched off: BrainFlow logs a failed handshake, then reads stay empty
    pytest.importorskip("brainsync.brainmap.stream")

    class SilentBoard(FailingBoard):
        def read(self):
            return np.empty((8, 0)), np.empty(0)

    board = SilentBoard(BoomError("unused"))
    monkeypatch.setattr(cli, "make_board", lambda args: board)
    monkeypatch.setattr(cli, "END_GRACE_S", 0.0)
    servers = spy_servers(monkeypatch)
    assert main(["--board", "cerelog", "--port", "0", "--seconds", "0.3"]) == 1
    err = capsys.readouterr().err
    assert "error: no samples arrived from the cerelog board" in err
    assert servers[0].sent[-1]["state"] == "error"
    assert board.closed


def test_ctrl_c_with_a_failing_close_reports_it_and_exits_130(
        monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("brainsync.brainmap.stream")
    board = FailingBoard(KeyboardInterrupt(), close_error=BoomError("release_session failed"))
    monkeypatch.setattr(cli, "make_board", lambda args: board)
    assert main(["--board", "cerelog", "--port", "0"]) == 130
    err = capsys.readouterr().err
    assert "error: closing the board failed: release_session failed" in err
    assert "stopped" in err
    assert "Traceback" not in err


def test_an_error_opening_the_board_exits_1(monkeypatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("brainsync.brainmap.stream")

    class NoBoard(FailingBoard):
        def __enter__(self):
            raise BoomError("no device on /dev/ttyUSB0")

    monkeypatch.setattr(cli, "make_board", lambda args: NoBoard(BoomError("unused")))
    monkeypatch.setattr(cli, "END_GRACE_S", 0.0)
    assert main(["--board", "cerelog", "--port", "0"]) == 1
    assert "error: no device on /dev/ttyUSB0" in capsys.readouterr().err


# ---------------------------------------------------------------- CLI: end-of-run grace

def run_main_in_thread(argv: list[str]) -> tuple[threading.Thread, list]:
    result: list = []
    t = threading.Thread(target=lambda: result.append(main(argv)), daemon=True)
    t.start()
    return t, result


def late_client_sees(servers: list, state: str) -> list[dict]:
    """Wait for ``state`` to be published, then connect well after the old 0.3 s flush."""
    wait_for(lambda: servers and any(m.get("state") == state for m in servers[0].sent),
             timeout=20)
    time.sleep(1.0)
    assert 1.0 < cli.END_GRACE_S
    ev = Events(servers[0].port)
    try:
        return ev.take(2)
    finally:
        ev.close()


def test_the_ended_status_reaches_a_page_after_the_replay(
        tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("brainsync.brainmap.stream")
    from brainsync.recordings import MONTAGE
    fs, n = 250.0, 750
    path = tmp_path / "short_trials.npz"
    np.savez(path, X=np.random.default_rng(0).normal(0, 8, (8, n)),
             t=1.7e9 + np.arange(n) / fs, fs=fs, ch_names=np.array(MONTAGE), markers="", f0=15.0)
    servers = spy_servers(monkeypatch)
    t, result = run_main_in_thread(["--board", "replay", "--recording", str(path), "--speed",
                                    "16", "--port", "0"])
    hello, last = late_client_sees(servers, "ended")
    assert hello["type"] == "hello"
    assert last["type"] == "status" and last["state"] == "ended"
    t.join(timeout=10)
    assert result == [0]


def test_the_error_status_reaches_a_page_after_a_board_failure(monkeypatch) -> None:
    pytest.importorskip("brainsync.brainmap.stream")
    monkeypatch.setattr(cli, "make_board", lambda args: FailingBoard(BoomError("unplugged")))
    servers = spy_servers(monkeypatch)
    t, result = run_main_in_thread(["--board", "cerelog", "--port", "0"])
    _, last = late_client_sees(servers, "error")
    assert last["state"] == "error" and "unplugged" in last["text"]
    t.join(timeout=10)
    assert result == [1]


# ---------------------------------------------------------------- CLI: fake demo

def test_the_fake_demo_rests_through_the_baseline_then_steps_through_its_tones() -> None:
    args = cli.build_parser().parse_args(["--baseline-s", "30"])
    demo = cli.make_board(args, clock=None)
    assert demo.virtual
    assert demo.board.amp == cli.FAKE_AMP_UV
    assert [demo.target_at(s) for s in (0, 29.9, 30, 39.9, 40, 50, 60, 75)] == \
        [-1, -1, 0, 0, 1, 2, 0, 1]
    assert list(demo.board.freqs[[0, 1, 2]]) == [10.0, 15.0, 17.0]


def test_the_fake_demo_lights_posterior_alpha_in_the_10hz_phase() -> None:
    """Through the CLI's own board and streaming path on a virtual FakeBoard: with a quiet
    baseline the 10 Hz tone stands out clearly on P7, P8, O1 and O2, and 15 Hz after it."""
    stream_mod = pytest.importorskip("brainsync.brainmap.stream")
    from brainsync.recordings import POSTERIOR
    args = cli.build_parser().parse_args(["--board", "fake", "--baseline-s", "30"])
    board = cli.make_board(args, clock=None)
    stream = stream_mod.MapStream(board.fs, board.ch_names, baseline_s=args.baseline_s,
                                  source="fake")
    hello = stream.hello()
    got: list[dict] = []
    why = cli.stream_board(board, stream, got.append, stop=lambda: board.elapsed >= 50.0)
    assert why == "stopped"
    rows = [list(board.ch_names).index(c) for c in POSTERIOR]
    t0 = board.board.t0

    def during(lo: float, hi: float) -> list[dict]:
        # whole 2 s windows inside the phase, past the smoothing's first frames
        return [m for m in got if m["type"] == "frame" and lo + 2.5 <= m["t"] - t0 < hi]

    def median(frames: list[dict], pick) -> np.ndarray:
        vals = np.array([[np.nan if v is None else v for v in pick(m)] for m in frames])
        return np.nanmedian(vals[:, rows], axis=0)

    alpha = median(during(30, 40), lambda m: m["z"]["alpha"])
    assert np.all(alpha > 2.0), dict(zip(POSTERIOR, alpha.round(2)))
    keys = hello.get("ssvep_keys") or [f"{f:g}" for f in hello["ssvep_hz"]]
    k15, k17 = keys[list(hello["ssvep_hz"]).index(15.0)], keys[list(hello["ssvep_hz"]).index(17.0)]
    snr15 = median(during(40, 50), lambda m: m["ssvep"][k15])
    snr17 = median(during(40, 50), lambda m: m["ssvep"][k17])
    assert np.all(snr15 > 6.0) and np.all(snr15 > snr17 + 3.0), (snr15, snr17)
    rest = [m for m in got if m["type"] == "frame" and m["t"] - t0 < 30]
    assert rest and all(m["baseline"]["state"] == "collecting" for m in rest[:-8])
