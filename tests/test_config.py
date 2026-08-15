"""Tests for configuration loading."""

import os

import yaml

from fshub.config import Config


def write_config(tmp_path, data):
    path = tmp_path / 'fshub.yaml'
    path.write_text(yaml.dump(data), encoding='utf-8')
    return path


def test_loads_values(tmp_path):
    path = write_config(tmp_path, {
        'data_path': str(tmp_path / 'data'),
        'listen_ip': '0.0.0.0',
        'listen_port': 1234,
    })

    config = Config(path)

    assert config.data_path == str(tmp_path / 'data')
    assert config.listen_ip == '0.0.0.0'
    assert config.listen_port == 1234
    assert config.config_path == str(path)


def test_string_port_is_accepted(tmp_path):
    config = Config(write_config(tmp_path, {'listen_port': '8080'}))
    assert config.listen_port == 8080


def test_invalid_port_falls_back_to_default(tmp_path, capsys):
    """A typo in the config must not crash the process at startup."""
    config = Config(write_config(tmp_path, {'listen_port': 'not-a-port'}))

    assert config.listen_port == 7303
    assert 'listen_port' in capsys.readouterr().out


def test_broken_yaml_is_reported_not_raised(tmp_path, capsys):
    path = tmp_path / 'fshub.yaml'
    path.write_text('data_path: [unclosed\n', encoding='utf-8')

    config = Config(path)

    assert config.listen_port == 7303
    assert 'Error loading config' in capsys.readouterr().out


def test_missing_file_uses_defaults(tmp_path):
    config = Config(tmp_path / 'nope.yaml')
    assert config.data_path == os.path.expanduser('~/.fshub/')
    assert config.config_path is None


def test_derived_paths(tmp_path):
    config = Config(write_config(tmp_path, {'data_path': str(tmp_path / 'd')}))
    config.ensure_dirs()

    assert config.snapshot_dir == os.path.join(str(tmp_path / 'd'), 'snapshots')
    assert os.path.isdir(config.devices_dir)
    assert os.path.isdir(config.backup_log_dir)


def test_out_of_range_port_falls_back_to_default(tmp_path, capsys):
    config = Config(write_config(tmp_path, {'listen_port': 70000}))

    assert config.listen_port == 7303
    assert 'listen_port' in capsys.readouterr().out


def test_malformed_values_are_reported_not_raised(tmp_path, capsys):
    """A wrong type in the config file must not crash the process at startup."""
    config = Config(write_config(tmp_path, {'data_path': 123, 'listen_ip': []}))

    out = capsys.readouterr().out
    assert config.data_path == os.path.expanduser('~/.fshub/')
    assert config.listen_ip == 'localhost'
    assert 'data_path' in out and 'listen_ip' in out
