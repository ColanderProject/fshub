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
    recursive_calc = request.args.get('recursive_calc', default=False, type=lambda x: x.lower() == 'true')
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
        return jsonify(filter_path_content(path_obj, snapshot_filename, filter_in, filter_out, recursive_calc))
    else:
        return jsonify(format_path_content(path_obj, snapshot_filename))


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

                    if group_name not in groups_dict:
                        groups_dict[group_name] = {'f': set(), 'd': set()}

                    if action_type == 'add':
                        groups_dict[group_name][item_type].add(path)
                    elif action_type == 'del':
                        groups_dict[group_name][item_type].discard(path)

    # Pre-calculate sub counts and total sizes for all directories
    # Reset the S and C fields for all directories initially
    for path_obj in snapshot_data:
        path_obj['S'] = sum(path_obj.get('s', []))  # Size of files directly in this directory
        path_obj['C'] = len(path_obj.get('f', []))  # Count of files directly in this directory

    # Recursive helper function to calculate total size and count for a directory
    def calculate_recursive_totals(path_obj):
        """Recursively calculate total size and file count for a directory and its subdirectories"""
        total_size = path_obj['S']  # Start with the size of files directly in this directory
        total_count = path_obj['C']  # Start with the count of files directly in this directory

        # Get the directory path to look up subdirectories
        current_path = path_obj['p']

        # Process all subdirectories of the current path
        for dirname in path_obj.get('d', []):
            subdir_path = os.path.join(current_path, dirname).replace('\\', '/')
            if subdir_path in path_index:
                subdir_idx = path_index[subdir_path]
                subdir_obj = snapshot_data[subdir_idx]

                # Recursively calculate for the subdirectory
                subdir_total_size, subdir_total_count = calculate_recursive_totals(subdir_obj)

                # Add the subdirectory's totals to the current directory's totals
                total_size += subdir_total_size
                total_count += subdir_total_count

        # Store the calculated totals in the path object
        path_obj['S'] = total_size  # Total size including subdirectories
        path_obj['C'] = total_count  # Total file count including subdirectories

        return total_size, total_count

    # To avoid recalculating for subdirectories multiple times, we should process from leaves up to root
    # We'll first mark which paths have already been processed
    # processed = set()

    # def get_path_depth(path):
    #     """Helper function to determine the depth of a path for ordering"""
    #     return path.count('/') if path != '/' else 0  # Root path has depth 0

    # # Sort paths by depth in descending order (deepest first) to ensure children are processed before parents
    # sorted_path_indices = sorted(range(len(snapshot_data)),
    #                              key=lambda i: get_path_depth(snapshot_data[i]['p']),
    #                              reverse=True)

    # Process each path object in depth order to calculate recursive totals
    # for idx in sorted_path_indices:
    #     path_obj = snapshot_data[idx]
    #     if path_obj['p'] not in processed:
    #         calculate_recursive_totals(path_obj)
    #         processed.add(path_obj['p'])
    calculate_recursive_totals(snapshot_data[0])  # Start from root
    loaded_snapshots[snapshot_filename] = {
        'data': snapshot_data,
        'index': path_index,
        'groups': groups_dict
    }

    return True




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


def format_path_content(path_obj, snapshot_filename):
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

        # Get sub counts and total size for this directory (calculated recursively)
        # Find the corresponding subdirectory object to get its S and C values
        subdir_path = os.path.join(path_obj['p'], dirname).replace('\\', '/')

        subdir_obj = None
        if snapshot_filename and subdir_path in loaded_snapshots[snapshot_filename]['index']:
            subdir_idx = loaded_snapshots[snapshot_filename]['index'][subdir_path]
            subdir_obj = loaded_snapshots[snapshot_filename]['data'][subdir_idx]

        dir_info = {
            'name': dirname,
            'created': timestamps[0],
            'modified': timestamps[1],
            'accessed': timestamps[2]
        }

        # Add recursive size and count if available
        if subdir_obj:
            dir_info['S'] = subdir_obj.get('S', 0)  # Total size including subdirectories
            dir_info['C'] = subdir_obj.get('C', 0)  # Total file count including subdirectories
            dir_info['size_formatted'] = format_bytes(subdir_obj.get('S', 0))
            dir_info['file_count'] = subdir_obj.get('C', 0)

        dirs.append(dir_info)

    # For Windows, normalize the root path to show drive letters
    current_path = path_obj['p']
    if platform.system() == 'Windows':
        # Convert Windows paths to a more web-friendly format
        current_path = current_path.replace('\\', '/')

    return {
        'current_path': current_path,
        'files': files,
        'dirs': dirs,
        'S': path_obj.get('S', 0),  # Total size including subdirectories
        'C': path_obj.get('C', 0),  # Total file count including subdirectories
        'total_size_formatted': format_bytes(path_obj.get('S', 0))
    }


