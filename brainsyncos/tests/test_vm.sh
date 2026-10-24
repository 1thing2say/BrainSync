#!/usr/bin/env bash
# test_vm.sh - build a BrainSyncOS VM edition ISO for real and check it (host side).
#
# Usage: tests/test_vm.sh [--keep]
#
# Needs the Ubuntu base ISO in build/downloads/ (scripts/fetch-vm-base.sh; this
# test never downloads it). Checks the VM configs (host bash 3.2), that the
# fetch is idempotent (no download, no re-hash of a verified file) and refuses
# a wrong pin without touching the file, verifies the base ISO in the builder
# and on the host, the boot-menu and autoinstall generators (the latter against
# the schema of the ISO's own subiquity), the md5sum merge, and the login checks
# of build-vm-iso.sh. Builds a test ISO (build/out/vm-test.iso, with a throwaway
# SSH key generated for the run; the real build/out/brainsyncos-*-vm-arm64.iso
# and config/authorized_keys are never used), compares its boot structure with
# the Ubuntu base (El Torito, MBR + CHS geometry, ESP bytes, ISO9660/Joliet
# names), checks its contents (including Ubuntu's signed casper checksums), and
# runs the install hook against Ubuntu's real target layers; boots it under
# QEMU to the kernel and initramfs when the host has qemu-system-aarch64
# (tests/boot-smoke.py). Also checks
# scripts/utm-vm.sh without creating or starting a VM (help, name guard,
# --dry-run, AppleScript compilation) and against stub osascript/utmctl/arp
# (call latency, address lookup, timeout/permission messages, install's
# refusal to eject after a power-off that is not a finished install). The test ISO (~3.6 GB) is deleted at the
# end unless --keep. Per-check logs: build/test-logs/vm/.
# The in-container halves are in tests/vm-checks.sh.

BSOS_SCRIPT=test_vm.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/lib.sh"

load_vm_config
load_vm_product_config

keep=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --keep) keep=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

