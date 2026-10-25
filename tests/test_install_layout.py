"""The apps run from an install, not only from the repo (BrainSyncOS Phase 3b).

The BRAINSYNC_* variables of brainsync/paths.py are read when the modules are imported, so
the wiring is checked in a fresh interpreter. The console scripts in pyproject.toml must each
name a top-level function; it is read from the source, since importing the display apps would
need PsychoPy.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from brainsync import paths, voice

REPO = paths.REPO_ROOT

PROBE = """
import json
from app import config, log, server
from brainsync.server import app as v3
print(json.dumps({
    "config": str(config.DEFAULT_CONFIG_PATH), "intents": str(config.DEFAULT_INTENTS_PATH),
    "env": str(config.DEFAULT_ENV_PATH), "profile": str(config.USER_PROFILE_PATH),
    "logs": str(log.DEFAULT_LOG_DIR), "ui": str(server.UI_DIR), "web": str(v3.WEB_DIR),
    "loads": config.load_config().server.port > 0,
}))
"""


def test_brainsync_variables_reach_every_module(tmp_path: Path) -> None:
    etc, share, state = tmp_path / "etc", tmp_path / "share", tmp_path / "state"
    etc.mkdir()
    for name in ("default.yaml", "intents.yaml", "profile.yaml"):
        (etc / name).write_bytes((REPO / "config" / name).read_bytes())
    env = {**os.environ, paths.CONFIG_DIR_VAR: str(etc), paths.UI_DIR_VAR: str(share / "ui"),
           paths.CLIENTS_DIR_VAR: str(share / "clients"), paths.STATE_DIR_VAR: str(state),
           paths.ENV_FILE_VAR: str(etc / "secrets.env"), "PYTHONPATH": str(REPO)}
    env.pop(paths.LOG_DIR_VAR, None)
    proc = subprocess.run([sys.executable, "-c", PROBE], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got == {
        "config": str(etc / "default.yaml"), "intents": str(etc / "intents.yaml"),
        "env": str(etc / "secrets.env"), "profile": str(state / "profiles" / "user.yaml"),
        "logs": str(state / "logs"), "ui": str(share / "ui"),
        "web": str(share / "clients" / "web"), "loads": True,
    }


def test_console_scripts_name_functions() -> None:
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]
    assert {"brainsync-demo", "brainsync-swipe", "brainchess", "brainsync-voice"} <= set(scripts)
    for name, target in scripts.items():
        module, _, attr = target.partition(":")
        source = REPO.joinpath(*module.split(".")).with_suffix(".py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        assert any(isinstance(node, ast.FunctionDef) and node.name == attr
                   for node in tree.body), f"{name}: no {attr}() in {source.relative_to(REPO)}"


def test_cartesia_key_from_the_installed_secrets_file(tmp_path: Path, monkeypatch) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("CARTESIA_API_KEY=sk-installed\n")
    monkeypatch.setenv(paths.ENV_FILE_VAR, str(secrets))
    monkeypatch.chdir(tmp_path)
    assert voice.find_api_key({}) == "sk-installed"
    assert voice.find_api_key({"CARTESIA_API_KEY": "from-env"}) == "from-env"


def test_debian_stockfish_is_a_fallback() -> None:
    pytest.importorskip("chess")
    from brainsync.brainchess import prior
    assert "/usr/games/stockfish" in prior.FALLBACK_PATHS


KEYS_PROBE = """
import json, os
from app import config
from app.menus import MenuMaker
from brainsync import voice
names = config.load_env()
cfg = config.load_config()
maker = MenuMaker(cfg.menus, cfg.llm, config.load_intents(), config.load_profile(), keys=os.environ)
print(json.dumps({"names": sorted(names), "cartesia": voice.find_api_key() == "sk-car-test",
                  "backends": {b.name: b.why_disabled for b in maker.backends}}))
"""


def test_every_key_from_the_installed_secrets_file(tmp_path: Path) -> None:
    # The file as BrainSyncOS's 70-brainsync.sh writes it (comments), with the keys added the
    # way the owner and 85-llama.sh add them: BCI Voice must see all three.
    secrets = tmp_path / "secrets.env"
    secrets.write_text("# BrainSync API keys\n#ANTHROPIC_API_KEY=\n\nANTHROPIC_API_KEY=sk-ant-test\n"
                       "CARTESIA_API_KEY=sk-car-test\nBRAINSYNC_LLAMA_API_KEY=llama-test\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CARTESIA_API_KEY", "BRAINSYNC_LLAMA_API_KEY")}
    env.update({paths.ENV_FILE_VAR: str(secrets), "PYTHONPATH": str(REPO)})
    proc = subprocess.run([sys.executable, "-c", KEYS_PROBE], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got == {"names": ["ANTHROPIC_API_KEY", "BRAINSYNC_LLAMA_API_KEY", "CARTESIA_API_KEY"],
                   "cartesia": True, "backends": {"local": "", "claude": ""}}
