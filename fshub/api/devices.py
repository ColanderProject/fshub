"""Device management API endpoints"""

import json
import os
import threading
import time

from flask import Blueprint, request, jsonify

from ..config import get_config
from ..utils import UnsafePathError, get_system_info, safe_join

device_bp = Blueprint('device_bp', __name__)

DEVICE_PREFIX = 'devices_'
MEDIA_PREFIX = 'media_'
DEVICE_SUFFIX = '.jl'

_stamp_lock = threading.Lock()
_last_stamp = 0.0


def _next_stamp():
    """A strictly increasing write stamp.

    Deduplication needs a total order over device records. The wall clock
    alone is not enough: on Windows time.time() only ticks every ~15 ms, so
    two quick updates share a stamp and the tie-break falls back to file
    name order, which says nothing about recency.
    """
    global _last_stamp
    with _stamp_lock:
        now = time.time()
        if now <= _last_stamp:
            now = _last_stamp + 1e-6
        _last_stamp = now
        return now


def _device_file(hostname, prefix):
    """Resolve a per-host data file, refusing anything that escapes the dir."""
    return safe_join(
        get_config().devices_dir,
        f'{prefix}{hostname}{DEVICE_SUFFIX}',
        what='host name',
    )


def _read_jl(path):
    records = []
    if not os.path.exists(path):
        return records
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return records


def _load_all_devices():
    """Yield (ordering_key, device) for every stored device record.

    The ordering key is (updated_at, file mtime, line number) so that records
    written before updated_at existed still fall back to something sensible
    rather than to file name order.
    """
    devices_dir = get_config().devices_dir
    try:
        entries = os.listdir(devices_dir)
    except OSError:
        return []

    all_devices = []
    for name in sorted(entries):
        if not (name.startswith(DEVICE_PREFIX) and name.endswith(DEVICE_SUFFIX)):
            continue
        path = os.path.join(devices_dir, name)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0
        for line_no, device in enumerate(_read_jl(path)):
            all_devices.append(((device.get('updated_at', 0), mtime, line_no), device))
    return all_devices


def _device_key(device):
    """Identify a device by thumbprint, falling back to host name."""
    return device.get('thumbprint') or device.get('host_name')


@device_bp.route('/api/v1/devices', methods=['GET'])
def get_devices():
    """Get all devices"""
    # Records for one device may live in several files (host_name can change
    # while the thumbprint stays put), so file order says nothing about
    # recency. Keep the record with the highest ordering key instead.
    best = {}
    for order, device in _load_all_devices():
        key = _device_key(device)
        if key not in best or order > best[key][0]:
            best[key] = (order, device)

    current_info = get_system_info()
    current_known = _device_key(current_info) in best

    return jsonify({
        'devices': [device for _order, device in best.values()],
        'current_device_known': current_known,
        'current_device_info': current_info
    })


@device_bp.route('/api/v1/devices', methods=['POST'])
def add_device():
    """Add or update a device"""
    device = request.get_json(silent=True) or {}
    hostname = device.get('host_name')

    if not hostname:
        return jsonify({'error': 'host_name is required'}), 400

    try:
        devices_path = _device_file(hostname, DEVICE_PREFIX)
        media_path = _device_file(hostname, MEDIA_PREFIX)
    except UnsafePathError as e:
        return jsonify({'error': str(e)}), 400

    media = device.pop('media', None)

    # Stamped on write so deduplication has a reliable ordering key.
    device['updated_at'] = _next_stamp()

    with open(devices_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(device) + '\n')

    if media is not None:
        with open(media_path, 'w', encoding='utf-8') as f:
            for item in media:
                f.write(json.dumps(item) + '\n')

    return jsonify({'success': True})


@device_bp.route('/api/v1/device/<hostname>/media', methods=['GET'])
def get_device_media(hostname):
    """Get media information for a specific device"""
    try:
        media_path = _device_file(hostname, MEDIA_PREFIX)
    except UnsafePathError as e:
        return jsonify({'error': str(e)}), 400

    return jsonify({'media': _read_jl(media_path)})
