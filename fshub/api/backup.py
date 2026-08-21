"""Backup API endpoints"""

import json
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile

from flask import Blueprint, jsonify

from ..config import get_config
from ..utils import (
    UnsafePathError,
    encode_name_component,
    ensure_within,
    snapshot_relative_path,
)
from . import json_body, validate_group_filters
from .explorer import get_filtered_files, get_snapshot_os, loaded_snapshots

backup_bp = Blueprint('backup_bp', __name__)

# Backup task states, guarded by backup_lock.
backup_tasks = {}
backup_lock = threading.Lock()

FINISHED = ('completed', 'completed_with_errors', 'error', 'cancelled')
MAX_FINISHED_TASKS = 50
MAX_REPORTED_ERRORS = 20
DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024
COPY_BUFFER = 1024 * 1024
STAGE_MEMORY_LIMIT = 8 * COPY_BUFFER


class BackupLog:
    """Append-only JSONL log kept open for the duration of a backup."""

    def __init__(self, path, meta):
        self.path = path
        # 'x': a run must never truncate another run's log.
        self._file = open(path, 'x', encoding='utf-8')
        self.write(meta)

    def write(self, entry):
        self._file.write(json.dumps(entry) + '\n')
        self._file.flush()

    def record(self, src, dest, size, action, result):
        self.write({
            'timestamp': int(time.time()),
            'src_path': src,
            'dest_path': dest,
            'filesize': size,
            'action': action,
            'result': result,
        })

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _prune_finished_tasks():
    finished = [
        (info['start_time'], task_id)
        for task_id, info in backup_tasks.items()
        if info['status'] in FINISHED
    ]
    if len(finished) <= MAX_FINISHED_TASKS:
        return
    finished.sort()
    for _, task_id in finished[: len(finished) - MAX_FINISHED_TASKS]:
        backup_tasks.pop(task_id, None)


def _update_task(task_id, **fields):
    with backup_lock:
        task = backup_tasks.get(task_id)
        if task is not None:
            task.update(fields)


def _task_status(task_id):
    with backup_lock:
        task = backup_tasks.get(task_id)
        return task['status'] if task else None


def _create_task(total_files):
    task_id = str(uuid.uuid4())
    with backup_lock:
        _prune_finished_tasks()
        backup_tasks[task_id] = {
            'status': 'started',
            'progress': 0,
            'total_files': total_files,
            'completed_files': 0,
            'failed_files': 0,
            'current_file': None,
            'start_time': time.time(),
            'error': None,
            'errors': [],
        }
    return task_id


def _begin_task(task_id):
    """Move a task to 'running', unless it was stopped before we got here.

    Compare-and-set: overwriting 'stopped' with 'running' would silently
    discard a stop request that arrived while the thread was starting.
    """
    with backup_lock:
        task = backup_tasks.get(task_id)
        if task is None or task['status'] != 'started':
            return False
        task['status'] = 'running'
        return True


def _record(task_id, log, src, dest, size, error=None):
    """Log one file and update the task counters (error=None means success)."""
    log.record(src, dest, size, 'failed' if error else 'backup',
               str(error) if error else 'success')

    with backup_lock:
        task = backup_tasks.get(task_id)
        if task is None:
            return
        if error:
            task['failed_files'] += 1
            if len(task['errors']) < MAX_REPORTED_ERRORS:
                task['errors'].append({'path': src, 'error': str(error)})
        else:
            task['completed_files'] += 1
        done = task['completed_files'] + task['failed_files']
        task['progress'] = int(done / task['total_files'] * 100) if task['total_files'] else 100
        task['current_file'] = src


