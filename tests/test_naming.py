"""Project identity and current environment contract; fully offline."""
import importlib.util
import tomllib
from pathlib import Path

from remi.settings import Settings, load_environment


def test_package_distribution_and_command_have_one_current_name():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["name"] == "remi"
    assert project["project"]["scripts"] == {"remi": "remi.cli:main"}
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/remi"]
    assert importlib.util.find_spec("remi") is not None
    assert not (Path(__file__).parents[1] / "src" / "embergraph").exists()


def test_current_settings_read_remi_environment(monkeypatch):
    monkeypatch.setenv("REMI_ADD_TIMEOUT", "37")
    monkeypatch.setenv("REMI_HOT_ENABLED", "0")
    monkeypatch.setenv("EMBERGRAPH_ADD_TIMEOUT", "99")
    monkeypatch.setenv("EMBERGRAPH_HOT_ENABLED", "1")
    assert Settings.from_environment().add_timeout == 37
    assert Settings.from_environment().hot_enabled is False


def test_current_configuration_selects_remi_env_file(monkeypatch, tmp_path):
    from unittest.mock import patch
    path = tmp_path / "private.env"
    monkeypatch.setenv("REMI_ENV_FILE", str(path))
    monkeypatch.delenv("REMI_NAMING_TEST", raising=False)
    with patch("pathlib.Path.read_text", return_value="REMI_NAMING_TEST=loaded\n"):
        load_environment()
    assert __import__("os").environ["REMI_NAMING_TEST"] == "loaded"
    monkeypatch.delenv("REMI_NAMING_TEST", raising=False)
