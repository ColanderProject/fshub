"""Hash management API endpoints for calculating file hashes"""

from flask import Blueprint, request, jsonify
import os
import hashlib
import threading
import queue
from datetime import datetime
from ..config import Config

hash_bp = Blueprint('hash_bp', __name__)

# Thread pool for hash calculation
hash_queue = queue.Queue()
hash_workers = []
hash_active = True


def hash_worker():
    """Worker thread for calculating file hashes"""
    while hash_active:
        try:
            task = hash_queue.get(timeout=1)
            if task is None:
                break
                
            file_path, result_callback = task
            file_hash = calculate_file_hash(file_path)
            
            # Call the result callback with the result
            if result_callback:
                result_callback(file_path, file_hash)
                
            hash_queue.task_done()
        except queue.Empty:
            continue


# Start worker threads
for i in range(4):  # 4 worker threads
    t = threading.Thread(target=hash_worker)
    t.daemon = True
    t.start()
    hash_workers.append(t)


def calculate_file_hash(file_path, algorithm='sha256'):
    """Calculate the hash of a file"""
    hash_func = hashlib.new(algorithm)
    try:
        with open(file_path, 'rb') as f:
            # Read the file in chunks to handle large files efficiently
            for chunk in iter(lambda: f.read(8192), b""):
                hash_func.update(chunk)
        return hash_func.hexdigest()
    except Exception as e:
        return None  # Return None if file can't be read


@hash_bp.route('/api/v1/hash/calculate', methods=['POST'])
def start_hash_calculation():
    """Start calculating hashes for files based on filters"""
    data = request.get_json()
    snapshot_filename = data.get('snapshot_filename', '')
    filter_in = data.get('filter_in', [])
    filter_out = data.get('filter_out', [])
    algorithm = data.get('algorithm', 'sha256')
    
    if not snapshot_filename:
        return jsonify({'error': 'Snapshot filename is required'}), 400
    
    # Get filtered files from the snapshot
    from .search import loaded_snapshots, load_snapshot
    
    if snapshot_filename not in loaded_snapshots:
        if not load_snapshot(snapshot_filename):
            return jsonify({'error': f'Cannot load snapshot: {snapshot_filename}'}), 400
    
    snapshot_data = loaded_snapshots[snapshot_filename]['data']
    groups_dict = loaded_snapshots[snapshot_filename]['groups']
    
    # Collect files to hash based on filters
    files_to_process = []
    
    for path_obj in snapshot_data:
        current_path = path_obj['p']
        
        # Process files
        for i, filename in enumerate(path_obj.get('f', [])):
            file_path = os.path.join(current_path, filename)
            full_path = f"f:{file_path}"
            
            # Check if file should be filtered out
            should_filter_out = False
            for group_name in filter_out:
                if group_name in groups_dict and full_path in groups_dict[group_name]:
                    should_filter_out = True
                    break
            
            if should_filter_out:
                continue
            
            # If filter_in is specified, only include files in those groups
            if filter_in:
                should_include = False
                for group_name in filter_in:
                    if group_name in groups_dict and full_path in groups_dict[group_name]:
                        should_include = True
                        break
                if not should_include:
                    continue
            
            # Add file to process list
            files_to_process.append(file_path)
    
    if not files_to_process:
        return jsonify({'error': 'No files to process with the given filters'}), 400
    
    # Add files to hash queue
    results = []
    for file_path in files_to_process:
        # In a real implementation, we would queue the actual file path to be hashed
        # For this implementation, I'll calculate the hash directly
        file_hash = calculate_file_hash(file_path, algorithm)
        if file_hash:
            results.append({
                'file_path': file_path,
                'hash': file_hash,
                'algorithm': algorithm
            })
    
    return jsonify({
        'success': True,
        'files_processed': len(results),
        'results': results
    })


@hash_bp.route('/api/v1/hash/duplicates', methods=['GET'])
def find_duplicates():
    """Find duplicate files by comparing hashes"""
    snapshot_filename = request.args.get('snapshot', '')
    
    if not snapshot_filename:
        return jsonify({'error': 'Snapshot filename is required'}), 400
    
    # This would involve calculating or retrieving hashes for all files in the snapshot
    # and then comparing them to identify duplicates
    # For now, this is a placeholder implementation
    return jsonify({
        'duplicates': []  # In a real implementation, this would contain duplicate file groups
    })