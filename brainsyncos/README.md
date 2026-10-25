# BrainSyncOS

BrainSyncOS is an appliance OS for NVIDIA Jetson. A Jetson installed from it
boots straight into **BrainSync**, the SSVEP brain-computer interface: a Cerelog
X8 EEG board over USB serial, a frame-locked PsychoPy flicker stimulus, LSL
markers, and CCA/FBCCA/TRCA decoders.

BrainSyncOS is not built from scratch. It is a remaster of NVIDIA's official
Jetson ISO installer for JetPack 7.2.1 / Jetson Linux R39.2.1. That ISO is
Ubuntu Server 24.04.3 with the subiquity installer and L4T added. We keep
NVIDIA's boot chain and partition logic untouched and add BrainSync on top.

Phase 1 delivers five things:
- a pinned, verified base ISO
- an arm64 builder container (base image pinned by digest; apt packages are
  not snapshot-pinned, see below)
- an idempotent extraction of the ISO
- an inventory report, tracked at [`docs/base-iso-inventory.md`](docs/base-iso-inventory.md)
- a repack pipeline that is proven by a no-op roundtrip

Phase 2 builds the first BrainSyncOS installer ISO for the **Jetson Orin
Nano** (`scripts/build-iso.sh`):
- a BrainSyncOS boot menu (NVMe / microSD / USB), with NVIDIA's kernel command lines unchanged
- a BrainSync install hook in NVIDIA's autoinstall, which runs `/brainsyncos/install.d/*.sh` inside the freshly installed system
- install steps for `/etc/brainsyncos-release` and a BrainSyncOS line on the console login screen
- a macOS USB writer (`scripts/write-usb.sh`)

Phase 2.5 gives the installed device its appliance identity, so it boots
without NVIDIA's first-boot setup (OOBE):
- hostname `brainsync-<serial suffix>`, reachable as `brainsync-<suffix>.local`
- a `brainsync` account whose logins come from the build: SSH keys and/or a console password
- key-only SSH
- NVIDIA's OOBE skipped

