"""Read a KEY=VALUE file into the environment, never overriding what is already set.

    names = load_env_file(".env")          # ["ANTHROPIC_API_KEY"] if it was not set before

The server (``python -m brainsync.server --env-file .env``) and ``tools/screen_eval.py`` use
this so that ANTHROPIC_API_KEY can live in a gitignored file instead of the shell. Blank lines
and ``#`` comments are skipped, ``export`` in front of a name is allowed, and matching single
or double quotes around a value are removed. A variable that is already set keeps its value.
Values are never printed: a bad line is reported by its number only, since its text may hold
a secret. Standard library only.
"""
from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from pathlib import Path

__all__ = ["ENV_KEY", "load_env_file"]

ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_env_file(path: str | Path,
                  environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Set KEY=VALUE lines of ``path`` in ``environ`` (os.environ) unless already set.

    Returns the names that were set. A bad line is a ValueError that names the line number
    but never its text. An unreadable file is a ValueError too.
    """
    environ = os.environ if environ is None else environ
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read the env file {path}: {type(exc).__name__}; check the "
                         f"path and that it is a readable UTF-8 text file") from None
    names: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not ENV_KEY.fullmatch(key):
            raise ValueError(f"{path}: line {n} is not KEY=VALUE with a name of letters, "
                             f"digits and _; fix that line")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in environ:
            environ[key] = value
            names.append(key)
    return names
