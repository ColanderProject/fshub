"""Tests for path safety helpers and the endpoints that rely on them."""

import json
import os
from urllib.parse import quote

import pytest

from fshub.utils import (
    UnsafePathError,
    encode_name_component,
    ensure_within,
    safe_join,
    sanitize_name,
    snapshot_dirname,
    snapshot_relative_path,
)


@pytest.mark.parametrize('name', ['..', '../x', 'a/b', 'a\\b', '', '/etc/passwd', 'a b'])
def test_sanitize_name_rejects_traversal(name):
    with pytest.raises(UnsafePathError):
        sanitize_name(name)


@pytest.mark.parametrize('name', ['snapshot_1_2.jsonl.gz', 'devices_host-1.jl', 'a.b_c-d'])
def test_sanitize_name_accepts_plain_names(name):
    assert sanitize_name(name) == name


def test_safe_join_stays_in_base(tmp_path):
    assert safe_join(str(tmp_path), 'ok.jl').startswith(str(tmp_path.resolve()))


def test_safe_join_rejects_escape(tmp_path):
    with pytest.raises(UnsafePathError):
        safe_join(str(tmp_path), '../escape.jl')


def test_ensure_within_rejects_escape(tmp_path):
    with pytest.raises(UnsafePathError):
        ensure_within(str(tmp_path), str(tmp_path.parent / 'other'))


@pytest.mark.parametrize('full_path,snapshot_os,expected', [
    ('/home/u/a.txt', 'Linux', 'home/u/a.txt'),
    ('C:\\Users\\u\\a.txt', 'Windows', 'C/Users/u/a.txt'),
    ('/../../etc/passwd', 'Linux', 'etc/passwd'),
    ('C:\\', 'Windows', 'C'),
])
def test_snapshot_relative_path_is_relative_and_safe(full_path, snapshot_os, expected):
    result = snapshot_relative_path(full_path, snapshot_os)
    assert result == expected
    assert not result.startswith('/')
    assert '..' not in result.split('/')


@pytest.mark.parametrize('path,snapshot_os,expected', [
    ('/home/u/a', 'Linux', '/home/u'),
    ('/home/u', 'Linux', '/home'),
    # The parent of a top level directory is the root itself, which is what
    # the snapshot index is keyed on.
    ('/home', 'Linux', '/'),
    ('/', 'Linux', None),
    ('C:\\Users\\u', 'Windows', 'C:\\Users'),
    # ... and on Windows the drive root keeps its separator, or it would not
    # match the indexed path.
    ('C:\\Users', 'Windows', 'C:\\'),
    ('C:\\', 'Windows', None),
    ('C:', 'Windows', None),
])
def test_snapshot_dirname(path, snapshot_os, expected):
    assert snapshot_dirname(path, snapshot_os=snapshot_os) == expected


def test_load_snapshot_rejects_traversal(client):
    response = client.post('/api/v1/load_snapshot',
                           json={'filename': '../../../etc/passwd.jsonl.gz'})
    assert response.status_code == 400


def test_load_snapshot_rejects_wrong_suffix(client):
    response = client.post('/api/v1/load_snapshot', json={'filename': 'passwd'})
    assert response.status_code == 400


def test_device_media_rejects_traversal(client):
    response = client.get('/api/v1/device/..%2F..%2Fetc/media')
    assert response.status_code in (400, 404)


def test_add_device_neutralizes_traversal_hostname(client, config):
    """A traversal-looking host name is encoded, never used as a path."""
    response = client.post('/api/v1/devices', json={'host_name': '../evil'})
    assert response.status_code == 200

    written = os.listdir(config.devices_dir)
    assert written == ['devices_..%2Fevil.jl']
    assert not os.path.exists(os.path.join(config.data_path, '..', 'evil'))


@pytest.mark.parametrize('value,expected', [
    # Plain names must stay untouched so existing files keep being found.
    ('host.local', 'host.local'),
    ('web-1', 'web-1'),
    ("Ann's MacBook Pro", 'Ann%27s%20MacBook%20Pro'),
    ('\u529e\u516c\u5ba4-PC', '%E5%8A%9E%E5%85%AC%E5%AE%A4-PC'),
    ('a/b', 'a%2Fb'),
    ('..%2f..', '..%252f..'),
])
def test_encode_name_component(value, expected):
    assert encode_name_component(value) == expected
    # Whatever comes out must be usable as a file name component.
    sanitize_name(encode_name_component(value))


def test_encode_name_component_is_injective_for_tricky_names():
    names = ['a b', 'a%20b', 'a/b', 'a%2Fb', '.', '..', 'x' * 400, 'y' * 400]
    assert len({encode_name_component(n) for n in names}) == len(names)


def test_encode_name_component_shortens_long_names():
    encoded = encode_name_component('h' * 500)
    assert len(encoded) <= 120
    sanitize_name(encoded)


@pytest.mark.parametrize('value', ['', None, 123])
def test_encode_name_component_requires_text(value):
    with pytest.raises(UnsafePathError):
        encode_name_component(value)


def test_device_roundtrip_with_unusual_hostname(client):
    """Host names with spaces or non-ASCII are normal, not an attack."""
    hostname = "Ann's MacBook Pro \u529e\u516c\u5ba4"

    response = client.post('/api/v1/devices', json={
        'host_name': hostname,
        'thumbprint': 'tp-1',
        'media': [{'name': 'disk0'}],
    })
    assert response.status_code == 200

    devices = client.get('/api/v1/devices').get_json()['devices']
    assert [d['host_name'] for d in devices] == [hostname]

    media = client.get(f'/api/v1/device/{hostname}/media')
    assert media.get_json() == {'media': [{'name': 'disk0'}]}


def test_ensure_within_accepts_children_of_a_root(tmp_path):
    """'/' + os.sep would be '//' and reject every child of the root."""
    root = os.path.abspath(os.sep)
    assert ensure_within(root, os.path.join(root, 'anything')).startswith(root)

    # The same normalisation issue appears with a trailing separator.
    assert ensure_within(str(tmp_path) + os.sep, tmp_path / 'x') == str(tmp_path / 'x')


def test_legacy_device_file_is_migrated(client, config):
    """Media saved before host names were encoded must stay reachable."""
    hostname = "Ann's PC"
    legacy = os.path.join(config.devices_dir, f'media_{hostname}.jl')
    with open(legacy, 'w', encoding='utf-8') as f:
        f.write(json.dumps({'name': 'usb'}) + '\n')

    response = client.get(f'/api/v1/device/{quote(hostname)}/media')

    assert response.get_json()['media'] == [{'name': 'usb'}]
    assert not os.path.exists(legacy)
    assert os.path.exists(os.path.join(
        config.devices_dir, f'media_{encode_name_component(hostname)}.jl'))


def test_digest_fallback_cannot_collide_with_a_normal_name():
    """The '%-' marker never appears in ordinary output, so the two namespaces
    stay disjoint even when a host name imitates a hashed one."""
    hashed = encode_name_component('z' * 500)
    assert '%-' in hashed
    assert encode_name_component(hashed) != hashed
