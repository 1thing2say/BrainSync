#!/usr/bin/env bash
# Shared helpers for BrainSyncOS scripts. Source it; do not execute it.
# Must stay compatible with macOS /bin/bash 3.2 (host) and bash 5 (builder
# container): no associative arrays, no ${v,,}, no mapfile/readarray, and no
# "${arr[@]}" on possibly-empty arrays under set -u.

# Path/config variables defined here are used by the scripts that source it.
# shellcheck disable=SC2034

set -euo pipefail

BSOS_SCRIPT="${BSOS_SCRIPT:-$(basename "$0")}"

log()  { printf '[bsos:%s] %s\n' "$BSOS_SCRIPT" "$*" >&2; }
warn() { printf '[bsos:%s] WARN: %s\n' "$BSOS_SCRIPT" "$*" >&2; }
die()  { printf '[bsos:%s] ERROR: %s\n' "$BSOS_SCRIPT" "$*" >&2; exit 1; }

# Root of the brainsyncos/ tree (the directory holding config/, scripts/, ...).
if [ -z "${BRAINSYNCOS_ROOT:-}" ]; then
    BRAINSYNCOS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi
export BRAINSYNCOS_ROOT

BSOS_CONFIG="${BSOS_CONFIG:-$BRAINSYNCOS_ROOT/config/base-iso.env}"
BSOS_PRODUCT_CONFIG="${BSOS_PRODUCT_CONFIG:-$BRAINSYNCOS_ROOT/config/brainsyncos.env}"
# VM edition (stock Ubuntu arm64 for UTM): its own base ISO pins and product config.
BSOS_VM_CONFIG="${BSOS_VM_CONFIG:-$BRAINSYNCOS_ROOT/config/vm-base-iso.env}"
BSOS_VM_PRODUCT_CONFIG="${BSOS_VM_PRODUCT_CONFIG:-$BRAINSYNCOS_ROOT/config/vm.env}"
BUILD_DIR="$BRAINSYNCOS_ROOT/build"
ISO_ROOT="$BUILD_DIR/iso-root"
META_DIR="$BUILD_DIR/meta"
REPORTS_DIR="$BUILD_DIR/reports"
OUT_DIR="$BUILD_DIR/out"
TMP_DIR="$BUILD_DIR/tmp"
STAGE_DIR="$BUILD_DIR/stage"
VM_STAGE_DIR="$BUILD_DIR/stage-vm"
# Downloaded base ISOs (VM edition). Inside brainsyncos/, so the builder sees
# them under /work without another mount.
DOWNLOADS_DIR="$BUILD_DIR/downloads"

BSOS_REQUIRED_KEYS="BASE_ISO_NAME BASE_ISO_SHA256 BASE_ISO_SIZE BASE_ISO_VOLID
BASE_ISO_DISK_INFO_BUILD BASE_ESP_START_SECTOR BASE_ESP_SIZE_SECTORS
BASE_ESP_SHA256 BASE_SYSTEM_AREA_SHA256 ISO_COMPLIANCE L4T_RELEASE UBUNTU_RELEASE TEGRA_KERNEL LIVE_KERNEL
CASPER_LAYERFS_PATH AUTOINSTALL_LAYER KERNEL_LAYER_MANIFEST BOOT_TARGETS
BUILDER_IMAGE_NAME BUILDER_PLATFORM MIN_FREE_GB"

BSOS_PRODUCT_REQUIRED_KEYS="BRAINSYNCOS_VERSION TARGET_BOARD HOOKED_AUTOINSTALL
INSTALL_TARGET_DEFAULT GRUB_TIMEOUT BRAINSYNC_USER HOSTNAME_PREFIX"

BSOS_VM_REQUIRED_KEYS="BASE_ISO_NAME BASE_ISO_URL BASE_ISO_SHA256 BASE_ISO_SIZE BASE_ISO_VOLID
BASE_ISO_DISK_INFO_BUILD BASE_ISO_ARCH BASE_ESP_START_SECTOR BASE_ESP_SIZE_SECTORS
BASE_ESP_SHA256 BASE_SYSTEM_AREA_SHA256 ISO_COMPLIANCE UBUNTU_RELEASE UBUNTU_CODENAME MIN_FREE_GB"

