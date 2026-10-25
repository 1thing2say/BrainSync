#!/usr/bin/env bash
# Set up (or update) the BrainSync server on a Jetson Orin Nano in one command: the steps of
# deploy/jetson/README.md and brainflow/INSTALL.md (b)-(c). Safe to run again after a pull.
#
#   sudo git clone https://github.com/1thing2say/BrainSync /opt/brainsync
#   sudo /opt/brainsync/deploy/jetson/install.sh                 # headset: --board cerelog
#   sudo /opt/brainsync/deploy/jetson/install.sh --board fake    # no BrainFlow build, fake EEG
#
# Update: sudo git -C /opt/brainsync pull && sudo /opt/brainsync/deploy/jetson/install.sh
#
# What it does:
#   1. apt packages; Python 3.12 (deadsnakes on Ubuntu 22.04 / JetPack 6, apt on 24.04)
#   2. removes brltty (it grabs the Cerelog V1's CH340 on Ubuntu desktop images) and stops
#      ModemManager (it probes new /dev/ttyACM* ports, the Cerelog V2, with AT commands)
#   3. the `brainsync` service user, in dialout (owns /dev/ttyUSB*, /dev/ttyACM*)
#   4. --board cerelog: builds the Cerelog BrainFlow wheel natively, as the user who ran sudo,
#      in ~/.cache/brainsync-brainflow (once; --rebuild-brainflow to redo it)
#   5. /opt/brainsync/.venv: setuptools<81, the wheel, pip install -e .
#   6. /etc/brainsync/env (0600, for ANTHROPIC_API_KEY), the systemd unit, restart, /health
#
# Environment: JOBS (compile jobs; default from CPUs and memory), WHEEL (use this wheel
# instead of building one).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$HERE/brainflow"
INSTALL_DIR=/opt/brainsync
UNIT=brainsync-server
PORT=8765
BOARD=cerelog
REBUILD=0

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }
log() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --board) BOARD="${2:-}"; shift 2 ;;
        --board=*) BOARD="${1#*=}"; shift ;;
        --rebuild-brainflow) REBUILD=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
[[ "$BOARD" == cerelog || "$BOARD" == fake ]] || die "--board must be cerelog or fake"
((EUID == 0)) || die "run with sudo"
[[ "$ROOT" == "$INSTALL_DIR" ]] || die "the unit runs $INSTALL_DIR; clone the repo there (see the top of this file), not $ROOT"
ARCH="$(uname -m)"

log "machine"
model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "not a Jetson ($ARCH)")"
note "model:  $model"
note "L4T:    $(head -n1 /etc/nv_tegra_release 2>/dev/null || echo none)"
. /etc/os-release
note "Ubuntu: $VERSION_ID ($PRETTY_NAME)"
note "code:   $(git -c safe.directory="$ROOT" -C "$ROOT" log -1 --format='%h %s' 2>/dev/null || echo 'not a git checkout')"
[[ "$ARCH" == aarch64 ]] || note "warning: $ARCH, not aarch64; fine for a container test, not a Jetson"

# 1. packages
log "apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl git file \
    build-essential cmake patch patchelf binutils
if ! apt-cache show python3.12-venv >/dev/null 2>&1; then
    note "python3.12 is not in this Ubuntu's archive: adding the deadsnakes PPA"
    apt-get install -y -q --no-install-recommends software-properties-common gpg-agent
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -q
fi
apt-get install -y -q --no-install-recommends python3.12 python3.12-venv
PY=/usr/bin/python3.12
note "$("$PY" --version)"

# 2. serial-port grabbers
log "serial port"
if dpkg -s brltty >/dev/null 2>&1; then
    apt-get remove -y -q brltty
    note "removed brltty"
fi
if systemctl list-unit-files ModemManager.service >/dev/null 2>&1 \
        && systemctl is-enabled ModemManager.service >/dev/null 2>&1; then
    systemctl disable --now ModemManager.service
    note "disabled ModemManager (undo: systemctl enable --now ModemManager)"
fi
ports="$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true)"
note "USB serial ports now: ${ports:-none (headset not plugged in?)}"

# 3. service user
log "service user"
if ! id brainsync >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin --groups dialout brainsync
    note "created brainsync (groups: $(id -nG brainsync))"
else
    usermod -aG dialout brainsync
    note "brainsync exists (groups: $(id -nG brainsync))"
fi
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root && " $(id -nG "$SUDO_USER") " != *" dialout "* ]]; then
    usermod -aG dialout "$SUDO_USER"
    note "added $SUDO_USER to dialout (log in again before reading the board by hand)"
fi

