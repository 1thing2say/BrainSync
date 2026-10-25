"""A Cloudflare quick tunnel to this server, for the partner page on a phone (PLAN.md section 10).

``cloudflared tunnel --url http://127.0.0.1:PORT`` needs no account: it prints a random
https://*.trycloudflare.com address that forwards to the server, websockets included, and stops
when the process does. The address is new every start. HTTPS matters: iPhone Safari allows the
microphone and speech recognition only on a secure page. app/remote.py keeps strangers out.

Install: ``brew install cloudflared`` (macOS) or the cloudflared-linux-amd64 binary from
https://github.com/cloudflare/cloudflared/releases on Linux / WSL.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CANDIDATES = ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared",
              str(Path.home() / ".local" / "bin" / "cloudflared"))


def find_cloudflared() -> str | None:
    found = os.environ.get("CLOUDFLARED") or shutil.which("cloudflared")
    return found or next((c for c in CANDIDATES if Path(c).is_file()), None)


def tunnel_url(line: str) -> str | None:
    """The public address in one line of cloudflared's output, if it is there."""
    match = URL.search(line)
    return match.group(0) if match else None


class Tunnel:
    """cloudflared as a child process; ``url`` is set once it has printed its address."""

    def __init__(self, port: int, binary: str) -> None:
        self.url: str | None = None
        self.ready = threading.Event()
        self.proc = subprocess.Popen(
            [binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._read, name="cloudflared", daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:  # keep draining, or cloudflared blocks on a full pipe
            if self.url is None and (url := tunnel_url(line)):
                self.url = url
                self.ready.set()
        self.ready.set()  # exited without an address

    def wait(self, timeout: float) -> str | None:
        self.ready.wait(timeout)
        return self.url

    def stop(self) -> None:
        stop_process(self.proc)


def stop_process(proc: subprocess.Popen, timeout_s: float = 5.0) -> None:
    """Terminate a child and wait for it, killing it if it is still there after ``timeout_s``.
    Blocks up to that long, so the server runs it outside the event loop."""
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
