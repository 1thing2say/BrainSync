#!/usr/bin/env bash
# utm-vm.sh - run the BrainSyncOS VM edition in UTM on this Mac (host side).
#
# Usage: scripts/utm-vm.sh COMMAND [--name NAME] [options]
#   check                 UTM installed, its version, and permission to script it
#   create [--backend apple|qemu] [--iso ISO] [--memory MB] [--cpus N] [--disk GB]
#          [--dry-run]    a new VM with the installer ISO attached (defaults:
#                         config/vm.env; ISO build/out/brainsyncos-VERSION-vm-arm64.iso)
#   install [--timeout MIN] [--hide]
#                         boot the installer (pick "Install BrainSyncOS ..." in
#                         the boot menu unless VM_GRUB_TIMEOUT >= 0; so --hide
#                         needs such an ISO), wait until the install powers the
#                         VM off, eject the ISO, start the VM from its disk,
#                         wait for SSH, print the login. The ISO is only ejected
#                         when the serial console showed the installer's success
#                         marker (or, without a console capture, the VM ran at
#                         least 5 min); a failure marker ends the wait at once
#   start [--hide] | stop [--force] | status | ip | ssh-info
#   eject-iso [--dry-run] remove the installer drive (VM must be stopped)
#   delete [--yes] [--dry-run]
#                         delete the VM and its disk (asks you to type the name;
#                         BSOS_CONFIRM_VM=NAME answers non-interactively)
#   list                  the BrainSyncOS VMs UTM knows
#   check-scripts         compile every AppleScript this uses against UTM's
#                         dictionary (osacompile; sends no Apple Events)
#
# NAME defaults to VM_NAME (config/vm.env) and must start with "BrainSyncOS":
# this script never creates, changes or deletes any other VM.
# Backends: apple (Apple Virtualization; fast; serial console hvc0; no USB
# passthrough before macOS 27) or qemu (QEMU with Hypervisor.framework;
# serial console ttyAMA0 on a TCP port; QEMU guest agent for `ip`; USB
# sharing for the Cerelog EEG board, which UTM 4.7 cannot switch on by script:
# Edit VM > Input > USB Sharing, then `utmctl usb connect NAME VID:PID`).
# The installer ISO is not attached as a removable drive: sandboxed UTM can
# only open a removable drive's file when it was picked in UTM's own file
# dialog ("Cannot access resource" on start otherwise). create clones the ISO
# (APFS clone, no extra disk space) into UTM's container and lets UTM import
# it into the VM bundle as a drive named brainsyncos-installer.iso; eject-iso
# and install's eject remove that drive, and UTM deletes its file.
# The NIC gets a fixed, locally administered MAC derived from NAME, and the
# guest sends it as its DHCP client id (vm/autoinstall.yaml), so the VM's
# address is found in macOS's DHCP leases (/var/db/dhcpd_leases), or else in
# the host's ARP cache.
# Driving UTM uses Apple Events: the first run asks "... wants access to
# control UTM"; answer OK (System Settings > Privacy & Security > Automation).
# Logs: build/vm/NAME-*.log (serial console capture during install).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"
load_vm_config
load_vm_product_config

UTM_APP="${UTM_APP:-/Applications/UTM.app}"
UTMCTL="$UTM_APP/Contents/MacOS/utmctl"
# BSOS_DHCP_LEASES and BSOS_OSA_TIMEOUT (seconds per Apple Event script or
# utmctl call) exist for tests/test_vm.sh.
LEASES="${BSOS_DHCP_LEASES:-/var/db/dhcpd_leases}"
OSA_TIMEOUT="${BSOS_OSA_TIMEOUT:-120}"
check_vm_number BSOS_OSA_TIMEOUT "$OSA_TIMEOUT" 1 3600
VM_LOG_DIR="$BUILD_DIR/vm"
# UTM's sandbox container (VM bundles in Documents/; BSOS_UTM_DATA for tests).
UTM_DATA="${BSOS_UTM_DATA:-$HOME/Library/Containers/com.utmapp.UTM/Data}"
# File name of the installer drive in the VM bundle (see the header).
INSTALLER_IMAGE=brainsyncos-installer.iso
QEMU_SERIAL_PORT_BASE=45550
# Printed on the serial consoles by vm/autoinstall.yaml's last late-command
# and by its error-commands (checked by scripts/check-autoinstall.py).
INSTALL_OK=BRAINSYNCOS-INSTALL-OK
INSTALL_FAILED=BRAINSYNCOS-INSTALL-FAILED
# Without a console capture, a power-off sooner than this is not an install.
MIN_INSTALL_SECS=300