S="$BRAINSYNCOS_ROOT/scripts"
B="$S/builder.sh"
LOGS="$BUILD_DIR/test-logs/vm"
NAME="vm-test"
ISO="$OUT_DIR/$NAME.iso"
LOGIN="build/tmp/vm-test-login"   # relative to brainsyncos/ (= /work in the builder)
BASE_FILE="$DOWNLOADS_DIR/$BASE_ISO_NAME"
mkdir -p "$LOGS" "$TMP_DIR"
rm -f "$LOGS"/*.log
# shellcheck source=tests/testlib.sh
source "$BRAINSYNCOS_ROOT/tests/testlib.sh"

cleanup_test_iso() {
    [ "$keep" = 1 ] && return 0
    rm_released "$ISO" "$ISO.sha256" "$OUT_DIR/$NAME.manifest.txt" "$OUT_DIR/$NAME.xorriso.log" \
        "$OUT_DIR/.$NAME.iso.partial"
}
trap cleanup_test_iso EXIT

in_builder() { "$B" run tests/vm-checks.sh "$1" "$NAME"; }

t_vm_config() {
    /bin/bash --version | head -1
    /bin/bash -c 'source "$1/scripts/lib.sh"; load_vm_config; load_vm_product_config
        echo "$BASE_ISO_NAME $UBUNTU_RELEASE $ISO_COMPLIANCE volid=[$(config_volid)]"
        echo "$EDITION $BRAINSYNCOS_VERSION $BRAINSYNC_USER $HOSTNAME_PREFIX $VM_NAME $VM_BACKEND -> $(vm_iso_name)"
        [ "$(vm_iso_name)" = "brainsyncos-$BRAINSYNCOS_VERSION-vm-arm64" ]
        [ "$(config_volid)" = "Ubuntu-Server 24.04.5 LTS arm64" ]' _ "$BRAINSYNCOS_ROOT"
    # The Jetson config is untouched by the VM edition.
    /bin/bash -c 'source "$1/scripts/lib.sh"; load_config; load_product_config
        echo "jetson: $BASE_ISO_NAME -> $(product_iso_name) EDITION=$(jetson_edition) volid=[$(config_volid)]"
        [ "$(product_iso_name)" = "brainsyncos-$BRAINSYNCOS_VERSION-orin-nano" ] && [ "$(config_volid)" = "$BASE_ISO_VOLID" ]' _ "$BRAINSYNCOS_ROOT"
    local bad="$TMP_DIR/bad-vm.env" kv out
    for kv in EDITION=orin-nano VM_GRUB_TIMEOUT=soon VM_INSTALL_KERNEL=lowlatency VM_CONSOLES=tty0,,x \
              VM_NAME=deb VM_NAME=MyBrainSyncOS VM_BACKEND=vmware VM_MEMORY_MB=512 VM_CPU_CORES=0 VM_DISK_GB=8; do
        sed "s/^${kv%%=*}=.*/$kv/" "$BSOS_VM_PRODUCT_CONFIG" > "$bad"
        if out="$(BSOS_VM_PRODUCT_CONFIG="$bad" /bin/bash -c 'source "$1/scripts/lib.sh"; load_vm_config; load_vm_product_config' _ "$BRAINSYNCOS_ROOT" 2>&1)"; then
            rm -f "$bad"; die "accepted $kv"
        fi
        case "$kv" in VM_NAME=*) want="VM name must" ;; *) want="${kv%%=*} must" ;; esac
        printf '%s\n' "$out" | grep -q "$want" || die "rejected $kv for the wrong reason: $out"
        echo "rejects $kv: ${out##*ERROR: }"
    done
    sed 's|^BASE_ISO_URL=.*|BASE_ISO_URL=http://example.com/other.iso|' "$BSOS_VM_CONFIG" > "$bad"
    if out="$(BSOS_VM_CONFIG="$bad" /bin/bash -c 'source "$1/scripts/lib.sh"; load_vm_config' _ "$BRAINSYNCOS_ROOT" 2>&1)"; then
        rm -f "$bad"; die "accepted a BASE_ISO_URL for another file"
    fi
    echo "rejects BASE_ISO_URL=http://example.com/other.iso: ${out##*ERROR: }"
    rm -f "$bad"
}

ident() { printf '%s %s %s\n' "$(file_ident "$1")" "$(file_mtime "$1")" "$(file_size "$1")"; }

# The base is fetched once (scripts/fetch-vm-base.sh); a second run must not
# download or re-hash it, and a wrong pin must be refused without touching it.
t_fetch() {
    local before out bad="$TMP_DIR/bad-vm-base.env"
    [ -f "$BASE_FILE" ] || die "no $BASE_FILE; run scripts/fetch-vm-base.sh first"
    before="$(ident "$BASE_FILE")"
    out="$(/bin/bash "$S/fetch-vm-base.sh" 2>&1)" || { printf '%s\n' "$out"; die "fetch failed"; }
    printf '%s\n' "$out"
    printf '%s\n' "$out" | grep -q 'already downloaded and verified' || die "fetch did work on a verified file"
    ! printf '%s\n' "$out" | grep -qi -e 'hashing' -e 'downloading' -e 'resuming' || die "fetch re-hashed or downloaded"
    [ ! -e "$BASE_FILE.part" ] || die "a .part file exists"
    [ "$(ident "$BASE_FILE")" = "$before" ] || die "base ISO changed: [$before] -> [$(ident "$BASE_FILE")]"
    sed 's/^BASE_ISO_SIZE=.*/BASE_ISO_SIZE=12345/' "$BSOS_VM_CONFIG" > "$bad"
    if out="$(BSOS_VM_CONFIG="$bad" /bin/bash "$S/fetch-vm-base.sh" --no-signature 2>&1)"; then
        rm -f "$bad"; die "fetch accepted a wrong size pin"
    fi
    rm -f "$bad"
    printf '%s\n' "$out" | tail -2
    printf '%s\n' "$out" | grep -q 'does not match config/vm-base-iso.env' || die "refused for the wrong reason"
    [ "$(ident "$BASE_FILE")" = "$before" ] || die "a refused fetch changed the base ISO"
    [ ! -e "$BASE_FILE.part" ] || die "a refused fetch started a download"
    echo "idempotent: no download, no re-hash; wrong pin refused, file untouched ($before)"
}

