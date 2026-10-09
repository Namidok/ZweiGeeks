import sqlite3

import pytest

from config_loader import ConfigError, get_setting, load_config, save_setting


def test_load_config(tmp_path):
    path = tmp_path / "service.yml"
    path.write_text("name: api\nport: 8080\ndebug: false\n")
    assert load_config(path) == {"name": "api", "port": 8080, "debug": False}


def test_load_config_rejects_bad_ports(tmp_path):
    path = tmp_path / "service.yml"
    path.write_text("name: api\nport: 70000\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_settings_round_trip():
    conn = sqlite3.connect(":memory:")
    save_setting(conn, "theme", "dark")
    save_setting(conn, "theme", "light")
    assert get_setting(conn, "theme") == "light"
