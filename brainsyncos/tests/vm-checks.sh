#!/usr/bin/env bash
# vm-checks.sh - the in-container halves of tests/test_vm.sh (builder only).
#
# Usage: scripts/builder.sh run tests/vm-checks.sh CHECK [ISO_NAME]
#   (ISO_NAME: build/out/ISO_NAME.iso, default the VM edition's product name)
#   grub         render-vm-grub.py on Ubuntu's real grub.cfg: install entry
#                first (GA or HWE), autoinstall + subiquity.autoinstallpath
#                before `---`, the configured consoles after it, every vendor
#                line kept, stock entries untouched, timeout, grub-script-check;
#                rejects a vendor file missing an entry or already autoinstalling
#   autoinstall  vm/autoinstall.yaml against the autoinstall JSON schema that
#                the ISO's own subiquity snap generates (subiquity.cmd.schema)
#                plus check-autoinstall.py's rules; broken variants rejected
#   md5          merge-md5sums.py on Ubuntu's (unsorted) md5sum.txt: identity
#                with nothing staged; a replaced file keeps its line, new ones
#                follow sorted
#   contents     built ISO: staged files == manifest, md5sum.txt = vendor
#                entries (vendor order) + correct new ones, read-only root-owned
#                payload, release (EDITION=vm-arm64), the autoinstall and boot
#                menu, nothing at /autoinstall.yaml, and every casper layer
#                still matching Ubuntu's signed casper/SHA256SUMS
#   hook         hook.sh from the built ISO against Ubuntu's real target layers
#                (ubuntu-server-minimal + ubuntu-server, merged as curtin's
#                overlay copy does, openssh-server dpkg-installed from the ISO
#                pool as `ssh: install-server` does), curtin via the chroot
#                shim, no device tree, a fake DMI: release, issue, hostname
#                from the DMI serial, account, sshd config (sshd -t), default
#                target untouched, no Jetson warning; then the product_uuid and
#                machine-id fallbacks; the autoinstall's user-data passes the
#                target's own `cloud-init schema`; the hook mounts the ISO's
#                package repository and 60-apt-repo.sh installs its keyring;
#                75-usb-serial.sh installs the board's udev rules but not the
#                Jetson-only ch341 module; 70-brainsync.sh the BrainSync apps
#                (brainsync-voice runs); 85-llama.sh brainsyncos-llama without
#                the CUDA backend, the service enabled, its key in secrets.env
# Work happens in /scratch (tmpfs); nothing is written to build/.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"
load_vm_config
load_vm_product_config
load_packages_config
require_container
[ "$#" = 1 ] || [ "$#" = 2 ] || die "usage: vm-checks.sh CHECK [ISO_NAME] (see --help)"
case "$1" in -h|--help) usage_from_header; exit 0 ;; esac

S="$BRAINSYNCOS_ROOT/scripts"
BASE="$(resolve_vm_base_iso)"
NAME="${2:-$(vm_iso_name)}"
OUT="$OUT_DIR/$NAME.iso"
AI_PATH=/cdrom/brainsyncos/autoinstall.yaml
W=/scratch/vm
force_rm_tree "$W"
mkdir -p "$W"

iso_get() {  # iso_get ISO ISO_PATH DEST
    mkdir -p "$(dirname "$3")"
    xorriso -osirrox on -indev "$1" -extract "$2" "$3" > /dev/null 2>&1 || die "cannot extract $2 from $1"
}
need_out() { [ -f "$OUT" ] || die "no built ISO at $OUT (run scripts/build-vm-iso.sh)"; }

render() {  # render VENDOR OUT KERNEL TIMEOUT
    python3 "$S/render-vm-grub.py" "$1" "$2" --version 9.9.9 --build-id t --timeout "$4" \
        --kernel "$3" --consoles "$VM_CONSOLES" --autoinstall "$AI_PATH"
}