def _finish_task(task_id):
    """Close a task. A run that lost files must never report plain success."""
    with backup_lock:
        task = backup_tasks.get(task_id)
        if task is None:
            return
        # stop_backup_task() can win the lock after the last per-file check.
        # Never overwrite its accepted stop request with a completed state.
        if task['status'] == 'stopped':
            task['status'] = 'cancelled'
            task['current_file'] = None
            return

        failed = task['failed_files']
        task['status'] = 'completed_with_errors' if failed else 'completed'
        task['progress'] = 100
        task['current_file'] = None
        if failed:
            task['error'] = f"{failed} of {task['total_files']} files failed"


def _open_log(backup_target_name, backup_name, file_count, meta):
    """Create the backup log inside the configured data directory.

    Returns (log, run_id). The run id also names the archives of a zip run,
    so it has to be unique: a wall-clock second is not, since two runs with
    the same names and file count can start within one second and would then
    share both the log path and the archive names.
    """
    log_dir = get_config().backup_log_dir
    os.makedirs(log_dir, exist_ok=True)

    timestamp = int(time.time())
    # Encoded, not rejected: "My Backup" is a perfectly normal label and the
    # original text is kept verbatim in the log metadata.
    target = encode_name_component(backup_target_name, what='backup_target_name')
    name = encode_name_component(backup_name, what='backup_name')
    run_id = f'{name}_{timestamp}_{uuid.uuid4().hex[:8]}'
    log_path = os.path.join(log_dir, f'{target}_{run_id}_{file_count}.jl')

    meta = dict(meta, timestamp=timestamp, run_id=run_id)
    return BackupLog(log_path, meta), run_id


def _prepare_backup(data, backup_type):
    """Shared validation for both backup endpoints.

    Returns (context_dict, None) or (None, (payload, status)).
    """
    snapshot_id = data.get('snapshot_id', '')
    target_path = data.get('target_path', '')

    if not isinstance(snapshot_id, str) or not snapshot_id:
        return None, ({'error': 'snapshot_id must be a non-empty string'}, 400)
    if not isinstance(target_path, str) or not target_path:
        return None, ({'error': 'target_path must be a non-empty string'}, 400)

    dry_run = data.get('dry_run', False)
    if not isinstance(dry_run, bool):
        return None, ({'error': 'dry_run must be a boolean'}, 400)

    if snapshot_id not in loaded_snapshots:
        return None, ({'error': f'Snapshot not loaded: {snapshot_id}'}, 400)

    if not os.path.isabs(target_path):
        return None, ({'error': 'target_path must be an absolute path'}, 400)

    # Both backup types write *into* the target, so an existing file there is
    # a user error worth reporting before a task is started.
    if os.path.exists(target_path) and not os.path.isdir(target_path):
        return None, ({'error': 'target_path must be a directory'}, 400)

    filter_in = data.get('filter_in', [])
    filter_out = data.get('filter_out', [])
    filter_error = validate_group_filters(filter_in, filter_out)
    if filter_error:
        return None, ({'error': filter_error}, 400)

    snapshot_os = get_snapshot_os(snapshot_id)
    files_to_backup = get_filtered_files(snapshot_id, filter_in, filter_out)
    if not files_to_backup:
        return None, ({'error': 'No files to backup with the given filters'}, 400)

    return {
        'snapshot_id': snapshot_id,
        'snapshot_os': snapshot_os,
        'target_path': os.path.realpath(target_path),
        'files': files_to_backup,
        'meta': {
            'backup_target_name': data.get('backup_target_name', 'backup_target'),
            'backup_name': data.get('backup_name', 'backup'),
            'filter_in': filter_in,
            'filter_out': filter_out,
            'snapshot': snapshot_id,
            'backup_type': backup_type,
            'target_path': target_path,
        },
    }, None


