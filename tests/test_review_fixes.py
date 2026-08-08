"""Regression tests for the issues raised in review of PR #1."""

import os
import sys
import time

import pytest

from conftest import select_all_files, wait_for_task, write_snapshot

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


# --- second review round -------------------------------------------------


@pytest.mark.skipif(sys.platform == 'win32', reason="'\\' is a separator on Windows")
def test_backslash_name_survives_scan_to_backup(client, config, tmp_path):
    """End to end: POSIX names containing '\\' must not be rewritten.

    join_snapshot_path used to run base_path.replace('\\\\', '/') and
    component.lstrip('\\\\/') for Linux snapshots, so a directory named
    'a\\b' collided with a real 'a/b', and a file named '\\odd.txt' lost its
    leading character. Both then reached the backup and hashing layers.
    """
    import zipfile

    from fshub.scanning import run_scan_to_snapshot

    tree = tmp_path / 'tree'
    # A genuine nested directory ...
    (tree / 'a' / 'b').mkdir(parents=True)
    (tree / 'a' / 'b' / 'inner.txt').write_bytes(b'real-nested')
    # ... and a single directory whose name happens to contain a backslash.
    (tree / 'a\\b').mkdir()
    (tree / 'a\\b' / 'inner.txt').write_bytes(b'odd-dir')
    # A file whose name starts with a backslash.
    (tree / '\\odd.txt').write_bytes(b'leading-backslash')

    snapshot = run_scan_to_snapshot(str(tree))['result_file']
    client.post('/api/v1/load_snapshot', json={'filename': snapshot})

    paths = {f['full_path'] for f in get_filtered_files(snapshot, [], [])}
    assert str(tree / 'a' / 'b' / 'inner.txt') in paths
    assert str(tree / 'a\\b' / 'inner.txt') in paths
    assert str(tree / '\\odd.txt') in paths
    assert len(paths) == 3

    for path in paths:
        client.post(f'/api/v1/group/{snapshot}/add_file',
                    json={'path': path, 'group_name': 'all'})

    target = tmp_path / 'zips'
    response = client.post('/api/v1/backup/zip', json={
        'snapshot_filename': snapshot,
        'target_path': str(target),
        'filter_in': ['all'],
    })
    wait_for_task(client, response.get_json()['task_id'])

    contents = {}
    for archive in target.glob('*.zip'):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                contents[name] = zf.read(name)

    # Three distinct archive members, none overwriting another.
    assert len(contents) == 3
    assert sorted(contents.values()) == [b'leading-backslash', b'odd-dir', b'real-nested']


def test_hashing_reads_a_backslash_name(client, config, tmp_path):
    """The corrupted path also made hashing target the wrong file."""
    from fshub.scanning import run_scan_to_snapshot

    tree = tmp_path / 'tree'
    (tree / 'a\\b').mkdir(parents=True)
    (tree / 'a\\b' / 'inner.txt').write_bytes(b'odd-dir')

    snapshot = run_scan_to_snapshot(str(tree))['result_file']
    client.post('/api/v1/load_snapshot', json={'filename': snapshot})

    data = client.post('/api/v1/hash/calculate',
                       json={'snapshot_filename': snapshot}).get_json()
    assert data['files_processed'] == 1
    assert data['files_failed'] == 0


def test_second_zip_backup_does_not_destroy_the_first(client, scanned_snapshot,
                                                      sample_tree, tmp_path):
    """Both runs write into one directory; neither may clobber the other."""
    import zipfile

    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)

    target = tmp_path / 'zips'
    archives_per_run = []

    for run in ('first', 'second'):
        response = client.post('/api/v1/backup/zip', json={
            'snapshot_filename': scanned_snapshot,
            'target_path': str(target),
            'filter_in': ['all'],
            'backup_name': run,
        })
        assert response.status_code == 200
        status = wait_for_task(client, response.get_json()['task_id'])
        assert status['status'] == 'completed'
        archives_per_run.append(sorted(p.name for p in target.glob('*.zip')))

    # The second run added archives instead of replacing the first run's.
    assert set(archives_per_run[0]) < set(archives_per_run[1])
    assert any(name.startswith('first_') for name in archives_per_run[1])
    assert any(name.startswith('second_') for name in archives_per_run[1])

    for archive in target.glob('*.zip'):
        with zipfile.ZipFile(archive) as zf:
            assert len(zf.namelist()) == 3


def test_device_rejects_unhashable_thumbprint(client):
    """A list thumbprint used to poison every later GET with a TypeError."""
    info = client.get('/api/v1/devices').get_json()['current_device_info']

    for bad in ([], {}, 0, ''):
        response = client.post('/api/v1/devices',
                               json=dict(info, host_name='ok', thumbprint=bad))
        assert response.status_code == 400, bad

    # The endpoint is still usable, and nothing was persisted.
    assert client.get('/api/v1/devices').get_json()['devices'] == []


