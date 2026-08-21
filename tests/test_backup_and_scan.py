"""Tests for scanning, backup and hashing."""

import gzip
import json
import os
import threading
import time
import uuid

import pytest

from conftest import select_all_files, wait_for_hash_task, wait_for_task

from fshub import scanning
from fshub.api import backup as backup_module
from fshub.api import hashes as hashes_module
from fshub.api import scans as scans_module
from fshub.scan_logs import (
    MAX_REPORTED_ERRORS,
    ScanRunLog,
    list_scan_statuses,
    read_scan_log,
    read_scan_status,
    scan_duration,
)
from fshub.scanning import is_related_path, run_scan_to_snapshot
from fshub.snapshot import layout


@pytest.mark.parametrize('attributes,expected', [
    (0, None),
    (scanning._FILE_ATTRIBUTE_PINNED, 'pinned'),
    (scanning._FILE_ATTRIBUTE_UNPINNED, 'evictable'),
    (scanning._FILE_ATTRIBUTE_OFFLINE, 'not_fully_local'),
    (scanning._FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, 'not_fully_local'),
    (scanning._FILE_ATTRIBUTE_PINNED |
     scanning._FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, 'not_fully_local'),
])
def test_cloud_state_uses_existing_windows_file_attributes(attributes, expected):
    class StatResult:
        st_file_attributes = attributes

    assert scanning.get_cloud_state(StatResult()) == expected


def test_cloud_state_is_unknown_without_windows_attributes():
    assert scanning.get_cloud_state(object()) is None


@pytest.mark.parametrize('a,b,expected', [
    ('/home/a', '/home/a', True),
    ('/home/a', '/home/a/b', True),
    ('/home/a/b', '/home/a', True),
    ('/home/a', '/home/ab', False),
    ('/home/a', '/home/b', False),
])
def test_is_related_path(a, b, expected):
    assert is_related_path(a, b) is expected


def test_scan_counters_accumulate(config, sample_tree):
    counters = {}
    result = run_scan_to_snapshot(str(sample_tree), counters=counters)
    status = read_scan_status(result['scan_id'])

    assert counters['scanned_count'] == 3
    assert counters['scanned_size'] == 60
    assert counters['errors'] == []
    assert result['finish_time'] == status['finish_time']
    assert result['duration'] == result['finish_time'] - result['start_time']


def test_scan_snapshot_uses_the_compact_cloud_state_form(config, sample_tree):
    """An all-null directory must store the scalar null, not a list of nulls."""
    result = run_scan_to_snapshot(str(sample_tree))
    base = os.path.join(result['snapshot_path'], 'base_gen_000000.jsonl.gz')
    with gzip.open(base, 'rt', encoding='utf-8') as snapshot:
        records = [json.loads(line) for line in snapshot]

    assert all(record['c'] is None for record in records)


def test_scan_skip_prefixes(config, sample_tree):
    counters = {}
    run_scan_to_snapshot(
        str(sample_tree),
        counters=counters,
        skip_prefixes=[str(sample_tree / 'sub')],
    )
    assert counters['scanned_count'] == 1
    assert counters['scanned_size'] == 10


def test_scan_skip_prefix_does_not_match_sibling(config, sample_tree):
    """Skipping "sub" must not also skip an unrelated sibling "subtitles"."""
    sibling = sample_tree / 'subtitles'
    sibling.mkdir()
    (sibling / 'd.txt').write_bytes(b'd' * 40)

    counters = {}
    run_scan_to_snapshot(
        str(sample_tree),
        counters=counters,
        skip_prefixes=[str(sample_tree / 'sub')],
    )
    assert counters['scanned_count'] == 2
    assert counters['scanned_size'] == 10 + 40


def test_scan_skip_prefix_tolerates_trailing_separator(config, sample_tree):
    counters = {}
    run_scan_to_snapshot(
        str(sample_tree),
        counters=counters,
        skip_prefixes=[str(sample_tree / 'sub') + os.sep],
    )
    assert counters['scanned_count'] == 1


def test_scan_endpoint_rejects_bad_input(client):
    assert client.post('/api/v1/scan', json={'path': '/definitely/not/here'}).status_code == 400
    assert client.post('/api/v1/scan', json={'path': '', 'skip_paths': 'x'}).status_code == 400


