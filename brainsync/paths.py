"""Where BrainSync looks for data on disk."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ENV_VAR = "BRAINSYNC_DATA_DIR"
REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_DATA = Path.home() / "brainsync_data"   # data root for a non-editable install


def _default_root(repo: Path = REPO_ROOT) -> Path:
    """<repo>/data in a source checkout; ~/brainsync_data when installed into site-packages."""
    return repo / "data" if (repo / "pyproject.toml").is_file() else INSTALLED_DATA


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


def models_dir() -> Path:
    """Trained classifier models written by brainsync.train."""
    return data_dir() / "models"


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
