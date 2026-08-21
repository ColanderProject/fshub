"""Shared pytest fixtures for fshub tests."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fshub import config as config_module  # noqa: E402
from fshub.api import backup as backup_module  # noqa: E402
from fshub.api import hashes as hashes_module  # noqa: E402
from fshub.api import scans as scans_module  # noqa: E402
from fshub.api.explorer import loaded_snapshots  # noqa: E402
from fshub.web import create_app  # noqa: E402


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A Config pointing at an isolated data directory."""
    cfg = config_module.Config.__new__(config_module.Config)
    cfg.data_path = str(tmp_path / 'data')
    # Deliberately outside data_path, like the real default: the producer id
    # must not travel with a copied data directory.
    cfg.local_state_path = str(tmp_path / 'state')
    cfg.listen_ip = 'localhost'
    cfg.listen_port = 7303
    cfg.config_path = None
    cfg.ensure_dirs()

    monkeypatch.setattr(config_module, '_config', cfg)
    return cfg


@pytest.fixture
def app(config):
    application = create_app()
    application.config['TESTING'] = True
    yield application
    loaded_snapshots.clear()
    backup_module.backup_tasks.clear()
    hashes_module.hash_tasks.clear()
    with scans_module.scan_lock:
        scans_module.running_scans.clear()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sample_tree(tmp_path):
    """A real on-disk tree: root/a.txt, root/sub/b.txt, root/sub/deep/c.txt."""
    root = tmp_path / 'tree'
    (root / 'sub' / 'deep').mkdir(parents=True)
    (root / 'a.txt').write_bytes(b'a' * 10)
    (root / 'sub' / 'b.txt').write_bytes(b'b' * 20)
    (root / 'sub' / 'deep' / 'c.txt').write_bytes(b'c' * 30)
    return root


def write_snapshot(config, records, snapshot_id=None, os_name='Linux',
                   root_path=None, skip_prefixes=(), providers=None):
    """Publish records as the base of a new snapshot, bypassing the scanner."""
    from fshub.snapshot import SnapshotWriter, build_scan_scope

    writer = SnapshotWriter.create(config.snapshot_dir, snapshot_id=snapshot_id)
    root_path = root_path if root_path is not None else records[0]['p']
    with writer:
        writer.publish_full_rescan(
            records,
            scan_scope=build_scan_scope(root_path, list(skip_prefixes)),
            os_name=os_name,
            sources=[{'path': root_path, 'source_id': None, 'kind': 'filesystem'}],
            identity_providers=providers or [
                {'id': 0, 'scheme': 'none', 'strength': 'none'}],
            start_scan_time=1700000000,
            finish_scan_time=1700000001,
        )
    return writer.layout.snapshot_id


def make_record(path, files=(), dirs=(), identity=None):
    """A complete directory record; every field is mandatory in the format."""
    from fshub.snapshot import TRAVERSED, new_record

    record = new_record(path)
    record['i'] = identity
    for name, size in files:
        record['f'].append(name)
        record['s'].append(size)
        record['t'].append([1700000000, 1700000001, 1700000002])
    for item in dirs:
        name, state, child_identity = item if isinstance(item, tuple) else \
            (item, TRAVERSED, None)
        record['d'].append(name)
        record['T'].append([1700000000, 1700000001, 1700000002])
        record['D'].append(child_identity)
        record['x'].append(state)
    return record


@pytest.fixture
def scanned_snapshot(config, sample_tree):
    """Scan sample_tree and return the resulting snapshot id."""
    from fshub.scanning import run_scan_to_snapshot

    result = run_scan_to_snapshot(str(sample_tree))
    return result['snapshot_id']


def select_all_files(client, snapshot, tree, group_name='all'):
    """Put every file of a scanned tree into one group."""
    from fshub.api.explorer import get_filtered_files

    for file_info in get_filtered_files(snapshot, [], []):
        client.post(f'/api/v1/group/{snapshot}/add_file', json={
            'path': file_info['full_path'],
            'group_name': group_name,
        })


def wait_for_hash_task(client, task_id, timeout=4.0):
    """Block until a hash task has completed, returning its status."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f'/api/v1/hash/status/{task_id}').get_json()
        if status['status'] in hashes_module.FINISHED:
            return status
        time.sleep(0.02)
    pytest.fail(f'hash task {task_id} did not finish within {timeout}s')


def wait_for_task(client, task_id, timeout=4.0):
    """Block until a backup task leaves the running state."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f'/api/v1/backup/status/{task_id}').get_json()
        if status['status'] in backup_module.FINISHED:
            return status
        time.sleep(0.02)
    pytest.fail(f'backup task {task_id} did not finish within {timeout}s')
