#!/usr/bin/env python3
"""mirror.py - third-party .debs that BrainSyncOS mirrors into its repository (builder only).

Usage: python3 -I scripts/mirror.py lock|fetch|check|files
           --mirror mirror/NAME --cache build/downloads/mirror/NAME
           --keyring FILE [--debs DIR]

mirror/NAME/mirror.env names an apt repository (MIRROR_URL, MIRROR_SUITE,
MIRROR_COMPONENT, MIRROR_ARCH), the packages wanted from it (MIRROR_ROOTS,
NAME=VERSION, comma-separated) and names none of them may be, provide or
depend on (MIRROR_FORBID, name prefixes). mirror/NAME/debs.lock pins every
.deb of their closure: "PACKAGE VERSION ARCH SIZE SHA256 FILENAME" per line.
--keyring is the repository's key as a binary keyring for gpgv; the caller
(scripts/fetch-mirror.sh, scripts/build-repo.sh) has checked its fingerprint.

The closure is computed from the signed index: InRelease must verify with
gpgv against --keyring, only the signed text is read, and the Packages index
must have the size and sha256 that text gives it. From each root, every
Depends and Pre-Depends group is followed: the first alternative the index
has a real package for wins (the locked version if it still satisfies the
relation, else the highest that does); a group none of whose alternatives
the index has is left to the target system and listed as external. A
virtual name (only a Provides in the index) is refused: pin a real package.

  lock   download InRelease and the index, resolve the closure afresh and
         write debs.lock; then fetch
  fetch  check the cached InRelease and index (download them again only when
         they or a .deb are missing or broken), that the closure equals debs.lock, and download
         each missing .deb into --cache, checking its size and sha256
  check  offline: the cached InRelease and index verify, the closure equals
         debs.lock, and every pinned .deb in --debs (default --cache) has its
         size and sha256
  files  print the pinned .debs' file names, one per line
"""

import argparse
import gzip
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

LOCK_HEADER = """\
# debs.lock: the .debs this mirror ships, pinned by sha256. Written by
# scripts/fetch-mirror.sh --lock (scripts/mirror.py) from the signed index of
# {url} {suite} {component} ({arch}).
# PACKAGE VERSION ARCH SIZE SHA256 FILENAME
"""


def die(msg):
    sys.exit("mirror.py: ERROR: %s" % msg)


def read_env(path):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key] = value
    return env