def filter_path_content(path_obj, snapshot_filename, filter_in, filter_out, recursive_calc=False):
    """Filter path content based on groups"""
    groups_dict = loaded_snapshots[snapshot_filename]['groups']

    # Filter files
    filtered_files = []
    for i, filename in enumerate(path_obj.get('f', [])):
        file_path = os.path.join(path_obj['p'], filename)

        # Check if file should be filtered out
        should_filter_out = False
        for group_name in filter_out:
            if (group_name in groups_dict and
                file_path in groups_dict[group_name]['f']):
                should_filter_out = True
                break

        if should_filter_out:
            continue

        # If filter_in is specified, only include files in those groups
        if filter_in:
            should_include = False
            for group_name in filter_in:
                if (group_name in groups_dict and
                    file_path in groups_dict[group_name]['f']):
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

        # Check if directory should be filtered out
        should_filter_out = False
        for group_name in filter_out:
            if (group_name in groups_dict and
                dir_path in groups_dict[group_name]['d']):
                should_filter_out = True
                break

        if should_filter_out:
            continue

        # If filter_in is specified, only include directories in those groups
        if filter_in:
            should_include = False
            for group_name in filter_in:
                if (group_name in groups_dict and
                    dir_path in groups_dict[group_name]['d']):
                    should_include = True
                    break
            if not should_include:
                continue

        timestamps = path_obj['T'][i] if i < len(path_obj['T']) else [None, None, None]

        # Get sub counts and total size for this directory (calculated recursively)
        subdir_path = os.path.join(path_obj['p'], dirname).replace('\\', '/')

        dir_info = {
            'name': dirname,
            'created': timestamps[0],
            'modified': timestamps[1],
            'accessed': timestamps[2]
        }

        # Add recursive size and count if available
        if subdir_path in loaded_snapshots[snapshot_filename]['index']:
            subdir_idx = loaded_snapshots[snapshot_filename]['index'][subdir_path]
            subdir_obj = loaded_snapshots[snapshot_filename]['data'][subdir_idx]

            if recursive_calc:
                # Calculate filtered sizes and counts recursively based on the filters
                filtered_size, filtered_count = calculate_filtered_recursive_totals(
                    subdir_obj, snapshot_filename, filter_in, filter_out
                )
                dir_info['S'] = filtered_size
                dir_info['C'] = filtered_count
                dir_info['size_formatted'] = format_bytes(filtered_size)
                dir_info['file_count'] = filtered_count
            else:
                dir_info['S'] = subdir_obj.get('S', 0)  # Total size including subdirectories
                dir_info['C'] = subdir_obj.get('C', 0)  # Total file count including subdirectories
                dir_info['size_formatted'] = format_bytes(subdir_obj.get('S', 0))
                dir_info['file_count'] = subdir_obj.get('C', 0)
        else:
            print("Subdirectory path not found in index:", subdir_path)
        filtered_dirs.append(dir_info)

    return {
        'current_path': path_obj['p'].replace('\\', '/') if os.name == 'nt' else path_obj['p'],
        'files': filtered_files,
        'dirs': filtered_dirs,
        'filtered': True
    }


def filter_on_snapshot(path_obj, data, path_index, filter_in, filter_out, groups_dict, files=None):
    """Recursively for a directory and its subdirectories based on filters"""

    # Start with files directly in this directory that pass the filter
    total_size = 0
    total_count = 0

    for i, filename in enumerate(path_obj.get('f', [])):
        file_path = os.path.join(path_obj['p'], filename)
        # Check if file should be filtered out
        should_filter_out = False
        for group_name in filter_out:
            if (group_name in groups_dict and
                file_path in groups_dict[group_name]['f']):
                should_filter_out = True
                break

        if should_filter_out:
            continue

        # If filter_in is specified, only include files in those groups
        should_include = True
        if filter_in:
            should_include = False
            for group_name in filter_in:
                if (group_name in groups_dict and
                    file_path in groups_dict[group_name]['f']):
                    should_include = True
                    break

        if should_include:
            size = path_obj['s'][i] if i < len(path_obj['s']) else 0
            total_size += size
            total_count += 1
            if files:
                files.append({
                    'name': filename,
                    'full_path': file_path,
                    'size': size,
                    'created': path_obj['t'][i][0] if i < len(path_obj['t']) else None,
                })

    # Process all subdirectories of the current path
    for dirname in path_obj.get('d', []):
        subdir_path = os.path.join(path_obj['p'], dirname).replace('\\', '/')
        if subdir_path in path_index:
            subdir_idx = path_index[subdir_path]
            subdir_obj = data[subdir_idx]

            # Recursively calculate for the subdirectory
            subdir_total_size, subdir_total_count = filter_on_snapshot(subdir_obj, data, path_index,
                filter_in, filter_out, groups_dict, files)

            # Add the subdirectory's totals to the current directory's totals
            total_size += subdir_total_size
            total_count += subdir_total_count

    return total_size, total_count


def calculate_filtered_recursive_totals(path_obj, snapshot_filename, filter_in, filter_out):
    return filter_on_snapshot(path_obj, loaded_snapshots[snapshot_filename]["data"],
                        loaded_snapshots[snapshot_filename]["index"], filter_in, 
                        filter_out, loaded_snapshots[snapshot_filename]["groups"],
                        None)


def get_filter_files(snapshot_filename, filter_in, filter_out):
    """Get files from a snapshot that match the filter criteria"""
    if snapshot_filename not in loaded_snapshots:
        return []

    snapshot_data = loaded_snapshots[snapshot_filename]['data']
    index = loaded_snapshots[snapshot_filename]['index']
    groups_dict = loaded_snapshots[snapshot_filename]['groups']
    path_obj = snapshot_data[0]  # Start from root
    filtered_files = []
    filter_on_snapshot(path_obj, snapshot_data, index, filter_in,
                        filter_out, groups_dict, filtered_files)
    return filtered_files
