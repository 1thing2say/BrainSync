"""EEG sources: the Cerelog X8 through the BrainFlow fork, and a fake board for tests.

Both boards share one small interface: ``fs``, ``ch_names``, use as a context manager,
and ``read() -> (X, t)`` returning every sample since the previous read, X in µV shaped
(n_ch, n) and t in unix seconds. ``record(board, seconds)`` collects a fixed duration.
"""
from __future__ import annotations

import glob
import math
import sys
import time
import warnings
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import numpy as np

from .recordings import MONTAGE, POSTERIOR

N_CHANNELS = 8
FS = 250.0
ADC_FULL_SCALE_UV = 4.5 / 24 * 1e6  # ADS1299, 4.5 V reference at gain 24: +-187.5 mV
SERIAL_PATTERNS = (
    "/dev/cu.usbserial-*",
    "/dev/cu.usbmodem*",
    "/dev/cu.wchusbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
)
# Windows has no /dev: COM ports are matched by USB vendor id instead (WCH CH340 on the
# X8 V1, Espressif on the ESP32-S3 V2, Silicon Labs CP210x, FTDI), in this order.
USB_SERIAL_VIDS = (0x1A86, 0x303A, 0x10C4, 0x0403)
POSTERIOR_ROWS = tuple(MONTAGE.index(c) for c in POSTERIOR)  # rows the fake tone goes on


class Board(Protocol):
    fs: float
    ch_names: tuple[str, ...]

    def read(self) -> tuple[np.ndarray, np.ndarray]: ...


def _names(ch_names: Sequence[str] | None, n: int) -> tuple[str, ...]:
    names = tuple(str(c) for c in (MONTAGE if ch_names is None else ch_names))
    if len(names) != n:
        raise ValueError(f"need {n} channel names, got {len(names)}: {list(names)}")
    if len(set(names)) != n:
        raise ValueError(f"channel names must be unique, got {list(names)}")
    return names


def detect_serial_port(patterns: Sequence[str] | None = None) -> str:
    """First USB serial device matching ``patterns`` (in pattern order), or "" if none.

    ``patterns`` None means this platform's default: ``SERIAL_PATTERNS`` on macOS and
    Linux, ``detect_com_port`` on Windows.
    """
    if patterns is None:
        if sys.platform == "win32":
            return detect_com_port()
        patterns = SERIAL_PATTERNS
    for pattern in patterns:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return ""


def detect_com_port(comports: Callable[[], Sequence[Any]] | None = None) -> str:
    """Windows: the COM port of the first known USB serial adapter, or "" if none.

    Adapters rank by ``USB_SERIAL_VIDS``, then by port number. ``comports`` replaces
    pyserial's ``list_ports.comports``, for tests; without pyserial nothing is found.
    """
    if comports is None:
        try:
            from serial.tools import list_ports
        except ImportError:
            return ""
        comports = list_ports.comports
    ports = [p for p in comports() if getattr(p, "vid", None) in USB_SERIAL_VIDS]
    if not ports:
        return ""

    def rank(p: Any) -> tuple[int, int, str]:
        digits = "".join(ch for ch in p.device if ch.isdigit())
        return USB_SERIAL_VIDS.index(p.vid), int(digits or 0), p.device

    return min(ports, key=rank).device


def serial_search_note() -> str:
    """What ``detect_serial_port()`` looks for on this platform, for error messages."""
    if sys.platform == "win32":
        return "COM ports of USB serial adapters (CH340, ESP32-S3, CP210x, FTDI)"
    return ", ".join(SERIAL_PATTERNS)