c_grub() {
    local v="$W/vendor.cfg" k first kline line n want_post
    iso_get "$BASE" /boot/grub/grub.cfg "$v"
    want_post="console=$(printf '%s' "$VM_CONSOLES" | sed 's/,/ console=/g')"
    for k in ga hwe; do
        render "$v" "$W/$k.cfg" "$k" -1
        grub-script-check "$W/$k.cfg"
        first="$(grep -m1 '^menuentry' "$W/$k.cfg")"
        kline="$(grep -m1 -E '^[[:space:]]*linux[[:space:]]' "$W/$k.cfg" | sed 's/^[[:space:]]*//')"
        case "$k:$first" in
            ga:'menuentry "Install BrainSyncOS 9.9.9 (VM, erases the virtual disk)" {') ;;
            hwe:'menuentry "Install BrainSyncOS 9.9.9 with the HWE kernel (VM, erases the virtual disk)" {') ;;
            *) die "kernel $k: first entry is $first" ;;
        esac
        case "$k:$kline" in
            ga:*/casper/vmlinuz\ *|hwe:*/casper/hwe-vmlinuz\ *) ;;
            *) die "kernel $k: first kernel line is $kline" ;;
        esac
        printf '%s\n' "$kline" | grep -q " autoinstall subiquity.autoinstallpath=$AI_PATH --- " \
            || die "kernel $k: autoinstall arguments missing or after ---: $kline"
        [ "${kline##* --- }" = "$want_post" ] || die "kernel $k: after --- is '${kline##* --- }', want '$want_post'"
        [ "$(grep -c "subiquity.autoinstallpath=$AI_PATH" "$W/$k.cfg")" = 2 ] || die "expected 2 BrainSyncOS install entries"
        # Every non-blank vendor line (menu entries, stock kernel lines) is still there.
        n=0
        while IFS= read -r line; do
            line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//')"
            case "$line" in ''|'set timeout='*) continue ;; esac
            n=$((n + 1))
            sed 's/^[[:space:]]*//' "$W/$k.cfg" | grep -qxF -- "$line" || die "vendor line missing: $line"
        done < "$v"
        [ "$(grep -cE '^[[:space:]]*linux[[:space:]]' "$W/$k.cfg")" = 4 ] || die "expected 4 kernel lines (2 ours, 2 stock)"
        grep -E '^[[:space:]]*linux[[:space:]]' "$W/$k.cfg" | grep -v autoinstall | grep -c -- '--- console=tty0$' | grep -qx 2 \
            || die "the stock entries' kernel lines changed"
        echo "kernel $k: $first"
        echo "    $kline"
    done
    echo "all $n non-blank vendor lines kept; stock entries unchanged"
    grep -qx 'set timeout=-1' "$W/ga.cfg" || die "timeout -1 not set"
    render "$v" "$W/t.cfg" ga 15
    grep -qx 'set timeout=15' "$W/t.cfg" || die "timeout 15 not set"
    sed '/menuentry "Ubuntu Server with the HWE kernel"/,/^}/d' "$v" > "$W/broken.cfg"
    if render "$W/broken.cfg" "$W/x.cfg" ga -1 2> "$W/err"; then die "accepted a vendor grub.cfg without the HWE entry"; fi
    echo "rejects missing entry: $(cat "$W/err")"
    sed 's|/casper/vmlinuz  ---|/casper/vmlinuz autoinstall ---|' "$v" > "$W/auto.cfg"
    if render "$W/auto.cfg" "$W/x.cfg" ga -1 2> "$W/err"; then die "accepted a vendor grub.cfg that already autoinstalls"; fi
    echo "rejects autoinstalling vendor entry: $(cat "$W/err")"
}

# subiquity_schema OUT: the autoinstall JSON schema of the ISO's own subiquity
# (subiquity.cmd.schema from the seeded snap, run with the builder's python;
# systemd.journal, a C extension for the snap's python 3.10, is stubbed).
subiquity_schema() {
    local layer=casper/ubuntu-server-minimal.ubuntu-server.installer.squashfs snap snapdir="$W/subiquity-snap"
    iso_get "$BASE" "/$layer" "$W/installer.squashfs"
    snap="$(unsquashfs -l "$W/installer.squashfs" | sed -n 's|^squashfs-root/\(var/lib/snapd/seed/snaps/subiquity_[0-9]*\.snap\)$|\1|p' | head -1)"
    [ -n "$snap" ] || die "no subiquity snap seeded in $layer"
    unsquashfs -cat "$W/installer.squashfs" "$snap" > "$W/subiquity.snap"
    rm -f "$W/installer.squashfs"
    unsquashfs -q -no-progress -d "$snapdir" "$W/subiquity.snap" > /dev/null
    rm -f "$W/subiquity.snap"
    mkdir -p "$W/stub/systemd"
    : > "$W/stub/systemd/__init__.py"
    printf 'LOG_INFO = 6\ndef send(*a, **k): pass\ndef sendv(*a, **k): pass\ndef stream_fd(*a, **k): return 2\nclass Reader:\n    def __init__(self, *a, **k): pass\n' \
        > "$W/stub/systemd/journal.py"
    (cd "$snapdir" && PYTHONPATH="$W/stub:$snapdir/lib/python3.10/site-packages:$snapdir/usr/lib/python3/dist-packages" \
        timeout 120 python3 -W ignore -m subiquity.cmd.schema) > "$1" 2> "$W/schema.err" \
        || { tail -5 "$W/schema.err" >&2; die "subiquity.cmd.schema failed"; }
    python3 -c 'import json, sys; s = json.load(open(sys.argv[1])); assert "late-commands" in s["properties"]' "$1" \
        || die "generated schema has no late-commands"
    echo "schema: $(basename "$snap"), $(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["properties"]))' "$1") top-level keys"
    force_rm_tree "$snapdir"
}

