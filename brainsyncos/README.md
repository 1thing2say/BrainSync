# BrainSyncOS

BrainSyncOS is an appliance OS for NVIDIA Jetson. A Jetson installed from it
boots straight into **BrainSync**, the SSVEP brain-computer interface: a Cerelog
X8 EEG board over USB serial, a frame-locked PsychoPy flicker stimulus, LSL
markers, and CCA/FBCCA/TRCA decoders.

BrainSyncOS is not built from scratch. It is a remaster of NVIDIA's official
Jetson ISO installer for JetPack 7.2.1 / Jetson Linux R39.2.1. That ISO is
Ubuntu Server 24.04.3 with the subiquity installer and L4T added. We keep
NVIDIA's boot chain and partition logic untouched and add BrainSync on top.

Phase 1 (this tree) delivers five things:
- a pinned, verified base ISO
- an arm64 builder container (base image pinned by digest; apt packages are
  not snapshot-pinned, see below)
- an idempotent extraction of the ISO
- an inventory report, tracked at [`docs/base-iso-inventory.md`](docs/base-iso-inventory.md)
- a repack pipeline that is proven by a no-op roundtrip

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

## Layout

```
brainsyncos/
  config/base-iso.env        pinned base ISO + release identity (KEY=value)
  docker/Dockerfile.builder  ubuntu:24.04 pinned by digest + ISO/squashfs tooling
  docs/base-iso-inventory.md tracked copy of the inventory report (deterministic)
  scripts/
    lib.sh                   strict mode, log/warn/die, paths, config loader, helpers
    builder.sh               host entrypoint: build | run CMD | shell | image | clean
    verify-base-iso.sh       size, volume id, sha256 (cached), .disk/info, kernel layer
    extract-iso.sh           atomic, idempotent extraction + boot metadata
    inspect-iso.sh           inventory report
    repack-iso.sh            replay-preserving rebuild from build/iso-root
    compare-iso.sh           El Torito / GPT / ESP / ISO9660+Joliet names / tree vs the base
  tests/test_phase1.sh       end-to-end Phase-1 test
  build/                     gitignored cache and output
    iso-root/                the extracted ISO tree, owner-writable, symlinks kept
    meta/                    el-torito.{plain,as_mkisofs}.txt, system-area.plain.txt,
                             partitions.sfdisk.txt, esp.env, esp.sha256, tree.txt,
                             md5sum.orig.txt, builder-packages.txt, extract.stamp,
                             base-iso.verified
    reports/                 base-iso-inventory.md (copied to docs/ on a full run)
    out/                     repacked ISOs (+ .sha256, xorriso log, tree-changes.txt)
    test-logs/phase1/        one log per test check
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

- **Phase 2: unattended install hook.**
  - Add a grub entry, set as the default with a timeout, that passes `subiquity.autoinstallpath=/cdrom/brainsyncos/autoinstall.yaml`.
  - That wrapper's early-commands are:
    1. `bash /ai/preseed.sh jetsoniso`, as its own list item, because the script ends with `exit 0`
    2. a BrainSync patcher that appends late-commands to the generated `/autoinstall.yaml`
  - The added late-commands create the appliance user `nooemconfig`-style, skip OOBE, and install BrainSync.
  - This needs no squashfs change. Fallback: an extra layer `…nvidia.brainsync.squashfs` selected with `layerfs-path=`.
  - Do not use `force-ai=/cdrom/...`, because `preseed.sh` runs `sed -i` on that file and the ISO is read-only.
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

Nothing has been boot-tested on Jetson hardware yet. Phase 1 proves the
structure and contents of the repacked ISO, not that it boots.
