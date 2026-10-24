#!/usr/bin/env bash
# phase2-checks.sh - the in-container halves of tests/test_phase2.sh (builder only).
#
# Usage: scripts/builder.sh run tests/phase2-checks.sh CHECK
#   grub         render-grub.py: each default target, verbatim vendor kernel
#                lines, timeout, grub-script-check, rejects a vendor file
#                missing an install entry
#   autoinstall  patch-autoinstall.py on the vendor orin file: +3 lines only,
#                rejects an already-patched file and a file without the anchor
#   md5          merge-md5sums.py with nothing staged == vendor md5sum.txt
#   contents     built ISO: staged files == manifest, md5sum.txt = vendor
#                entries + correct new ones, read-only root-owned payload,
#                release file and boot menu match the config
#   layer        built ISO's overlay layer vs NVIDIA's: only the hooked
#                autoinstall file differs (content); every path's type, mode,
#                owner, mtime, device number and xattrs equal; same
#                compressor/block/mkfs time. (Compared in Python: GNU diff -r
#                randomly reports identical 0:0 overlayfs whiteouts as different.)
#   hook         hook.sh from the built ISO against NVIDIA's real target base
#                layer (ubuntu-server-minimal.squashfs), curtin via chroot shim
#   hook-fail    hook.sh fails the install when a step fails or there is no target
# Work happens in /scratch (tmpfs); nothing is written to build/ except reads.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"
load_config
load_product_config
require_container
[ "$#" = 1 ] || die "usage: phase2-checks.sh CHECK (see --help)"
case "$1" in -h|--help) usage_from_header; exit 0 ;; esac

S="$BRAINSYNCOS_ROOT/scripts"
BASE="$(resolve_base_iso)"
NAME="$(product_iso_name)"
OUT="$OUT_DIR/$NAME.iso"
LAYER="casper/$AUTOINSTALL_LAYER"
W=/scratch/p2
force_rm_tree "$W"
mkdir -p "$W"

iso_get() {  # iso_get ISO ISO_PATH DEST
    mkdir -p "$(dirname "$3")"
    xorriso -osirrox on -indev "$1" -extract "$2" "$3" > /dev/null 2>&1 || die "cannot extract $2 from $1"
}
need_out() { [ -f "$OUT" ] || die "no built ISO at $OUT (run scripts/build-iso.sh)"; }

c_grub() {
    local v="$W/vendor.cfg" d first line n
    iso_get "$BASE" /boot/grub/grub.cfg "$v"
    for d in nvme microsd usb; do
        python3 "$S/render-grub.py" "$v" "$W/$d.cfg" --version 9.9.9 --build-id t --default "$d" --timeout -1
        grub-script-check "$W/$d.cfg"
        first="$(grep -m1 '^menuentry' "$W/$d.cfg")"
        case "$d:$first" in
            nvme:*"on NVMe SSD"*|microsd:*"on microSD card"*|usb:*"on USB drive"*) ;;
            *) die "default $d: first entry is $first" ;;
        esac
        n=0
        while IFS= read -r line; do
            n=$((n + 1))
            grep -qxF -- "$line" <(sed 's/^ *//' "$W/$d.cfg") || die "vendor kernel line missing: $line"
        done < <(grep -E '^ *linux ' "$v" | sed 's/^ *//')
        [ "$n" = 6 ] || die "expected 6 vendor kernel lines, got $n"
        [ "$(grep -cE '^ *linux ' "$W/$d.cfg")" = 6 ] || die "rendered menu does not have 6 kernel lines"
        echo "default $d: $first"
    done
    grep -qx 'set timeout=-1' "$W/nvme.cfg" || die "timeout -1 not set"
    python3 "$S/render-grub.py" "$v" "$W/t.cfg" --version 9.9.9 --build-id t --default nvme --timeout 15
    grep -qx 'set timeout=15' "$W/t.cfg" || die "timeout 15 not set"
    sed '/menuentry "Install on microSD/,/^    }/d' "$v" > "$W/broken.cfg"
    if python3 "$S/render-grub.py" "$W/broken.cfg" "$W/x.cfg" --version 9.9.9 --build-id t --default nvme --timeout -1 2> "$W/err"; then
        die "accepted a vendor grub.cfg without the microSD entry"
    fi
    echo "rejects missing entry: $(cat "$W/err")"
}

