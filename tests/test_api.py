"""End-to-end tests over the HTTP API using a real scanned snapshot."""

import json
import os


def load(client, snapshot):
    return client.post('/api/v1/load_snapshot', json={'filename': snapshot})


def test_health_and_index(client):
    assert client.get('/api/v1/health').status_code == 200
    assert client.get('/').status_code == 200


def test_no_login_endpoint_remains(app):
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert '/api/v1/login' not in rules


def test_empty_data_dir_lists_nothing(client):
    assert client.get('/api/v1/snapshots').get_json() == {'snapshots': []}
    assert client.get('/api/v1/scans').get_json() == {'scan_files': []}
    assert client.get('/api/v1/devices').get_json()['devices'] == []


def test_scan_produces_loadable_snapshot(client, scanned_snapshot, sample_tree):
    listing = client.get('/api/v1/snapshots').get_json()['snapshots']
    assert [s['filename'] for s in listing] == [scanned_snapshot]

    response = load(client, scanned_snapshot)
    assert response.status_code == 200
    info = response.get_json()['snapshot_info']
    assert info['total_files'] == 3
    assert info['total_size'] == 10 + 20 + 30
    assert info['root_path'] == str(sample_tree)


def test_recursive_totals_roll_up(client, scanned_snapshot, sample_tree):
    load(client, scanned_snapshot)

    root = client.get('/api/v1/getPath', query_string={
        'snapshot': scanned_snapshot, 'path': str(sample_tree)}).get_json()

    assert root['C'] == 3
    assert root['S'] == 60

    sub = next(d for d in root['dirs'] if d['name'] == 'sub')
    assert sub['C'] == 2
    assert sub['S'] == 50


def test_get_path_unknown_path_is_404(client, scanned_snapshot):
    load(client, scanned_snapshot)
    response = client.get('/api/v1/getPath', query_string={
        'snapshot': scanned_snapshot, 'path': '/nope'})
    assert response.status_code == 404


def test_get_path_requires_loaded_snapshot(client):
    response = client.get('/api/v1/getPath', query_string={
        'snapshot': 'snapshot_1_1.jsonl.gz', 'path': '/'})
    assert response.status_code == 400


def test_search_and_limit(client, scanned_snapshot):
    load(client, scanned_snapshot)

    data = client.post('/api/v1/search', json={'query': '.txt'}).get_json()
    assert data['count'] == 3
    assert data['truncated'] is False

    data = client.post('/api/v1/search', json={'query': '.txt', 'limit': 2}).get_json()
    assert data['count'] == 2
    assert data['truncated'] is True

    data = client.post('/api/v1/search', json={'query': 'starts:b'}).get_json()
    assert [r['name'] for r in data['results']] == ['b.txt']


def test_search_requires_query(client):
    assert client.post('/api/v1/search', json={}).status_code == 400


def test_endpoints_tolerate_missing_json_body(client):
    for url in ('/api/v1/load_snapshot', '/api/v1/unload_snapshot',
                '/api/v1/search', '/api/v1/scan',
                '/api/v1/backup/zip', '/api/v1/backup/folder'):
        response = client.post(url)
        assert response.status_code == 400, url


def test_group_roundtrip_and_filtering(client, scanned_snapshot, sample_tree):
    load(client, scanned_snapshot)
    target = os.path.join(str(sample_tree), 'a.txt')

    added = client.post(f'/api/v1/group/{scanned_snapshot}/add_file',
                        json={'path': target, 'group_name': 'keep'})
    assert added.status_code == 200

    groups = client.get(f'/api/v1/groups/{scanned_snapshot}').get_json()['groups']
    assert groups == [{'name': 'keep', 'file_count': 1, 'dir_count': 0, 'total_count': 1}]

    filtered = client.get('/api/v1/getPath', query_string={
        'snapshot': scanned_snapshot,
        'path': str(sample_tree),
        'use_filter': 'true',
        'filter_out': json.dumps(['keep']),
    }).get_json()
    assert [f['name'] for f in filtered['files']] == []

    # The action log survives a reload.
    client.post('/api/v1/unload_snapshot', json={'filename': scanned_snapshot})
    load(client, scanned_snapshot)
    groups = client.get(f'/api/v1/groups/{scanned_snapshot}').get_json()['groups']
    assert groups[0]['file_count'] == 1

    client.post(f'/api/v1/group/{scanned_snapshot}/remove_file',
                json={'path': target, 'group_name': 'keep'})
    client.post('/api/v1/unload_snapshot', json={'filename': scanned_snapshot})
    load(client, scanned_snapshot)
    groups = client.get(f'/api/v1/groups/{scanned_snapshot}').get_json()['groups']
    assert groups[0]['file_count'] == 0


def test_group_endpoints_require_loaded_snapshot(client):
    response = client.post('/api/v1/group/snapshot_1_1.jsonl.gz/add_file',
                           json={'path': '/x', 'group_name': 'g'})
    assert response.status_code == 400


def test_device_registration_is_deduplicated(client):
    info = client.get('/api/v1/devices').get_json()
    assert info['current_device_known'] is False

    device = dict(info['current_device_info'], device_type='PC')
    assert client.post('/api/v1/devices', json=device).status_code == 200
    assert client.post('/api/v1/devices', json=device).status_code == 200

    info = client.get('/api/v1/devices').get_json()
    assert len(info['devices']) == 1
    assert info['current_device_known'] is True
