"""Stream from a fake Cerelog X8 on a pseudo-terminal through the real Cerelog driver.

Linux only, no hardware. The emulator writes 37-byte packets (0xAB 0xCD start, 24-bit
big-endian channels at bytes 10..33, additive checksum of bytes 2..33 at byte 34) at
250 Hz into the master side of a pty. The driver opens the slave (/dev/pts/N), which
has no V1/V2 hint in its name, so it runs connect_v1 first: open, 9600 baud, handshake,
close, re-open, raw mode + 1 s timeout (the reconstructed patch), 115200, verification
read. Then the BoardShim calls brainsync/board.py's CerelogBoard makes.

The pty starts in canonical mode, as a real tty does, so the V1 verification read only
works because the patch puts the re-opened port in raw mode.
"""
import math
import os
import threading
import time
import warnings

import numpy as np

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from brainflow.board_shim import BoardIds, BoardShim, BrainFlowInputParams, LogLevels

FS = 250
TONE_HZ = 10.0
AMP_UV = [10.0 * (ch + 1) for ch in range(8)]  # a different amplitude on each channel
UV_PER_COUNT = (2 * 4.5 / 24) / 2 ** 24 * 1e6  # the driver's ADS1299 scale


def packet(n: int) -> bytes:
    p = bytearray(37)
    p[0], p[1] = 0xAB, 0xCD
    p[2] = n & 0xFF
    p[3:7] = (n * 4).to_bytes(4, "big")  # board ms counter
    for ch in range(8):
        value = int(round(AMP_UV[ch] * math.sin(2 * math.pi * TONE_HZ * n / FS) / UV_PER_COUNT))
        p[10 + 3 * ch: 13 + 3 * ch] = (value & 0xFFFFFF).to_bytes(3, "big")
    p[34] = sum(p[2:34]) & 0xFF
    p[35], p[36] = 0xDC, 0xBA
    return bytes(p)


master, slave = os.openpty()  # this process keeps the slave open so the pty survives re-opens
port = os.ttyname(slave)
os.set_blocking(master, False)
stop = threading.Event()
sent = [0]


def emulate() -> None:
    t0 = time.monotonic()
    while not stop.is_set():
        due = int((time.monotonic() - t0) * FS)
        while sent[0] < due:
            try:
                os.write(master, packet(sent[0]))
            except (BlockingIOError, OSError):
                pass  # queue full or no reader yet: drop it, as a UART would
            sent[0] += 1
        try:
            os.read(master, 65536)  # discard the handshake and any canonical-mode echo
        except (BlockingIOError, OSError):
            pass
        time.sleep(0.002)


threading.Thread(target=emulate, daemon=True).start()
print("fake Cerelog X8 on", port)

board_id = BoardIds.CERELOG_X8_BOARD
BoardShim.set_log_level(LogLevels.LEVEL_INFO.value)
params = BrainFlowInputParams()
params.serial_port = port
params.timeout = 15
shim = BoardShim(board_id, params)
eeg_rows = list(BoardShim.get_eeg_channels(board_id))
ts_row = int(BoardShim.get_timestamp_channel(board_id))

t0 = time.monotonic()
shim.prepare_session()
print(f"prepare_session ok after {time.monotonic() - t0:.1f} s")
shim.start_stream(int(600 * FS))
shim.get_board_data()  # flush, as brainsync's record() does
time.sleep(4.0)
data = np.asarray(shim.get_board_data(), dtype=float)
shim.stop_stream()
shim.release_session()
del shim  # BoardShim.__del__ calls into ctypes; at interpreter exit that prints a traceback
stop.set()

X, t = data[eeg_rows] * 1e6, data[ts_row]
n = X.shape[1]
print(f"got {n} samples in 4 s (expected ~{4 * FS})")
assert 3 * FS <= n <= 5 * FS, n
dt = np.diff(t)
print(f"timestamp step median {np.median(dt) * 1e3:.2f} ms, monotonic: {bool(np.all(dt > 0))}")
assert np.all(dt > 0) and abs(np.median(dt) - 1 / FS) < 1e-6
amp = np.max(np.abs(X), axis=1)
print("per-channel peak uV:", np.round(amp, 2).tolist(), "expected", AMP_UV)
assert np.allclose(amp, AMP_UV, rtol=0.02), amp
print("CERELOG PTY STREAM PASSED")