BSOS_VM_PRODUCT_REQUIRED_KEYS="EDITION VM_GRUB_TIMEOUT VM_INSTALL_KERNEL VM_CONSOLES
VM_NAME VM_BACKEND VM_MEMORY_MB VM_CPU_CORES VM_DISK_GB"

# load_env_file FILE KEYS: source FILE after checking every line is plain
# KEY=value (so it stays usable as a docker --env-file and sourcing it cannot
# run code), then require each of KEYS to be non-empty.
load_env_file() {
    local f="$1" keys="$2" n=0 line key
    [ -r "$f" ] || die "config not found: $f"
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        case "$line" in ''|'#'*) continue ;; esac
        if ! printf '%s\n' "$line" | grep -Eq '^[A-Z][A-Z0-9_]*=[A-Za-z0-9._,:/+@=-]*$'; then
            die "$f:$n: not a plain KEY=value line: $line"
        fi
    done < "$f"
    set -a
    # shellcheck disable=SC1090
    . "$f"
    set +a
    for key in $keys; do
        eval "[ -n \"\${$key:-}\" ]" || die "config $f: $key is missing or empty"
    done
}

# config/base-iso.env: the pinned vendor ISO.
load_config() { load_env_file "$BSOS_CONFIG" "$BSOS_REQUIRED_KEYS"; }

# config/brainsyncos.env: what we build on top of it (after load_config).
load_product_config() {
    load_env_file "$BSOS_PRODUCT_CONFIG" "$BSOS_PRODUCT_REQUIRED_KEYS"
    printf '%s\n' "$BRAINSYNCOS_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
        || die "BRAINSYNCOS_VERSION must be MAJOR.MINOR.PATCH, got $BRAINSYNCOS_VERSION"
    case "$INSTALL_TARGET_DEFAULT" in
        nvme|microsd|usb) ;;
        *) die "INSTALL_TARGET_DEFAULT must be nvme, microsd or usb, got $INSTALL_TARGET_DEFAULT" ;;
    esac
    printf '%s\n' "$GRUB_TIMEOUT" | grep -Eq '^(-1|[0-9]+)$' \
        || die "GRUB_TIMEOUT must be -1 or a number of seconds, got $GRUB_TIMEOUT"
    printf '%s\n' "$BRAINSYNC_USER" | grep -Eq '^[a-z_][a-z0-9_-]{0,30}$' \
        || die "BRAINSYNC_USER must be a valid Linux user name, got $BRAINSYNC_USER"
    case "$BRAINSYNC_USER" in
        root|nvidia|ubuntu) die "BRAINSYNC_USER must not be $BRAINSYNC_USER" ;;
    esac
    # Leaves room for "-" and a 6-character serial suffix within 63 characters.
    printf '%s\n' "$HOSTNAME_PREFIX" | grep -Eq '^[a-z0-9]([a-z0-9-]{0,54}[a-z0-9])?$' \
        || die "HOSTNAME_PREFIX must be lowercase letters, digits and inner hyphens, got $HOSTNAME_PREFIX"
}

# product_iso_name: basename (no .iso) of the build-iso.sh output, e.g.
# brainsyncos-0.1.0-orin-nano for TARGET_BOARD=jetson-orin-nano-devkit.
product_iso_name() {
    printf 'brainsyncos-%s-%s\n' "$BRAINSYNCOS_VERSION" "$(jetson_edition)"
}

# jetson_edition: the Jetson edition's name (release file EDITION=), e.g.
# orin-nano for TARGET_BOARD=jetson-orin-nano-devkit.
jetson_edition() {
    local board="${TARGET_BOARD#jetson-}"
    printf '%s\n' "${board%-devkit}"
}

# --- VM edition (stock Ubuntu Server arm64, for UTM) ---------------------------
# A script works on one edition: it calls load_config + load_product_config
# (Jetson) or load_vm_config + load_vm_product_config (VM), never both.

# config/vm-base-iso.env: the pinned Ubuntu ISO (same key names as base-iso.env).
load_vm_config() {
    load_env_file "$BSOS_VM_CONFIG" "$BSOS_VM_REQUIRED_KEYS"
    case "$BASE_ISO_URL" in
        https://*/"$BASE_ISO_NAME") ;;
        *) die "BASE_ISO_URL must be an https URL ending in /$BASE_ISO_NAME, got $BASE_ISO_URL" ;;
    esac
}