t_builder_image() {
    "$B" build
    "$B" run bash -c 'command -v xorriso grub-script-check ssh-keygen gpgv unsquashfs && test -w /scratch && test -s /usr/share/keyrings/ubuntu-archive-keyring.gpg'
}

t_verify() {
    "$B" run scripts/verify-base-iso.sh --edition vm --no-cache
    /bin/bash "$S/verify-base-iso.sh" --edition vm
    local bad="$TMP_DIR/bad-vm-base.env" out
    sed 's/^BASE_ISO_VOLID=.*/BASE_ISO_VOLID=Ubuntu-Server_24.04.4_LTS_arm64/' "$BSOS_VM_CONFIG" > "$bad"
    if out="$(BSOS_VM_CONFIG="$bad" /bin/bash "$S/verify-base-iso.sh" --edition vm 2>&1)"; then
        rm -f "$bad"; die "verify accepted a wrong volume id pin"
    fi
    rm -f "$bad"
    printf '%s\n' "$out" | grep 'verify FAILED \[volid\]'
}

t_test_login() {
    "$B" run bash -c 'set -e; rm -rf "$1"; mkdir -p "$1"
        ssh-keygen -q -t ed25519 -N "" -C brainsyncos-vm-test -f "$1/id_ed25519"
        cp "$1/id_ed25519.pub" "$1/authorized_keys"
        openssl passwd -6 -salt bsostestsalt bsos-test-pw > "$1/password.hash"' _ "$LOGIN"
    [ -s "$BRAINSYNCOS_ROOT/$LOGIN/authorized_keys" ] || die "no test key"
    ! cmp -s "$BRAINSYNCOS_ROOT/$LOGIN/authorized_keys" "$BRAINSYNCOS_ROOT/config/authorized_keys" || die "test key equals the real one"
    cat "$BRAINSYNCOS_ROOT/$LOGIN/authorized_keys"
}

t_build_logins() {
    local out
    expect_refusal() {  # expect_refusal MESSAGE ARGS...
        local msg="$1"; shift
        if out="$("$B" run scripts/build-vm-iso.sh --name refused "$@" 2>&1)"; then die "build accepted: $*"; fi
        printf '%s\n' "$out" | grep -q "$msg" || die "refused for the wrong reason ($*): $out"
        echo "refuses $*: $(printf '%s\n' "$out" | tail -1)"
    }
    expect_refusal 'no way to log in' --authorized-keys "$LOGIN/none" --password-hash "$LOGIN/none"
    expect_refusal 'contains a PRIVATE key' --authorized-keys "$LOGIN/id_ed25519" --password-hash "$LOGIN/none"
    expect_refusal 'not a valid authorized_keys' --authorized-keys "$LOGIN/password.hash" --password-hash "$LOGIN/none"
    expect_refusal 'not a single crypt(3) hash' --authorized-keys "$LOGIN/none" --password-hash "$LOGIN/authorized_keys"
    expect_refusal 'invalid --name' --name ../x --authorized-keys "$LOGIN/authorized_keys"
    [ ! -e "$OUT_DIR/refused.iso" ] || die "a refused build left an ISO"
}

t_build() {
    "$B" run scripts/build-vm-iso.sh --name "$NAME" --authorized-keys "$LOGIN/authorized_keys" --password-hash "$LOGIN/none"
    [ -s "$ISO" ] && [ -s "$ISO.sha256" ] && [ -s "$OUT_DIR/$NAME.manifest.txt" ] || die "build outputs missing"
    (cd "$OUT_DIR" && shasum -a 256 -c "$NAME.iso.sha256")
    grep -q 'brainsyncos-vm-test' "$VM_STAGE_DIR/brainsyncos/config/authorized_keys" || die "staged key is not the throwaway one"
}

