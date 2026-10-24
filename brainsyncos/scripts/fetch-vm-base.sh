#!/usr/bin/env bash
# fetch-vm-base.sh - download and verify the VM edition's Ubuntu ISO (host side).
#
# Usage: scripts/fetch-vm-base.sh [--no-signature] [--force]
#
# Downloads BASE_ISO_URL (config/vm-base-iso.env) to build/downloads/NAME and
# verifies it against the pins: size and sha256. Idempotent: a file that was
# verified before (stamp NAME.verified, keyed on size, mtime, inode, ctime and
# the pinned sha256) is not downloaded or hashed again; an unverified one is
# hashed. An interrupted download resumes from NAME.part. Refuses to start
# without room for the rest of the file plus 1 GiB.
#
# Also fetches Canonical's SHA256SUMS and SHA256SUMS.gpg next to it, requires
# the pinned sha256 to be the one Canonical lists for NAME, and checks the
# signature with gpgv in the builder container (the Ubuntu archive keyring is
# in its image); --no-signature skips that check (e.g. without Docker).
# A mismatch is fatal: a bad download (.part) is deleted, an existing NAME is
# left alone (--force deletes it and downloads again).
# Files are deleted with rm_released (truncate first; see lib.sh).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_vm_config

check_sig=1
force=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-signature) check_sig=0; shift ;;
        --force) force=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_cmd curl

dest="$DOWNLOADS_DIR/$BASE_ISO_NAME"
part="$dest.part"
stamp="$dest.verified"
sums_url="${BASE_ISO_URL%/*}/SHA256SUMS"
mkdir -p "$DOWNLOADS_DIR"

# The stamp covers the file (size, mtime, inode, ctime) and both pins.
stamp_key() { printf '%s %s %s %s %s\n' "$(file_size "$1")" "$(file_mtime "$1")" "$(file_ident "$1")" "$BASE_ISO_SIZE" "$BASE_ISO_SHA256"; }

# verify FILE: size and sha256 against the pins; 0 if they match.
verify() {
    local f="$1" size sum
    size="$(file_size "$f")"
    if [ "$size" != "$BASE_ISO_SIZE" ]; then
        warn "$(basename "$f"): $size bytes, expected $BASE_ISO_SIZE"
        return 1
    fi
    log "hashing $(basename "$f") ($((size / 1024 / 1024)) MiB) ..."
    sum="$(sha256_of "$f")"
    if [ "$sum" != "$BASE_ISO_SHA256" ]; then
        warn "$(basename "$f"): sha256 $sum, expected $BASE_ISO_SHA256"
        return 1
    fi
    log "ok sha256: $sum"
}

# Canonical's checksum list must name our pin, and (optionally) be signed.
check_published_sum() {
    local sums="$DOWNLOADS_DIR/SHA256SUMS" line
    curl -fsSL --retry 3 -o "$sums.tmp" "$sums_url" || die "could not download $sums_url"
    curl -fsSL --retry 3 -o "$sums.gpg.tmp" "$sums_url.gpg" || die "could not download $sums_url.gpg"
    mv "$sums.tmp" "$sums"
    mv "$sums.gpg.tmp" "$sums.gpg"
    line="$(grep -F " *$BASE_ISO_NAME" "$sums" | awk -v n="*$BASE_ISO_NAME" '$2 == n' || true)"
    [ "$line" = "$BASE_ISO_SHA256 *$BASE_ISO_NAME" ] \
        || die "Canonical's SHA256SUMS lists '${line:-nothing}' for $BASE_ISO_NAME, not the pin $BASE_ISO_SHA256"
    log "ok SHA256SUMS: Canonical lists the pinned sha256 for $BASE_ISO_NAME"
    if [ "$check_sig" = 0 ]; then
        warn "--no-signature: SHA256SUMS.gpg not checked"
        return 0
    fi
    command -v docker > /dev/null 2>&1 || die "docker not found; the signature is checked in the builder (or pass --no-signature)"
    "$BRAINSYNCOS_ROOT/scripts/builder.sh" run gpgv --keyring /usr/share/keyrings/ubuntu-archive-keyring.gpg \
        build/downloads/SHA256SUMS.gpg build/downloads/SHA256SUMS > "$sums.gpgv.log" 2>&1 \
        || { cat "$sums.gpgv.log" >&2; die "bad or unknown signature on SHA256SUMS"; }
    grep 'Good signature' "$sums.gpgv.log" | sed 's/^/[bsos:fetch-vm-base.sh] /' >&2
    rm -f "$sums.gpgv.log"
}

if [ -f "$dest" ] && [ "$force" = 1 ]; then
    log "--force: deleting $dest"
    rm_released "$dest" "$stamp"
fi

if [ -f "$dest" ]; then
    if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$(stamp_key "$dest")" ]; then
        log "already downloaded and verified: $dest (stamp $(basename "$stamp"))"
        exit 0
    fi
    verify "$dest" || die "$dest does not match config/vm-base-iso.env; delete it or run with --force"
    check_published_sum
    stamp_key "$dest" > "$stamp"
    log "verified: $dest"
    exit 0
fi

have=0
[ -f "$part" ] && have="$(file_size "$part")"
[ "$have" -le "$BASE_ISO_SIZE" ] || { rm_released "$part"; die "$part was larger than the ISO; deleted it, run again"; }
need_gb=$(( (BASE_ISO_SIZE - have) / 1024 / 1024 / 1024 + 2 ))
check_free_space "$DOWNLOADS_DIR" "$need_gb"

if [ "$have" -lt "$BASE_ISO_SIZE" ]; then
    if [ "$have" -gt 0 ]; then
        log "resuming $BASE_ISO_NAME at $((have / 1024 / 1024)) of $((BASE_ISO_SIZE / 1024 / 1024)) MiB"
    else
        log "downloading $BASE_ISO_URL ($((BASE_ISO_SIZE / 1024 / 1024)) MiB)"
    fi
    curl -fL --retry 5 --retry-delay 5 -C - -o "$part" "$BASE_ISO_URL" \
        || die "download failed (run again to resume): $BASE_ISO_URL"
fi
if ! verify "$part"; then
    rm_released "$part"
    die "downloaded file does not match the pin; deleted it"
fi
check_published_sum
mv "$part" "$dest"
stamp_key "$dest" > "$stamp"
log "downloaded and verified: $dest"
