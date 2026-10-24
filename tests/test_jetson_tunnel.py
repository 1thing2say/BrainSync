"""Tests of deploy/jetson/cloudflare: the boot-time Cloudflare quick tunnel for the Jetson.

brainsync-tunnel.sh runs for real with a stand-in cloudflared (test/fake-cloudflared, which
prints a quick-tunnel banner and never contacts Cloudflare), a stand-in qrencode, and the
real server (``python -m brainsync.server --board fake``) on a free port. The serial consoles
are pseudo-terminals whose master side the test reads. Each test gets its own HOME, so the
link file, the token file and the cloudflared config check never touch the real home.

The module skips without bash or curl, and on Windows.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "deploy" / "jetson" / "cloudflare"
SCRIPT = HERE / "brainsync-tunnel.sh"
INSTALL = HERE / "install.sh"
FAKE_CF = HERE / "test" / "fake-cloudflared"
FAKE_QR = HERE / "test" / "fake-qrencode"
HOST = "fake-tunnel-test.trycloudflare.com"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None or shutil.which("curl") is None,
    reason="needs a POSIX system with bash and curl")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
            s.sendall(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            return s.recv(64).startswith(b"HTTP/1.")
    except OSError:
        return False


def _wait(cond, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout} s waiting for {what}")


class Serial:
    """A pseudo-terminal standing in for a serial console: the script writes the slave path,
    and a thread collects what arrives on the master."""

    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while not self._stop.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            with self._lock:
                self._buf += chunk

    @property
    def text(self) -> str:
        with self._lock:
            return self._buf.decode("utf-8", "replace")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def serial():
    s = Serial()
    yield s
    s.close()


class Tunnel:
    """brainsync-tunnel.sh as a subprocess with test settings; its output goes to a file."""

    def __init__(self, base: Path, serial_devices: str, **env: str) -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.home = base / "home"
        self.home.mkdir(exist_ok=True)
        self.port = int(env.pop("PORT", 0)) or _free_port()
        self.link_file = self.home / "brainsync-link.txt"
        self.token_file = self.home / ".config" / "brainsync" / "token"
        self.cf_pidfile = base / "cf.pid"
        self.cf_argsfile = base / "cf.args"
        self.out = base / "tunnel.out"
        full = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "TMPDIR": str(base),
            "BRAINSYNC_DIR": str(ROOT),
            "PYTHON": sys.executable,
            "CLOUDFLARED": str(FAKE_CF),
            "QRENCODE": str(FAKE_QR),
            "PORT": str(self.port),
            "BOARD": "fake",
            "SERVER_ARGS": "--no-detect",
            "SERIAL_DEVICES": serial_devices,
            "ANNOUNCE_EVERY": "1",
            "URL_TIMEOUT": "10",
            "HEALTH_TIMEOUT": "60",
            "SERIAL_TIMEOUT": "2",
            "STOP_TIMEOUT": "5",
            "FAKE_CF_PIDFILE": str(self.cf_pidfile),
            "FAKE_CF_ARGSFILE": str(self.cf_argsfile),
        }
        full.update(env)
        self._fh = open(self.out, "wb")
        self.proc = subprocess.Popen(["bash", str(SCRIPT)], env=full, stdout=self._fh,
                                     stderr=subprocess.STDOUT, start_new_session=True)

    @property
    def output(self) -> str:
        return self.out.read_text(errors="replace")

    def wait(self, timeout: float) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"the tunnel script still runs after {timeout} s:\n{self.output}")

    def cf_pid(self) -> int | None:
        try:
            return int(self.cf_pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        self._fh.close()


@pytest.fixture
def tunnels():
    started: list[Tunnel] = []

    def start(base: Path, serial_devices: str = "", **env: str) -> Tunnel:
        t = Tunnel(base, serial_devices, **env)
        started.append(t)
        return t

    yield start
    for t in started:
        t.close()


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _hello(port: int, origin: str, token: str) -> dict:
    """Open /ws with this Origin and send a viewer hello. Returns the first answer, or raises
    the websockets error (InvalidStatus with 403 for a refused Origin)."""
    from websockets.sync.client import connect
    with connect(f"ws://127.0.0.1:{port}/ws", origin=origin, open_timeout=5) as ws:
        ws.send(json.dumps({"type": "hello", "protocol": 3, "token": token, "role": "viewer",
                            "client": {"name": "tunnel-test", "kind": "test", "version": "0"}}))
        return json.loads(ws.recv(timeout=5))


def _link(t: Tunnel) -> str:
    return _wait(lambda: t.link_file.exists() and t.link_file.read_text(), 60, "the link file")


# -- the link, the serial consoles, the Origin ------------------------------------------------

def test_link_reaches_the_file_and_serial_and_the_origin_is_exact(tmp_path, serial, tunnels):
    pytest.importorskip("websockets")
    from websockets.exceptions import InvalidStatus
    t = tunnels(tmp_path, serial.path)
    link = _link(t)
    token = t.token_file.read_text().strip()

    assert len(token) >= 16
    assert link == f"https://{HOST}/#token={token}\n"
    assert stat.S_IMODE(t.link_file.stat().st_mode) == 0o600

    _wait(lambda: serial.text.count("BrainSync is up") >= 2, 15, "a second serial announcement")
    text = serial.text
    assert f"https://{HOST}/#token={token}" in text
    assert f"wss://{HOST}/ws" in text
    assert "FAKE-QR-START" in text

    assert _hello(t.port, f"https://{HOST}", token)["type"] == "welcome"
    with pytest.raises(InvalidStatus) as refused:
        _hello(t.port, "https://evil.example", token)
    assert refused.value.response.status_code == 403
    # an unescaped dot in --origin would let this one through
    with pytest.raises(InvalidStatus):
        _hello(t.port, "https://fake-tunnel-testXtrycloudflareXcom", token)

    args = t.cf_argsfile.read_text().split("\n")
    assert args[:4] == ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{t.port}"]
    assert "--protocol" not in args

    cf = t.cf_pid()
    t.proc.send_signal(signal.SIGTERM)
    assert t.wait(30) == 0
    assert not t.link_file.exists()
    assert not _health(t.port)
    assert not _pid_alive(cf)
    _wait(lambda: "BrainSync tunnel stopped." in serial.text, 5, "the stop notice on serial")

    out = t.output
    assert token not in out, "the token reached the journal"
    assert f"tunnel up: https://{HOST}" in out


def test_link_without_token_and_no_usable_serial(tmp_path, tunnels):
    t = tunnels(tmp_path, "/nonexistent/ttyGS0", LINK_WITH_TOKEN="0",
                CLOUDFLARED_PROTOCOL="http2")
    assert _link(t) == f"https://{HOST}/\n"
    assert "--protocol\nhttp2" in t.cf_argsfile.read_text()
    _wait(lambda: "/nonexistent/ttyGS0:missing" in t.output, 10, "the serial status line")
    assert t.token_file.read_text().strip() not in t.output


# -- failures ---------------------------------------------------------------------------------

def test_no_address_from_cloudflared_exits_1_without_a_link(tmp_path, serial, tunnels):
    t = tunnels(tmp_path, serial.path, FAKE_CF_MODE="nourl", URL_TIMEOUT="2")
    assert t.wait(30) == 1
    assert not t.link_file.exists()
    assert "no trycloudflare address" in t.output
    assert "api.trycloudflare.com" in t.output   # the log tail, for whoever reads the journal
    assert not _pid_alive(t.cf_pid())
    assert "tunnel down, restarting" in serial.text


def test_cloudflared_dying_stops_the_server_and_deletes_the_link(tmp_path, serial, tunnels):
    t = tunnels(tmp_path, serial.path, FAKE_CF_MODE="exit", FAKE_CF_EXIT_AFTER="12")
    _link(t)
    assert _health(t.port)
    assert t.wait(40) == 1
    assert not t.link_file.exists()
    assert not _health(t.port)
    assert "cloudflared exited" in t.output
    assert "tunnel down, restarting" in serial.text


@pytest.mark.parametrize("arg", ["--no-auth", "--no-a", "--no-aut", "--no-auth=1", "--no-"])
def test_no_auth_is_refused(tmp_path, tunnels, arg):
    t = tunnels(tmp_path, SERVER_ARGS=f"--no-detect {arg}")
    assert t.wait(15) == 2
    assert "never runs without the token" in t.output
    assert not t.cf_argsfile.exists(), "cloudflared started although the settings are refused"
    assert not t.link_file.exists()


def test_cloudflared_config_file_is_refused(tmp_path, tunnels):
    cfg = tmp_path / "home" / ".cloudflared"
    cfg.mkdir(parents=True)
    (cfg / "config.yml").write_text("tunnel: named\n")
    t = tunnels(tmp_path)
    assert t.wait(15) == 2
    assert "refuses a quick tunnel" in t.output
    assert not t.cf_argsfile.exists()


@pytest.mark.parametrize("env", [{"LINK_WITH_TOKEN": "yes"}, {"ANNOUNCE_EVERY": "soon"},
                                 {"CLOUDFLARED_PROTOCOL": "udp"}, {"PORT": "70000"}])
def test_bad_settings_exit_2(tmp_path, tunnels, env):
    t = tunnels(tmp_path, **env)
    assert t.wait(15) == 2
    assert not t.cf_argsfile.exists()


def test_a_busy_port_is_refused_and_the_other_link_kept(tmp_path, tunnels):
    first = tunnels(tmp_path / "a")
    _link(first)
    second = tunnels(tmp_path / "b", PORT=str(first.port))
    assert second.wait(15) == 1
    assert "already answers" in second.output
    assert first.link_file.exists()


# -- the unit ---------------------------------------------------------------------------------

def test_the_unit_renders_for_the_login_user():
    out = subprocess.run(["bash", str(INSTALL), "--print-unit", "--user", "jetson",
                          "--home", "/home/jetson", "--dir", "/opt/brainsync"],
                         capture_output=True, text=True, check=True).stdout
    assert "@" not in re.sub(r"(?m)^#.*$", "", out), "a placeholder was left unfilled"
    assert "\nUser=jetson\n" in out
    assert "Environment=LINK_FILE=/home/jetson/brainsync-link.txt" in out
    assert "ExecStart=/opt/brainsync/deploy/jetson/cloudflare/brainsync-tunnel.sh" in out
    assert "Conflicts=brainsync-server.service" in out
    assert "ProtectHome=yes" not in out
    assert "Restart=always" in out and "StartLimitIntervalSec=0" in out


@pytest.mark.parametrize("args", [["--user", "Bad User"],
                                  ["--user", "jetson", "--home", "/home/a b"]])
def test_install_refuses_odd_user_names_and_paths(args):
    r = subprocess.run(["bash", str(INSTALL), "--print-unit", *args, "--dir", "/opt/b"],
                       capture_output=True, text=True)
    assert r.returncode != 0
