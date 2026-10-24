# BrainFlow (Cerelog fork) for the Jetson Orin Nano

The Cerelog X8 needs the Cerelog fork of BrainFlow. The copy vendored in the ssvep
reference repository's `third_party/brainflow-cerelog` (see the main README) only has
macOS arm64 dylibs. This folder, `deploy/jetson/brainflow/`, builds the same fork for
Linux aarch64 (JetPack 6 = Ubuntu 22.04, glibc 2.35) and packages it as a wheel,
`out/brainflow-0.0.1-py3-none-linux_aarch64.whl`. The build writes `src/`, `build-src/`
and `out/` next to the script; `.gitignore` keeps them out of git.

Files:

| file | what it is |
| --- | --- |
| `build_brainflow.sh` | clone (if needed) -> patch a copy -> CMake build -> wheel -> verify |
| `cerelog-serial-timeout.patch` | **reconstructed** `connect_v1` raw-mode + 1 s timeout change (see below) |
| `setuptools-pin.patch` | `setuptools<81` in `setup.py`, the vendored copy's local change |
| `Dockerfile` | Ubuntu 22.04 arm64 toolchain (cmake 3.22, g++ 11, python3.12 from deadsnakes, python3.10, patchelf) |
| `verify_wheel.sh` | installs the wheel into a fresh venv, runs `ldd` and the two checks below |
| `check_brainflow.py` | dlopen every .so, Cerelog id / 250 Hz / descriptor, SYNTHETIC_BOARD session, DataFilter bandpass |
| `cerelog_pty_test.py` | fake Cerelog X8 on a pseudo-terminal, streamed through the real Cerelog driver (Linux only) |
| `ab_unpatched.sh` | runs the pty test against a libBoardController.so built without the serial patch |

Package contents (`brainflow/lib`): libBoardController.so, libDataHandler.so,
libMLModule.so, libGanglionLib.so, libMuseLib.so, libSynchroniLib_arm64.so and the
prebuilt Synchroni SDK libsensor_arm64.so. These are the same six libraries as the
vendored macOS dylibs (default CMake options: no BLE, Bluetooth, ONNX or tests), plus
the SDK that macOS gets as a framework. CMake also copies three prebuilt vendor SDKs for
other CPUs (libeego-SDK.so and libunicorn.so are x86-64, libunicorn_raspberry.so is
32-bit ARM). They can't load on aarch64 and are only used by ANT Neuro / g.tec boards,
so the script removes them. The Synchroni wrapper's build-tree RUNPATH is rewritten to
`$ORIGIN`.

Source: https://github.com/shakimiansky/Shared_brainflow-cerelog at
`932b3944bfe6c0f4e29a46b4a34afdeba39edd85`. The script refuses a clone at any other
commit or with local changes; it exports that commit with `git archive` into
`build-src/` and patches and builds only there.

## The serial patch is a reconstruction

The macOS dylibs were built with an uncommitted change to
`src/board_controller/cerelog/cerelog.cpp` that is not in the fork. VENDORED.md
only says it "sets the re-opened serial port to raw mode with a 1 s read timeout in
`connect_v1`". `cerelog-serial-timeout.patch` re-creates that from the description:
after the port is re-opened it calls the fork's own
`serial->set_serial_port_settings (1000, false)` (POSIX: raw 8N1, `VMIN=0`,
`VTIME=10`, flush), before `set_custom_baudrate`, the same call `connect_v2` already
makes. **Compare it with the original change if that can be found** (on the Mac that
built the vendored dylibs, `git diff` in the fork checkout).

A review disassembled `connect_v1` in the vendored macOS dylib. The original makes the
same call (`set_serial_port_settings(1000, false)`, before `set_custom_baudrate`), with
the same error message and return code. The only difference is that the patch also
closes the port on that error path, which is harmless.

Measured on a pty, which starts in canonical mode like a real tty (2026-10-24, Docker
arm64):

- A streaming fake board connects through V1 in 7.7 s with the patch. Without it
  (`ab_unpatched.sh`), the V1 check reads only 2 bytes, and the driver falls back to V2,
  which connects after 13.5 s.
- With a silent port, the unpatched driver blocks after "Re-opening port..." with no
  time limit (it was killed at 90 s). The patched driver gives up on V1 in 1.5 s and
  fails cleanly after 25.5 s.

Whether the V2 fallback would also work for a real V1 (CH340) board was not tested.

`cerelog-serial-timeout.reviewed.patch` is optional. It keeps the same hunk and adds a
second fix, a bug the original also has: the *first* open in `connect_v1` is never set
to raw mode. On a tty that has never been configured, a 0x0A byte in the 9600-baud
handshake goes out as 0x0D 0x0A. That was seen on the pty, and it could affect roughly
1% of first sessions after the device is plugged in. Use the reviewed patch for that
fix, or the plain patch to match the macOS build exactly.

## (a) Rebuild with Docker on a Mac (Apple Silicon, runs arm64 natively)

