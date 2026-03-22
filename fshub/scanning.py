"""Shared scan logic for API and CLI usage."""

from datetime import datetime
import gzip
import json
import os
import platform
import string
import time

from .config import Config
from .utils import get_system_info


def scan_windows_drives(counters, result_callback=None):
    """Scan all Windows drives when path is '/' on Windows."""
    all_results = []

    # Initialize counters
    counters['scanned_count'] = 0
    counters['scanned_size'] = 0
    counters['errors'] = []
    counters['current_path'] = '/'

    # Get all drive letters in Windows
    drives = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            drives.append(drive)

    # Create a root entry that represents "This PC"
    root_obj = {
        'p': '/',
        'f': [],
        'd': [f"{letter}:" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")],
        't': [],
        'T': [],
        's': []
    }

    # Get timestamps for each drive
    for drive in drives:
        try:
            stat = os.stat(drive)
            root_obj['T'].append([
                int(stat.st_ctime),
                int(stat.st_mtime),
                int(stat.st_atime)
            ])
        except OSError:
            root_obj['T'].append([0, 0, 0])

    all_results.append(root_obj)

    for drive in drives:
        counters['current_path'] = drive
        if result_callback:
            result_callback(counters)
        drive_results = scan(drive, counters, result_callback)
        all_results.extend(drive_results)

    return all_results


def scan(path, counters, result_callback=None):
    """Scan a directory and return structured data about files and folders."""
    result = []

    # Initialize counters
    counters['scanned_count'] = 0
    counters['scanned_size'] = 0
    counters['errors'] = []
    counters['current_path'] = path

    for root, dirs, files in os.walk(path):
        try:
            counters['current_path'] = root
            if result_callback:
                result_callback(counters)

            path_obj = {
                'p': root,
                'f': [],
                'd': [],
                't': [],
                'T': [],
                's': []
            }

            for file in files:
                file_path = os.path.join(root, file)
                try:
                    stat = os.stat(file_path)
                    path_obj['f'].append(file)
                    path_obj['s'].append(stat.st_size)
                    path_obj['t'].append([
                        int(stat.st_ctime),
                        int(stat.st_mtime),
                        int(stat.st_atime)
                    ])

                    counters['scanned_count'] += 1
                    counters['scanned_size'] += stat.st_size

                    if result_callback:
                        result_callback(counters)

                except OSError as e:
                    counters['errors'].append(f"Error accessing file {file_path}: {str(e)}")

            for directory in dirs:
                dir_path = os.path.join(root, directory)
                try:
                    stat = os.stat(dir_path)
                    path_obj['d'].append(directory)
                    path_obj['T'].append([
                        int(stat.st_ctime),
                        int(stat.st_mtime),
                        int(stat.st_atime)
                    ])
                except OSError as e:
                    counters['errors'].append(f"Error accessing directory {dir_path}: {str(e)}")

            result.append(path_obj)
        except OSError as e:
            counters['errors'].append(f"Error accessing directory {root}: {str(e)}")

    return result


def save_scan_result(scan_result, use_index=False):
    """Save scan results to the configured snapshot directory."""
    timestamp = int(time.time())
    config = Config()
    snapshot_dir = os.path.join(config.data_path, 'snapshots')
    os.makedirs(snapshot_dir, exist_ok=True)

    if use_index:
        filename_base = f"snapshot_{timestamp}_{len(scan_result)}"
        index_filename = f"{filename_base}_index.jsonl.gz"
        bin_filename = f"{filename_base}.bin.gz"

        index_filepath = os.path.join(snapshot_dir, index_filename)
        bin_filepath = os.path.join(snapshot_dir, bin_filename)

        index_data = []
        with gzip.open(bin_filepath, 'wt', encoding='utf-8') as bin_file:
            for item in scan_result:
                compressed_item = {k: v for k, v in item.items() if k != 'p'}
                original_path = item['p']
                compressed_str = json.dumps(compressed_item)
                bin_file.write(compressed_str + '\n')
                index_data.append({
                    'p': original_path,
                    'compressed_length': len(compressed_str)
                })

        with gzip.open(index_filepath, 'wt', encoding='utf-8') as index_file:
            for idx_item in index_data:
                index_file.write(json.dumps(idx_item) + '\n')

        return {
            'result_file': index_filename,
            'result_path': index_filepath,
            'data_file': bin_filename,
            'data_path': bin_filepath,
        }

    filename = f"snapshot_{timestamp}_{len(scan_result)}.jsonl.gz"
    filepath = os.path.join(snapshot_dir, filename)

    with gzip.open(filepath, 'wt', encoding='utf-8') as f:
        for item in scan_result:
            f.write(json.dumps(item) + '\n')

    return {
        'result_file': filename,
        'result_path': filepath,
    }


def run_scan_to_snapshot(scan_path, use_index=False, counters=None, result_callback=None):
    """Run a scan and save its output to a snapshot file."""
    counters = counters if counters is not None else {}
    start_time = datetime.now()

    if platform.system() == 'Windows' and scan_path == '/':
        scan_result = scan_windows_drives(counters, result_callback)
    else:
        scan_result = scan(scan_path, counters, result_callback)

    finish_time = datetime.now()

    if scan_result:
        system_info = get_system_info()
        scan_result[0]['device_name'] = system_info['device_name']
        scan_result[0]['device_id'] = system_info['thumbprint']
        scan_result[0]['cpu_model'] = system_info['cpu_model']
        scan_result[0]['cpu_name'] = system_info['cpu_model']
        scan_result[0]['memory_size'] = system_info['memory_size']
        scan_result[0]['host_name'] = system_info['host_name']
        scan_result[0]['ip_addr'] = system_info['ip_addr']
        scan_result[0]['mac_addr'] = system_info['mac_addr']
        scan_result[0]['os_name'] = system_info['os_name']
        scan_result[0]['start_scan_time'] = int(start_time.timestamp())
        scan_result[0]['finish_scan_time'] = int(finish_time.timestamp())

    saved_result = save_scan_result(scan_result, use_index=use_index)
    counters['current_path'] = scan_path

    return {
        'result_file': saved_result['result_file'],
        'result_path': saved_result['result_path'],
        'entry_count': len(scan_result),
        'counters': counters,
        **{k: v for k, v in saved_result.items() if k not in {'result_file', 'result_path'}},
    }
