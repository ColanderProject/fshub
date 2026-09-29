"""Regression tests for the fifth review round on PR #1.

Covers: malformed-but-valid JSON must be a 400 and never a 500, the ZIP64
boundary in the zip backup, filtered recursive
totals for whole-directory selections, synchronized group reads and the
host-name fallback when a device thumbprint changes.
"""

import json
import os
import threading
import zipfile

import pytest

from conftest import wait_for_task

from fshub.api.explorer import get_filtered_files, snapshots_lock


# --------------------------------------------------------------------------
# Type validation: a client mistake must not surface as a server fault.
# --------------------------------------------------------------------------

MALFORMED_POSTS = [
    ('/api/v1/load_snapshot', {'filename': ['a']}),
    ('/api/v1/unload_snapshot', {'filename': ['a']}),
    ('/api/v1/hash/calculate', {'algorithm': [], 'snapshot_filename': 'x'}),
    ('/api/v1/hash/calculate', {'snapshot_filename': ['x']}),
    ('/api/v1/hash/calculate', {'snapshot_filename': 'x', 'filter_in': 'g'}),
    ('/api/v1/hash/duplicates', {'algorithm': {}, 'snapshot_filename': 'x'}),
    ('/api/v1/hash/duplicates', {'snapshot_filename': 'x', 'filter_out': 3}),
    ('/api/v1/backup/zip', {'snapshot_filename': ['x'], 'target_path': '/tmp/out'}),
    ('/api/v1/backup/zip', {'snapshot_filename': 'x', 'target_path': ['/tmp/out']}),
    ('/api/v1/backup/zip', {'snapshot_filename': 'x', 'target_path': '/tmp/out',
                            'filter_in': [{'name': 'g'}]}),
    ('/api/v1/backup/folder', {'snapshot_filename': ['x'], 'target_path': '/tmp/out'}),
    ('/api/v1/backup/folder', {'snapshot_filename': 'x', 'target_path': ['/tmp/out']}),
    ('/api/v1/backup/folder', {'snapshot_filename': 'x', 'target_path': '/tmp/out',
                               'filter_out': [None]}),
]


@pytest.mark.parametrize('url,payload', MALFORMED_POSTS)
def test_wrong_field_types_are_client_errors(client, url, payload):
    """Valid JSON with the wrong types used to hit set/dict/path calls and 500."""
    response = client.post(url, json=payload)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()['error']


def test_hash_and_backup_agree_on_filter_validation(client, scanned_snapshot):
    """The same bad filter answered 400 on backup and 500 on hash."""
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})

    payload = {'snapshot_filename': scanned_snapshot, 'filter_in': 'notalist'}
    hash_response = client.post('/api/v1/hash/calculate', json=payload)
    backup_response = client.post('/api/v1/backup/folder',
                                  json=dict(payload, target_path='/tmp/out'))

    assert hash_response.status_code == backup_response.status_code == 400
    assert hash_response.get_json()['error'] == backup_response.get_json()['error']


def test_group_mutation_still_rejects_non_strings(client, scanned_snapshot):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    response = client.post(f'/api/v1/group/{scanned_snapshot}/add_file',
                           json={'path': ['/x'], 'group_name': 'g'})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# zip backup: the ZIP64 boundary and per-file error containment.
# --------------------------------------------------------------------------

def _select_all(client, snapshot, group='all'):
    for file_info in get_filtered_files(snapshot, [], []):
        client.post(f'/api/v1/group/{snapshot}/add_file',
                    json={'path': file_info['full_path'], 'group_name': group})


def test_zip_backup_survives_the_zip64_boundary(client, scanned_snapshot, sample_tree,
                                                tmp_path, monkeypatch):
    """A member above the ZIP64 limit used to abort the whole run.

    The real boundary is 2 GiB; lowering ZIP64_LIMIT reproduces it in bytes.
    Without force_zip64 the entry raises RuntimeError on close, which is not
    an OSError, so the run ended as 'error' with failed_files == 0 and every
    remaining file silently skipped.
    """
    monkeypatch.setattr(zipfile, 'ZIP64_LIMIT', 8)

    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    _select_all(client, scanned_snapshot)

    target = tmp_path / 'zipout'
    response = client.post('/api/v1/backup/zip', json={
        'snapshot_filename': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
        'max_file_size': 10 * 1024 * 1024,
    })
    assert response.status_code == 200
    status = wait_for_task(client, response.get_json()['task_id'])

    assert status['status'] == 'completed', status.get('error')
    assert status['completed_files'] == 3
    assert status['failed_files'] == 0

    members = []
    for archive in sorted(target.glob('*.zip')):
        with zipfile.ZipFile(archive) as zf:
            members.extend(zf.namelist())
    assert len(members) == 3


# --------------------------------------------------------------------------
# Filtered browsing must agree with what backup/hash actually select.
# --------------------------------------------------------------------------

def _get_path(client, snapshot, path, filter_in, recursive=True):
    return client.get('/api/v1/getPath', query_string={
        'snapshot': snapshot,
        'path': path,
        'use_filter': 'true',
        'recursive_calc': 'true' if recursive else 'false',
        'filter_in': json.dumps(filter_in),
        'filter_out': '[]',
    }).get_json()