# 4. BrainFlow wheel
if [[ "$BOARD" == cerelog && -z "${WHEEL:-}" ]]; then
    log "BrainFlow (Cerelog fork) for linux_$ARCH"
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
        builder="$SUDO_USER"
        cache="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.cache/brainsync-brainflow"
    else
        builder=root
        cache=/var/cache/brainsync-brainflow
    fi
    shopt -s nullglob
    wheels=("$cache"/out/brainflow-*-py3-none-linux_"$ARCH".whl)
    shopt -u nullglob
    if ((REBUILD)) || ((${#wheels[@]} == 0)); then
        if [[ -z "${JOBS:-}" ]]; then
            # ~1.5 GB per C++ job keeps a 4 GB or 8 GB board out of swap
            mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
            JOBS=$(( mem_kb / 1500000 )); ((JOBS >= 1)) || JOBS=1
            ((JOBS <= $(nproc))) || JOBS=$(nproc)
        fi
        note "building as $builder in $cache with $JOBS jobs (several minutes)"
        sudo -u "$builder" mkdir -p "$cache"
        sudo -u "$builder" env SRC="$cache/src" BUILD_SRC="$cache/build-src" OUT="$cache/out" \
            JOBS="$JOBS" PYTHON="$PY" HOME="$(getent passwd "$builder" | cut -d: -f6)" \
            "$KIT/build_brainflow.sh"
        wheels=("$cache"/out/brainflow-*-py3-none-linux_"$ARCH".whl)
    else
        note "using the wheel built earlier (--rebuild-brainflow to build it again)"
    fi
    ((${#wheels[@]} == 1)) || die "expected one wheel in $cache/out"
    WHEEL="${wheels[0]}"
fi
[[ -z "${WHEEL:-}" || -f "$WHEEL" ]] || die "WHEEL=$WHEEL does not exist"

# 5. venv
log "venv $INSTALL_DIR/.venv"
VPY="$INSTALL_DIR/.venv/bin/python"
if [[ -x "$VPY" ]] && ! "$VPY" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null; then
    note "the venv is not Python 3.12: making it again"
    rm -rf "$INSTALL_DIR/.venv"
fi
[[ -x "$VPY" ]] || "$PY" -m venv "$INSTALL_DIR/.venv"
"$VPY" -m pip install -q --upgrade pip "setuptools<81"
if [[ -n "${WHEEL:-}" ]]; then
    # first, so nothing pulls the PyPI brainflow (no Cerelog board; its id 65 is another device)
    "$VPY" -m pip install -q --force-reinstall --no-deps "$WHEEL"
    "$VPY" -m pip install -q "$WHEEL"
    note "brainflow wheel: $(basename "$WHEEL")"
fi
"$VPY" -m pip install -q -e "$INSTALL_DIR"
if [[ "$BOARD" == cerelog ]]; then
    # read pip's whole output: `| grep -q` would exit early and, under pipefail, fail on pip's SIGPIPE
    bf_version="$("$VPY" -m pip show brainflow 2>/dev/null | sed -n 's/^Version: //p')"
    [[ "$bf_version" == 0.0.1 ]] || die "the venv's brainflow is ${bf_version:-missing}, not the Cerelog fork (0.0.1)"
    rate="$("$VPY" -c 'from brainflow.board_shim import BoardShim, BoardIds; print(BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD))')"
    [[ "$rate" == 250 ]] || die "Cerelog X8 sampling rate is $rate, expected 250"
    note "Cerelog X8 board loads, 250 Hz"
fi

# 6. config, unit, start
log "systemd unit"
install -d -m 0755 /etc/brainsync
[[ -f /etc/brainsync/env ]] || install -m 0600 /dev/null /etc/brainsync/env
note "/etc/brainsync/env: $(grep -c '^ANTHROPIC_API_KEY=.' /etc/brainsync/env || true) API key line(s) (sudoedit /etc/brainsync/env)"
install -m 0644 "$HERE/$UNIT.service" /etc/systemd/system/
DROPIN="/etc/systemd/system/$UNIT.service.d"
if [[ "$BOARD" == fake ]]; then
    install -d -m 0755 "$DROPIN"
    cat >"$DROPIN/board.conf" <<EOF
# written by deploy/jetson/install.sh --board fake; install.sh --board cerelog removes it
[Service]
ExecStart=
ExecStart=$INSTALL_DIR/.venv/bin/python -m brainsync.server \\
    --board fake --host 127.0.0.1 --port $PORT \\
    --token-file /var/lib/brainsync/token \\
    --log /var/log/brainsync/session.jsonl
EOF
    note "--board fake (drop-in $DROPIN/board.conf)"
else
    rm -f "$DROPIN/board.conf"
    rmdir "$DROPIN" 2>/dev/null || true
fi

if [[ ! -d /run/systemd/system ]]; then
    log "systemd is not running here (a container?): unit installed, not started"
    note "start by hand: sudo -u brainsync $VPY -m brainsync.server --board $BOARD --token-file /tmp/token"
    exit 0
fi
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl restart "$UNIT"
note "waiting for http://127.0.0.1:$PORT/health"
for _ in $(seq 60); do
    if health="$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null)"; then
        note "health: $health"
        log "done: $UNIT is running with --board $BOARD"
        note "logs:   journalctl -u $UNIT -f"
        note "token:  sudo cat /var/lib/brainsync/token"
        note "next:   clients reach it through Tailscale or a tunnel (deploy/jetson/README.md, step 5)"
        exit 0
    fi
    systemctl is-active --quiet "$UNIT" || break
    sleep 1
done
journalctl -u "$UNIT" -n 30 --no-pager | sed -E 's/#token=[^[:space:]]*/#token=<redacted>/g'
die "$UNIT did not answer /health (log above)"