cmd="${1:-}"
[ -n "$cmd" ] || { usage_from_header; exit 2; }
shift
case "$cmd" in -h|--help|help) usage_from_header; exit 0 ;; esac

name="$VM_NAME"
backend="$VM_BACKEND"
memory="$VM_MEMORY_MB"
cpus="$VM_CPU_CORES"
disk_gb="$VM_DISK_GB"
iso="$OUT_DIR/$(vm_iso_name).iso"
dry_run=0
yes=0
force=0
hide=0
timeout_min=90
while [ "$#" -gt 0 ]; do
    case "$1" in
        --name) name="${2:?--name needs a value}"; shift 2 ;;
        --backend) backend="${2:?--backend needs apple or qemu}"; shift 2 ;;
        --iso) iso="${2:?--iso needs a file}"; shift 2 ;;
        --memory) memory="${2:?--memory needs MiB}"; shift 2 ;;
        --cpus) cpus="${2:?--cpus needs a number}"; shift 2 ;;
        --disk) disk_gb="${2:?--disk needs GiB}"; shift 2 ;;
        --timeout) timeout_min="${2:?--timeout needs minutes}"; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        --yes) yes=1; shift ;;
        --force) force=1; shift ;;
        --hide) hide=1; shift ;;
        -h|--help) usage_from_header; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
check_vm_name "$name"
case "$backend" in apple|qemu) ;; *) die "--backend must be apple or qemu, got $backend" ;; esac
check_vm_number --memory "$memory" 2048 65536
check_vm_number --cpus "$cpus" 1 64
check_vm_number --disk "$disk_gb" 16 2048
check_vm_number --timeout "$timeout_min" 1 1440

# vm_mac NAME: locally administered unicast MAC (02:...), stable per name.
vm_mac() {
    local h
    h="$(printf '%s' "$1" | shasum -a 256 | cut -c1-8)"
    printf '02:b5:%s:%s:%s:%s\n' "$(printf '%s' "$h" | cut -c1-2)" "$(printf '%s' "$h" | cut -c3-4)" \
        "$(printf '%s' "$h" | cut -c5-6)" "$(printf '%s' "$h" | cut -c7-8)"
}

# qemu_serial_port NAME: a TCP port for the QEMU serial console, stable per name.
qemu_serial_port() {
    printf '%d\n' $((QEMU_SERIAL_PORT_BASE + 0x$(printf '%s' "$1" | shasum -a 256 | cut -c1-3) % 400))
}

# --- AppleScript ------------------------------------------------------------------
# Each as_* function prints one script; osa SCRIPT_FN ARGS... runs it with
# ARGS as argv (never interpolated into the source).

as_lookup() { cat <<'EOF'
on run argv
    tell application "UTM"
        if exists virtual machine named (item 1 of argv) then
            set vm to virtual machine named (item 1 of argv)
            return (id of vm) & tab & (status of vm as text) & tab & (backend of vm as text)
        end if
        return ""
    end tell
end run
EOF
}

as_list() { cat <<'EOF'
on run argv
    set out to ""
    tell application "UTM"
        repeat with vm in virtual machines
            set out to out & (name of vm) & tab & (id of vm) & tab & (status of vm as text) & tab & (backend of vm as text) & linefeed
        end repeat
    end tell
    return out
end run
EOF
}

