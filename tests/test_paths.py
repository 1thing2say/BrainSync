"""Data-root resolution and the shared writability probe."""
from __future__ import annotations

from pathlib import Path

import pytest

from brainsync import paths


def test_env_var_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path))
    assert paths.data_dir() == tmp_path
    assert paths.recordings_dir() == tmp_path / "spectrum_checks"
    assert paths.nakanishi_dir() == tmp_path / "nakanishi2015"
    assert paths.models_dir() == tmp_path / "models"


def test_models_dir_sits_under_the_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.ENV_VAR, raising=False)
    assert paths.models_dir() == paths.data_dir() / "models"


def test_source_checkout_uses_repo_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.ENV_VAR, raising=False)
    assert (paths.REPO_ROOT / "pyproject.toml").is_file()
    assert paths.data_dir() == paths.REPO_ROOT / "data"


def test_installed_copy_never_points_into_site_packages(tmp_path: Path) -> None:
    # regression: a non-editable install resolved the data root to site-packages/data
    site = tmp_path / "site-packages"
    site.mkdir()
    assert paths._default_root(site) == paths.INSTALLED_DATA
    assert site not in paths.INSTALLED_DATA.parents
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\n")
    assert paths._default_root(checkout) == checkout / "data"


def test_unwritable_reason(tmp_path: Path) -> None:
    assert paths.unwritable_reason(tmp_path / "new" / "dir") == ""
    assert (tmp_path / "new" / "dir").is_dir()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert paths.unwritable_reason(blocker / "sub") != ""
