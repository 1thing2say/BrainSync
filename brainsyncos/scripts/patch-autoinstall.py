#!/usr/bin/env python3
"""patch-autoinstall.py - add the BrainSync install hook to an NVIDIA autoinstall file.

Usage: patch-autoinstall.py IN OUT

IN is a vendor per-platform autoinstall (e.g. /ai/jetsoniso_orin-ai.yaml from
the NVIDIA overlay squashfs). preseed.sh fills in its CHANGE_* placeholders at
install time and hands it to subiquity, so it is patched as text: the hook
lines are inserted just before NVIDIA's final "Step 13/13" progress line,
after every vendor step (packages, bootloader, users, EFI boot order) and
before anything preseed.sh appends (e.g. the offwhendone poweroff). Vendor
formatting is preserved byte for byte outside the inserted lines.

The result is checked with a YAML parser: every top-level key except
late-commands must be unchanged, and late-commands must equal the vendor list
with exactly the hook commands inserted at that point.
"""

import re
import sys

import yaml

MARKER = "# BrainSyncOS: install the BrainSync layer"
HOOK_LINES = [
    "  %s (inserted by brainsyncos/scripts/patch-autoinstall.py)" % MARKER,
    '  - logger -p info "BrainSyncOS - installing the BrainSync layer from /cdrom/brainsyncos"',
    "  - /bin/bash /cdrom/brainsyncos/hook.sh",
]
HOOK_COMMANDS = [
    'logger -p info "BrainSyncOS - installing the BrainSync layer from /cdrom/brainsyncos"',
    "/bin/bash /cdrom/brainsyncos/hook.sh",
]
ANCHOR = re.compile(r'^  - logger -p info ".*Step 13/13 Finished installation"$')


def fail(msg):
    sys.exit("patch-autoinstall.py: " + msg)


def main():
    if len(sys.argv) != 3:
        fail("usage: patch-autoinstall.py IN OUT")
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        text = f.read()
    if MARKER in text:
        fail("%s already contains the BrainSync hook; patch the vendor file" % src)
    if "CHANGE_" in "".join(HOOK_LINES):
        fail("hook lines must not contain CHANGE_ (preseed.sh rewrites those)")

    lines = text.split("\n")
    hits = [i for i, l in enumerate(lines) if ANCHOR.match(l)]
    if len(hits) != 1:
        fail("expected exactly one 'Step 13/13 Finished installation' line in %s, found %d" % (src, len(hits)))
    at = hits[0]
    patched = "\n".join(lines[:at] + HOOK_LINES + lines[at:])

    before = yaml.safe_load(text)
    after = yaml.safe_load(patched)
    if set(before) != set(after):
        fail("top-level keys changed: %s -> %s" % (sorted(before), sorted(after)))
    for key in before:
        if key != "late-commands" and before[key] != after[key]:
            fail("top-level key %r changed" % key)
    late = before["late-commands"]
    idx = next(i for i, c in enumerate(late) if isinstance(c, str) and "Step 13/13" in c)
    expected = late[:idx] + HOOK_COMMANDS + late[idx:]
    if after["late-commands"] != expected:
        fail("late-commands are not the vendor list plus the hook")

    with open(dst, "w") as f:
        f.write(patched)
    print("patched %s: hook inserted as late-commands %d-%d of %d (vendor had %d)"
          % (dst, idx + 1, idx + len(HOOK_COMMANDS), len(expected), len(late)))


if __name__ == "__main__":
    main()