as_create_apple() { cat <<'EOF'
on run argv
    set {vmName, isoPath, mem, cpus, diskMiB, mac} to argv
    -- Outside the tell block: inside it, UTM itself would be asked to
    -- resolve "POSIX file" and fails with -1728 (Can't get POSIX file).
    -- «class SrPt» below is the configuration's "serial ports" property:
    -- written as words, AppleScript compiles that to the "serial port"
    -- element class, and UTM rejects the record (-1700).
    set iso to POSIX file isoPath
    tell application "UTM"
        set vm to make new virtual machine with properties {backend:apple, configuration:{name:vmName, memory:(mem as integer), cpu cores:(cpus as integer), drives:{{source:iso}, {guest size:(diskMiB as integer)}}, network interfaces:{{mode:shared, address:mac}}, «class SrPt»:{{interface:ptty}}, displays:{{dynamic resolution:true}}}}
        return id of vm
    end tell
end run
EOF
}

as_create_qemu() { cat <<'EOF'
on run argv
    set {vmName, isoPath, mem, cpus, diskMiB, mac, serialPort} to argv
    set iso to POSIX file isoPath -- outside the tell block (see as_create_apple)
    tell application "UTM"
        set vm to make new virtual machine with properties {backend:qemu, configuration:{name:vmName, architecture:"aarch64", machine:"virt", memory:(mem as integer), cpu cores:(cpus as integer), hypervisor:true, uefi:true, drives:{{source:iso, raw:true, interface:VirtIO}, {guest size:(diskMiB as integer), interface:VirtIO}}, network interfaces:{{mode:shared, address:mac}}, «class SrPt»:{{interface:tcp, port:(serialPort as integer)}}, displays:{{hardware:"virtio-ramfb-gl"}}}}
        return id of vm
    end tell
end run
EOF
}

# Drives of a VM: "removable<TAB>id<TAB>interface" per drive (interface:
# QEMU only). UTM 4.7 does not report an existing drive's file (source).
as_drives() { cat <<'EOF'
on run argv
    set out to ""
    tell application "UTM"
        set cfg to configuration of virtual machine id (item 1 of argv)
        repeat with d in (drives of cfg)
            set r to contents of d
            set did to ""
            set itf to ""
            try
                set did to (id of r) as text
            end try
            try
                set itf to (interface of r) as text
            end try
            set out to out & (removable of r as text) & tab & did & tab & itf & linefeed
        end repeat
    end tell
    return out
end run
EOF
}

# as_eject VM_ID DRIVE_ID...: drops the removable drives and the listed ones.
as_eject() { cat <<'EOF'
on run argv
    set drop to rest of argv
    tell application "UTM"
        set vm to virtual machine id (item 1 of argv)
        if status of vm is not stopped then error "the VM is not stopped" number 1001
        set cfg to configuration of vm
        set keep to {}
        repeat with d in (drives of cfg)
            set r to contents of d
            if removable of r is false and drop does not contain ((id of r) as text) then set end of keep to r
        end repeat
        set drives of cfg to keep
        update configuration of vm with cfg
        return (count of keep) as text
    end tell
end run
EOF
}

as_serial() { cat <<'EOF'
on run argv
    tell application "UTM"
        set p to serial port 1 of virtual machine id (item 1 of argv)
        return (interface of p as text) & tab & (address of p) & tab & ((port of p) as text)
    end tell
end run
EOF
}

as_delete() { cat <<'EOF'
on run argv
    tell application "UTM"
        set vm to virtual machine id (item 1 of argv)
        if status of vm is not stopped then error "the VM is not stopped" number 1001
        delete vm
    end tell
end run
EOF
}

# run_timeout SECS OUTFILE CMD...: CMD with stdout+stderr in OUTFILE, killed
# (rc 143) after SECS.
run_timeout() {
    local secs="$1" out="$2" pid w rc
    shift 2
    "$@" > "$out" 2>&1 &
    pid=$!
    # The watchdog and its sleep get /dev/null as every fd: holding the
    # caller's stdout would make a $(...) or pipe around the caller wait for
    # the sleep to end. On TERM it kills its sleep, so none is left behind.
    (
        s=""
        trap '[ -z "$s" ] || kill "$s" 2>/dev/null; exit 0' TERM
        sleep "$secs" &
        s=$!
        wait "$s"
        kill "$pid" 2>/dev/null || true
    ) > /dev/null 2>&1 < /dev/null &
    w=$!
    # Not set +e/-e: that would turn errexit back on in a caller that had
    # turned it off, and the caller's rc check would never run.
    wait "$pid" && rc=0 || rc=$?
    kill "$w" 2>/dev/null || true
    wait "$w" 2>/dev/null || true
    return "$rc"
}

# ae_failed WHAT RC OUTFILE: die with the most useful message for a failed
# osascript or utmctl call (both talk to UTM through Apple Events); removes OUTFILE.
ae_failed() {
    local what="$1" rc="$2" out="$3" timed_out=0
    if grep -q -e '-1743' -e 'Not authorized to send Apple events' "$out"; then
        rm -f "$out"
        die "macOS does not allow this terminal to control UTM. Open System Settings > Privacy & Security > Automation, allow UTM for your terminal app (or run 'scripts/utm-vm.sh check' and answer OK), then retry"
    fi
    # 143/137: killed by run_timeout; -1712: the Apple Event itself timed out.
    case "$rc" in 143|137) timed_out=1 ;; esac
    grep -q -e '-1712' "$out" && timed_out=1
    cat "$out" >&2
    rm -f "$out"
    [ "$timed_out" = 0 ] || die "$what: no answer from UTM within ${OSA_TIMEOUT}s. If macOS is asking whether your terminal may control UTM (\"... wants access to control UTM\"), answer OK, or allow it in System Settings > Privacy & Security > Automation, and retry; otherwise check UTM for an open dialog"
    die "$what failed (rc=$rc)"
}

