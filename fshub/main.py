"""Main entry point for fshub command-line interface"""

import os
from pathlib import Path
import platform
import sys
import time

import click

if __package__ in (None, ""):
    # Allow `python3 fshub/main.py` from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fshub.config import generate_config
    from fshub.utils import format_bytes
else:
    from .config import generate_config
    from .utils import format_bytes


@click.group()
def cli():
    """fshub - File System Hub for managing files across devices"""
    pass


class ScanProgressReporter:
    """Render CLI scan progress on a single terminal line."""

    def __init__(self):
        self.last_render = 0.0
        self.last_width = 0

    def _render(self, counters):
        current_path = counters.get('current_path', '')
        if len(current_path) > 80:
            current_path = '...' + current_path[-77:]

        line = (
            f"Scanning {current_path} | "
            f"files={counters.get('scanned_count', 0)} | "
            f"size={format_bytes(counters.get('scanned_size', 0))} | "
            f"errors={len(counters.get('errors', []))}"
        )
        padded = line.ljust(self.last_width)
        sys.stdout.write('\r' + padded)
        sys.stdout.flush()
        self.last_width = max(self.last_width, len(line))
        self.last_render = time.monotonic()

    def update(self, counters, force=False):
        if force or time.monotonic() - self.last_render >= 5:
            self._render(counters)

    def finish(self, counters=None):
        if counters is not None:
            self._render(counters)
        if self.last_width:
            sys.stdout.write('\n')
            sys.stdout.flush()


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


@cli.command()
@click.argument('path')
@click.option('--use-index', is_flag=True, help='Write indexed snapshot output.')
@click.option(
    '--skip-path',
    'skip_paths',
    multiple=True,
    help='Skip any path whose normalized absolute path starts with this prefix. Repeat for multiple prefixes.',
)
def scan(path, use_index, skip_paths):
    """Scan a directory and save a snapshot."""
    if not (platform.system() == 'Windows' and path == '/') and not os.path.exists(path):
        raise click.ClickException(f'Invalid path: {path}')

    for skip_path in skip_paths:
        if not skip_path:
            raise click.ClickException('Skip paths must not be empty')

    if __package__ in (None, ""):
        from fshub.scanning import run_scan_to_snapshot
    else:
        from .scanning import run_scan_to_snapshot

    reporter = ScanProgressReporter()
    try:
        try:
            result = run_scan_to_snapshot(
                path,
                use_index=use_index,
                counters={},
                result_callback=reporter.update,
                skip_prefixes=skip_paths,
            )
        except OSError as e:
            raise click.ClickException(f'Scan failed: {e}') from e
    finally:
        reporter.finish(result['counters'] if 'result' in locals() else None)

    click.echo(f"Snapshot saved to {result['result_path']}")
    click.echo(
        "Scanned "
        f"{result['counters'].get('scanned_count', 0)} files, "
        f"{format_bytes(result['counters'].get('scanned_size', 0))}, "
        f"errors={len(result['counters'].get('errors', []))}"
    )
    if skip_paths:
        click.echo(f"Skipped prefixes: {', '.join(skip_paths)}")


if __name__ == '__main__':
    cli()
