"""Search API endpoints"""

from flask import Blueprint, request, jsonify
import os
from .explorer import loaded_snapshots
from ..utils import join_snapshot_path

search_bp = Blueprint('search_bp', __name__)


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

    # Check if all requested snapshots are loaded
    unloaded_snapshots = []
    for snapshot_filename in snapshot_files:
        if snapshot_filename not in loaded_snapshots:
            unloaded_snapshots.append(snapshot_filename)

    if unloaded_snapshots:
        return jsonify({'error': f'The following snapshots are not loaded: {", ".join(unloaded_snapshots)}'}), 400

    for snapshot_filename in snapshot_files:
        snapshot_data = loaded_snapshots[snapshot_filename]['data']
        
        # Get OS from the first snapshot object for path joining
        snapshot_os = snapshot_data[0].get('os_name') if snapshot_data else None
        
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
                        'full_path': join_snapshot_path(current_path, filename, snapshot_os=snapshot_os),
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
                        'full_path': join_snapshot_path(current_path, dirname, snapshot_os=snapshot_os),
                        'snapshot': snapshot_filename
                    }
                    results.append(result)
    
    return jsonify({'results': results})