# osa SCRIPT_FN ARG...: run an as_* script; prints its result.
osa() {
    local fn="$1" out src rc
    shift
    [ -d "$UTM_APP" ] || die "UTM not found at $UTM_APP (https://mac.getutm.app)"
    out="$(mktemp)"
    src="$(mktemp)"
    "$fn" > "$src"
    # The script goes in as a file: run_timeout backgrounds osascript, and a
    # background job's stdin is /dev/null.
    run_timeout "$OSA_TIMEOUT" "$out" osascript "$src" "$@" && rc=0 || rc=$?
    rm -f "$src"
    [ "$rc" = 0 ] || ae_failed "AppleScript ${fn#as_}" "$rc" "$out"
    cat "$out"
    rm -f "$out"
}

# utm ARG...: utmctl ARG... with the same timeout and error messages as osa.
utm() {
    local out rc
    [ -x "$UTMCTL" ] || die "utmctl missing in $UTM_APP"
    out="$(mktemp)"
    run_timeout "$OSA_TIMEOUT" "$out" "$UTMCTL" "$@" && rc=0 || rc=$?
    [ "$rc" = 0 ] || ae_failed "utmctl $1" "$rc" "$out"
    cat "$out"
    rm -f "$out"
}

# lookup: sets VM_ID, VM_STATUS, VM_BACKEND_NOW for $name; dies if absent.
lookup() {
    local r
    r="$(osa as_lookup "$name")"
    [ -n "$r" ] || die "UTM has no VM named $name (scripts/utm-vm.sh create --name $name)"
    VM_ID="$(printf '%s' "$r" | cut -f1)"
    VM_STATUS="$(printf '%s' "$r" | cut -f2)"
    VM_BACKEND_NOW="$(printf '%s' "$r" | cut -f3)"
}

# mac_short MAC: lower case, octets without leading zeros (how macOS's
# dhcpd_leases and arp print them).
mac_short() {
    printf '%s' "$1" | tr 'A-F' 'a-f' | sed -E 's/(^|:)0([0-9a-f])/\1\2/g'
}

# lease_ip MAC: the address of macOS's newest DHCP lease (largest lease=
# expiry, not the first in the file: the installer and the installed system
# both lease) whose client id (hw_address or identifier) is 1,MAC. A guest
# that sends a DUID instead (ff,...) cannot be matched; vm/autoinstall.yaml
# sets dhcp-identifier: mac for that reason.
lease_ip() {
    [ -r "$LEASES" ] || return 0
    awk -v m="1,$(mac_short "$1")" '
        function hex(s,  i, n) { n = 0; s = tolower(s); sub(/^0x/, "", s)
            for (i = 1; i <= length(s); i++) n = n * 16 + index("0123456789abcdef", substr(s, i, 1)) - 1
            return n }
        /^[[:space:]]*\{/ { ip = ""; hit = 0; t = -1 }
        /^[[:space:]]*ip_address=/ { sub(/.*ip_address=/, ""); ip = $0 }
        /^[[:space:]]*(hw_address|identifier)=/ { v = $0; sub(/^[^=]*=/, "", v); if (v == m) hit = 1 }
        /^[[:space:]]*lease=/ { v = $0; sub(/.*lease=/, "", v); t = hex(v) }
        /^[[:space:]]*\}/ { if (hit && ip != "" && t > best) { best = t; bip = ip } }
        BEGIN { best = -2 }
        END { if (bip != "") print bip }' "$LEASES"
}

