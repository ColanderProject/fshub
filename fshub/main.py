"""Main entry point for fshub command-line interface"""

import click
from .web import start_web_server
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