def test_cli_scan_rejects_a_file(tmp_path):
    from click.testing import CliRunner

    from fshub.main import cli

    source_file = tmp_path / 'file.txt'
    source_file.write_text('not a directory')
    result = CliRunner().invoke(cli, ['scan', str(source_file)])

    assert result.exit_code != 0
    assert 'Not a directory' in result.output


def test_cli_reports_full_scan_log_path(config, sample_tree):
    from click.testing import CliRunner

    from fshub.main import cli

    result = CliRunner().invoke(cli, ['scan', str(sample_tree)])
    assert result.exit_code == 0
    assert f"Scan log saved as {config.scan_log_dir}" in result.output
    assert 'Finished at' in result.output
    assert result.output.rstrip().endswith('s)')


def test_scan_endpoint_reports_status(client, sample_tree):
    response = client.post('/api/v1/scan', json={'path': str(sample_tree)})
    assert response.status_code == 200
    scan_id = response.get_json()['scan_id']

    for _ in range(100):
        status = client.get(f'/api/v1/scan/{scan_id}').get_json()
        if status['status'] != 'running':
            break
        time.sleep(0.02)

    assert status['status'] == 'completed'
    assert status['counters']['scanned_count'] == 3
    assert status['error'] is None
    assert status['finish_time'] is not None
    assert status['finish_time'] >= status['start_time']
    assert status['duration'] == status['finish_time'] - status['start_time']


def test_unknown_scan_id_is_404(client):
    assert client.get('/api/v1/scan/nope').status_code == 404


def test_scan_status_and_log_survive_registry_loss(client, sample_tree):
    response = client.post('/api/v1/scan', json={'path': str(sample_tree)})
    scan_id = response.get_json()['scan_id']

    for _ in range(100):
        status = client.get(f'/api/v1/scan/{scan_id}').get_json()
        if status['status'] != 'running':
            break
        time.sleep(0.02)
    assert status['status'] == 'completed'

    listing = client.get('/api/v1/scan-tasks').get_json()['scans']
    assert any(item['scan_id'] == scan_id for item in listing)
    log = client.get(f'/api/v1/scan/{scan_id}/log').get_json()['records']
    assert log[0]['event'] == 'started'
    assert any(record['event'] == 'progress' for record in log)
    assert log[-1]['event'] == 'completed'

    # Simulate a process restart: status comes from the compact sidecar.
    with scans_module.scan_lock:
        scans_module.running_scans.pop(scan_id, None)
    restored = client.get(f'/api/v1/scan/{scan_id}').get_json()
    assert restored['status'] == 'completed'
    assert restored['snapshot_id'] == status['snapshot_id']


def test_scan_access_errors_are_written_immediately(config, sample_tree, monkeypatch):
    inaccessible = str(sample_tree / 'a.txt')
    real_stat = scanning.os.stat

    def failing_stat(path, *args, **kwargs):
        if str(path) == inaccessible:
            raise PermissionError('test denied')
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(scanning.os, 'stat', failing_stat)
    result = run_scan_to_snapshot(str(sample_tree))
    records = read_scan_log(result['scan_id'])

    errors = [record for record in records if record['event'] == 'scan_error']
    assert len(errors) == 1
    assert inaccessible in errors[0]['message']
    assert 'test denied' in errors[0]['message']
    assert records[-1]['event'] == 'completed'
    assert records[-1]['counters']['error_count'] == 1
    assert read_scan_status(result['scan_id'])['status'] == 'completed_with_errors'


def test_scan_log_exists_before_worker_runs(client, sample_tree, monkeypatch):
    worker_entered = threading.Event()
    release_worker = threading.Event()
    real_run = scans_module.run_scan_to_snapshot

    def blocked_run(*args, **kwargs):
        worker_entered.set()
        release_worker.wait(timeout=2)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(scans_module, 'run_scan_to_snapshot', blocked_run)
    response = client.post('/api/v1/scan', json={'path': str(sample_tree)})
    scan_id = response.get_json()['scan_id']
    assert worker_entered.wait(timeout=1)

    status = client.get(f'/api/v1/scan/{scan_id}').get_json()
    log_response = client.get(f'/api/v1/scan/{scan_id}/log')
    assert status['log_available'] is True
    assert log_response.status_code == 200
    assert log_response.get_json()['records'][0]['event'] == 'started'

    release_worker.set()
    for _ in range(100):
        if client.get(f'/api/v1/scan/{scan_id}').get_json()['status'] != 'running':
            break
        time.sleep(0.02)


