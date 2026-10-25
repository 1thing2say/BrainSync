#!/usr/bin/env bash
# Install a built brainflow wheel into a fresh venv and check it without hardware.
#   ./verify_wheel.sh out/brainflow-0.0.1-py3-none-linux_aarch64.whl [python3.12]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEEL="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
PY="${2:-python3.12}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
printf '\n==> verifying %s with %s\n' "$(basename "$WHEEL")" "$("$PY" --version 2>&1)"
"$PY" -m venv "$TMP/venv"
VPY="$TMP/venv/bin/python"
echo "pip: $("$VPY" -m pip --version)"   # stock venv pip, not upgraded: what the Jetson has
"$VPY" -m pip install --quiet "$WHEEL"
"$VPY" -m pip show brainflow | sed -n '1,2p'
"$VPY" -m pip list 2>/dev/null | grep -iE '^(numpy|setuptools) '

LIB="$("$VPY" -I -c 'import os, warnings; warnings.simplefilter("ignore"); import brainflow; print(os.path.join(os.path.dirname(brainflow.__file__), "lib"))')"
printf '\n-- ldd %s/*.so\n' "$LIB"
missing=0
for so in "$LIB"/*.so; do
    echo "$(basename "$so"): $(file -b "$so" | cut -d, -f1-2)"
    ldd "$so" | sed 's/^/    /'
    if ldd "$so" | grep -q 'not found'; then missing=1; fi
done
[[ $missing == 0 ]] || { echo "FAIL: unresolved libraries above" >&2; exit 1; }
echo "ldd: no 'not found'"
if grep -aq 'Failed to configure re-opened serial port.' "$LIB/libBoardController.so"; then
    echo "libBoardController.so contains the connect_v1 raw-mode patch"
else
    echo "FAIL: patch string missing from libBoardController.so" >&2; exit 1
fi

printf '\n-- check_brainflow.py\n'
"$VPY" -I "$HERE/check_brainflow.py"
if [[ "$(uname -s)" == Linux ]]; then
    printf '\n-- cerelog_pty_test.py\n'
    "$VPY" -I "$HERE/cerelog_pty_test.py"
fi
