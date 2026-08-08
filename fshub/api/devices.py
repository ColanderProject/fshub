"""Device management API endpoints"""

import json
import os

from flask import Blueprint, request, jsonify

from ..config import get_config
from ..utils import UnsafePathError, get_system_info, safe_join

device_bp = Blueprint('device_bp', __name__)

DEVICE_PREFIX = 'devices_'
MEDIA_PREFIX = 'media_'
DEVICE_SUFFIX = '.jl'


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
    devices_dir = get_config().devices_dir
    try:
        entries = os.listdir(devices_dir)
    except OSError:
        return []

    all_devices = []
    for name in sorted(entries):
        if name.startswith(DEVICE_PREFIX) and name.endswith(DEVICE_SUFFIX):
            all_devices.extend(_read_jl(os.path.join(devices_dir, name)))
    return all_devices


def _device_key(device):
    """Identify a device by thumbprint, falling back to host name."""
    return device.get('thumbprint') or device.get('host_name')


@device_bp.route('/api/v1/devices', methods=['GET'])
def get_devices():
    """Get all devices"""
    # Later records for the same device win, so an update overwrites the old one.
    unique = {}
    for device in _load_all_devices():
        unique[_device_key(device)] = device

    current_info = get_system_info()
    current_known = _device_key(current_info) in unique

    return jsonify({
        'devices': list(unique.values()),
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