t_boot_compare() {
    local out added
    out="$("$B" run scripts/compare-iso.sh "build/out/$NAME.iso" --edition vm 2>&1)" || { printf '%s\n' "$out"; return 1; }
    printf '%s\n' "$out"
    added="$(find "$VM_STAGE_DIR/brainsyncos" | wc -l | tr -d ' ')"
    printf '%s\n' "$out" | grep -q "^PASS names .*iso9660 [0-9]* names equal (+$added added); joliet [0-9]* names equal (+$added added)" \
        || die "names check: expected all base names unchanged and $added added"
    for c in eltorito boot-esp mkisofs chs descriptors mbr esp-bytes names; do
        printf '%s\n' "$out" | grep -q "^PASS $c " || die "check $c did not pass"
    done
}

# Boot the test ISO under QEMU (UEFI) to the BrainSyncOS menu, Enter, and the
# kernel with our arguments running the initramfs. Needs a host
# qemu-system-aarch64 and edk2 firmware (QEMU's own or UTM's); else skipped.
t_boot_smoke() {
    local q fw="" f
    q="$(command -v qemu-system-aarch64 || true)"
    if [ -n "$q" ]; then
        for f in "$(dirname "$q")/../share/qemu/edk2-aarch64-code.fd" /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
                 /Applications/UTM.app/Contents/Resources/qemu/edk2-aarch64-code.fd; do
            [ -f "$f" ] && { fw="$f"; break; }
        done
    fi
    if [ -z "$q" ] || [ -z "$fw" ]; then
        echo "SKIP boot-smoke: no qemu-system-aarch64 + edk2-aarch64-code.fd on this host"
        return 0
    fi
    echo "qemu: $q ($("$q" --version | head -1)), firmware: $fw"
    python3 "$BRAINSYNCOS_ROOT/tests/boot-smoke.py" "$ISO" --qemu "$q" --firmware "$fw" \
        --log "$LOGS/boot-smoke.serial.log" --timeout 900 \
        --expect "autoinstall subiquity.autoinstallpath=/cdrom/brainsyncos/autoinstall.yaml ---" \
        --expect "--- console=$(printf '%s' "$VM_CONSOLES" | sed 's/,/ console=/g')"
}

t_utm_script() {
    local u="$S/utm-vm.sh" out
    /bin/bash "$u" --help | grep -q 'never creates, changes or deletes any other VM'
    for args in "status --name deb" "delete --name deb --yes" "create --name Ubuntu --dry-run" "eject-iso --name brainsyncos"; do
        # shellcheck disable=SC2086
        if out="$(/bin/bash "$u" $args 2>&1)"; then die "utm-vm.sh accepted: $args"; fi
        printf '%s\n' "$out" | grep -q 'VM name must start with BrainSyncOS' || die "wrong refusal for $args: $out"
        echo "refuses $args"
    done
    # Any existing file serves as the ISO for a dry run (the test ISO is not built yet).
    out="$(/bin/bash "$u" create --dry-run --iso "$BASE_FILE" 2>&1)"
    printf '%s\n' "$out" | grep -q 'make new virtual machine with properties {backend:apple' || die "apple dry-run: $out"
    # UTM gets a clone staged in its own container, not the ISO's own path.
    printf '%s\n' "$out" | grep -q "argv: $VM_NAME [^ ]*/com.utmapp.UTM/Data/tmp/brainsyncos-$VM_NAME/brainsyncos-installer.iso $VM_MEMORY_MB $VM_CPU_CORES $((VM_DISK_GB * 1024)) 02:" || die "apple dry-run argv: $out"
    printf '%s\n' "$out" | grep -q 'drives:{{source:iso}, {guest size:' || die "apple dry-run: the installer is not an imported drive: $out"
    out="$(/bin/bash "$u" create --dry-run --backend qemu --name BrainSyncOS-qemu --memory 8192 --iso "$BASE_FILE" 2>&1)"
    printf '%s\n' "$out" | grep -q 'backend:qemu, configuration:{name:vmName, architecture:"aarch64"' || die "qemu dry-run: $out"
    printf '%s\n' "$out" | grep -q 'argv: BrainSyncOS-qemu .* 8192 ' || die "qemu dry-run argv: $out"
    printf '%s\n' "$out" | grep -q 'drives:{{source:iso, raw:true, interface:VirtIO}' || die "qemu dry-run: the installer is not an imported raw drive: $out"
    if out="$(/bin/bash "$u" create --dry-run --iso /nonexistent.iso 2>&1)"; then die "dry-run accepted a missing ISO"; fi
    echo "dry-runs: apple and qemu AppleScript + argv as expected; missing ISO refused"
    if [ -d /Applications/UTM.app ] && command -v osacompile > /dev/null; then
        /bin/bash "$u" check-scripts
    else
        echo "SKIP check-scripts: no UTM.app or osacompile"
    fi
}

