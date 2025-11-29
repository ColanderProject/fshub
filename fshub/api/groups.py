"""Group management API endpoints"""

from flask import Blueprint, request, jsonify
import os
import json
from datetime import datetime
from ..config import Config

group_bp = Blueprint('group_bp', __name__)

# In-memory storage for groups (in production, this should be persistent)
loaded_groups = {}  # Key: snapshot_filename, Value: {group_name: set of paths}


@group_bp.route('/api/v1/groups/<snapshot_filename>', methods=['GET'])
def get_groups(snapshot_filename):
    """Get all groups for a specific snapshot with file and directory counts"""
    if snapshot_filename not in loaded_groups:
        # Load groups from file if not already loaded
        load_groups_for_snapshot(snapshot_filename)

    snapshot_groups = loaded_groups.get(snapshot_filename, {})
    groups_with_counts = []

    for group_name, items in snapshot_groups.items():
        files = [item for item in items if item.startswith('f:')]
        dirs = [item for item in items if item.startswith('d:')]

        groups_with_counts.append({
            'name': group_name,
            'file_count': len(files),
            'dir_count': len(dirs),
            'total_count': len(files) + len(dirs)
        })

    return jsonify({'groups': groups_with_counts})


@group_bp.route('/api/v1/group/<snapshot_filename>/add_file', methods=['POST'])
def add_file_to_group(snapshot_filename):
    """Add a file to a group"""
    data = request.get_json()
    file_path = data.get('path', '')
    group_name = data.get('group_name', '')
    
    if not file_path or not group_name:
        return jsonify({'error': 'Path and group name are required'}), 400
    
    # Add to in-memory storage
    if snapshot_filename not in loaded_groups:
        load_groups_for_snapshot(snapshot_filename)
    
    if snapshot_filename not in loaded_groups:
        loaded_groups[snapshot_filename] = {}
    
    if group_name not in loaded_groups[snapshot_filename]:
        loaded_groups[snapshot_filename][group_name] = set()
    
    loaded_groups[snapshot_filename][group_name].add(f"f:{file_path}")
    
    # Save to file
    save_group_action(snapshot_filename, file_path, 'f', group_name, 'add')
    
    return jsonify({'success': True})


@group_bp.route('/api/v1/group/<snapshot_filename>/add_dir', methods=['POST'])
def add_dir_to_group(snapshot_filename):
    """Add a directory to a group"""
    data = request.get_json()
    dir_path = data.get('path', '')
    group_name = data.get('group_name', '')
    
    if not dir_path or not group_name:
        return jsonify({'error': 'Path and group name are required'}), 400
    
    # Add to in-memory storage
    if snapshot_filename not in loaded_groups:
        load_groups_for_snapshot(snapshot_filename)
    
    if snapshot_filename not in loaded_groups:
        loaded_groups[snapshot_filename] = {}
    
    if group_name not in loaded_groups[snapshot_filename]:
        loaded_groups[snapshot_filename][group_name] = set()
    
    loaded_groups[snapshot_filename][group_name].add(f"d:{dir_path}")
    
    # Save to file
    save_group_action(snapshot_filename, dir_path, 'd', group_name, 'add')
    
    return jsonify({'success': True})


@group_bp.route('/api/v1/group/<snapshot_filename>/remove_file', methods=['POST'])
def remove_file_from_group(snapshot_filename):
    """Remove a file from a group"""
    data = request.get_json()
    file_path = data.get('path', '')
    group_name = data.get('group_name', '')
    
    if not file_path or not group_name:
        return jsonify({'error': 'Path and group name are required'}), 400
    
    # Remove from in-memory storage
    if snapshot_filename not in loaded_groups:
        load_groups_for_snapshot(snapshot_filename)
    
    if (snapshot_filename in loaded_groups and 
        group_name in loaded_groups[snapshot_filename]):
        loaded_groups[snapshot_filename][group_name].discard(f"f:{file_path}")
    
    # Save to file
    save_group_action(snapshot_filename, file_path, 'f', group_name, 'del')
    
    return jsonify({'success': True})


@group_bp.route('/api/v1/group/<snapshot_filename>/remove_dir', methods=['POST'])
def remove_dir_from_group(snapshot_filename):
    """Remove a directory from a group"""
    data = request.get_json()
    dir_path = data.get('path', '')
    group_name = data.get('group_name', '')
    
    if not dir_path or not group_name:
        return jsonify({'error': 'Path and group name are required'}), 400
    
    # Remove from in-memory storage
    if snapshot_filename not in loaded_groups:
        load_groups_for_snapshot(snapshot_filename)
    
    if (snapshot_filename in loaded_groups and 
        group_name in loaded_groups[snapshot_filename]):
        loaded_groups[snapshot_filename][group_name].discard(f"d:{dir_path}")
    
    # Save to file
    save_group_action(snapshot_filename, dir_path, 'd', group_name, 'del')
    
    return jsonify({'success': True})


@group_bp.route('/api/v1/group/<snapshot_filename>/files', methods=['GET'])
def get_files_in_group(snapshot_filename):
    """Get all files and directories in a specific group"""
    group_name = request.args.get('group_name', '')
    
    if not group_name:
        return jsonify({'error': 'Group name is required'}), 400
    
    if snapshot_filename not in loaded_groups:
        load_groups_for_snapshot(snapshot_filename)
    
    group_items = loaded_groups.get(snapshot_filename, {}).get(group_name, set())
    
    files = []
    dirs = []
    
    for item in group_items:
        if item.startswith('f:'):
            files.append(item[2:])  # Remove 'f:' prefix
        elif item.startswith('d:'):
            dirs.append(item[2:])   # Remove 'd:' prefix
    
    return jsonify({
        'group_name': group_name,
        'files': files,
        'dirs': dirs
    })


def load_groups_for_snapshot(snapshot_filename):
    """Load groups for a specific snapshot from file"""
    config = Config()
    snapshot_dir = os.path.join(config.data_path, 'snapshots')
    
    # Create the groups filename based on the snapshot filename
    base_name = snapshot_filename.replace('.jsonl.gz', '')
    groups_filename = f"{base_name}_groups.jl"
    groups_filepath = os.path.join(snapshot_dir, groups_filename)
    
    if not os.path.exists(groups_filepath):
        loaded_groups[snapshot_filename] = {}
        return
    
    groups_dict = {}
    
    with open(groups_filepath, 'r', encoding='utf-8') as f:
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
    
    loaded_groups[snapshot_filename] = groups_dict


def save_group_action(snapshot_filename, path, item_type, group_name, action_type):
    """Save a group action to file"""
    config = Config()
    snapshot_dir = os.path.join(config.data_path, 'snapshots')
    
    # Create the groups filename based on the snapshot filename
    base_name = snapshot_filename.replace('.jsonl.gz', '')
    groups_filename = f"{base_name}_groups.jl"
    groups_filepath = os.path.join(snapshot_dir, groups_filename)
    
    action = [path, item_type, group_name, action_type, datetime.now().isoformat()]
    
    with open(groups_filepath, 'a', encoding='utf-8') as f:
        f.write(json.dumps(action) + '\n')