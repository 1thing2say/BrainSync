#!/usr/bin/env bash
# BrainSync behind a Cloudflare quick tunnel, for venues where phones cannot reach the Jetson
# by its local address (Wi-Fi with client isolation). See README.md next to this file.
#
#   1. starts `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:$PORT` and waits for
#      its https://<random>.trycloudflare.com address (no Cloudflare account involved);
#   2. starts `$PYTHON -m brainsync.server` on 127.0.0.1:$PORT, allowing that exact Origin;
#   3. once GET /health answers, writes https://<host>/#token=<token> to $LINK_FILE (one line,
#      mode 0600) and announces it on every writable serial console in $SERIAL_DEVICES, again
#      every $ANNOUNCE_EVERY seconds for whoever plugs in later;
#   4. when either process exits: stops the other, deletes $LINK_FILE, says so on the serial
#      consoles and exits 1, so systemd starts everything again (with a new address).
#      SIGTERM, SIGINT or SIGHUP: the same, but it exits 0.
#
# The token never reaches stdout or stderr (the journal): the server's "open ...#token=..."
# line is redacted, and the journal names the host only. Exit codes: 0 stopped by a signal,
# 1 something that a restart may fix, 2 a setting that must be fixed first.
#
# Environment (all optional):
#   BRAINSYNC_DIR        the checkout, the server's working directory (default /opt/brainsync)
#   PYTHON               the interpreter (default $BRAINSYNC_DIR/.venv/bin/python)
#   CLOUDFLARED          the cloudflared binary (default cloudflared, found in PATH)
#   CLOUDFLARED_PROTOCOL empty: cloudflared's default (QUIC on UDP 7844, falling back to
#                        HTTP/2); "http2": TCP 443 only, for venues that block UDP 7844
#   PORT                 the server's port on 127.0.0.1 (default 8765)
#   BOARD                cerelog (default) or fake
#   SERVER_ARGS          more server arguments, split on whitespace, e.g. "--no-detect" or
#                        "--log /home/me/session.jsonl"; --host, --port, --board, --token-file
#                        and --origin are set by this script and win over these. --no-auth (or
#                        any abbreviation of it) is refused: cloudflared connects from
#                        127.0.0.1, so the server's loopback-only rule for it does not protect a
#                        tunnel that the whole internet can reach
#   LINK_WITH_TOKEN      1 (default): the link in $LINK_FILE and on serial carries #token=...,
#                        ready to open; 0: the bare https://<host>/, and the token is read from
#                        TOKEN_FILE by hand
#   TOKEN_FILE           the server's token (default ~/.config/brainsync/token; the server
#                        makes it, mode 0600, on its first run; delete it to get a new one)
#   LINK_FILE            where the link is written (default $HOME/brainsync-link.txt)
#   SERIAL_DEVICES       consoles to announce on, separated by spaces (default "/dev/ttyGS0
#                        /dev/ttyTCU0": the USB-C device-mode serial gadget and the debug
#                        UART); missing or unwritable ones are skipped
#   ANNOUNCE_EVERY       seconds between announcements (default 60; 0 announces once)
#   URL_TIMEOUT          seconds to wait for cloudflared's address (default 90)
#   HEALTH_TIMEOUT       seconds to wait for the server's /health (default 60)
#   SERIAL_TIMEOUT       seconds one serial write may take before it is abandoned (default 2)
#   STOP_TIMEOUT         seconds each process gets to exit after SIGTERM (default 10)
#   QRENCODE             qrencode, for a QR code of the link on serial (default qrencode;
#                        no QR code when it is missing)
set -euo pipefail
shopt -s extglob   # for the *([^[:space:]]) in filter
umask 077