# utm-vm.sh against stub osascript/utmctl/arp (no UTM involved): every
# AppleScript result captured by $(...) or a pipe must come back at once (a
# watchdog holding the pipe used to stall each call for the full timeout),
# the address lookup (newest lease by client id 1,MAC, then ARP), the
# timeout and permission messages, install's refusal to eject the ISO
# after a power-off that is not a finished install, create's staged clone in
# UTM's container, eject by the installer drive's ImageName, and a start
# that UTM refuses; a serial TCP console that opens late.
t_utm_stubbed() {
    local d="$TMP_DIR/utm-stub" u="$S/utm-vm.sh" vm=BrainSyncOS-stub out t mac port=45991
    rm -rf "$d"
    mkdir -p "$d/bin" "$d/UTM.app/Contents/MacOS"
    cat > "$d/bin/osascript" <<'EOF'
#!/bin/bash
# Stub osascript: answers utm-vm.sh's AppleScripts from $STUB_DIR.
src="$1"
case "${STUB_MODE:-}" in
    hang) exec sleep 60 ;;
    deny) echo "execution error: Not authorized to send Apple events to UTM. (-1743)" >&2; exit 1 ;;
esac
sleep 0.3
next_status() {  # the first line of status.seq, consumed unless it is the last
    local s
    s="$(head -1 "$STUB_DIR/status.seq")"
    [ "$(wc -l < "$STUB_DIR/status.seq")" -le 1 ] || sed -i '' 1d "$STUB_DIR/status.seq"
    printf '%s' "$s"
}
shift
if grep -q 'update configuration' "$src"; then echo "eject $*" >> "$STUB_DIR/calls"; echo 1
elif grep -q 'make new virtual machine' "$src"; then
    # create: record argv and whether the staged installer exists right now.
    echo "create $* staged=$([ -s "$2" ] && echo yes || echo no)" >> "$STUB_DIR/calls"; echo ID-NEW
elif grep -q 'repeat with vm in virtual machines' "$src"; then
    printf 'BrainSyncOS-stub\tID-1\tstopped\tapple\nOther VM\tID-2\tstarted\tqemu\n'
elif grep -q 'exists virtual machine named' "$src"; then
    [ -n "${STUB_NOVM:-}" ] || printf 'ID-1\t%s\tapple' "$(next_status)"
elif grep -q 'drives of cfg' "$src"; then printf 'false\tDRIVE-ISO\t\nfalse\tDRIVE-DISK\t\n'
elif grep -q 'serial port 1' "$src"; then cat "$STUB_DIR/serial"
fi
EOF
    cat > "$d/UTM.app/Contents/MacOS/utmctl" <<'EOF'
#!/bin/bash
echo "utmctl $*" >> "$STUB_DIR/calls"
[ "$1" != version ] || echo 4.7.4-stub
# UTM 4.7's utmctl reports a refused start like this, and still exits 0.
[ "$1" != start ] || [ -z "${STUB_START_FAIL:-}" ] || printf 'Error from event: The operation couldn%st be completed. (OSStatus error -2700.)\nCannot access resource: /x.iso\n' "'"
EOF
    cat > "$d/bin/arp" <<'EOF'
