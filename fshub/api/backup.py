"""Backup API endpoints"""

import json
import os
import shutil
import threading
import time
import traceback
import uuid
import zipfile

from flask import Blueprint, request, jsonify

from ..config import get_config
from ..utils import UnsafePathError, ensure_within, sanitize_name, snapshot_relative_path
from .explorer import get_filtered_files, get_snapshot_os, loaded_snapshots

backup_bp = Blueprint('backup_bp', __name__)

# Backup task states, guarded by backup_lock.
backup_tasks = {}
backup_lock = threading.Lock()

MAX_FINISHED_TASKS = 50
DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024
COPY_BUFFER = 1024 * 1024


class BackupLog:
    """Append-only JSONL log kept open for the duration of a backup."""

    def __init__(self, path, meta):
        self.path = path
        self._file = open(path, 'w', encoding='utf-8')
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
        if info['status'] in ('completed', 'error', 'cancelled')
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
            'current_file': None,
            'start_time': time.time(),
            'error': None,
        }
    return task_id


def _open_log(backup_target_name, backup_name, file_count, meta):
    """Create the backup log inside the configured data directory.

    Returns (log, run_id); run_id also names the archives of a zip run so
    successive backups into one directory do not overwrite each other.
    """
    log_dir = get_config().backup_log_dir
    os.makedirs(log_dir, exist_ok=True)

    timestamp = int(time.time())
    target = sanitize_name(backup_target_name, what='backup_target_name')
    name = sanitize_name(backup_name, what='backup_name')
    run_id = f'{name}_{timestamp}'
    log_path = os.path.join(log_dir, f'{target}_{name}_{file_count}_{timestamp}.jl')

    meta = dict(meta, timestamp=timestamp, run_id=run_id)
    return BackupLog(log_path, meta), run_id


def _prepare_backup(data, backup_type):
    """Shared validation for both backup endpoints.

    Returns (context_dict, None) or (None, (payload, status)).
    """
    snapshot_filename = data.get('snapshot_filename', '')
    target_path = data.get('target_path', '')

    if not snapshot_filename or not target_path:
        return None, ({'error': 'Snapshot filename and target path are required'}, 400)

    if snapshot_filename not in loaded_snapshots:
        return None, ({'error': f'Snapshot not loaded: {snapshot_filename}'}, 400)

    if not os.path.isabs(target_path):
        return None, ({'error': 'target_path must be an absolute path'}, 400)

    filter_in = data.get('filter_in', [])
    filter_out = data.get('filter_out', [])
    if not isinstance(filter_in, list) or not isinstance(filter_out, list):
        return None, ({'error': 'Filters must be lists of group names'}, 400)

    files_to_backup = get_filtered_files(snapshot_filename, filter_in, filter_out)
    if not files_to_backup:
        return None, ({'error': 'No files to backup with the given filters'}, 400)

    return {
        'snapshot_filename': snapshot_filename,
        'snapshot_os': get_snapshot_os(snapshot_filename),
        'target_path': os.path.realpath(target_path),
        'files': files_to_backup,
        'meta': {
            'backup_target_name': data.get('backup_target_name', 'backup_target'),
            'backup_name': data.get('backup_name', 'backup'),
            'filter_in': filter_in,
            'filter_out': filter_out,
            'snapshot': snapshot_filename,
            'backup_type': backup_type,
            'target_path': target_path,
        },
    }, None


@backup_bp.route('/api/v1/backup/zip', methods=['POST'])
def create_zip_backup():
    """Create a zip backup with filtered files"""
    data = request.get_json(silent=True) or {}
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
    data = request.get_json(silent=True) or {}
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
        if task['status'] in ('completed', 'error', 'cancelled'):
            return jsonify({'success': False, 'message': f"Task already {task['status']}"})
        task['status'] = 'stopped'

    return jsonify({'success': True, 'message': 'Backup task stopped'})


def perform_zip_backup(task_id, files_to_backup, target_path, compress_level,
                       max_file_size, log, snapshot_os, run_id):
    """Perform the actual zip backup in a separate thread"""
    try:
        os.makedirs(target_path, exist_ok=True)
        _update_task(task_id, status='running')

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

                    file_info = files_to_backup[files_processed]
                    file_size = file_info.get('size', 0) or 0
                    source_path = file_info['full_path']

                    # Start a new archive rather than blowing past the limit.
                    if chunk_size > 0 and chunk_size + file_size > max_file_size:
                        break

                    # Drive letters / leading separators are stripped so the
                    # entry can never point outside the archive root.
                    arcname = snapshot_relative_path(source_path, snapshot_os)

                    try:
                        with open(source_path, 'rb') as src:
                            with zipf.open(arcname, 'w') as dst:
                                shutil.copyfileobj(src, dst, COPY_BUFFER)
                    except OSError as e:
                        log.record(source_path, zip_filename, file_size, 'failed', str(e))
                        files_processed += 1
                        continue

                    log.record(source_path, zip_filename, file_size, 'backup', 'success')

                    chunk_size += file_size
                    files_processed += 1

                    _update_task(
                        task_id,
                        completed_files=files_processed,
                        progress=int(files_processed / total * 100),
                        current_file=source_path,
                    )

            zip_index += 1

        _update_task(task_id, status='completed', progress=100, completed_files=total)

    except Exception as e:  # noqa: BLE001 - reported through the task state
        traceback.print_exc()
        _update_task(task_id, status='error', error=str(e))
    finally:
        log.close()


def perform_folder_backup(task_id, files_to_backup, target_path, log, snapshot_os):
    """Perform the actual folder backup in a separate thread"""
    try:
        os.makedirs(target_path, exist_ok=True)
        _update_task(task_id, status='running')

        total = len(files_to_backup)

        for i, file_info in enumerate(files_to_backup):
            if _task_status(task_id) == 'stopped':
                _update_task(task_id, status='cancelled')
                return

            source_path = file_info['full_path']
            size = file_info.get('size', 0) or 0

            _update_task(
                task_id,
                completed_files=i + 1,
                progress=int((i + 1) / total * 100),
                current_file=source_path,
            )

            relative_path = snapshot_relative_path(source_path, snapshot_os)
            dest_full_path = os.path.join(target_path, *relative_path.split('/'))

            try:
                os.makedirs(os.path.dirname(dest_full_path), exist_ok=True)
                # Validate the *complete* destination after the parents exist:
                # a pre-existing symlink there would otherwise let copy2 write
                # straight through it to a file outside the target.
                ensure_within(target_path, dest_full_path, what='destination')
                if os.path.islink(dest_full_path):
                    raise UnsafePathError(f'Destination is a symlink: {dest_full_path}')
                shutil.copy2(source_path, dest_full_path)
            except (OSError, UnsafePathError) as e:
                log.record(source_path, dest_full_path, size, 'failed', str(e))
                continue

            log.record(source_path, dest_full_path, size, 'backup', 'success')

        _update_task(task_id, status='completed', progress=100, completed_files=total)

    except Exception as e:  # noqa: BLE001 - reported through the task state
        traceback.print_exc()
        _update_task(task_id, status='error', error=str(e))
    finally:
        log.close()
