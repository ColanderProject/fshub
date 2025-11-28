"""Scanning API endpoints"""

from flask import Blueprint, request, jsonify
import os
import json
import gzip
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import psutil
from ..config import Config
from ..utils import get_system_info

scan_bp = Blueprint('scan_bp', __name__)

# Thread-safe dictionary to keep track of running scans
running_scans = {}
scan_lock = threading.Lock()


def scan(path, counters, result_callback=None):
    """Scan a directory and return structured data about files and folders"""
    result = []
    
    # Initialize counters
    counters['scanned_count'] = 0
    counters['scanned_size'] = 0
    counters['errors'] = []
    
    for root, dirs, files in os.walk(path):
        try:
            path_obj = {
                'p': root,  # current path
                'f': [],    # file list
                'd': [],    # dirs list
                't': [],    # file timestamps [create, modify, access]
                'T': [],    # dir timestamps [create, modify, access] 
                's': []     # file sizes
            }
            
            # Process files in the current directory
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    stat = os.stat(file_path)
                    path_obj['f'].append(file)
                    path_obj['s'].append(stat.st_size)
                    path_obj['t'].append([
                        int(stat.st_ctime),  # Unix timestamp for created
                        int(stat.st_mtime),  # Unix timestamp for modified
                        int(stat.st_atime)   # Unix timestamp for accessed
                    ])
                    
                    counters['scanned_count'] += 1
                    counters['scanned_size'] += stat.st_size
                    
                    if result_callback:
                        result_callback(counters)
                        
                except OSError as e:
                    counters['errors'].append(f"Error accessing file {file_path}: {str(e)}")
            
            # Process subdirectories
            for directory in dirs:
                dir_path = os.path.join(root, directory)
                try:
                    stat = os.stat(dir_path)
                    path_obj['d'].append(directory)
                    path_obj['T'].append([
                        int(stat.st_ctime),  # Unix timestamp for created
                        int(stat.st_mtime),  # Unix timestamp for modified
                        int(stat.st_atime)   # Unix timestamp for accessed
                    ])
                except OSError as e:
                    counters['errors'].append(f"Error accessing directory {dir_path}: {str(e)}")
            
            result.append(path_obj)
        except OSError as e:
            counters['errors'].append(f"Error accessing directory {root}: {str(e)}")
    
    return result


@scan_bp.route('/api/v1/scan', methods=['POST'])
def start_scan():
    """Start a scan of a directory"""
    data = request.get_json()
    scan_path = data.get('path', '')
    
    if not scan_path or not os.path.exists(scan_path):
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
            start_time = datetime.now()
            scan_result = scan(scan_path, counters, lambda c: time.sleep(0.01))  # Update callback
            finish_time = datetime.now()
            
            # Add extra info to the first element
            if scan_result:
                system_info = get_system_info()
                scan_result[0]['device_name'] = system_info['device_name']
                scan_result[0]['device_id'] = system_info['thumbprint']
                scan_result[0]['cpu_model'] = system_info['cpu_model']
                scan_result[0]['cpu_name'] = system_info['cpu_model']  # Same as model for now
                scan_result[0]['memory_size'] = system_info['memory_size']
                scan_result[0]['host_name'] = system_info['host_name']
                scan_result[0]['ip_addr'] = system_info['ip_addr']
                scan_result[0]['mac_addr'] = system_info['mac_addr']
                scan_result[0]['start_scan_time'] = int(start_time.timestamp())  # Unix timestamp for start time
                scan_result[0]['finish_scan_time'] = int(finish_time.timestamp())  # Unix timestamp for finish time
            
            # Save the result to a compressed file
            timestamp = int(time.time())
            config = Config()
            snapshot_dir = os.path.join(config.data_path, 'snapshots')
            os.makedirs(snapshot_dir, exist_ok=True)

            # Check if user wants to use index files
            use_index = data.get('use_index', False)

            if use_index:
                # Create index and binary files
                filename_base = f"snapshot_{timestamp}_{len(scan_result)}"
                index_filename = f"{filename_base}_index.jsonl.gz"
                bin_filename = f"{filename_base}.bin.gz"

                index_filepath = os.path.join(snapshot_dir, index_filename)
                bin_filepath = os.path.join(snapshot_dir, bin_filename)

                # Create and save the index file (with path and compressed length)
                index_data = []
                with gzip.open(bin_filepath, 'wt', encoding='utf-8') as bin_file:
                    for item in scan_result:
                        # Store compressed fields (excluding 'p' which is in the index)
                        compressed_item = {k: v for k, v in item.items() if k != 'p'}

                        # Get the original path
                        original_path = item['p']

                        # Write the compressed item to bin file
                        compressed_str = json.dumps(compressed_item)
                        bin_file.write(compressed_str + '\n')

                        # Record the position and length for indexing
                        index_data.append({
                            'p': original_path,
                            'compressed_length': len(compressed_str)
                        })

                # Save the index file
                with gzip.open(index_filepath, 'wt', encoding='utf-8') as index_file:
                    for idx_item in index_data:
                        index_file.write(json.dumps(idx_item) + '\n')

                result_filename = index_filename  # Use index file as primary
            else:
                # Standard single file approach
                filename = f"snapshot_{timestamp}_{len(scan_result)}.jsonl.gz"
                filepath = os.path.join(snapshot_dir, filename)

                with gzip.open(filepath, 'wt', encoding='utf-8') as f:
                    for item in scan_result:
                        f.write(json.dumps(item) + '\n')

                result_filename = filename

            # Update the scan status
            with scan_lock:
                if scan_id in running_scans:
                    running_scans[scan_id]['status'] = 'completed'
                    running_scans[scan_id]['result_file'] = result_filename
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