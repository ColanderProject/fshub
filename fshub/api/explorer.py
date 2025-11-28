"""File explorer API endpoints"""

from flask import Blueprint, request, jsonify
import os
import json
import gzip
from datetime import datetime
from ..config import Config
from ..utils import format_bytes

explorer_bp = Blueprint('explorer_bp', __name__)

# Global variable for loaded snapshots (shared with search module)
loaded_snapshots = {}  # Key: filename, Value: {'data': list, 'index': dict, 'groups': dict}


@explorer_bp.route('/api/v1/load_snapshot', methods=['POST'])
def load_snapshot():
    """Load a snapshot file into memory"""
    data = request.get_json()
    snapshot_filename = data.get('filename', '')
    
    if not snapshot_filename:
        return jsonify({'error': 'Snapshot filename is required'}), 400
    
    success = load_snapshot_file(snapshot_filename)
    if not success:
        return jsonify({'error': f'Failed to load snapshot: {snapshot_filename}'}), 400
    
    # Calculate sub counts and total sizes
    calculate_sub_counts(snapshot_filename)
    
    return jsonify({
        'success': True,
        'message': f'Snapshot {snapshot_filename} loaded successfully',
        'snapshot_info': get_snapshot_info(snapshot_filename)
    })


@explorer_bp.route('/api/v1/unload_snapshot', methods=['POST'])
def unload_snapshot():
    """Unload a snapshot from memory"""
    data = request.get_json()
    snapshot_filename = data.get('filename', '')
    
    if snapshot_filename in loaded_snapshots:
        del loaded_snapshots[snapshot_filename]
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Snapshot not loaded'}), 400


@explorer_bp.route('/api/v1/getPath', methods=['GET'])
def get_path():
    """Get the content of a specific path from a snapshot"""
    snapshot_filename = request.args.get('snapshot', '')
    path = request.args.get('path', None)
    index = request.args.get('index', None, type=int)
    use_filter = request.args.get('use_filter', default=False, type=lambda x: x.lower() == 'true')
    filter_in = request.args.get('filter_in', default='[]')
    filter_out = request.args.get('filter_out', default='[]')
    
    try:
        filter_in = json.loads(filter_in) if filter_in else []
        filter_out = json.loads(filter_out) if filter_out else []
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid filter format'}), 400
    
    if not snapshot_filename:
        return jsonify({'error': 'Snapshot filename is required'}), 400
    
    # Use either path or index, not both
    if path is not None and index is not None:
        return jsonify({'error': 'Cannot specify both path and index'}), 400
    
    if snapshot_filename not in loaded_snapshots:
        # if not load_snapshot_file(snapshot_filename):
        return jsonify({'error': f'Snapshot not found: {snapshot_filename}'}), 400
    
    snapshot_data = loaded_snapshots[snapshot_filename]['data']
    
    path_obj = None
    if path is not None:
        # Find the path in the snapshot
        path_idx = loaded_snapshots[snapshot_filename]['index'].get(path)
        if path_idx is not None:
            path_obj = snapshot_data[path_idx]
    elif index is not None:
        if 0 <= index < len(snapshot_data):
            path_obj = snapshot_data[index]
    
    if path_obj is None:
        return jsonify({'error': 'Path not found'}), 404
    
    # Apply filters if requested
    if use_filter:
        return jsonify(filter_path_content(path_obj, snapshot_filename, filter_in, filter_out))
    else:
        return jsonify(format_path_content(path_obj))


@explorer_bp.route('/api/v1/snapshots', methods=['GET'])
def get_snapshots():
    """Get a list of all available snapshots"""
    config = Config()
    snapshot_dir = os.path.join(config.data_path, 'snapshots')
    
    available_snapshots = []
    for file in os.listdir(snapshot_dir):
        if file.startswith('snapshot_') and file.endswith('.jsonl.gz'):
            # Get file stats
            filepath = os.path.join(snapshot_dir, file)
            stat = os.stat(filepath)
            
            # Extract timestamp from filename
            parts = file.split('_')
            if len(parts) >= 3:
                timestamp = int(parts[1]) if parts[1].isdigit() else 0
            else:
                timestamp = 0
                
            available_snapshots.append({
                'filename': file,
                'size': stat.st_size,
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                'loaded': file in loaded_snapshots,
                'timestamp': timestamp
            })
    
    # Sort by timestamp (newest first)
    available_snapshots.sort(key=lambda x: x['timestamp'], reverse=True)
    
    return jsonify({'snapshots': available_snapshots})