BRAINSYNC_DIR="${BRAINSYNC_DIR:-/opt/brainsync}"
PYTHON="${PYTHON:-$BRAINSYNC_DIR/.venv/bin/python}"
CLOUDFLARED="${CLOUDFLARED:-cloudflared}"
CLOUDFLARED_PROTOCOL="${CLOUDFLARED_PROTOCOL:-}"
PORT="${PORT:-8765}"
BOARD="${BOARD:-cerelog}"
SERVER_ARGS="${SERVER_ARGS:-}"
LINK_WITH_TOKEN="${LINK_WITH_TOKEN:-1}"
TOKEN_FILE="${TOKEN_FILE:-${HOME:?HOME is not set}/.config/brainsync/token}"
LINK_FILE="${LINK_FILE:-$HOME/brainsync-link.txt}"
SERIAL_DEVICES="${SERIAL_DEVICES-/dev/ttyGS0 /dev/ttyTCU0}"
ANNOUNCE_EVERY="${ANNOUNCE_EVERY:-60}"
URL_TIMEOUT="${URL_TIMEOUT:-90}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
SERIAL_TIMEOUT="${SERIAL_TIMEOUT:-2}"
STOP_TIMEOUT="${STOP_TIMEOUT:-10}"
QRENCODE="${QRENCODE:-qrencode}"

CF_PID=""
SRV_PID=""
RUN_DIR=""
TMP_LINK=""
SERIAL_STATE=""
export PYTHONUNBUFFERED=1

log() { printf 'brainsync-tunnel: %s\n' "$*" >&2; }

# filter PREFIX [FILE]: copy a process's output to the journal, each line after PREFIX, with
# any "#token=..." hidden (and to FILE as well). Plain bash read, not awk: mawk, Ubuntu's
# default awk, reads a pipe in blocks, so cloudflared's few lines would sit unread.
filter() {
    local prefix="$1" file="${2:-}" line
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line//\#token=*([^[:space:]])/#token=<redacted>}"
        if [[ -n "$file" ]]; then printf '%s\n' "$line" >>"$file"; fi
        printf '%s%s\n' "$prefix" "$line" >&2
    done
}

alive() { [[ -n "$1" ]] && kill -0 "$1" 2>/dev/null; }

# stop_pid PID: SIGTERM, up to STOP_TIMEOUT s, a second SIGTERM (cloudflared skips its grace
# period on the second), one more second, then SIGKILL. Returns once the process is reaped.
stop_pid() {
    local pid="$1" i
    alive "$pid" || { wait "$pid" 2>/dev/null || true; return 0; }
    kill -TERM "$pid" 2>/dev/null || true
    for ((i = 0; i < STOP_TIMEOUT * 10; i++)); do
        alive "$pid" || break
        sleep 0.1
    done
    if alive "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
        sleep 1
        if alive "$pid"; then kill -KILL "$pid" 2>/dev/null || true; fi
    fi
    wait "$pid" 2>/dev/null || true
}

# Stop both processes (the server first, so its clients get "phase stop" and cloudflared has
# no open connections left to wait for) and delete the link. Safe to call twice.
cleanup() {
    local srv="$SRV_PID" cf="$CF_PID"
    SRV_PID=""
    CF_PID=""
    if [[ -n "$srv" ]]; then stop_pid "$srv"; fi
    if [[ -n "$cf" ]]; then stop_pid "$cf"; fi
    rm -f "$LINK_FILE"
    if [[ -n "$TMP_LINK" ]]; then rm -f "$TMP_LINK"; fi
}

# serial_write FILE DEVICE: copy FILE to DEVICE in the background and give up after
# SERIAL_TIMEOUT s, so a tty that blocks (flow control, a gadget with no host) cannot hang
# the supervisor. Returns 1 when the write was abandoned, 2 when it failed.
serial_write() {
    local w i
    cat "$1" 2>/dev/null >"$2" &
    w=$!
    for ((i = 0; i < SERIAL_TIMEOUT * 10; i++)); do
        alive "$w" || break
        sleep 0.1
    done
    if alive "$w"; then
        kill -KILL "$w" 2>/dev/null || true
        wait "$w" 2>/dev/null || true
        return 1
    fi
    wait "$w" 2>/dev/null || return 2
}

