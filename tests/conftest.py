"""Shared fixtures and the skip logic for tests that need real data.

Mark a test ``@pytest.mark.realdata`` to need the own recordings (data_dir()/spectrum_checks),
or ``@pytest.mark.realdata("nakanishi2015/s1.mat", ...)`` to name the paths it needs,
relative to data_dir(). Such tests are skipped when the paths are absent, so the suite
passes without BRAINSYNC_DATA_DIR. The fixtures below skip the same way.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from brainsync import paths
from brainsync.recordings import list_recordings

DEFAULT_REALDATA = ("spectrum_checks",)


def _present(rel: str) -> bool:
    p = paths.data_dir() / rel
    if p.is_dir():
        return any(p.glob("*.npz")) or any(p.glob("*.mat"))
    return p.is_file()


def missing_data(*rel: str) -> list[str]:
    """Paths (relative to data_dir()) that are absent; empty when all are present."""
    return [r for r in (rel or DEFAULT_REALDATA) if not _present(r)]


def require_data(*rel: str) -> Path:
    """Skip the calling test unless the named data paths exist; returns data_dir()."""
    absent = missing_data(*rel)
    if absent:
        pytest.skip(f"real data absent under {paths.data_dir()}: {', '.join(absent)}")
    return paths.data_dir()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        marker = item.get_closest_marker("realdata")
        if marker is None:
            continue
        absent = missing_data(*marker.args)
        if absent:
            item.add_marker(pytest.mark.skip(
                reason=f"real data absent under {paths.data_dir()}: {', '.join(absent)}"))


@pytest.fixture
def data_root() -> Path:
    """data_dir() when the own recordings exist, else skip."""
    return require_data(*DEFAULT_REALDATA)


@pytest.fixture
def real_recordings() -> list[Path]:
    """All *_trials.npz and *_cued.npz recordings, or skip."""
    require_data(*DEFAULT_REALDATA)
    found = list_recordings()
    if not found:
        pytest.skip(f"no recordings in {paths.recordings_dir()}")
    return found


@pytest.fixture
def nakanishi_root() -> Path:
    """The Nakanishi 2015 directory when s1.mat exists, else skip."""
    require_data("nakanishi2015/s1.mat")
    return paths.nakanishi_dir()


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(1234)


@pytest.fixture(autouse=True)
def _no_spoken_prompts(monkeypatch):
    """No test reaches Cartesia or plays audio: brainsync.voice stays silent."""
    monkeypatch.setenv("BRAINSYNC_TTS", "off")
