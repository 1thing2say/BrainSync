#!/usr/bin/env bash
# Smoke test of the Jetson setup (deploy/jetson/README.md) in containers, behind a TLS proxy.
#
#   deploy/jetson/smoke_test.sh
#   WHEEL=path/to/brainflow-0.0.1-py3-none-linux_aarch64.whl deploy/jetson/smoke_test.sh
#
# It builds an Ubuntu 22.04 arm64 image like a JetPack 6 Jetson: Python 3.12 from deadsnakes,
# the Cerelog BrainFlow wheel, then this checkout with `pip install -e .`, in the order of
# README step 2. On a private Docker network it then runs
#
#   server  python -m brainsync.server --board fake --host 0.0.0.0 --token-file /tmp/token
#   proxy   caddy:2 serving https://jetson.tailtest.ts.net with `tls internal`, the stand-in
#           for `tailscale serve`; its port 443 is published on 127.0.0.1 only
#   client  the same image, talking https/wss to the proxy and trusting only Caddy's root CA
#
# and checks /health, the reference page, a controller that gets a welcome and a selection
# through wss with the page's Origin, a wrong token (close 4001) and a foreign Origin (HTTP
# 403). Each check prints PASS or FAIL; the exit code is 1 when any failed. Containers and
# the network are removed on exit; the images stay as a build cache.
#
# Environment (all optional):
#   WHEEL   the BrainFlow wheel to install (default: brainflow/out/*.whl, built with the kit
#           in brainflow/ first when there is none; that clones the fork and takes a while)
#
# The token never leaves the containers: the client reads it on stdin from the server
# container, and server logs are shown with it redacted. Needs Docker that runs linux/arm64
# (natively on Apple Silicon or on a Jetson), and network access for the image builds.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KIT="$HERE/brainflow"
SITE="jetson.tailtest.ts.net"
IMAGE="brainsync-jetson-smoke"
KIT_IMAGE="brainflow-cerelog-jetson"
RUN="brainsync-smoke-$$"
NET="$RUN-net"
SERVER="$RUN-server"
PROXY="$RUN-proxy"
CLIENT="$RUN-client"
CA_IN_CADDY="/data/caddy/pki/authorities/local/root.crt"
TMP="$(mktemp -d)"
PASSED=0
FAILED=0

cleanup() {
    docker rm -f "$CLIENT" "$PROXY" "$SERVER" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$TMP"
}
trap cleanup EXIT

