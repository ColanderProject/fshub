"""Shared scan logic for API and CLI usage."""

from datetime import datetime
import gzip
import json
import os
import platform
import string
import time
import uuid

from .config import get_config
from .scan_logs import MAX_REPORTED_ERRORS, ScanRunLog
from .utils import get_system_info


def _normalize_prefix(path):
    """Normalize a path for prefix comparisons (no symlink resolution)."""
    return os.path.normcase(os.path.abspath(path))


def _canonical_path(path):
    """Like _normalize_prefix, but resolves symlinks so aliases compare equal."""
    return os.path.normcase(os.path.realpath(path))


def is_related_path(path_a, path_b):
    """True when one path is the other, or contains it.

    Compares whole path components so ``/home/a`` and ``/home/ab`` are not
    considered related, and resolves symlinks so two aliases of one tree are
    not scanned concurrently. On Windows ``/`` means "every drive", so it is
    related to any path.
    """
    if platform.system() == 'Windows' and '/' in (path_a, path_b):
        return True

    a = _canonical_path(path_a).rstrip(os.sep)
    b = _canonical_path(path_b).rstrip(os.sep)
    if a == b:
        return True
    return a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _init_counters(counters, current_path):
    """Initialise shared counters once per scan run."""
    counters.setdefault('scanned_count', 0)
    counters.setdefault('scanned_size', 0)
    counters.setdefault('errors', [])
    counters.setdefault('error_count', len(counters['errors']))
    counters['current_path'] = current_path


def _normalize_skip_prefixes(skip_prefixes):
    """Normalize configured skip prefixes."""
    return [_normalize_prefix(path).rstrip(os.sep) or os.sep
            for path in skip_prefixes if path]


def _should_skip_path(path, normalized_skip_prefixes):
    """Return True when a path is, or lives under, a configured skip prefix.

    Whole path components are compared, mirroring is_related_path: skipping
    ``/home/a`` must not also skip the unrelated sibling ``/home/ab``.
    """
    normalized_path = _normalize_prefix(path)
    for prefix in normalized_skip_prefixes:
        if normalized_path == prefix:
            return True
        if normalized_path.startswith(prefix.rstrip(os.sep) + os.sep):
            return True
    return False


