#!/usr/bin/env bash
# build.sh - brainsyncos-ch341-tegra: ch341.ko for NVIDIA's L4T kernel.
#
# Run by scripts/build-packages.sh (builder only; see its header for the
# BSOS_* variables), for the Jetson edition only. NVIDIA's kernel leaves
# CONFIG_USB_SERIAL_CH341 unset, so a V1 Cerelog X8 (CH340 bridge, USB vendor
# 1a86) never gets a /dev/ttyUSB* on the Jetson. This builds Linux 6.8.12's
# ch341.c (GPL-2.0, this folder) out of tree against nvidia-l4t-kernel-headers
# from the base ISO's pool, the exact TEGRA_KERNEL (config/base-iso.env), and
# checks the result against NVIDIA's own modules in nvidia-l4t-kernel:
# same vermagic as cp210x.ko, its one dependency (usbserial) present, and no
# ch341 of NVIDIA's own. The package installs it to
# /lib/modules/KERNEL_RELEASE/updates/ch341.ko, runs depmod on install and
# removal, and depends on exactly that nvidia-l4t-kernel, so a kernel update
# shows up as an apt conflict instead of a silently missing driver.
# The module is not signed: it loads only while module signature enforcement
# (and Secure Boot with lockdown) stays off, as on a stock JetPack install.
# The source goes into /usr/share/doc/brainsyncos-ch341-tegra/ (GPL-2.0).

BSOS_SCRIPT=brainsyncos-ch341-tegra
source "$BRAINSYNCOS_ROOT/scripts/lib.sh"
load_config
load_packages_config
load_env_file package.env "PKG_NAME KERNEL_RELEASE"
require_cmd xorriso dpkg-deb make gcc modinfo strip file
kver="$KERNEL_RELEASE"
here="$(pwd -P)"

s="$(scratch_dir ch341)"
trap 'force_rm_tree "$s"' EXIT
iso="$(resolve_base_iso)"

# pool_deb PACKAGE DEST: PACKAGE_TEGRA_KERNEL_arm64.deb from the base ISO's pool.
pool_deb() {
    local p="/pool/main/n/$1/$1_${TEGRA_KERNEL}_arm64.deb"
    xorriso -osirrox on -indev "$iso" -extract "$p" "$2" > /dev/null 2>&1 || die "cannot extract $p from $iso"
    [ "$(dpkg-deb -f "$2" Package Version | tr '\n' ' ')" = "Package: $1 Version: $TEGRA_KERNEL " ] \
        || die "$p is not $1 $TEGRA_KERNEL"
}

pool_deb nvidia-l4t-kernel-headers "$s/headers.deb"
dpkg-deb -x "$s/headers.deb" "$s/hdr"
rel="$(find "$s/hdr/usr/src" -path '*/include/config/kernel.release' | head -n 1)"
[ -n "$rel" ] || die "nvidia-l4t-kernel-headers has no include/config/kernel.release"
kdir="$(dirname "$(dirname "$(dirname "$rel")")")"
[ "$(cat "$rel")" = "$kver" ] || die "the headers are for $(cat "$rel"), package.env says $kver"
[ -f "$kdir/Module.symvers" ] && [ -x "$kdir/scripts/mod/modpost" ] || die "no Module.symvers or modpost in $kdir"
grep -qx '# CONFIG_USB_SERIAL_CH341 is not set' "$kdir/.config" \
    || die "this kernel's .config does not leave CONFIG_USB_SERIAL_CH341 unset; ship NVIDIA's driver instead"
grep -qx 'CONFIG_USB_SERIAL=m' "$kdir/.config" || die "CONFIG_USB_SERIAL is not a module in this kernel"
log "headers: ${kdir#$s/hdr} ($kver)"

pool_deb nvidia-l4t-kernel "$s/kernel.deb"
dpkg-deb -x "$s/kernel.deb" "$s/kernel"
mods="$s/kernel/lib/modules/$kver"
[ -f "$mods/kernel/drivers/usb/serial/usbserial.ko" ] || die "nvidia-l4t-kernel has no usbserial.ko under /lib/modules/$kver"
[ -z "$(find "$mods" -name 'ch341.ko*')" ] && ! grep -q 'ch341' "$mods/modules.builtin" \
    || die "nvidia-l4t-kernel already has ch341; this package is not needed"

mkdir -p "$s/m"
cp "$here/ch341.c" "$here/Kbuild" "$s/m/"
t0="$(date +%s)"
make -C "$kdir" M="$s/m" modules > "$s/make.log" 2>&1 || { tail -30 "$s/make.log" >&2; die "module build failed"; }
grep -i 'warning' "$s/make.log" | sed 's/^/  /' >&2 || true
ko="$s/m/ch341.ko"
strip --strip-debug "$ko"
! grep -q "$s" "$ko" || die "ch341.ko contains the build path"
log "built ch341.ko in $(( $(date +%s) - t0 )) s: $(file_size "$ko") bytes"

