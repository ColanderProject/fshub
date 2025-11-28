"""Device management API endpoints"""

from flask import Blueprint, request, jsonify
import os
import json
import socket
import uuid
import platform
import psutil
from pathlib import Path
from datetime import datetime
from ..config import Config

device_bp = Blueprint('device_bp', __name__)

# Global variable to store loaded devices
loaded_devices = {}
current_device = None


def get_system_info():
    """Get system information for the current device"""
    # Get MAC address
    mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff)
                   for elements in range(0,2*6,2)][::-1])

    device_info = {
        'device_name': platform.node(),  # Default to hostname
        'device_type': 'PC',  # Will be updated by user
        'device_model': platform.machine(),
        'host_name': platform.node(),
        'cpu_model': platform.processor(),
        'memory_size': psutil.virtual_memory().total,
        'storage_space': psutil.disk_usage('/').total if os.name != 'nt' else psutil.disk_usage('C:\\').total,
        'ip_addr': socket.gethostbyname(socket.gethostname()),
        'mac_addr': mac,
        'os_name': platform.system(),  # OS name (e.g., Linux, Windows, Darwin)
        'os_version': platform.version(),  # OS version
        'os_release': platform.release(),  # OS release (e.g., kernel version on Linux)
        'thumbprint': calculate_thumbprint(),
        'freedesktop_os_release': platform.freedesktop_os_release()
    }
    return device_info


def calculate_thumbprint():
    """Calculate a unique thumbprint for the current machine"""
    import hashlib

    # Combine various system identifiers to create a unique thumbprint
    identifiers = [
        platform.node(),  # hostname
        platform.machine(),  # architecture
        platform.processor(),  # CPU
        platform.system(),  # OS name
        platform.release(),  # OS release
        str(uuid.getnode()),  # MAC address
    ]

    combined = ''.join(identifiers).encode('utf-8')
    return hashlib.sha256(combined).hexdigest()


@device_bp.route('/api/v1/devices', methods=['GET'])
def get_devices():
    """Get all devices"""
    config = Config()
    devices_dir = os.path.join(config.data_path, 'devices')
    
    # Load all device files
    device_files = [f for f in os.listdir(devices_dir) if f.startswith('devices_') and f.endswith('.jl')]
    
    all_devices = []
    for file in device_files:
        filepath = os.path.join(devices_dir, file)
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    device = json.loads(line)
                    all_devices.append(device)
    
    # Check if current device is in the list
    current_hostname = socket.gethostname()
    current_device_exists = any(device.get('host_name') == current_hostname for device in all_devices)
    
    return jsonify({
        'devices': all_devices,
        'current_device_known': current_device_exists,
        'current_device_info': get_system_info()
    })


@device_bp.route('/api/v1/devices', methods=['POST'])
def add_device():
    """Add a new device"""
    config = Config()
    device = request.get_json()
    
    device_file = f"devices_{device['host_name']}.jl"
    devices_path = os.path.join(config.data_path, 'devices', device_file)
    
    with open(devices_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(device) + '\n')
    
    # Also save media info if provided
    if 'media' in device:
        media_file = f"media_{device['host_name']}.jl"
        media_path = os.path.join(config.data_path, 'devices', media_file)
        with open(media_path, 'w', encoding='utf-8') as f:
            for media in device['media']:
                f.write(json.dumps(media) + '\n')
    
    return jsonify({'success': True})


@device_bp.route('/api/v1/device/<hostname>/media', methods=['GET'])
def get_device_media(hostname):
    """Get media information for a specific device"""
    config = Config()
    media_file = f"media_{hostname}.jl"
    media_path = os.path.join(config.data_path, 'devices', media_file)
    
    if not os.path.exists(media_path):
        return jsonify({'media': []})
    
    media_list = []
    with open(media_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                media_list.append(json.loads(line))
    
    return jsonify({'media': media_list})