# announce FILE: write FILE to every serial console that exists and is writable. The journal
# gets a line only when the set of consoles that took it changes.
announce() {
    local dev rc state=""
    for dev in $SERIAL_DEVICES; do
        if [[ ! -e "$dev" ]]; then
            state="$state $dev:missing"
        elif [[ ! -c "$dev" ]]; then
            state="$state $dev:not-a-tty"
        elif [[ ! -w "$dev" ]]; then
            state="$state $dev:not-writable"
        else
            rc=0
            serial_write "$1" "$dev" || rc=$?
            case "$rc" in
                0) state="$state $dev:ok" ;;
                1) state="$state $dev:timed-out" ;;
                *) state="$state $dev:write-failed" ;;
            esac
        fi
    done
    if [[ "$state" != "$SERIAL_STATE" ]]; then
        SERIAL_STATE="$state"
        log "serial:${state:- none configured}"
    fi
}

# say TEXT...: a short notice (no token) on the serial consoles, one CRLF line per argument.
say() {
    local f="$RUN_DIR/notice" line
    : >"$f"
    for line in "$@"; do printf '%s\r\n' "$line" >>"$f"; done
    announce "$f"
}

# fail CODE MESSAGE: stop everything, tell the journal and the serial consoles, exit CODE.
fail() {
    local code="$1"
    shift
    trap : TERM INT HUP
    log "$*"
    cleanup
    if [[ -n "$RUN_DIR" ]]; then
        if ((code == 2)); then
            say "" "BrainSync tunnel stopped: $*" \
                "Fix it, then: sudo systemctl restart brainsync-tunnel" ""
        else
            say "" "BrainSync tunnel down, restarting: $*" \
                "A new link follows when it is back up." ""
        fi
    fi
    exit "$code"
}

on_signal() {
    trap : TERM INT HUP
    log "stopping (signal)"
    cleanup
    say "" "BrainSync tunnel stopped." ""
    exit 0
}

on_exit() {
    cleanup
    if [[ -n "$RUN_DIR" ]]; then rm -rf "$RUN_DIR"; fi
}

cf_log_tail() {
    if [[ -s "$RUN_DIR/cloudflared.log" ]]; then
        log "last cloudflared lines:"
        tail -n 15 "$RUN_DIR/cloudflared.log" | sed 's/^/    /' >&2
    fi
}

# The first https://<name>.trycloudflare.com in cloudflared's log, or nothing. api. is the
# address cloudflared asks for a tunnel at, and shows up in its error messages.
tunnel_url() {
    { grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN_DIR/cloudflared.log" 2>/dev/null \
        | grep -Ev '^https://(api|www)\.trycloudflare\.com$' | head -n 1; } || true
}

health() { curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null; }

# -- settings --------------------------------------------------------------------------------

trap on_exit EXIT
trap on_signal TERM INT HUP

for pair in "PORT=$PORT" "ANNOUNCE_EVERY=$ANNOUNCE_EVERY" "URL_TIMEOUT=$URL_TIMEOUT" \
    "HEALTH_TIMEOUT=$HEALTH_TIMEOUT" "SERIAL_TIMEOUT=$SERIAL_TIMEOUT" "STOP_TIMEOUT=$STOP_TIMEOUT"; do
    [[ "${pair#*=}" =~ ^[0-9]+$ ]] || fail 2 "${pair%%=*} must be a whole number, got '${pair#*=}'"
done
((PORT >= 1 && PORT <= 65535)) || fail 2 "PORT must lie in 1..65535, got $PORT"
case "$CLOUDFLARED_PROTOCOL" in
    "" | auto | quic | http2) ;;
    *) fail 2 "CLOUDFLARED_PROTOCOL must be empty, auto, quic or http2, got '$CLOUDFLARED_PROTOCOL'" ;;
esac
case "$LINK_WITH_TOKEN" in
    0 | 1) ;;
    *) fail 2 "LINK_WITH_TOKEN must be 0 or 1, got '$LINK_WITH_TOKEN'" ;;
esac

