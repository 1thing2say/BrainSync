"""Start the stimulus in its own process, and spoken prompts."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, NamedTuple

from .paths import REPO_ROOT

READY = "measured refresh"
STIMULUS_MODULE = "brainsync.stimulus"


class Stimulus(NamedTuple):
    """A running stimulus process and the file collecting its stderr (markers) and stdout."""

    proc: subprocess.Popen
    log: Path

    def text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def finish(self, timeout: float = 10.0) -> int:
        """Wait up to ``timeout`` s for the process to end, killing it after; its exit code."""
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()


def stimulus_command(args: Sequence[str], python: str | None = None) -> list[str]:
    """``<python> -m brainsync.stimulus <args>``.

    Nothing is added: the stimulus then checks the measured refresh against its config
    (60 Hz) and refuses to run on a ProMotion or 120 Hz panel.
    """
    return [python or sys.executable, "-m", STIMULUS_MODULE, *map(str, args)]


def _last_error(text: str) -> str:
    """The stimulus's last "error: ..." line, or ''."""
    errors = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("error:")]
    return errors[-1] if errors else ""


def launch_stimulus(args: Sequence[str], stdin: int | IO | None = None, timeout: float = 60.0,
                    command: Sequence[str] | None = None,
                    log_dir: str | Path | None = None) -> Stimulus:
    """Start the stimulus; return once it prints "measured refresh" (window up, refresh OK).

    stderr and stdout go to a log file. ``stdin=subprocess.PIPE`` opens a pipe for
    ``--feedback stdin``. ``command`` replaces the whole command line, for tests. Raises
    RuntimeError naming the log if the process exits or ``timeout`` s pass first.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")
    cmd = list(command) if command is not None else stimulus_command(args)
    fd, name = tempfile.mkstemp(prefix="brainsync_stimulus_", suffix=".log",
                                dir=None if log_dir is None else str(log_dir))
    log = Path(name)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if p)
    env["PYTHONUNBUFFERED"] = "1"
    with os.fdopen(fd, "w") as sink:
        proc = subprocess.Popen(cmd, stdout=sink, stderr=subprocess.STDOUT, stdin=stdin,
                                env=env, text=True)
    stim = Stimulus(proc, log)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if READY in stim.text():
                return stim
            if proc.poll() is not None:
                why = _last_error(stim.text())
                raise RuntimeError(f"the stimulus exited with code {proc.returncode} before "
                                   "its window came up" + (f" ({why})" if why else "")
                                   + f"; see {log}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the stimulus did not report '{READY}' within "
                                   f"{timeout:g} s; see {log}")
            time.sleep(0.1)
    except BaseException:   # includes Ctrl-C while waiting: never leave the window behind
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise


def speak(text: str, voice: bool | str = True) -> None:
    """Print ``text`` to stderr and say it aloud with Cartesia text-to-speech (offline:
    espeak-ng, when installed).

    Returns at once; prompts play one after another (see brainsync.voice). ``voice``
    False stays silent; a string picks the Cartesia voice id.
    """
    print(f"[speak] {text}", file=sys.stderr, flush=True)
    if voice is False:
        return
    from . import voice as tts   # lazy: apps that never speak skip the import

    tts.speak(text, voice if isinstance(voice, str) and voice else None)