def test_selected_directory_reports_its_whole_subtree(client, scanned_snapshot, sample_tree):
    """A directory in filter_in is backed up whole, so its totals must say so."""
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    sub = str(sample_tree / 'sub')
    client.post(f'/api/v1/group/{scanned_snapshot}/add_dir',
                json={'path': sub, 'group_name': 'g'})

    listing = _get_path(client, scanned_snapshot, str(sample_tree), ['g'])
    dirs = {d['name']: d for d in listing['dirs']}
    assert 'sub' in dirs

    # b.txt (20) + deep/c.txt (30), exactly what get_filtered_files selects.
    selected = get_filtered_files(scanned_snapshot, ['g'], [])
    assert dirs['sub']['S'] == sum(f['size'] for f in selected) == 50
    assert dirs['sub']['C'] == len(selected) == 2


def test_browsing_into_a_selected_directory_shows_its_contents(client, scanned_snapshot,
                                                               sample_tree):
    """Descendants of a selected directory are included, so they must be shown."""
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    sub = str(sample_tree / 'sub')
    client.post(f'/api/v1/group/{scanned_snapshot}/add_dir',
                json={'path': sub, 'group_name': 'g'})

    listing = _get_path(client, scanned_snapshot, sub, ['g'])
    assert [f['name'] for f in listing['files']] == ['b.txt']
    deep = {d['name']: d for d in listing['dirs']}
    assert 'deep' in deep
    assert deep['deep']['S'] == 30
    assert deep['deep']['C'] == 1


def test_filter_out_still_wins_inside_a_selected_directory(client, scanned_snapshot,
                                                           sample_tree):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    sub = str(sample_tree / 'sub')
    client.post(f'/api/v1/group/{scanned_snapshot}/add_dir',
                json={'path': sub, 'group_name': 'g'})
    client.post(f'/api/v1/group/{scanned_snapshot}/add_dir',
                json={'path': os.path.join(sub, 'deep'), 'group_name': 'skip'})

    listing = client.get('/api/v1/getPath', query_string={
        'snapshot': scanned_snapshot,
        'path': str(sample_tree),
        'use_filter': 'true',
        'recursive_calc': 'true',
        'filter_in': json.dumps(['g']),
        'filter_out': json.dumps(['skip']),
    }).get_json()

    dirs = {d['name']: d for d in listing['dirs']}
    assert dirs['sub']['S'] == 20  # deep/c.txt excluded
    assert dirs['sub']['C'] == 1

    selected = get_filtered_files(scanned_snapshot, ['g'], ['skip'])
    assert len(selected) == 1


def test_unselected_directory_still_reports_nothing(client, scanned_snapshot, sample_tree):
    """The fix must not turn the include filter into 'show everything'."""
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    client.post(f'/api/v1/group/{scanned_snapshot}/add_file',
                json={'path': str(sample_tree / 'a.txt'), 'group_name': 'g'})

    listing = _get_path(client, scanned_snapshot, str(sample_tree), ['g'])
    assert [f['name'] for f in listing['files']] == ['a.txt']
    assert listing['dirs'] == []


# --------------------------------------------------------------------------
# Group reads must be synchronized with group mutations.
# --------------------------------------------------------------------------

@pytest.mark.parametrize('url_suffix', ['', '/files'])
def test_group_reads_wait_for_the_mutation_lock(client, scanned_snapshot, url_suffix):
    """Reading a group dict/set while it is mutated raised 'changed size'."""
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    client.post(f'/api/v1/group/{scanned_snapshot}/add_file',
                json={'path': '/x', 'group_name': 'g'})

    if url_suffix:
        url = f'/api/v1/group/{scanned_snapshot}/files?group_name=g'
    else:
        url = f'/api/v1/groups/{scanned_snapshot}'

    seen = []

    def read():
        seen.append(client.get(url).status_code)

    reader = threading.Thread(target=read)
    with snapshots_lock:
        reader.start()
        reader.join(0.3)
        assert reader.is_alive(), 'group read did not take the snapshot lock'

    reader.join(5)
    assert seen == [200]


# --------------------------------------------------------------------------
# Device onboarding must survive a thumbprint change.
# --------------------------------------------------------------------------

CURRENT_DEVICE = {
    'host_name': 'raspberrypi',
    'mac_addr': '3c:22:fb:12:34:56',
    'thumbprint': 'new-thumbprint',
}


@pytest.fixture
def current_device(monkeypatch):
    import fshub.api.devices as devices_module
    info = dict(CURRENT_DEVICE)
    monkeypatch.setattr(devices_module, 'get_system_info', lambda: dict(info))
    return info


def _known(client, stored):
    client.post('/api/v1/devices', json=stored)
    return client.get('/api/v1/devices').get_json()['current_device_known']


def test_current_device_is_known_after_its_thumbprint_changed(client, current_device):
    """The thumbprint mixes in the kernel release, so an update rotates it."""
    assert _known(client, dict(current_device, thumbprint='old-thumbprint')) is True


def test_same_host_name_on_another_machine_is_not_known(client, current_device):
    """Host names repeat across machines; they alone must not match."""
    stored = dict(current_device, thumbprint='other', mac_addr='3c:22:fb:ab:cd:ef')
    assert _known(client, stored) is False


def test_host_name_without_mac_is_not_known(client, current_device):
    stored = {'host_name': current_device['host_name'], 'thumbprint': 'other'}
    assert _known(client, stored) is False


def test_random_fallback_mac_is_not_an_identity(client, current_device):
    """uuid.getnode() invents a multicast MAC when it finds no interface."""
    current_device['mac_addr'] = '07:11:22:33:44:55'
    stored = dict(current_device, thumbprint='other')
    assert _known(client, stored) is False
