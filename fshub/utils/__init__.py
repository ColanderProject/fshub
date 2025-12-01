"""Utility functions for fshub"""

import platform
import socket
import uuid
import os

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


def join_snapshot_path(base_path, *paths, snapshot_os=None):
    """
    Join paths based on the snapshot's OS platform.
    
    Args:
        base_path: The base path from the snapshot
        *paths: Additional path components to join
        snapshot_os: The OS of the snapshot (Windows, Linux, Darwin, etc.)
                    If None, will try to detect from path format
    
    Returns:
        Joined path using the appropriate separator for the snapshot's OS
    """
    if snapshot_os is None:
        # Try to detect OS from path format
        if base_path.startswith('/'):
            # Unix-like path (Linux/Darwin)
            snapshot_os = 'Linux'
        elif len(base_path) >= 2 and base_path[1] == ':':
            # Windows path (e.g., C:\ or C:/)
            snapshot_os = 'Windows'
        else:
            # Default to Unix-like
            snapshot_os = 'Linux'
    
    # Choose separator based on snapshot's OS
    if snapshot_os == 'Windows':
        separator = '\\'
        
        # Special case: Windows root "/" with drive letters
        if base_path == '/' and paths:
            # Check if the first path component is a drive letter (e.g., "C:")
            first_path = paths[0]
            if len(first_path) >= 2 and first_path[1] == ':':
                # This is a drive letter, use it directly
                if len(first_path) == 2:
                    result = first_path + '\\'
                else:
                    result = first_path.replace('/', '\\')
                # Append remaining paths
                for path in paths[1:]:
                    if path:
                        result = result.rstrip('\\') + '\\' + path.lstrip('\\/')
                return result
        
        # For consistency, convert any forward slashes to backslashes
        result = base_path.replace('/', '\\')
        for path in paths:
            if path:
                result = result.rstrip('\\') + '\\' + path.lstrip('\\/')
    else:
        # Unix-like systems (Linux, Darwin, etc.)
        separator = '/'
        # Convert any backslashes to forward slashes
        result = base_path.replace('\\', '/')
        for path in paths:
            if path:
                result = result.rstrip('/') + '/' + path.lstrip('\\/')
    
    return result