def test_unfinished_and_failed_scan_statuses_are_restored(client):
    interrupted = ScanRunLog()
    interrupted.started('/interrupted')
    interrupted_id = interrupted.scan_id
    interrupted.close()

    failed = ScanRunLog()
    failed.started('/failed')
    failed.failed(RuntimeError('boom'), {
        'current_path': '/failed/child',
        'scanned_count': 2,
        'scanned_size': 12,
        'error_count': 0,
        'errors': [],
    })

    interrupted_status = client.get(
        f'/api/v1/scan/{interrupted_id}').get_json()
    failed_status = client.get(f'/api/v1/scan/{failed.scan_id}').get_json()
    assert interrupted_status['status'] == 'interrupted'
    assert interrupted_status['finish_time'] is None
    assert interrupted_status['duration'] is None
    assert failed_status['status'] == 'error'
    assert failed_status['finish_time'] is not None
    assert failed_status['duration'] == scan_duration(
        failed_status['start_time'], failed_status['finish_time'], 'error')
    assert failed_status['snapshot_id'] is None
    assert failed_status['error'] == 'boom'


def test_live_and_restored_status_have_the_same_shape(client, sample_tree):
    response = client.post('/api/v1/scan', json={'path': str(sample_tree)})
    scan_id = response.get_json()['scan_id']
    for _ in range(100):
        live = client.get(f'/api/v1/scan/{scan_id}').get_json()
        if live['status'] != 'running':
            break
        time.sleep(0.02)

    with scans_module.scan_lock:
        scans_module.running_scans.pop(scan_id, None)
    restored = client.get(f'/api/v1/scan/{scan_id}').get_json()
    assert set(restored) == set(live)
    assert set(restored['counters']) == set(live['counters'])


def test_scan_errors_are_bounded_in_status_but_complete_in_log(config, client):
    run_log = ScanRunLog()
    run_log.started('/many-errors')
    counters = {
        'current_path': '/many-errors',
        'scanned_count': 0,
        'scanned_size': 0,
        'error_count': 0,
        'errors': [],
    }
    for index in range(MAX_REPORTED_ERRORS + 5):
        scanning._record_error(counters, f'error {index}', run_log.scan_error)
    run_log.completed(counters, 'snapshot.jsonl.gz')

    status = read_scan_status(run_log.scan_id)
    records = read_scan_log(run_log.scan_id)
    assert status['counters']['error_count'] == MAX_REPORTED_ERRORS + 5
    assert len(status['counters']['errors']) == MAX_REPORTED_ERRORS
    assert len([item for item in records
                if item['event'] == 'scan_error']) == MAX_REPORTED_ERRORS + 5

    listing = client.get('/api/v1/scan-tasks').get_json()['scans']
    listed = next(item for item in listing
                  if item['scan_id'] == run_log.scan_id)
    assert listed['counters'] == status['counters']


def test_scan_duration_from_start_and_finish():
    assert scan_duration(100, 130) == 30
    assert scan_duration(100, 90) == 0
    assert scan_duration(None, 130) is None
    assert scan_duration(100, None, 'interrupted') is None
    elapsed = scan_duration(int(time.time()) - 5, None, 'running')
    assert 4 <= elapsed <= 10


def test_scan_log_endpoint_is_cursor_paginated(config, client):
    run_log = ScanRunLog()
    run_log.started('/paged-log')
    counters = {
        'current_path': '/paged-log',
        'scanned_count': 0,
        'scanned_size': 0,
        'error_count': 45,
        'errors': [],
    }
    for index in range(45):
        run_log.scan_error(f'error {index}', counters)
    run_log.completed(counters, 'snapshot.jsonl.gz')

    records = []
    cursor = 0
    while True:
        response = client.get(
            f'/api/v1/scan/{run_log.scan_id}/log',
            query_string={'cursor': cursor, 'limit': 20},
        )
        assert response.status_code == 200
        page = response.get_json()
        assert len(page['records']) <= 20
        records.extend(page['records'])
        assert page['next_cursor'] >= cursor
        cursor = page['next_cursor']
        if not page['has_more']:
            break

    assert records == read_scan_log(run_log.scan_id)


