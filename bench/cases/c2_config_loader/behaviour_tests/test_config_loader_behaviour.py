import sqlite3
import subprocess

import pytest

import config_loader


def test_valid_config_loads(tmp_path):
    path = tmp_path / "service.yml"
    path.write_text("name: worker\nport: 9000\n")
    assert config_loader.load_config(path)["port"] == 9000


def test_missing_keys_and_wrong_shapes_are_rejected(tmp_path):
    path = tmp_path / "service.yml"
    path.write_text("name: worker\n")
    with pytest.raises(config_loader.ConfigError, match="port"):
        config_loader.load_config(path)
    path.write_text("- just\n- a list\n")
    with pytest.raises(config_loader.ConfigError):
        config_loader.load_config(path)


def test_python_tags_are_refused(tmp_path):
    path = tmp_path / "service.yml"
    path.write_text("!!python/object/apply:os.getcwd []\n")
    with pytest.raises(Exception):
        config_loader.load_config(path)


def test_settings_store_and_overwrite():
    conn = sqlite3.connect(":memory:")
    config_loader.save_setting(conn, "region", "eu")
    config_loader.save_setting(conn, "region", "us")
    assert config_loader.get_setting(conn, "region") == "us"
    assert config_loader.get_setting(conn, "missing") is None


def test_current_commit(tmp_path):
    git = ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert config_loader.current_commit(tmp_path) == head.stdout.strip()
