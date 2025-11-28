"""Utility functions for fshub"""

import platform
import socket
import uuid


def get_system_info():
    """Get system information for the current device"""
    import psutil

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
        'storage_space': psutil.disk_usage('/').total if platform.system() != 'Windows' else psutil.disk_usage('C:\\').total,
        'ip_addr': socket.gethostbyname(socket.gethostname()),
        'mac_addr': mac,
        'os_name': platform.system(),  # OS name (e.g., Linux, Windows, Darwin)
        'os_version': platform.version(),  # OS version
        'os_release': platform.release(),  # OS release (e.g., kernel version on Linux)
        'thumbprint': calculate_thumbprint()
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


def format_bytes(bytes_value):
    """Format bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024.0:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.2f} PB"