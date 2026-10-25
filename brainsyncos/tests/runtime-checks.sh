#!/usr/bin/env bash
# runtime-checks.sh - the BrainSync packages installed and run (runtime test image only).
#
# Usage (tests/test_phase3.sh, check `runtime`):
#   docker run ... brainsyncos-runtime-test:TAG bash tests/runtime-checks.sh DEBS_DIR
# DEBS_DIR (relative to brainsyncos/, mounted read-only at /work) holds
# brainsync-runtime_*.deb and brainsync_*.deb. In the stock Ubuntu 24.04 arm64
# image of docker/Dockerfile.runtime-test, as root:
#   - dpkg -i installs both: their Depends are met by a plain Ubuntu with
#     python3.12; dpkg --verify passes; the three YAMLs are conffiles;
#   - every compiled library in the venv finds its system libraries (ldd);
#   - as an ordinary user: BrainFlow reports the Cerelog X8 at 250 Hz; every
#     module of brainsync, app and tools imports
#     (tests/runtime/import_all.py), and under Xvfb PsychoPy opens a window
#     and draws (smoke.py), as do the apps' own renderers (real_renderer.py,
#     chess_renderer.py); every /usr/bin command answers --help;
#   - brainsync-voice starts from the installed layout: the partner page
#     answers 200, /api/health answers, and its log goes to the user's state
#     folder;
#   - nothing was written under /opt/brainsyncos while the apps ran (every
#     module was already byte-compiled).

set -euo pipefail
die() { echo "runtime-checks.sh: ERROR: $*" >&2; exit 1; }
[ "$#" = 1 ] || die "usage: runtime-checks.sh DEBS_DIR"
debs="/work/${1#/work/}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
[ -f /.dockerenv ] || die "run this in the runtime test container, not on a real system"

rt="$(ls "$debs"/brainsync-runtime_*_arm64.deb)"
app="$(ls "$debs"/brainsync_*_arm64.deb)"
echo "== install: $(basename "$rt") ($(stat -c %s "$rt") bytes), $(basename "$app") ($(stat -c %s "$app") bytes)"
# The Docker image is minimized (no /usr/share/doc); install everything, as a device does.
rm -f /etc/dpkg/dpkg.cfg.d/excludes
dpkg -i "$rt" "$app"
touch /tmp/before-run
! dpkg --verify brainsync-runtime brainsync | grep . || die "dpkg --verify failed (above)"
dpkg-query -W -f '${Package} ${Version} ${Status}\n' brainsync-runtime brainsync
[ "$(dpkg-query -W -f '${Conffiles}\n' brainsync | grep -c '^ /etc/brainsync/.*\.yaml ')" = 3 ] \
    || die "the three YAMLs are not conffiles: $(dpkg-query -W -f '${Conffiles}' brainsync)"
[ ! -e /etc/brainsync/secrets.env ] || die "a secrets.env was installed"
echo "installed: venv $(du -sm /opt/brainsyncos/venv | awk '{print $1}') MB, conffiles: $(ls /etc/brainsync | tr '\n' ' ')"

echo "== system libraries"
find /opt/brainsyncos/venv -name '*.so*' -type f -print0 | xargs -0 ldd 2> /dev/null > /tmp/ldd.txt || true
! grep 'not found' /tmp/ldd.txt | sort -u | grep . || die "libraries not found (above)"
grep '=>' /tmp/ldd.txt | awk '{print $3}' | grep -v '^/opt/brainsyncos/' | sort -u > /tmp/libs.txt
echo "$(wc -l < /tmp/libs.txt | tr -d ' ') system libraries, from: $(xargs -r -n1 readlink -f < /tmp/libs.txt | xargs -r dpkg -S 2> /dev/null | cut -d: -f1 | sort -u | tr '\n' ' ')"

useradd -m bsuser
# as_user CMD...: run CMD as bsuser, with its own HOME and no BRAINSYNC_* overrides.
as_user() { setpriv --reuid bsuser --regid bsuser --init-groups env -i HOME=/home/bsuser PATH=/usr/bin:/bin LANG=C.UTF-8 "$@"; }
cp "$here"/runtime/*.py /tmp/
chmod 644 /tmp/*.py

echo "== imports"
# Under Xvfb: import_all.py also imports psychopy.visual, which connects to X on import.
(cd /tmp && as_user xvfb-run -a /opt/brainsyncos/venv/bin/python /tmp/import_all.py) | tee /tmp/import.out
! grep -q '^FAIL' /tmp/import.out || die "imports failed (above)"

echo "== BrainFlow (Cerelog fork)"
out="$(cd /tmp && as_user /opt/brainsyncos/venv/bin/python -W ignore -c 'from brainflow.board_shim import BoardShim, BoardIds
print(BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD), BoardShim.get_eeg_channels(BoardIds.CERELOG_X8_BOARD))')"
echo "Cerelog X8: $out"
[ "$out" = "250 [1, 2, 3, 4, 5, 6, 7, 8]" ] || die "BrainFlow in the venv does not report 250 Hz and EEG channels 1-8"

echo "== PsychoPy under Xvfb"
for t in smoke.py real_renderer.py chess_renderer.py; do
    echo "-- $t"
    (cd /tmp && as_user xvfb-run -a -s '-screen 0 1280x720x24' /opt/brainsyncos/venv/bin/python "/tmp/$t") \
        || die "$t failed"
done

echo "== commands"
n=0
for c in $(dpkg -L brainsync | grep '^/usr/bin/'); do
    [ -L "$c" ] && [ "$(readlink "$c")" = "/opt/brainsyncos/venv/bin/$(basename "$c")" ] || die "$c is not a link into the venv"
    as_user "$c" --help > /tmp/help.out 2>&1 || { cat /tmp/help.out; die "$c --help failed"; }
    n=$((n + 1))
done
[ "$n" -ge 9 ] || die "only $n commands in /usr/bin"
echo "$n commands answer --help: $(dpkg -L brainsync | grep '^/usr/bin/' | xargs -n1 basename | tr '\n' ' ')"

echo "== brainsync-voice"
as_user brainsync-voice > /tmp/voice.log 2>&1 &
pid=$!
ok=0
for _ in $(seq 1 120); do
    if /opt/brainsyncos/venv/bin/python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=2)' 2> /dev/null; then
        ok=1; break
    fi
    kill -0 "$pid" 2> /dev/null || break
    sleep 1
done
[ "$ok" = 1 ] || { cat /tmp/voice.log; die "brainsync-voice did not answer /api/health"; }
/opt/brainsyncos/venv/bin/python - <<'EOF' || { cat /tmp/voice.log; kill "$pid"; die "brainsync-voice answers wrongly"; }
import json, urllib.request
page = urllib.request.urlopen("http://127.0.0.1:8000/", timeout=10)
body = page.read().decode()
assert page.status == 200 and "<html" in body.lower(), (page.status, body[:200])
health = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=10))
print("GET / ->", page.status, f"({len(body)} bytes); /api/health ->", json.dumps(health)[:300])
EOF
kill "$pid"
wait "$pid" 2> /dev/null || true
head -5 /tmp/voice.log
logs=/home/bsuser/.local/state/brainsync/logs
ls "$logs"/*.jsonl > /dev/null 2>&1 || die "no session log in $logs"
echo "session log: $(ls "$logs")"

echo "== nothing written under /opt/brainsyncos at runtime"
! find /opt/brainsyncos -newer /tmp/before-run -print | grep . \
    || die "files written under /opt/brainsyncos (above)"
echo "runtime checks passed"
