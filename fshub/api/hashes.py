"""Hash management API endpoints for calculating file hashes."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import os
import threading
import time
import uuid

from flask import Blueprint, jsonify

from . import json_body, validate_group_filters
from .explorer import get_filtered_files, loaded_snapshots, snapshots_lock

hash_bp = Blueprint('hash_bp', __name__)

ALLOWED_ALGORITHMS = {'md5', 'sha1', 'sha256', 'sha512', 'blake2b'}
MAX_WORKERS = 4
MAX_FINISHED_TASKS = 50
CHUNK_SIZE = 1024 * 1024
FINISHED = ('completed', 'error')

# One pool for the whole process. A pool per request would let a handful of
# concurrent callers start an unbounded number of readers and saturate disks.
_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='fshub-hash')

# Hashing a large snapshot can take hours, so requests create background tasks
# rather than occupying a Flask worker until all files have been read.
hash_tasks = {}
hash_lock = threading.Lock()


def calculate_file_hash(file_path, algorithm='sha256'):
    """Calculate a hash, returning ``(hex_digest, error_message)``."""
    try:
        hash_func = hashlib.new(algorithm)
    except ValueError:
        return None, f'Unsupported algorithm: {algorithm}'

    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b''):
                hash_func.update(chunk)
    except OSError as e:
        return None, str(e)

    return hash_func.hexdigest(), None


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _hash_files(file_paths, algorithm):
    """Hash paths with at most ``MAX_WORKERS`` futures queued per task."""
    results = []
    errors = []
    paths = iter(enumerate(file_paths))
    pending = {}

    def work(path):
        return path, calculate_file_hash(path, algorithm)

    def submit_one():
        try:
            index, path = next(paths)
        except StopIteration:
            return False
        pending[_pool.submit(work, path)] = index
        return True

    for _ in range(MAX_WORKERS):
        if not submit_one():
            break

    while pending:
        done, _not_done = wait(tuple(pending), return_when=FIRST_COMPLETED)
        for future in done:
            index = pending.pop(future)
            path, (file_hash, error) = future.result()
            if error:
                errors.append((index, {'file_path': path, 'error': error}))
            else:
                results.append((index, {
                    'file_path': path,
                    'hash': file_hash,
                    'algorithm': algorithm,
                    'size': _safe_size(path),
                }))
            submit_one()

    # Completion order depends on file size; retain snapshot listing order in
    # the API response without retaining millions of queued Future objects.
    results.sort(key=lambda item: item[0])
    errors.sort(key=lambda item: item[0])
    return [item for _index, item in results], [item for _index, item in errors]


def _validate_request(data):
    """Validate common request fields, returning an error tuple or None."""
    snapshot_filename = data.get('snapshot_filename', '')
    if not isinstance(snapshot_filename, str) or not snapshot_filename:
        return 'Snapshot filename is required', 400

    algorithm = data.get('algorithm', 'sha256')
    if not isinstance(algorithm, str) or algorithm not in ALLOWED_ALGORITHMS:
        return f'Unsupported algorithm: {algorithm}', 400

    filter_error = validate_group_filters(
        data.get('filter_in', []), data.get('filter_out', []),
    )
    if filter_error:
        return filter_error, 400

    with snapshots_lock:
        if snapshot_filename not in loaded_snapshots:
            return f'Snapshot not loaded: {snapshot_filename}', 400

    return None


def _collect_files(data):
    """Resolve a validated request into local paths to hash."""
    files = get_filtered_files(
        data['snapshot_filename'],
        data.get('filter_in', []),
        data.get('filter_out', []),
    )
    return [item['full_path'] for item in files]


def _prune_finished_tasks():
    finished = [
        (task['start_time'], task_id)
        for task_id, task in hash_tasks.items()
        if task['status'] in FINISHED
    ]
    if len(finished) <= MAX_FINISHED_TASKS:
        return
    finished.sort()
    for _started, task_id in finished[:len(finished) - MAX_FINISHED_TASKS]:
        hash_tasks.pop(task_id, None)


def _create_task(kind, algorithm):
    task_id = str(uuid.uuid4())
    with hash_lock:
        _prune_finished_tasks()
        hash_tasks[task_id] = {
            'kind': kind,
            'algorithm': algorithm,
            'status': 'started',
            'start_time': time.time(),
            'error': None,
            'result': None,
        }
    return task_id


def _update_task(task_id, **fields):
    with hash_lock:
        task = hash_tasks.get(task_id)
        if task is not None:
            task.update(fields)


def _calculate_result(file_paths, algorithm):
    if not file_paths:
        raise ValueError('No files to process with the given filters')

    results, errors = _hash_files(file_paths, algorithm)
    return {
        'success': True,
        'algorithm': algorithm,
        'files_processed': len(results),
        'files_failed': len(errors),
        'results': results,
        'errors': errors,
    }


def _duplicates_result(file_paths, algorithm):
    by_size = {}
    errors = []
    for path in file_paths:
        size = _safe_size(path)
        if size is None:
            errors.append({'file_path': path, 'error': 'Could not read file size'})
            continue
        by_size.setdefault(size, []).append(path)

    candidates = [path for paths in by_size.values() if len(paths) > 1 for path in paths]
    results, hash_errors = _hash_files(candidates, algorithm) if candidates else ([], [])
    errors.extend(hash_errors)

    by_hash = {}
    for item in results:
        by_hash.setdefault(item['hash'], []).append(item)

    duplicates = [
        {
            'hash': file_hash,
            'size': items[0]['size'],
            'count': len(items),
            'files': [item['file_path'] for item in items],
        }
        for file_hash, items in by_hash.items()
        if len(items) > 1
    ]
    duplicates.sort(key=lambda item: (item['size'] or 0) * item['count'], reverse=True)

    return {
        'duplicates': duplicates,
        'algorithm': algorithm,
        'files_compared': len(candidates),
        'files_skipped': len(errors),
        'errors': errors,
    }


def _run_task(task_id, data, result_builder):
    _update_task(task_id, status='running')
    try:
        file_paths = _collect_files(data)
        result = result_builder(file_paths, data.get('algorithm', 'sha256'))
    except Exception as e:  # noqa: BLE001 - the error is exposed via task status
        _update_task(task_id, status='error', error=str(e))
        return
    _update_task(task_id, status='completed', result=result)


def _start_task(data, kind, result_builder):
    error = _validate_request(data)
    if error:
        message, status = error
        return jsonify({'error': message}), status

    # Copy mutable lists before handing the request to another thread.
    task_data = dict(data)
    task_data['filter_in'] = list(data.get('filter_in', []))
    task_data['filter_out'] = list(data.get('filter_out', []))
    task_id = _create_task(kind, data.get('algorithm', 'sha256'))
    threading.Thread(
        target=_run_task,
        args=(task_id, task_data, result_builder),
        daemon=True,
    ).start()

    return jsonify({
        'success': True,
        'task_id': task_id,
        'status': 'started',
    }), 202


@hash_bp.route('/api/v1/hash/calculate', methods=['POST'])
def start_hash_calculation():
    """Start hashing the files selected by the group filters."""
    return _start_task(json_body(), 'calculate', _calculate_result)


@hash_bp.route('/api/v1/hash/duplicates', methods=['POST'])
def find_duplicates():
    """Start duplicate detection over the filtered file set."""
    return _start_task(json_body(), 'duplicates', _duplicates_result)


@hash_bp.route('/api/v1/hash/status/<task_id>', methods=['GET'])
def get_hash_status(task_id):
    """Return progress/final output for a hash task."""
    with hash_lock:
        task = hash_tasks.get(task_id)
        if task is None:
            return jsonify({'error': 'Task not found'}), 404
        payload = dict(task)
    return jsonify(payload)