c_autoinstall() {
    local v="$W/vendor.yaml" p="$W/patched.yaml" f="${HOOKED_AUTOINSTALL%%,*}" added removed
    iso_get "$BASE" "/$LAYER" "$W/layer.squashfs"
    unsquashfs -cat "$W/layer.squashfs" "$f" > "$v"
    python3 "$S/patch-autoinstall.py" "$v" "$p"
    added="$(diff "$v" "$p" | grep -c '^>' || true)"
    removed="$(diff "$v" "$p" | grep -c '^<' || true)"
    [ "$added" = 3 ] && [ "$removed" = 0 ] || die "patch changed +$added/-$removed lines (want +3/-0)"
    diff "$v" "$p" || true
    if python3 "$S/patch-autoinstall.py" "$p" "$W/twice.yaml" 2> "$W/err"; then die "patched an already-patched file"; fi
    echo "rejects already patched: $(cat "$W/err")"
    grep -v 'Step 13/13' "$v" > "$W/noanchor.yaml"
    if python3 "$S/patch-autoinstall.py" "$W/noanchor.yaml" "$W/x.yaml" 2> "$W/err"; then die "patched a file without the anchor"; fi
    echo "rejects missing anchor: $(cat "$W/err")"
    # What subiquity gets after preseed.sh on an NVMe install: the NVMe storage
    # block uncommented, every CHANGE_* placeholder filled, offwhendone appended.
    sed -e 's/^#CHANGE_STORAGE_DO_NVME//' -e 's/NVME_DYNAMIC_APP_SIZE/123456789B/' \
        -e 's/CHANGE_[A-Z0-9_]*/true/g' "$p" > "$W/preseeded.yaml"
    printf '\n  - poweroff\n' >> "$W/preseeded.yaml"
    python3 - "$W/preseeded.yaml" <<'EOF'
import sys, yaml
ai = yaml.safe_load(open(sys.argv[1]))
late = ai["late-commands"]
assert all(isinstance(c, str) for c in late), "a late-command is not a plain string"
i = next(i for i, c in enumerate(late) if "Step 13/13" in c)
assert late[i - 1] == "/bin/bash /cdrom/brainsyncos/hook.sh", late[i - 1]
assert late[i - 2].startswith("logger -p info \"BrainSyncOS - "), late[i - 2]
assert late[-1] == "poweroff", late[-1]
assert ai["storage"]["config"][0]["type"] == "disk", "storage block not active"
print("preseeded yaml parses: %d late-commands, hook at %d-%d, before Step 13/13 and poweroff" % (len(late), i - 1, i))
EOF
}

c_md5() {
    iso_get "$BASE" /md5sum.txt "$W/md5sum.txt"
    mkdir -p "$W/empty"
    python3 "$S/merge-md5sums.py" "$W/md5sum.txt" "$W/empty" > "$W/merged.txt"
    cmp "$W/md5sum.txt" "$W/merged.txt" || die "merge with nothing staged differs from the vendor md5sum.txt"
    echo "merge with nothing staged is byte-identical to the vendor md5sum.txt ($(wc -l < "$W/merged.txt") entries)"
}

c_contents() {
    need_out
    local x="$W/iso" manifest="$OUT_DIR/$NAME.manifest.txt" p
    [ -s "$manifest" ] || die "no manifest $manifest"
    while read -r _ p; do iso_get "$OUT" "/${p#./}" "$x/${p#./}"; done < "$manifest"
    (cd "$x" && sha256sum -c --quiet "$manifest") || die "ISO files differ from the manifest"
    echo "all $(wc -l < "$manifest" | tr -d ' ') manifest files match the ISO"
    iso_get "$BASE" /md5sum.txt "$W/vendor-md5sum.txt"
    python3 - "$W/vendor-md5sum.txt" "$x/md5sum.txt" "$manifest" "$x" <<'EOF'
import hashlib, sys
def load(p):
    return {l[34:].rstrip("\n"): l[:32] for l in open(p)}
vendor, new = load(sys.argv[1]), load(sys.argv[2])
staged = {l.split()[1] for l in open(sys.argv[3])} - {"./md5sum.txt"}
errs = [k for k in vendor if k not in staged and new.get(k) != vendor[k]]
for k in staged:
    if new.get(k) != hashlib.md5(open(sys.argv[4] + "/" + k[2:], "rb").read()).hexdigest():
        errs.append(k)
extra = set(new) - set(vendor) - staged
if errs or extra:
    sys.exit("md5sum.txt wrong for %s; unexpected %s" % (errs[:5], sorted(extra)[:5]))
keys = list(new)
if keys != sorted(keys, key=lambda s: s.encode()):
    sys.exit("md5sum.txt is not in vendor (bytewise path) order")
print("md5sum.txt: %d vendor entries unchanged, %d staged entries correct" % (len(vendor) - len(staged & set(vendor)), len(staged)))
EOF
    xorriso -indev "$OUT" -find /brainsyncos -exec lsdl -- 2>/dev/null > "$W/lsdl"
    cat "$W/lsdl"
    [ "$(wc -l < "$W/lsdl" | tr -d ' ')" -ge 6 ] || die "payload listing too short"
    ! awk '{print $1}' "$W/lsdl" | grep -q w || die "payload has writable modes"
    awk '$3 != 0 || $4 != 0' "$W/lsdl" | grep -q . && die "payload not owned by 0:0"
    grep -qx "VERSION=$BRAINSYNCOS_VERSION" "$x/brainsyncos/release" || die "release VERSION wrong"
    grep -qx "BASE_ISO_SHA256=$BASE_ISO_SHA256" "$x/brainsyncos/release" || die "release BASE_ISO_SHA256 wrong"
    grep -qx "BOARD=$TARGET_BOARD" "$x/brainsyncos/release" || die "release BOARD wrong"
    grep -qx "set timeout=$GRUB_TIMEOUT" "$x/boot/grub/grub.cfg" || die "grub timeout is not GRUB_TIMEOUT=$GRUB_TIMEOUT"
    grep -m1 '^menuentry' "$x/boot/grub/grub.cfg"
    cat "$x/brainsyncos/release"
}