c_autoinstall() {
    local schema="$W/schema.json" t="$BRAINSYNCOS_ROOT/vm/autoinstall.yaml" spec name msg
    subiquity_schema "$schema"
    python3 "$S/check-autoinstall.py" "$t" --schema "$schema"
    # Every VM_DESKTOP rendering is a valid autoinstall with its package first.
    for spec in none: minimal:ubuntu-desktop-minimal full:ubuntu-desktop; do
        python3 "$S/render-vm-autoinstall.py" "$t" "$W/desktop.yaml" --desktop "${spec%%:*}"
        python3 "$S/check-autoinstall.py" "$W/desktop.yaml" --schema "$schema" > /dev/null
        python3 -c 'import sys, yaml; p = yaml.safe_load(open(sys.argv[1]))["autoinstall"]["packages"]; sys.exit(0 if (p[0] == sys.argv[2] if sys.argv[2] else "ubuntu-desktop" not in " ".join(p)) else 1)' \
            "$W/desktop.yaml" "${spec#*:}" || die "VM_DESKTOP=${spec%%:*}: wrong packages"
    done
    python3 "$S/render-vm-autoinstall.py" "$t" "$W/desktop.yaml" --desktop none > /dev/null
    cmp -s "$t" "$W/desktop.yaml" || die "VM_DESKTOP=none changed the template"
    echo "VM_DESKTOP none/minimal/full: valid autoinstalls, desktop package first; none = the template"
    sed 's/^  packages:$/  packages: []/' "$t" > "$W/nopkgs.yaml"
    if python3 "$S/render-vm-autoinstall.py" "$W/nopkgs.yaml" "$W/x.yaml" --desktop minimal 2> "$W/err"; then die "rendered a template without a packages list"; fi
    echo "render-vm-autoinstall rejects a template without '  packages:': $(cat "$W/err")"
    # mutate NAME PYTHON: a copy of the template changed by PYTHON (ai = the autoinstall dict).
    mutate() {
        python3 - "$t" "$W/$1.yaml" "$2" <<'EOF'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
ai = doc["autoinstall"]
exec(sys.argv[3])
yaml.safe_dump(doc, open(sys.argv[2], "w"), sort_keys=False)
EOF
    }
    while IFS='|' read -r name spec msg; do
        [ -n "$name" ] || continue
        mutate "$name" "$spec"
        if python3 "$S/check-autoinstall.py" "$W/$name.yaml" --schema "$schema" 2> "$W/err"; then
            die "accepted the $name variant"
        fi
        grep -qF -- "$msg" "$W/err" || die "$name variant rejected for the wrong reason: $(cat "$W/err")"
        echo "rejects $name: $(head -1 "$W/err" | sed 's/^[^:]*: [^:]*: //')"
    done <<'EOF'
identity|ai["identity"] = {"username": "u", "hostname": "h", "password": "x"}|identity must not be set
interactive|ai["interactive-sections"] = ["storage"]|interactive-sections must not be set
no-hook|ai["late-commands"] = ai["late-commands"][1:]|the first late-command must be
hook-twice|ai["late-commands"].append(ai["late-commands"][0])|exactly once
mapping-command|ai["late-commands"].append({"echo a": "b"})|plain strings
default-user|ai["user-data"] = {"preserve_hostname": True}|users: []
password-ssh|ai["ssh"]["allow-pw"] = True|allow-pw: false
no-avahi|ai["packages"].remove("avahi-daemon")|packages lacks avahi-daemon
bad-shutdown|ai["shutdown"] = "halt"|not in ['reboot', 'poweroff']
typo-key|ai["late-command"] = ai.pop("late-commands")|unknown to this subiquity
bad-keyboard|ai["keyboard"] = {"layout": "us", "variants": "intl"}|unknown key 'variants'
extra-top-level|doc["users"] = []|exactly one top-level key
duid-dhcp|ai["network"]["ethernets"]["nics"].pop("dhcp-identifier")|dhcp-identifier: mac
no-network|ai.pop("network")|network needs ethernets
no-ok-marker|ai["late-commands"].pop()|the last late-command must print BRAINSYNCOS-INSTALL-OK
literal-marker|ai["late-commands"][-1] = "echo BRAINSYNCOS-INSTALL-OK > /dev/hvc0 /dev/ttyAMA0 m=INSTALL-OK; echo BRAINSYNCOS-$m"|the last late-command must print
no-error-marker|ai.pop("error-commands")|error-commands must print BRAINSYNCOS-INSTALL-FAILED
EOF
}

