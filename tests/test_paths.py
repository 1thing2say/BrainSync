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


APP_DIRS = [  # (function, its variable, folder in a checkout, place when installed)
    (paths.config_dir, paths.CONFIG_DIR_VAR, "config", Path("/etc/brainsync")),
    (paths.ui_dir, paths.UI_DIR_VAR, "ui", Path("/usr/share/brainsync/ui")),
    (paths.clients_dir, paths.CLIENTS_DIR_VAR, "clients", Path("/usr/share/brainsync/clients")),
    (paths.env_file, paths.ENV_FILE_VAR, ".env", Path("/etc/brainsync/secrets.env")),
]


@pytest.fixture
def no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (paths.CONFIG_DIR_VAR, paths.UI_DIR_VAR, paths.CLIENTS_DIR_VAR, paths.ENV_FILE_VAR,
                paths.STATE_DIR_VAR, paths.LOG_DIR_VAR, "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("where, var, in_repo, installed", APP_DIRS,
                         ids=[d[0].__name__ for d in APP_DIRS])
def test_app_dirs_checkout_installed_and_override(where, var, in_repo, installed, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch,
                                                  no_overrides: None) -> None:
    assert where() == paths.REPO_ROOT / in_repo
    site = tmp_path / "site-packages"
    site.mkdir()
    assert where(site) == installed
    monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
    assert where() == where(site) == tmp_path / "elsewhere"


def test_state_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_overrides: None) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()
    assert paths.state_dir() == paths.REPO_ROOT
    assert paths.log_dir() == paths.REPO_ROOT / "logs"
    assert paths.state_dir(site) == Path.home() / ".local" / "state" / "brainsync"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert paths.state_dir(site) == tmp_path / "xdg" / "brainsync"
    assert paths.log_dir(site) == tmp_path / "xdg" / "brainsync" / "logs"
    monkeypatch.setenv(paths.STATE_DIR_VAR, str(tmp_path / "state"))
    assert paths.log_dir() == tmp_path / "state" / "logs"   # logs follow the state folder
    monkeypatch.setenv(paths.LOG_DIR_VAR, str(tmp_path / "log"))
    assert paths.log_dir() == tmp_path / "log"


def test_installed_places_never_point_into_site_packages(tmp_path: Path, no_overrides: None) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()
    for where in (paths.config_dir, paths.ui_dir, paths.clients_dir, paths.env_file,
                  paths.state_dir, paths.log_dir):
        assert site != where(site) and site not in where(site).parents, where.__name__


def test_unwritable_reason(tmp_path: Path) -> None:
    assert paths.unwritable_reason(tmp_path / "new" / "dir") == ""
    assert (tmp_path / "new" / "dir").is_dir()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert paths.unwritable_reason(blocker / "sub") != ""
