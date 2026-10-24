#!/usr/bin/env bash
# make-password-hash.sh - set the console password of the BrainSyncOS account (host side).
#
# Usage: scripts/make-password-hash.sh [--remove]
#
# Prompts twice (no echo) and writes a SHA-512 crypt hash to
# config/brainsync-password.hash (gitignored, mode 600); build-iso.sh bakes it
# into the next ISO. The password itself is never stored; it reaches the
# builder container's `openssl passwd -6 -stdin` through a pipe. --remove
# deletes the hash (SSH keys only; the account then gets passwordless sudo).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_config

out="$BRAINSYNCOS_ROOT/config/brainsync-password.hash"
case "${1:-}" in
    --remove) rm -f "$out"; log "removed $out"; exit 0 ;;
    -h|--help) usage_from_header; exit 0 ;;
    '') ;;
    *) die "unknown argument: $1" ;;
esac
[ -t 0 ] || die "run this in a terminal (it prompts for the password)"

printf 'Console password for the BrainSyncOS account: ' >&2
read -rs pw1; echo >&2
printf 'Again: ' >&2
read -rs pw2; echo >&2
[ "$pw1" = "$pw2" ] || die "passwords do not match"
[ "${#pw1}" -ge 8 ] || die "use at least 8 characters"

hash="$(printf '%s\n' "$pw1" | "$BRAINSYNCOS_ROOT/scripts/builder.sh" run openssl passwd -6 -stdin)"
pw1=""; pw2=""
printf '%s\n' "$hash" | grep -Eqx '\$6\$[A-Za-z0-9./]+\$[A-Za-z0-9./]+' || die "unexpected openssl output"
(umask 077; printf '%s\n' "$hash" > "$out")
log "wrote $out; rebuild with scripts/build-iso.sh"
