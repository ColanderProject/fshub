"""Configuration management for fshub.

Authentication is intentionally NOT handled here: fshub is expected to run
behind a reverse proxy (e.g. nginx with HTTP basic auth) that terminates
authentication before the request reaches this application.
"""

import os
import threading
import yaml
from pathlib import Path

CONFIG_FILENAME = 'fshub.yaml'

DEFAULTS = {
    'data_path': '~/.fshub/',
    'listen_ip': 'localhost',
    'listen_port': 7303,
}


class Config:
    def __init__(self, config_path=None):
        self.data_path = os.path.expanduser(DEFAULTS['data_path'])
        self.listen_ip = DEFAULTS['listen_ip']
        self.listen_port = DEFAULTS['listen_port']
        self.config_path = None
        self.load_config(config_path)

    @staticmethod
    def find_config_path():
        """Return the first existing config file location, or None."""
        candidates = [
            Path(CONFIG_FILENAME),
            Path.home() / '.config' / CONFIG_FILENAME,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def load_config(self, config_path=None):
        """Load configuration from file if it exists."""
        config_path = Path(config_path) if config_path else self.find_config_path()
        if not config_path or not config_path.exists():
            return

        self.config_path = str(config_path)
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            print(f"Error loading config {config_path}: {e}")
            return

        if not isinstance(config_data, dict):
            return

        self.data_path = os.path.expanduser(config_data.get('data_path', self.data_path))
        self.listen_ip = config_data.get('listen_ip', self.listen_ip)

        # A malformed port must not take the whole process down: every other
        # problem in this file is reported and then ignored.
        listen_port = config_data.get('listen_port', self.listen_port)
        try:
            self.listen_port = int(listen_port)
        except (TypeError, ValueError):
            print(f"Invalid listen_port {listen_port!r} in {config_path}, "
                  f"using {self.listen_port}")

    # -- derived paths ---------------------------------------------------

    @property
    def snapshot_dir(self):
        return os.path.join(self.data_path, 'snapshots')

    @property
    def devices_dir(self):
        return os.path.join(self.data_path, 'devices')

    @property
    def backup_log_dir(self):
        return os.path.join(self.data_path, 'backups')

    def ensure_dirs(self):
        """Create every data directory the application relies on."""
        for path in (self.data_path, self.snapshot_dir, self.devices_dir, self.backup_log_dir):
            os.makedirs(path, exist_ok=True)

    def to_dict(self):
        """Return configuration as a dictionary"""
        return {
            'data_path': self.data_path,
            'listen_ip': self.listen_ip,
            'listen_port': self.listen_port,
        }


_config = None
_config_lock = threading.Lock()


def get_config(reload=False):
    """Return the process-wide Config singleton.

    The config file is parsed once instead of on every request.
    """
    global _config
    if _config is None or reload:
        with _config_lock:
            if _config is None or reload:
                _config = Config()
    return _config


def generate_config():
    """Generate a default configuration file"""
    config_dict = Config().to_dict()

    with open(CONFIG_FILENAME, 'w', encoding='utf-8') as f:
        yaml.dump(config_dict, f, default_flow_style=False)

    print(f"Configuration file {CONFIG_FILENAME} created successfully!")
    print("fshub does not handle authentication; put it behind a reverse "
          "proxy (e.g. nginx HTTP basic auth) if it is reachable by others.")