# config/brainsyncos.env (version, account, hostname prefix; shared with the
# Jetson edition) and config/vm.env (the VM edition's own settings).
load_vm_product_config() {
    load_product_config
    load_env_file "$BSOS_VM_PRODUCT_CONFIG" "$BSOS_VM_PRODUCT_REQUIRED_KEYS"
    [ "$EDITION" = vm-arm64 ] || die "EDITION must be vm-arm64, got $EDITION"
    printf '%s\n' "$VM_GRUB_TIMEOUT" | grep -Eq '^(-1|[0-9]+)$' \
        || die "VM_GRUB_TIMEOUT must be -1 or a number of seconds, got $VM_GRUB_TIMEOUT"
    case "$VM_INSTALL_KERNEL" in
        ga|hwe) ;;
        *) die "VM_INSTALL_KERNEL must be ga or hwe, got $VM_INSTALL_KERNEL" ;;
    esac
    printf '%s\n' "$VM_CONSOLES" | grep -Eq '^[a-zA-Z][a-zA-Z0-9]*(,[a-zA-Z][a-zA-Z0-9]*)*$' \
        || die "VM_CONSOLES must be a comma-separated list of console devices, got $VM_CONSOLES"
    check_vm_name "$VM_NAME"
    case "$VM_BACKEND" in
        apple|qemu) ;;
        *) die "VM_BACKEND must be apple or qemu, got $VM_BACKEND" ;;
    esac
    check_vm_number VM_MEMORY_MB "$VM_MEMORY_MB" 2048 65536
    check_vm_number VM_CPU_CORES "$VM_CPU_CORES" 1 64
    check_vm_number VM_DISK_GB "$VM_DISK_GB" 16 2048
}

# check_vm_name NAME: a UTM VM name BrainSyncOS scripts may create or touch.
check_vm_name() {
    printf '%s\n' "$1" | grep -Eq '^BrainSyncOS[A-Za-z0-9._-]{0,48}$' \
        || die "VM name must start with BrainSyncOS (letters, digits, . _ - only), got '$1'"
}

# check_vm_number KEY VALUE MIN MAX
check_vm_number() {
    printf '%s\n' "$2" | grep -Eq '^[0-9]+$' && [ "$2" -ge "$3" ] && [ "$2" -le "$4" ] \
        || die "$1 must be a number from $3 to $4, got $2"
}

# vm_iso_name: basename (no .iso) of the build-vm-iso.sh output, e.g.
# brainsyncos-0.1.0-vm-arm64.
vm_iso_name() {
    printf 'brainsyncos-%s-%s\n' "$BRAINSYNCOS_VERSION" "$EDITION"
}

# resolve_vm_base_iso: the downloaded Ubuntu ISO (scripts/fetch-vm-base.sh),
# build/downloads/<name>; on the host and in the builder (/work/build/...).
# Deliberately not $BASE_ISO: builder.sh sets that to the Jetson ISO.
resolve_vm_base_iso() {
    local p="$DOWNLOADS_DIR/$BASE_ISO_NAME"
    [ -f "$p" ] || die "VM base ISO not found at $p (run scripts/fetch-vm-base.sh)"
    [ -r "$p" ] || die "VM base ISO not readable: $p"
    printf '%s\n' "$p"
}

# config_volid: BASE_ISO_VOLID with _ turned back into spaces (config values
# cannot contain spaces; Ubuntu's volume id has them, NVIDIA's has neither).
config_volid() {
    printf '%s\n' "$BASE_ISO_VOLID" | tr '_' ' '
}

# --- product ISO builds (build-iso.sh, build-vm-iso.sh) --------------------------