```bash
cd deploy/jetson/brainflow         # holds Dockerfile, build_brainflow.sh, the patches
docker build --platform linux/arm64 -t brainflow-cerelog-jetson .
docker run --rm --platform linux/arm64 \
    -e VERIFY_PYTHONS="python3.12 python3.10" \
    -v "$PWD":/work brainflow-cerelog-jetson /work/build_brainflow.sh
ls out/                            # brainflow-0.0.1-py3-none-linux_aarch64.whl
```

The clone goes to `./src` the first time (or put one there at the commit above). Copy
the wheel to the Jetson, e.g. `scp out/brainflow-0.0.1-py3-none-linux_aarch64.whl jetson:~/`.

## (b) Build natively on the Jetson

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git patch patchelf file python3-venv
# the kit is in the clone; build as yourself, outside the root-owned /opt/brainsync
# (the patches and checks must sit next to the script):
cp -r /opt/brainsync/deploy/jetson/brainflow ~/brainflow-jetson
cd ~/brainflow-jetson
./build_brainflow.sh                # PYTHON=python3 is used when python3.12 is missing
```

The wheel is pure Python plus ctypes-loaded .so files, so the interpreter that builds it
does not matter (stock 3.10 is fine); it is tagged `py3-none-linux_aarch64`. Use
`JOBS=4` if the 8 GB board runs short of memory while compiling. The script also runs
`verify_wheel.sh` with `python3.12` if present, else `python3` (`SKIP_VERIFY=1` skips it).

## (c) Install into the brainsync venv on the Jetson

brainsync needs Python >= 3.11; JetPack 6 ships 3.10. Either

```bash
sudo apt-get install -y software-properties-common     # provides add-apt-repository
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12 python3.12-venv
cd ~/brainsync && python3.12 -m venv .venv
```

or `uv venv --python 3.12 .venv`. Then

```bash
cd ~/brainsync
.venv/bin/python -m pip install "setuptools<81"
.venv/bin/python -m pip install ~/brainflow-0.0.1-py3-none-linux_aarch64.whl
.venv/bin/python -m pip install -e '.[dev]'     # dev adds pytest for tests/test_board.py
```

`~/brainsync` is a clone of your own. For the systemd service the clone is the root-owned
`/opt/brainsync`: run the same three installs there with `sudo`, as step 2 of
`deploy/jetson/README.md` does.

The wheel's metadata already requires `numpy` and `setuptools<81` (the package imports
`pkg_resources`). If an older brainflow is installed, add `--force-reinstall`. Do not
install `brainflow` from PyPI into this venv: it has no Cerelog board and its id 65
is another device.

## (d) Check it

```bash
.venv/bin/python -c "from brainflow.board_shim import BoardShim, BoardIds; print(BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD))"   # 250
~/brainflow-jetson/verify_wheel.sh ~/brainflow-0.0.1-py3-none-linux_aarch64.whl python3.12   # fresh venv, ldd, both checks
.venv/bin/python -m pytest -q tests/test_board.py   # the real-shim BrainFlow test now runs instead of skipping
```

`verify_wheel.sh` needs `check_brainflow.py` and `cerelog_pty_test.py` next to it in
`~/brainflow-jetson`.

What `verify_wheel.sh` printed in the Ubuntu 22.04.5 arm64 container (glibc 2.35;
newest symbol the libs need is GLIBC_2.34), once with python3.12 / pip 25.0.1 and once
with the stock python3.10 / pip 22.0.2 (so the `py3-none-linux_aarch64` tag installs
with the pip JetPack ships):

```
ldd: no 'not found'
libBoardController.so contains the connect_v1 raw-mode patch
CERELOG_X8_BOARD id: 65 sampling rate: 250
CERELOG_X8_BOARD descr: {'eeg_channels': [1, 2, 3, 4, 5, 6, 7, 8], ..., 'num_rows': 11, 'sampling_rate': 250, 'timestamp_channel': 10}
synthetic: data shape (32, 501) at 250 Hz after 2 s
ALL CHECKS PASSED
prepare_session ok after 7.7 s
got 1000 samples in 4 s (expected ~1000)
CERELOG PTY STREAM PASSED
```

It has not been run on a real Jetson or with the headset yet.

With the headset plugged in:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM*     # V1 (CH340) shows as ttyUSB*, V2 (ESP32-S3 CDC) as ttyACM*
.venv/bin/python -c "
from brainsync.board import CerelogBoard, record
with CerelogBoard(verbose=True) as b:
    X, t = record(b, 5)
print(X.shape, t[-1] - t[0])"
```

### Serial permissions

`/dev/ttyUSB*` and `/dev/ttyACM*` belong to group `dialout`. Without it,
`prepare_session` fails with UNABLE_TO_OPEN_PORT_ERROR:

```bash
sudo usermod -aG dialout "$USER"    # then log out and back in (or reboot)
id -nG | grep -w dialout
```

If `/dev/ttyUSB0` never appears for the V1 board, look at `dmesg | grep -i -e ch34 -e ttyUSB`
(the CH340 needs the kernel's `ch341` driver; not checked on a JetPack kernel here) and
make sure `brltty` is not grabbing the device (a common cause on Ubuntu desktop images).