def _brainflow() -> Any:
    """Import brainflow.board_shim without its pkg_resources deprecation warning."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from brainflow import board_shim
    except ImportError as exc:
        raise ImportError(
            "brainflow with Cerelog X8 support is not installed; copy the brainflow-cerelog "
            f"folder somewhere writable and run `{sys.executable or 'python'} -m pip install "
            "--no-build-isolation <copy>` (needs setuptools<81; on Windows run "
            "deploy/windows/setup.ps1 instead)") from exc
    return board_shim


class CerelogBoard:
    """A BrainFlow streaming session on the Cerelog X8 (8 channels, 250 Hz).

    ``serial_port`` None auto-detects a USB serial device ("" leaves the search to
    BrainFlow). BrainFlow logs only warnings unless ``verbose``. ``shim`` replaces the
    BoardShim, for tests.
    """

    def __init__(self, serial_port: str | None = None, ch_names: Sequence[str] | None = None,
                 timeout: int = 15, buffer_s: float = 600.0, verbose: bool = False,
                 shim: Any | None = None) -> None:
        if buffer_s <= 0:
            raise ValueError(f"buffer_s must be positive, got {buffer_s}")
        self.serial_port = detect_serial_port() if serial_port is None else serial_port
        self.buffer_s = float(buffer_s)
        self._streaming = False
        if shim is not None:
            self._shim = shim
            self.fs = FS
            self._eeg_rows = list(range(1, N_CHANNELS + 1))
            self._ts_row = N_CHANNELS + 2
        else:
            bs = _brainflow()
            board_id = bs.BoardIds.CERELOG_X8_BOARD
            if not verbose:
                bs.BoardShim.set_log_level(bs.LogLevels.LEVEL_WARN.value)
            params = bs.BrainFlowInputParams()
            params.serial_port = self.serial_port
            params.timeout = int(timeout)
            self._shim = bs.BoardShim(board_id, params)
            self.fs = float(bs.BoardShim.get_sampling_rate(board_id))
            self._eeg_rows = list(bs.BoardShim.get_eeg_channels(board_id))
            self._ts_row = int(bs.BoardShim.get_timestamp_channel(board_id))
        self.ch_names = _names(ch_names, len(self._eeg_rows))

    def open(self) -> CerelogBoard:
        """Connect and start streaming into a ``buffer_s`` ring buffer."""
        if not self._streaming:
            port = self.serial_port or "BrainFlow's default ports"
            try:
                self._shim.prepare_session()
                self._shim.start_stream(int(self.buffer_s * self.fs))
            except Exception as exc:  # BrainFlowError carries the driver's exit code
                self.close()
                raise RuntimeError(f"could not start the Cerelog X8 on {port}: {exc}") from exc
            self._streaming = True
        return self

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        """Every sample since the previous read: X in µV (n_ch, n), t unix seconds (n,)."""
        if not self._streaming:
            raise RuntimeError("board is not streaming; use it as a context manager or call open()")
        data = np.asarray(self._shim.get_board_data(), dtype=float)
        if data.ndim != 2 or data.shape[1] == 0:
            return np.empty((len(self.ch_names), 0)), np.empty(0)
        return data[self._eeg_rows] * 1e6, data[self._ts_row].copy()

    def close(self) -> None:
        try:
            if self._shim.is_prepared():
                if self._streaming:
                    self._shim.stop_stream()
                self._shim.release_session()
        finally:
            self._streaming = False

    def __enter__(self) -> CerelogBoard:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeBoard:
    """Synthetic 8-channel board: Gaussian noise everywhere plus a tone on channels 4..7.

    The tone follows the active target, which steps through ``freqs`` every ``switch_s``
    seconds with a continuous phase. Without ``clock`` time only moves through
    ``advance(seconds)``; with one (a callable giving unix seconds, such as time.time)
    ``read`` returns the samples due since the board was created.
    """

    def __init__(self, freqs: Sequence[float], fs: float = FS, switch_s: float = 10.0,
                 amp: float = 2.0, noise: float = 8.0, seed: int = 0,
                 clock: Callable[[], float] | None = None,
                 ch_names: Sequence[str] | None = None, t0: float | None = None) -> None:
        f = np.asarray(freqs, dtype=float).reshape(-1)
        if f.size == 0:
            raise ValueError("FakeBoard needs at least one frequency, got []")
        if fs <= 0:
            raise ValueError(f"fs must be positive, got {fs}")
        if np.any(f <= 0) or np.any(f >= fs / 2):
            raise ValueError(f"frequencies must lie in (0, {fs / 2:g}) Hz, got {f.tolist()}")
        if not switch_s > 0:
            raise ValueError(f"switch_s must be positive, got {switch_s}")
        if amp < 0 or noise < 0:
            raise ValueError(f"amp and noise must be >= 0, got {amp}, {noise}")
        self.freqs = f
        self.fs = float(fs)
        self.switch_s = float(switch_s)
        self.amp = float(amp)
        self.noise = float(noise)
        self.ch_names = _names(ch_names, N_CHANNELS)
        self.clock = clock
        self._rng = np.random.default_rng(seed)
        start = clock() if clock is not None else time.time()
        self.t0 = float(start if t0 is None else t0)
        self._clock0 = start
        self._n = 0          # samples generated so far
        self._due_s = 0.0    # virtual seconds requested through advance()
        self._phase = 0.0
        self._forced: int | None = None
        self._pending: list[tuple[np.ndarray, np.ndarray]] = []

    def __enter__(self) -> FakeBoard:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def open(self) -> FakeBoard:
        return self

    def close(self) -> None:
        pass

    @property
    def virtual(self) -> bool:
        """True when time only moves through advance()."""
        return self.clock is None

    @property
    def elapsed(self) -> float:
        """Seconds of data generated so far."""
        return self._n / self.fs

    @property
    def now(self) -> float:
        """Unix time of the next sample."""
        return self.t0 + self._n / self.fs

    def _scheduled(self, idx: np.ndarray) -> np.ndarray:
        if math.isinf(self.switch_s):
            return np.zeros(idx.shape, dtype=int)
        block = np.floor(idx / (self.switch_s * self.fs) + 1e-9).astype(int)
        return block % self.freqs.size

    def active_target(self) -> int:
        """Target whose tone the next sample carries; -1 while set_target(-1) holds."""
        if self._forced is not None:
            return self._forced
        return int(self._scheduled(np.array([self._n]))[0])

    def set_target(self, target: int | None) -> None:
        """Hold one target (-1: no tone) instead of the schedule; None resumes switching."""
        if target is not None and not -1 <= int(target) < self.freqs.size:
            raise ValueError(f"target must be -1..{self.freqs.size - 1} or None, got {target}")
        self._forced = None if target is None else int(target)

    def _generate(self, n: int) -> None:
        if n <= 0:
            return
        idx = self._n + np.arange(n)
        targets = (np.full(n, self._forced) if self._forced is not None
                   else self._scheduled(idx))
        f = np.where(targets >= 0, self.freqs[np.maximum(targets, 0)], 0.0)
        phase = self._phase + 2 * np.pi * np.cumsum(f) / self.fs
        prev = np.concatenate(([self._phase], phase[:-1]))  # phase at each sample
        self._phase = float(np.mod(phase[-1], 2 * np.pi))
        X = self.noise * self._rng.standard_normal((N_CHANNELS, n))
        tone = np.where(targets >= 0, self.amp * np.sin(prev), 0.0)
        X[list(POSTERIOR_ROWS)] += tone
        t = self.t0 + idx / self.fs
        self._pending.append((X, t))
        self._n += n

    def advance(self, seconds: float) -> None:
        """Generate ``seconds`` more data (virtual time; not with a clock)."""
        if self.clock is not None:
            raise ValueError("advance() is for a FakeBoard without a clock; this one has one")
        if seconds < 0:
            raise ValueError(f"cannot advance by a negative time, got {seconds}")
        self._due_s += float(seconds)
        self._generate(int(math.floor(self._due_s * self.fs + 1e-9)) - self._n)

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        """Every sample since the previous read: X in µV (8, n), t unix seconds (n,)."""
        if self.clock is not None:
            due = int(math.floor((self.clock() - self._clock0) * self.fs + 1e-9))
            self._generate(due - self._n)
        if not self._pending:
            return np.empty((N_CHANNELS, 0)), np.empty(0)
        X = np.concatenate([x for x, _ in self._pending], axis=1)
        t = np.concatenate([s for _, s in self._pending])
        self._pending = []
        return X, t


def record(board: Board, seconds: float, progress: Callable[[float, float], None] | None = None,
           flush: bool = True, stop: Callable[[], bool] | None = None, poll_s: float = 0.05,
           stall_s: float = 5.0, sleep: Callable[[float], None] | None = None,
           clock: Callable[[], float] | None = None,
           keep_on_interrupt: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Collect ``seconds`` of data from ``board``: (X (n_ch, n) µV, t (n,) unix seconds).

    ``flush`` first discards whatever the board had buffered. Collection ends early when
    ``stop()`` turns true or when no sample has arrived for ``stall_s`` seconds (a warning
    goes to stderr); what arrived is returned. Ctrl-C propagates, so callers stop, unless
    ``keep_on_interrupt`` (a session worth saving), when the partial data is returned.
    ``progress(elapsed, seconds)`` is called each time another whole second has passed.
    A FakeBoard without a clock runs in virtual time.
    """
    if seconds < 0:
        raise ValueError(f"seconds must be >= 0, got {seconds}")
    if poll_s <= 0 or stall_s <= 0:
        raise ValueError(f"poll_s and stall_s must be positive, got {poll_s}, {stall_s}")
    if getattr(board, "virtual", False):
        fake: Any = board
        sleep = sleep or fake.advance
        clock = clock or (lambda: fake.elapsed)
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    capacity = int(round(seconds * board.fs))
    buf = np.empty((len(board.ch_names), capacity))
    stamps = np.empty(capacity)
    filled = 0

    def pull() -> int:
        """Move one read into the buffer (dropping overflow); samples the board gave."""
        nonlocal filled
        X, t = board.read()
        take = min(X.shape[1], capacity - filled)
        buf[:, filled:filled + take] = X[:, :take]
        stamps[filled:filled + take] = t[:take]
        filled += take
        return X.shape[1]

    if flush:
        board.read()
    began = clock()
    heard = began
    seconds_reported = 0
    try:
        while filled < capacity:
            sleep(poll_s)
            arrived = pull()
            now = clock()
            if arrived:
                heard = now
            elif now - heard > stall_s:
                print(f"warning: the board sent nothing for {now - heard:.1f} s; keeping the "
                      f"{filled / board.fs:.1f} s recorded so far", file=sys.stderr)
                break
            if progress is not None and int(now - began) > seconds_reported:
                seconds_reported = int(now - began)
                progress(min(now - began, seconds), seconds)
            if stop is not None and stop():
                pull()
                break
    except KeyboardInterrupt:
        if not keep_on_interrupt:
            raise
        print(f"interrupted; keeping {filled / board.fs:.1f} s", file=sys.stderr)
    return buf[:, :filled].copy(), stamps[:filled].copy()
