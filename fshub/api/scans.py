"""Scanning API endpoints"""

import os
import platform
import threading
import traceback
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify

from ..config import get_config
from ..scanning import is_related_path, run_scan_to_snapshot
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
        if info['status'] in ('completed', 'error')
    ]
    if len(finished) <= MAX_FINISHED_SCANS:
        return
    finished.sort()
    for _, scan_id in finished[: len(finished) - MAX_FINISHED_SCANS]:
        running_scans.pop(scan_id, None)


@scan_bp.route('/api/v1/scan', methods=['POST'])
def start_scan():
    """Start a scan of a directory"""
    data = request.get_json(silent=True) or {}
    scan_path = data.get('path', '')
    skip_paths = data.get('skip_paths', []) or []
    use_index = bool(data.get('use_index', False))

    if not isinstance(skip_paths, list):
        return jsonify({'error': 'skip_paths must be a list'}), 400

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
                )
            except Exception as e:  # noqa: BLE001 - surfaced to the caller below
                traceback.print_exc()
                with scan_lock:
                    if scan_id in running_scans:
                        running_scans[scan_id]['status'] = 'error'
                        running_scans[scan_id]['error'] = str(e)
                return

            with scan_lock:
                if scan_id in running_scans:
                    running_scans[scan_id]['status'] = 'completed'
                    running_scans[scan_id]['result_file'] = result['result_file']
                    running_scans[scan_id]['counters'] = counters

        thread = threading.Thread(target=run_scan, daemon=True)
        running_scans[scan_id] = {
            'path': scan_path,
            'status': 'running',
            'start_time': int(datetime.now().timestamp()),
            'counters': counters,
            'error': None,
        }
        thread.start()

    return jsonify({'scan_id': scan_id, 'status': 'started'})


@scan_bp.route('/api/v1/scan/<scan_id>', methods=['GET'])
def get_scan_status(scan_id):
    """Get the status of a scan"""
    with scan_lock:
        scan_info = running_scans.get(scan_id)
        if scan_info is None:
            return jsonify({'error': 'Scan ID not found'}), 404

        payload = {
            'scan_id': scan_id,
            'path': scan_info['path'],
            'status': scan_info['status'],
            'start_time': scan_info['start_time'],
            'counters': dict(scan_info['counters']),
            'error': scan_info.get('error'),
            'result_file': scan_info.get('result_file'),
        }

    return jsonify(payload)


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