def scan_windows_drives(counters, result_callback=None, skip_prefixes=None,
                        error_callback=None):
    """Scan all Windows drives when path is '/' on Windows."""
    all_results = []
    normalized_skip_prefixes = _normalize_skip_prefixes(skip_prefixes or [])

    _init_counters(counters, '/')

    # Skipped drives are dropped here, so the root entry never advertises a
    # drive that was not scanned.
    drives = [f'{letter}:\\' for letter in string.ascii_uppercase
              if os.path.exists(f'{letter}:\\')
              and not _should_skip_path(f'{letter}:\\', normalized_skip_prefixes)]

    # Create a root entry that represents "This PC"
    root_obj = {
        'p': '/',
        'f': [],
        'd': [drive[:2] for drive in drives],
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
        except OSError as e:
            root_obj['T'].append([0, 0, 0])
            _record_error(
                counters,
                f"Error accessing directory {drive}: {str(e)}",
                error_callback,
            )

    all_results.append(root_obj)

    for drive in drives:
        counters['current_path'] = drive
        if result_callback:
            result_callback(counters)
        drive_results = scan(
            drive,
            counters,
            result_callback,
            skip_prefixes=skip_prefixes,
            error_callback=error_callback,
        )
        all_results.extend(drive_results)

    return all_results


def _record_error(counters, message, error_callback=None):
    """Count every error but keep only a bounded sample in task status."""
    counters['error_count'] += 1
    if len(counters['errors']) < MAX_REPORTED_ERRORS:
        counters['errors'].append(message)
    if error_callback:
        error_callback(message, counters)


def scan(path, counters, result_callback=None, skip_prefixes=None,
         error_callback=None):
    """Scan a directory and return structured data about files and folders."""
    result = []
    normalized_skip_prefixes = _normalize_skip_prefixes(skip_prefixes or [])

    # Counters accumulate across calls so scanning several Windows drives
    # reports a single combined total.
    _init_counters(counters, path)

    def walk_error(error):
        error_path = getattr(error, 'filename', None) or path
        _record_error(
            counters,
            f"Error accessing directory {error_path}: {str(error)}",
            error_callback,
        )

    for root, dirs, files in os.walk(path, onerror=walk_error):
        try:
            if _should_skip_path(root, normalized_skip_prefixes):
                dirs[:] = []
                continue

            counters['current_path'] = root
            if result_callback:
                result_callback(counters)

            dirs[:] = [
                directory
                for directory in dirs
                if not _should_skip_path(os.path.join(root, directory), normalized_skip_prefixes)
            ]

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
                if _should_skip_path(file_path, normalized_skip_prefixes):
                    continue
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
                    _record_error(
                        counters,
                        f"Error accessing file {file_path}: {str(e)}",
                        error_callback,
                    )

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
                    _record_error(
                        counters,
                        f"Error accessing directory {dir_path}: {str(e)}",
                        error_callback,
                    )

            result.append(path_obj)
        except OSError as e:
            _record_error(
                counters,
                f"Error accessing directory {root}: {str(e)}",
                error_callback,
            )

    return result


def save_scan_result(scan_result, use_index=False):
    """Save scan results to the configured snapshot directory."""
    config = get_config()
    snapshot_dir = config.snapshot_dir
    os.makedirs(snapshot_dir, exist_ok=True)

    # A wall-clock second plus an entry count is not unique: two concurrent
    # scans can produce the same base name and one would overwrite the other
    # (taking its group log with it), so add a random suffix.
    filename_base = f'snapshot_{int(time.time())}_{len(scan_result)}_{uuid.uuid4().hex[:8]}'

    if use_index:
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

    filename = f"{filename_base}.jsonl.gz"
    filepath = os.path.join(snapshot_dir, filename)

    with gzip.open(filepath, 'wt', encoding='utf-8') as f:
        for item in scan_result:
            f.write(json.dumps(item) + '\n')

    return {
        'result_file': filename,
        'result_path': filepath,
    }


def run_scan_to_snapshot(scan_path, use_index=False, counters=None,
                         result_callback=None, skip_prefixes=None, scan_id=None,
                         run_log=None):
    """Run a scan, save its snapshot, and durably log its status/errors."""
    counters = counters if counters is not None else {}
    start_time = datetime.now()
    counters['skip_prefixes'] = list(skip_prefixes or [])
    _init_counters(counters, scan_path)

    if run_log is None:
        run_log = ScanRunLog(scan_id)
        run_log.started(scan_path, use_index=use_index, skip_paths=skip_prefixes)

    def report_progress(current_counters):
        run_log.progress(current_counters)
        if result_callback:
            result_callback(current_counters)

    try:
        if platform.system() == 'Windows' and scan_path == '/':
            scan_result = scan_windows_drives(
                counters,
                report_progress,
                skip_prefixes=skip_prefixes,
                error_callback=run_log.scan_error,
            )
        else:
            scan_result = scan(
                scan_path,
                counters,
                report_progress,
                skip_prefixes=skip_prefixes,
                error_callback=run_log.scan_error,
            )

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
        run_log.completed(counters, saved_result['result_file'])
    except Exception as error:
        run_log.failed(error, counters)
        raise

    return {
        'scan_id': run_log.scan_id,
        'scan_log': run_log.path,
        'result_file': saved_result['result_file'],
        'result_path': saved_result['result_path'],
        'entry_count': len(scan_result),
        'counters': counters,
        **{k: v for k, v in saved_result.items() if k not in {'result_file', 'result_path'}},
    }
