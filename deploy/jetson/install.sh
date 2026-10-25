#!/usr/bin/env bash
# Set up (or update) the BrainSync server on a Jetson Orin Nano in one command: the steps of
# deploy/jetson/README.md, brainflow/INSTALL.md (b)-(c) and the Cloudflare tunnel. Safe to run
# again after a pull. Run it with sudo from the login user: the server runs as that user.
#
#   sudo git clone https://github.com/1thing2say/BrainSync /opt/brainsync
#   sudo /opt/brainsync/deploy/jetson/install.sh                 # headset: --board cerelog
#   sudo /opt/brainsync/deploy/jetson/install.sh --board fake    # no BrainFlow build, fake EEG
#
# Update: sudo git -C /opt/brainsync pull && sudo /opt/brainsync/deploy/jetson/install.sh
# Without --board a rerun keeps the board in /etc/brainsync/tunnel.env (cerelog when none).
# With cerelog, plug the headset in first: without it the server exits, and every restart
# gives a new link.
#
# What it does:
#   1. apt packages; Python 3.12 (deadsnakes on Ubuntu 22.04 / JetPack 6, apt on 24.04)
#   2. removes brltty (it grabs the Cerelog V1's CH340 on Ubuntu desktop images) and stops
#      ModemManager (it probes new /dev/ttyACM* ports, the Cerelog V2, with AT commands)
#   3. --board cerelog: builds the Cerelog BrainFlow wheel natively, as the user who ran sudo,
#      in ~/.cache/brainsync-brainflow (once; --rebuild-brainflow to redo it)
#   4. /opt/brainsync/.venv: setuptools<81, the wheel, pip install -e .; checks that the login
#      user can import the server from it (and adds read access to the checkout's tracked
#      files and the venv when a strict umask left them unreadable; untracked files such as
#      a .env stay as they are)
#   5. /etc/brainsync/env (0600, for ANTHROPIC_API_KEY); removes an old brainsync-server.service
#   6. cloudflare/install.sh: cloudflared, brainsync-tunnel.service as the login user (in
#      dialout and tty) with BOARD set, started; then waits for ~/brainsync-link.txt and
#      checks that the link stays the same for 20 s (the server did not restart)
#
# Environment: JOBS (compile jobs; default from CPUs and memory), WHEEL (use this wheel
# instead of building one).
set -euo pipefail
umask 022   # the venv and the checkout stay readable by the login user the server runs as

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$HERE/brainflow"
INSTALL_DIR=/opt/brainsync
UNIT=brainsync-tunnel
OLD_UNIT=brainsync-server
TUNNEL_ENV=/etc/brainsync/tunnel.env
BOARD=""
BOARD_GIVEN=""
REBUILD=0
LINK_WAIT=120
STABLE_WAIT=20

usage() { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; }
log() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# The journal names the host only, but hide a #token= anyway, as the tunnel script does.
redact() { sed -E 's/#token=[^[:space:]]*/#token=<redacted>/g'; }