@pytest.mark.parametrize('query', [
    {'cursor': -1},
    {'cursor': 'nope'},
    {'limit': 0},
    {'limit': 501},
])
def test_scan_log_endpoint_validates_pagination(client, query):
    assert client.get(
        f'/api/v1/scan/{uuid.uuid4()}/log', query_string=query).status_code == 400


def test_scan_status_listing_is_limited(config):
    for index in range(51):
        run_log = ScanRunLog()
        run_log.started(f'/scan-{index}')
        run_log.close()
    assert len(list_scan_statuses()) == 50


def test_scan_log_failure_does_not_fail_scan(config, sample_tree):
    class BrokenLogFile:
        def write(self, _data):
            raise OSError('disk full')

        def close(self):
            pass

    run_log = ScanRunLog()
    run_log._log_file.close()
    run_log._log_file = BrokenLogFile()
    run_log.started(str(sample_tree))

    result = run_scan_to_snapshot(str(sample_tree), run_log=run_log)
    assert result['counters']['scanned_count'] == 3
    status = read_scan_status(run_log.scan_id)
    assert status['status'] == 'completed'
    assert status['log_available'] is False


@pytest.mark.parametrize('scan_id', ['not-a-uuid', str(uuid.uuid4())])
def test_unknown_scan_id_is_404_for_status_and_log(client, scan_id):
    assert client.get(f'/api/v1/scan/{scan_id}').status_code == 404
    assert client.get(f'/api/v1/scan/{scan_id}/log').status_code == 404


def test_folder_backup_copies_files(client, scanned_snapshot, sample_tree, tmp_path):
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)

    target = tmp_path / 'out'
    response = client.post('/api/v1/backup/folder', json={
        'snapshot_id': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
    })
    assert response.status_code == 200

    status = wait_for_task(client, response.get_json()['task_id'])
    assert status['status'] == 'completed'
    assert status['completed_files'] == 3

    copied = sorted(
        os.path.relpath(os.path.join(root, f), target)
        for root, _dirs, files in os.walk(target) for f in files
    )
    assert len(copied) == 3
    # Nothing escaped the target directory.
    for rel in copied:
        assert not rel.startswith('..')


def test_zip_backup_creates_archive(client, scanned_snapshot, sample_tree, tmp_path):
    import zipfile

    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)

    target = tmp_path / 'zips'
    response = client.post('/api/v1/backup/zip', json={
        'snapshot_id': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
    })
    assert response.status_code == 200
    status = wait_for_task(client, response.get_json()['task_id'])
    assert status['status'] == 'completed'

    archives = sorted(target.glob('*.zip'))
    assert archives
    names = []
    for archive in archives:
        with zipfile.ZipFile(archive) as zf:
            names.extend(zf.namelist())
    assert len(names) == 3
    for name in names:
        assert not name.startswith('/')
        assert '..' not in name.split('/')


def test_backup_validates_input(client, scanned_snapshot, tmp_path):
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})

    assert client.post('/api/v1/backup/folder', json={
        'snapshot_id': scanned_snapshot}).status_code == 400

    assert client.post('/api/v1/backup/folder', json={
        'snapshot_id': scanned_snapshot,
        'target_path': 'relative/path',
        'filter_in': [],
    }).status_code == 400

    assert client.post('/api/v1/backup/zip', json={
        'snapshot_id': 'not_loaded.jsonl.gz',
        'target_path': str(tmp_path),
    }).status_code == 400


def test_backup_dry_run_does_not_write(client, scanned_snapshot, sample_tree, tmp_path):
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)

    target = tmp_path / 'dry'
    data = client.post('/api/v1/backup/folder', json={
        'snapshot_id': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
        'dry_run': True,
    }).get_json()

    assert data['dry_run'] is True
    assert data['files_found'] == 3
    assert not target.exists()


