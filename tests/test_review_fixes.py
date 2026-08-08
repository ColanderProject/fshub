"""Regression tests for the issues raised in review of PR #1."""

import os
import sys
import time

import pytest

from conftest import write_snapshot

from fshub.api.explorer import get_filtered_files, load_snapshot_file, loaded_snapshots
from fshub.utils import snapshot_relative_path


def test_colon_in_posix_name_is_preserved():
    """Stripping ':' made distinct sources collide on one destination."""
    assert snapshot_relative_path('/data/report:', 'Linux') == 'data/report:'
    assert snapshot_relative_path('/data/report', 'Linux') == 'data/report'
    assert (snapshot_relative_path('/data/report:', 'Linux')
            != snapshot_relative_path('/data/report', 'Linux'))


def test_backslash_in_posix_name_is_not_a_separator():
    """POSIX allows '\\' in file names; only Windows snapshots split on it."""
    assert snapshot_relative_path('/data/a\\b', 'Linux') == 'data/a\\b'
    assert (snapshot_relative_path('/data/a\\b', 'Linux')
            != snapshot_relative_path('/data/a/b', 'Linux'))
    assert snapshot_relative_path('C:\\data\\a', 'Windows') == 'C/data/a'


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX symlink semantics')
def test_folder_backup_refuses_symlinked_destination(client, scanned_snapshot,
                                                     sample_tree, tmp_path):
    """A symlink inside the target must not let copy2 write straight through it."""
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    source = os.path.join(str(sample_tree), 'a.txt')
    client.post(f'/api/v1/group/{scanned_snapshot}/add_file',
                json={'path': source, 'group_name': 'one'})

    target = tmp_path / 'out'
    outside = tmp_path / 'outside.txt'
    outside.write_text('untouched')

    # Pre-create the destination as a symlink pointing outside the target.
    dest = target / snapshot_relative_path(source, 'Linux')
    dest.parent.mkdir(parents=True)
    dest.symlink_to(outside)

    response = client.post('/api/v1/backup/folder', json={
        'snapshot_filename': scanned_snapshot,
        'target_path': str(target),
        'filter_in': ['one'],
    })
    task_id = response.get_json()['task_id']
    for _ in range(200):
        status = client.get(f'/api/v1/backup/status/{task_id}').get_json()
        if status['status'] in ('completed', 'error', 'cancelled'):
            break
        time.sleep(0.02)

    assert outside.read_text() == 'untouched'


def test_filtered_traversal_survives_a_deep_tree(config, app):
    """get_filtered_files() used to raise RecursionError on deep snapshots."""
    depth = 3000
    records = []
    for level in range(depth):
        node_path = '/' + '/'.join(f'd{i}' for i in range(level + 1))
        children = [f'd{level + 1}'] if level + 1 < depth else []
        records.append({
            'p': node_path,
            'f': ['leaf'] if not children else [],
            's': [7] if not children else [],
            't': [[0, 0, 0]] if not children else [],
            'd': children,
            'T': [[0, 0, 0]] if children else [],
        })
    records[0]['os_name'] = 'Linux'

    name = f'snapshot_3_{depth}.jsonl.gz'
    write_snapshot(config, name, records)
    assert load_snapshot_file(name)

    files = get_filtered_files(name, [], [])
    assert len(files) == 1
    assert files[0]['size'] == 7


def test_filtered_files_keep_listing_order(client, scanned_snapshot):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    files = get_filtered_files(scanned_snapshot, [], [])
    assert [f['name'] for f in files] == ['a.txt', 'b.txt', 'c.txt']


def test_empty_files_are_reported_as_duplicates(client, config, tmp_path):
    from fshub.scanning import run_scan_to_snapshot

    tree = tmp_path / 'empties'
    tree.mkdir()
    (tree / 'one').write_bytes(b'')
    (tree / 'two').write_bytes(b'')

    snapshot = run_scan_to_snapshot(str(tree))['result_file']
    client.post('/api/v1/load_snapshot', json={'filename': snapshot})

    data = client.post('/api/v1/hash/duplicates', json={
        'snapshot_filename': snapshot}).get_json()

    assert len(data['duplicates']) == 1
    assert data['duplicates'][0]['count'] == 2
    assert data['duplicates'][0]['size'] == 0


def test_exactly_limit_results_is_not_truncated(client, scanned_snapshot):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})

    data = client.post('/api/v1/search', json={'query': '.txt', 'limit': 3}).get_json()
    assert data['count'] == 3
    assert data['truncated'] is False

    data = client.post('/api/v1/search', json={'query': '.txt', 'limit': 2}).get_json()
    assert data['count'] == 2
    assert data['truncated'] is True


