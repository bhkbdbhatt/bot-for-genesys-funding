"""Config bootstrap: search order, auto-create and the missing-config guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

import config_loader
from config_loader import Config, ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config.example.yaml"


def _fake_install(tmp_path: Path, *, with_example: bool) -> Path:
    package_dir = tmp_path / "site-packages"
    package_dir.mkdir()
    (package_dir / "config_loader.py").write_text("", encoding="utf-8")
    if with_example:
        (package_dir / "config.example.yaml").write_text(
            EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return package_dir


def test_bootstrap_copies_example_beside_the_code(tmp_path, monkeypatch):
    package_dir = _fake_install(tmp_path, with_example=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_loader, "__file__", str(package_dir / "config_loader.py"))

    config = Config.load()

    created = package_dir / "config.yaml"
    assert created.is_file()
    assert config.file_path == created


def test_missing_config_points_at_the_current_directory(tmp_path, monkeypatch):
    package_dir = _fake_install(tmp_path, with_example=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_loader, "__file__", str(package_dir / "config_loader.py"))

    with pytest.raises(ConfigError) as excinfo:
        Config.load()

    message = str(excinfo.value)
    assert str(tmp_path / "config.yaml") in message
    assert "config.example.yaml" in message
    assert "https://github.com/bhkbdbhatt/bot-for-opensource-funding" in message
    assert not (tmp_path / "config.yaml").exists()
    assert not (package_dir / "config.yaml").exists()


def test_cwd_config_wins_over_the_installed_directory(tmp_path, monkeypatch):
    package_dir = _fake_install(tmp_path, with_example=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_loader, "__file__", str(package_dir / "config_loader.py"))
    (tmp_path / "config.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    config = Config.load()

    assert config.file_path == tmp_path / "config.yaml"
