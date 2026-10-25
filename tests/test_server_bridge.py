"""python -m app.server --bridge: starts brainsync.voice_bridge against itself.

The argument checks run main() in-process (they exit before anything starts). The end-to-end
test starts the real server with ``--bridge fake --bridge-headless`` on a free port and waits
until /api/health shows the bridge connected and acked under its fake name, once with the
default config (c-VEP: voice_cvep3) and once with config/fvep_page.yaml (f-VEP: four_diamond).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from app.server import BRIDGE_KINDS, bridge_command, main

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("argv, fragment", [
    (["--bridge", "fake", "--fake-decoder"], "pick one"),
    (["--bridge-headless"], "--bridge-headless needs --bridge"),
    (["--bridge", "openbci"], "invalid choice"),
])
def test_bridge_argument_errors_exit_2(argv, fragment, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert fragment in capsys.readouterr().err


def test_bridge_command():
    url = "ws://127.0.0.1:8000/ws/decoder"
    assert BRIDGE_KINDS == ("cerelog", "fake")
    assert bridge_command("cerelog", url, 42) == [
        sys.executable, "-m", "brainsync.voice_bridge", "--url", url, "--parent-pid", "42",
        "--paradigm", "code"]
    assert bridge_command("fake", url, 42, headless=True)[-2:] == ["--fake", "--no-stimulus"]
    assert bridge_command("cerelog", url, 42, paradigm="freq", stimulus="two_targets")[-4:] == [
        "--paradigm", "freq", "-c", "two_targets"]
    with pytest.raises(ValueError):
        bridge_command("openbci", url, 42)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return json.loads(r.read())
    except OSError:
        return None


def _has_four_diamond() -> bool:
    from brainsync.stimulus.config import resolve_config

    try:
        resolve_config("four_diamond")
    except FileNotFoundError:
        return False
    return True


@pytest.mark.slow
@pytest.mark.skipif(not _has_four_diamond(), reason="brainsync/stimulus/configs/four_diamond.toml "
                                                    "is not there yet")
@pytest.mark.parametrize("config_name, bridge_name", [
    ("default.yaml", "fake-brainsync-cvep"), ("fvep_page.yaml", "fake-brainsync-fbcca")])
def test_server_starts_a_headless_fake_bridge(tmp_path, config_name, bridge_name):
    port = _free_port()
    cfg = yaml.safe_load((ROOT / "config" / config_name).read_text(encoding="utf-8"))
    cfg["server"]["port"] = port
    config = tmp_path / "server.yaml"
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    # No API keys: the server must not reach Claude or Cartesia from a test.
    env = dict(os.environ, ANTHROPIC_API_KEY="", CARTESIA_API_KEY="")
    server = subprocess.Popen([sys.executable, "-m", "app.server", "--config", str(config),
                               "--bridge", "fake", "--bridge-headless"],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              text=True)
    try:
        deadline = time.monotonic() + 60
        decoder = {}
        while time.monotonic() < deadline:
            health = _health(port)
            decoder = (health or {}).get("decoder", {})
            if decoder.get("connected") and decoder.get("name") and decoder.get("ok"):
                break
            assert server.poll() is None, server.stderr.read()
            time.sleep(0.5)
        assert decoder.get("connected"), decoder
        assert decoder["name"] == bridge_name and decoder["ok"], decoder  # the hello was acked
        assert decoder["fake_decoder"] is True  # FAKE DECODER badge
    finally:
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    # The bridge went with the server (stop_process, or its --parent-pid watch).
    deadline = time.monotonic() + 10
    pattern = f"voice_bridge --url ws://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        left = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
        if not left.strip():
            break
        time.sleep(0.5)
    assert not left.strip(), f"bridge still running: {left}"