@backup_bp.route('/api/v1/backup/zip', methods=['POST'])
def create_zip_backup():
    """Create a zip backup with filtered files"""
    data = json_body()
    ctx, error = _prepare_backup(data, 'zip')
    if error:
        payload, status = error
        return jsonify(payload), status

    if data.get('dry_run', False):
        return jsonify({
            'success': True,
            'files_found': len(ctx['files']),
            'files_to_backup': ctx['files'],
            'dry_run': True
        })

    try:
        compress_level = int(data.get('compress_level', 6))
        max_file_size = int(data.get('max_file_size', DEFAULT_MAX_FILE_SIZE))
    except (TypeError, ValueError):
        return jsonify({'error': 'compress_level and max_file_size must be integers'}), 400

    if not 0 <= compress_level <= 9:
        return jsonify({'error': 'compress_level must be between 0 and 9'}), 400
    if max_file_size <= 0:
        return jsonify({'error': 'max_file_size must be positive'}), 400

    try:
        log, run_id = _open_log(
            ctx['meta']['backup_target_name'], ctx['meta']['backup_name'],
            len(ctx['files']), ctx['meta'],
        )
    except (UnsafePathError, OSError) as e:
        return jsonify({'error': str(e)}), 400

    task_id = _create_task(len(ctx['files']))
    threading.Thread(
        target=perform_zip_backup,
        args=(task_id, ctx['files'], ctx['target_path'], compress_level,
              max_file_size, log, ctx['snapshot_os'], run_id),
        daemon=True,
    ).start()

    return jsonify({
        'success': True,
        'task_id': task_id,
        'message': 'Backup started',
        'files_to_backup': len(ctx['files'])
    })


@backup_bp.route('/api/v1/backup/folder', methods=['POST'])
def create_folder_backup():
    """Create a folder backup with filtered files"""
    data = json_body()
    ctx, error = _prepare_backup(data, 'folder')
    if error:
        payload, status = error
        return jsonify(payload), status

    if data.get('dry_run', False):
        return jsonify({
            'success': True,
            'files_found': len(ctx['files']),
            'files_to_backup': ctx['files'],
            'dry_run': True
        })

    try:
        log, _run_id = _open_log(
            ctx['meta']['backup_target_name'], ctx['meta']['backup_name'],
            len(ctx['files']), ctx['meta'],
        )
    except (UnsafePathError, OSError) as e:
        return jsonify({'error': str(e)}), 400

    task_id = _create_task(len(ctx['files']))
    threading.Thread(
        target=perform_folder_backup,
        args=(task_id, ctx['files'], ctx['target_path'], log, ctx['snapshot_os']),
        daemon=True,
    ).start()

    return jsonify({
        'success': True,
        'task_id': task_id,
        'message': 'Backup started',
        'files_to_backup': len(ctx['files'])
    })


@backup_bp.route('/api/v1/backup/status/<task_id>', methods=['GET'])
def get_backup_status(task_id):
    """Get the status of a backup task"""
    with backup_lock:
        task = backup_tasks.get(task_id)
        if task is None:
            return jsonify({'error': 'Task not found'}), 404
        return jsonify(dict(task))


@backup_bp.route('/api/v1/backup/stop/<task_id>', methods=['POST'])
def stop_backup_task(task_id):
    """Request that a backup task stops"""
    with backup_lock:
        task = backup_tasks.get(task_id)
        if task is None:
            return jsonify({'error': 'Task not found'}), 404
        if task['status'] in FINISHED:
            return jsonify({'success': False, 'message': f"Task already {task['status']}"})
        task['status'] = 'stopped'

    return jsonify({'success': True, 'message': 'Backup task stopped'})