def deb822(text):
    """Stanzas of a deb822 file as dicts; continuation lines are kept."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        d, key = {}, None
        for line in block.splitlines():
            if line.startswith((" ", "\t")) and key:
                d[key] += "\n" + line
            elif ":" in line:
                key, _, value = line.partition(":")
                d[key] = value.strip()
        if d:
            out.append(d)
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, size=None, sha256=None):
    """url -> dest via dest.part; checks size and sha256 when given."""
    part = dest + ".part"
    h, n = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                f.write(chunk)
                h.update(chunk)
                n += len(chunk)
    except OSError as e:
        if os.path.exists(part):
            os.unlink(part)
        die("cannot download %s: %s" % (url, e))
    if (size is not None and n != size) or (sha256 is not None and h.hexdigest() != sha256):
        os.unlink(part)
        die("%s: got %d bytes, sha256 %s; want %s bytes, sha256 %s" % (url, n, h.hexdigest(), size, sha256))
    os.replace(part, dest)


def version_ok(have, op, want):
    """dpkg --compare-versions HAVE OP WANT (op as in Depends: << <= = >= >>)."""
    return subprocess.run(["dpkg", "--compare-versions", have, op, want]).returncode == 0


def newest(versions):
    best = None
    for v in versions:
        if best is None or version_ok(v, ">>", best):
            best = v
    return best


class Mirror:
    def __init__(self, args):
        self.dir = args.mirror
        self.cache = args.cache
        self.keyring = args.keyring
        self.env = read_env(os.path.join(self.dir, "mirror.env"))
        for key in ("MIRROR_URL", "MIRROR_SUITE", "MIRROR_COMPONENT", "MIRROR_ARCH", "MIRROR_ROOTS", "MIRROR_FORBID"):
            if not self.env.get(key):
                die("%s/mirror.env: %s is missing" % (self.dir, key))
        self.url = self.env["MIRROR_URL"].rstrip("/")
        self.suite = self.env["MIRROR_SUITE"]
        self.index_path = "%s/binary-%s/Packages.gz" % (self.env["MIRROR_COMPONENT"], self.env["MIRROR_ARCH"])
        self.forbid = [p for p in self.env["MIRROR_FORBID"].split(",") if p]
        self.roots = []
        for spec in self.env["MIRROR_ROOTS"].split(","):
            name, _, version = spec.partition("=")
            if not name or not version:
                die("MIRROR_ROOTS: '%s' is not NAME=VERSION" % spec)
            self.roots.append((name, version))
        self.lock_file = os.path.join(self.dir, "debs.lock")
        os.makedirs(self.cache, exist_ok=True)

    # --- the signed index -----------------------------------------------------------
    def fetch_index(self):
        base = "%s/dists/%s" % (self.url, self.suite)
        download(base + "/InRelease", os.path.join(self.cache, "InRelease"))
        files = self.release_files()
        size, sha = files[self.index_path]
        download("%s/%s" % (base, self.index_path), os.path.join(self.cache, "Packages.gz"), size, sha)

    def release_files(self):
        """The verified InRelease text's SHA256 entries: {path: (size, sha256)}."""
        inrel = os.path.join(self.cache, "InRelease")
        if not os.path.isfile(inrel):
            die("no cached InRelease in %s (run scripts/fetch-mirror.sh)" % self.cache)
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, "Release")
            r = subprocess.run(["gpgv", "--keyring", self.keyring, "--output", out, inrel],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if r.returncode != 0:
                die("%s does not verify against %s:\n%s" % (inrel, self.keyring, r.stdout))
            rel = deb822(open(out, encoding="utf-8").read())[0]
        if self.suite not in (rel.get("Codename"), rel.get("Suite")):
            die("InRelease is for %s/%s, not %s" % (rel.get("Suite"), rel.get("Codename"), self.suite))
        files = {}
        for line in rel.get("SHA256", "").splitlines():
            f = line.split()
            if len(f) == 3:
                files[f[2]] = (int(f[1]), f[0])
        if self.index_path not in files:
            die("InRelease lists no %s" % self.index_path)
        self.release_date = rel.get("Date", "?")
        return files

    def index(self):
        """{package: [stanza, ...]} from the cached index, after checking it against InRelease."""
        size, sha = self.release_files()[self.index_path]
        p = os.path.join(self.cache, "Packages.gz")
        if not os.path.isfile(p) or os.path.getsize(p) != size or sha256_file(p) != sha:
            die("the cached %s does not match the signed InRelease (run scripts/fetch-mirror.sh --lock)" % self.index_path)
        db = {}
        for st in deb822(gzip.open(p).read().decode("utf-8")):
            db.setdefault(st["Package"], []).append(st)
        return db

    # --- the closure ----------------------------------------------------------------
    @staticmethod
    def relations(st):
        """Depends + Pre-Depends: a list of groups, each [(name, op, version), ...]."""
        groups = []
        for field in ("Pre-Depends", "Depends"):
            for group in st.get(field, "").split(","):
                alts = []
                for alt in group.split("|"):
                    m = re.match(r"\s*([a-z0-9][a-z0-9+.-]*)(?::\S+)?\s*(?:\(\s*(<<|<=|=|>=|>>)\s*([^)\s]+)\s*\))?\s*$", alt)
                    if not m and alt.strip():
                        die("cannot parse the relation '%s' of %s" % (alt.strip(), st["Package"]))
                    if m:
                        alts.append(m.groups())
                if alts:
                    groups.append(alts)
        return groups

    def forbidden(self, name):
        return any(name.startswith(p) for p in self.forbid)

    def closure(self, db, prefer):
        """{package: stanza} reachable from the roots, and the external relations."""
        provided = {}
        for name, sts in db.items():
            for st in sts:
                for p in st.get("Provides", "").split(","):
                    p = p.strip().split(" ")[0]
                    if p:
                        provided.setdefault(p, set()).add(name)
        chosen, external = {}, set()

        def pick(name, op, version):
            cands = [st for st in db.get(name, []) if op is None or version_ok(st["Version"], op, version)]
            if not cands:
                return None
            want = prefer.get(name)
            for st in cands:
                if st["Version"] == want:
                    return st
            v = newest(st["Version"] for st in cands)
            return next(st for st in cands if st["Version"] == v)

        todo = []
        for name, version in self.roots:
            st = pick(name, "=", version)
            if st is None:
                die("the index has no %s %s" % (name, version))
            todo.append(st)
        while todo:
            st = todo.pop()
            name = st["Package"]
            if name in chosen:
                if chosen[name]["Version"] != st["Version"]:
                    die("%s is needed at %s and at %s" % (name, chosen[name]["Version"], st["Version"]))
                continue
            chosen[name] = st
            for group in self.relations(st):
                for alt_name, op, version in group:
                    if self.forbidden(alt_name):
                        die("%s depends on %s, which MIRROR_FORBID rules out" % (name, alt_name))
                    if alt_name in db:
                        dep = pick(alt_name, op, version)
                        if dep is None:
                            die("%s needs %s %s %s; the index has %s" % (name, alt_name, op, version,
                                ", ".join(s["Version"] for s in db[alt_name])))
                        todo.append(dep)
                        break
                    if alt_name in provided:
                        die("%s needs %s, a virtual package (provided by %s); pin a real package"
                            % (name, alt_name, ", ".join(sorted(provided[alt_name]))))
                else:
                    external.add(" | ".join(n + (" (%s %s)" % (o, v) if o else "") for n, o, v in group))
        for name, st in chosen.items():
            names = [name] + [p.strip().split(" ")[0] for p in st.get("Provides", "").split(",") if p.strip()]
            bad = [n for n in names if self.forbidden(n)]
            if bad:
                die("%s is or provides %s, which MIRROR_FORBID rules out" % (name, ", ".join(bad)))
            if st.get("Architecture") not in (self.env["MIRROR_ARCH"], "all"):
                die("%s is for %s" % (name, st.get("Architecture")))
        return chosen, external

    # --- the lock -------------------------------------------------------------------
    @staticmethod
    def lock_line(st):
        return "%s %s %s %s %s %s" % (st["Package"], st["Version"], st["Architecture"], st["Size"], st["SHA256"], st["Filename"])

    def read_lock(self):
        if not os.path.isfile(self.lock_file):
            die("no %s (run scripts/fetch-mirror.sh --lock)" % self.lock_file)
        entries = []
        for n, line in enumerate(open(self.lock_file, encoding="utf-8"), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            f = line.split()
            if len(f) != 6 or not re.fullmatch(r"[0-9]+", f[3]) or not re.fullmatch(r"[0-9a-f]{64}", f[4]) \
                    or "/" in os.path.basename(f[5]) or not f[5].startswith("pool/"):
                die("%s:%d: not 'PACKAGE VERSION ARCH SIZE SHA256 pool/...': %s" % (self.lock_file, n, line))
            entries.append(f)
        if not entries:
            die("%s pins nothing" % self.lock_file)
        return entries

    def resolve(self, db, fresh):
        prefer = {} if fresh else {e[0]: e[1] for e in self.read_lock()}
        chosen, external = self.closure(db, prefer)
        lines = sorted(self.lock_line(st) for st in chosen.values())
        return chosen, external, lines

    def match_lock(self, lines):
        locked = sorted(" ".join(e) for e in self.read_lock())
        if locked != lines:
            gone = sorted(set(locked) - set(lines))
            new = sorted(set(lines) - set(locked))
            die("debs.lock differs from the closure of MIRROR_ROOTS in the signed index (run scripts/fetch-mirror.sh --lock)\n"
                + "".join("  locked only: %s\n" % l for l in gone) + "".join("  index only:  %s\n" % l for l in new))

    def summary(self, chosen, external):
        size = sum(int(st["Size"]) for st in chosen.values())
        inst = sum(int(st.get("Installed-Size", 0)) for st in chosen.values())
        print("%s %s (InRelease of %s): %d .debs, %.1f MB download, %.1f MB installed: %s"
              % (self.url, self.suite, self.release_date, len(chosen), size / 1e6, inst * 1024 / 1e6,
                 ", ".join("%s=%s" % (n, chosen[n]["Version"]) for n in sorted(chosen))))
        print("relations left to the target system: %s" % ("; ".join(sorted(external)) or "none"))

    # --- the .debs ------------------------------------------------------------------
    def deb_ok(self, d, entry):
        p = os.path.join(d, os.path.basename(entry[5]))
        return os.path.isfile(p) and os.path.getsize(p) == int(entry[3]) and sha256_file(p) == entry[4]

    def fetch_debs(self):
        for e in self.read_lock():
            if self.deb_ok(self.cache, e):
                print("cached: %s" % os.path.basename(e[5]))
                continue
            print("downloading %s (%d bytes)" % (e[5], int(e[3])), flush=True)
            download("%s/%s" % (self.url, e[5]), os.path.join(self.cache, os.path.basename(e[5])), int(e[3]), e[4])

    def check_debs(self, d):
        bad = [os.path.basename(e[5]) for e in self.read_lock() if not self.deb_ok(d, e)]
        if bad:
            die("missing in %s, or not the pinned size and sha256: %s" % (d, ", ".join(bad)))
        print("%d pinned .debs in %s match their size and sha256" % (len(self.read_lock()), d))


def main():
    ap = argparse.ArgumentParser(description="third-party .debs mirrored into the BrainSyncOS repository")
    ap.add_argument("command", choices=("lock", "fetch", "check", "files"))
    ap.add_argument("--mirror", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--keyring", required=True)
    ap.add_argument("--debs")
    args = ap.parse_args()
    m = Mirror(args)
    if args.command == "files":
        for e in m.read_lock():
            print(os.path.basename(e[5]))
        return
    if args.command == "lock":
        m.fetch_index()
        chosen, external, lines = m.resolve(m.index(), fresh=True)
        with open(m.lock_file + ".tmp", "w", encoding="utf-8") as f:
            f.write(LOCK_HEADER.format(url=m.url, suite=m.suite, component=m.env["MIRROR_COMPONENT"], arch=m.env["MIRROR_ARCH"]))
            f.write("".join(l + "\n" for l in lines))
        os.replace(m.lock_file + ".tmp", m.lock_file)
        print("wrote %s" % m.lock_file)
    elif args.command == "fetch":
        locked = m.read_lock()
        try:
            m.index()
            cached = all(m.deb_ok(m.cache, e) for e in locked)
        except SystemExit as e:  # no, or a broken, cached InRelease or index
            print(e, file=sys.stderr)
            cached = False
        if not cached:
            m.fetch_index()
    chosen, external, lines = m.resolve(m.index(), fresh=False)
    m.match_lock(lines)
    m.summary(chosen, external)
    if args.command in ("lock", "fetch"):
        m.fetch_debs()
        m.check_debs(m.cache)
    else:
        m.check_debs(args.debs or m.cache)


if __name__ == "__main__":
    main()
