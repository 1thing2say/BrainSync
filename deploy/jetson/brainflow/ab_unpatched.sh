#!/usr/bin/env bash
# A/B for the reconstructed patch: run cerelog_pty_test.py against libBoardController.so
# built from the UNPATCHED fork (932b3944 as is). Linux only; uses container-local /tmp.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEEL="$(ls "$HERE"/out/brainflow-*-py3-none-linux_*.whl)"
T="$(mktemp -d)"
git -c safe.directory="$HERE/src" -C "$HERE/src" archive 932b3944bfe6c0f4e29a46b4a34afdeba39edd85 | { mkdir -p "$T/src" && tar -x -C "$T/src"; }
cmake -S "$T/src" -B "$T/build" -DCMAKE_BUILD_TYPE=Release -DBRAINFLOW_VERSION=0.0.1 >/dev/null
cmake --build "$T/build" -j "$(nproc)" --target BoardController >/dev/null || exit 1
python3.12 -m venv "$T/venv"
"$T/venv/bin/python" -m pip install --quiet "$WHEEL"
LIB="$("$T/venv/bin/python" -I -c 'import os, warnings; warnings.simplefilter("ignore"); import brainflow; print(os.path.dirname(brainflow.__file__) + "/lib")')"
cp "$T/src/compiled/libBoardController.so" "$LIB/libBoardController.so"
if grep -aq 'Failed to configure re-opened serial port.' "$LIB/libBoardController.so"; then
    echo "unexpected: patch string present"; exit 1
fi
echo "== unpatched libBoardController.so installed; running cerelog_pty_test.py (120 s limit)"
start=$(date +%s)
timeout 120 "$T/venv/bin/python" -I "$HERE/cerelog_pty_test.py"
echo "== exit code $? after $(( $(date +%s) - start )) s"
