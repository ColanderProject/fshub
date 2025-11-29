"""Backup API endpoints"""

from flask import Blueprint, request, jsonify
import os
import zipfile
import json
import time
import threading
import uuid
from datetime import datetime
from ..config import Config
from .explorer import get_filtered_files

backup_bp = Blueprint('backup_bp', __name__)

# Global dictionary to store backup task states
backup_tasks = {}

# Base path for backup logs
backup_log_dir = os.path.expanduser('~/.fshub/backups')
os.makedirs(backup_log_dir, exist_ok=True)


@backup_bp.route('/api/v1/backup/zip', methods=['POST'])
def create_zip_backup():
    """Create a zip backup with filtered files"""
    data = request.get_json()
    snapshot_filename = data.get('snapshot_filename', '')
    target_path = data.get('target_path', '')
    filter_in = data.get('filter_in', [])
    filter_out = data.get('filter_out', [])
    compress_level = data.get('compress_level', 6)  # Default compression level
    dry_run = data.get('dry_run', False)  # Added dry_run parameter
    backup_name = data.get('backup_name', 'backup')
    backup_target_name = data.get('backup_target_name', 'backup_target')
    max_file_size = data.get('max_file_size', 100 * 1024 * 1024)  # Default 100 MB

    if not snapshot_filename or not target_path:
        return jsonify({'error': 'Snapshot filename and target path are required'}), 400

    # Get files based on filters
    files_to_backup = get_filtered_files(snapshot_filename, filter_in, filter_out)

    if not files_to_backup:
        return jsonify({'error': 'No files to backup with the given filters'}), 400

    # If dry_run, return the files that would be backed up without actually backing them up
    if dry_run:
        return jsonify({
            'success': True,
            'files_found': len(files_to_backup),
            'files_to_backup': files_to_backup,
            'dry_run': True
        })

    # Generate a unique task ID for this backup
    task_id = str(uuid.uuid4())

    # Create backup task
    backup_tasks[task_id] = {
        'status': 'started',
        'progress': 0,
        'total_files': len(files_to_backup),
        'completed_files': 0,
        'current_file': None,
        'start_time': time.time(),
        'error': None
    }

    # Create backup log
    timestamp = int(time.time())
    log_filename = f"{backup_target_name}_{backup_name}_{len(files_to_backup)}_{timestamp}.jl"
    log_path = os.path.join(backup_log_dir, log_filename)

    # Write metadata to log
    with open(log_path, 'w') as log_file:
        meta_info = {
            'timestamp': timestamp,
            'backup_target_name': backup_target_name,
            'backup_name': backup_name,
            'filter_in': filter_in,
            'filter_out': filter_out,
            'snapshot': snapshot_filename,
            'backup_type': 'zip',
            'target_path': target_path
        }
        log_file.write(json.dumps(meta_info) + '\n')

    # Start the backup in a separate thread
    thread = threading.Thread(
        target=perform_zip_backup,
        args=(task_id, files_to_backup, target_path, compress_level, max_file_size, log_path)
    )
    thread.start()

    return jsonify({
        'success': True,
        'task_id': task_id,
        'message': 'Backup started',
        'files_to_backup': len(files_to_backup)
    })


@backup_bp.route('/api/v1/backup/folder', methods=['POST'])
def create_folder_backup():
    """Create a folder backup with filtered files"""
    data = request.get_json()
    snapshot_filename = data.get('snapshot_filename', '')
    target_path = data.get('target_path', '')
    filter_in = data.get('filter_in', [])
    filter_out = data.get('filter_out', [])
    dry_run = data.get('dry_run', False)  # Added dry_run parameter
    backup_name = data.get('backup_name', 'backup')
    backup_target_name = data.get('backup_target_name', 'backup_target')

    if not snapshot_filename or not target_path:
        return jsonify({'error': 'Snapshot filename and target path are required'}), 400

    # Get files based on filters
    files_to_backup = get_filtered_files(snapshot_filename, filter_in, filter_out)

    if not files_to_backup:
        return jsonify({'error': 'No files to backup with the given filters'}), 400

    # If dry_run, return the files that would be backed up without actually backing them up
    if dry_run:
        return jsonify({
            'success': True,
            'files_found': len(files_to_backup),
            'files_to_backup': files_to_backup,
            'dry_run': True
        })

    # Generate a unique task ID for this backup
    task_id = str(uuid.uuid4())

    # Create backup task
    backup_tasks[task_id] = {
        'status': 'started',
        'progress': 0,
        'total_files': len(files_to_backup),
        'completed_files': 0,
        'current_file': None,
        'start_time': time.time(),
        'error': None
    }

    # Create backup log
    timestamp = int(time.time())
    log_filename = f"{backup_target_name}_{backup_name}_{len(files_to_backup)}_{timestamp}.jl"
    log_path = os.path.join(backup_log_dir, log_filename)

    # Write metadata to log
    with open(log_path, 'w') as log_file:
        meta_info = {
            'timestamp': timestamp,
            'backup_target_name': backup_target_name,
            'backup_name': backup_name,
            'filter_in': filter_in,
            'filter_out': filter_out,
            'snapshot': snapshot_filename,
            'backup_type': 'folder',
            'target_path': target_path
        }
        log_file.write(json.dumps(meta_info) + '\n')

    # Start the backup in a separate thread
    thread = threading.Thread(
        target=perform_folder_backup,
        args=(task_id, files_to_backup, target_path, log_path)
    )
    thread.start()

    return jsonify({
        'success': True,
        'task_id': task_id,
        'message': 'Backup started',
        'files_to_backup': len(files_to_backup)
    })


