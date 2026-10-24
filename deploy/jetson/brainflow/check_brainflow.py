"""Checks an installed brainflow (Cerelog fork) without hardware. Run by verify_wheel.sh.

1. every .so in brainflow/lib dlopens (dependencies resolve)
2. CERELOG_X8_BOARD exists, 250 Hz, its descriptor has 8 EEG channels
3. SYNTHETIC_BOARD: prepare_session / start_stream / get_board_data / release_session
4. DataHandler: detrend + bandpass on the synthetic data
5. Cerelog controller on a port that does not exist fails fast with a BrainFlowError
"""
import ctypes
import glob
import os
import time
import warnings

import numpy as np

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # pkg_resources deprecation, as brainsync does
    import brainflow
    from brainflow.board_shim import (BoardIds, BoardShim, BrainFlowError,
                                      BrainFlowInputParams, LogLevels)
    from brainflow.data_filter import DataFilter, DetrendOperations, FilterTypes

lib = os.path.join(os.path.dirname(brainflow.__file__), "lib")
print("brainflow package:", os.path.dirname(brainflow.__file__))
sos = sorted(glob.glob(os.path.join(lib, "*.so")))
assert sos, "no .so files in " + lib
for so in sos:
    ctypes.CDLL(so)
    print("dlopen ok:", os.path.basename(so))

BoardShim.set_log_level(LogLevels.LEVEL_WARN.value)
cer = BoardIds.CERELOG_X8_BOARD
fs = BoardShim.get_sampling_rate(cer)
print("CERELOG_X8_BOARD id:", int(cer), "sampling rate:", fs)
assert fs == 250
descr = BoardShim.get_board_descr(cer)
print("CERELOG_X8_BOARD descr:", descr)
assert descr["eeg_channels"] == [1, 2, 3, 4, 5, 6, 7, 8], descr
print("eeg rows:", BoardShim.get_eeg_channels(cer),
      "timestamp row:", BoardShim.get_timestamp_channel(cer))

syn = BoardIds.SYNTHETIC_BOARD
board = BoardShim(syn, BrainFlowInputParams())
board.prepare_session()
assert board.is_prepared()
board.start_stream(45000)
time.sleep(2.0)
data = board.get_board_data()
board.stop_stream()
board.release_session()
syn_fs = BoardShim.get_sampling_rate(syn)
print("synthetic: data shape", data.shape, "at", syn_fs, "Hz after 2 s")
assert data.shape[0] == BoardShim.get_num_rows(syn) and data.shape[1] >= syn_fs, data.shape

ch = np.ascontiguousarray(data[BoardShim.get_eeg_channels(syn)[0]])
before = float(np.std(ch))
DataFilter.detrend(ch, DetrendOperations.LINEAR.value)
DataFilter.perform_bandpass(ch, syn_fs, 5.0, 45.0, 4, FilterTypes.BUTTERWORTH.value, 0.0)
print(f"DataHandler bandpass 5-45 Hz: std {before:.3f} -> {float(np.std(ch)):.3f}")
assert np.all(np.isfinite(ch))

params = BrainFlowInputParams()
params.serial_port = "/dev/ttyUSB-does-not-exist"
params.timeout = 15
t0 = time.monotonic()
try:
    BoardShim(cer, params).prepare_session()
    raise AssertionError("prepare_session on a missing port should fail")
except BrainFlowError as exc:
    print(f"cerelog on a missing port: BrainFlowError after {time.monotonic() - t0:.2f} s: {exc}")

print("ALL CHECKS PASSED")