# arp_ip MAC: the host's ARP cache entry for MAC (UTM's shared network is
# bridge100), when no lease matches.
arp_ip() {
    arp -an 2>/dev/null | awk -v m="$(mac_short "$1")" '$3 == "at" && $4 == m { gsub(/[()]/, "", $2); print $2; exit }' || true
}

vm_ip() {
    local ip="" out mac
    if [ "$VM_BACKEND_NOW" = qemu ] && [ "$VM_STATUS" = started ] && [ -x "$UTMCTL" ]; then
        # No guest agent yet is normal: a failure here only means "unknown".
        out="$(mktemp)"
        if run_timeout 20 "$out" "$UTMCTL" ip-address "$VM_ID"; then
            ip="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' "$out" | head -1 || true)"
        fi
        rm -f "$out"
    fi
    mac="$(vm_mac "$name")"
    [ -n "$ip" ] || ip="$(lease_ip "$mac")"
    [ -n "$ip" ] || ip="$(arp_ip "$mac")"
    printf '%s\n' "$ip"
}

status_now() { lookup; printf '%s\n' "$VM_STATUS"; }

# installer_drive_ids: UTM drive ids of $name's imported installer drive
# (ImageName $INSTALLER_IMAGE in the bundle's config.plist; UTM's scripting
# reports no file names). Prints nothing, with a warning, when the bundle is
# not where create puts it.
installer_drive_ids() {
    local plist="$UTM_DATA/Documents/$name.utm/config.plist" i=0
    [ -r "$plist" ] || { warn "no $plist: cannot tell which drive is the installer"; return 0; }
    while plutil -extract "Drive.$i" xml1 -o /dev/null "$plist" 2>/dev/null; do
        if [ "$(plutil -extract "Drive.$i.ImageName" raw -o - "$plist" 2>/dev/null || true)" = "$INSTALLER_IMAGE" ]; then
            plutil -extract "Drive.$i.Identifier" raw -o - "$plist"
        fi
        i=$((i + 1))
    done
}

# installer_attached: true when $name still has its installer drive or a
# removable drive (an ISO attached by hand in UTM).
installer_attached() {
    [ -n "$(installer_drive_ids)" ] || osa as_drives "$VM_ID" | grep -q '^true'
}

print_login() {
    local ip="$1"
    log "VM $name: $ip"
    cat <<EOF
Log in (key from the build's --authorized-keys, default config/authorized_keys):
  ssh $BRAINSYNC_USER@$ip
The VM also announces $HOSTNAME_PREFIX-<suffix>.local over mDNS (avahi); the
console login screen shows the name and IP, or: ssh $BRAINSYNC_USER@$ip hostname
EOF
}

# capture_serial WHAT: background copy of the serial console to a fresh
# build/vm/NAME-WHAT.log; sets SERIAL_PID (empty when not captured).
capture_serial() {
    local r iface addr port logf="$VM_LOG_DIR/$name-$1.log"
    SERIAL_PID=""
    mkdir -p "$VM_LOG_DIR"
    : > "$logf"   # markers from an earlier run must not count
    r="$(osa as_serial "$VM_ID" 2>/dev/null || true)"
    iface="$(printf '%s' "$r" | cut -f1)"; addr="$(printf '%s' "$r" | cut -f2)"; port="$(printf '%s' "$r" | cut -f3)"
    case "$iface" in
        # Raw, no echo: with ECHO on, the PTY would send the guest's own
        # output back to it as console input; canonical mode would hold
        # partial lines (and cap long ones).
        ptty) [ -c "$addr" ] && { stty -f "$addr" raw -echo 2>/dev/null || warn "could not set $addr raw"
                                  cat "$addr" >> "$logf" 2>/dev/null & SERIAL_PID=$!; } ;;
        # QEMU opens its TCP server a moment after UTM reports the VM
        # started: retry the connection (refused: nc exits 1) for a minute.
        # -d: macOS nc otherwise disconnects at once on stdin's EOF.
        tcp) { ( n=0
                 until nc -d 127.0.0.1 "$port" >> "$logf" 2>/dev/null; do
                     n=$((n + 1)); [ "$n" -lt 60 ] || exit 0; sleep 1
                 done ) < /dev/null > /dev/null 2>&1 & SERIAL_PID=$!; } ;;
    esac
    if [ -n "$SERIAL_PID" ]; then log "serial console ($iface ${addr:-}${port:+:$port}) -> ${logf#$BRAINSYNCOS_ROOT/}"; else warn "serial console not captured ($r)"; fi
}