want="$(modinfo -F vermagic "$mods/kernel/drivers/usb/serial/cp210x.ko")"
got="$(modinfo -F vermagic "$ko")"
[ "$got" = "$want" ] || die "ch341.ko vermagic '$got' differs from NVIDIA's cp210x.ko '$want'"
[ "$(modinfo -F depends "$ko")" = usbserial ] || die "ch341.ko depends on '$(modinfo -F depends "$ko")', want usbserial"
modinfo -F alias "$ko" | grep -qx 'usb:v1A86p7523d\*dc\*dsc\*dp\*ic\*isc\*ip\*in\*' || die "ch341.ko does not claim 1a86:7523 (CH340)"
file -b "$ko" | grep -q '^ELF 64-bit LSB relocatable, ARM aarch64' || die "ch341.ko is not an aarch64 module: $(file -b "$ko")"
log "vermagic $got (as cp210x.ko), depends usbserial, $(modinfo -F alias "$ko" | wc -l | tr -d ' ') USB aliases"

p="$(scratch_dir ch341-stage)"
trap 'force_rm_tree "$s"; force_rm_tree "$p"' EXIT
doc="$p/usr/share/doc/brainsyncos-ch341-tegra"
mkdir -p "$p/DEBIAN" "$p/lib/modules/$kver/updates" "$doc"
install -m 644 "$ko" "$p/lib/modules/$kver/updates/ch341.ko"
install -m 644 "$here/ch341.c" "$here/Kbuild" "$doc/"
cat > "$doc/README" <<EOF
ch341.ko, the CH340/CH341 USB-serial driver from Linux 6.8.12 (ch341.c here,
GPL-2.0), built out of tree for NVIDIA's L4T kernel $kver
(nvidia-l4t-kernel $TEGRA_KERNEL), which does not include it.
It gives V1 Cerelog X8 boards (CH340, USB vendor 1a86) a /dev/ttyUSB* port.

Built by BrainSyncOS (packages/brainsyncos-ch341-tegra) with
  make -C <nvidia-l4t-kernel-headers>/.../linux-noble M=<this folder> modules
vermagic: $got

The module is not signed. It loads while module signature enforcement is
off (CONFIG_MODULE_SIG_FORCE unset, no lockdown), as on stock JetPack; the
kernel then marks itself tainted. With Secure Boot and enforced signatures it
is refused until it is signed with an enrolled key.
EOF
chmod 644 "$doc/README"
cat > "$doc/copyright" <<EOF
ch341.c (Linux 6.8.12, drivers/usb/serial/ch341.c):
Copyright 2007 Frank A Kingswood, 2007 Werner Cornelius, 2009 Boris Hajduk.
License: GPL-2.0 (/usr/share/common-licenses/GPL-2).
EOF
chmod 644 "$doc/copyright"
cat > "$p/DEBIAN/postinst" <<EOF
#!/bin/sh
# brainsyncos-ch341-tegra: index the module for kernel $kver.
set -e
if [ "\$1" = configure ] && [ -d /lib/modules/$kver/kernel ]; then
    depmod -a $kver
fi
EOF
cat > "$p/DEBIAN/postrm" <<EOF
#!/bin/sh
# brainsyncos-ch341-tegra: drop the module from kernel $kver's index.
set -e
case "\$1" in
    remove|purge)
        if [ -d /lib/modules/$kver/kernel ]; then
            depmod -a $kver
        fi
        ;;
esac
EOF
chmod 755 "$p/DEBIAN/postinst" "$p/DEBIAN/postrm"
cat > "$p/DEBIAN/control" <<EOF
Package: brainsyncos-ch341-tegra
Version: ${BSOS_PKG_VERSION:?}
Architecture: arm64
Maintainer: BrainSyncOS <$PACKAGE_MAINTAINER_EMAIL>
Depends: nvidia-l4t-kernel (= $TEGRA_KERNEL), kmod
Section: kernel
Priority: optional
Description: CH340 USB-serial driver for NVIDIA's L4T kernel
 ch341.ko from Linux 6.8.12 for kernel $kver
 (nvidia-l4t-kernel $TEGRA_KERNEL), which lacks it, so V1 Cerelog X8
 EEG boards (CH340 bridge) appear as /dev/ttyUSB*. Not signed.
EOF
build_deb "$p" "${BSOS_PKG_OUT:?}" > /dev/null