def perform_zip_backup(task_id, files_to_backup, target_path, compress_level,
                       max_file_size, log, snapshot_os, run_id):
    """Perform the actual zip backup in a separate thread"""
    try:
        if not _begin_task(task_id):
            _update_task(task_id, status='cancelled')
            return

        os.makedirs(target_path, exist_ok=True)

        total = len(files_to_backup)
        files_processed = 0
        zip_index = 0

        while files_processed < total:
            # Archives carry the run id, so backing up twice into the same
            # directory adds a new set instead of destroying the previous one.
            zip_filename = os.path.join(target_path, f'{run_id}_{zip_index:03d}.zip')

            # 'x' rather than 'w': never silently clobber an existing archive.
            with zipfile.ZipFile(zip_filename, 'x', compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=compress_level) as zipf:
                chunk_size = 0

                while files_processed < total:
                    if _task_status(task_id) == 'stopped':
                        _update_task(task_id, status='cancelled')
                        return

                    source_path = files_to_backup[files_processed]['full_path']

                    # The size on disk, not the snapshot's stale copy: the
                    # split limit has to match what is actually archived.
                    try:
                        file_size = os.path.getsize(source_path)
                    except OSError as e:
                        _record(task_id, log, source_path, zip_filename, 0, e)
                        files_processed += 1
                        continue

                    # Start a new archive rather than blowing past the limit.
                    if chunk_size > 0 and chunk_size + file_size > max_file_size:
                        break

                    # Drive letters / leading separators are stripped so the
                    # entry can never point outside the archive root.
                    arcname = snapshot_relative_path(source_path, snapshot_os)

                    # Read the complete source before opening a ZIP member. If
                    # the source fails halfway through, zipfile would otherwise
                    # finalize and expose that truncated member under its real
                    # name. Small files stay in memory; large ones spill to a
                    # temporary file.
                    try:
                        with tempfile.SpooledTemporaryFile(
                                max_size=STAGE_MEMORY_LIMIT, mode='w+b') as staged:
                            with open(source_path, 'rb') as src:
                                shutil.copyfileobj(src, staged, COPY_BUFFER)
                            staged.seek(0)
                            with zipf.open(arcname, 'w') as dst:
                                shutil.copyfileobj(staged, dst, COPY_BUFFER)
                    except OSError as e:
                        _record(task_id, log, source_path, zip_filename, file_size, e)
                        files_processed += 1
                        continue

                    _record(task_id, log, source_path, zip_filename, file_size)
                    chunk_size += file_size
                    files_processed += 1

            zip_index += 1

        _finish_task(task_id)

    except Exception as e:  # noqa: BLE001 - reported through the task state
        traceback.print_exc()
        _update_task(task_id, status='error', error=str(e))
    finally:
        log.close()


def perform_folder_backup(task_id, files_to_backup, target_path, log, snapshot_os):
    """Perform the actual folder backup in a separate thread"""
    try:
        if not _begin_task(task_id):
            _update_task(task_id, status='cancelled')
            return

        os.makedirs(target_path, exist_ok=True)

        for file_info in files_to_backup:
            if _task_status(task_id) == 'stopped':
                _update_task(task_id, status='cancelled')
                return

            source_path = file_info['full_path']
            size = file_info.get('size', 0) or 0

            relative_path = snapshot_relative_path(source_path, snapshot_os)
            dest_full_path = os.path.join(target_path, *relative_path.split('/'))
            dest_dir = os.path.dirname(dest_full_path)

            try:
                # Check the parent *before* creating it: a symlinked component
                # already in the target would otherwise let makedirs() build a
                # directory tree outside of it. The complete path is checked
                # again afterwards so a pre-existing symlink there cannot make
                # copy2 write straight through it either.
                ensure_within(target_path, dest_dir, what='destination')
                os.makedirs(dest_dir, exist_ok=True)
                ensure_within(target_path, dest_full_path, what='destination')
                if os.path.islink(dest_full_path):
                    raise UnsafePathError(f'Destination is a symlink: {dest_full_path}')
                shutil.copy2(source_path, dest_full_path)
            except (OSError, UnsafePathError) as e:
                _record(task_id, log, source_path, dest_full_path, size, e)
                continue

            _record(task_id, log, source_path, dest_full_path, size)

        _finish_task(task_id)

    except Exception as e:  # noqa: BLE001 - reported through the task state
        traceback.print_exc()
        _update_task(task_id, status='error', error=str(e))
    finally:
        log.close()
