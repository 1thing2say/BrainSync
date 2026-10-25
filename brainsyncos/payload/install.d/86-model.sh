#!/bin/bash
# 86-model.sh - install the local LLM's model.
#
# Runs inside the installed system (hook.sh, curtin in-target), after
# 60-apt-repo.sh, with the ISO's package repository at $BSOS_REPO_DIR. It
# installs the one brainsyncos-model-* package the repository has (it
# Provides brainsyncos-model): the GGUF file in /opt/brainsyncos/models and
# /etc/brainsyncos/llama.d/model.conf, which brainsyncos-llama.service reads.
# Only the Jetson repository has one by default (PKG_EDITIONS in
# packages/brainsyncos-model/package.env); without one the step says so and
# does nothing, so the VM edition installs no model and the service stays
# off. It then checks model.conf and the model's sha256 (BSOS_MODEL_VERIFY=0
# skips the hash, about 1.3 GB to read).

set -euo pipefail

d="${BSOS_INSTALL_DIR:?}"
# shellcheck source=payload/lib/apt-repo.sh
. "$d/lib/apt-repo.sh"
# shellcheck disable=SC1091  # written by hook.sh
. "$d/device.env"
fail() { echo "86-model.sh: $*" >&2; exit 1; }
conf=/etc/brainsyncos/llama.d/model.conf

bsos_repo_open
# The package names the repository's index lists (the target's own sources are not used).
pkgs="$(apt-cache "${BSOS_APT_OPTS[@]}" dumpavail | sed -n 's/^Package: \(brainsyncos-model-.*\)$/\1/p' | sort -u)"
if [ -z "$pkgs" ]; then
    echo "86-model.sh: ${BSOS_EDITION:-?} edition: the repository has no model package; no model installed (the local LLM stays off)"
    exit 0
fi
[ "$(printf '%s\n' "$pkgs" | wc -l)" = 1 ] || fail "the repository has more than one model package: $(printf '%s\n' "$pkgs" | tr '\n' ' ')"
bsos_apt install "$pkgs"

[ -s "$conf" ] || fail "$pkgs did not install $conf"
BSOS_LLAMA_MODEL=""
BSOS_LLAMA_ALIAS=""
BSOS_LLAMA_CTX_SIZE=""
# shellcheck disable=SC1090  # installed by the model package
. "$conf"
case "$BSOS_LLAMA_MODEL" in
    /opt/brainsyncos/models/*.gguf) ;;
    *) fail "$conf: BSOS_LLAMA_MODEL is '$BSOS_LLAMA_MODEL', not a .gguf in /opt/brainsyncos/models" ;;
esac
[ -s "$BSOS_LLAMA_MODEL" ] || fail "$BSOS_LLAMA_MODEL is missing"
[ "$BSOS_LLAMA_CTX_SIZE" -ge 512 ] 2> /dev/null || fail "$conf: BSOS_LLAMA_CTX_SIZE is '$BSOS_LLAMA_CTX_SIZE', not a context length"
[ -n "$BSOS_LLAMA_ALIAS" ] || fail "$conf: BSOS_LLAMA_ALIAS is empty"
[ "$(head -c 4 "$BSOS_LLAMA_MODEL")" = GGUF ] || fail "$BSOS_LLAMA_MODEL is not a GGUF file"
if [ "${BSOS_MODEL_VERIFY:-1}" = 1 ]; then
    (cd "$(dirname "$BSOS_LLAMA_MODEL")" && sha256sum --quiet -c "$(basename "$BSOS_LLAMA_MODEL").sha256") \
        || fail "$BSOS_LLAMA_MODEL does not match its sha256"
    sum="sha256 verified"
else
    sum="sha256 not checked (BSOS_MODEL_VERIFY=0)"
fi
echo "86-model.sh: $(dpkg-query -W -f '${Package} ${Version}' "$pkgs") installed: $BSOS_LLAMA_MODEL ($(stat -c %s "$BSOS_LLAMA_MODEL") bytes, $sum), alias $BSOS_LLAMA_ALIAS, context $BSOS_LLAMA_CTX_SIZE"
