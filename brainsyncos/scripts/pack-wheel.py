#!/usr/bin/env python3
"""pack-wheel.py - write a Python wheel from a staged file tree, reproducibly.

Usage: pack-wheel.py --root DIR --name DIST --version VER --tag PY-ABI-PLAT
           --out DIR [--summary TEXT] [--license NAME] [--license-file FILE]
           [--requires FILE] [--epoch SECONDS]

DIR holds the files exactly as they go into site-packages (e.g. brainflow/...).
The wheel gets DIST-VER.dist-info with METADATA, WHEEL, RECORD (sha256 of
every file) and the licence file. Entries are sorted, timestamps are --epoch
(default SOURCE_DATE_EPOCH, else 1980-01-01), modes are 644 (755 if the staged
file is executable), so the same tree gives the same bytes. Root-Is-Purelib
is false whenever the tag's platform is not "any". Prints the wheel's path.
Used by wheels/*/build.sh (scripts/build-wheels.sh).
"""
import argparse
import base64
import hashlib
import os
import re
import sys
import time
import zipfile


def record_hash(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", default="")
    ap.add_argument("--license", default="")
    ap.add_argument("--license-file")
    ap.add_argument("--requires", help="file with one Requires-Dist per line (# comments)")
    ap.add_argument("--epoch", type=int, default=int(os.environ.get("SOURCE_DATE_EPOCH", "315532800")))
    a = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", a.name):
        sys.exit("pack-wheel.py: bad distribution name: %s" % a.name)
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*(\+[a-z0-9]+(\.[a-z0-9]+)*)?", a.version):
        sys.exit("pack-wheel.py: version must be N.N.N[+local], got %s" % a.version)
    parts = a.tag.split("-")
    if len(parts) != 3 or not all(parts):
        sys.exit("pack-wheel.py: tag must be PY-ABI-PLATFORM, got %s" % a.tag)
    dist = re.sub(r"[-_.]+", "_", a.name).lower()
    info = "%s-%s.dist-info" % (dist, a.version)
    wheel_path = os.path.join(a.out, "%s-%s-%s.whl" % (dist, a.version, a.tag))

    files = []
    for top, dirs, names in os.walk(a.root):
        dirs.sort()
        for n in names:
            p = os.path.join(top, n)
            if os.path.islink(p) or not os.path.isfile(p):
                sys.exit("pack-wheel.py: not a regular file: %s" % p)
            rel = os.path.relpath(p, a.root).replace(os.sep, "/")
            if rel.endswith(".dist-info") or ".dist-info/" in rel or "/__pycache__/" in "/" + rel:
                sys.exit("pack-wheel.py: unexpected file in the tree: %s" % rel)
            files.append(rel)
    if not files:
        sys.exit("pack-wheel.py: %s is empty" % a.root)
    files.sort()

    requires = []
    if a.requires:
        with open(a.requires) as f:
            requires = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    meta = ["Metadata-Version: 2.1", "Name: %s" % a.name, "Version: %s" % a.version]
    if a.summary:
        meta.append("Summary: %s" % a.summary)
    if a.license:
        meta.append("License: %s" % a.license)
    extra = []
    if a.license_file:
        with open(a.license_file, "rb") as f:
            extra.append((os.path.basename(a.license_file), f.read()))
        meta.append("License-File: %s" % os.path.basename(a.license_file))
    meta += ["Requires-Dist: %s" % r for r in requires]
    extra.append(("METADATA", ("\n".join(meta) + "\n").encode()))
    entries = [(rel, None) for rel in files]
    purelib = "true" if parts[2] == "any" else "false"
    extra.append(("WHEEL", ("Wheel-Version: 1.0\nGenerator: brainsyncos pack-wheel.py\n"
                            "Root-Is-Purelib: %s\nTag: %s\n" % (purelib, a.tag)).encode()))
    entries += [("%s/%s" % (info, n), data) for n, data in sorted(extra)]

    stamp = time.gmtime(max(a.epoch, 315532800))[:6]
    os.makedirs(a.out, exist_ok=True)
    tmp = wheel_path + ".tmp"
    record = []
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, data in entries:
            mode = 0o644
            if data is None:
                src = os.path.join(a.root, rel)
                with open(src, "rb") as f:
                    data = f.read()
                if os.stat(src).st_mode & 0o111:
                    mode = 0o755
            zi = zipfile.ZipInfo(rel, date_time=stamp)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3
            zi.external_attr = (0o100000 | mode) << 16
            z.writestr(zi, data)
            record.append("%s,%s,%d" % (rel, record_hash(data), len(data)))
        rec = "%s/RECORD" % info
        record.append("%s,," % rec)
        zi = zipfile.ZipInfo(rec, date_time=stamp)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.create_system = 3
        zi.external_attr = (0o100000 | 0o644) << 16
        z.writestr(zi, ("\n".join(record) + "\n").encode())
    os.replace(tmp, wheel_path)
    print(wheel_path)


if __name__ == "__main__":
    main()
