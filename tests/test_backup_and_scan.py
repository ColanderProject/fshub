"""Tests for scanning, backup and hashing."""

import os
import time

import pytest

from fshub.scanning import is_related_path, run_scan_to_snapshot


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
    run_scan_to_snapshot(str(sample_tree), counters=counters)
    assert counters['scanned_count'] == 3
    assert counters['scanned_size'] == 60
    assert counters['errors'] == []


def test_scan_skip_prefixes(config, sample_tree):
    counters = {}
    run_scan_to_snapshot(
        str(sample_tree),
        counters=counters,
        skip_prefixes=[str(sample_tree / 'sub')],
    )
    assert counters['scanned_count'] == 1
    assert counters['scanned_size'] == 10


def test_scan_endpoint_rejects_bad_input(client):
    assert client.post('/api/v1/scan', json={'path': '/definitely/not/here'}).status_code == 400
    assert client.post('/api/v1/scan', json={'path': '', 'skip_paths': 'x'}).status_code == 400


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


def test_unknown_scan_id_is_404(client):
    assert client.get('/api/v1/scan/nope').status_code == 404


def _select_all(client, snapshot, sample_tree):
    """Put every file of the sample tree into a group named 'all'."""
    for rel in ('a.txt', 'sub/b.txt', 'sub/deep/c.txt'):
        client.post(f'/api/v1/group/{snapshot}/add_file', json={
            'path': os.path.join(str(sample_tree), *rel.split('/')),
            'group_name': 'all',
        })


def _wait_for_task(client, task_id):
    for _ in range(200):
        status = client.get(f'/api/v1/backup/status/{task_id}').get_json()
        if status['status'] in ('completed', 'error', 'cancelled'):
            return status
        time.sleep(0.02)
    pytest.fail(f'backup task {task_id} did not finish')


def test_folder_backup_copies_files(client, scanned_snapshot, sample_tree, tmp_path):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    _select_all(client, scanned_snapshot, sample_tree)

    target = tmp_path / 'out'
    response = client.post('/api/v1/backup/folder', json={
        'snapshot_filename': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
    })
    assert response.status_code == 200

    status = _wait_for_task(client, response.get_json()['task_id'])
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

    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    _select_all(client, scanned_snapshot, sample_tree)

    target = tmp_path / 'zips'
    response = client.post('/api/v1/backup/zip', json={
        'snapshot_filename': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
    })
    assert response.status_code == 200
    status = _wait_for_task(client, response.get_json()['task_id'])
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
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})

    assert client.post('/api/v1/backup/folder', json={
        'snapshot_filename': scanned_snapshot}).status_code == 400

    assert client.post('/api/v1/backup/folder', json={
        'snapshot_filename': scanned_snapshot,
        'target_path': 'relative/path',
        'filter_in': [],
    }).status_code == 400

    assert client.post('/api/v1/backup/zip', json={
        'snapshot_filename': 'not_loaded.jsonl.gz',
        'target_path': str(tmp_path),
    }).status_code == 400


def test_backup_dry_run_does_not_write(client, scanned_snapshot, sample_tree, tmp_path):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    _select_all(client, scanned_snapshot, sample_tree)

    target = tmp_path / 'dry'
    data = client.post('/api/v1/backup/folder', json={
        'snapshot_filename': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
        'dry_run': True,
    }).get_json()

    assert data['dry_run'] is True
    assert data['files_found'] == 3
    assert not target.exists()


def test_hash_calculate_and_duplicates(client, scanned_snapshot, sample_tree):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    _select_all(client, scanned_snapshot, sample_tree)

    data = client.post('/api/v1/hash/calculate', json={
        'snapshot_filename': scanned_snapshot,
        'filter_in': ['all'],
    }).get_json()

    assert data['files_processed'] == 3
    assert data['files_failed'] == 0
    assert all(len(item['hash']) == 64 for item in data['results'])

    # Distinct contents and sizes, so no duplicates.
    dupes = client.post('/api/v1/hash/duplicates', json={
        'snapshot_filename': scanned_snapshot,
        'filter_in': ['all'],
    }).get_json()
    assert dupes['duplicates'] == []


def test_hash_rejects_unknown_algorithm(client, scanned_snapshot):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    response = client.post('/api/v1/hash/calculate', json={
        'snapshot_filename': scanned_snapshot,
        'algorithm': 'rot13',
    })
    assert response.status_code == 400
