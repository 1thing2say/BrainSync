#!/usr/bin/env python3
"""render-vm-autoinstall.py - the VM edition's autoinstall for one build.

Usage: render-vm-autoinstall.py TEMPLATE OUT --desktop none|minimal|full

Copies vm/autoinstall.yaml line for line (comments kept) and, for a desktop,
adds its package as the first entry of autoinstall.packages: minimal ->
ubuntu-desktop-minimal (GNOME, as on the Jetson), full -> ubuntu-desktop;
none leaves the server install as it is. subiquity installs the packages
from the network before the late-commands, so the BrainSyncOS hook runs on
the system with the desktop. The result is parsed back and must equal the
template with just that package added.
"""

import argparse
import sys

import yaml

DESKTOPS = {"none": None, "minimal": "ubuntu-desktop-minimal", "full": "ubuntu-desktop"}
ANCHOR = "  packages:\n"


def fail(msg):
    sys.exit("render-vm-autoinstall.py: " + msg)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("template")
    ap.add_argument("out")
    ap.add_argument("--desktop", required=True, choices=sorted(DESKTOPS))
    args = ap.parse_args()

    with open(args.template) as f:
        template = f.read()
    lines = template.splitlines(keepends=True)
    pkg = DESKTOPS[args.desktop]
    if pkg:
        at = [i for i, line in enumerate(lines) if line == ANCHOR]
        if len(at) != 1:
            fail("expected exactly one %r line in %s, found %d" % (ANCHOR.strip(), args.template, len(at)))
        lines[at[0] + 1:at[0] + 1] = [
            "    # VM_DESKTOP=%s (config/vm.env), added by scripts/render-vm-autoinstall.py\n" % args.desktop,
            "    - %s\n" % pkg,
        ]
    text = "".join(lines)

    want = yaml.safe_load(template)
    if pkg:
        want["autoinstall"]["packages"].insert(0, pkg)
    if yaml.safe_load(text) != want:
        fail("rendered autoinstall differs from the template by more than the desktop package")
    with open(args.out, "w") as f:
        f.write(text)
    print("autoinstall: desktop %s%s" % (args.desktop, " (%s)" % pkg if pkg else ""))


if __name__ == "__main__":
    main()
