"""Check that brainsync.lock.txt still covers the app's dependencies.

Usage: python3 check-lock.py PYPROJECT LOCK   (with pip's wheel on PYTHONPATH,
for its vendored `packaging`; scripts/venv.sh and packages/brainsync do this)

Every requirement of [project.dependencies] and of the extras the image uses
(display, chess, reels; not dev) must be pinned in the lock at a version that
satisfies it. Prints one line per requirement; exits 1 if any is missing or
out of range, which means: re-lock (brainsyncos/README.md, "The Python runtime").
"""
from __future__ import annotations

import re
import sys
import tomllib

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name

EXTRAS = ("display", "chess", "reels")


def main(pyproject: str, lock: str) -> int:
    project = tomllib.load(open(pyproject, "rb"))["project"]
    reqs = list(project["dependencies"])
    for extra in EXTRAS:
        reqs += project.get("optional-dependencies", {}).get(extra, [])
    pins = {}
    for line in open(lock, encoding="utf-8"):
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ \\]+)", line)
        if m:
            pins[canonicalize_name(m.group(1))] = m.group(2)
    bad = 0
    for text in reqs:
        req = Requirement(text)
        name = canonicalize_name(req.name)
        have = pins.get(name)
        if have is None:
            print(f"MISSING  {text}: not in the lock")
            bad += 1
        elif not req.specifier.contains(have, prereleases=True):
            print(f"OUTDATED {text}: the lock has {have}")
            bad += 1
        else:
            print(f"ok       {text}: {have}")
    print(f"{len(reqs)} requirements (dependencies + {', '.join(EXTRAS)}), {len(pins)} pins, {bad} not covered")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