# meta DIR: one line per path with type+mode, owner, mtime, link target,
# device number and xattrs. hashes DIR: "sha256 path" per regular file.
meta() {
    python3 - "$1" <<'EOF'
import os, stat, sys
root = sys.argv[1]
for d, ds, fs in os.walk(root):
    ds.sort()
    for n in sorted(ds + fs):
        p = os.path.join(d, n)
        st = os.lstat(p)
        link = os.readlink(p) if stat.S_ISLNK(st.st_mode) else ""
        dev = "%d:%d" % (os.major(st.st_rdev), os.minor(st.st_rdev)) if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode) else ""
        xa = sorted((a, os.getxattr(p, a, follow_symlinks=False)) for a in os.listxattr(p, follow_symlinks=False))
        print(os.path.relpath(p, root), oct(st.st_mode), st.st_uid, st.st_gid, int(st.st_mtime), link, dev, xa)
EOF
}
hashes() {
    (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum)
}

c_layer() {
    need_out
    local f="${HOOKED_AUTOINSTALL%%,*}" d
    iso_get "$BASE" "/$LAYER" "$W/vendor.squashfs"
    iso_get "$OUT" "/$LAYER" "$W/new.squashfs"
    unsquashfs -q -no-progress -d "$W/a" "$W/vendor.squashfs" > /dev/null
    unsquashfs -q -no-progress -d "$W/b" "$W/new.squashfs" > /dev/null
    hashes "$W/a" > "$W/hash.a"
    hashes "$W/b" > "$W/hash.b"
    d="$(diff "$W/hash.a" "$W/hash.b" | sed -n 's/^[<>] [0-9a-f]*  //p' | sort -u || true)"
    [ "$d" = "./$f" ] || die "unexpected layer content differences: $d"
    echo "content: $(wc -l < "$W/hash.a" | tr -d ' ') regular files, only $f differs"
    meta "$W/a" > "$W/meta.a"
    meta "$W/b" > "$W/meta.b"
    diff "$W/meta.a" "$W/meta.b" || die "path metadata (mode/owner/mtime/device/xattrs) differs"
    echo "metadata: $(wc -l < "$W/meta.a" | tr -d ' ') paths, $(grep -c trusted.overlay "$W/meta.a") with trusted.overlay xattrs, $(grep -c ' 0:0 ' "$W/meta.a") whiteouts, all equal"
    python3 "$S/patch-autoinstall.py" "$W/a/$f" "$W/expected.yaml" > /dev/null
    cmp "$W/expected.yaml" "$W/b/$f" || die "$f in the built layer is not patch-autoinstall.py's output"
    unsquashfs -s "$W/vendor.squashfs" | grep -vE '^(Found|Filesystem size|Number of fragments)' > "$W/s.a"
    unsquashfs -s "$W/new.squashfs" | grep -vE '^(Found|Filesystem size|Number of fragments)' > "$W/s.b"
    diff "$W/s.a" "$W/s.b" || die "squashfs superblock parameters differ"
    echo "superblock: $(grep -E '^(Compression|Block size|Creation)' "$W/s.b" | tr '\n' ';')"
}