#!/bin/bash
[ -z "${STUB_ARP:-}" ] || printf '? (%s) at %s on bridge100 ifscope [bridge]\n' "${STUB_ARP%% *}" "${STUB_ARP#* }"
printf '? (10.0.0.1) at d0:ea:11:91:a8:93 on en0 ifscope [ethernet]\n'
EOF
    chmod +x "$d/bin/osascript" "$d/bin/arp" "$d/UTM.app/Contents/MacOS/utmctl"
    : > "$d/calls"
    printf 'ptty\t/dev/null\t0' > "$d/serial"
    echo stopped > "$d/status.seq"
    : > "$d/leases"
    # The VM bundle as UTM writes it: the installer is drive DRIVE-ISO.
    mkdir -p "$d/utmdata/Documents/$vm.utm" "$d/utmdata/tmp"
    cat > "$d/utmdata/Documents/$vm.utm/config.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Drive</key><array>
<dict><key>Identifier</key><string>DRIVE-DISK</string><key>ImageName</key><string>DRIVE-DISK.img</string></dict>
<dict><key>Identifier</key><string>DRIVE-ISO</string><key>ImageName</key><string>brainsyncos-installer.iso</string></dict>
</array></dict></plist>
EOF
    stub() {  # stub ARGS...: utm-vm.sh with the stubs; output in $out, seconds in $t
        local t0 r
        t0="$(date +%s)"
        set +e
        out="$(PATH="$d/bin:$PATH" STUB_DIR="$d" UTM_APP="$d/UTM.app" BSOS_DHCP_LEASES="$d/leases" BSOS_UTM_DATA="$d/utmdata" \
               BSOS_OSA_TIMEOUT="${STUB_TIMEOUT:-20}" /bin/bash "$u" "$@" 2>&1)"
        r=$?
        set -e
        t=$(( $(date +%s) - t0 ))
        return "$r"
    }
    stub list || die "list failed: $out"
    [ "$t" -le 8 ] || die "list took ${t}s (an AppleScript piped into awk waited for the watchdog)"
    printf '%s\n' "$out" | grep -q '^BrainSyncOS-stub ' && ! printf '%s\n' "$out" | grep -q 'Other' || die "list: $out"
    echo "list in ${t}s, only BrainSyncOS VMs: $out"
    stub status --name "$vm" || die "status failed: $out"
    [ "$t" -le 8 ] && [ "$out" = stopped ] || die "status: ${t}s, '$out' (a \$(osa ...) waited for the watchdog?)"
    echo "status (\$(osa ...)) in ${t}s: $out"
    # Address: newest lease (not the first) by client id 1,MAC; DUID leases ignored; then ARP.
    stub create --dry-run --name "$vm" --iso "$BASE_FILE" || die "dry-run: $out"
    mac="$(printf '%s\n' "$out" | sed -n 's/.*, MAC \([0-9a-f:]*\), ISO.*/\1/p' | tr 'A-F' 'a-f' | sed -E 's/(^|:)0([0-9a-f])/\1\2/g')"
    [ -n "$mac" ] || die "no MAC in: $out"
    cat > "$d/leases" <<EOF
{
	name=ubuntu-server
	ip_address=192.168.64.10
	hw_address=1,$mac
	identifier=1,$mac
	lease=0x6a000000
}
{
	name=brainsync-abc123
	ip_address=192.168.64.99
	hw_address=ff,6d:b5:fa:2f:0:1:0:1:31:f5:e8:5d:6e:cf:6d:b5:fa:2f
	identifier=ff,6d:b5:fa:2f:0:1:0:1:31:f5:e8:5d:6e:cf:6d:b5:fa:2f
	lease=0x6b000000
}
{
	name=brainsync-abc123
	ip_address=192.168.64.11
	identifier=1,$mac
	hw_address=1,$mac
	lease=0x6adc1bc6
}
{
	name=other
	ip_address=192.168.64.12
	hw_address=1,2a:c5:c8:28:a9:78
	identifier=1,2a:c5:c8:28:a9:78
	lease=0x6aff0000
}
EOF
    stub ip --name "$vm" || die "ip failed: $out"
    [ "$out" = 192.168.64.11 ] || die "ip picked '$out', want the newest lease for $mac (192.168.64.11)"
    : > "$d/leases"
    STUB_ARP="192.168.64.77 $mac" stub ip --name "$vm" || die "ip via arp failed: $out"
    [ "$out" = 192.168.64.77 ] || die "ip via arp: '$out'"
    echo "ip: newest 1,$mac lease (192.168.64.11, not the older one or a DUID lease), else ARP (192.168.64.77)"
    # Timeouts and permissions.
    if STUB_MODE=hang STUB_TIMEOUT=2 stub status --name "$vm"; then die "a hanging osascript succeeded"; fi
    [ "$t" -le 8 ] && printf '%s\n' "$out" | grep -q 'no answer from UTM within 2s.*Automation' || die "hang: ${t}s: $out"
    echo "hanging osascript: stopped after ${t}s: $(printf '%s\n' "$out" | tail -1 | cut -c1-110)..."
    if STUB_MODE=deny stub status --name "$vm"; then die "a refused Apple Event succeeded"; fi
    printf '%s\n' "$out" | grep -q 'does not allow this terminal to control UTM' || die "deny: $out"
    echo "-1743: $(printf '%s\n' "$out" | tail -1 | cut -c1-90)..."
    # install: --hide with a boot menu that waits for a key is refused.
    if [ "$VM_GRUB_TIMEOUT" = -1 ]; then
        if stub install --name "$vm" --hide; then die "install --hide accepted with VM_GRUB_TIMEOUT=-1"; fi
        printf '%s\n' "$out" | grep -q -- '--hide: the ISO.s boot menu waits for a key' || die "--hide: $out"
        echo "install --hide refused: VM_GRUB_TIMEOUT=-1"
    fi
    # create: the ISO goes to UTM as a clone staged in UTM's container, which
    # is gone afterwards; UTM imports it (no removable drive).
    printf 'fake iso\n' > "$d/fake.iso"
    : > "$d/calls"
    STUB_NOVM=1 stub create --name "$vm" --iso "$d/fake.iso" || die "create: $out"
    grep -q "^create $vm $d/utmdata/tmp/brainsyncos-$vm/brainsyncos-installer.iso .* staged=yes" "$d/calls" || die "create argv: $(cat "$d/calls")"
    [ ! -e "$d/utmdata/tmp/brainsyncos-$vm" ] || die "create left the staged ISO behind"
    echo "create: staged clone passed to UTM, removed afterwards"
    # eject: the installer drive (by ImageName in config.plist), not the disk.
    stub eject-iso --dry-run --name "$vm" || die "eject dry-run: $out"
    printf '%s\n' "$out" | grep -q 'would remove 1 drive(s) .*DRIVE-ISO' && ! printf '%s\n' "$out" | grep -q DRIVE-DISK || die "eject dry-run: $out"
    : > "$d/calls"
    stub eject-iso --name "$vm" || die "eject: $out"
    grep -qx 'eject ID-1 DRIVE-ISO' "$d/calls" || die "eject argv: $(cat "$d/calls")"
    echo "eject-iso: drops DRIVE-ISO (brainsyncos-installer.iso), keeps the disk"
    # start: utmctl exits 0 when UTM refuses; the VM's status says it failed.
    printf 'stopped\n' > "$d/status.seq"
    if STUB_START_FAIL=1 stub start --name "$vm"; then die "start accepted a VM that stayed stopped"; fi
    printf '%s\n' "$out" | grep -q 'UTM did not start.*Cannot access resource' || die "refused start: $out"
    echo "refused start reported after ${t}s: $(printf '%s\n' "$out" | tail -1 | cut -c1-100)"
    # install: a power-off right after start (no console output) is not an install.
    printf 'stopped\nstarted\nstopped\n' > "$d/status.seq"
    : > "$d/calls"
    if stub install --name "$vm"; then die "install accepted an immediate power-off"; fi
    printf '%s\n' "$out" | grep -q 'too soon for an install' || die "early stop: $out"
    ! grep -q eject "$d/calls" || die "early stop ejected the ISO"
    grep -q "utmctl start ID-1" "$d/calls" || die "install did not start the VM: $(cat "$d/calls")"
    echo "power-off after ${t}s without console output: refused, ISO kept"
    # install: console captured (boot menu text) but no success marker, then stopped.
    printf 'tcp\t\t%s' "$port" > "$d/serial"
    printf 'stopped\nstarted\nstarted\nstopped\n' > "$d/status.seq"
    : > "$d/calls"
    (printf 'GNU GRUB  version 2.12\nInstall BrainSyncOS\n' | nc -l 127.0.0.1 "$port" > /dev/null 2>&1 &)
    sleep 1
    if stub install --name "$vm"; then die "install accepted a stop at the boot menu"; fi
    printf '%s\n' "$out" | grep -q "without the installer's success marker" || die "boot-menu stop: $out"
    ! grep -q eject "$d/calls" || die "boot-menu stop ejected the ISO"
    grep -q 'GNU GRUB' "$BUILD_DIR/vm/$vm-install.log" || die "serial console not captured"
    echo "power-off after ${t}s with console output but no success marker: refused, ISO kept"
    # install: the failure marker ends the wait while the VM still runs. The
    # console's TCP server opens only 8 s later (QEMU's opens after the start):
    # the capture must keep retrying, and leave no nc behind.
    printf 'stopped\nstarted\n' > "$d/status.seq"
    : > "$d/calls"
    (sleep 8; printf 'Ubuntu installer\nBRAINSYNCOS-INSTALL-FAILED\n' | nc -l 127.0.0.1 "$port" > /dev/null 2>&1) &
    if stub install --name "$vm" --timeout 5; then die "install ignored the failure marker"; fi
    printf '%s\n' "$out" | grep -q 'reported a failure' || die "failure marker: $out"
    [ "$t" -ge 8 ] && [ "$t" -le 60 ] || die "failure marker noticed after ${t}s (console opened at 8 s)"
    ! grep -q eject "$d/calls" || die "failed install ejected the ISO"
    ! pgrep -f "nc -d 127.0.0.1 $port" > /dev/null || die "serial capture left an nc running"
    echo "failure marker (console up 8 s late): install failed after ${t}s, VM left running, ISO kept"
    pkill -f "nc -l 127.0.0.1 $port" 2>/dev/null || true
    rm -f "$BUILD_DIR/vm/$vm-install.log"
    rmdir "$BUILD_DIR/vm" 2>/dev/null || true
    rm -rf "$d"
}