The **VM edition** (`scripts/build-vm-iso.sh`) is a second build target for
development without a Jetson: stock Ubuntu Server 24.04 arm64, remastered so
that UTM on an Apple Silicon Mac installs it unattended with the same install
hook, account, SSH policy and hostname scheme. See
[VM edition (UTM on Apple Silicon)](#vm-edition-utm-on-apple-silicon).

Phase 3 installs BrainSync itself. It is being delivered in parts. Part 3a,
done, is the delivery channel:
- one build command for a fresh clone (`scripts/build.sh`, or `make`)
- builder containers capped at 2 CPUs by default (`config/build.env`)
- a package build (`packages/`)
- a signed apt repository, shipped on every ISO at `/brainsyncos-repo`
- the first install step from it, `60-apt-repo.sh`

The BrainSync apps, BrainFlow, llama.cpp and CUDA come next, through the same
repository. See [Building BrainSyncOS](#building-brainsyncos) and
[The package repository (Phase 3)](#the-package-repository-phase-3).

## Base image (user-supplied)

| | |
|---|---|
| file | `jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso` |
| sha256 | `f31cf35023cd072707dfc46ef23ed71318449516333055fe3bb9455a615395d0` |
| size | 5,035,601,920 bytes |
| volume id | `jetsoninstaller-r39.2.1` |
| contents | Ubuntu-Server 24.04.3 (20250806.1), L4T 39.2.1, target kernel `nvidia-l4t-kernel 6.8.12-tegra-39.2.1-20260806224157` |
| boards | Jetson AGX Thor, AGX Orin, Orin NX/Nano (UEFI arm64 only) |

`config/base-iso.env` is the single source of truth for these values. It is
plain `KEY=value`, so it can be sourced by bash or passed to
`docker run --env-file`.

You download the ISO yourself from NVIDIA's JetPack 7.2.1 page. **It is never
committed, copied into the repo or modified.** The builder mounts the
directory that holds it read-only at `/in`. The scripts find the ISO at
`$BASE_ISO` if that is set, otherwise at `~/Downloads/<name>`.

## Host requirements

- **Docker** with arm64 support:
  - **Docker Desktop on Apple Silicon** runs it natively. This is the reference host.
  - **arm64 Linux** runs it natively.
  - **x86_64 Linux** works for Phase 1, but you must register qemu-user-static/binfmt with Docker (`docker run --privileged --rm tonistiigi/binfmt --install arm64`). Later phases chroot into the arm64 rootfs and are much slower under emulation.
- **Builder pinning:** `docker/Dockerfile.builder` pins `ubuntu:24.04` by digest, but apt installs whatever noble/noble-updates currently ships. snapshot.ubuntu.com returns 401 for `ubuntu-ports`, so arm64 cannot be snapshot-pinned. The installed versions (with `xorriso -version`) are recorded in `build/meta/builder-packages.txt` on every extraction.
- **Linux hosts:** the container runs as root. With rootful Docker, everything under `build/` ends up owned by root, and the host-side test steps that write there fail. Use rootless Docker or Docker Desktop for Linux (both map container root to your user), or run `sudo chown -R "$(id -u):$(id -g)" build` after each step. macOS Docker Desktop is not affected.
- **CPU:** every builder container is capped by `config/build.env`. The defaults are 2 CPUs, 2 compiler jobs, 6 GiB of memory and a 3 GiB `/scratch`. Builds get slower (the Jetson ISO takes about 4 minutes instead of 1), not different. Override one run with `BSOS_BUILD_CPUS=6 scripts/build.sh ...` (and `BSOS_BUILD_JOBS`, `BSOS_BUILD_MEMORY`, `BSOS_BUILD_SCRATCH_SIZE`).
- **A filesystem that keeps file modes** for the checkout. Windows drives under WSL (`/mnt/c`) ignore `chmod`, so the repository key would be readable by everyone; clone under `~` instead. `scripts/make-repo-key.sh` refuses to write the key on such a filesystem, and `scripts/build.sh doctor` reports it.
- **bash 3.2 or newer** on the host. Host-side scripts stay compatible with macOS `/bin/bash`. All heavy tools run in the container: xorriso, squashfs-tools, mtools, sfdisk, gpgv and others.
- **Disk:**
  - `build/iso-root` takes about 4.2 GiB.
  - Each output ISO takes about 4.7 GiB (Jetson) or 3.6 GiB (VM), plus its package repository.
  - The product ISO builds compute what they need: the base ISO plus the package repository plus 2 GiB, so about 7 GiB for the Jetson ISO today. `scripts/build.sh doctor` reports it. The Phase 1 tools (`extract-iso.sh`, `repack-iso.sh`) still refuse to start below `MIN_FREE_GB=20` GiB.
  - Never keep more than one extraction.
  - **Docker Desktop on macOS caveat:** the Docker VM keeps host file descriptors open for files it has accessed through the bind mount, even after they are deleted on either side. A plain `rm` of a 5 GB ISO therefore frees nothing until Docker restarts (check with `lsof +L1`). The scripts truncate files before deleting them (`rm_released` and `force_rm_tree` in `scripts/lib.sh`). Clean up with `scripts/builder.sh clean [--all]`, not with `rm -rf build`. If space has already gone missing, either restart Docker Desktop or run `docker run --rm --privileged brainsyncos-builder:latest sh -c 'sync; echo 2 > /proc/sys/vm/drop_caches'`.

## Building BrainSyncOS

From a fresh clone, with Docker running:

```sh
cd brainsyncos
export BASE_ISO=/path/to/jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso  # if not in ~/Downloads
cp ~/.ssh/id_ed25519.pub config/authorized_keys     # logins for the brainsync account (see Phase 2)

scripts/build.sh doctor                # what is missing, and how to fix it
scripts/build.sh key                   # once: the package repository signing key (config/, gitignored)
scripts/build.sh all                   # packages -> signed repository -> build/out/brainsyncos-0.1.0-orin-nano.iso
scripts/build.sh all --edition vm      # the UTM edition (downloads Ubuntu's ISO first)
scripts/build.sh test phase3           # tests: phase1 phase2 phase3 vm
```

`make`, `make EDITION=vm`, `make doctor`, `make test TESTS="phase2 vm"` and so on do the same (see `Makefile`).

The steps of `all` also run one at a time:
- `packages` builds the edition's packages into `build/debs/EDITION/`.
- `wheels` (not part of `all` yet) builds the Python wheels into `build/wheels/`. It is the same for both editions and skips a wheel that is up to date.
- `repo` signs them into `build/repo/EDITION/`.
- `iso` writes the installer ISO. Its options go to `build-iso.sh` / `build-vm-iso.sh`, e.g. `scripts/build.sh iso --name test`.

**Back up `config/repo-signing-key.asc`.** An installed device trusts only the key its ISO was signed with. Updates for it (Phase 6) can only be signed with that same key.

## Quickstart (Phase 1)

```sh
cd brainsyncos
export BASE_ISO=/path/to/jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso  # if not in ~/Downloads

scripts/builder.sh build                              # arm64 builder image (tag = Dockerfile hash)
scripts/verify-base-iso.sh                            # host-side check (shasum/bsdtar), or:
scripts/builder.sh run scripts/verify-base-iso.sh --no-cache
scripts/builder.sh run scripts/extract-iso.sh         # -> build/iso-root (no-op when up to date)
scripts/builder.sh run scripts/inspect-iso.sh         # -> build/reports/ + docs/base-iso-inventory.md
scripts/builder.sh run scripts/repack-iso.sh          # -> build/out/brainsyncos-l4t-39.2.1.iso (--expect-noop for a roundtrip)
scripts/builder.sh run scripts/compare-iso.sh build/out/brainsyncos-l4t-39.2.1.iso
scripts/builder.sh clean                              # drop build/out

tests/test_phase1.sh                                  # the full chain with PASS/FAIL per check (~2 min)
```

Every script accepts `--help`. `builder.sh run` starts the container with
`--init` (tini, which signals the whole process group), so Ctrl-C stops an
in-container job even when output is piped or redirected.

When committing, add only `brainsyncos/` and `.gitignore`. The root
`.gitignore` also excludes the tool-local `.claude/` and `.dmux/` directories.

## Quickstart (Phase 2): build and install BrainSyncOS

```sh
cd brainsyncos
export BASE_ISO=/path/to/jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso

# Logins for the brainsync account (at least one; both files are gitignored):
ssh-keygen -t ed25519                                 # if you have no key yet (~/.ssh/id_ed25519.pub)
cp ~/.ssh/id_ed25519.pub config/authorized_keys       # SSH key(s), one per line
scripts/make-password-hash.sh                         # optional console password -> config/brainsync-password.hash

scripts/build.sh key                                  # once: the package repository signing key
scripts/build.sh all                                  # packages + repository + build/out/brainsyncos-0.1.0-orin-nano.iso (~5 min)
tests/test_phase2.sh                                  # a separate test ISO with throwaway keys, 16 checks (~6 min)

scripts/write-usb.sh                                  # list external disks
scripts/write-usb.sh build/out/brainsyncos-0.1.0-orin-nano.iso /dev/diskN   # ERASES diskN
```

Product settings live in `config/brainsyncos.env`:
- `BRAINSYNCOS_VERSION`
- `TARGET_BOARD`
- `HOOKED_AUTOINSTALL`: which NVIDIA autoinstall files get the hook
- `INSTALL_TARGET_DEFAULT`: `nvme`, `microsd` or `usb`, the boot-menu entry listed first
- `GRUB_TIMEOUT`
- `BRAINSYNC_USER`: the appliance account, default `brainsync`
- `HOSTNAME_PREFIX`: default `brainsync`

`GRUB_TIMEOUT=-1` (the default) waits at the boot menu for a key press, as NVIDIA's ISO does. A value of 0 or more makes the stick install unattended. Booting it then **erases the default disk with nobody at the keyboard**, so only set it for flashing several units in a row.

The build id comes from the git revision of `brainsyncos/`, with `-dirty` appended for uncommitted changes. It ends up in the boot menu, on the ISO in `/brainsyncos/release`, and on the installed system in `/etc/brainsyncos-release`.

## Installing on a Jetson Orin Nano

1. **Firmware.** The board needs JetPack 6-era UEFI firmware (36.x) or newer before the r39 ISO can update it. A factory Orin Nano dev kit may need NVIDIA's JetPack 6 update path first; see the Orin Nano "update firmware" page. The ISO then updates QSPI itself with a UEFI capsule.
2. **Write the stick.** Use `scripts/write-usb.sh` or balenaEtcher.
3. **Boot it.**
   1. Connect HDMI or DP, a keyboard and Ethernet. For the serial console, `sudo screen /dev/cu.usbmodem* 115200` on macOS.
   2. Plug in the stick and power on. Press **Esc** at the NVIDIA logo, then pick the USB stick in the UEFI boot manager.
4. **Pick the entry** for your disk: **Install BrainSyncOS … on NVMe SSD** or **… on microSD card**. The install erases that disk.
5. **When the installer offers the firmware capsule update, press `Y` within about 30 s.** Skipping it breaks the install (NVIDIA known issue 6266271). The board reboots twice for the capsule, then the install continues for about 15 minutes. The second phase shows `BrainSyncOS - installing the BrainSync layer` just before `Step 13/13`.
6. **First boot.** Remove the stick. There is no OOBE: the board boots to the GNOME login screen with the `brainsync` account. Log in at the console with the console password if you built one, or over Ethernet with SSH: `ssh brainsync@brainsync-<last 6 of the serial>.local`. The console login screen (HDMI text console or serial) shows `BrainSyncOS 0.1.0 (build …)` and, on Ubuntu's line, the hostname. Otherwise find the board's IP in your router's DHCP list.
7. **Check the install:**

   ```sh
   cat /etc/brainsyncos-release            # VERSION, BUILD_ID, BASE_ISO_SHA256, INSTALLED=...
   hostname                                # brainsync-xxxxxx
   id                                      # brainsync, in sudo dialout video render gpio i2c ...
   systemctl get-default                   # graphical.target (not nv-oobe.target)
   cat /var/log/brainsyncos/install.log    # ends with "hook.sh: exit 0"
   cat /etc/nv_tegra_release               # R39 (release), REVISION: 2.1
   ```

If the hook fails, the installer stops with an error, on purpose. The log is in `/var/log/brainsyncos/install.log` on the live system (open a shell from the installer's error screen) and on the target disk.

**The hook only runs on NVIDIA's Orin autoinstall path** (`jetsoniso_orin-ai.yaml`). If `preseed.sh` cannot detect the board or the boot disk (for example, no NVMe drive behind the NVMe entry), or `run-subiquity`/`force-ai=`/`force-platform=` is on the kernel command line, it falls back to the interactive stock Ubuntu installer. That installs **without** BrainSync and without NVIDIA's L4T steps. If `/etc/brainsyncos-release` is missing after an install, that is what happened; check the disk and use the matching menu entry.

## VM edition (UTM on Apple Silicon)

A second build target for working on BrainSyncOS without a Jetson: the BrainSync
app, the kiosk session and new `install.d` steps can be developed and tested in a
UTM VM on the Mac, with the same `brainsync` account, key-only SSH, hostname
scheme and `/etc/brainsyncos-release` as the device.

**What it is:** stock **Ubuntu Server 24.04.5 arm64** (`config/vm-base-iso.env`),
installed unattended by Ubuntu's own installer, then the same `payload/hook.sh`
and `install.d/` steps as on the Jetson.
**What it is not:**
- no L4T, no CUDA/TensorRT, no NVIDIA services or OOBE, no Jetson kernel
- the PsychoPy flicker timing is **not** representative: a virtual display has
  no real vsync, so frame-locked stimulus timing must be validated on the device
- the Cerelog EEG board only reaches the VM through USB passthrough, which on
  macOS 26 needs the **QEMU backend** (see below)

### Build it

```sh
cd brainsyncos
scripts/fetch-vm-base.sh                          # once: 3.6 GB into build/downloads/, sha256 + signed SHA256SUMS
scripts/build.sh all --edition vm                 # packages + repository + build/out/brainsyncos-0.1.0-vm-arm64.iso (~4 min)
tests/test_vm.sh                                  # 19 checks with throwaway keys (~11 min at 2 CPUs)
```

- `fetch-vm-base.sh` resumes an interrupted download (`.part`), checks the size
  and sha256 pins, requires the pin to be the value in Canonical's `SHA256SUMS`, and
  checks that file's signature with `gpgv` in the builder (the Ubuntu archive keyring
  ships in the builder image; `--no-signature` skips it). A verified file is
  stamped and never downloaded or hashed again. A mismatch is fatal; an existing
  file is never deleted unless you pass `--force`.
- `build-vm-iso.sh` takes the same login options as `build-iso.sh`
  (`--authorized-keys`, `--password-hash`, `--name`, `--build-id`); by default your
  `config/authorized_keys` and `config/brainsync-password.hash`.
- Settings: `config/vm.env` (`EDITION=vm-arm64`, `VM_GRUB_TIMEOUT`, `VM_DESKTOP`, `VM_INSTALL_KERNEL`
  ga|hwe, `VM_CONSOLES`, and the UTM defaults: `VM_NAME`, `VM_BACKEND`, memory,
  CPUs, disk). Version, account and hostname prefix come from `config/brainsyncos.env`,
  shared with the Jetson edition.

### Run it in UTM

`scripts/utm-vm.sh` drives UTM 4.7 by AppleScript. It only ever touches VMs whose
names start with `BrainSyncOS`.

```sh
scripts/utm-vm.sh check                 # UTM found, Apple Events allowed (first run: answer OK)
scripts/utm-vm.sh create                # VM "BrainSyncOS": Apple Virtualization, 4 GiB, 4 CPUs, 32 GiB, installer drive
scripts/utm-vm.sh install               # boot -> pick "Install BrainSyncOS ..." -> waits -> ejects installer -> boots -> prints ssh
ssh brainsync@<ip>                      # or: scripts/utm-vm.sh ssh-info
scripts/utm-vm.sh stop | start | status | ip | eject-iso | serial-off | list
scripts/utm-vm.sh delete                # asks you to type the VM name
scripts/utm-vm.sh create --backend qemu --name BrainSyncOS-usb   # for USB passthrough
```

- **The installer is an imported drive, not a removable CD.** Sandboxed UTM opens a
  removable drive's file only when you picked it in UTM's own file dialog; one set by
  script fails at start with "Cannot access resource". So `create` clones the ISO
  (APFS clone, no extra disk space) into UTM's container and UTM imports it into the
  VM bundle as `brainsyncos-installer.iso`. `eject-iso` (and `install`, after the
  install) removes that drive, recognised by its file name in the bundle's
  `config.plist`, and UTM deletes the file. `utmctl start` exits 0 even when UTM
  refuses to start a VM, so `start` and `install` check that the VM is running and
  print UTM's message if not.
- **The install needs network access.** UTM's default shared network provides it.
  `avahi-daemon`, `libnss-mdns` and `qemu-guest-agent` are not on the ISO, and the
  security updates are downloaded during the install.
- **Desktop:** `VM_DESKTOP=minimal` (default) installs `ubuntu-desktop-minimal`, GNOME
  as on the Jetson, so the VM boots to a graphical login (`full` = `ubuntu-desktop`,
  `none` = server only). It is downloaded during the install (about 1.5 GB) and
  added by `scripts/render-vm-autoinstall.py` as the first entry of the
  autoinstall's `packages`, so the BrainSyncOS hook runs on the desktop system.
  On a VM installed with `none`: `sudo apt install ubuntu-desktop-minimal && sudo reboot`.
- The boot menu starts its first entry, **Install BrainSyncOS 0.1.0 (VM, erases
  the virtual disk)**, after 10 seconds (`VM_GRUB_TIMEOUT`; `-1` waits for a key).
  It installs unattended (about 5 to 20 minutes) and then **powers the VM off**. It does not reboot, because the ISO
  is still attached and would install again. `install` waits for that power-off,
  ejects the ISO, starts the VM from its disk and waits for SSH. It reads the serial
  console (`build/vm/NAME-install.log`): the last late-command prints
  `BRAINSYNCOS-INSTALL-OK` there and `error-commands` print `BRAINSYNCOS-INSTALL-FAILED`.
  A power-off without the success marker (stopped at the boot menu, window closed,
  crash) keeps the ISO attached so `install` can simply run again; a failure marker
  ends the wait at once (subiquity stays at its error screen and does not power off).
  A captured but empty console means the installer never started (left in the
  boot menu): the ISO stays. Only when the console cannot be captured at all does
  it fall back to "ran at least 5 minutes". `install --hide` is refused while the
  boot menu waits for a key (`VM_GRUB_TIMEOUT=-1`).
- **Serial port (apple backend):** UTM's serial port there is a PTY that something
  on the Mac must keep reading. Once its buffer (about 16 KB) is full, every console
  write in the VM blocks, and a boot nobody reads hangs at *EFI stub: Booting Linux
  Kernel...*. `install` reads it while it waits and removes the port after a
  successful install, so the installed system never depends on it. For a VM
  installed before that change, shut it down and run `scripts/utm-vm.sh
  serial-off`; until then, `cat` of its PTY (`address of serial port 1` in UTM's
  scripting) unblocks a hung boot. The qemu backend's TCP console drops output
  nobody reads, and keeps its port.
- **Keyboard and mouse:** UTM creates an Apple-backend VM made by script with
  keyboard and pointer *Disabled*, and its scripting cannot change them. `create`
  therefore quits UTM (only when no VM is running), sets `Keyboard=Generic` and
  `Pointer=Mouse` in the bundle's `config.plist`, and starts UTM again.
  *Advanced options*
  has the same install with the HWE kernel, plus Ubuntu's stock installer entries
  unchanged (no BrainSyncOS).
- **Logging in:** `ssh brainsync@<ip>` with the build's key (or the console password,
  if you built one). The VM announces `brainsync-<suffix>.local` over mDNS. The
  console login screen shows `BrainSyncOS 0.1.0 VM edition (build …), IP <address>`.
  `utm-vm.sh ip` finds the address in macOS's DHCP leases (`/var/db/dhcpd_leases`,
  newest lease wins): the NIC has a fixed MAC derived from the VM name, and the
  autoinstall's `network` section sets `dhcp-identifier: mac` on the installer and
  the installed system (netplan's default, a machine-id DUID, would be recorded
  instead of the MAC). Else it uses the host's ARP cache, and on QEMU the guest agent.
- AppleScript and `utmctl` calls time out after 120 s. A timeout usually means macOS
  is still asking whether your terminal may control UTM: answer OK.
- **Backends:**
  - `apple` (default): Apple Virtualization, fast, serial console `hvc0`.
  - `qemu`: QEMU with Hypervisor.framework. Serial console `ttyAMA0` on a local TCP
    port (`utm-vm.sh create` prints it). Has the guest agent (`utmctl ip-address`) and
    USB sharing.
- **EEG board (USB passthrough):** QEMU backend only on macOS 26 (Apple
  Virtualization gets USB in macOS 27). UTM 4.7 cannot enable it by script:
  stop the VM, then *Edit → Input → USB Sharing* (USB 3.0), start it, and run
  `utmctl usb list` / `utmctl usb connect BrainSyncOS-usb <VID:PID>`. Untested with
  the real board: macOS may keep its own USB-serial driver on the device.
- **Manual GUI fallback** (no automation permission): UTM → *Create a New Virtual
  Machine* → *Virtualize* → *Linux*. Pick *Use Apple Virtualization* (or leave it
  off for QEMU). Boot ISO image: `build/out/brainsyncos-0.1.0-vm-arm64.iso`. 4096 MiB,
  4 cores, 32 GiB disk, shared network. Start it and pick the install entry. When the
  VM has powered off, clear the ISO (*CD/DVD → Clear*) and start it again.
- To reinstall, `delete` and `create` again (`install` refuses a VM without an
  installer drive). After rebuilding the ISO, recreate the VM: the VM bundle holds
  its own clone of the ISO it was created with.

### How the VM ISO differs from Ubuntu's

`build-vm-iso.sh` rebuilds nothing. It replays Ubuntu's boot setup (`-boot_image any
replay`: MBR, El Torito EFI entry, the 7 MiB ESP with shim + signed GRUB, byte for
byte) and maps three paths over it:

| ISO path | Change |
|---|---|
| `/brainsyncos/` | the payload (`hook.sh`, `install.d/`), `release` (`EDITION=vm-arm64`), `settings.env`, `config/` logins, and `autoinstall.yaml` (from `vm/autoinstall.yaml`) |
| `/boot/grub/grub.cfg` | the BrainSyncOS menu (`scripts/render-vm-grub.py`) |
| `/md5sum.txt` | Ubuntu's entries, in Ubuntu's (not sorted) order, plus the staged files |

- **The autoinstall is only used from our menu entries.** Their kernel line is
  Ubuntu's plus `autoinstall subiquity.autoinstallpath=/cdrom/brainsyncos/autoinstall.yaml`
  before `---`. `autoinstall` skips subiquity's "Continue with autoinstall?" prompt.
  After `---` (which the installed system's kernel line inherits) come
  `console=ttyAMA0 console=hvc0 console=tty0`. Nothing goes to `/cdrom/autoinstall.yaml`,
  where subiquity would pick it up for every entry, so the stock entries stay stock.
- **The autoinstall** (`vm/autoinstall.yaml`; checked at build time by
  `scripts/check-autoinstall.py`):
  - **storage:** `direct` layout on the one disk.
  - **ssh:** `install-server: true`, `allow-pw: false`.
  - **packages:** `openssh-server`, `avahi-daemon`, `libnss-mdns`, `qemu-guest-agent`.
  - **updates:** `security`.
  - **network:** DHCP on `en*` with `dhcp-identifier: mac` (see Logging in).
  - **late-commands:** `/bin/bash /cdrom/brainsyncos/hook.sh` first; last, the
    `BRAINSYNCOS-INSTALL-OK` console marker (assembled at run time, so a console
    that echoes the command line does not show it). **error-commands:** the
    `BRAINSYNCOS-INSTALL-FAILED` marker.
  - **shutdown:** `poweroff`.
  - **No `identity` section:** the account comes from `30-user.sh`, as on the Jetson.
    Subiquity accepts a `user-data` section instead. `users: []` stops cloud-init
    from creating Ubuntu's default `ubuntu` user on first boot, and
    `preserve_hostname: true` keeps our hostname.
- **Boot structure:**
  - ISO9660 and Joliet names are kept exactly, with
    `ISO_COMPLIANCE=iso_9660_level=3:joliet_long_names:allow_dir_id_ext:joliet_long_paths`.
    That is Jetson's rule set without `lowercase`, found by trial as in Phase 1.
  - Ubuntu's MBR uses cylinder alignment (228 heads × 32 sectors). The new image
    is one cylinder larger, so the ESP now ends past cylinder 1023, and xorriso can
    no longer re-derive the geometry from the saturated CHS fields. `compare-iso.sh
    --edition vm` therefore checks the CHS fields and the ESP's cylinder alignment
    directly (`chs`), with Ubuntu's own MBR as a control.

**Shared with the Jetson edition, made edition-safe:**
- `hook.sh` reads `EDITION` from the release file. A Jetson keeps its device-tree
  identity, exactly as before. A VM has no device tree (UEFI + ACPI), so the model
  comes from DMI, and the serial is the first usable of `product_serial`,
  `product_uuid` (UTM's VM UUID on QEMU) and the live system's `machine-id`.
  `device.env` records `BSOS_EDITION`, `BSOS_MODEL` and `BSOS_ID_SOURCE` as well.
  The "built for Jetson Orin Nano" warning only applies to the Jetson edition.
- `00-release.sh` requires a known `EDITION`. `10-issue.sh` shows `VM edition` and
  the IP (agetty's `\4`) on the VM.
- `50-skip-oobe.sh` only switches to `graphical.target` when the default target is
  NVIDIA's `nv-oobe.target`. Otherwise it leaves the default alone, so a server VM
  keeps its own.
- The groups in `30-user.sh` are filtered to those that exist (no `gpio`/`i2c` on a VM).

**Tested without UTM** (`tests/test_vm.sh`, 19 checks; the in-container halves are
in `tests/vm-checks.sh`):
- **Configs and fetch:** config validation, both configs in macOS bash 3.2, fetch
  idempotency and wrong-pin refusal, base verification.
- **Generators:** the boot menu and md5 generators on Ubuntu's real files.
- **The autoinstall:** checked against the JSON schema that this ISO's own subiquity
  (snap rev 7426) generates, plus seventeen broken variants that must be rejected. Its
  `user-data` passes the target's own `cloud-init schema`.
- **Login checks:** `build-vm-iso.sh` refuses bad or missing logins.
- **`utm-vm.sh` without a VM:** name guard, `--dry-run` for both backends, and all
  AppleScripts compiled against UTM's dictionary. With stub `osascript`/`utmctl`/`arp`:
  captured AppleScript results come back at once (not after the 120 s watchdog),
  the address lookup (newest `1,MAC` lease, DUID leases ignored, ARP fallback), the
  timeout and `-1743` messages, and `install` refusing `--hide`, an immediate
  power-off, a power-off without the success marker, and stopping at the failure
  marker, each without ejecting the ISO. `create` hands UTM a clone staged in UTM's
  container and removes it afterwards, `eject-iso` drops the drive whose bundle
  file is `brainsyncos-installer.iso` (not the disk), and `start` fails with UTM's
  message when utmctl exits 0 but the VM stays stopped.
- **The built ISO:**
  - its boot structure against Ubuntu's
  - its contents, including every casper layer against Ubuntu's signed `casper/SHA256SUMS`
- **The hook:** run against Ubuntu's real target layers.
  - `ubuntu-server-minimal` and `ubuntu-server` are merged the way curtin copies
    the overlay (163 whiteouts).
  - `openssh-server` is `dpkg -i`'d from the ISO pool (it is not in the layers).
  - A fake DMI supplies the model and serial, and there is no device tree.
  - It covers: the release, issue and hostname; the serial fallbacks; the account;
    Ubuntu's sshd effective config (`sshd -T`); the default target left alone; no
    Jetson warning.
  - The autoinstall's `network` section rendered by the target's own `netplan
    generate` gives `Name=en*` with `ClientIdentifier=mac`.
- **The boot chain:** `tests/boot-smoke.py` boots the ISO under plain QEMU (UEFI,
  edk2) to the BrainSyncOS menu, presses Enter, and checks the kernel command line
  and the start of the initramfs.

**End to end in UTM 4.7.4 (macOS 26.6, M4), done by hand, not part of `test_vm.sh`:**
a test ISO (throwaway key, test-only `VM_GRUB_TIMEOUT=5` via
`BSOS_VM_PRODUCT_CONFIG`) went through `utm-vm.sh create` + `install` on both backends.
Each run: unattended install in about 5 minutes, `BRAINSYNCOS-INSTALL-OK` read from
the serial console (Apple PTY `hvc0`, QEMU TCP `ttyAMA0`), installer drive ejected,
first boot, SSH with the key. The guest shows `EDITION=vm-arm64`, a
`brainsync-<suffix>` hostname (Apple: DMI `product_serial`; QEMU: DMI
`product_uuid`), passwordless sudo, `sshd -T` with `passwordauthentication no`,
`permitrootlogin no` and `allowusers brainsync`, no `ubuntu`/`nvidia` user, `hook.sh:
exit 0` in `/var/log/brainsyncos/install.log`, and no failed units. Root logins and
password logins are refused (`Permission denied (publickey)`). The DHCP lease is
recorded as `1,MAC`, so `utm-vm.sh ip` works; `NAME.local` resolves over mDNS; on
QEMU `utmctl ip-address` works through the guest agent. Bugs this found and fixed in
`utm-vm.sh`:
- `POSIX file` inside UTM's `tell` block: UTM had to resolve it and failed (-1728).
- `serial ports` in a configuration record compiled to the element class, so UTM
  rejected the record (-1700); the script now uses `«class SrPt»`.
- A scripted removable ISO cannot be opened by sandboxed UTM ("Cannot access
  resource"), and `utmctl start` still exited 0. Fixed with the imported installer
  drive, plus a status check after start.
- The QEMU TCP console capture ran `nc` before QEMU was listening, and without `-d`,
  so macOS `nc` dropped the connection at stdin's EOF. It now retries with `nc -d`.

## Layout

```
brainsyncos/
  Makefile                   thin wrapper around scripts/build.sh (make, make EDITION=vm, ...)
  .gitignore                 build/, ISOs and the user-specific secrets in config/
  config/base-iso.env        pinned base ISO + release identity (KEY=value)
  config/brainsyncos.env     product: version, board, hooked autoinstall, boot menu, ISO size limit
  config/vm-base-iso.env     VM edition: pinned Ubuntu Server arm64 ISO (same keys + BASE_ISO_URL)
  config/vm.env              VM edition: EDITION, boot menu, consoles, UTM VM defaults
  config/build.env           builder container resources: CPUs, jobs, memory, /scratch
  config/packages.env        package repository: suite, component, key lifetime, maintainer
  config/model.env           the local LLM's default model: Hugging Face repo, revision, file, sha256, licence
  config/repo-signing-key{,.pub}.asc   the repository signing key (gitignored; scripts/build.sh key)
  docker/Dockerfile.builder  ubuntu:24.04 pinned by digest + ISO/squashfs/grub/apt-repo tooling
  docker/Dockerfile.runtime-test  test only: the same base + Xvfb and GL, for tests/runtime-checks.sh
  docs/base-iso-inventory.md tracked copy of the inventory report (deterministic)
  packages/NAME/             one folder per BrainSyncOS package: package.env + build.sh
    brainsyncos-archive-keyring/   the repository's public key, for apt
    brainsyncos-ch341-tegra/       ch341.c (Linux 6.8.12) built for NVIDIA's kernel (Jetson only)
    brainsync-udev/                udev rules for the Cerelog X8's serial port (both editions)
    brainsync-runtime/       the apps' Python venv, /opt/brainsyncos/venv (scripts/venv.sh)
    brainsync/               the apps: their wheel in that venv, /usr/bin links, /etc/brainsync, /usr/share/brainsync
    brainsyncos-model/       the local LLM's model (config/model.env) and model.conf (Jetson only by default)
  python/                    the venv's inputs: brainsync.in, brainsync.lock.txt (hash-locked wheels),
                             psychopy-excludes.txt, pip.env (pinned pip), local-wheels.txt, check-lock.py
  wheels/NAME/               one folder per Python wheel BrainSyncOS compiles: wheel.env + build.sh
    brainflow-cerelog/       the BrainFlow Cerelog fork: pins, patches/, requires.txt
  payload/                   copied to /brainsyncos on the ISO
    hook.sh                  late-command: installs the payload, mounts the package
                             repository, runs install.d in-target
    install.d/NN-*.sh        install steps, run inside the installed system in order
    lib/apt-repo.sh          for install steps: apt against the ISO's signed repository only
  vm/autoinstall.yaml        VM edition: subiquity autoinstall (-> /brainsyncos/autoinstall.yaml)
  scripts/
    lib.sh                   strict mode, log/warn/die, paths, config loaders, ISO and package helpers
    venv.sh                  the BrainSync venv: wheelhouse, pinned pip, build at /opt/brainsyncos/venv
    model.sh                 the model pin: config loader, cached and checked download, GGUF header
    build.sh                 one entry point: doctor | key | wheels | packages | repo | iso | all | test | clean
    builder.sh               host entrypoint: build | run CMD | shell | image | clean
    make-repo-key.sh         the repository signing key (Ed25519, sign-only, expiring)
    build-wheels.sh          wheels/*/build.sh -> build/wheels/ (+ manifest, SHA256SUMS, requirements.txt)
    pack-wheel.py            a reproducible wheel from a staged tree
    build-packages.sh        packages/*/build.sh -> build/debs/EDITION/
    build-repo.sh            build/debs/EDITION -> signed apt repository build/repo/EDITION
    verify-base-iso.sh       size, volume id, sha256 (cached), .disk/info, kernel layer (--edition vm)
    fetch-vm-base.sh         VM edition: download + verify the Ubuntu ISO (host, resumable)
    extract-iso.sh           atomic, idempotent extraction + boot metadata
    inspect-iso.sh           inventory report
    repack-iso.sh            replay-preserving rebuild from build/iso-root
    compare-iso.sh           El Torito / GPT|MBR+CHS / ESP / ISO9660+Joliet names / tree vs the base
    build-iso.sh             the BrainSyncOS ISO (Phase 2)
    build-vm-iso.sh          the BrainSyncOS VM edition ISO (UTM)
    render-vm-grub.py        VM edition boot menu from Ubuntu's grub.cfg
    check-autoinstall.py     validates vm/autoinstall.yaml (optionally against subiquity's schema)
    utm-vm.sh                macOS: create/install/start/stop/delete BrainSyncOS* VMs in UTM
    patch-autoinstall.py     inserts the hook into an NVIDIA autoinstall file
    render-grub.py           BrainSyncOS boot menu from NVIDIA's grub.cfg
    merge-md5sums.py         md5sum.txt = vendor entries + staged and mapped files
    write-usb.sh             macOS: guarded dd of an ISO to an external disk
    make-password-hash.sh    console password hash for the brainsync account
  tests/
    testlib.sh               PASS/FAIL check runner; nvidia-l4t-kernel stub for hook targets
    test_phase1.sh           end-to-end Phase-1 test
    test_phase2.sh           end-to-end Phase-2 test (host side)
    phase2-checks.sh         its in-container checks
    test_vm.sh               end-to-end VM edition test (host side)
    vm-checks.sh             its in-container checks
    test_phase3.sh           Phase 3: configs, caps, build.sh, key, packages, repository, wheel (host side)
    phase3-checks.sh         its in-container checks: apt helper, BrainSync debs, md5 maps, hook + 60/70,
                             board packages, 75-usb-serial.sh, the BrainFlow wheel, the model
                             package, 86-model.sh, the ISO size limit
    runtime-checks.sh        the BrainSync packages installed and run (runtime test container)
    runtime/*.py             its Xvfb smoke scripts and the import-everything check
    boot-smoke.py            boots a VM ISO under QEMU to the menu, kernel and initramfs
    shims/curtin             `curtin in-target` as chroot, for the hook simulation
  build/                     gitignored cache and output
    iso-root/                the extracted ISO tree, owner-writable, symlinks kept
    meta/                    el-torito.{plain,as_mkisofs}.txt, system-area.plain.txt,
                             partitions.sfdisk.txt, esp.env, esp.sha256, tree.txt,
                             md5sum.orig.txt, builder-packages.txt, extract.stamp,
                             base-iso.verified
    reports/                 base-iso-inventory.md (copied to docs/ on a full run)
    stage/                   files build-iso.sh maps over the base ISO
    stage-vm/                files build-vm-iso.sh maps over the Ubuntu ISO
    debs/{jetson,vm}/        built packages (scripts/build-packages.sh)
    wheelhouse/, pip/        the downloaded wheels and pip (scripts/venv.sh), for offline rebuilds
    repo/{jetson,vm}/        signed apt repositories (scripts/build-repo.sh), mapped onto the ISOs
    downloads/               the Ubuntu ISO, its .verified stamp, SHA256SUMS(.gpg);
                             brainflow-cerelog.git (sparse partial clone of the fork);
                             models/<sha256>/ (the model and its licence)
    wheels/                  built wheels, NAME.env manifests, SHA256SUMS, requirements.txt
    out/                     ISOs (+ .sha256, xorriso log, tree-changes.txt / manifest.txt)
    test-logs/{phase1,phase2,phase3,vm}/  one log per test check
    vm/                      utm-vm.sh serial console captures
```

## How the repack works

`repack-iso.sh` does not rebuild the boot structures. It runs:

```
xorriso -indev BASE.iso -outdev OUT.iso -joliet on \
        -compliance iso_9660_level=3:joliet_long_names:lowercase:allow_dir_id_ext:joliet_long_paths \
        -boot_image any replay -padding included -update_r build/iso-root / -commit
truncate -s $((ISO_IMAGE_SIZE_512 * 512)) OUT.iso
```

Before that, it checks the base file's ESP and its 32 KiB system area against
`BASE_ESP_SHA256` / `BASE_SYSTEM_AREA_SHA256` in the config. The replay copies
those bytes straight from the file, so a cached sha256 alone is not enough.
It also prunes host junk (`.DS_Store`, `._*`, Spotlight/fseventsd dirs) from
`build/iso-root` and writes the paths added or removed since extraction to
`build/out/NAME.tree-changes.txt`. With `--expect-noop`, any change is fatal.

- **`-boot_image any replay`** reproduces the boot setup of the base ISO:
  - the protective MBR
  - the GPT (Gap0 / Appended2 ESP / Gap1)
  - the El Torito UEFI entry, which points at the appended ESP
  - the 512 MiB ESP itself, copied byte for byte from the base

  The ESP holds NVIDIA's L4TLauncher, shim, grub and the QSPI firmware capsules. **Never rebuild it.**
- **`-update_r`** syncs the ISO tree to `build/iso-root`: it adds, replaces and deletes files.
- **`md5sum.txt`** is regenerated first. For an unchanged tree the result is byte-identical to NVIDIA's file.
- **Fixes for four gotchas found in Phase 1:**
  - `-compliance ...` (`ISO_COMPLIANCE` in the config) reproduces NVIDIA's plain ISO9660 names (lowercase, ISO level 3, dots in directory names, so `.disk` and `boot/grub/grub.cfg;1`) and full-length Joliet names. Replay does not restore these settings. Without them, 7-Zip, Windows Explorer or `mount -o norock` see uppercased names and Joliet names cut at 64 characters, for example the `…installer.kernel.nvidia.squashfs` layer that casper needs. The rule set was found by trial and matches both namespaces of the base exactly.
  - `-padding included` keeps the Gap1 partition.
  - `-joliet on` keeps Joliet.
  - `truncate` drops xorriso's 32-block tail padding, so the backup GPT sits at the end of the file and `sfdisk -V` is clean.

**What still differs after a no-op roundtrip:**
- The ISO9660 area is about 6 MiB smaller, because xorriso packs the files more tightly. So the El Torito catalog LBA, the ESP start and the image length change.
- GPT GUIDs are new on every build.
- `boot/boot.cat` is regenerated.
- Files carry the extraction's `u+w` permission bits.
- The 4 symlinks get new mtimes.

`compare-iso.sh` checks everything else:
- El Torito and replay options
- the volume descriptor set (PVD, boot record, Joliet)
- the plain ISO9660 and Joliet name trees, read from the raw directory records
- the partition types, names, attributes and sizes
- the ESP bytes, against the config pin
- the full file tree (Rock Ridge), compared by content

## How the BrainSyncOS build works (Phase 2)

`build-iso.sh` never edits `build/iso-root`. It reads the three vendor files
it changes straight from the base ISO and checks them against NVIDIA's
`md5sum.txt`. It stages only what changes in `build/stage/`, then writes
`xorriso -indev BASE ... -boot_image any replay -map build/stage /`, so every
other file and all the boot bytes are NVIDIA's.

| ISO path | Change |
|---|---|
| `/casper/…installer.kernel.nvidia.squashfs` | `ai/jetsoniso_orin-ai.yaml` gets 3 lines: a comment, a progress `logger` line, and `/bin/bash /cdrom/brainsyncos/hook.sh`. They go before NVIDIA's final `Step 13/13` line, after every vendor step. |
| `/brainsyncos/` | the payload (`hook.sh`, `install.d/`, `lib/`) plus a `release` file and the package repository's public key, `repo-key.gpg` |
| `/brainsyncos-repo/` | the signed package repository (Phase 3), mapped from `build/repo/jetson` in place |
| `/boot/grub/grub.cfg` | the BrainSyncOS menu |
| `/md5sum.txt` | vendor entries plus the staged files and the repository |

**The rebuilt layer.**
- It is unpacked in a tmpfs (`/scratch`), because its `trusted.overlay.*` xattrs need `CAP_SYS_ADMIN` and cannot live on the macOS bind mount.
- Before patching, the build re-squashes the unmodified tree and requires a **byte-identical** match with NVIDIA's file (gzip, 1 MiB blocks, the original mkfs time). So the patched layer differs only in the one YAML file.
- `tests/phase2-checks.sh layer` re-checks this on the finished ISO: content, mode, owner, mtime and xattrs of all 1430 paths.

**The hook.** `patch-autoinstall.py` inserts the hook lines as text, so NVIDIA's formatting survives. It then parses both files with a YAML parser and requires:
- every other key to be unchanged
- `late-commands` to equal the vendor list plus exactly the hook

That check caught a real bug during development. A `logger` message containing `: ` had turned the command into a YAML mapping, which subiquity would have rejected on the device.

Our lines contain no `CHANGE_` text, so `preseed.sh`'s placeholder `sed`s cannot touch them. Anything `preseed.sh` appends (e.g. `offwhendone` → `poweroff`) still runs after the hook.

**Design decision.** The Phase 1 roadmap suggested a `subiquity.autoinstallpath=` wrapper, which needs no squashfs change. Patching the overlay layer was chosen instead, because:
- NVIDIA's Jetson ISO Customization Guide documents it
- the byte-identical re-squash proves it changes only the one file
- it does not depend on how subiquity re-reads its config after early-commands, which cannot be verified without hardware

NVIDIA's built-in `oem-iso-cfg` hook was also rejected. It runs `/cdrom/oemdata/oem-iso-cfg.sh` *inside* the target, but this autoinstall bind-mounts the ISO at `/target/tmp/cdrom`, not `/target/cdrom`.

**`hook.sh`** runs in the live installer (ISO at `/cdrom`, target at `$TARGET_MOUNT_POINT`, default `/target`). It:
1. copies the payload to `/opt/brainsyncos/installer`
2. mounts the ISO's package repository read-only at `/tmp/brainsyncos-repo` inside the target (Phase 3), and always unmounts it again on exit
3. runs each `install.d/NN-*.sh` with `curtin in-target`
4. tees everything to `/var/log/brainsyncos/install.log`, which it copies to the target even on failure

Any failing step fails the installation. It warns, but does not stop, if the board is not an Orin Nano.

## The package repository (Phase 3)

Everything BrainSyncOS installs beyond the base system comes from one signed
apt repository per edition, which every ISO carries at `/brainsyncos-repo`.
The pool on NVIDIA's ISO is signed with NVIDIA's key, so it cannot take our
packages.

**Building it.**
- `scripts/build.sh key` (`make-repo-key.sh`) creates the signing key once:
  - Ed25519, sign-only, expiring after `REPO_KEY_EXPIRE` (3 years).
  - It lives in `config/repo-signing-key.asc` (mode 600, gitignored) and is generated in a tmpfs GnuPG home.
- `build-packages.sh --edition E` runs `packages/NAME/build.sh` for every package whose `package.env` lists the edition. `build_deb` in `lib.sh` gives the packages root ownership, `md5sums` and the commit time as file times. The same input gives the same `.deb`.
- `build-repo.sh --edition E` lays out `pool/` and `dists/brainsyncos/` with `apt-ftparchive` and signs `InRelease` and `Release.gpg`. The secret key is only ever imported on `/scratch`. Before the new repository replaces the old one, the script checks:
  - `gpgv` verifies the signatures;
  - every `.deb` matches its index entry;
  - nothing outside `pool/` holds private key material.
- The ISO builds check the repository again. It must be signed by its own `repo-key.gpg` and built for the right edition: a VM repository is refused for the Jetson ISO. They then map it onto the ISO in place, without copying it into `build/stage`. `repo-key.gpg` also goes into the payload, and its fingerprint goes into the release file as `REPO_KEY=`.

**Installing from it.**
- `hook.sh` bind-mounts `/cdrom/brainsyncos-repo` read-only at `/tmp/brainsyncos-repo` in the target and unmounts it when it exits, even on failure.
- Install steps source `payload/lib/apt-repo.sh`. `bsos_repo_open` points apt at that repository alone:
  - its sources and lists go in a private directory, so the target's own apt configuration is neither used nor changed, and nothing goes to the network;
  - `Signed-By` is the payload's key;
  - `apt-get update --error-on=any` turns a missing, unsigned or tampered repository into a failed install;
  - `Check-Date: no`, because a Jetson without an RTC battery can start the installer with a wrong clock;
  - no dpkg pty and no `needrestart`, because the installer's chroot has neither a terminal nor services.
- `60-apt-repo.sh` is the first such step, on both editions. It installs `brainsyncos-archive-keyring` and requires the installed keyring (`/usr/share/keyrings/brainsyncos-archive-keyring.gpg`) to verify the repository by itself. The device keeps that keyring for later updates. No apt source is left behind.
- `70-brainsync.sh` installs `brainsync-runtime` and `brainsync` the same way, on both editions. It then checks that the venv imports the apps and that `brainsync-voice --help` runs, and lists any display libraries the system lacks.

**The BrainSync apps (Phase 3b).** Two packages, the same on both editions:
- **`brainsync-runtime`** (79 MB, 434 MB installed) is a Python 3.12 venv at `/opt/brainsyncos/venv` with the third-party packages the apps need. A venv works only at the path it was made at, so `scripts/venv.sh` builds it in the builder at that exact path:
  - from the 57 hash-locked cp312 aarch64 wheels of `python/brainsync.lock.txt` (no sdists), installed with `--no-deps --require-hashes` by the pip pinned in `python/pip.env`, which runs from its own wheel;
  - PsychoPy goes in without the dependencies the apps do not use (`python/psychopy-excludes.txt`). `pip check` would list those as missing, so it is never run;
  - folders named `tests` and `psychopy/demos` are removed (never `numpy/testing`: scipy imports it);
  - every module is byte-compiled to unchecked-hash `.pyc` files, so nothing is written under `/opt` at runtime and two builds give the same bytes.
  - It depends on `python3.12`, `libc6`, `libgcc-s1`, `libstdc++6` and `zlib1g`. The GL and X libraries PsychoPy loads are Recommends: NVIDIA's install puts them on the Jetson, and a VM without a desktop lacks them.
  - `/usr/share/doc/brainsync-runtime/wheels.txt` names every installed wheel with its sha256.
- **`brainsync`** (1.1 MB) is the app's wheel (`brainsync`, `app`, `tools`) installed into that venv, plus:
  - a `/usr/bin` link to each of the 9 console scripts (`brainsync-voice`, `brainsync-demo`, `brainchess`, `brainsync-server`, ...);
  - `config/{default,intents,profile}.yaml` as conffiles in `/etc/brainsync`;
  - `ui/` in `/usr/share/brainsync/ui`, and `clients/` without its tests in `/usr/share/brainsync/clients`, where `brainsync/paths.py` looks when installed.
  - It depends on exactly its own version of `brainsync-runtime`. An app change does not re-ship the runtime.
  - It never holds API keys, and the build refuses any `.env` file. Keys go on the device in `/etc/brainsync/secrets.env`.
- `scripts/builder.sh` mounts the BrainSync repository around `brainsyncos/` read-only at `/src`. `packages/brainsync` builds the wheel from a copy of it. `python/check-lock.py` fails that build when `pyproject.toml` asks for something the lock does not pin.
- **Offline rebuilds.** The first build downloads the wheels into `build/wheelhouse/` and pip into `build/pip/`. Later builds use only those. `scripts/build.sh clean --all` deletes them.
- **Build time.** Under qemu on an x86-64 laptop, one edition's packages took about 14 minutes with 3 CPUs and about 25 at the default 2. The venv takes about 5 minutes; most of the rest is xz compressing the 430 MB runtime (15 minutes alone on 2 busy CPUs). A native arm64 builder is much faster.
- **Re-locking**, after a dependency change in `pyproject.toml`: mirror the change in `python/brainsync.in`, then in `python/` run, with uv 0.13.5:
  `uv pip compile brainsync.in --excludes psychopy-excludes.txt --python-version 3.12 --python-platform aarch64-manylinux_2_39 --only-binary :all: --generate-hashes -o brainsync.lock.txt`.
  A PsychoPy update must pass the `runtime` check: a missing undeclared dependency can show up only as a vague error when a window opens.
- **Locally built wheels** go into the same venv: the Cerelog BrainFlow fork (Phase 3c) is the one listed today.
  - `python/local-wheels.txt` names them; `scripts/build.sh wheels` leaves them in `build/wheels/` with `SHA256SUMS`, and `scripts/build.sh all` runs that step first (it is skipped while up to date).
  - The venv build fails when a listed wheel is missing or does not match `SHA256SUMS`, and refuses unlisted ones. Their own dependencies must already be in the lock.
  - The `runtime` check requires BrainFlow in the installed venv to report the Cerelog X8 at 250 Hz.

**Verified by `tests/test_phase3.sh`** (19 checks; `BSOS_TEST_ONLY=NAME,...` runs some of them):
- the container really runs at the configured CPU and memory caps;
- the key, packages and repositories for both editions; every package, the venv included, is byte-identical between the two editions' separate builds;
- `app-debs`: the BrainSync packages' fields and dependencies, who ships what (no file in both, no app files in the runtime), the conffiles, no `.env` file, no `tests` folders but `numpy/testing`, an unchecked-hash `.pyc` beside every module;
- `runtime`: both packages installed with `dpkg -i` in a stock Ubuntu 24.04 arm64 container with Xvfb (`docker/Dockerfile.runtime-test`, test only, no network): every module imports, PsychoPy and the apps' own renderers draw, the nine commands answer `--help`, `brainsync-voice` serves its page and `/api/health` with its log in the user's state folder, and nothing is written under `/opt` while they run;
- in NVIDIA's real target layer, the hook mounts the repository, `60-apt-repo.sh` installs the keyring, `70-brainsync.sh` installs the apps and `brainsync-voice` runs there, the target's apt state is unchanged, and the mount is gone afterwards (also when a later step fails or the repository is missing);
- the apt helper accepts a clock set to 2001;
- the apt helper refuses: a tampered index, a tampered `.deb`, an unsigned repository, the wrong key, a missing repository;
- the board packages: contents, the exact kernel Depends, and `ch341.ko` against the ISO's own kernel (vermagic, and every modversion CRC against the headers' `Module.symvers` and against the CRCs NVIDIA's 1352 modules import); the udev rules pass `udevadm verify`;
- `75-usb-serial.sh` in NVIDIA's target layer with `nvidia-l4t-kernel` from the pool: modprobe resolves `ch341` to `updates/ch341.ko` after `usbserial.ko`, a re-run works, a purge takes it out of `modules.dep`, and the VM edition gets the rules only;
- the BrainFlow wheel: manifest, contents, a `--require-hashes` install into a fresh venv that reports 250 Hz and EEG channels 1 to 8, and a tampered copy refused;
- `model-pin`: `config/model.env` refuses bad values, the VM edition builds no model, and the pins match Hugging Face's API (skipped offline);
- `model-deb`: the model package's fields, its six files, the model and licence against the pins, the GGUF file type, and `model.conf` in `EnvironmentFile` syntax, not a conffile;
- `model-step`: `86-model.sh` in NVIDIA's real target layer (on disk) installs the model and checks its sha256, a re-run works, a damaged model fails the step, a purge removes `model.conf`, and a repository without a model is skipped;
- `iso-size`: the estimate, bad `ISO_MAX_BYTES` values refused, `finalize_iso` refusing an image over the limit, `build-iso.sh` stopping at its first estimate, and the test repository fitting the default limit.

**Not tested here:** anything on hardware. Nobody has loaded the module on an Orin, plugged in a board, or streamed from BrainFlow on Linux.

`tests/test_phase2.sh` and `tests/test_vm.sh` build their test ISOs with a throwaway repository (without the model), and run every step in each edition's target.

**The EEG board (Phase 3c).** The Cerelog X8 is a USB serial device: V1 boards have a CH340 bridge (vendor `1a86`, driver `ch341`, `/dev/ttyUSB*`), V2 boards an ESP32-S3 (vendor `303a`, driver `cdc_acm`, `/dev/ttyACM*`).
- `brainsyncos-ch341-tegra` (Jetson only) is the CH340 driver. NVIDIA's kernel leaves `CONFIG_USB_SERIAL_CH341` unset.
  - `ch341.c` from Linux 6.8.12 is built against `nvidia-l4t-kernel-headers` from the base ISO's pool and installed as `/lib/modules/6.8.12-1021-tegra/updates/ch341.ko`.
  - The build checks it against NVIDIA's `nvidia-l4t-kernel`: the same vermagic as `cp210x.ko`, and `usbserial.ko` present.
  - Its postinst and postrm run `depmod`. It depends on exactly `nvidia-l4t-kernel (= 6.8.12-tegra-39.2.1-20260806224157)`, so a kernel update becomes an apt conflict instead of a missing driver.
  - **The module is not signed.** It loads only while module signature enforcement is off, as on stock JetPack.
- `brainsync-udev` (both editions) installs `/usr/lib/udev/rules.d/70-brainsync-cerelog.rules`. Tty devices of vendors `1a86` and `303a` get `GROUP=dialout`, `MODE=0660`, the symlink `/dev/cerelog`, and `ID_MM_DEVICE_IGNORE=1`, so ModemManager does not probe them (its DTR toggle resets an ESP32-S3). The postinst reloads udev only on a running system, not in the installer's chroot.
- `75-usb-serial.sh` installs both on the Jetson. On the VM it installs only `brainsync-udev`: Ubuntu's kernel has `ch341` in `linux-modules-extra`.
- **The BrainFlow fork is a wheel, not a package.** It goes into the BrainSync venv (`/opt/brainsyncos/venv`, `brainsync-runtime`), which cannot see system site-packages.
  - `scripts/build.sh wheels` (`build-wheels.sh`) builds `wheels/brainflow-cerelog/` into `build/wheels/`.
  - It fetches the fork at commit `932b3944` without credentials, as a sparse partial clone cached in `build/downloads/` (40 MB instead of 351 MB), and checks the tree hash.
  - It applies `patches/0001-cerelog-connect_v1-raw-timeout.patch` (sha256-pinned), runs CMake with `BUILD_SYNCHRONI_SDK=OFF BUILD_BLE=OFF BUILD_BLUETOOTH=OFF BUILD_ONNX=OFF`, and builds only `BoardController DataHandler MLModule`.
  - The wheel `brainflow-5.0.0+cerelog932b394.1-py3-none-linux_aarch64.whl` holds `brainflow/*.py` and the three stripped libraries, nothing else. The fork's own `lib/` folder also collects x86-64 and 32-bit ARM vendor blobs.
  - `Requires-Dist: numpy, setuptools<81`: the fork imports `pkg_resources`.
  - Before it lands in `build/wheels/`, the build installs it with pip into a fresh venv, where `BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD)` must print 250.
  - Output: the wheel; `brainflow-cerelog.env`, a KEY=value manifest (file, sha256, size, version, source commit and tree, patches, CMake flags, toolchain, inputs hash); `SHA256SUMS`; `requirements.txt` for `pip install --no-index --find-links build/wheels --require-hashes --no-deps -r build/wheels/requirements.txt`.
  - A rebuild is skipped while the inputs hash (the recipe, `pack-wheel.py`, the builder's Dockerfile) and the wheel's sha256 still match; `--force` rebuilds. Under x86-64 emulation the compile takes about 47 minutes at 3 jobs.

**The local LLM's model (Phase 3d).** The default model is Qwen3.5-2B, quantized to Q4_K_M, as one GGUF file of 1,280,835,840 bytes (Apache-2.0).
- `config/model.env` pins it: the Hugging Face repository `unsloth/Qwen3.5-2B-GGUF`, the revision `f6d5376b…` (a commit, never a branch), the file, its size and sha256, and the GGUF file type (15, Q4_K_M). The licence text is pinned the same way, from the official `Qwen/Qwen3.5-2B`. The file says why this GGUF: Qwen and ggml-org publish none of the 2B, and unsloth's is converted from Qwen's own release and by far the most used.
- `scripts/model.sh` downloads only `https://huggingface.co/<repo>/resolve/<revision>/<file>`, once, into `build/downloads/models/<sha256>/` (an interrupted download resumes). Every build checks the size and sha256 again, about 7 s under qemu.
- `packages/brainsyncos-model` builds `brainsyncos-model-qwen3.5-2b-q4km` (Architecture `all`). It installs:
  - `/opt/brainsyncos/models/Qwen3.5-2B-Q4_K_M.gguf`, and its `.sha256` beside it;
  - `/etc/brainsyncos/llama.d/model.conf`, the file the llama-server unit reads (see below);
  - the licence, a copyright file and a README in `/usr/share/doc/`.
  - It Provides, Conflicts with and Replaces `brainsyncos-model`, so one model is installed at a time.
  - The build checks the GGUF header (version 3, architecture `qwen35`, file type 15).
  - It is staged on disk under `build/tmp`, with the model hard-linked from the download cache, never on the `/scratch` tmpfs. `build-packages.sh` now builds every package on disk, under its output folder.
  - It is compressed with zstd at level 3: Q4_K_M weights barely compress (21 MB saved, in 19 s under qemu at 1 CPU). xz would take about 45 minutes. dpkg-deb's own zstd default level is about 60 times slower than level 3.
- **Editions.** `PKG_EDITIONS=jetson` in its `package.env` is the switch: the Orin Nano ISO carries the model, the VM ISO does not. Set it to `all` to give the VM the model too.
- **`86-model.sh`** installs the one `brainsyncos-model-*` package the repository has, then checks `model.conf` and the model's sha256. When the repository has no model package (the VM by default) it says so and does nothing.
- **The contract with the llama-server unit** (`brainsyncos-llama.service`, Phase 3d, `85-llama.sh`). `/etc/brainsyncos/llama.d/model.conf` is an `EnvironmentFile`:

  ```
  BSOS_LLAMA_MODEL=/opt/brainsyncos/models/Qwen3.5-2B-Q4_K_M.gguf
  BSOS_LLAMA_ARGS="-c 4096"
  ```

  - The unit uses `ConditionPathExists=/etc/brainsyncos/llama.d/model.conf` and `EnvironmentFile=/etc/brainsyncos/llama.d/model.conf`, then `-m ${BSOS_LLAMA_MODEL} $BSOS_LLAMA_ARGS` in `ExecStart`. Unbraced, `$BSOS_LLAMA_ARGS` splits into separate options.
  - `BSOS_LLAMA_ARGS` holds model options only (`MODEL_LLAMA_ARGS` in `config/model.env`). Host, port, key, GPU layers and limits belong to the unit.
  - `model.conf` is not a conffile: an upgrade replaces it, and removing the package removes it, so the unit no longer starts. Local options go in `/etc/brainsyncos/llama.d/local.conf`, which the unit reads after it, if it exists (`EnvironmentFile=-…`).

**The ISO size limit.** `ISO_MAX_BYTES` in `config/brainsyncos.env` (default 7,800,000,000) is the largest ISO a build may write. Both ISO builds check it:
- first, an estimate before the base ISO is read: the base ISO plus the repository and the payload (`iso_tree_bytes`: each file in 2 KiB blocks plus its directory records);
- on the Jetson, again from the staged files before xorriso runs;
- last, on the finished image, which is deleted when it is too large.
- Why 7.8e9: an ISO is written raw to the whole stick, so a stick takes an image as large as its capacity. "8 GB" means 8,000,000,000 bytes, and real sticks are often a little smaller, because the controller keeps part of the flash. 7.8e9 leaves 200 MB (2.5 %) for that. `scripts/write-usb.sh` still compares the ISO with the actual stick. For a 16 GB stick, use e.g. 15600000000.

## Appliance identity (Phase 2.5)

Four more `install.d` steps run after `00-release.sh` and `10-issue.sh`. `hook.sh` first records the board's device-tree model and serial number in `device.env`; `build-iso.sh` stages `settings.env` and the logins in `/brainsyncos/config/`. On the installed system that directory is readable by root only.

| Step | What it does |
|---|---|
| `20-hostname.sh` | `HOSTNAME_PREFIX-<last 6 of the serial>` in `/etc/hostname` and `/etc/hosts`. A cloud-init drop-in (`preserve_hostname: true`) keeps first boot from renaming it. |
| `30-user.sh` | Creates `BRAINSYNC_USER` in NVIDIA's hardware groups plus `dialout` (the EEG board). It installs the SSH keys and, if built, the console password hash. Without a password the account is locked for password logins and gets passwordless sudo, checked with `visudo`. With one, sudo asks for it. It deletes NVIDIA's `nvidia`/`ubuntu` users, which have known passwords, if the `nooemconfig` path created them. It marks GNOME's first-login wizard done. |
| `40-ssh.sh` | sshd drop-in: no root login, no passwords, `AllowUsers brainsync`. Enables `ssh.socket` and checks the result with `sshd -t`. |
| `50-skip-oobe.sh` | `default.target` → `graphical.target` when NVIDIA's OOBE is the default target (a Jetson installed with `nooemconfig`, or the VM edition, keeps its default). Removes the `nv-oobe.target` links and `/etc/nv/nvautoconfig`. `nvpmodel`, `nvpower` and `nvfancontrol` stay enabled, because their packages also link them into `multi-user.target.wants`. |

**What skipping OOBE skips:** NVIDIA's OOBE (`nv-oobe.sh`) only switches to `graphical.target`; GNOME Initial Setup then creates the user and shows **NVIDIA's Jetson software license**. A BrainSyncOS install never shows that license. Read it (`/usr/share/doc/nvidia-l4t-oobe/` on the device) and decide for yourself that you accept it, as you would on first boot of stock JetPack. Also not configured: Wi-Fi (Ethernet works out of the box; use `nmcli` over SSH), keyboard layout, time zone (UTC).

**Logins on the ISO:** `build-iso.sh` rejects:
- private keys, and `authorized_keys` files that `ssh-keygen -l` cannot read
- password files that are not a single crypt hash
- builds with no login at all

Public keys and the salted hash are readable by anyone holding the USB stick, so treat the stick like the device.

**Tested without hardware** (`tests/test_phase2.sh`, 16 checks):
- **Generators:** the config validation, both generators against NVIDIA's real files including their failure modes, and the md5 merge identity.
- **The built ISO:**
  - its boot structure against the base: El Torito, GPT, ESP bytes, and every ISO9660/Joliet name unchanged, with exactly the payload and the package repository added
  - every staged file
  - all 2463 untouched `md5sum.txt` entries
  - read-only, root-owned payload modes
- **The hook:** run from the built ISO against NVIDIA's real target base layer (`ubuntu-server-minimal.squashfs`).
  - The target is first brought to the state NVIDIA's late-commands leave it in: vendor groups, OOBE as the default target, and `openssh-server` + `libwrap0` from the ISO pool, with what their postinst does.
  - A fake Orin Nano device tree supplies the model and serial.
  - `curtin in-target` is a shim that, like curtin, bind-mounts `/dev`, `/proc` and `/sys` and chroots.
  - It covers:
    - success including the whole identity (hostname, groups, locked password + sudoers, key file modes, sshd config with `sshd -t`, OOBE links, NVIDIA's default user removed);
    - the repository keyring from `60-apt-repo.sh`;
    - a re-run;
    - the console-password variant;
    - a failing step, a build without logins, and a missing target.
- **Login and repository checks:** `build-iso.sh` refuses missing, private and malformed credentials, a missing package repository, and a repository built for the VM edition. The test build uses throwaway SSH and repository keys and its own `phase2-test.iso`, which is deleted afterwards, so your real ISO and `config/` are never touched.

**Not tested yet:**
- the real subiquity/curtin run on a Jetson
- the firmware capsule path

## What the base ISO looks like (Phase 1 findings)

The full report is [`docs/base-iso-inventory.md`](docs/base-iso-inventory.md). `scripts/inspect-iso.sh` generates it into `build/reports/` and copies it to `docs/`. The output is deterministic and contains no host paths.

**Autoinstall:**
- It lives in the NVIDIA overlay layer `casper/ubuntu-server-minimal.ubuntu-server.installer.kernel.nvidia.squashfs`, not at the ISO root and not in the initrd.
- Its `/autoinstall.yaml` has a single early-command: `bash /ai/preseed.sh jetsoniso`.
- `preseed.sh` then:
  1. detects the board
  2. repartitions the boot disk from `/opt/nvidia/ptable.<family>.<disk>.dynamic`
  3. fills the `CHANGE_*` placeholders in `/ai/jetsoniso_{orin,thor}-ai.yaml`
  4. copies the result over `/autoinstall.yaml`; subiquity re-reads it after early-commands
- Most of the real work happens in late-commands:
  - install 1961 packages offline from the ISO pool, including ubuntu-desktop
  - purge GRUB and switch to L4TLauncher + extlinux
  - reinstall `nvidia-l4t-bootloader` (a QSPI update)
  - on first boot, run NVIDIA's OOBE/oem-config unless `nooemconfig` is set

**Layer chain:**
- The live installer mounts `minimal → ubuntu-server → installer → installer.kernel → installer.kernel.nvidia`.
- `generic-hwe` is never mounted.
- The installed target gets only the OS layers listed in `casper/install-sources.yaml`.

**Signatures and hashes:**
- Two squashfs `.gpg` signatures are already BAD on the vendor ISO, and one layer has no signature at all. Nothing verifies them.
- The apt repo `dists/noble` is signed by NVIDIA's DGX key, so we cannot re-sign it. BrainSync packages go in a separate repo on the ISO.
- `md5sum.txt` is only reported, not enforced. We regenerate it anyway.

**The pool lacks** `python3-pip`, `python3-venv`, PyQt5, PsychoPy, liblsl, cage, weston, the CUDA toolkit and TensorRT. It does have numpy, scipy, pyserial, docker.io and nvidia-container-toolkit.

## Roadmap

- **Phase 2: install hook. Done (see above).**
- **Phase 2.5: appliance identity. Done (see above).** Phases 2 and 2.5 are waiting on a hardware install on an Orin Nano.
  - Moved to Phase 4: dropping desktop extras. The kiosk session will replace GNOME, and removing packages now risks breaking NVIDIA's dependencies.
  - Open: Wi-Fi provisioning at build time.
- **VM edition (UTM): done (see above).** Installed end to end in UTM on both backends. Not tried yet: USB passthrough of the real EEG board.
  - Phase 3 and 4 `install.d` steps must run on both editions (or skip cleanly on the one they do not apply to).
- **Phase 3: BrainSync payload**, in five parts, each shipped through the signed repository. Status, pins and the handoff to an x86-64 build machine are in [`docs/phase3-notes.md`](docs/phase3-notes.md).
  - **3a, build system and repository: done** (see [The package repository](#the-package-repository-phase-3)).
  - **3b, the apps.**
    - Make the app installable outside the repo: configurable paths instead of the repo-root lookups, console commands, a speech fallback without macOS `say`.
    - Ship it as a self-contained Python venv (`brainsync-runtime`, built from a hash-locked set of wheels) plus the app package. PsychoPy goes in with a trimmed dependency set, because wxPython has no arm64 wheels.
    - Core apps: the SSVEP demo and tools, BrainSwipe, BrainChess and BCI Voice. Reels is left out, because it needs Chrome and Instagram.
  - **3c, the board: built, not yet tried on hardware** (see [The package repository](#the-package-repository-phase-3)).
    - The Cerelog BrainFlow fork, built for arm64 at a pinned commit plus its one local patch, as a wheel for the BrainSync venv.
    - udev rules for the board.
    - The **CH340 driver**. NVIDIA's kernel lacks it: `CONFIG_USB_SERIAL_CH341` is not set, while `USB_ACM`, `CP210X` and `FTDI_SIO` are modules. It ships as `brainsyncos-ch341-tegra`, built for the exact L4T kernel; the VM's Ubuntu kernel already has it.
  - **3d, local LLMs.**
    - llama.cpp: a CPU build for both editions, plus the CUDA backend for the Orin (sm_87).
    - The CUDA 13.2 runtime only (cudart and cuBLAS, about 0.5 GB), with no toolkit or TensorRT. On the device, `apt install nvidia-jetpack` still works through NVIDIA's repo.
    - A sandboxed `llama-server` on localhost and a pinned default model (Qwen3.5-2B Q4).
    - BCI Voice tries the local model first, then Claude, then the fixed lists.
  - **3e, integration.** Both ISOs built end to end, a VM install in UTM, and a hardware checklist for the first Orin (GPU backend, memory, CH340, flicker timing while the LLM runs).
- **Phase 4: appliance runtime.**
  - Kiosk session: gdm autologin on X11 (the AI file disables Wayland), with systemd units starting BrainSync.
  - Disable suspend (errata 6235055).
  - Set nvpmodel and validate vsync/frame timing for the PsychoPy flicker on real hardware.
- **Phase 5: firmware and provisioning.**
  - The ISO prompts for a QSPI capsule update ("press Y within 30 s"), so a fully unattended install needs firmware at 39.2.x first.
  - Document the macOS path: balenaEtcher plus the serial console, with the Orin Nano JP5→JP6 SD-card bridge where needed.
  - Recovery-mode flashing needs an x86-64 Ubuntu host.
- **Phase 6: release engineering.**
  - Reproducible build in CI.
  - Versioned ISO naming plus a sha256 manifest.
  - A hardware test matrix: Orin Nano Super, AGX Orin and Thor; NVMe and microSD.
  - An update/OTA story; the apt upgrade path to r39.2 is not supported by NVIDIA.

Nothing has been boot-tested on Jetson hardware yet. Phases 1 and 2 prove the
structure and contents of the ISO and run the hook against NVIDIA's real
target filesystem in a container. They do not prove that it boots and installs.
