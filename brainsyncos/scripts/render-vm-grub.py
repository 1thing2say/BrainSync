#!/usr/bin/env python3
"""render-vm-grub.py - BrainSyncOS VM edition boot menu from Ubuntu's grub.cfg.

Usage: render-vm-grub.py VENDOR_GRUB_CFG OUT --version V --build-id ID
                         --timeout SECONDS --kernel ga|hwe --consoles LIST
                         --autoinstall PATH [--base-iso NAME]

The stock Ubuntu Server arm64 ISO has two installer entries (GA and HWE
kernel) whose kernel lines read `linux /casper/<kernel> --- console=tty0`.
The two BrainSyncOS install entries copy them and add, before `---`,
`autoinstall` (subiquity then starts without asking "Continue with
autoinstall?") and `subiquity.autoinstallpath=PATH` (our autoinstall, read
only when booted from these entries: the stock entries stay stock, because
nothing is put at /cdrom/autoinstall.yaml). After `---`, which the installer
copies into the installed system's kernel command line, the vendor's console=
arguments are replaced by one console= per --consoles item (last one =
/dev/console). The vendor entries are kept verbatim in the Advanced options
submenu, the trailing 'Boot from next volume' / 'UEFI Firmware Settings'
entries at the top level. Fails if the vendor file does not have exactly the
entries this script knows, so a new base ISO cannot silently change the menu.
"""

import argparse
import re
import sys

# Vendor menuentry title -> our key. All must be present exactly once.
INSTALLERS = {
    "Try or Install Ubuntu Server": "ga",
    "Ubuntu Server with the HWE kernel": "hwe",
}
TRAILER = ["Boot from next volume", "UEFI Firmware Settings"]
ENTRY_RE = re.compile(r"^menuentry\s+(?P<q>[\"'])(?P<title>.+?)(?P=q)\s*\{\n(?P<body>.*?)^\}\n", re.M | re.S)
LINUX_RE = re.compile(r"^\s*linux\s+(?P<kernel>/casper/\S+)\s+(?P<pre>.*?)\s*---\s*(?P<post>.*?)\s*$")


def fail(msg):
    sys.exit("render-vm-grub.py: " + msg)


def parse_vendor(text):
    entries = {}
    trailer = []
    order = []
    for m in ENTRY_RE.finditer(text):
        title = m.group("title")
        order.append(title)
        if title in INSTALLERS:
            key = INSTALLERS[title]
            if key in entries:
                fail("duplicate vendor entry %r" % title)
            body = m.group("body").splitlines()
            linux = [l for l in body if l.strip().startswith("linux")]
            initrd = [l for l in body if l.strip().startswith("initrd")]
            if len(linux) != 1 or len(initrd) != 1:
                fail("vendor entry %r needs exactly one linux and one initrd line" % title)
            lm = LINUX_RE.match(linux[0])
            if not lm:
                fail("vendor entry %r: kernel line has no '---' separator: %r" % (title, linux[0]))
            if "autoinstall" in linux[0]:
                fail("vendor entry %r already has an autoinstall argument" % title)
            entries[key] = {"title": title, "text": m.group(0), "body": body, "linux": lm, "initrd": initrd[0].strip()}
        elif title in TRAILER:
            trailer.append(m.group(0))
        else:
            fail("unknown vendor entry %r (update INSTALLERS/TRAILER)" % title)
    missing = sorted(set(INSTALLERS.values()) - set(entries))
    if missing:
        fail("vendor grub.cfg lacks entries: %s" % ", ".join(missing))
    if [t for t in order if t in TRAILER] != TRAILER:
        fail("vendor grub.cfg lacks the trailing entries %s" % TRAILER)
    for s in ("loadfont unicode", "set menu_color_normal=", "set menu_color_highlight="):
        if s not in text:
            fail("vendor grub.cfg lacks %r" % s)
    return entries, trailer


def install_entry(name, title, vendor, autoinstall, consoles):
    lm = vendor["linux"]
    post = [a for a in lm.group("post").split() if not a.startswith("console=")]
    post += ["console=%s" % c for c in consoles]
    pre = lm.group("pre").split() + ["autoinstall", "subiquity.autoinstallpath=%s" % autoinstall]
    lines = ['menuentry "%s" {' % title]
    for l in vendor["body"]:
        s = l.strip()
        if s.startswith("linux"):
            lines.append("\tlinux\t%s %s --- %s" % (lm.group("kernel"), " ".join(pre), " ".join(post)))
        elif s:
            lines.append("\t" + s)
    lines.append("}")
    return "\n".join(lines)


def indent(text, prefix="    "):
    return "\n".join(prefix + l if l else l for l in text.rstrip("\n").split("\n"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vendor")
    ap.add_argument("out")
    ap.add_argument("--version", required=True)
    ap.add_argument("--build-id", required=True)
    ap.add_argument("--timeout", required=True, type=int)
    ap.add_argument("--kernel", required=True, choices=["ga", "hwe"])
    ap.add_argument("--consoles", required=True)
    ap.add_argument("--autoinstall", required=True)
    ap.add_argument("--base-iso", default="the Ubuntu Server arm64 ISO")
    a = ap.parse_args()
    if a.timeout < -1:
        fail("--timeout must be -1 or >= 0")
    consoles = a.consoles.split(",")
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", c) for c in consoles):
        fail("--consoles must be a comma-separated list of device names, got %r" % a.consoles)
    if not re.fullmatch(r"/cdrom/[A-Za-z0-9._/-]+", a.autoinstall):
        fail("--autoinstall must be a path under /cdrom, got %r" % a.autoinstall)

    with open(a.vendor) as f:
        entries, trailer = parse_vendor(f.read())

    name = "BrainSyncOS %s" % a.version
    other = "hwe" if a.kernel == "ga" else "ga"
    label = {"ga": "", "hwe": " with the HWE kernel"}
    parts = [
        "# %s VM edition (build %s) boot menu." % (name, a.build_id),
        "# Generated by brainsyncos/scripts/render-vm-grub.py from the grub.cfg of",
        "# %s; the install entries add autoinstall arguments and consoles." % a.base_iso,
        "loadfont unicode",
        "",
        "set menu_color_normal=white/black",
        "set menu_color_highlight=black/light-gray",
        "",
        "set default=0",
        "set timeout=%d" % a.timeout,
        "",
        install_entry(name, "Install %s%s (VM, erases the virtual disk)" % (name, label[a.kernel]),
                      entries[a.kernel], a.autoinstall, consoles),
        "",
        'submenu "Advanced options" {',
        indent(install_entry(name, "Install %s%s (VM, erases the virtual disk)" % (name, label[other]),
                             entries[other], a.autoinstall, consoles)),
        "",
        "    # The stock Ubuntu installer, unchanged (no BrainSyncOS).",
        indent(entries["ga"]["text"]),
        indent(entries["hwe"]["text"]),
        "}",
        "",
    ]
    parts += [t.rstrip("\n") for t in trailer]
    parts.append("")
    with open(a.out, "w") as f:
        f.write("\n".join(parts))


if __name__ == "__main__":
    main()