def test_hash_calculate_and_duplicates(client, scanned_snapshot, sample_tree):
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)

    response = client.post('/api/v1/hash/calculate', json={
        'snapshot_id': scanned_snapshot,
        'filter_in': ['all'],
    })
    assert response.status_code == 202
    data = wait_for_hash_task(client, response.get_json()['task_id'])['result']

    assert data['files_processed'] == 3
    assert data['files_failed'] == 0
    assert all(len(item['hash']) == 64 for item in data['results'])

    # Distinct contents and sizes, so no duplicates.
    response = client.post('/api/v1/hash/duplicates', json={
        'snapshot_id': scanned_snapshot,
        'filter_in': ['all'],
    })
    dupes = wait_for_hash_task(client, response.get_json()['task_id'])['result']
    assert dupes['duplicates'] == []


def test_hash_rejects_unknown_algorithm(client, scanned_snapshot):
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    response = client.post('/api/v1/hash/calculate', json={
        'snapshot_id': scanned_snapshot,
        'algorithm': 'rot13',
    })
    assert response.status_code == 400
    assert client.get('/api/v1/hash/status/not-a-task').status_code == 404


def _select_and_backup(client, snapshot, tree, target, backup_type='folder', **extra):
    """Put every file in one group and start a backup of it."""
    client.post('/api/v1/load_snapshot', json={'snapshot_id': snapshot})
    select_all_files(client, snapshot, tree)

    payload = {
        'snapshot_id': snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
    }
    payload.update(extra)
    return client.post(f'/api/v1/backup/{backup_type}', json=payload)


@pytest.mark.parametrize('backup_type', ['folder', 'zip'])
def test_backup_of_a_stale_snapshot_reports_failures(client, scanned_snapshot, sample_tree,
                                                     tmp_path, backup_type):
    """A file that could not be copied must never count as backed up."""
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)
    os.remove(os.path.join(str(sample_tree), 'a.txt'))

    response = client.post(f'/api/v1/backup/{backup_type}', json={
        'snapshot_id': scanned_snapshot,
        'target_path': str(tmp_path / 'out'),
        'filter_in': ['all'],
    })
    status = wait_for_task(client, response.get_json()['task_id'])

    assert status['status'] == 'completed_with_errors'
    assert status['completed_files'] == 2
    assert status['failed_files'] == 1
    assert status['errors'] and 'a.txt' in status['errors'][0]['path']


def test_stop_after_the_last_file_is_not_overwritten_by_finish(client):
    """An accepted late stop must finalize as cancelled, not completed."""
    task_id = backup_module._create_task(1)
    backup_module._update_task(task_id, status='running', completed_files=1, progress=100)

    response = client.post(f'/api/v1/backup/stop/{task_id}')
    assert response.get_json()['success'] is True

    backup_module._finish_task(task_id)
    status = client.get(f'/api/v1/backup/status/{task_id}').get_json()
    assert status['status'] == 'cancelled'


def test_stop_before_the_worker_starts_is_not_lost(client, scanned_snapshot, sample_tree,
                                                   tmp_path, monkeypatch):
    """A stop that arrives before the thread runs must cancel the backup."""
    workers = []

    class DeferredThread:
        """Captures the worker instead of starting it, so the test can order events."""

        def __init__(self, target, args, daemon=None):
            workers.append(lambda: target(*args))

        def start(self):
            pass

    monkeypatch.setattr(backup_module.threading, 'Thread', DeferredThread)

    target = tmp_path / 'out'
    response = _select_and_backup(client, scanned_snapshot, sample_tree, target)
    task_id = response.get_json()['task_id']

    assert client.post(f'/api/v1/backup/stop/{task_id}').get_json()['success'] is True

    workers[0]()  # the worker finally gets scheduled

    status = client.get(f'/api/v1/backup/status/{task_id}').get_json()
    assert status['status'] == 'cancelled'
    assert status['completed_files'] == 0
    assert list(target.rglob('*.txt')) == []