c_md5() {
    iso_get "$BASE" /md5sum.txt "$W/md5sum.txt"
    mkdir -p "$W/empty" "$W/stage/boot/grub" "$W/stage/brainsyncos"
    python3 "$S/merge-md5sums.py" "$W/md5sum.txt" "$W/empty" > "$W/merged.txt"
    cmp "$W/md5sum.txt" "$W/merged.txt" || die "merge with nothing staged differs from Ubuntu's md5sum.txt"
    echo "merge with nothing staged is byte-identical to Ubuntu's md5sum.txt ($(wc -l < "$W/merged.txt" | tr -d ' ') entries, not bytewise sorted: $(awk '{print $2}' "$W/md5sum.txt" | LC_ALL=C sort -c 2>&1 | head -1 | cut -c1-60))"
    echo new > "$W/stage/boot/grub/grub.cfg"
    echo b > "$W/stage/brainsyncos/b"
    echo a > "$W/stage/brainsyncos/a"
    python3 "$S/merge-md5sums.py" "$W/md5sum.txt" "$W/stage" > "$W/merged2.txt"
    [ "$(awk '{print $2}' "$W/merged2.txt" | head -n "$(wc -l < "$W/md5sum.txt")")" = "$(awk '{print $2}' "$W/md5sum.txt")" ] \
        || die "vendor order not kept"
    [ "$(tail -2 "$W/merged2.txt" | awk '{print $2}' | tr '\n' ' ')" = "./brainsyncos/a ./brainsyncos/b " ] || die "new entries not appended sorted"
    grep -qx "$(md5sum "$W/stage/boot/grub/grub.cfg" | awk '{print $1}')  ./boot/grub/grub.cfg" "$W/merged2.txt" || die "replaced grub.cfg md5 wrong"
    [ "$(diff "$W/md5sum.txt" "$W/merged2.txt" | grep -c '^[<>]' || true)" = 4 ] \
        || die "unexpected changes: $(diff "$W/md5sum.txt" "$W/merged2.txt" || true)"
    echo "staged: replaced line updated in place, 2 new entries appended sorted"
}

c_contents() {
    need_out
    local x="$W/iso" manifest="$OUT_DIR/$NAME.manifest.txt" p f want got n=0
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
if list(new)[:len(vendor)] != list(vendor):
    sys.exit("md5sum.txt does not keep Ubuntu's order")
print("md5sum.txt: %d vendor entries unchanged in Ubuntu's order, %d staged entries correct" % (len(vendor) - len(staged & set(vendor)), len(staged)))
EOF
    xorriso -indev "$OUT" -find /brainsyncos -exec lsdl -- 2>/dev/null > "$W/lsdl"
    xorriso -indev "$OUT" -find "$ISO_REPO_PATH" -exec lsdl -- 2>/dev/null >> "$W/lsdl"
    cat "$W/lsdl"
    [ "$(wc -l < "$W/lsdl" | tr -d ' ')" -ge 8 ] || die "payload listing too short"
    grep -q "'$ISO_REPO_PATH/dists/$REPO_SUITE/InRelease'\$" "$W/lsdl" || die "no package repository on the ISO"
    ! awk '{print $1}' "$W/lsdl" | grep -q w || die "payload has writable modes"
    awk '$3 != 0 || $4 != 0' "$W/lsdl" | grep -q . && die "payload not owned by 0:0"
    for p in "EDITION=$EDITION" "VERSION=$BRAINSYNCOS_VERSION" "UBUNTU_RELEASE=$UBUNTU_RELEASE" "BASE_ISO_SHA256=$BASE_ISO_SHA256"; do
        grep -qx "$p" "$x/brainsyncos/release" || die "release lacks $p"
    done
    cmp "$x/brainsyncos/repo-key.gpg" "$x${ISO_REPO_PATH}/repo-key.gpg" || die "payload repo-key.gpg differs from the repository's"
    grep -qx "REPO_KEY=$(key_fingerprint "$x/brainsyncos/repo-key.gpg")" "$x/brainsyncos/release" || die "release REPO_KEY wrong"
    gpgv --keyring "$x/brainsyncos/repo-key.gpg" "$x${ISO_REPO_PATH}/dists/$REPO_SUITE/InRelease" > /dev/null 2>&1 \
        || die "the payload key does not verify the ISO's repository"
    [ "$(cat "$x${ISO_REPO_PATH}/edition")" = vm ] || die "the ISO's repository is not the vm one"
    echo "repository: $ISO_REPO_PATH (vm), verified by the payload key named in the release file"
    python3 "$S/render-vm-autoinstall.py" "$BRAINSYNCOS_ROOT/vm/autoinstall.yaml" "$W/ai-want.yaml" --desktop "$VM_DESKTOP" > /dev/null
    cmp "$x/brainsyncos/autoinstall.yaml" "$W/ai-want.yaml" || die "ISO autoinstall is not vm/autoinstall.yaml rendered for VM_DESKTOP=$VM_DESKTOP"
    echo "autoinstall: vm/autoinstall.yaml with VM_DESKTOP=$VM_DESKTOP: $(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["autoinstall"]["packages"])' "$x/brainsyncos/autoinstall.yaml")"
    python3 "$S/check-autoinstall.py" "$x/brainsyncos/autoinstall.yaml"
    grep -qx "set timeout=$VM_GRUB_TIMEOUT" "$x/boot/grub/grub.cfg" || die "grub timeout is not VM_GRUB_TIMEOUT=$VM_GRUB_TIMEOUT"
    grep -m1 '^menuentry' "$x/boot/grub/grub.cfg" | grep -q "Install BrainSyncOS $BRAINSYNCOS_VERSION" || die "first menu entry is not the install"
    grep -q "subiquity.autoinstallpath=$AI_PATH" "$x/boot/grub/grub.cfg" || die "boot menu does not point at $AI_PATH"
    grep -m1 '^menuentry' "$x/boot/grub/grub.cfg"
    # Stock entries must stay stock: nothing where subiquity looks by default.
    ! xorriso -indev "$OUT" -find / -maxdepth 1 -name autoinstall.yaml 2>/dev/null | grep -q . || die "/autoinstall.yaml on the ISO root"
    cat "$x/brainsyncos/release"
    # The casper layers are Ubuntu's: their signed checksum list still holds.
    iso_get "$OUT" /casper/SHA256SUMS "$W/casper/SHA256SUMS"
    iso_get "$OUT" /casper/SHA256SUMS.gpg "$W/casper/SHA256SUMS.gpg"
    gpgv --keyring /usr/share/keyrings/ubuntu-archive-keyring.gpg "$W/casper/SHA256SUMS.gpg" "$W/casper/SHA256SUMS" 2>&1 \
        | grep 'Good signature' || die "casper/SHA256SUMS signature does not verify"
    while read -r want f; do
        f="${f#\*}"
        iso_get "$OUT" "/casper/$f" "$W/casper/f"
        got="$(sha256sum "$W/casper/f" | awk '{print $1}')"
        rm -f "$W/casper/f"
        [ "$got" = "$want" ] || die "casper/$f: sha256 $got, signed list says $want"
        n=$((n + 1))
    done < "$W/casper/SHA256SUMS"
    echo "casper: all $n files listed in the signed casper/SHA256SUMS match"
}