# stop_capture: end the capture (for TCP, the retry loop and its nc).
# Children are looked up first: once the loop is gone, its nc has a new parent.
stop_capture() {
    local kids
    [ -n "${SERIAL_PID:-}" ] || return 0
    kids="$(pgrep -P "$SERIAL_PID" 2>/dev/null || true)"
    # shellcheck disable=SC2086
    kill "$SERIAL_PID" $kids 2>/dev/null || true
}

# start_vm: start $VM_ID and wait until UTM reports it running. utmctl start
# exits 0 even when UTM refuses ("Error from event ... Cannot access
# resource"), and --hide alone can print an error (-10004) for a VM that does
# start; so the VM's status decides, and utmctl's words go into the error.
start_vm() {
    local flags="" said i
    [ "$hide" = 0 ] || flags="--hide"
    # shellcheck disable=SC2086
    said="$(utm start $flags "$VM_ID" 2>&1)"
    for i in 1 2 3 4 5 6 7 8 9 10; do
        [ "$(status_now)" = stopped ] || return 0
        sleep 2
    done
    die "UTM did not start $name${said:+: $(printf '%s' "$said" | tr '\n' ' ')}"
}

# --- commands -------------------------------------------------------------------------

c_check() {
    [ -d "$UTM_APP" ] || die "UTM not found at $UTM_APP (https://mac.getutm.app)"
    [ -x "$UTMCTL" ] || die "utmctl missing in $UTM_APP"
    log "UTM $(utm version 2>/dev/null || echo '?') at $UTM_APP"
    osa as_list > /dev/null
    log "Apple Events to UTM allowed"
    [ -r "$LEASES" ] && log "DHCP leases readable: $LEASES" || warn "$LEASES not readable; ip lookup needs the QEMU guest agent"
}

c_check_scripts() {
    local fn src n=0
    [ -d "$UTM_APP" ] || die "UTM not found at $UTM_APP"
    src="$(mktemp)"
    for fn in as_lookup as_list as_create_apple as_create_qemu as_drives as_eject as_serial as_delete; do
        "$fn" > "$src"
        osacompile -o "$src.scpt" "$src" 2> "$src.err" || { cat "$src.err" >&2; rm -rf "$src" "$src.scpt" "$src.err"; die "AppleScript ${fn#as_} does not compile"; }
        rm -rf "$src.scpt" "$src.err"
        n=$((n + 1))
    done
    rm -f "$src"
    log "all $n AppleScripts compile against UTM's dictionary"
}

c_list() {
    osa as_list | awk -F '\t' '$1 ~ /^BrainSyncOS/ {printf "%-24s %-8s %-6s %s\n", $1, $3, $4, $2}'
}

