#!/usr/bin/env python3
"""merge-md5sums.py - md5sum.txt for a remastered ISO.

Usage: merge-md5sums.py VENDOR_MD5SUM_TXT STAGE_DIR > STAGE_DIR/md5sum.txt

Keeps NVIDIA's entry for every file the remaster does not touch and hashes
every regular file under STAGE_DIR (the files mapped over the base ISO), in
the vendor's format ("<md5>  ./path") and order (bytewise by path, i.e.
`find . -type f | LC_ALL=C sort`). md5sum.txt and boot/boot.cat are never
listed, as in the vendor file. With an empty STAGE_DIR the output is
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
    if len(sys.argv) != 3:
        sys.exit("usage: merge-md5sums.py VENDOR_MD5SUM_TXT STAGE_DIR")
    vendor, stage = sys.argv[1], sys.argv[2]
    entries = {}
    with open(vendor, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if len(line) < 36 or line[32:34] != "  " or not line[34:].startswith("./"):
                sys.exit("merge-md5sums.py: %s:%d: not an md5sum line: %r" % (vendor, n, line))
            entries[line[34:]] = line[:32]
    for root, _dirs, files in os.walk(stage):
        for name in files:
            full = os.path.join(root, name)
            rel = "./" + os.path.relpath(full, stage)
            if rel in SKIP or os.path.islink(full):
                continue
            entries[rel] = md5(full)
    out = sys.stdout
    for path in sorted(entries, key=lambda p: p.encode("utf-8")):
        out.write("%s  %s\n" % (entries[path], path))


if __name__ == "__main__":
    main()
