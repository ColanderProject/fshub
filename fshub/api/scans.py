"""Scanning API endpoints"""

import os
import platform
import threading
import traceback
import uuid
from datetime import datetime

from flask import Blueprint, jsonify

from ..config import get_config
from ..scan_logs import list_scan_logs, read_scan_log, summarize_scan_log
from ..scanning import is_related_path, run_scan_to_snapshot
from . import json_body
from .explorer import list_snapshot_files

scan_bp = Blueprint('scan_bp', __name__)

# Thread-safe registry of scans started by this process.
running_scans = {}
scan_lock = threading.Lock()

# How many finished scans to keep around for status polling.
MAX_FINISHED_SCANS = 50


def _prune_finished_scans():
    """Drop the oldest finished scans so the registry cannot grow forever."""
    finished = [
        (info['start_time'], scan_id)
        for scan_id, info in running_scans.items()
        if info['status'] in ('completed', 'completed_with_errors', 'error')
    ]
    if len(finished) <= MAX_FINISHED_SCANS:
        return
    finished.sort()
    for _, scan_id in finished[: len(finished) - MAX_FINISHED_SCANS]:
        running_scans.pop(scan_id, None)


@scan_bp.route('/api/v1/scan', methods=['POST'])
def start_scan():
    """Start a scan of a directory"""
    data = json_body()
    scan_path = data.get('path', '')
    skip_paths = data.get('skip_paths', [])
    use_index = data.get('use_index', False)

    if not isinstance(use_index, bool):
        return jsonify({'error': 'use_index must be a boolean'}), 400

    if not isinstance(skip_paths, list) or not all(
            isinstance(path, str) and path for path in skip_paths):
        return jsonify({'error': 'skip_paths must be a list of non-empty strings'}), 400

    if not isinstance(scan_path, str):
        return jsonify({'error': 'Invalid path'}), 400

    # On Windows "/" means "This PC", i.e. scan every drive.
    is_windows_root = platform.system() == 'Windows' and scan_path == '/'
    if not is_windows_root and (not scan_path or not os.path.isdir(scan_path)):
        return jsonify({'error': 'Invalid path'}), 400

    with scan_lock:
        _prune_finished_scans()

        for existing_id, info in running_scans.items():
            if info['status'] != 'running':
                continue
            if is_related_path(info['path'], scan_path):
                return jsonify({
                    'error': f"Scan already running for related path: {info['path']}",
                    'scan_id': existing_id,
                }), 409

        scan_id = str(uuid.uuid4())
        counters = {}

        def run_scan():
            try:
                result = run_scan_to_snapshot(
                    scan_path,
                    use_index=use_index,
                    counters=counters,
                    skip_prefixes=skip_paths,
                    scan_id=scan_id,
                )
            except Exception as e:  # noqa: BLE001 - surfaced to the caller below
                traceback.print_exc()
                with scan_lock:
                    if scan_id in running_scans:
                        running_scans[scan_id]['status'] = 'error'
                        running_scans[scan_id]['error'] = str(e)
                        running_scans[scan_id]['finish_time'] = int(
                            datetime.now().timestamp())
                return

            with scan_lock:
                if scan_id in running_scans:
                    running_scans[scan_id]['status'] = (
                        'completed_with_errors' if counters.get('errors')
                        else 'completed')
                    running_scans[scan_id]['result_file'] = result['result_file']
                    running_scans[scan_id]['counters'] = counters
                    running_scans[scan_id]['finish_time'] = int(
                        datetime.now().timestamp())

        thread = threading.Thread(target=run_scan, daemon=True)
        running_scans[scan_id] = {
            'path': scan_path,
            'status': 'running',
            'start_time': int(datetime.now().timestamp()),
            'counters': counters,
            'error': None,
            'finish_time': None,
        }
        thread.start()

    return jsonify({'scan_id': scan_id, 'status': 'started'})


def _task_payload(scan_id, scan_info, include_errors=True):
    counters = dict(scan_info['counters'])
    errors = list(counters.get('errors', []))
    counters['error_count'] = len(errors)
    counters['errors'] = errors if include_errors else []
    return {
        'scan_id': scan_id,
        'path': scan_info['path'],
        'status': scan_info['status'],
        'start_time': scan_info['start_time'],
        'finish_time': scan_info.get('finish_time'),
        'counters': counters,
        'error': scan_info.get('error'),
        'result_file': scan_info.get('result_file'),
        'log_available': True,
    }


@scan_bp.route('/api/v1/scan/<scan_id>', methods=['GET'])
def get_scan_status(scan_id):
    """Get live status, falling back to its durable log after a restart."""
    with scan_lock:
        scan_info = running_scans.get(scan_id)
        if scan_info is not None:
            return jsonify(_task_payload(scan_id, scan_info))

    try:
        records = read_scan_log(scan_id)
    except ValueError:
        records = None
    summary = summarize_scan_log(records or [])
    if summary is None:
        return jsonify({'error': 'Scan ID not found'}), 404
    return jsonify(summary)


@scan_bp.route('/api/v1/scan-tasks', methods=['GET'])
def get_scan_tasks():
    """List running and historical scans, including logs from prior runs."""
    durable = {item['scan_id']: item for item in list_scan_logs()}
    for item in durable.values():
        errors = item['counters'].get('errors', [])
        item['counters']['error_count'] = len(errors)
        item['counters']['errors'] = []
    with scan_lock:
        for scan_id, scan_info in running_scans.items():
            durable[scan_id] = _task_payload(
                scan_id, scan_info, include_errors=False)
    scans = sorted(
        durable.values(),
        key=lambda item: item.get('start_time') or 0,
        reverse=True,
    )
    return jsonify({'scans': scans})


@scan_bp.route('/api/v1/scan/<scan_id>/log', methods=['GET'])
def get_scan_log(scan_id):
    """Return the durable lifecycle/progress/error records for a scan."""
    try:
        records = read_scan_log(scan_id)
    except ValueError:
        records = None
    if records is None:
        return jsonify({'error': 'Scan ID not found'}), 404
    return jsonify({'scan_id': scan_id, 'records': records})


@scan_bp.route('/api/v1/scans', methods=['GET'])
def get_all_scans():
    """Get a list of all scan result files"""
    snapshot_dir = get_config().snapshot_dir

    scan_files = []
    for file in list_snapshot_files(snapshot_dir):
        try:
            stat = os.stat(os.path.join(snapshot_dir, file))
        except OSError:
            continue
        scan_files.append({
            'filename': file,
            'size': stat.st_size,
            'modified': int(stat.st_mtime)
        })

    return jsonify({'scan_files': scan_files})
