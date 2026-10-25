"""Where BrainSync looks for data on disk.

Each place is the environment variable named below if it is set. Otherwise it is the folder
in a source checkout, or, when installed (BrainSyncOS packages), the system place:

    BRAINSYNC_DATA_DIR     recordings          <repo>/data      ~/brainsync_data
    BRAINSYNC_CONFIG_DIR   BCI Voice YAMLs     <repo>/config    /etc/brainsync
    BRAINSYNC_UI_DIR       partner page        <repo>/ui        /usr/share/brainsync/ui
    BRAINSYNC_CLIENTS_DIR  server's clients    <repo>/clients   /usr/share/brainsync/clients
    BRAINSYNC_ENV_FILE     API keys            <repo>/.env      /etc/brainsync/secrets.env
    BRAINSYNC_STATE_DIR    profile, TTS cache  <repo>           $XDG_STATE_HOME/brainsync
    BRAINSYNC_LOG_DIR      session logs        <state>/logs     <state>/logs
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ENV_VAR = "BRAINSYNC_DATA_DIR"
CONFIG_DIR_VAR = "BRAINSYNC_CONFIG_DIR"
UI_DIR_VAR = "BRAINSYNC_UI_DIR"
CLIENTS_DIR_VAR = "BRAINSYNC_CLIENTS_DIR"
ENV_FILE_VAR = "BRAINSYNC_ENV_FILE"
STATE_DIR_VAR = "BRAINSYNC_STATE_DIR"
LOG_DIR_VAR = "BRAINSYNC_LOG_DIR"
REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_DATA = Path.home() / "brainsync_data"   # data root for a non-editable install
INSTALLED_CONFIG = Path("/etc/brainsync")
INSTALLED_SHARE = Path("/usr/share/brainsync")


def in_checkout(repo: Path = REPO_ROOT) -> bool:
    """True when the package runs from a source checkout, not from site-packages."""
    return (repo / "pyproject.toml").is_file()


def _from_env(var: str, checkout: Path, installed: Path, repo: Path = REPO_ROOT) -> Path:
    env = os.environ.get(var, "").strip()
    if env:
        return Path(env).expanduser()
    return checkout if in_checkout(repo) else installed


def _default_root(repo: Path = REPO_ROOT) -> Path:
    """<repo>/data in a source checkout; ~/brainsync_data when installed into site-packages."""
    return repo / "data" if in_checkout(repo) else INSTALLED_DATA


def config_dir(repo: Path = REPO_ROOT) -> Path:
    """BCI Voice's default.yaml, intents.yaml and profile.yaml."""
    return _from_env(CONFIG_DIR_VAR, repo / "config", INSTALLED_CONFIG, repo)


def ui_dir(repo: Path = REPO_ROOT) -> Path:
    """BCI Voice's partner page (static files)."""
    return _from_env(UI_DIR_VAR, repo / "ui", INSTALLED_SHARE / "ui", repo)


def clients_dir(repo: Path = REPO_ROOT) -> Path:
    """The web and JS clients that ``python -m brainsync.server`` serves."""
    return _from_env(CLIENTS_DIR_VAR, repo / "clients", INSTALLED_SHARE / "clients", repo)


def env_file(repo: Path = REPO_ROOT) -> Path:
    """KEY=VALUE file with API keys; never committed, never built into an image."""
    return _from_env(ENV_FILE_VAR, repo / ".env", INSTALLED_CONFIG / "secrets.env", repo)


def state_dir(repo: Path = REPO_ROOT) -> Path:
    """Files the apps write and keep: the user's profile, the TTS clip cache."""
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    installed = (Path(xdg) if xdg else Path.home() / ".local" / "state") / "brainsync"
    return _from_env(STATE_DIR_VAR, repo, installed, repo)


def log_dir(repo: Path = REPO_ROOT) -> Path:
    """BCI Voice's JSONL session logs."""
    state = state_dir(repo)
    return _from_env(LOG_DIR_VAR, state / "logs", state / "logs", repo)


def data_dir() -> Path:
    """Data root: $BRAINSYNC_DATA_DIR if set, else <repo>/data (see _default_root)."""
    env = os.environ.get(ENV_VAR, "").strip()
    return Path(env).expanduser() if env else _default_root()


def recordings_dir() -> Path:
    """Own EEG recordings written by the record tool."""
    return data_dir() / "spectrum_checks"


def nakanishi_dir() -> Path:
    """Nakanishi et al. 2015 public 12-target dataset (s1.mat .. s10.mat)."""
    return data_dir() / "nakanishi2015"


def heatmaps_dir() -> Path:
    """TRIBE v2 predictions per demo clip, written by brainsync.heatmap predict."""
    return data_dir() / "heatmaps"


def unwritable_reason(directory: Path) -> str:
    """'' when a file can be created in ``directory`` (created if missing), else why not."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".brainsync_probe_"):
            pass
    except OSError as exc:
        return f"{exc.strerror or exc}"
    return ""
