"""Main entry point for fshub command-line interface"""

from pathlib import Path
import sys

import click

if __package__ in (None, ""):
    # Allow `python3 fshub/main.py` from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fshub.config import generate_config
else:
    from .config import generate_config


@click.group()
def cli():
    """fshub - File System Hub for managing files across devices"""
    pass


@cli.command()
@click.option('--host', default=None, help='Host to bind to')
@click.option('--port', default=None, help='Port to bind to')
def web(host, port):
    """Start the web UI server"""
    if __package__ in (None, ""):
        from fshub.web import start_web_server
    else:
        from .web import start_web_server
    start_web_server(host, port)


@cli.group()
def config():
    """Configuration management"""
    pass


@config.command(name='gen')
def config_gen():
    """Generate default configuration file"""
    generate_config()


if __name__ == '__main__':
    cli()
