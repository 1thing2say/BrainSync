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

BrainSync itself is not installed yet; that is Phases 3 and 4.

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
- **bash 3.2 or newer** on the host. Host-side scripts stay compatible with macOS `/bin/bash`. All heavy tools run in the container: xorriso, squashfs-tools, mtools, sfdisk, gpgv and others.
- **Disk:**
  - `build/iso-root` takes about 4.2 GiB.
  - Each output ISO takes about 4.7 GiB.
  - The scripts refuse to start below `MIN_FREE_GB=20` GiB free.
  - Never keep more than one extraction.
  - **Docker Desktop on macOS caveat:** the Docker VM keeps host file descriptors open for files it has accessed through the bind mount, even after they are deleted on either side. A plain `rm` of a 5 GB ISO therefore frees nothing until Docker restarts (check with `lsof +L1`). The scripts truncate files before deleting them (`rm_released` and `force_rm_tree` in `scripts/lib.sh`). Clean up with `scripts/builder.sh clean [--all]`, not with `rm -rf build`. If space has already gone missing, either restart Docker Desktop or run `docker run --rm --privileged brainsyncos-builder:latest sh -c 'sync; echo 2 > /proc/sys/vm/drop_caches'`.

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

scripts/builder.sh run scripts/build-iso.sh          # -> build/out/brainsyncos-0.1.0-orin-nano.iso (~1 min)
tests/test_phase2.sh                                  # builds a separate test ISO with a throwaway key, 15 checks (~3 min)

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

## Layout