c_create() {
    local disk_mib=$((disk_gb * 1024)) mac script id port
    # stage and staged are global, not local: the EXIT trap below must see them.
    mac="$(vm_mac "$name")"
    case "$iso" in /*) ;; *) iso="$(pwd -P)/$iso" ;; esac
    [ -f "$iso" ] || die "no ISO at $iso (scripts/builder.sh run scripts/build-vm-iso.sh)"
    # UTM imports the installer from inside its own sandbox container (see
    # the header); the staged clone is removed once UTM has its own.
    stage="$UTM_DATA/tmp/brainsyncos-$name"
    staged="$stage/$INSTALLER_IMAGE"
    log "create $name: $backend backend, $memory MiB, $cpus CPUs, $disk_gb GiB disk, MAC $mac, ISO $iso"
    if [ "$backend" = apple ]; then
        script=as_create_apple
        set -- "$name" "$staged" "$memory" "$cpus" "$disk_mib" "$mac"
    else
        script=as_create_qemu
        port="$(qemu_serial_port "$name")"
        set -- "$name" "$staged" "$memory" "$cpus" "$disk_mib" "$mac" "$port"
        log "QEMU serial console: TCP 127.0.0.1:$port (nc 127.0.0.1 $port)"
    fi
    if [ "$dry_run" = 1 ]; then
        log "--dry-run: would run this AppleScript with argv: $*"
        "$script"
        return 0
    fi
    [ -z "$(osa as_lookup "$name")" ] || die "UTM already has a VM named $name (delete it first, or pick --name)"
    [ -d "$UTM_DATA" ] || die "no UTM container at $UTM_DATA (start UTM once)"
    rm -rf "$stage"
    mkdir -p "$stage"
    trap 'rm -rf "$stage"' EXIT
    # An APFS clone costs no space; across volumes it is a real copy.
    cp -c "$iso" "$staged" 2>/dev/null || { warn "cannot clone $iso into UTM's container; copying it"; cp "$iso" "$staged"; }
    id="$(osa "$script" "$@")"
    rm -rf "$stage"
    log "created $name ($id), installer drive $INSTALLER_IMAGE imported into the VM bundle"
    echo "Next: scripts/utm-vm.sh install${name:+ --name $name}   (or start it and pick 'Install BrainSyncOS ...')"
    [ "$backend" = apple ] || echo "USB passthrough for the EEG board: UTM > $name > Edit > Input > USB Sharing (not scriptable in UTM 4.7)"
}

c_start() { lookup; [ "$VM_STATUS" = stopped ] || die "$name is $VM_STATUS"; start_vm; log "started $name"; }

c_stop() {
    lookup
    [ "$VM_STATUS" != stopped ] || { log "$name is already stopped"; return 0; }
    if [ "$force" = 1 ]; then utm stop --force "$VM_ID"; else utm stop --request "$VM_ID"; fi
    log "stop requested for $name ($([ "$force" = 1 ] && echo force || echo ACPI power button))"
}

c_status() { status_now; }

c_ip() {
    local ip
    lookup
    ip="$(vm_ip)"
    [ -n "$ip" ] || die "no address known for $name yet (is it running and installed?)"
    printf '%s\n' "$ip"
}

c_ssh_info() {
    local ip
    lookup
    ip="$(vm_ip)"
    [ -n "$ip" ] || die "no address known for $name yet"
    print_login "$ip"
}

# eject_installer: drop $name's installer drive and any removable drive.
eject_installer() {
    local ids
    ids="$(installer_drive_ids)"
    # shellcheck disable=SC2086
    osa as_eject "$VM_ID" $ids > /dev/null
}

c_eject() {
    local drives ids n
    lookup
    [ "$VM_STATUS" = stopped ] || die "$name is $VM_STATUS; stop it first (scripts/utm-vm.sh stop)"
    drives="$(osa as_drives "$VM_ID")"
    ids="$(installer_drive_ids)"
    n="$(printf '%s\n' "$drives" | awk -F '\t' -v ids=" $(printf '%s' "$ids" | tr '\n' ' ') " \
        '$1 == "true" || index(ids, " " $2 " ") { n++ } END { print n + 0 }')"
    [ "$n" -gt 0 ] || { log "$name has no installer or removable drive (ISO already ejected)"; return 0; }
    if [ "$dry_run" = 1 ]; then
        # UTM 4.7 reports no file name for an existing drive: show UTM's drive ids.
        log "--dry-run: would remove $n drive(s) from $name (UTM drive id, interface): $(printf '%s\n' "$drives" | awk -F '\t' -v ids=" $(printf '%s' "$ids" | tr '\n' ' ') " \
            '$1 == "true" || index(ids, " " $2 " ") {printf "%s%s ", $2, ($3 != "" ? " (" $3 ")" : "")}')"
        return 0
    fi
    eject_installer
    log "ejected the installer from $name"
}

c_delete() {
    local answer
    lookup
    [ "$VM_STATUS" = stopped ] || die "$name is $VM_STATUS; stop it first"
    if [ "$dry_run" = 1 ]; then log "--dry-run: would delete $name ($VM_ID) and its disk"; return 0; fi
    if [ "$yes" = 0 ]; then
        answer="${BSOS_CONFIRM_VM:-}"
        if [ -z "$answer" ]; then
            printf 'Delete UTM VM %s and its virtual disk? Type its name to confirm: ' "$name" >&2
            read -r answer || die "no confirmation; nothing deleted"
        fi
        [ "$answer" = "$name" ] || die "confirmation '$answer' does not match $name; nothing deleted"
    fi
    osa as_delete "$VM_ID" > /dev/null
    log "deleted $name"
}

# install_state LOG: ok, failed or unknown, from the markers vm/autoinstall.yaml
# prints on the serial consoles (last late-command / error-commands).
install_state() {
    if [ -f "$1" ] && grep -q "$INSTALL_FAILED" "$1"; then echo failed
    elif [ -f "$1" ] && grep -q "$INSTALL_OK" "$1"; then echo ok
    else echo unknown; fi
}

c_install() {
    local ip="" until t0 s st elapsed last=0 logf="$VM_LOG_DIR/$name-install.log" rel
    rel="${logf#$BRAINSYNCOS_ROOT/}"
    # Hidden, nobody could pick the install entry in a boot menu that waits for a key.
    [ "$hide" = 0 ] || [ "$VM_GRUB_TIMEOUT" -ge 0 ] \
        || die "--hide: the ISO's boot menu waits for a key (VM_GRUB_TIMEOUT=$VM_GRUB_TIMEOUT in config/vm.env), so nobody could pick 'Install BrainSyncOS ...'; drop --hide, or build an ISO with VM_GRUB_TIMEOUT >= 0"
    lookup
    [ "$VM_STATUS" = stopped ] || die "$name is $VM_STATUS; the install starts from a stopped VM"
    installer_attached || die "$name has no installer drive (create a new VM to reinstall)"
    log "starting the installer in $name; in the boot menu pick 'Install BrainSyncOS ...' (erases the VM disk)"
    t0="$(date +%s)"
    until=$((t0 + timeout_min * 60))
    start_vm
    capture_serial install
    trap stop_capture EXIT
    while :; do
        [ "$(install_state "$logf")" != failed ] \
            || die "the installer in $name reported a failure ($INSTALL_FAILED on the serial console); the VM is left at the error and the ISO stays attached. See $rel, and /var/log/installer/ in the VM"
        s="$(status_now)"
        [ "$s" != stopped ] || break
        [ "$(date +%s)" -lt "$until" ] \
            || die "$name did not power off within $timeout_min min (install still running or waiting at the boot menu?); see $rel"
        if [ $(( $(date +%s) - last )) -ge 60 ]; then
            log "waiting for the install in $name to power it off (now $s)"
            last="$(date +%s)"
        fi
        sleep 10
    done
    stop_capture
    elapsed=$(( $(date +%s) - t0 ))
    st="$(install_state "$logf")"
    case "$st" in
        ok) log "installer finished after ${elapsed}s ($INSTALL_OK on the serial console; VM powered off)" ;;
        failed) die "the installer in $name reported a failure ($INSTALL_FAILED); the ISO stays attached. See $rel" ;;
        *)
            # A captured console without the marker: stopped before the end
            # (boot menu, window closed, crash). The ISO stays for a retry.
            [ -z "$SERIAL_PID" ] || [ ! -s "$logf" ] \
                || die "$name powered off after ${elapsed}s without the installer's success marker ($INSTALL_OK) on the serial console: stopped at the boot menu, closed, or crashed? The ISO stays attached; run install again. See $rel"
            [ "$elapsed" -ge "$MIN_INSTALL_SECS" ] \
                || die "$name powered off after only ${elapsed}s, too soon for an install (stopped at the boot menu, or closed?). The ISO stays attached; run install again"
            warn "no serial console output to confirm the install; $name ran ${elapsed}s and powered off, so assuming the install finished"
            ;;
    esac
    eject_installer
    log "ejected the installer drive"
    start_vm
    capture_serial firstboot
    until=$(( $(date +%s) + 600 ))
    while [ "$(date +%s)" -lt "$until" ]; do
        lookup
        ip="$(vm_ip)"
        if [ -n "$ip" ] && nc -z -G 3 "$ip" 22 2>/dev/null; then
            print_login "$ip"
            return 0
        fi
        sleep 10
    done
    die "$name started but SSH did not answer within 10 min (last address: ${ip:-none}); see build/vm/$name-firstboot.log"
}

case "$cmd" in
    check) c_check ;;
    check-scripts) c_check_scripts ;;
    list) c_list ;;
    create) c_create ;;
    install) c_install ;;
    start) c_start ;;
    stop) c_stop ;;
    status) c_status ;;
    ip) c_ip ;;
    ssh-info) c_ssh_info ;;
    eject-iso) c_eject ;;
    delete) c_delete ;;
    *) die "unknown command: $cmd (try --help)" ;;
esac
