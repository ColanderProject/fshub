"""Tests for snapshot loading: rebuild, deep trees and Windows paths."""

import json
import os

import pytest

from fshub.api.explorer import (
    get_filtered_files,
    load_snapshot_file,
    loaded_snapshots,
    to_web_path,
)
from fshub.snapshot import InvalidSnapshot, SnapshotLayout

from conftest import make_record, write_snapshot


def _windows_records():
    """A minimal 'This PC' snapshot: / -> C:\\ -> C:\\Users."""
    return [
        make_record('/', dirs=['C:']),
        make_record('C:\\', files=[('boot.ini', 100)], dirs=['Users']),
        make_record('C:\\Users', files=[('a.txt', 1), ('b.txt', 2)]),
    ]


def _windows_snapshot(config, records=None):
    return write_snapshot(config, records or _windows_records(),
                          os_name='Windows', root_path='/')


def test_windows_totals_roll_up_to_this_pc(config, app):
    snapshot = _windows_snapshot(config)
    assert load_snapshot_file(snapshot)

    data = loaded_snapshots[snapshot]['data']
    root, drive, users = data

    assert users['S'] == 3 and users['C'] == 2
    assert drive['S'] == 103 and drive['C'] == 3
    # No cloud state recorded, so every file counts as fully local.
    assert drive['LS'] == drive['S'] and drive['LC'] == drive['C']
    # The "/" root must aggregate the drives too.
    assert root['S'] == 103 and root['C'] == 3
    assert root['LS'] == root['S'] and root['LC'] == root['C']


def test_windows_paths_are_web_normalized(client, config):
    snapshot = _windows_snapshot(config)
    client.post('/api/v1/load_snapshot', json={'snapshot_id': snapshot})

    listing = client.get('/api/v1/getPath', query_string={
        'snapshot': snapshot, 'path': '/C:'}).get_json()
    assert listing['current_path'] == '/C:'
    assert [f['name'] for f in listing['files']] == ['boot.ini']
    assert listing['files'][0]['cloud_state'] is None
    # Every response states which version of the tree it was built from.
    assert listing['snapshot_generation'] == 0
    assert listing['manifest_revision'] == 0
    assert listing['observation_coverage'] == 'complete'

    # The filtered response uses the same path format as the plain one.
    filtered = client.get('/api/v1/getPath', query_string={
        'snapshot': snapshot, 'path': '/C:', 'use_filter': 'true'}).get_json()
    assert filtered['current_path'] == listing['current_path']


def test_cloud_state_is_returned_with_file_entries(client, config):
    records = _windows_records()
    records[1]['c'] = ['not_fully_local']
    snapshot = _windows_snapshot(config, records)
    client.post('/api/v1/load_snapshot', json={'snapshot_id': snapshot})

    listing = client.get('/api/v1/getPath', query_string={
        'snapshot': snapshot, 'path': '/C:'}).get_json()
    assert listing['files'][0]['cloud_state'] == 'not_fully_local'
    assert listing['S'] == 103 and listing['C'] == 3
    assert listing['local_size'] == 3
    assert listing['local_file_count'] == 2
    assert listing['dirs'][0]['local_size'] == 3
    assert listing['dirs'][0]['local_file_count'] == 2


def test_filtered_totals_include_fully_local_values(client, config):
    records = _windows_records()
    records[1]['c'] = ['not_fully_local']
    snapshot = _windows_snapshot(config, records)
    client.post('/api/v1/load_snapshot', json={'snapshot_id': snapshot})
    listing = client.get('/api/v1/getPath', query_string={
        'snapshot': snapshot,
        'path': '/',
        'use_filter': 'true',
        'recursive_calc': 'true',
        'filter_in': json.dumps([]),
    }).get_json()
    assert listing['dirs'][0]['S'] == 103
    assert listing['dirs'][0]['local_size'] == 3
    assert listing['local_size'] == 3
    assert listing['local_file_count'] == 2


def test_to_web_path_is_noop_for_posix():
    assert to_web_path('/home/u', 'Linux') == '/home/u'


def test_deep_tree_does_not_hit_recursion_limit(config, app):
    """A 3000-level tree used to blow the Python stack while loading."""
    depth = 3000
    records = []
    for level in range(depth):
        node_path = '/' + '/'.join(f'd{i}' for i in range(level + 1))
        children = [f'd{level + 1}'] if level + 1 < depth else []
        records.append(make_record(
            node_path,
            files=[] if children else [('leaf', 7)],
            dirs=children,
        ))

    snapshot = write_snapshot(config, records, root_path='/d0')
    assert load_snapshot_file(snapshot)

    data = loaded_snapshots[snapshot]['data']
    assert data[0]['C'] == 1
    assert data[0]['S'] == 7