def test_device_listing_survives_a_poisoned_record(client, config):
    """A record written by an older build must not break the endpoint."""
    import json as _json

    path = os.path.join(config.devices_dir, 'devices_host.jl')
    with open(path, 'w', encoding='utf-8') as f:
        # Unusable thumbprint: falls back to host_name rather than being lost.
        f.write(_json.dumps({'host_name': 'host', 'thumbprint': []}) + '\n')
        f.write(_json.dumps({'host_name': 'good', 'thumbprint': 'abc'}) + '\n')
        # No usable identity at all: dropped instead of raising.
        f.write(_json.dumps({'host_name': [], 'thumbprint': {}}) + '\n')

    response = client.get('/api/v1/devices')
    assert response.status_code == 200
    assert sorted(d['host_name'] for d in response.get_json()['devices']) == ['good', 'host']


def test_device_rejects_bad_host_name_and_media(client):
    assert client.post('/api/v1/devices', json={'host_name': 123}).status_code == 400
    assert client.post('/api/v1/devices', json={'host_name': ''}).status_code == 400
    assert client.post('/api/v1/devices',
                       json={'host_name': 'ok', 'media': 'nope'}).status_code == 400


# --- third review round --------------------------------------------------


def _this_pc_snapshot():
    """A 'This PC' snapshot: / -> C:\\ -> C:\\Users -> C:\\Users\\me."""
    return [
        {'p': '/', 'f': [], 's': [], 't': [], 'd': ['C:'], 'T': [[0, 0, 0]],
         'os_name': 'Windows'},
        {'p': 'C:\\', 'f': [], 's': [], 't': [], 'd': ['Users'], 'T': [[0, 0, 0]]},
        {'p': 'C:\\Users', 'f': [], 's': [], 't': [], 'd': ['me'], 'T': [[0, 0, 0]]},
        {'p': 'C:\\Users\\me', 'f': ['doc.txt', 'skip.txt'], 's': [11, 22],
         't': [[0, 0, 0], [0, 0, 0]], 'd': [], 'T': []},
    ]


def test_filter_in_reaches_below_a_windows_drive(client, config):
    """snapshot_dirname returned 'C:' where the index holds 'C:\\'.

    The ancestor chain therefore never contained the drive root, so a
    filter_in selection was pruned at the 'This PC' root and backups of a
    Windows snapshot silently produced nothing.
    """
    name = 'snapshot_9_4.jsonl.gz'
    write_snapshot(config, name, _this_pc_snapshot())
    client.post('/api/v1/load_snapshot', json={'filename': name})

    client.post(f'/api/v1/group/{name}/add_file',
                json={'path': 'C:\\Users\\me\\doc.txt', 'group_name': 'pick'})

    files = get_filtered_files(name, ['pick'], [])
    assert [f['full_path'] for f in files] == ['C:\\Users\\me\\doc.txt']


def test_filter_in_on_a_windows_directory_selects_the_subtree(client, config):
    name = 'snapshot_9_4.jsonl.gz'
    write_snapshot(config, name, _this_pc_snapshot())
    client.post('/api/v1/load_snapshot', json={'filename': name})

    client.post(f'/api/v1/group/{name}/add_dir',
                json={'path': 'C:\\Users\\me', 'group_name': 'pick'})

    files = get_filtered_files(name, ['pick'], [])
    assert sorted(f['name'] for f in files) == ['doc.txt', 'skip.txt']


def test_backups_started_in_the_same_second_do_not_collide(client, config, scanned_snapshot,
                                                           sample_tree, tmp_path,
                                                           monkeypatch):
    """A wall-clock second is not a unique run id.

    Two runs with identical names and file count used to share a log path
    (opened with 'w', so one truncated the other) and identical archive
    names (so the second failed outright in mode 'x').
    """
    import zipfile

    import fshub.api.backup as backup_module

    monkeypatch.setattr(backup_module.time, 'time', lambda: 1000.0)

    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)

    target = tmp_path / 'zips'
    for _ in range(2):
        response = client.post('/api/v1/backup/zip', json={
            'snapshot_filename': scanned_snapshot,
            'target_path': str(target),
            'filter_in': ['all'],
        })
        assert response.status_code == 200
        status = wait_for_task(client, response.get_json()['task_id'])
        assert status['status'] == 'completed', status.get('error')

    # Two independent logs, neither truncated.
    assert len(os.listdir(config.backup_log_dir)) == 2

    # Two independent archive sets, six members in total.
    members = []
    for archive in target.glob('*.zip'):
        with zipfile.ZipFile(archive) as zf:
            members.extend(zf.namelist())
    assert len(members) == 6


def test_folder_backups_in_the_same_second_keep_separate_logs(client, config, scanned_snapshot,
                                                              sample_tree, tmp_path,
                                                              monkeypatch):
    import fshub.api.backup as backup_module

    monkeypatch.setattr(backup_module.time, 'time', lambda: 1000.0)

    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    select_all_files(client, scanned_snapshot, sample_tree)

    for i in range(2):
        response = client.post('/api/v1/backup/folder', json={
            'snapshot_filename': scanned_snapshot,
            'target_path': str(tmp_path / f'out{i}'),
            'filter_in': ['all'],
        })
        assert response.status_code == 200
        status = wait_for_task(client, response.get_json()['task_id'])
        assert status['status'] == 'completed', status.get('error')

    logs = os.listdir(config.backup_log_dir)
    assert len(logs) == 2
    for log in logs:
        with open(os.path.join(config.backup_log_dir, log), encoding='utf-8') as f:
            lines = [line for line in f if line.strip()]
        assert len(lines) == 1 + 3  # metadata + one entry per file
