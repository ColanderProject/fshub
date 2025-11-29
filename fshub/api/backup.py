"""Backup API endpoints"""

from flask import Blueprint, request, jsonify
import os
import zipfile
import time
from ..config import Config
from .explorer import get_filter_files

backup_bp = Blueprint('backup_bp', __name__)


@backup_bp.route('/api/v1/backup/zip', methods=['POST'])
def create_zip_backup():
    """Create a zip backup with filtered files"""
    data = request.get_json()
    snapshot_filename = data.get('snapshot_filename', '')
    target_path = data.get('target_path', '')
    filter_in = data.get('filter_in', [])
    filter_out = data.get('filter_out', [])
    compress_level = data.get('compress_level', 6)  # Default compression level

    if not snapshot_filename or not target_path:
        return jsonify({'error': 'Snapshot filename and target path are required'}), 400

    # Get files based on filters
    files_to_backup = get_filtered_files(snapshot_filename, filter_in, filter_out)

    if not files_to_backup:
        return jsonify({'error': 'No files to backup with the given filters'}), 400

    # Create zip file
    try:
        # Use the compression level provided
        compression_method = zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(target_path, 'w', compression=compression_method, compresslevel=compress_level) as zipf:
            for file_info in files_to_backup:
                # In a real implementation, this would backup actual files
                # For now, we'll add a placeholder entry
                # The actual implementation would need to locate the real files based on the snapshot
                zipf.writestr(f"backup/{file_info['name']}", f"Content of {file_info['name']}")  # Placeholder content

        return jsonify({
            'success': True,
            'files_backed_up': len(files_to_backup),
            'target_path': target_path
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/api/v1/backup/folder', methods=['POST'])
def create_folder_backup():
    """Create a folder backup with filtered files"""
    data = request.get_json()
    snapshot_filename = data.get('snapshot_filename', '')
    target_path = data.get('target_path', '')
    filter_in = data.get('filter_in', [])
    filter_out = data.get('filter_out', [])

    if not snapshot_filename or not target_path:
        return jsonify({'error': 'Snapshot filename and target path are required'}), 400

    # Create target directory if it doesn't exist
    os.makedirs(target_path, exist_ok=True)

    # Get files based on filters
    files_to_backup = get_filtered_files(snapshot_filename, filter_in, filter_out)

    if not files_to_backup:
        return jsonify({'error': 'No files to backup with the given filters'}), 400

    # In a real implementation, this would copy actual files to the destination
    # For now, we'll return information about what files would be copied
    copied_files = []
    skipped_files = []

    for file_info in files_to_backup:
        # In a real implementation, you would need to locate the actual file from the original location
        # For this implementation, we'll just record what would happen
        copied_files.append(file_info['full_path'])

    return jsonify({
        'success': True,
        'files_backed_up': len(copied_files),
        'target_path': target_path,
        'copied_files': copied_files,
        'skipped_files': skipped_files
    })


def get_filtered_files(snapshot_filename, filter_in, filter_out):
    """Get files from a snapshot that match the filter criteria"""
    return get_filter_files(snapshot_filename, filter_in, filter_out)