# Absolute paths, so that the cd below does not change which files they name.
case "$LINK_FILE" in /*) ;; *) LINK_FILE="$PWD/$LINK_FILE" ;; esac
case "$TOKEN_FILE" in /*) ;; *) TOKEN_FILE="$PWD/$TOKEN_FILE" ;; esac
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/brainsync-tunnel.XXXXXX")"
rm -f "$LINK_FILE"   # whatever an earlier run left is stale now

# Quick tunnels do not start when cloudflared finds a config file (that is for named tunnels,
# which need an account). These are the places cloudflared looks.
for dir in "$HOME/.cloudflared" "$HOME/.cloudflare-warp" "$HOME/cloudflare-warp" \
    /etc/cloudflared /usr/local/etc/cloudflared; do
    for name in config.yml config.yaml; do
        if [[ -e "$dir/$name" ]]; then
            fail 2 "$dir/$name exists, and cloudflared refuses a quick tunnel while it does;" \
                "rename it (e.g. to $name.off) to use the quick tunnel"
        fi
    done
done
command -v "$CLOUDFLARED" >/dev/null 2>&1 \
    || fail 2 "cloudflared not found ($CLOUDFLARED); install it with deploy/jetson/cloudflare/install.sh"
command -v curl >/dev/null 2>&1 || fail 2 "curl not found; sudo apt-get install curl"
[[ -d "$BRAINSYNC_DIR" ]] || fail 2 "BRAINSYNC_DIR $BRAINSYNC_DIR is not a directory"
cd "$BRAINSYNC_DIR"   # the server's working directory; relative paths below resolve here
[[ -x "$PYTHON" ]] || fail 2 "PYTHON $PYTHON is not an executable (make the venv, see deploy/jetson/README.md)"
if health; then
    fail 1 "something already answers on 127.0.0.1:$PORT (brainsync-server.service?); it must be free"
fi

read -r -a extra <<<"$SERVER_ARGS" || true
# argparse takes any unambiguous prefix (--no-a, --no-au, ...), so match prefixes, not the name.
for arg in ${extra[@]+"${extra[@]}"}; do
    name="${arg%%=*}"
    if ((${#name} >= 5)) && [[ "--no-auth" == "$name"* ]]; then
        fail 2 "SERVER_ARGS has $name: the tunnel is reachable from the whole internet, so it never runs without the token"
    fi
done

# -- 1. the tunnel ---------------------------------------------------------------------------

cf_args=(tunnel --no-autoupdate --url "http://127.0.0.1:$PORT")
if [[ -n "$CLOUDFLARED_PROTOCOL" ]]; then cf_args+=(--protocol "$CLOUDFLARED_PROTOCOL"); fi
say "" "BrainSync: starting the Cloudflare tunnel..." ""
log "starting $CLOUDFLARED ${cf_args[*]}"
: >"$RUN_DIR/cloudflared.log"
"$CLOUDFLARED" "${cf_args[@]}" > >(filter "cloudflared: " "$RUN_DIR/cloudflared.log") 2>&1 &
CF_PID=$!

deadline=$((SECONDS + URL_TIMEOUT))
url=""
while :; do
    url="$(tunnel_url)"
    [[ -z "$url" ]] || break
    if ! alive "$CF_PID"; then
        status=0
        wait "$CF_PID" || status=$?
        CF_PID=""
        cf_log_tail
        fail 1 "cloudflared exited (status $status) before it gave a trycloudflare address"
    fi
    if ((SECONDS >= deadline)); then
        cf_log_tail
        fail 1 "no trycloudflare address from cloudflared within $URL_TIMEOUT s (no internet yet? captive portal?)"
    fi
    sleep 0.5
done
host="${url#https://}"
log "tunnel address: $url"

# -- 2. the server ---------------------------------------------------------------------------

origin="https://$(printf '%s' "$host" | sed 's/\./\\./g')"
srv_args=(-m brainsync.server ${extra[@]+"${extra[@]}"}
    --host 127.0.0.1 --port "$PORT" --board "$BOARD" --token-file "$TOKEN_FILE" --origin "$origin")
log "starting $PYTHON ${srv_args[*]}"
"$PYTHON" "${srv_args[@]}" > >(filter "") 2>&1 &
SRV_PID=$!

deadline=$((SECONDS + HEALTH_TIMEOUT))
until health; do
    if ! alive "$SRV_PID"; then
        status=0
        wait "$SRV_PID" || status=$?
        SRV_PID=""
        ((status == 2)) && fail 2 "the server refused its arguments (status 2); check SERVER_ARGS and BOARD"
        fail 1 "the server exited (status $status) before it answered /health"
    fi
    alive "$CF_PID" || fail 1 "cloudflared exited while the server was starting"
    ((SECONDS < deadline)) || fail 1 "the server did not answer /health within $HEALTH_TIMEOUT s"
    sleep 0.5
done
[[ -s "$TOKEN_FILE" ]] || fail 1 "the server answers but $TOKEN_FILE is missing or empty"
token=""
IFS= read -r token <"$TOKEN_FILE" || true
token="${token%$'\r'}"
[[ "$token" =~ ^[A-Za-z0-9_-]+$ ]] \
    || fail 2 "the token in $TOKEN_FILE has characters a link cannot carry; delete the file to make a new one"

# -- 3. the link -----------------------------------------------------------------------------

if ((LINK_WITH_TOKEN)); then link="https://$host/#token=$token"; else link="https://$host/"; fi
mkdir -p "$(dirname "$LINK_FILE")"
TMP_LINK="$(mktemp "$(dirname "$LINK_FILE")/.brainsync-link.XXXXXX")"
chmod 600 "$TMP_LINK"
printf '%s\n' "$link" >"$TMP_LINK"
mv -f "$TMP_LINK" "$LINK_FILE"
TMP_LINK=""

banner="$RUN_DIR/announcement"   # holds the token: RUN_DIR is 0700, the file 0600
{
    printf '\r\n'
    printf '================ BrainSync is up ================\r\n'
    printf 'Open on a phone or laptop:\r\n'
    printf '  %s\r\n' "$link"
    if ((LINK_WITH_TOKEN)); then
        printf 'Chrome extension: server wss://%s/ws\r\n' "$host"
        printf '                  token  the part after #token=\r\n'
    else
        printf 'Add #token=<the token> to the link; the token is in %s.\r\n' "$TOKEN_FILE"
        printf 'Chrome extension: server wss://%s/ws and that token\r\n' "$host"
    fi
    printf 'Also in %s on the Jetson.\r\n' "$LINK_FILE"
    if command -v "$QRENCODE" >/dev/null 2>&1; then
        printf '%s' "$link" | "$QRENCODE" -t utf8 -m 2 2>/dev/null \
            | awk '{ printf "%s\r\n", $0 }' || true
    fi
    if ((ANNOUNCE_EVERY > 0)); then
        printf 'A restart gives a new link; this repeats every %s s.\r\n' "$ANNOUNCE_EVERY"
    else
        printf 'A restart gives a new link.\r\n'
    fi
    printf '=================================================\r\n'
} >"$banner"
log "tunnel up: https://$host (link in $LINK_FILE)"

# -- 4. watch --------------------------------------------------------------------------------

announce "$banner"
next=$((SECONDS + ANNOUNCE_EVERY))
while :; do
    sleep 0.5
    for name in server cloudflared; do
        if [[ "$name" == server ]]; then pid="$SRV_PID"; else pid="$CF_PID"; fi
        if ! alive "$pid"; then
            status=0
            wait "$pid" || status=$?
            if [[ "$name" == server ]]; then SRV_PID=""; else CF_PID=""; fi
            [[ "$name" == cloudflared ]] && cf_log_tail
            fail 1 "$name exited (status $status)"
        fi
    done
    if ((ANNOUNCE_EVERY > 0 && SECONDS >= next)); then
        announce "$banner"
        next=$((SECONDS + ANNOUNCE_EVERY))
    fi
done