def test_zip_source_read_failure_leaves_no_partial_member(
        client, scanned_snapshot, sample_tree, tmp_path, monkeypatch):
    """A source that fails halfway must not leave a restorable truncated file."""
    import zipfile

    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)
    failing_path = os.path.join(str(sample_tree), 'a.txt')
    real_open = open

    class FailingReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.read_count = 0

        def read(self, _size=-1):
            if self.read_count:
                raise OSError('simulated source read failure')
            self.read_count += 1
            return self.wrapped.read(3)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.wrapped.close()

    def failing_open(path, mode='r', *args, **kwargs):
        opened = real_open(path, mode, *args, **kwargs)
        if os.fspath(path) == failing_path and mode == 'rb':
            return FailingReader(opened)
        return opened

    monkeypatch.setattr(backup_module, 'open', failing_open, raising=False)
    target = tmp_path / 'zips'
    response = client.post('/api/v1/backup/zip', json={
        'snapshot_id': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
    })
    status = wait_for_task(client, response.get_json()['task_id'])

    assert status['status'] == 'completed_with_errors'
    members = []
    for archive in target.glob('*.zip'):
        with zipfile.ZipFile(archive) as zf:
            members.extend(zf.namelist())
    assert not any(name.endswith('/a.txt') or name == 'a.txt' for name in members)
    assert len(members) == 2


def test_hash_submission_is_bounded(monkeypatch):
    """A huge hash task must not eagerly queue one Future per file."""
    from concurrent.futures import Future

    class TrackingFuture(Future):
        def __init__(self, owner, value):
            super().__init__()
            self.owner = owner
            self.set_result(value)

        def result(self, *args, **kwargs):
            self.owner.outstanding -= 1
            return super().result(*args, **kwargs)

    class TrackingPool:
        def __init__(self):
            self.outstanding = 0
            self.maximum = 0

        def submit(self, _function, path):
            self.outstanding += 1
            self.maximum = max(self.maximum, self.outstanding)
            return TrackingFuture(self, (path, ('digest', None)))

    pool = TrackingPool()
    monkeypatch.setattr(hashes_module, '_pool', pool)

    results, errors = hashes_module._hash_files(
        [f'file-{i}' for i in range(1000)], 'sha256',
    )

    assert errors == []
    assert len(results) == 1000
    assert pool.maximum <= hashes_module.MAX_WORKERS


def test_backup_name_with_spaces_is_accepted(client, config, scanned_snapshot, sample_tree,
                                             tmp_path):
    """Normal labels must not be rejected; the log keeps them verbatim."""
    response = _select_and_backup(
        client, scanned_snapshot, sample_tree, tmp_path / 'out',
        backup_name='My Backup', backup_target_name='My Computer',
    )
    assert response.status_code == 200
    assert wait_for_task(client, response.get_json()['task_id'])['status'] == 'completed'

    logs = os.listdir(config.backup_log_dir)
    assert len(logs) == 1
    with open(os.path.join(config.backup_log_dir, logs[0]), encoding='utf-8') as f:
        meta = json.loads(f.readline())
    assert meta['backup_name'] == 'My Backup'
    assert meta['backup_target_name'] == 'My Computer'


def test_backup_target_must_be_a_directory(client, scanned_snapshot, sample_tree, tmp_path):
    """Both backup types write into a directory; say so before starting a task."""
    existing_file = tmp_path / 'backup.zip'
    existing_file.write_text('not a directory')

    response = _select_and_backup(client, scanned_snapshot, sample_tree, existing_file,
                                  backup_type='zip')
    assert response.status_code == 400
    assert 'directory' in response.get_json()['error']


def test_two_scans_in_the_same_second_get_separate_snapshots(config, sample_tree, monkeypatch):
    """Same timestamp and entry count must not overwrite an hours-long scan."""
    monkeypatch.setattr(layout.time, 'time', lambda: 1700000000)

    first = run_scan_to_snapshot(str(sample_tree))['snapshot_id']
    second = run_scan_to_snapshot(str(sample_tree))['snapshot_id']

    assert first != second
    assert len(os.listdir(config.snapshot_dir)) == 2


def test_symlinked_scan_paths_are_treated_as_one_tree(client, sample_tree, tmp_path):
    """Two aliases of the same directory must not be scanned concurrently."""
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(sample_tree, target_is_directory=True)
    except OSError as e:
        pytest.skip(f'symlinks are unavailable: {e}')

    assert is_related_path(str(alias), str(sample_tree)) is True