# merge_layer UPPER LOWER: apply an overlayfs upper layer (as unsquashed:
# whiteouts are 0:0 char devices, opaque dirs carry trusted.overlay.opaque=y)
# onto the tree LOWER, like curtin's copy of the mounted overlay: whiteouts
# delete, opaque dirs replace, everything else is moved over; trusted.overlay.*
# xattrs are dropped. Moves within the tmpfs, so no extra space is needed.
merge_layer() {
    python3 - "$1" "$2" <<'EOF'
import os, shutil, stat, sys
upper, lower = sys.argv[1], sys.argv[2]
counts = {"whiteout": 0, "opaque": 0, "moved": 0}
def ovl_xattrs(p):
    return [a for a in os.listxattr(p, follow_symlinks=False) if a.startswith("trusted.overlay.")]
def remove(p):
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    elif os.path.lexists(p):
        os.unlink(p)
def merge(u, l):
    for name in sorted(os.listdir(u)):
        up, lp = os.path.join(u, name), os.path.join(l, name)
        st = os.lstat(up)
        if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
            remove(lp)
            counts["whiteout"] += 1
            continue
        if stat.S_ISDIR(st.st_mode):
            opaque = "trusted.overlay.opaque" in ovl_xattrs(up) and os.getxattr(up, "trusted.overlay.opaque") == b"y"
            if opaque:
                remove(lp)
                counts["opaque"] += 1
            for a in ovl_xattrs(up):
                os.removexattr(up, a, follow_symlinks=False)
            if os.path.isdir(lp) and not os.path.islink(lp):
                merge(up, lp)
                os.chmod(lp, stat.S_IMODE(st.st_mode))
                os.chown(lp, st.st_uid, st.st_gid)
                os.utime(lp, ns=(st.st_atime_ns, st.st_mtime_ns))
                continue
        remove(lp)
        os.rename(up, lp)
        counts["moved"] += 1
merge(upper, lower)
print("merged layer: %(moved)d entries moved, %(whiteout)d whiteouts, %(opaque)d opaque dirs" % counts)
EOF
}