log() { printf '\n==> %s\n' "$*"; }
pass() { printf 'PASS  %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
summary() { printf '\nsmoke test: %d passed, %d failed\n' "$PASSED" "$FAILED"; }
# A failure that leaves nothing else to check: report it and stop.
fatal() { fail "$*"; summary; exit 1; }
# The server prints its page address with the token in the fragment; never show that.
server_log() {
    docker logs "$SERVER" 2>&1 | sed -E 's/#token=[^[:space:]]*/#token=<redacted>/g' | tail -n 30
}
# wait_for SECONDS COMMAND...: true once COMMAND succeeds, false after SECONDS.
wait_for() {
    local end=$((SECONDS + $1)); shift
    until "$@" >/dev/null 2>&1; do
        ((SECONDS < end)) || return 1
        sleep 0.5
    done
}

command -v docker >/dev/null || { echo "error: docker not found" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "error: the Docker daemon is not running" >&2; exit 2; }

# 1. the BrainFlow wheel
if [[ -n "${WHEEL:-}" ]]; then
    [[ -f "$WHEEL" ]] || { echo "error: WHEEL=$WHEEL does not exist" >&2; exit 2; }
else
    shopt -s nullglob
    wheels=("$KIT"/out/brainflow-*-py3-none-linux_aarch64.whl)
    if ((${#wheels[@]} == 0)); then
        log "no wheel in $KIT/out: building it with the kit (brainflow/INSTALL.md)"
        docker build --platform linux/arm64 -t "$KIT_IMAGE" "$KIT"
        # as this user, so a Linux host (a Jetson) is not left with root-owned src/, out/
        docker run --rm --platform linux/arm64 --user "$(id -u):$(id -g)" -e HOME=/tmp \
            -v "$KIT":/work "$KIT_IMAGE" /work/build_brainflow.sh
        wheels=("$KIT"/out/brainflow-*-py3-none-linux_aarch64.whl)
    fi
    shopt -u nullglob
    ((${#wheels[@]} == 1)) || { echo "error: expected one wheel in $KIT/out" >&2; exit 2; }
    WHEEL="${wheels[0]}"
fi
log "wheel $(basename "$WHEEL") sha256 $(shasum -a 256 "$WHEEL" | cut -d' ' -f1)"

# 2. the image: a copy of this checkout without venvs, data, caches, git or local secrets
CTX="$TMP/context"
mkdir -p "$CTX/wheel" "$CTX/repo"
cp "$WHEEL" "$CTX/wheel/"
rsync -a \
    --exclude '/.venv/' --exclude '/venv/' --exclude '/data/' --exclude '/logs/' \
    --exclude '/.git' --exclude '/.dmux/' --exclude '/.claude/' --exclude 'CLAUDE*.md' \
    --exclude '.env' --exclude '.env.*' --exclude 'node_modules/' --exclude '__pycache__/' \
    --exclude '.pytest_cache/' --exclude '*.egg-info/' \
    --exclude '/deploy/jetson/brainflow/src/' --exclude '/deploy/jetson/brainflow/build-src/' \
    --exclude '/deploy/jetson/brainflow/out/' \
    "$ROOT/" "$CTX/repo/"
cat > "$CTX/Dockerfile" <<'EOF'
# What README step 2 does on a JetPack 6 Jetson (Ubuntu 22.04 aarch64, Python 3.10 stock).
FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates software-properties-common gpg-agent \
 && add-apt-repository -y ppa:deadsnakes/ppa \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --system --no-create-home --shell /usr/sbin/nologin --groups dialout brainsync
WORKDIR /opt/brainsync
RUN python3.12 -m venv .venv && .venv/bin/python -m pip install --quiet "setuptools<81"
COPY wheel/ /tmp/wheel/
RUN .venv/bin/python -m pip install --quiet /tmp/wheel/*.whl
# The dependencies first, in their own layer, so a changed checkout reinstalls only brainsync.
COPY repo/pyproject.toml /tmp/pyproject.toml
RUN .venv/bin/python -c 'import tomllib; print("\n".join(tomllib.load(open("/tmp/pyproject.toml", "rb"))["project"]["dependencies"]))' > /tmp/requirements.txt \
 && .venv/bin/python -m pip install --quiet -r /tmp/requirements.txt
COPY repo/ /opt/brainsync/
RUN .venv/bin/python -m pip install --quiet -e . \
 && .venv/bin/python -m pip show brainflow | grep -x 'Version: 0.0.1'
USER brainsync
ENV PYTHONUNBUFFERED=1 PATH=/opt/brainsync/.venv/bin:$PATH
EOF
log "building $IMAGE (linux/arm64)"
if docker build --platform linux/arm64 --progress=plain -t "$IMAGE" "$CTX" >"$TMP/build.log" 2>&1; then
    pass "image: Ubuntu 22.04 arm64, Python 3.12, the BrainFlow wheel, then pip install -e ."
else
    tail -n 40 "$TMP/build.log"
    fatal "image build (log above)"
fi

# 3. the server on a private network, as the unit runs it but with the fake board
log "starting the server and the proxy"
docker network create "$NET" >/dev/null
docker run -d --platform linux/arm64 --name "$SERVER" --network "$NET" --network-alias brainsync \
    "$IMAGE" python -m brainsync.server --board fake --host 0.0.0.0 --port 8765 \
    --token-file /tmp/token --no-detect >/dev/null
server_up() { docker logs "$SERVER" 2>&1 | grep -q '^BrainSync server on '; }
if wait_for 60 server_up; then
    pass "server: --board fake on 0.0.0.0:8765 inside the network, token in /tmp/token"
else
    server_log
    fatal "server did not start within 60 s (log above)"
fi

# 4. the TLS proxy, the stand-in for `tailscale serve --https=443 http://127.0.0.1:8765`
cat > "$TMP/Caddyfile" <<EOF
{
	skip_install_trust
}

$SITE {
	tls internal
	reverse_proxy brainsync:8765
}
EOF
docker image inspect caddy:2 >/dev/null 2>&1 || docker pull -q --platform linux/arm64 caddy:2 >/dev/null
docker create --platform linux/arm64 --name "$PROXY" --network "$NET" --network-alias "$SITE" \
    -p 127.0.0.1::443 caddy:2 >/dev/null
docker cp "$TMP/Caddyfile" "$PROXY:/etc/caddy/Caddyfile"
docker start "$PROXY" >/dev/null
if wait_for 30 docker exec "$PROXY" test -s "$CA_IN_CADDY"; then
    mkdir -p "$TMP/smoke"
    docker cp "$PROXY:$CA_IN_CADDY" "$TMP/smoke/root.crt"
    pass "proxy: caddy:2 with tls internal for $SITE"
else
    docker logs "$PROXY" 2>&1 | tail -n 30
    fatal "proxy made no local CA within 30 s (log above)"
fi
published="$(docker port "$PROXY" 443/tcp)"
if [[ -n "$published" ]] && ! grep -qv '^127\.0\.0\.1:' <<<"$published"; then
    pass "proxy port 443 published on 127.0.0.1 only ($(tr '\n' ' ' <<<"$published" | xargs))"
else
    fail "proxy port 443 published on more than loopback: $published"
fi
port="$(head -n 1 <<<"$published" | cut -d: -f2)"
health_from_mac() {
    curl -sS --fail --max-time 5 --cacert "$TMP/smoke/root.crt" \
        --resolve "$SITE:$port:127.0.0.1" "https://$SITE:$port/health"
}
if wait_for 20 health_from_mac && health_from_mac | grep -q '"ok": *true'; then
    pass "this machine: GET https://$SITE:$port/health via 127.0.0.1 ($(health_from_mac))"
else
    fail "this machine: GET https://$SITE:$port/health via 127.0.0.1"
fi

# 5. the client checks, from a container on the network; $SITE resolves to the proxy there
cat > "$TMP/smoke/check.py" <<'EOF'
"""Client side of deploy/jetson/smoke_test.sh: checks a BrainSync server behind a TLS proxy.

    python -I check.py SITE < token

The token comes on stdin and is never printed. TLS trusts only /smoke/root.crt, the proxy's
local CA. Prints PASS or FAIL per check and exits 1 when any check failed.
"""
import functools
import json
import re
import secrets
import ssl
import sys
import time
import urllib.request
import warnings
from importlib.metadata import version

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from brainsync.server import client as bsc

SITE = sys.argv[1]
BASE = f"https://{SITE}"
URL = f"wss://{SITE}/ws"
CTX = ssl.create_default_context(cafile="/smoke/root.crt")
TOKEN = sys.stdin.readline().strip()
failed = 0


def check(name, fn):
    global failed
    try:
        detail = fn()
    except Exception as exc:   # ServerError and the rest never carry the token
        failed += 1
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}", flush=True)
    else:
        print(f"PASS  {name}" + (f" ({detail})" if detail else ""), flush=True)


def get(path):
    with urllib.request.urlopen(BASE + path, context=CTX, timeout=10) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def connect_with(origin):
    """websockets' connect with the proxy's CA and a browser's Origin header."""
    return functools.partial(connect, ssl=CTX, origin=origin)


def wheel():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # pkg_resources deprecation
        from brainflow.board_shim import BoardIds, BoardShim
    fs = BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD)
    assert version("brainflow") == "0.0.1" and fs == 250, (version("brainflow"), fs)
    return f"brainflow {version('brainflow')}, CERELOG_X8_BOARD {int(BoardIds.CERELOG_X8_BOARD)} at {fs} Hz"


def health():
    status, ctype, body = get("/health")
    data = json.loads(body)
    assert status == 200 and data.get("ok") is True, (status, data)
    return f"{status} {ctype}, board {data.get('board')!r}"


def page():
    status, ctype, body = get("/")
    assert status == 200 and ctype.startswith("text/html"), (status, ctype)
    html = body.decode()
    assert html.lower().startswith("<!doctype html"), html[:40]
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert assets, "the page references no /static/ files"
    for path in assets:
        status, _, _ = get(path)
        assert status == 200, (path, status)
    return f"index.html and {len(assets)} static files: {', '.join(assets)}"


def selection():
    bsc.ws_connect = connect_with(BASE)
    c = bsc.Client(URL, TOKEN, slots=("yes", "no"), refresh_hz=60.0)
    try:
        welcome = c.connect(timeout=15)
        assert c.fake, "the welcome does not say fake: the server should run --board fake"
        t0 = time.monotonic()
        c.gaze("yes")
        sel = c.until("selection", timeout=90)
        assert sel.get("slot") == "yes", sel
        return (f"welcome freqs {welcome.get('freqs')}, selection {sel['slot']!r} score "
                f"{sel.get('score', float('nan')):.2f} after {time.monotonic() - t0:.1f} s")
    finally:
        c.close()


def wrong_token():
    bsc.ws_connect = connect_with(BASE)
    c = bsc.Client(URL, secrets.token_urlsafe(24), slots=("yes", "no"))
    try:
        c.connect(timeout=15)
    except bsc.ServerError as exc:
        assert exc.close_code == 4001, (exc.code, exc.close_code)
        return f"error {exc.code}, close {exc.close_code}"
    c.close()
    raise AssertionError("the server accepted a wrong token")


def evil_origin():
    try:
        with connect(URL, ssl=CTX, origin="https://evil.example", open_timeout=15):
            pass
    except InvalidStatus as exc:
        assert exc.response.status_code == 403, exc.response.status_code
        return f"HTTP {exc.response.status_code}"
    raise AssertionError("the upgrade went through")


for _ in range(40):   # the proxy may answer 502 until it has reached the server once
    try:
        get("/health")
        break
    except Exception:
        time.sleep(0.5)
check("client: brainflow wheel installed", wheel)
check(f"client: GET {BASE}/health", health)
check(f"client: GET {BASE}/ loads the reference page", page)
check(f"client: {URL} with Origin {BASE} and the token gets welcome, then a selection", selection)
check(f"client: {URL} with a wrong token is closed with 4001", wrong_token)
check(f"client: {URL} with Origin https://evil.example gets HTTP 403", evil_origin)
sys.exit(1 if failed else 0)
EOF
log "client checks"
# Caddy keeps its root certificate at 0600; it is public, and the client runs as brainsync.
chmod 0755 "$TMP/smoke"
chmod 0644 "$TMP/smoke/root.crt" "$TMP/smoke/check.py"
docker create -i --platform linux/arm64 --name "$CLIENT" --network "$NET" \
    "$IMAGE" python -I /smoke/check.py "$SITE" >/dev/null
docker cp "$TMP/smoke" "$CLIENT:/smoke"
set +e
docker exec "$SERVER" cat /tmp/token | docker start -ai "$CLIENT" 2>"$TMP/client.err" | tee "$TMP/client.out"
client_status=("${PIPESTATUS[@]}")
set -e
PASSED=$((PASSED + $(grep -c '^PASS' "$TMP/client.out" || true)))
FAILED=$((FAILED + $(grep -c '^FAIL' "$TMP/client.out" || true)))
if [[ "${client_status[*]}" != "0 0 0" ]]; then
    [[ -s "$TMP/client.err" ]] && tail -n 20 "$TMP/client.err"
    grep -q '^FAIL' "$TMP/client.out" || fail "client checks exited with ${client_status[*]}"
fi
if docker inspect -f '{{.State.Running}}' "$SERVER" | grep -qx true; then
    pass "server still running after the checks"
else
    server_log
    fail "server exited during the checks (log above)"
fi
((FAILED == 0)) || { printf '\nserver log (token redacted):\n'; server_log; }

summary
((FAILED == 0))
