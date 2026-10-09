"""yaml.load without a Loader: on PyYAML 6 it crashes, on older versions it
runs arbitrary Python from the settings file. A fix must parse plain YAML
and refuse Python object tags."""

import pytest
import yaml

import app


def test_load_settings_parses_plain_yaml_safely(tmp_path):
    plain = tmp_path / "settings.yml"
    plain.write_text("name: shop\nport: 8080\n")
    assert app.load_settings(str(plain)) == {"name": "shop", "port": 8080}

    evil = tmp_path / "evil.yml"
    evil.write_text("!!python/object/apply:os.getcwd []\n")
    with pytest.raises(yaml.YAMLError):
        app.load_settings(str(evil))