# setup_target: NVIDIA's target base layer unpacked at $W/target, and the
# payload from the built ISO at $W/iso/brainsyncos (read-only, as on /cdrom).
setup_target() {
    need_out
    iso_get "$OUT" /brainsyncos "$W/iso/brainsyncos"
    iso_get "$BASE" /casper/ubuntu-server-minimal.squashfs "$W/target.squashfs"
    unsquashfs -q -no-progress -d "$W/target" "$W/target.squashfs" > /dev/null
    rm -f "$W/target.squashfs"
    [ -f "$W/target/etc/os-release" ] || die "target layer has no /etc/os-release"
}

run_hook() {  # run_hook PAYLOAD_DIR TARGET -> hook's exit code
    PATH="$BRAINSYNCOS_ROOT/tests/shims:$PATH" TARGET_MOUNT_POINT="$2" BSOS_HOOK_LOG="$W/live.log" \
        bash "$1/hook.sh"
}

c_hook() {
    setup_target
    local t="$W/target" rel
    run_hook "$W/iso/brainsyncos" "$t"
    rel="$t/etc/brainsyncos-release"
    cat "$rel"
    grep -qx "VERSION=$BRAINSYNCOS_VERSION" "$rel" || die "VERSION missing in $rel"
    grep -qx "BASE_ISO_SHA256=$BASE_ISO_SHA256" "$rel" || die "BASE_ISO_SHA256 missing in $rel"
    grep -Eqx 'INSTALLED=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$rel" || die "INSTALLED missing in $rel"
    [ "$(stat -c %a "$rel")" = 644 ] || die "$rel mode is $(stat -c %a "$rel")"
    head -1 "$t/etc/issue" | grep -qx "BrainSyncOS $BRAINSYNCOS_VERSION (build .*)" || die "/etc/issue line 1: $(head -1 "$t/etc/issue")"
    head -2 "$t/etc/issue"
    cmp "$W/iso/brainsyncos/release" "$t/opt/brainsyncos/installer/release" || die "payload not installed"
    ! find "$t/opt/brainsyncos" -perm -o+w | grep -q . || die "installed payload is world-writable"
    tail -1 "$t/var/log/brainsyncos/install.log" | grep -qx 'hook.sh: exit 0' || die "target log incomplete: $(tail -1 "$t/var/log/brainsyncos/install.log")"
    grep -q 'BrainSyncOS layer installed (2 steps)' "$t/var/log/brainsyncos/install.log" || die "log lacks the summary"
    # Running the hook again (a re-run late-command) must give the same result.
    run_hook "$W/iso/brainsyncos" "$t" > /dev/null
    [ "$(grep -c '^BrainSyncOS ' "$t/etc/issue")" = 1 ] || die "/etc/issue gained a second BrainSyncOS line on re-run"
    echo "hook: release, issue, payload and log installed; re-run is idempotent"
}

c_hook_fail() {
    setup_target
    local rc bad="$W/bad"
    cp -R "$W/iso/brainsyncos" "$bad"
    chmod -R u+w "$bad"
    printf '#!/bin/bash\necho failing on purpose; exit 3\n' > "$bad/install.d/50-fail.sh"
    set +e; run_hook "$bad" "$W/target" > "$W/fail.out" 2>&1; rc=$?; set -e
    [ "$rc" != 0 ] || die "hook succeeded although 50-fail.sh failed"
    grep -q 'failing on purpose' "$W/target/var/log/brainsyncos/install.log" || die "failure not logged on the target"
    tail -1 "$W/target/var/log/brainsyncos/install.log" | grep -qx 'hook.sh: exit [1-9][0-9]*' || die "target log lacks the exit line"
    echo "failing step: hook rc=$rc, logged on the target"
    mkdir -p "$W/empty-target"
    set +e; run_hook "$W/iso/brainsyncos" "$W/empty-target" > "$W/notarget.out" 2>&1; rc=$?; set -e
    [ "$rc" != 0 ] || die "hook succeeded without an installed system"
    grep 'no installed system' "$W/notarget.out"
}

case "$1" in
    grub) c_grub ;;
    autoinstall) c_autoinstall ;;
    md5) c_md5 ;;
    contents) c_contents ;;
    layer) c_layer ;;
    hook) c_hook ;;
    hook-fail) c_hook_fail ;;
    *) die "unknown check: $1 (see --help)" ;;
esac
force_rm_tree "$W"
