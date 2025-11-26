"""Configuration management for fshub"""

import os
import yaml
import secrets
from pathlib import Path


class Config:
    def __init__(self):
        self.data_path = os.path.expanduser('~/.fshub/')
        self.login_password = secrets.token_urlsafe(32)
        self.require_password = True
        self.listen_ip = 'localhost'
        self.listen_port = 7303
        self.load_config()

    def load_config(self):
        """Load configuration from file if exists"""
        # First try current directory
        config_path = Path('fshub.yaml')
        if not config_path.exists():
            # Try user config directory
            config_dir = Path.home() / '.config'
            config_path = config_dir / 'fshub.yaml'
        
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = yaml.safe_load(f)
                    if config_data:
                        self.data_path = os.path.expanduser(config_data.get('data_path', self.data_path))
                        self.login_password = config_data.get('login_password', self.login_password)
                        self.require_password = config_data.get('require_password', self.require_password)
                        self.listen_ip = config_data.get('listen_ip', self.listen_ip)
                        self.listen_port = config_data.get('listen_port', self.listen_port)
            except Exception as e:
                print(f"Error loading config: {e}")

    def to_dict(self):
        """Return configuration as a dictionary"""
        return {
            'data_path': self.data_path,
            'login_password': self.login_password,
            'require_password': self.require_password,
            'listen_ip': self.listen_ip,
            'listen_port': self.listen_port
        }


def generate_config():
    """Generate a default configuration file"""
    config = Config()
    config_dict = config.to_dict()
    
    # Always use the default password when generating
    config_dict['login_password'] = secrets.token_urlsafe(32)
    
    with open('fshub.yaml', 'w', encoding='utf-8') as f:
        yaml.dump(config_dict, f, default_flow_style=False)
    
    print("Configuration file fshub.yaml created successfully!")
    print(f"Default login password: {config_dict['login_password']}")