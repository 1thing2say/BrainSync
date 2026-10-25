"""Start the stimulus in its own process (optionally reading its output live), and spoken
prompts."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
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


def launch_stimulus_live(args: Sequence[str], on_line: Callable[[str], None],
                         stdin: int | IO | None = subprocess.PIPE, timeout: float = 60.0,
                         command: Sequence[str] | None = None,
                         log_dir: str | Path | None = None,
                         on_end: Callable[[int], None] | None = None,
                         on_spawn: Callable[[subprocess.Popen], None] | None = None) -> Stimulus:
    """Like launch_stimulus, but every stderr/stdout line also reaches ``on_line`` as it comes.

    A daemon thread reads the process's output pipe, appends each line to the log file (so
    ``Stimulus.text()`` and error messages work as with launch_stimulus) and calls
    ``on_line(line)`` with the line, newline stripped, on that thread. At the end of the
    output it waits for the process and calls ``on_end(exit code)``. Returns once a line
    holds "measured refresh"; every line up to and including it has reached ``on_line`` by
    then. Raises RuntimeError naming the log if the process exits or ``timeout`` s pass first.
    ``on_spawn(proc)`` is called right after the process starts, before the wait, so a caller
    that shuts down meanwhile can end a launch still in progress. The voice bridge
    (brainsync.voice_bridge) uses it to see marker lines live.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")
    cmd = list(command) if command is not None else stimulus_command(args)
    fd, name = tempfile.mkstemp(prefix="brainsync_stimulus_", suffix=".log",
                                dir=None if log_dir is None else str(log_dir))
    os.close(fd)
    log = Path(name)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if p)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=stdin,
                            env=env, text=True, bufsize=1, errors="replace")
    if on_spawn is not None:
        on_spawn(proc)
    ready = threading.Event()

    def pump() -> None:
        with open(log, "a", encoding="utf-8") as sink:
            for raw in proc.stdout:   # type: ignore[union-attr]
                sink.write(raw)
                sink.flush()
                line = raw.rstrip("\r\n")
                try:
                    on_line(line)
                except Exception as exc:   # a broken callback must not stop the log
                    print(f"warning: stimulus line handler failed: {exc}", file=sys.stderr)
                if READY in line:
                    ready.set()
        code = proc.wait()
        ready.set()   # wakes the launcher at once when the process ended early
        if on_end is not None:
            try:
                on_end(code)
            except Exception as exc:
                print(f"warning: stimulus exit handler failed: {exc}", file=sys.stderr)

    threading.Thread(target=pump, name="stimulus-output", daemon=True).start()
    stim = Stimulus(proc, log)
    try:
        if not ready.wait(timeout):
            raise RuntimeError(f"the stimulus did not report '{READY}' within "
                               f"{timeout:g} s; see {log}")
        if READY not in stim.text():
            proc.wait()
            why = _last_error(stim.text())
            raise RuntimeError(f"the stimulus exited with code {proc.returncode} before "
                               "its window came up" + (f" ({why})" if why else "")
                               + f"; see {log}")
        return stim
    except BaseException:   # includes Ctrl-C while waiting: never leave the window behind
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise


def speak(text: str, voice: bool | str = True) -> None:
    """Print ``text`` to stderr and say it aloud with Cartesia text-to-speech.

    Returns at once; prompts play one after another (see brainsync.voice). ``voice``
    False stays silent; a string picks the Cartesia voice id.
    """
    print(f"[speak] {text}", file=sys.stderr, flush=True)
    if voice is False:
        return
    from . import voice as tts   # lazy: apps that never speak skip the import

    tts.speak(text, voice if isinstance(voice, str) and voice else None)