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
            "errors="
            f"{counters.get('error_count', len(counters.get('errors', [])))}"
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
@click.option(
    '--skip-path',
    'skip_paths',
    multiple=True,
    help='Skip this path and everything under it. Repeat for multiple paths.',
)
def scan(path, skip_paths):
    """Scan a directory and save a snapshot."""
    if not (platform.system() == 'Windows' and path == '/') and not os.path.isdir(path):
        raise click.ClickException(f'Not a directory: {path}')

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
                counters={},
                result_callback=reporter.update,
                skip_prefixes=skip_paths,
            )
        except OSError as e:
            raise click.ClickException(f'Scan failed: {e}') from e
    finally:
        reporter.finish(result['counters'] if 'result' in locals() else None)

    click.echo(f"Snapshot {result['snapshot_id']} saved to {result['snapshot_path']}")
    click.echo(f"Scan log saved as {result['scan_log']}")
    click.echo(
        "Scanned "
        f"{result['counters'].get('scanned_count', 0)} files, "
        f"{format_bytes(result['counters'].get('scanned_size', 0))}, "
        "errors="
        f"{result['counters'].get('error_count', len(result['counters'].get('errors', [])))}"
    )
    click.echo(
        f"Finished at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(result['finish_time']))} "
        f"({result['duration']}s)"
    )
    if skip_paths:
        click.echo(f"Skipped prefixes: {', '.join(skip_paths)}")


def _snapshot_writer(snapshot_id):
    """Open the writer for one snapshot directory."""
    if __package__ in (None, ""):
        from fshub.config import get_config
        from fshub.snapshot import SnapshotWriter
    else:
        from .config import get_config
        from .snapshot import SnapshotWriter
    return SnapshotWriter(os.path.join(get_config().snapshot_dir, snapshot_id))


@cli.command()
@click.argument('snapshot_id')
def rescan(snapshot_id):
    """Rescan a snapshot's scope and store only what changed."""
    if __package__ in (None, ""):
        from fshub.scanning import run_incremental_scan
    else:
        from .scanning import run_incremental_scan

    reporter = ScanProgressReporter()
    result = None
    try:
        try:
            result = run_incremental_scan(
                snapshot_id, counters={}, result_callback=reporter.update)
        except OSError as e:
            raise click.ClickException(f'Rescan failed: {e}') from e
    finally:
        reporter.finish(result['counters'] if result else None)

    if result['changed_count'] == 0:
        click.echo(f"No changes; {snapshot_id} stays at generation "
                   f"{result['snapshot_generation']}")
        return
    click.echo(f"Stored {result['changed_count']} changed directories as "
               f"generation {result['snapshot_generation']}")
    click.echo(f"Scan log saved as {result['scan_log']}")


@cli.command()
@click.argument('snapshot_id')
def compact(snapshot_id):
    """Fold a snapshot's increments back into a new base."""
    writer = _snapshot_writer(snapshot_id)
    with writer:
        tree = writer.compact()
    if tree is None:
        click.echo('Nothing to compact')
        return
    click.echo(f"Compacted into a base of {len(tree['data'])} records "
               f"({tree['digest']})")


@cli.command()
@click.argument('snapshot_id')
@click.option('--min-age', default=3600, show_default=True,
              help='Never delete files younger than this many seconds.')
@click.option('--keep-revisions', default=3, show_default=True,
              help='How many recent revisions stay readable for slow readers.')
def gc(snapshot_id, min_age, keep_revisions):
    """Delete payload files no live revision depends on."""
    writer = _snapshot_writer(snapshot_id)
    with writer:
        deleted = writer.collect_garbage(min_age=min_age,
                                         keep_revisions=keep_revisions)
    click.echo(f'Deleted {len(deleted)} files')
    for name in deleted:
        click.echo(f'  {name}')


if __name__ == '__main__':
    cli()
