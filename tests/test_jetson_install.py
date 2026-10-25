"""Tests of deploy/jetson/install.sh and the other Jetson scripts, without a Jetson or root.

Every bash script under deploy/jetson parses (bash -n); install.sh shows its usage, refuses a
bad --board and refuses to run without root. The deployment, its docs, the server and the
clients never mention Tailscale (or a tailnet, or a *.ts.net name): Cloudflare quick tunnels
are the only way other machines reach the server. tests/ is not scanned: the origin tests
use a *.ts.net name on purpose, as one that is refused.

The module skips without bash, and on Windows.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JETSON = ROOT / "deploy" / "jetson"
INSTALL = JETSON / "install.sh"
# made by build_brainflow.sh next to itself (gitignored): a BrainFlow clone, not ours
BUILD_DIRS = {JETSON / "brainflow" / name for name in ("src", "build-src", "out")}

pytestmark = pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                                reason="needs a POSIX system with bash")


def _ours(path: Path) -> bool:
    return not any(d == path or d in path.parents for d in BUILD_DIRS)


def _bash_scripts() -> list[Path]:
    found = []
    for path in sorted(JETSON.rglob("*")):
        if not path.is_file() or not _ours(path):
            continue
        with open(path, "rb") as fh:
            first = fh.readline()
        if first.startswith(b"#!") and b"bash" in first:
            found.append(path)
    return found


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True,
                          timeout=30)


def test_the_scripts_are_found():
    names = {p.relative_to(JETSON).as_posix() for p in _bash_scripts()}
    assert {"install.sh", "smoke_test.sh", "cloudflare/install.sh",
            "cloudflare/brainsync-tunnel.sh", "brainflow/build_brainflow.sh"} <= names


@pytest.mark.parametrize("script", _bash_scripts(), ids=lambda p: p.relative_to(JETSON).as_posix())
def test_every_script_parses(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_the_old_server_unit_is_gone():
    assert not (JETSON / "brainsync-server.service").exists()
    text = INSTALL.read_text()
    assert "useradd" not in text, "the server runs as the login user, not a service user"
    assert '"$HERE/cloudflare/install.sh"' in text


def test_help_shows_the_usage():
    r = _run("--help")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("Set up (or update) the BrainSync server on a Jetson")
    assert "--board fake" in r.stdout and "cloudflare/install.sh" in r.stdout
    assert "set -euo pipefail" not in r.stdout, "the usage runs past the header"


@pytest.mark.parametrize("args", [["--board", "pi"], ["--board=FAKE"], ["--board"]])
def test_a_bad_board_is_refused(args):
    r = _run(*args)
    assert r.returncode == 1
    assert "--board must be cerelog or fake" in r.stderr


def test_an_unknown_option_is_refused():
    r = _run("--tailnet")
    assert r.returncode == 1 and "unknown option" in r.stderr


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="runs as root")
@pytest.mark.parametrize("board", ["cerelog", "fake"])
def test_it_needs_root(board):
    r = _run("--board", board)
    assert r.returncode == 1
    assert "run with sudo" in r.stderr
    assert "==>" not in r.stdout, "it started before checking for root"


TAILSCALE = re.compile(r"tailscale|tailnet|\.ts\.net", re.IGNORECASE)
CODE_SUFFIXES = {".py", ".js", ".mjs", ".html", ".md"}


def _scanned_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "PROTOCOL-v3.md"]
    files += [p for p in sorted((ROOT / "deploy").rglob("*")) if p.is_file() and _ours(p)]
    for top in ("brainsync", "clients"):
        files += [p for p in sorted((ROOT / top).rglob("*"))
                  if p.is_file() and p.suffix in CODE_SUFFIXES
                  and "node_modules" not in p.relative_to(ROOT).parts]
    return files


def test_the_scan_covers_the_server_and_the_clients():
    names = {p.relative_to(ROOT).as_posix() for p in _scanned_files()}
    assert {"brainsync/server/transport.py", "clients/web/app.js",
            "clients/js/test/web_app.test.mjs", "deploy/jetson/install.sh"} <= names


def test_nothing_mentions_tailscale():
    found = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue   # a binary file
        for n, line in enumerate(text.splitlines(), 1):
            if TAILSCALE.search(line):
                found.append(f"{path.relative_to(ROOT).as_posix()}:{n}: {line.strip()}")
    assert not found, "\n".join(found)
