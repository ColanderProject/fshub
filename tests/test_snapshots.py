"""Tests for snapshot loading: formats, deep trees and Windows paths."""

import os

from fshub.api.explorer import (
    get_filtered_files,
    load_snapshot_file,
    loaded_snapshots,
    to_web_path,
)

from conftest import write_snapshot


def _windows_snapshot():
    """A minimal 'This PC' snapshot: / -> C:\\ -> C:\\Users."""
    return [
        {'p': '/', 'f': [], 's': [], 't': [], 'd': ['C:'], 'T': [[0, 0, 0]],
         'os_name': 'Windows'},
        {'p': 'C:\\', 'f': ['boot.ini'], 's': [100], 't': [[0, 0, 0]],
         'd': ['Users'], 'T': [[0, 0, 0]]},
        {'p': 'C:\\Users', 'f': ['a.txt', 'b.txt'], 's': [1, 2],
         't': [[0, 0, 0], [0, 0, 0]], 'd': [], 'T': []},
    ]


def test_windows_totals_roll_up_to_this_pc(config, app):
    write_snapshot(config, 'snapshot_1_3.jsonl.gz', _windows_snapshot())
    assert load_snapshot_file('snapshot_1_3.jsonl.gz')

    data = loaded_snapshots['snapshot_1_3.jsonl.gz']['data']
    root, drive, users = data

    assert users['S'] == 3 and users['C'] == 2
    assert drive['S'] == 103 and drive['C'] == 3
    # The "/" root must aggregate the drives too.
    assert root['S'] == 103 and root['C'] == 3


def test_windows_paths_are_web_normalized(client, config):
    write_snapshot(config, 'snapshot_1_3.jsonl.gz', _windows_snapshot())
    client.post('/api/v1/load_snapshot', json={'filename': 'snapshot_1_3.jsonl.gz'})

    listing = client.get('/api/v1/getPath', query_string={
        'snapshot': 'snapshot_1_3.jsonl.gz', 'path': '/C:'}).get_json()
    assert listing['current_path'] == '/C:'
    assert [f['name'] for f in listing['files']] == ['boot.ini']

    # The filtered response uses the same path format as the plain one.
    filtered = client.get('/api/v1/getPath', query_string={
        'snapshot': 'snapshot_1_3.jsonl.gz', 'path': '/C:', 'use_filter': 'true'}).get_json()
    assert filtered['current_path'] == listing['current_path']


def test_to_web_path_is_noop_for_posix():
    assert to_web_path('/home/u', 'Linux') == '/home/u'


def test_deep_tree_does_not_hit_recursion_limit(config, app):
    """A 3000-level tree used to blow the Python stack while loading."""
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

    write_snapshot(config, f'snapshot_2_{depth}.jsonl.gz', records)
    assert load_snapshot_file(f'snapshot_2_{depth}.jsonl.gz')

    data = loaded_snapshots[f'snapshot_2_{depth}.jsonl.gz']['data']
    assert data[0]['C'] == 1
    assert data[0]['S'] == 7


def test_index_format_snapshot_is_loadable(config, app, sample_tree):
    from fshub.scanning import run_scan_to_snapshot

    result = run_scan_to_snapshot(str(sample_tree), use_index=True)
    filename = result['result_file']
    assert filename.endswith('_index.jsonl.gz')

    assert load_snapshot_file(filename)
    data = loaded_snapshots[filename]['data']
    assert data[0]['p'] == str(sample_tree)
    assert data[0]['C'] == 3
    assert data[0]['S'] == 60


def test_filter_in_reaches_files_in_subdirectories(client, scanned_snapshot, sample_tree):
    """Selecting a single deep file must not be swallowed by the traversal."""
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    deep_file = os.path.join(str(sample_tree), 'sub', 'deep', 'c.txt')
    client.post(f'/api/v1/group/{scanned_snapshot}/add_file',
                json={'path': deep_file, 'group_name': 'pick'})

    files = get_filtered_files(scanned_snapshot, ['pick'], [])
    assert [f['full_path'] for f in files] == [deep_file]


def test_filter_in_on_directory_selects_whole_subtree(client, scanned_snapshot, sample_tree):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    client.post(f'/api/v1/group/{scanned_snapshot}/add_dir',
                json={'path': os.path.join(str(sample_tree), 'sub'), 'group_name': 'pick'})

    files = get_filtered_files(scanned_snapshot, ['pick'], [])
    assert sorted(f['name'] for f in files) == ['b.txt', 'c.txt']


def test_filter_out_removes_a_subtree(client, scanned_snapshot, sample_tree):
    client.post('/api/v1/load_snapshot', json={'filename': scanned_snapshot})
    client.post(f'/api/v1/group/{scanned_snapshot}/add_dir',
                json={'path': os.path.join(str(sample_tree), 'sub'), 'group_name': 'skip'})

    files = get_filtered_files(scanned_snapshot, [], ['skip'])
    assert [f['name'] for f in files] == ['a.txt']


def test_group_paths_from_the_ui_match_a_windows_snapshot(client, config):
    """The UI sends /C:/Users/a.txt; the snapshot is indexed as C:\\Users\\a.txt."""
    filename = 'snapshot_1_3.jsonl.gz'
    write_snapshot(config, filename, _windows_snapshot())
    client.post('/api/v1/load_snapshot', json={'filename': filename})

    added = client.post(f'/api/v1/group/{filename}/add_file',
                        json={'path': '/C:/Users/a.txt', 'group_name': 'keep'})
    assert added.status_code == 200

    selected = [f['full_path'] for f in get_filtered_files(filename, ['keep'], [])]
    assert selected == ['C:\\Users\\a.txt']


def test_truncated_index_snapshot_is_rejected(client, config, sample_tree):
    """A half-written pair of files must not load as a shorter snapshot."""
    import gzip

    from fshub.scanning import run_scan_to_snapshot

    filename = run_scan_to_snapshot(str(sample_tree), use_index=True)['result_file']
    data_path = os.path.join(config.snapshot_dir,
                             filename.replace('_index.jsonl.gz', '.bin.gz'))

    with gzip.open(data_path, 'rt', encoding='utf-8') as f:
        lines = f.readlines()
    with gzip.open(data_path, 'wt', encoding='utf-8') as f:
        f.writelines(lines[:-1])

    response = client.post('/api/v1/load_snapshot', json={'filename': filename})

    assert response.status_code == 400
    assert filename not in loaded_snapshots


def test_corrupt_snapshot_is_a_client_error(client, config):
    """A snapshot from a killed scan is an operator problem, not a 500."""
    path = os.path.join(config.snapshot_dir, 'snapshot_1_1.jsonl.gz')
    with open(path, 'wb') as f:
        f.write(b'not gzip at all')

    response = client.post('/api/v1/load_snapshot', json={'filename': 'snapshot_1_1.jsonl.gz'})

    assert response.status_code == 400
    assert 'Invalid snapshot file' in response.get_json()['error']