```
brainsyncos/
  config/base-iso.env        pinned base ISO + release identity (KEY=value)
  config/brainsyncos.env     product: version, board, hooked autoinstall, boot menu
  docker/Dockerfile.builder  ubuntu:24.04 pinned by digest + ISO/squashfs/grub tooling
  docs/base-iso-inventory.md tracked copy of the inventory report (deterministic)
  payload/                   copied to /brainsyncos on the ISO
    hook.sh                  late-command: installs the payload, runs install.d in-target
    install.d/NN-*.sh        install steps, run inside the installed system in order
  scripts/
    lib.sh                   strict mode, log/warn/die, paths, config loaders, ISO helpers
    builder.sh               host entrypoint: build | run CMD | shell | image | clean
    verify-base-iso.sh       size, volume id, sha256 (cached), .disk/info, kernel layer
    extract-iso.sh           atomic, idempotent extraction + boot metadata
    inspect-iso.sh           inventory report
    repack-iso.sh            replay-preserving rebuild from build/iso-root
    compare-iso.sh           El Torito / GPT / ESP / ISO9660+Joliet names / tree vs the base
    build-iso.sh             the BrainSyncOS ISO (Phase 2)
    patch-autoinstall.py     inserts the hook into an NVIDIA autoinstall file
    render-grub.py           BrainSyncOS boot menu from NVIDIA's grub.cfg
    merge-md5sums.py         md5sum.txt = vendor entries + staged files
    write-usb.sh             macOS: guarded dd of an ISO to an external disk
    make-password-hash.sh    console password hash for the brainsync account
  tests/
    testlib.sh               PASS/FAIL check runner
    test_phase1.sh           end-to-end Phase-1 test
    test_phase2.sh           end-to-end Phase-2 test (host side)
    phase2-checks.sh         its in-container checks
    shims/curtin             `curtin in-target` as chroot, for the hook simulation
  build/                     gitignored cache and output
    iso-root/                the extracted ISO tree, owner-writable, symlinks kept
    meta/                    el-torito.{plain,as_mkisofs}.txt, system-area.plain.txt,
                             partitions.sfdisk.txt, esp.env, esp.sha256, tree.txt,
                             md5sum.orig.txt, builder-packages.txt, extract.stamp,
                             base-iso.verified
    reports/                 base-iso-inventory.md (copied to docs/ on a full run)
    stage/                   files build-iso.sh maps over the base ISO
    out/                     ISOs (+ .sha256, xorriso log, tree-changes.txt / manifest.txt)
    test-logs/phase{1,2}/    one log per test check
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
| `/brainsyncos/` | the payload (`hook.sh`, `install.d/`) plus a `release` file |
| `/boot/grub/grub.cfg` | the BrainSyncOS menu |
| `/md5sum.txt` | vendor entries plus the staged files |

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
2. runs each `install.d/NN-*.sh` with `curtin in-target`
3. tees everything to `/var/log/brainsyncos/install.log`, which it copies to the target even on failure

Any failing step fails the installation. It warns, but does not stop, if the board is not an Orin Nano.

## Appliance identity (Phase 2.5)

Four more `install.d` steps run after `00-release.sh` and `10-issue.sh`. `hook.sh` first records the board's device-tree model and serial number in `device.env`; `build-iso.sh` stages `settings.env` and the logins in `/brainsyncos/config/`. On the installed system that directory is readable by root only.

| Step | What it does |
|---|---|
| `20-hostname.sh` | `HOSTNAME_PREFIX-<last 6 of the serial>` in `/etc/hostname` and `/etc/hosts`. A cloud-init drop-in (`preserve_hostname: true`) keeps first boot from renaming it. |
| `30-user.sh` | Creates `BRAINSYNC_USER` in NVIDIA's hardware groups plus `dialout` (the EEG board). It installs the SSH keys and, if built, the console password hash. Without a password the account is locked for password logins and gets passwordless sudo, checked with `visudo`. With one, sudo asks for it. It deletes NVIDIA's `nvidia`/`ubuntu` users, which have known passwords, if the `nooemconfig` path created them. It marks GNOME's first-login wizard done. |
| `40-ssh.sh` | sshd drop-in: no root login, no passwords, `AllowUsers brainsync`. Enables `ssh.socket` and checks the result with `sshd -t`. |
| `50-skip-oobe.sh` | `default.target` → `graphical.target`. Removes the `nv-oobe.target` links and `/etc/nv/nvautoconfig`. `nvpmodel`, `nvpower` and `nvfancontrol` stay enabled, because their packages also link them into `multi-user.target.wants`. |

**What skipping OOBE skips:** NVIDIA's OOBE (`nv-oobe.sh`) only switches to `graphical.target`; GNOME Initial Setup then creates the user and shows **NVIDIA's Jetson software license**. A BrainSyncOS install never shows that license. Read it (`/usr/share/doc/nvidia-l4t-oobe/` on the device) and decide for yourself that you accept it, as you would on first boot of stock JetPack. Also not configured: Wi-Fi (Ethernet works out of the box; use `nmcli` over SSH), keyboard layout, time zone (UTC).

**Logins on the ISO:** `build-iso.sh` rejects:
- private keys, and `authorized_keys` files that `ssh-keygen -l` cannot read
- password files that are not a single crypt hash
- builds with no login at all

Public keys and the salted hash are readable by anyone holding the USB stick, so treat the stick like the device.

**Tested without hardware** (`tests/test_phase2.sh`, 15 checks):
- **Generators:** the config validation, both generators against NVIDIA's real files including their failure modes, and the md5 merge identity.
- **The built ISO:**
  - its boot structure against the base: El Torito, GPT, ESP bytes, and every ISO9660/Joliet name unchanged with +6 added
  - every staged file
  - all 2463 untouched `md5sum.txt` entries
  - read-only, root-owned payload modes
- **The hook:** run from the built ISO against NVIDIA's real target base layer (`ubuntu-server-minimal.squashfs`).
  - The target is first brought to the state NVIDIA's late-commands leave it in: vendor groups, OOBE as the default target, and `openssh-server` + `libwrap0` from the ISO pool, with what their postinst does.
  - A fake Orin Nano device tree supplies the model and serial.
  - `curtin in-target` is a shim that, like curtin, bind-mounts `/dev`, `/proc` and `/sys` and chroots.
  - It covers: success including the whole identity (hostname, groups, locked password + sudoers, key file modes, sshd config with `sshd -t`, OOBE links, NVIDIA's default user removed), a re-run, the console-password variant, a failing step, a build without logins, and a missing target.
- **Login checks:** `build-iso.sh` refuses missing, private and malformed credentials. The test build uses a throwaway key and its own `phase2-test.iso`, which is deleted afterwards, so your real ISO is never touched.

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
- **Phase 3: BrainSync payload.**
  - Build these in an arm64 chroot of the target layers:
    - the `ssvep` package
    - the vendored BrainFlow fork
    - liblsl
    - a Python venv or wheels, because the pool has no pip/venv
  - Ship them as a separate apt repo under `/brainsyncos/repo`, signed with our own key and installed into `/target/etc/apt/keyrings`.
  - Check the kernel config for `CONFIG_USB_ACM`, `CP210X`, `FTDI_SIO` and `CH341` in `nvidia-l4t-kernel`. Missing drivers ship as an out-of-tree module deb.
- **Phase 4: appliance runtime.**
  - Kiosk session: gdm autologin on X11 (the AI file disables Wayland), with systemd units starting BrainSync.
  - Disable suspend (errata 6235055).
  - Set nvpmodel, add udev rules for the Cerelog board, and validate vsync/frame timing for the PsychoPy flicker on real hardware.
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