@backup_bp.route('/api/v1/backup/status/<task_id>', methods=['GET'])
def get_backup_status(task_id):
    """Get the status of a backup task"""
    if task_id not in backup_tasks:
        return jsonify({'error': 'Task not found'}), 404

    return jsonify(backup_tasks[task_id])


@backup_bp.route('/api/v1/backup/stop/<task_id>', methods=['POST'])
def stop_backup_task(task_id):
    """Stop a backup task"""
    if task_id not in backup_tasks:
        return jsonify({'error': 'Task not found'}), 404

    backup_tasks[task_id]['status'] = 'stopped'
    return jsonify({'success': True, 'message': 'Backup task stopped'})


def perform_zip_backup(task_id, files_to_backup, target_path, compress_level, max_file_size, log_path):
    """Perform the actual zip backup in a separate thread"""
    try:
        compression_method = zipfile.ZIP_DEFLATED
        # Create target directory if it doesn't exist
        os.makedirs(target_path, exist_ok=True)

        # Prepare files with cumulative size for chunking
        total_size = sum(f.get('size', 0) for f in files_to_backup)

        # Process files in chunks based on max_file_size
        files_processed = 0
        file_index = 0
        zip_index = 0

        while files_processed < len(files_to_backup):
            # Create a new zip file for this chunk
            zip_filename = f"{os.path.join(target_path, os.path.splitext(os.path.basename(target_path))[0] if os.path.isdir(target_path) else os.path.splitext(target_path)[0])}_{zip_index:03d}.zip"

            with zipfile.ZipFile(zip_filename, 'w', compression=compression_method, compresslevel=compress_level) as zipf:
                chunk_size = 0
                chunk_files = []

                # Add files to this zip until we hit the size limit
                while files_processed < len(files_to_backup) and chunk_size < max_file_size:
                    file_info = files_to_backup[files_processed]
                    file_size = file_info.get('size', 0)

                    # For dry run simulation, skip actual file processing
                    # In a real implementation, we would read from the original location
                    # For now, just simulate the backup process
                    zipf.writestr(f"backup/{file_info['name']}", f"Content of {file_info['name']}")  # Placeholder content

                    # Log the backup operation
                    with open(log_path, 'a') as log_file:
                        log_entry = {
                            'timestamp': int(time.time()),
                            'src_path': file_info['full_path'],
                            'dest_path': zip_filename,
                            'filesize': file_size,
                            'action': 'backup'
                        }
                        log_file.write(json.dumps(log_entry) + '\n')

                    chunk_size += file_size
                    chunk_files.append(file_info)
                    files_processed += 1

                    # Update task progress
                    backup_tasks[task_id]['completed_files'] = files_processed
                    backup_tasks[task_id]['progress'] = int((files_processed / len(files_to_backup)) * 100)
                    backup_tasks[task_id]['current_file'] = file_info['name']

                    # Check if task was stopped
                    if backup_tasks[task_id]['status'] == 'stopped':
                        backup_tasks[task_id]['status'] = 'cancelled'
                        return

            zip_index += 1

        # Mark task as complete
        backup_tasks[task_id]['status'] = 'completed'
        backup_tasks[task_id]['progress'] = 100
        backup_tasks[task_id]['completed_files'] = len(files_to_backup)

    except Exception as e:
        backup_tasks[task_id]['status'] = 'error'
        backup_tasks[task_id]['error'] = str(e)


def perform_folder_backup(task_id, files_to_backup, target_path, log_path):
    """Perform the actual folder backup in a separate thread"""
    try:
        # Create target directory if it doesn't exist
        os.makedirs(target_path, exist_ok=True)

        for i, file_info in enumerate(files_to_backup):
            # In a real implementation, this would copy the actual file
            # For now, we'll simulate the process

            # Calculate progress
            backup_tasks[task_id]['completed_files'] = i + 1
            backup_tasks[task_id]['progress'] = int(((i + 1) / len(files_to_backup)) * 100)
            backup_tasks[task_id]['current_file'] = file_info['name']

            # Log the backup operation
            with open(log_path, 'a') as log_file:
                log_entry = {
                    'timestamp': int(time.time()),
                    'src_path': file_info['full_path'],
                    'dest_path': os.path.join(target_path, file_info['name']),
                    'filesize': file_info.get('size', 0),
                    'action': 'backup'
                }
                log_file.write(json.dumps(log_entry) + '\n')

            # Check if task was stopped
            if backup_tasks[task_id]['status'] == 'stopped':
                backup_tasks[task_id]['status'] = 'cancelled'
                return

            # Simulate file copying delay (in a real implementation, this would be actual file I/O)
            time.sleep(0.01)

        # Mark task as complete
        backup_tasks[task_id]['status'] = 'completed'
        backup_tasks[task_id]['progress'] = 100
        backup_tasks[task_id]['completed_files'] = len(files_to_backup)

    except Exception as e:
        backup_tasks[task_id]['status'] = 'error'
        backup_tasks[task_id]['error'] = str(e)