# setup_target: Ubuntu's target layers merged at $W/target, with
# openssh-server installed from the ISO pool the way subiquity installs it
# for `ssh: install-server` (dpkg in the target, services not started:
# curtin's policy-rc.d), the payload from the built ISO at $W/iso/brainsyncos
# (read-only, as on /cdrom), an empty device tree dir (ACPI arm64: no model)
# and a fake DMI at $W/dmi.
setup_target() {
    need_out
    local t="$W/target" l deb
    iso_get "$OUT" /brainsyncos "$W/iso/brainsyncos"
    iso_get "$OUT" "$ISO_REPO_PATH" "$W/iso${ISO_REPO_PATH}"
    iso_get "$BASE" /casper/ubuntu-server-minimal.squashfs "$W/l1.squashfs"
    unsquashfs -q -no-progress -d "$t" "$W/l1.squashfs" > /dev/null
    rm -f "$W/l1.squashfs"
    iso_get "$BASE" /casper/ubuntu-server-minimal.ubuntu-server.squashfs "$W/l2.squashfs"
    unsquashfs -q -no-progress -d "$W/upper" "$W/l2.squashfs" > /dev/null
    rm -f "$W/l2.squashfs"
    merge_layer "$W/upper" "$t"
    force_rm_tree "$W/upper"
    ! getfattr -R -d -m trusted.overlay "$t" 2>/dev/null | grep -q . || die "overlay xattrs left in the target"
    [ -f "$t/etc/os-release" ] && grep -q "VERSION_ID=\"${UBUNTU_RELEASE%.*}\"" "$t/etc/os-release" || die "target is not Ubuntu ${UBUNTU_RELEASE%.*}"
    [ -x "$t/usr/bin/cloud-init" ] || die "merged target lacks the ubuntu-server layer (cloud-init)"
    [ ! -e "$t/usr/sbin/sshd" ] || die "sshd already in the layers; the pool install is not needed"
    mkdir -p "$t/tmp/debs"
    for deb in openssh/openssh-server_1%3a9.6p1-3ubuntu13.19 openssh/openssh-sftp-server_1%3a9.6p1-3ubuntu13.19 \
               tcp-wrappers/libwrap0_7.6.q-33; do
        l="${deb#*/}"
        case "$deb" in tcp-wrappers/*) iso_get "$BASE" "/pool/main/t/$deb"_arm64.deb "$t/tmp/debs/${l}_arm64.deb" ;;
                       *) iso_get "$BASE" "/pool/main/o/$deb"_arm64.deb "$t/tmp/debs/${l}_arm64.deb" ;; esac
    done
    printf '#!/bin/sh\nexit 101\n' > "$t/usr/sbin/policy-rc.d"
    chmod 755 "$t/usr/sbin/policy-rc.d"
    in_target "$t" env DEBIAN_FRONTEND=noninteractive dpkg -i /tmp/debs/libwrap0_7.6.q-33_arm64.deb \
        /tmp/debs/openssh-sftp-server_1%3a9.6p1-3ubuntu13.19_arm64.deb /tmp/debs/openssh-server_1%3a9.6p1-3ubuntu13.19_arm64.deb \
        > "$W/dpkg.log" 2>&1 || { tail -20 "$W/dpkg.log" >&2; die "dpkg -i openssh-server failed in the target"; }
    rm -rf "$t/tmp/debs" "$t/usr/sbin/policy-rc.d"
    [ -x "$t/usr/sbin/sshd" ] && ls "$t"/etc/ssh/ssh_host_*_key > /dev/null || die "openssh-server postinst did not run"
    echo "target: Ubuntu layers merged, openssh-server $(in_target "$t" dpkg-query -W -f '${Version}' openssh-server) installed from the pool"
    mkdir -p "$W/dt" "$W/dmi"
    printf 'QEMU\n' > "$W/dmi/sys_vendor"
    printf 'QEMU Virtual Machine\n' > "$W/dmi/product_name"
    printf 'UTM-0042AB12CD\n' > "$W/dmi/product_serial"
    printf 'b71c2a7d-0000-4000-8000-0000005e17ab\n' > "$W/dmi/product_uuid"
    printf '0123456789abcdef0123456789fedcba\n' > "$W/machine-id"
}

in_target() {  # in_target TARGET CMD...: run CMD in TARGET via the curtin shim
    local t="$1"; shift
    "$BRAINSYNCOS_ROOT/tests/shims/curtin" in-target --target="$t" -- "$@"
}

run_hook() {  # run_hook PAYLOAD_DIR TARGET [DMI_DIR] -> hook's exit code
    PATH="$BRAINSYNCOS_ROOT/tests/shims:$PATH" TARGET_MOUNT_POINT="$2" BSOS_HOOK_LOG="$W/live.log" \
        BSOS_DT_DIR="$W/dt" BSOS_DMI_DIR="${3:-$W/dmi}" BSOS_MACHINE_ID_FILE="$W/machine-id" \
        BSOS_REPO_SRC="$W/iso${ISO_REPO_PATH}" bash "$1/hook.sh"
}

# check_host TARGET SUFFIX SOURCE: hostname, /etc/hosts and device.env.
check_host() {
    local t="$1" host="$HOSTNAME_PREFIX-$2"
    [ "$(cat "$t/etc/hostname")" = "$host" ] || die "hostname is $(cat "$t/etc/hostname"), want $host"
    [ "$(grep -c '^127\.0\.1\.1[[:space:]]' "$t/etc/hosts")" = 1 ] || die "/etc/hosts needs exactly one 127.0.1.1 line"
    grep -qxP "127\.0\.1\.1\t$host" "$t/etc/hosts" || die "/etc/hosts lacks 127.0.1.1 $host"
    local env="$t/opt/brainsyncos/installer/device.env" got
    # shellcheck disable=SC1090
    got="$(. "$env"; printf '%s|%s|%s' "$BSOS_EDITION" "$BSOS_ID_SOURCE" "$BSOS_MODEL")"
    case "$3:$got" in
        "machine-id:vm-arm64|machine-id|unknown") ;;
        "dmi "*":vm-arm64|$3|QEMU QEMU Virtual Machine") ;;
        *) die "device.env for $3: $got ($(tr '\n' ' ' < "$env"))" ;;
    esac
    echo "hostname: $host (serial from $3; device.env $got)"
}

c_hook() {
    setup_target
    local t="$W/target" rel u="$BRAINSYNC_USER" groups g shadow home log="$W/target/var/log/brainsyncos/install.log" before
    before="$(readlink "$t/etc/systemd/system/default.target" 2>/dev/null || echo none)"
    run_hook "$W/iso/brainsyncos" "$t"
    rel="$t/etc/brainsyncos-release"
    cat "$rel"
    for p in "EDITION=vm-arm64" "VERSION=$BRAINSYNCOS_VERSION" "BASE_ISO_SHA256=$BASE_ISO_SHA256"; do
        grep -qx "$p" "$rel" || die "$rel lacks $p"
    done
    grep -Eqx 'INSTALLED=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$rel" || die "INSTALLED missing in $rel"
    head -1 "$t/etc/issue" | grep -qx "BrainSyncOS $BRAINSYNCOS_VERSION VM edition (build .*), IP \\\\4" \
        || die "/etc/issue line 1: $(head -1 "$t/etc/issue")"
    head -2 "$t/etc/issue"
    tail -1 "$log" | grep -qx 'hook.sh: exit 0' || die "target log incomplete: $(tail -1 "$log")"
    grep -q 'BrainSyncOS layer installed (10 steps)' "$log" || die "log lacks the summary"
    grep -q '60-apt-repo.sh: repository verified; brainsyncos-archive-keyring ' "$log" || die "60-apt-repo.sh did not finish"
    cmp "$t/usr/share/keyrings/brainsyncos-archive-keyring.gpg" "$W/iso/brainsyncos/repo-key.gpg" \
        || die "the installed keyring is not the build's repository key"
    ! mountpoint -q "$t/tmp/brainsyncos-repo" && [ ! -e "$t/tmp/brainsyncos-repo" ] || die "the repository is still mounted"
    grep -q '70-brainsync.sh: brainsync .*, brainsync-runtime .* installed' "$log" || die "70-brainsync.sh did not finish"
    [ "$(in_target "$t" dpkg-query -W -f '${Status}\n' brainsync brainsync-runtime | grep -cx 'install ok installed')" = 2 ] \
        || die "brainsync and brainsync-runtime are not installed"
    in_target "$t" /usr/bin/brainsync-voice --help | grep -q 'usage:' || die "brainsync-voice does not run in the target"
    echo "packages: brainsyncos-archive-keyring, brainsync-runtime and brainsync installed from the ISO's repository; repository unmounted"
    echo "packages: brainsyncos-archive-keyring installed from the ISO's repository; repository unmounted"
    grep -q '75-usb-serial.sh: vm-arm64 edition: no brainsyncos-ch341-tegra' "$log" || die "75-usb-serial.sh did not skip the module"
    [ -s "$t/usr/lib/udev/rules.d/70-brainsync-cerelog.rules" ] || die "the board's udev rules are missing"
    ! in_target "$t" dpkg -s brainsyncos-ch341-tegra > /dev/null 2>&1 || die "the VM got the Jetson-only ch341 module"
    echo "board: brainsync-udev installed, no ch341 module (Ubuntu's kernel has it)"
    grep -q '85-llama.sh: vm-arm64 edition: CPU backends only' "$log" && grep -q '85-llama.sh: installed brainsyncos-llama [^,]*; ' "$log" \
        || die "85-llama.sh did not install brainsyncos-llama alone"
    ! in_target "$t" dpkg -s brainsyncos-llama-cuda > /dev/null 2>&1 || die "the VM got the Jetson-only CUDA backend"
    [ -L "$t/etc/systemd/system/multi-user.target.wants/brainsyncos-llama.service" ] || die "brainsyncos-llama.service is not enabled"
    grep -qx "BRAINSYNC_LLAMA_API_KEY=$(cat "$t/etc/brainsyncos/llama/api-key")" "$t/etc/brainsync/secrets.env" \
        || die "secrets.env lacks the llama-server key"
    echo "llm: brainsyncos-llama installed without the CUDA backend, service enabled, key in secrets.env"
    grep -q 'hook.sh: vm-arm64 edition on: QEMU QEMU Virtual Machine, serial UTM0042AB12CD (from dmi product_serial)' "$log" \
        || die "machine identity not logged: $(grep 'edition on' "$log")"
    ! grep -n 'WARNING\|No such file' "$log" || die "the VM install logged a warning"
    check_host "$t" ab12cd "dmi product_serial"
    grep -qx 'preserve_hostname: true' "$t/etc/cloud/cloud.cfg.d/99-brainsyncos.cfg" || die "cloud-init may reset the hostname"

    in_target "$t" id "$u" > /dev/null || die "user $u missing"
    groups="$(in_target "$t" id -nG "$u")"
    for g in sudo dialout adm audio video plugdev; do
        printf ' %s ' "$groups" | grep -q " $g " || die "$u is not in group $g ($groups)"
    done
    ! in_target "$t" id ubuntu > /dev/null 2>&1 || die "an ubuntu user exists"
    shadow="$(grep "^$u:" "$t/etc/shadow" | cut -d: -f2)"
    home="$(grep "^$u:" "$t/etc/passwd" | cut -d: -f6)"
    [ "$shadow" = '!' ] || die "password of $u is not locked: $shadow"
    in_target "$t" visudo -cf /etc/sudoers.d/90-brainsyncos
    cmp "$W/iso/brainsyncos/config/authorized_keys" "$t$home/.ssh/authorized_keys" || die "authorized_keys not installed"
    [ "$(in_target "$t" stat -c '%a %U' "$home/.ssh/authorized_keys")" = "600 $u" ] || die "authorized_keys mode/owner"
    echo "account: $u ($groups), SSH key only, passwordless sudo"

    grep -qx "AllowUsers $u" "$t/etc/ssh/sshd_config.d/10-brainsyncos.conf" || die "sshd drop-in missing"
    [ -L "$t/etc/systemd/system/sockets.target.wants/ssh.socket" ] || die "ssh.socket not enabled"
    grep -q '40-ssh.sh: sshd config OK' "$log" || die "sshd -t did not run"
    in_target "$t" /usr/sbin/sshd -T -C user="$u",host=mac,addr=192.168.64.1 > "$W/sshd-T" 2>&1 || { cat "$W/sshd-T"; die "sshd -T failed"; }
    grep -qx 'passwordauthentication no' "$W/sshd-T" && grep -qx 'permitrootlogin no' "$W/sshd-T" \
        && grep -qx "allowusers $u" "$W/sshd-T" || die "effective sshd config is not key-only for $u"
    echo "ssh: Ubuntu's sshd with our drop-in: passwordauthentication no, permitrootlogin no, allowusers $u"

    [ "$(readlink "$t/etc/systemd/system/default.target" 2>/dev/null || echo none)" = "$before" ] \
        || die "default target changed from $before to $(readlink "$t/etc/systemd/system/default.target")"
    grep -q '50-skip-oobe.sh: no NVIDIA OOBE default target' "$log" || die "50-skip-oobe.sh did not leave the target alone"
    echo "default target: untouched ($before; distribution default $(readlink "$t/usr/lib/systemd/system/default.target"))"

    # The autoinstall's user-data must pass cloud-init's own schema (subiquity checks it the same way).
    python3 -c 'import sys, yaml; print("#cloud-config"); print(yaml.safe_dump(yaml.safe_load(open(sys.argv[1]))["autoinstall"]["user-data"]))' \
        "$W/iso/brainsyncos/autoinstall.yaml" > "$t/tmp/user-data.yaml"
    in_target "$t" cloud-init schema -c /tmp/user-data.yaml || die "user-data fails cloud-init's schema"
    rm -f "$t/tmp/user-data.yaml"

    # The autoinstall's network section, as subiquity writes it to the target
    # ({"network": ...}), must make the target's own netplan send the MAC as
    # DHCP client id (scripts/utm-vm.sh looks the VM up by MAC in macOS's leases).
    python3 -c 'import sys, yaml; print(yaml.safe_dump({"network": yaml.safe_load(open(sys.argv[1]))["autoinstall"]["network"]}))' \
        "$W/iso/brainsyncos/autoinstall.yaml" > "$t/etc/netplan/90-bsos-test.yaml" && [ -d "$t/etc/netplan" ]
    chmod 600 "$t/etc/netplan/90-bsos-test.yaml"
    rm -rf "$t/run/systemd/network"
    in_target "$t" netplan generate || die "the target's netplan rejects the autoinstall's network section"
    rm -f "$t/etc/netplan/90-bsos-test.yaml"
    grep -l '^ClientIdentifier=mac$' "$t"/run/systemd/network/*.network > "$W/netplan-hits" 2>/dev/null \
        && xargs grep -l '^Name=en\*$' < "$W/netplan-hits" | grep -q . \
        || { head -50 "$t"/run/systemd/network/* >&2 || true; die "netplan did not produce a networkd file with Name=en* and ClientIdentifier=mac"; }
    echo "network: the target's netplan renders $(basename "$(head -1 "$W/netplan-hits")") with Name=en* DHCP=ipv4 ClientIdentifier=mac"
    rm -rf "$t/run/systemd/network"

    # Fallbacks: no usable DMI serial -> product_uuid; no DMI -> machine-id. Each re-run is idempotent.
    printf 'Not Specified\n' > "$W/dmi/product_serial"
    run_hook "$W/iso/brainsyncos" "$t" > /dev/null
    check_host "$t" 5e17ab "dmi product_uuid"
    run_hook "$W/iso/brainsyncos" "$t" "$W/no-dmi" > /dev/null
    check_host "$t" fedcba machine-id
    [ "$(grep -c '^BrainSyncOS ' "$t/etc/issue")" = 1 ] || die "/etc/issue gained a second BrainSyncOS line on re-run"
    ! grep -n 'WARNING' "$log" || die "a re-run logged a warning"
    echo "hook: VM identity, account, SSH and release installed; serial fallbacks and re-runs OK"
}

case "$1" in
    grub) c_grub ;;
    autoinstall) c_autoinstall ;;
    md5) c_md5 ;;
    contents) c_contents ;;
    hook) c_hook ;;
    *) die "unknown check: $1 (see --help)" ;;
esac
force_rm_tree "$W"
