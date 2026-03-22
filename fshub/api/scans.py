"""Scanning API endpoints"""

from flask import Blueprint, request, jsonify
import os
import threading
import uuid
import platform
from datetime import datetime
from ..config import Config
from ..scanning import run_scan_to_snapshot

scan_bp = Blueprint('scan_bp', __name__)

# Thread-safe dictionary to keep track of running scans
running_scans = {}
scan_lock = threading.Lock()
@scan_bp.route('/api/v1/scan', methods=['POST'])
def start_scan():
    """Start a scan of a directory"""
    data = request.get_json()
    scan_path = data.get('path', '')
    
    # Handle special case for Windows "This PC"
    if platform.system() == 'Windows' and scan_path == '/':
        # On Windows, "/" means scan all drives (This PC)
        pass  # We'll handle this in the scan thread
    elif not scan_path or not os.path.exists(scan_path):
        return jsonify({'error': 'Invalid path'}), 400
    
    # Check if a scan is already running for this path or subdirectories/parents
    with scan_lock:
        for existing_path in running_scans:
            if (existing_path.startswith(scan_path) or 
                scan_path.startswith(existing_path)):
                return jsonify({'error': f'Scan already running for related path: {existing_path}'}), 400
        
        scan_id = str(uuid.uuid4())
        counters = {}
        
        # Create a thread to run the scan
        def run_scan():
            result = run_scan_to_snapshot(
                scan_path,
                use_index=data.get('use_index', False),
                counters=counters,
            )

            # Update the scan status
            with scan_lock:
                if scan_id in running_scans:
                    running_scans[scan_id]['status'] = 'completed'
                    running_scans[scan_id]['result_file'] = result['result_file']
                    running_scans[scan_id]['counters'] = counters
            
        thread = threading.Thread(target=run_scan)
        running_scans[scan_id] = {
            'path': scan_path,
            'status': 'running',
            'thread': thread,
            'start_time': int(datetime.now().timestamp()),  # Unix timestamp for start time
            'counters': counters
        }
        thread.start()
        
        return jsonify({'scan_id': scan_id, 'status': 'started'})


@scan_bp.route('/api/v1/scan/<scan_id>', methods=['GET'])
def get_scan_status(scan_id):
    """Get the status of a scan"""
    if scan_id not in running_scans:
        return jsonify({'error': 'Scan ID not found'}), 404
    
    scan_info = running_scans[scan_id]
    return jsonify({
        'scan_id': scan_id,
        'path': scan_info['path'],
        'status': scan_info['status'],
        'start_time': scan_info['start_time'],
        'counters': scan_info['counters']
    })


@scan_bp.route('/api/v1/scans', methods=['GET'])
def get_all_scans():
    """Get a list of all scan result files"""
    config = Config()
    snapshot_dir = os.path.join(config.data_path, 'snapshots')
    
    scan_files = []
    for file in os.listdir(snapshot_dir):
        if file.startswith('snapshot_') and file.endswith('.jsonl.gz'):
            # Get file stats
            filepath = os.path.join(snapshot_dir, file)
            stat = os.stat(filepath)
            scan_files.append({
                'filename': file,
                'size': stat.st_size,
                'modified': int(stat.st_mtime)  # Unix timestamp for modified time
            })
    
    return jsonify({'scan_files': scan_files})
