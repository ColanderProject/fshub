"""Scanning API endpoints"""

import os
import platform
import threading
import traceback
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from ..config import get_config
from ..scan_logs import (
    MAX_REPORTED_ERRORS,
    ScanRunLog,
    list_scan_statuses,
    read_scan_log_page,
    read_scan_status,
    scan_duration,
)
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
        run_log = ScanRunLog(scan_id)
        run_log.started(scan_path, use_index=use_index, skip_paths=skip_paths)

        def run_scan():
            try:
                result = run_scan_to_snapshot(
                    scan_path,
                    use_index=use_index,
                    counters=counters,
                    skip_prefixes=skip_paths,
                    run_log=run_log,
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
                        'completed_with_errors' if counters.get('error_count', 0)
                        else 'completed')
                    running_scans[scan_id]['result_file'] = result['result_file']
                    running_scans[scan_id]['counters'] = counters
                    running_scans[scan_id]['finish_time'] = result['finish_time']

        thread = threading.Thread(target=run_scan, daemon=True)
        running_scans[scan_id] = {
            'path': scan_path,
            'status': 'running',
            'start_time': run_log.start_time,
            'counters': counters,
            'error': None,
            'finish_time': None,
            'run_log': run_log,
        }
        thread.start()

    return jsonify({'scan_id': scan_id, 'status': 'started'})


def _task_payload(scan_id, scan_info):
    source = scan_info['counters']
    errors = list(source.get('errors', []))[:MAX_REPORTED_ERRORS]
    counters = {
        'current_path': source.get('current_path', scan_info['path']),
        'scanned_count': source.get('scanned_count', 0),
        'scanned_size': source.get('scanned_size', 0),
        'error_count': source.get('error_count', len(errors)),
        'errors': errors,
    }
    start_time = scan_info['start_time']
    finish_time = scan_info.get('finish_time')
    status = scan_info['status']
    return {
        'scan_id': scan_id,
        'path': scan_info['path'],
        'status': status,
        'start_time': start_time,
        'finish_time': finish_time,
        'duration': scan_duration(start_time, finish_time, status),
        'counters': counters,
        'error': scan_info.get('error'),
        'result_file': scan_info.get('result_file'),
        'log_available': scan_info['run_log'].available,
    }


@scan_bp.route('/api/v1/scan/<scan_id>', methods=['GET'])
def get_scan_status(scan_id):
    """Get live status, falling back to its durable log after a restart."""
    with scan_lock:
        scan_info = running_scans.get(scan_id)
        if scan_info is not None:
            return jsonify(_task_payload(scan_id, scan_info))

    try:
        status = read_scan_status(scan_id)
    except ValueError:
        status = None
    if status is None:
        return jsonify({'error': 'Scan ID not found'}), 404
    return jsonify(status)


@scan_bp.route('/api/v1/scan-tasks', methods=['GET'])
def get_scan_tasks():
    """List live tasks plus the 50 newest compact status sidecars."""
    durable = {item['scan_id']: item for item in list_scan_statuses()}
    with scan_lock:
        for scan_id, scan_info in running_scans.items():
            durable[scan_id] = _task_payload(scan_id, scan_info)
    scans = sorted(
        durable.values(),
        key=lambda item: item.get('start_time') or 0,
        reverse=True,
    )
    return jsonify({'scans': scans})


@scan_bp.route('/api/v1/scan/<scan_id>/log', methods=['GET'])
def get_scan_log(scan_id):
    """Return one bounded page of durable scan-log records."""
    try:
        cursor = int(request.args.get('cursor', 0))
        limit = int(request.args.get('limit', 200))
    except (TypeError, ValueError):
        return jsonify({'error': 'cursor and limit must be integers'}), 400
    if cursor < 0 or not 1 <= limit <= 500:
        return jsonify({'error': 'cursor must be non-negative and limit must be 1-500'}), 400

    try:
        page = read_scan_log_page(scan_id, cursor=cursor, limit=limit)
    except ValueError:
        page = None
    if page is None:
        return jsonify({'error': 'Scan ID not found'}), 404
    records, next_cursor, has_more = page
    return jsonify({
        'scan_id': scan_id,
        'records': records,
        'next_cursor': next_cursor,
        'has_more': has_more,
    })


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