def test_filter_in_reaches_files_in_subdirectories(client, scanned_snapshot, sample_tree):
    """Selecting a single deep file must not be swallowed by the traversal."""
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    deep_file = os.path.join(str(sample_tree), 'sub', 'deep', 'c.txt')
    client.post(f'/api/v1/group/{scanned_snapshot}/add_file',
                json={'path': deep_file, 'group_name': 'pick'})

    files = get_filtered_files(scanned_snapshot, ['pick'], [])
    assert [f['full_path'] for f in files] == [deep_file]


def test_filter_in_on_directory_selects_whole_subtree(client, scanned_snapshot, sample_tree):
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    client.post(f'/api/v1/group/{scanned_snapshot}/add_dir',
                json={'path': os.path.join(str(sample_tree), 'sub'), 'group_name': 'pick'})

    files = get_filtered_files(scanned_snapshot, ['pick'], [])
    assert sorted(f['name'] for f in files) == ['b.txt', 'c.txt']


def test_filter_out_removes_a_subtree(client, scanned_snapshot, sample_tree):
    client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})
    client.post(f'/api/v1/group/{scanned_snapshot}/add_dir',
                json={'path': os.path.join(str(sample_tree), 'sub'), 'group_name': 'skip'})

    files = get_filtered_files(scanned_snapshot, [], ['skip'])
    assert [f['name'] for f in files] == ['a.txt']


def test_group_paths_from_the_ui_match_a_windows_snapshot(client, config):
    """The UI sends /C:/Users/a.txt; the snapshot is indexed as C:\\Users\\a.txt."""
    snapshot = _windows_snapshot(config)
    client.post('/api/v1/load_snapshot', json={'snapshot_id': snapshot})

    added = client.post(f'/api/v1/group/{snapshot}/add_file',
                        json={'path': '/C:/Users/a.txt', 'group_name': 'keep'})
    assert added.status_code == 200

    selected = [f['full_path'] for f in get_filtered_files(snapshot, ['keep'], [])]
    assert selected == ['C:\\Users\\a.txt']


def test_truncated_base_is_rejected(client, config, sample_tree, scanned_snapshot):
    """A half-written payload file must not load as a shorter snapshot."""
    layout = SnapshotLayout(os.path.join(config.snapshot_dir, scanned_snapshot))
    base = os.path.join(layout.dir, 'base_gen_000000.jsonl.gz')
    with open(base, 'rb') as handle:
        data = handle.read()
    with open(base, 'wb') as handle:
        handle.write(data[: len(data) // 2])

    response = client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})

    assert response.status_code == 400
    assert scanned_snapshot not in loaded_snapshots


def test_corrupt_snapshot_is_a_client_error(client, config, scanned_snapshot):
    """A snapshot from a killed writer is an operator problem, not a 500."""
    layout = SnapshotLayout(os.path.join(config.snapshot_dir, scanned_snapshot))
    with open(os.path.join(layout.dir, 'base_gen_000000.jsonl.gz'), 'wb') as handle:
        handle.write(b'not gzip at all')

    response = client.post('/api/v1/load_snapshot', json={'snapshot_id': scanned_snapshot})

    assert response.status_code == 400
    assert 'Invalid snapshot' in response.get_json()['error']


def test_missing_snapshot_is_a_client_error(client, config):
    response = client.post('/api/v1/load_snapshot',
                           json={'snapshot_id': 'snapshot_1700000000_deadbeef'})
    assert response.status_code == 400


def test_non_object_record_is_rejected(config):
    """A JSON value of the wrong shape must not escape as a TypeError 500."""
    from fshub.snapshot.loader import _decode_record

    with pytest.raises(InvalidSnapshot):
        _decode_record(b'["not", "an", "object"]', 'base')


def test_malformed_group_records_are_ignored(client, config):
    """Old or hand-edited group logs cannot poison snapshot loading."""
    snapshot = _windows_snapshot(config)
    layout = SnapshotLayout(os.path.join(config.snapshot_dir, snapshot))
    with open(layout.groups_path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(['/x', 'f', [], 'add', 0]) + '\n')
        f.write(json.dumps([[], 'f', 'bad', 'add', 0]) + '\n')
        f.write(json.dumps(['/x', 'wrong', 'bad', 'add', 0]) + '\n')

    response = client.post('/api/v1/load_snapshot', json={'snapshot_id': snapshot})
    assert response.status_code == 200
    assert loaded_snapshots[snapshot]['groups'] == {}
