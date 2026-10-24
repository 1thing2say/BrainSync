#!/usr/bin/env python3
"""merge-md5sums.py - md5sum.txt for a remastered ISO.

Usage: merge-md5sums.py VENDOR_MD5SUM_TXT STAGE_DIR [--map SRC ISO_PATH]...
           > STAGE_DIR/md5sum.txt

Keeps the vendor's entry for every file the remaster does not touch and
hashes every regular file under STAGE_DIR (the files mapped over the base
ISO) and under each extra tree SRC (mapped at ISO_PATH, e.g. the package
repository at /brainsyncos-repo), in the vendor's format ("<md5>  ./path")
and order. NVIDIA's file is
sorted bytewise by path (`find . -type f | LC_ALL=C sort`); then the whole
output is, with new paths in their sorted place. Ubuntu's file has its own
order; then the vendor entries keep it (a replaced file keeps its line) and
new paths follow at the end, sorted. md5sum.txt and boot/boot.cat are never
listed, as in the vendor files. With an empty STAGE_DIR the output is
byte-identical to the vendor file.
"""

import hashlib
import os
import sys

SKIP = {"./md5sum.txt", "./boot/boot.cat"}


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    args = sys.argv[1:]
    usage = "usage: merge-md5sums.py VENDOR_MD5SUM_TXT STAGE_DIR [--map SRC ISO_PATH]..."
    if len(args) < 2 or (len(args) - 2) % 3:
        sys.exit(usage)
    vendor, stage = args[0], args[1]
    trees = [(stage, ".")]
    for i in range(2, len(args), 3):
        if args[i] != "--map" or not args[i + 2].startswith("/") or args[i + 2] == "/":
            sys.exit(usage)
        if not os.path.isdir(args[i + 1]):
            sys.exit("merge-md5sums.py: --map source %s is not a directory" % args[i + 1])
        trees.append((args[i + 1], "." + args[i + 2].rstrip("/")))
    entries = {}
    order = []
    with open(vendor, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if len(line) < 36 or line[32:34] != "  " or not line[34:].startswith("./"):
                sys.exit("merge-md5sums.py: %s:%d: not an md5sum line: %r" % (vendor, n, line))
            entries[line[34:]] = line[:32]
            order.append(line[34:])
    for top, prefix in trees:
        for root, _dirs, files in os.walk(top):
            for name in files:
                full = os.path.join(root, name)
                rel = prefix + "/" + os.path.relpath(full, top)
                if rel in SKIP or os.path.islink(full):
                    continue
                entries[rel] = md5(full)
    def bytewise(paths):
        return sorted(paths, key=lambda p: p.encode("utf-8"))
    if order == bytewise(order):
        paths = bytewise(entries)
    else:
        known = set(order)
        paths = order + bytewise(p for p in entries if p not in known)
    out = sys.stdout
    for path in paths:
        out.write("%s  %s\n" % (entries[path], path))


if __name__ == "__main__":
    main()