def test_group_mutation_rejects_non_string_fields(client, scanned_snapshot):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})

    for payload in ({'path': '/x', 'group_name': []},
                    {'path': ['/x'], 'group_name': 'g'},
                    {'path': '/x', 'group_name': {'a': 1}}):
        response = client.post(f'/api/v1/group/{scanned_snapshot}/add_file', json=payload)
        assert response.status_code == 400, payload


def test_group_log_and_memory_agree_after_concurrent_writes(client, scanned_snapshot,
                                                            sample_tree):
    """The persisted log must match memory, whatever the interleaving."""
    import threading

    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    target = os.path.join(str(sample_tree), 'a.txt')

    def toggle(action):
        for _ in range(20):
            client.post(f'/api/v1/group/{scanned_snapshot}/{action}',
                        json={'path': target, 'group_name': 'g'})

    threads = [threading.Thread(target=toggle, args=(a,))
               for a in ('add_file', 'remove_file')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    in_memory = set(loaded_snapshots[scanned_snapshot]['groups']['g']['f'])
    client.post('/api/v1/unload_snapshot', json={'filename': scanned_snapshot})
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    from_disk = set(loaded_snapshots[scanned_snapshot]['groups']['g']['f'])

    assert in_memory == from_disk


def test_windows_root_scan_is_related_to_every_drive(monkeypatch):
    """On Windows '/' means "all drives", so it overlaps any drive path."""
    import fshub.scanning as scanning

    # Identity normalisation mimics ntpath: without the '/' special case the
    # comparison below is a plain string test that finds nothing in common.
    monkeypatch.setattr(scanning, '_normalize_prefix', lambda p: p)

    monkeypatch.setattr(scanning.platform, 'system', lambda: 'Linux')
    assert scanning.is_related_path('/', 'D:\\data') is False

    monkeypatch.setattr(scanning.platform, 'system', lambda: 'Windows')
    assert scanning.is_related_path('/', 'D:\\data') is True
    assert scanning.is_related_path('D:\\data', '/') is True
    assert scanning.is_related_path('C:\\a', 'D:\\b') is False


def test_device_dedup_uses_update_time(client):
    """A rename must not resurrect the older record from another file."""
    info = client.get('/api/v1/devices').get_json()['current_device_info']

    old = dict(info, host_name='zzz-old', device_type='old')
    client.post('/api/v1/devices', json=old)

    new = dict(info, host_name='aaa-new', device_type='new')
    client.post('/api/v1/devices', json=new)

    devices = client.get('/api/v1/devices').get_json()['devices']
    assert len(devices) == 1
    assert devices[0]['device_type'] == 'new'


def test_write_stamps_increase_under_a_coarse_clock(monkeypatch):
    """Windows' ~15 ms clock made two quick updates share a timestamp."""
    import fshub.api.devices as devices

    monkeypatch.setattr(devices.time, 'time', lambda: 1000.0)
    monkeypatch.setattr(devices, '_last_stamp', 0.0)

    stamps = [devices._next_stamp() for _ in range(5)]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 5


def test_device_dedup_survives_a_coarse_clock(client, monkeypatch):
    """End to end: a rename must not resurrect the older record."""
    import fshub.api.devices as devices

    monkeypatch.setattr(devices.time, 'time', lambda: 1000.0)

    info = client.get('/api/v1/devices').get_json()['current_device_info']
    client.post('/api/v1/devices', json=dict(info, host_name='zzz-old', device_type='old'))
    client.post('/api/v1/devices', json=dict(info, host_name='aaa-new', device_type='new'))

    devices_list = client.get('/api/v1/devices').get_json()['devices']
    assert len(devices_list) == 1
    assert devices_list[0]['device_type'] == 'new'


def test_device_dedup_prefers_the_last_line_of_a_file(client, config):
    """Legacy records without updated_at still resolve by write order."""
    import json as _json

    info = client.get('/api/v1/devices').get_json()['current_device_info']
    path = os.path.join(config.devices_dir, 'devices_host.jl')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(_json.dumps(dict(info, host_name='host', device_type='first')) + '\n')
        f.write(_json.dumps(dict(info, host_name='host', device_type='second')) + '\n')

    devices_list = client.get('/api/v1/devices').get_json()['devices']
    assert len(devices_list) == 1
    assert devices_list[0]['device_type'] == 'second'