while (($#)); do
    case "$1" in
        --board) BOARD="${2:-}"; BOARD_GIVEN=1; shift $(($# >= 2 ? 2 : 1)) ;;
        --board=*) BOARD="${1#*=}"; BOARD_GIVEN=1; shift ;;
        --rebuild-brainflow) REBUILD=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
if [[ -z "${BOARD_GIVEN:-}" ]]; then
    # a rerun (an update) keeps the board set before, so a fake setup stays fake
    BOARD="$(sed -n 's/^BOARD=//p' "$TUNNEL_ENV" 2>/dev/null | tail -n 1 || true)"
    BOARD="${BOARD:-cerelog}"
fi
if [[ "$BOARD" != cerelog && "$BOARD" != fake ]]; then
    [[ -n "${BOARD_GIVEN:-}" ]] || die "BOARD=$BOARD in $TUNNEL_ENV: --board must be cerelog or fake"
    die "--board must be cerelog or fake"
fi
((EUID == 0)) || die "run with sudo"
[[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]] \
    || die "run with sudo from the login user (not as root): the server runs as that user"
USER_NAME="$SUDO_USER"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
[[ "$USER_HOME" == /* ]] || die "no home directory for $USER_NAME"
[[ "$ROOT" == "$INSTALL_DIR" ]] || die "the unit runs $INSTALL_DIR; clone the repo there (see the top of this file), not $ROOT"
ARCH="$(uname -m)"
SYSTEMD=0
[[ -d /run/systemd/system ]] && SYSTEMD=1

log "machine"
model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "not a Jetson ($ARCH)")"
note "model:  $model"
note "L4T:    $(head -n1 /etc/nv_tegra_release 2>/dev/null || echo none)"
. /etc/os-release
note "Ubuntu: $VERSION_ID ($PRETTY_NAME)"
note "code:   $(git -c safe.directory="$ROOT" -C "$ROOT" log -1 --format='%h %s' 2>/dev/null || echo 'not a git checkout')"
note "user:   $USER_NAME ($USER_HOME), runs the server"
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
if [[ "$BOARD" == cerelog && -z "$ports" ]]; then
    note "warning: --board cerelog and no headset: the server exits until it is plugged in, and"
    note "         every restart gives a new link. Plug it in now, or use --board fake for now."
fi

# The login user runs the BrainFlow build from the checkout (step 3) and the server from it,
# so a checkout cloned under a strict umask gets read access first. Only the tracked files
# and the venv: an untracked secret in the checkout (a .env with API keys) stays private.
grant_read() {
    find "$INSTALL_DIR" -path "$INSTALL_DIR/.git" -prune -o -type d -exec chmod a+rx {} +
    git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" ls-files -z \
        | while IFS= read -r -d '' f; do
            if [[ -e "$INSTALL_DIR/$f" ]]; then chmod a+rX -- "$INSTALL_DIR/$f"; fi
        done
    if [[ -d "$INSTALL_DIR/.venv" ]]; then chmod -R a+rX "$INSTALL_DIR/.venv"; fi
}
if ! sudo -u "$USER_NAME" test -r "$KIT/build_brainflow.sh"; then
    note "$USER_NAME cannot read the checkout: adding read access to its tracked files"
    grant_read
fi

# 3. BrainFlow wheel
if [[ "$BOARD" == cerelog && -z "${WHEEL:-}" ]]; then
    log "BrainFlow (Cerelog fork) for linux_$ARCH"
    cache="$USER_HOME/.cache/brainsync-brainflow"
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
        note "building as $USER_NAME in $cache with $JOBS jobs (several minutes)"
        sudo -u "$USER_NAME" mkdir -p "$cache"
        sudo -u "$USER_NAME" env SRC="$cache/src" BUILD_SRC="$cache/build-src" OUT="$cache/out" \
            JOBS="$JOBS" PYTHON="$PY" HOME="$USER_HOME" "$KIT/build_brainflow.sh"
        shopt -s nullglob
        wheels=("$cache"/out/brainflow-*-py3-none-linux_"$ARCH".whl)
        shopt -u nullglob
    else
        note "using the wheel built earlier (--rebuild-brainflow to build it again)"
    fi
    ((${#wheels[@]} == 1)) || die "expected one wheel in $cache/out"
    WHEEL="${wheels[0]}"
fi
[[ -z "${WHEEL:-}" || -f "$WHEEL" ]] || die "WHEEL=$WHEEL does not exist"

# 4. venv
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
# The service runs the venv as the login user, so check it as that user (from /, a directory
# every user can enter). A venv made earlier under a stricter umask gets read access added.
user_imports() { (cd / && sudo -u "$USER_NAME" "$VPY" -c 'import brainsync.server') >/dev/null 2>&1; }
if ! user_imports; then
    note "$USER_NAME cannot import brainsync.server from the venv: adding read access to it"
    grant_read
    user_imports || die "$USER_NAME still cannot run $VPY -c 'import brainsync.server' (try it by hand)"
fi
note "$USER_NAME can run the server from the venv"

# 5. config; the unit of earlier versions
log "config"
install -d -m 0755 /etc/brainsync
[[ -f /etc/brainsync/env ]] || install -m 0600 /dev/null /etc/brainsync/env
note "/etc/brainsync/env: $(grep -c '^ANTHROPIC_API_KEY=.' /etc/brainsync/env || true) API key line(s) (sudoedit /etc/brainsync/env)"
if [[ -e "/etc/systemd/system/$OLD_UNIT.service" || -d "/etc/systemd/system/$OLD_UNIT.service.d" ]]; then
    if ((SYSTEMD)); then systemctl disable --now "$OLD_UNIT.service" 2>/dev/null || true; fi
    rm -rf "/etc/systemd/system/$OLD_UNIT.service" "/etc/systemd/system/$OLD_UNIT.service.d"
    if ((SYSTEMD)); then systemctl daemon-reload; fi
    note "removed the old $OLD_UNIT.service: the server now runs inside $UNIT.service"
    [[ ! -d /var/lib/brainsync ]] \
        || note "its token in /var/lib/brainsync is not used any more (sudo rm -rf /var/lib/brainsync /var/log/brainsync)"
fi

# 6. the tunnel, which runs the server
log "Cloudflare tunnel ($UNIT.service, as $USER_NAME, --board $BOARD)"
LINK="$USER_HOME/brainsync-link.txt"
rm -f "$LINK"   # so the wait below sees the new link, not one from before the restart
"$HERE/cloudflare/install.sh" --user "$USER_NAME" --dir "$INSTALL_DIR" --board "$BOARD"

if ((!SYSTEMD)); then
    # cloudflare/install.sh has said "systemd is not running here: unit installed, not started"
    note "start by hand: sudo -u $USER_NAME BOARD=$BOARD $HERE/cloudflare/brainsync-tunnel.sh"
    exit 0
fi
note "waiting up to $LINK_WAIT s for $LINK (Wi-Fi, then Cloudflare)"
for _ in $(seq "$LINK_WAIT"); do
    if [[ -s "$LINK" ]]; then
        # The server answers /health before it opens the board, so a link can appear and go
        # again when the board fails. Done only when the same link is still there a bit later.
        first="$(head -n 1 "$LINK")"
        note "link written; checking for $STABLE_WAIT s that the server stays up"
        sleep "$STABLE_WAIT"
        if ! systemctl is-active --quiet "$UNIT" || [[ ! -s "$LINK" ]] \
                || [[ "$(head -n 1 "$LINK")" != "$first" ]]; then
            journalctl -u "$UNIT" -n 30 --no-pager | redact
            [[ "$BOARD" != cerelog ]] || note "is the headset plugged in? (or: install.sh --board fake)"
            die "the server keeps restarting (log above), each time with a new link: journalctl -u $UNIT -f"
        fi
        host="$(sed -E -n '1s|^https://([^/#]+).*|\1|p' "$LINK")"
        log "done: $UNIT is running with --board $BOARD"
        note "link:   https://${host:-?}/ plus the token: cat $LINK (do not post it)"
        note "        a new link after every restart; also on the serial console"
        note "logs:   journalctl -u $UNIT -f"
        note "next:   API key for point mode: sudoedit /etc/brainsync/env, then"
        note "        sudo systemctl restart $UNIT; settings: /etc/brainsync/tunnel.env"
        note "        (deploy/jetson/cloudflare/README.md)"
        exit 0
    fi
    systemctl is-failed --quiet "$UNIT" && break
    sleep 1
done
journalctl -u "$UNIT" -n 30 --no-pager | redact
die "no link in $LINK after $LINK_WAIT s (log above); the service keeps retrying: journalctl -u $UNIT -f"
