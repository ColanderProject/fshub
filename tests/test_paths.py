"""Tests for path safety helpers and the endpoints that rely on them."""

import pytest

from fshub.utils import (
    UnsafePathError,
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
    ('/home/u', 'Linux', '/home'),
    ('/home', 'Linux', None),
    ('C:\\Users\\u', 'Windows', 'C:\\Users'),
    ('C:\\', 'Windows', None),
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


def test_add_device_rejects_bad_hostname(client):
    response = client.post('/api/v1/devices', json={'host_name': '../evil'})
    assert response.status_code == 400
