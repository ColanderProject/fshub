"""Search API endpoints"""

from flask import Blueprint, request, jsonify
import os
import json
import gzip
from pathlib import Path
from ..config import Config

search_bp = Blueprint('search_bp', __name__)

# Global variable for loaded snapshots
loaded_snapshots = {}  # Key: filename, Value: {'data': list, 'index': dict, 'groups': dict}


@search_bp.route('/api/v1/search', methods=['POST'])
def search_files():
    """Search for files/folders by name across all loaded snapshots"""
    data = request.get_json()
    query = data.get('query', '').lower()
    snapshot_files = data.get('snapshots', [])  # List of snapshot files to search in
    
    if not query:
        return jsonify({'error': 'Query is required'}), 400
    
    results = []
    
    # If no specific snapshots provided, search all loaded snapshots
    if not snapshot_files:
        snapshot_files = list(loaded_snapshots.keys())
    
    for snapshot_filename in snapshot_files:
        if snapshot_filename not in loaded_snapshots:
            # Try to load the snapshot
            if not load_snapshot(snapshot_filename):
                continue
        
        snapshot_data = loaded_snapshots[snapshot_filename]['data']
        
        # Parse query for advanced options (e.g., 'ends:xxx', 'starts:xxx')
        search_mode = 'contains'  # default
        search_term = query
        
        if query.startswith('ends:'):
            search_mode = 'endswith'
            search_term = query[5:].lower()  # Remove 'ends:' prefix
        elif query.startswith('starts:'):
            search_mode = 'startswith'
            search_term = query[7:].lower()  # Remove 'starts:' prefix
        
        for path_obj in snapshot_data:
            current_path = path_obj['p']
            
            # Search in files
            for i, filename in enumerate(path_obj.get('f', [])):
                match = False
                if search_mode == 'contains' and search_term in filename.lower():
                    match = True
                elif search_mode == 'endswith' and filename.lower().endswith(search_term):
                    match = True
                elif search_mode == 'startswith' and filename.lower().startswith(search_term):
                    match = True
                
                if match:
                    result = {
                        'type': 'file',
                        'name': filename,
                        'path': current_path,
                        'full_path': os.path.join(current_path, filename),
                        'size': path_obj['s'][i] if i < len(path_obj['s']) else 0,
                        'snapshot': snapshot_filename
                    }
                    if 't' in path_obj and i < len(path_obj['t']):
                        result['timestamps'] = path_obj['t'][i]
                    results.append(result)
            
            # Search in directories
            for dirname in path_obj.get('d', []):
                match = False
                if search_mode == 'contains' and search_term in dirname.lower():
                    match = True
                elif search_mode == 'endswith' and dirname.lower().endswith(search_term):
                    match = True
                elif search_mode == 'startswith' and dirname.lower().startswith(search_term):
                    match = True
                
                if match:
                    result = {
                        'type': 'directory',
                        'name': dirname,
                        'path': current_path,
                        'full_path': os.path.join(current_path, dirname),
                        'snapshot': snapshot_filename
                    }
                    results.append(result)
    
    return jsonify({'results': results})


def load_snapshot(snapshot_filename):
    """Load a snapshot from file into memory"""
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