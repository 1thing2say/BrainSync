#!/bin/bash
# 85-llama.sh - the local LLM: llama-server as brainsyncos-llama.service.
#
# Runs inside the installed system (hook.sh, curtin in-target), on both
# editions, after 60-apt-repo.sh (the repository key) and 80-cuda.sh (the
# Jetson's CUDA runtime, in the same repository). Installs brainsyncos-llama
# from the ISO's signed package repository (lib/apt-repo.sh: that repository
# alone, offline), and on a Jetson edition brainsyncos-llama-cuda too, whose
# Depends pull NVIDIA's cuda-cudart-13-2 and libcublas-13-2 from it.
# Then, once per device (a re-run keeps them):
#   - a random API key in /etc/brainsyncos/llama/api-key (root:root 0600),
#     which the service gets through systemd's LoadCredential
#   - the same key as BRAINSYNC_LLAMA_API_KEY in /etc/brainsync/secrets.env,
#     where BCI Voice reads it. The file is created as root:brainsync 0640
#     with a commented template if it is missing; other lines are never
#     touched, and a stale BRAINSYNC_LLAMA_API_KEY line is replaced in place.
# Never at build time: the key must differ on every device.
# It enables brainsyncos-llama.service. The service starts on boot only when
# a model package has installed /etc/brainsyncos/llama.d/model.conf and a
# model in /opt/brainsyncos/models (ConditionPathExists); the VM edition has
# none by default. Last, it checks that llama-server runs here.

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck source=payload/lib/apt-repo.sh
. "$d/lib/apt-repo.sh"
# shellcheck disable=SC1091  # written by hook.sh
. "$d/device.env"
# shellcheck disable=SC1091  # from the build: BRAINSYNC_USER
. "$d/settings.env"
fail() { echo "85-llama.sh: $*" >&2; exit 1; }
edition="${BSOS_EDITION:-}"
pkgs=(brainsyncos-llama)
case "$edition" in
    '') fail "no BSOS_EDITION in $d/device.env" ;;
    vm-*) echo "85-llama.sh: $edition edition: CPU backends only" ;;
    *) pkgs+=(brainsyncos-llama-cuda) ;;
esac
group="${BRAINSYNC_USER:?}"
getent group "$group" > /dev/null || fail "no group $group (30-user.sh creates it)"

bsos_repo_open
bsos_apt install "${pkgs[@]}"

prefix=/opt/brainsyncos/llama
unit=brainsyncos-llama.service
[ -x "$prefix/bin/llama-server" ] || fail "brainsyncos-llama did not install $prefix/bin/llama-server"
[ -f "/usr/lib/systemd/system/$unit" ] || fail "brainsyncos-llama did not install $unit"

# The API key: once per device.
key=/etc/brainsyncos/llama/api-key
install -d -m 700 -o root -g root "$(dirname "$key")"
if ! grep -Eqx '[0-9a-f]{64}' "$key" 2> /dev/null; then
    tmp="$(mktemp "$key.XXXXXX")"
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$tmp"
    echo >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$key"
    echo "85-llama.sh: made a new API key in $key"
else
    echo "85-llama.sh: keeping the API key in $key"
fi
chown root:root "$key"
chmod 600 "$key"
value="$(head -n 1 "$key")"

# BCI Voice's copy of it.
secrets=/etc/brainsync/secrets.env
if [ ! -e "$secrets" ]; then
    install -d -m 755 "$(dirname "$secrets")"
    tmp="$(mktemp "$secrets.XXXXXX")"
    cat > "$tmp" <<'EOF'
# /etc/brainsync/secrets.env - API keys for the BrainSync apps (KEY=value).
# Readable by root and the brainsync group only. Fill in the ones you use:
# ANTHROPIC_API_KEY=
# CARTESIA_API_KEY=
EOF
    chown "root:$group" "$tmp"
    chmod 640 "$tmp"
    mv "$tmp" "$secrets"
    echo "85-llama.sh: created $secrets"
fi
line="BRAINSYNC_LLAMA_API_KEY=$value"
if grep -qx "$line" "$secrets"; then
    echo "85-llama.sh: $secrets already has the key"
else
    tmp="$(mktemp "$secrets.XXXXXX")"
    if grep -q '^BRAINSYNC_LLAMA_API_KEY=' "$secrets"; then
        awk -v l="$line" '/^BRAINSYNC_LLAMA_API_KEY=/ { if (!done) print l; done = 1; next } { print }' "$secrets" > "$tmp"
        echo "85-llama.sh: replaced BRAINSYNC_LLAMA_API_KEY in $secrets"
    else
        cat "$secrets" > "$tmp"
        [ -z "$(tail -c 1 "$secrets")" ] || echo >> "$tmp"
        echo "$line" >> "$tmp"
        echo "85-llama.sh: added BRAINSYNC_LLAMA_API_KEY to $secrets"
    fi
    chown --reference="$secrets" "$tmp"
    chmod --reference="$secrets" "$tmp"
    mv "$tmp" "$secrets"
fi

systemctl enable "$unit" 2>&1 | sed 's/^/85-llama.sh: /'
systemctl is-enabled -q "$unit" || fail "$unit is not enabled"

ver="$("$prefix/bin/llama-server" --version 2>&1 | grep -m1 '^version:')" || fail "llama-server does not run"
model="$(sed -n 's/^BSOS_LLAMA_MODEL=//p' /etc/brainsyncos/llama.d/model.conf 2> /dev/null || true)"
echo "85-llama.sh: installed $(dpkg-query -W -f '${Package} ${Version}\n' "${pkgs[@]}" | awk '{printf "%s%s", s, $0; s = ", "}'); llama-server $ver"
echo "85-llama.sh: $unit enabled; model: ${model:-none yet (the service starts once a model package is installed)}"
