#!/usr/bin/env python3
"""boot-smoke.py - boot a BrainSyncOS VM ISO under QEMU far enough to prove the boot chain.

Usage: boot-smoke.py ISO --qemu QEMU --firmware EDK2_CODE_FD --log LOG
                     [--timeout SECONDS] [--expect TEXT]...

Boots ISO (UEFI, aarch64 `virt`, the ISO as a read-only SCSI CD, no disk, no
network, serial console on stdio) and drives it like a person at the console:
  1. waits for the BrainSyncOS boot menu (`Install BrainSyncOS`) - so UEFI ->
     shim -> GRUB from the replayed ESP found and read our grub.cfg
  2. presses Enter (the menu waits forever with VM_GRUB_TIMEOUT=-1)
  3. waits for the kernel's `Kernel command line:` and checks that it holds
     every --expect text (our autoinstall arguments and consoles)
  4. waits until the kernel runs the initramfs (`Run /init`), then stops QEMU
     (with --until REGEX: keeps booting the live system until the serial
     console matches REGEX, e.g. subiquity's autoinstall progress)
Nothing is installed and nothing is written except the serial log and a
temporary UEFI variable store. Exit 0 on success; 1 with the reason otherwise.
Runs on the host with any qemu-system-aarch64 (TCG is slow but enough).
"""

import argparse
import os
import re
import select
import subprocess
import sys
import tempfile
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\r")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iso")
    ap.add_argument("--qemu", required=True)
    ap.add_argument("--firmware", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--expect", action="append", default=[])
    ap.add_argument("--until", metavar="REGEX",
                    help="after the initramfs, keep booting until the serial console matches REGEX")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        vars_fd = os.path.join(tmp, "vars.fd")
        with open(vars_fd, "wb") as f:
            f.truncate(os.path.getsize(a.firmware))
        cmd = [
            a.qemu, "-M", "virt", "-cpu", "max", "-m", "2048", "-smp", "2",
            "-nographic", "-nic", "none", "-no-reboot",
            "-drive", "if=pflash,format=raw,readonly=on,file=" + a.firmware,
            "-drive", "if=pflash,format=raw,file=" + vars_fd,
            "-device", "virtio-scsi-pci",
            "-drive", "if=none,id=cd,format=raw,readonly=on,media=cdrom,file=" + a.iso,
            "-device", "scsi-cd,drive=cd",
        ]
        t0 = time.time()
        log = open(a.log, "wb")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        buf = b""
        stage = "menu"
        cmdline = None
        result = None

        def say(msg):
            print("[boot-smoke %4ds] %s" % (time.time() - t0, msg), flush=True)

        try:
            while result is None:
                if time.time() - t0 > a.timeout:
                    result = "timeout after %ds waiting for: %s" % (a.timeout, stage)
                    break
                r, _, _ = select.select([p.stdout], [], [], 1.0)
                if not r:
                    if p.poll() is not None:
                        result = "QEMU exited (rc %s) while waiting for: %s" % (p.returncode, stage)
                    continue
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:
                    result = "QEMU closed its output while waiting for: %s" % stage
                    break
                log.write(chunk)
                log.flush()
                buf = (buf + ANSI.sub(b"", chunk))[-200000:]
                if stage == "menu" and b"Install BrainSyncOS" in buf:
                    say("BrainSyncOS boot menu on the serial console; pressing Enter")
                    time.sleep(2)
                    p.stdin.write(b"\r")
                    p.stdin.flush()
                    stage = "kernel"
                    buf = b""
                elif stage == "kernel":
                    m = re.search(rb"Kernel command line: ([^\n]*)\n", buf)
                    if m:
                        cmdline = m.group(1).decode("utf-8", "replace").strip()
                        say("kernel command line: " + cmdline)
                        missing = [e for e in a.expect if e not in cmdline]
                        if missing:
                            result = "kernel command line lacks %r" % missing
                            break
                        stage = "initramfs"
                elif stage == "initramfs" and re.search(rb"Run /init as init process", buf):
                    say("kernel runs the initramfs")
                    if a.until:
                        stage = "until /%s/" % a.until
                        buf = b""
                    else:
                        result = "ok"
                elif stage.startswith("until") and re.search(a.until.encode(), buf):
                    say("console shows: %s" % re.search(a.until.encode(), buf).group(0).decode("utf-8", "replace"))
                    result = "ok"
        finally:
            p.kill()
            p.wait()
            log.close()
    if result != "ok":
        print("boot-smoke.py: FAILED: %s (serial log: %s)" % (result, a.log), file=sys.stderr)
        sys.exit(1)
    print("boot-smoke.py: OK in %ds: UEFI -> GRUB menu -> kernel with our arguments -> initramfs" % (time.time() - t0))


if __name__ == "__main__":
    main()