def load_snapshot_file(snapshot_filename):
    """Load a snapshot file into memory"""
    config = Config()
    snapshot_dir = os.path.join(config.data_path, 'snapshots')
    snapshot_path = os.path.join(snapshot_dir, snapshot_filename)
    
    if not os.path.exists(snapshot_path):
        return False
    
    # Load the snapshot data
    snapshot_data = []
    with gzip.open(snapshot_path, 'rt', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                snapshot_data.append(json.loads(line))
    
    # Build an index for faster path lookups
    path_index = {}
    for i, path_obj in enumerate(snapshot_data):
        path_index[path_obj['p']] = i
    
    # Load groups if they exist
    base_name = snapshot_filename.replace('.jsonl.gz', '')
    groups_filename = f"{base_name}_groups.jl"
    groups_path = os.path.join(snapshot_dir, groups_filename)
    
    groups_dict = {}
    if os.path.exists(groups_path):
        with open(groups_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    action = json.loads(line)
                    path, item_type, group_name, action_type, timestamp = action
                    
                    full_path = f"{item_type}:{path}"
                    
                    if group_name not in groups_dict:
                        groups_dict[group_name] = set()
                    
                    if action_type == 'add':
                        groups_dict[group_name].add(full_path)
                    elif action_type == 'del':
                        groups_dict[group_name].discard(full_path)
    
    loaded_snapshots[snapshot_filename] = {
        'data': snapshot_data,
        'index': path_index,
        'groups': groups_dict
    }
    
    return True


def calculate_sub_counts(snapshot_filename):
    """Calculate sub file/directory counts and total sizes for each path in the snapshot"""
    snapshot_data = loaded_snapshots[snapshot_filename]['data']
    
    # First pass: calculate counts for each individual path object
    for path_obj in snapshot_data:
        path_obj['sub_file_count'] = len(path_obj.get('f', []))
        path_obj['sub_dir_count'] = len(path_obj.get('d', []))
        
        total_size = sum(path_obj.get('s', []))
        path_obj['total_size'] = total_size


def get_snapshot_info(snapshot_filename):
    """Get information about a loaded snapshot"""
    if snapshot_filename not in loaded_snapshots:
        return None
    
    snapshot_data = loaded_snapshots[snapshot_filename]['data']
    
    if not snapshot_data:
        return {
            'path_count': 0,
            'total_files': 0,
            'total_dirs': 0,
            'total_size': 0
        }
    
    # Get root information from the first path object (usually the root)
    root_obj = snapshot_data[0]
    
    # Count total files and directories across all paths
    total_files = sum(len(path_obj.get('f', [])) for path_obj in snapshot_data)
    total_dirs = sum(len(path_obj.get('d', [])) for path_obj in snapshot_data)
    total_size = sum(sum(path_obj.get('s', [])) for path_obj in snapshot_data)
    
    return {
        'path_count': len(snapshot_data),
        'total_files': total_files,
        'total_dirs': total_dirs,
        'total_size': total_size,
        'total_size_formatted': format_bytes(total_size),
        'root_path': root_obj.get('p', '')
    }


def format_path_content(path_obj):
    """Format path content for API response"""
    import platform
    
    files = []
    for i, filename in enumerate(path_obj.get('f', [])):
        size = path_obj['s'][i] if i < len(path_obj['s']) else 0
        timestamps = path_obj['t'][i] if i < len(path_obj['t']) else [None, None, None]
        
        files.append({
            'name': filename,
            'size': size,
            'size_formatted': format_bytes(size),
            'created': timestamps[0],
            'modified': timestamps[1],
            'accessed': timestamps[2]
        })
    
    dirs = []
    for i, dirname in enumerate(path_obj.get('d', [])):
        timestamps = path_obj['T'][i] if i < len(path_obj['T']) else [None, None, None]
        
        # Calculate sub counts if available
        sub_file_count = path_obj.get('sub_file_count', 0) if i == 0 else 0  # Only for root
        sub_dir_count = path_obj.get('sub_dir_count', 0) if i == 0 else 0  # Only for root
        
        dirs.append({
            'name': dirname,
            'created': timestamps[0],
            'modified': timestamps[1],
            'accessed': timestamps[2],
            'sub_file_count': sub_file_count,
            'sub_dir_count': sub_dir_count
        })
    
    # For Windows, normalize the root path to show drive letters
    current_path = path_obj['p']
    if platform.system() == 'Windows':
        # Convert Windows paths to a more web-friendly format
        current_path = current_path.replace('\\', '/')
    
    return {
        'current_path': current_path,
        'files': files,
        'dirs': dirs,
        'sub_file_count': path_obj.get('sub_file_count', 0),
        'sub_dir_count': path_obj.get('sub_dir_count', 0),
        'total_size': path_obj.get('total_size', 0),
        'total_size_formatted': format_bytes(path_obj.get('total_size', 0))
    }


def filter_path_content(path_obj, snapshot_filename, filter_in, filter_out):
    """Filter path content based on groups"""
    groups_dict = loaded_snapshots[snapshot_filename]['groups']
    
    # Filter files
    filtered_files = []
    for i, filename in enumerate(path_obj.get('f', [])):
        file_path = os.path.join(path_obj['p'], filename)
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
        
        size = path_obj['s'][i] if i < len(path_obj['s']) else 0
        timestamps = path_obj['t'][i] if i < len(path_obj['t']) else [None, None, None]
        
        filtered_files.append({
            'name': filename,
            'size': size,
            'size_formatted': format_bytes(size),
            'created': timestamps[0],
            'modified': timestamps[1],
            'accessed': timestamps[2]
        })
    
    # Filter directories
    filtered_dirs = []
    for i, dirname in enumerate(path_obj.get('d', [])):
        dir_path = os.path.join(path_obj['p'], dirname)
        full_path = f"d:{dir_path}"
        
        # Check if directory should be filtered out
        should_filter_out = False
        for group_name in filter_out:
            if group_name in groups_dict and full_path in groups_dict[group_name]:
                should_filter_out = True
                break
        
        if should_filter_out:
            continue
        
        # If filter_in is specified, only include directories in those groups
        if filter_in:
            should_include = False
            for group_name in filter_in:
                if group_name in groups_dict and full_path in groups_dict[group_name]:
                    should_include = True
                    break
            if not should_include:
                continue
        
        timestamps = path_obj['T'][i] if i < len(path_obj['T']) else [None, None, None]
        
        filtered_dirs.append({
            'name': dirname,
            'created': timestamps[0],
            'modified': timestamps[1],
            'accessed': timestamps[2]
        })
    
    return {
        'current_path': path_obj['p'].replace('\\', '/') if os.name == 'nt' else path_obj['p'],
        'files': filtered_files,
        'dirs': filtered_dirs,
        'filtered': True
    }