# parse_build_args DEFAULT_NAME ARG...: the options both product builds take.
# Sets BUILD_ID, BUILD_NAME, KEYS_FILE, HASH_FILE.
parse_build_args() {
    BUILD_NAME="$1"
    shift
    BUILD_ID="${BSOS_GIT_REV:-unknown}"
    KEYS_FILE="$BRAINSYNCOS_ROOT/config/authorized_keys"
    HASH_FILE="$BRAINSYNCOS_ROOT/config/brainsync-password.hash"
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --build-id) BUILD_ID="${2:?--build-id needs a value}"; shift 2 ;;
            --name) BUILD_NAME="${2:?--name needs a value}"; shift 2 ;;
            --authorized-keys) KEYS_FILE="${2:?--authorized-keys needs a file}"; shift 2 ;;
            --password-hash) HASH_FILE="${2:?--password-hash needs a file}"; shift 2 ;;
            -h|--help) usage_from_header; exit 0 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    printf '%s\n' "$BUILD_ID" | grep -Eq '^[A-Za-z0-9._+-]+$' || die "invalid build id: $BUILD_ID"
    case "$BUILD_NAME" in */*|.*|'') die "invalid --name: $BUILD_NAME" ;; esac
}

# validate_logins: check KEYS_FILE / HASH_FILE (either may be absent or
# empty, not both). Sets HAVE_KEYS and HAVE_HASH to 0 or 1.
validate_logins() {
    HAVE_KEYS=0
    HAVE_HASH=0
    if [ -s "$KEYS_FILE" ]; then
        ! grep -q 'PRIVATE KEY' "$KEYS_FILE" || die "$KEYS_FILE contains a PRIVATE key; use the .pub file"
        ssh-keygen -l -f "$KEYS_FILE" > /dev/null 2>&1 || die "$KEYS_FILE is not a valid authorized_keys file"
        HAVE_KEYS=1
        log "SSH keys for $BRAINSYNC_USER: $(ssh-keygen -l -f "$KEYS_FILE" | wc -l | tr -d ' ') from ${KEYS_FILE#$BRAINSYNCOS_ROOT/}"
    fi
    if [ -s "$HASH_FILE" ]; then
        [ "$(grep -c . "$HASH_FILE")" = 1 ] && grep -Eqx '\$(6|5|y)\$[A-Za-z0-9./$]+' "$HASH_FILE" \
            || die "$HASH_FILE is not a single crypt(3) hash line (\$6\$..., \$y\$...); make one with scripts/make-password-hash.sh"
        HAVE_HASH=1
        log "console password for $BRAINSYNC_USER: hash from ${HASH_FILE#$BRAINSYNCOS_ROOT/}"
    fi
    [ "$HAVE_KEYS" = 1 ] || [ "$HAVE_HASH" = 1 ] \
        || die "no way to log in: add SSH public keys to config/authorized_keys and/or run scripts/make-password-hash.sh"
}

# stage_payload STAGE < RELEASE: payload/ -> STAGE/brainsyncos, with the
# release file read from stdin, settings.env and the logins (validate_logins).
stage_payload() {
    local d="$1/brainsyncos"
    cp -R "$BRAINSYNCOS_ROOT/payload" "$d"
    cat > "$d/release"
    cat > "$d/settings.env" <<EOF
BRAINSYNC_USER=$BRAINSYNC_USER
HOSTNAME_PREFIX=$HOSTNAME_PREFIX
EOF
    mkdir -p "$d/config"
    [ "$HAVE_KEYS" = 0 ] || grep -v '^[[:space:]]*$' "$KEYS_FILE" > "$d/config/authorized_keys"
    [ "$HAVE_HASH" = 0 ] || cp "$HASH_FILE" "$d/config/password.hash"
}

# merge_stage_md5sums VENDOR_MD5SUM_TXT STAGE: STAGE/md5sum.txt = the vendor
# entries plus every staged file (scripts/merge-md5sums.py).
merge_stage_md5sums() {
    local vendor="$1" stage="$2" tmp="$2.md5sum.tmp"
    python3 "$BRAINSYNCOS_ROOT/scripts/merge-md5sums.py" "$vendor" "$stage" > "$tmp"
    mv "$tmp" "$stage/md5sum.txt"
    log "md5sum.txt: $(wc -l < "$stage/md5sum.txt" | tr -d ' ') entries (vendor $(wc -l < "$vendor" | tr -d ' '))"
}

# write_stage_iso BASE STAGE NAME ISO_PATH...: OUT_DIR/NAME.iso = BASE with its
# boot setup replayed and STAGE mapped over /; the given ISO paths (what STAGE
# adds or replaces) get the vendor's 0:0 ownership and read-only modes. Then
# finalize_iso and NAME.manifest.txt (sha256 of every staged file).
write_stage_iso() {
    local base="$1" stage="$2" name="$3"
    shift 3
    local out="$OUT_DIR/$name.iso" partial="$OUT_DIR/.$name.iso.partial" xlog="$OUT_DIR/$name.xorriso.log"
    rm_released "$partial" "$out" "$out.sha256"
    # shellcheck disable=SC2064  # expand the paths now: they are locals
    trap "rm_released '$partial' '$partial.sfdisk-V'" EXIT
    log "writing $out"
    write_replayed_iso "$base" "$partial" "$xlog" \
        -map "$stage" / \
        -chown_r 0 "$@" -- \
        -chgrp_r 0 "$@" -- \
        -chmod_r a-w,a+r "$@" --
    finalize_iso "$partial" "$out"
    trap - EXIT
    (cd "$stage" && find . -type f | LC_ALL=C sort | xargs sha256sum) > "$OUT_DIR/$name.manifest.txt"
    log "manifest: $OUT_DIR/$name.manifest.txt ($(wc -l < "$OUT_DIR/$name.manifest.txt" | tr -d ' ') files)"
}

require_cmd() {
    local c
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"
    done
}

in_container() {
    [ "${BRAINSYNCOS_IN_CONTAINER:-0}" = 1 ] || [ -f /.dockerenv ]
}

require_container() {
    in_container || die "run this inside the builder: scripts/builder.sh run scripts/$BSOS_SCRIPT $*"
}

# Locate the base ISO: $BASE_ISO, else /in/<name> (container), else ~/Downloads/<name>.
resolve_base_iso() {
    local p
    if [ -n "${BASE_ISO:-}" ]; then
        p="$BASE_ISO"
    elif in_container; then
        p="/in/$BASE_ISO_NAME"
    else
        p="$HOME/Downloads/$BASE_ISO_NAME"
    fi
    [ -f "$p" ] || die "base ISO not found at $p (set BASE_ISO=/path/to/$BASE_ISO_NAME)"
    [ -r "$p" ] || die "base ISO not readable: $p"
    printf '%s\n' "$p"
}

# real_path FILE: absolute path with every symlink resolved (bash 3.2 safe;
# macOS has no readlink -f before 12.3).
real_path() {
    local p="$1" l n=0
    while [ -L "$p" ]; do
        n=$((n + 1))
        [ "$n" -le 40 ] || die "too many symlink levels: $1"
        l="$(readlink "$p")"
        case "$l" in
            /*) p="$l" ;;
            *) p="$(dirname "$p")/$l" ;;
        esac
    done
    printf '%s/%s\n' "$(cd "$(dirname "$p")" && pwd -P)" "$(basename "$p")"
}

# file_ident FILE: "inode ctime" (ctime cannot be set back by touch or cp -p).
file_ident() {
    stat -c '%i %Z' "$1" 2>/dev/null || stat -f '%i %c' "$1"
}

file_size() {
    stat -c %s "$1" 2>/dev/null || stat -f %z "$1"
}

file_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# check_free_space DIR [GB]: abort when the filesystem holding DIR has < GB free.
check_free_space() {
    local dir="$1" need="${2:-$MIN_FREE_GB}" avail_kb
    while [ ! -d "$dir" ]; do dir="$(dirname "$dir")"; done
    avail_kb="$(df -Pk "$dir" | awk 'NR==2 {print $4}')"
    [ -n "$avail_kb" ] || die "could not read free space for $dir"
    if [ "$avail_kb" -lt $((need * 1024 * 1024)) ]; then
        die "only $((avail_kb / 1024 / 1024)) GiB free under $dir; need at least $need GiB"
    fi
    log "free space under $dir: $((avail_kb / 1024 / 1024)) GiB (need $need)"
}

# iso_cat ISO PATH: print one file from an ISO (xorriso in the builder, bsdtar on the host).
iso_cat() {
    local iso="$1" path="$2" t
    if command -v xorriso >/dev/null 2>&1; then
        t="$(mktemp)"
        xorriso -osirrox on -indev "$iso" -extract "$path" "$t" >/dev/null 2>&1 \
            || { rm -f "$t"; return 1; }
        cat "$t"
        rm -f "$t"
    elif command -v bsdtar >/dev/null 2>&1; then
        bsdtar -xOf "$iso" "${path#/}"
    else
        die "need xorriso or bsdtar to read files from the ISO"
    fi
}

# Docker Desktop (macOS) keeps host file descriptors open for files that were
# accessed through the bind mount, even after they are deleted, so their space
# is not returned. Truncating first frees the blocks; rm then drops the name.
rm_released() {
    local f
    for f in "$@"; do
        if [ -f "$f" ] && [ ! -L "$f" ]; then
            chmod u+w "$f" 2>/dev/null || true
            : > "$f"
        fi
        rm -f "$f"
    done
}

# Remove a tree that may contain read-only files/dirs (ISO extractions are r-x),
# truncating large files first (see rm_released).
force_rm_tree() {
    [ -e "$1" ] || [ -L "$1" ] || return 0
    chmod -R u+w "$1" 2>/dev/null || true
    find "$1" -type f -size +1024k -exec sh -c 'for f; do : > "$f"; done' _ {} + 2>/dev/null || true
    rm -rf "$1"
}

# check_base_boot_bytes ISO: the replay copies the ESP and the system area
# (MBR + GPT) from the base file itself, so check those bytes against the pins
# in config/base-iso.env rather than trusting the sha256 cache.
check_base_boot_bytes() {
    local iso="$1" esp_sum sa_sum
    log "checking the base ISO boot bytes against the pinned hashes"
    esp_sum="$(dd if="$iso" bs=4M iflag=skip_bytes,count_bytes skip=$((BASE_ESP_START_SECTOR * 512)) \
        count=$((BASE_ESP_SIZE_SECTORS * 512)) 2>/dev/null | sha256sum | awk '{print $1}')"
    [ "$esp_sum" = "$BASE_ESP_SHA256" ] || die "base ISO ESP sha256 is $esp_sum, expected $BASE_ESP_SHA256"
    sa_sum="$(dd if="$iso" bs=2048 count=16 2>/dev/null | sha256sum | awk '{print $1}')"
    [ "$sa_sum" = "$BASE_SYSTEM_AREA_SHA256" ] || die "base ISO system area sha256 is $sa_sum, expected $BASE_SYSTEM_AREA_SHA256"
}

# write_replayed_iso BASE PARTIAL XLOG XORRISO_TREE_ARGS...: write a new image
# to PARTIAL that replays BASE's boot setup (protective MBR, GPT, El Torito
# UEFI entry, appended ESP copied byte-for-byte), with the ISO tree changed by
# the given xorriso commands (e.g. -update_r DIR / or -map DIR /).
# -compliance reproduces NVIDIA's ISO9660/Joliet names (replay does not),
# -padding included keeps the Gap1 partition, -joliet on keeps Joliet.
write_replayed_iso() {
    local base="$1" partial="$2" xlog="$3"
    shift 3
    if ! xorriso -indev "$base" -outdev "$partial" \
            -joliet on \
            -compliance "$ISO_COMPLIANCE" \
            -boot_image any replay \
            -padding included \
            "$@" \
            -commit > "$xlog" 2>&1; then
        tail -20 "$xlog" >&2
        die "xorriso failed (log: $xlog)"
    fi
    grep -q 'Replayed [0-9]* boot related commands' "$xlog" || die "xorriso did not replay the boot setup (log: $xlog)"
}

# finalize_iso PARTIAL OUT: truncate PARTIAL to the image size (xorriso pads
# stdio output to 32 blocks; the backup GPT must sit at the very end of the
# file), check the partition table, move it to OUT and write OUT.sha256.
finalize_iso() {
    local partial="$1" out="$2" sectors vlog
    vlog="$partial.sfdisk-V"
    sectors="$(xorriso -indev "$partial" -report_system_area plain 2>/dev/null | awk '/ISO image size\/512/ {print $NF}')"
    [ -n "$sectors" ] || die "could not read the image size of $partial"
    truncate -s $((sectors * 512)) "$partial"
    sfdisk -V "$partial" > "$vlog" 2>&1 || true
    if ! grep -q 'No errors detected' "$vlog"; then
        cat "$vlog" >&2; rm_released "$vlog"
        die "partition table of $partial is not valid"
    fi
    rm_released "$vlog"
    mv "$partial" "$out"
    (cd "$(dirname "$out")" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
    log "done: $out ($(file_size "$out") bytes, sha256 $(cut -d' ' -f1 "$out.sha256"))"
}

# tree_listing DIR: "type size path[ -> target]" per node, sorted by path (GNU find).
# Directory sizes are filesystem-specific, so they are printed as 0.
tree_listing() {
    (cd "$1" && find . -mindepth 1 \
        \( -type l -printf 'l 0 %P -> %l\n' \) -o \
        \( -type d -printf 'd 0 %P\n' \) -o \
        -printf '%y %s %P\n') | LC_ALL=C sort -k3
}

# usage_from_header: print the leading comment block of the calling script.
usage_from_header() {
    sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "$0"
}