t_cleanup() {
    cleanup_test_iso
    [ "$keep" = 1 ] || [ ! -e "$ISO" ] || die "test ISO still present"
    local held
    if command -v lsof > /dev/null 2>&1; then
        held="$(lsof +L1 2>/dev/null | awk -v n="$NAME.iso" 'index($0, n) && $7 > 0' || true)"
        [ -z "$held" ] || die "deleted test ISO still holds disk space: $held"
        echo "no deleted-but-allocated test ISO (lsof +L1)"
    fi
    df -h "$BUILD_DIR" | tail -1
}

log "VM edition test chain (logs: ${LOGS#$BRAINSYNCOS_ROOT/}/)"
check vm-config            t_vm_config
check disk-space           check_free_space "$BUILD_DIR" "$MIN_FREE_GB"
check fetch-idempotent     t_fetch
if [ "$nfail" -gt 0 ]; then log "aborting: prerequisites failed"; exit 1; fi
check builder-image        t_builder_image
check verify-base          t_verify
check test-login           t_test_login
check render-grub          in_builder grub
check autoinstall          in_builder autoinstall
check merge-md5sums        in_builder md5
check build-logins         t_build_logins
check utm-script           t_utm_script
check utm-stubbed          t_utm_stubbed
if [ "$nfail" -gt 0 ]; then log "aborting: generators or login checks failed"; exit 1; fi
check build-vm-iso         t_build
if [ -f "$ISO" ]; then
    check boot-compare     t_boot_compare
    check iso-contents     in_builder contents
    check boot-smoke       t_boot_smoke
    check install-hook     in_builder hook
fi
check cleanup              t_cleanup

finish "VM edition"
