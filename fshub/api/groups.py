"""Group management API endpoints"""

from flask import Blueprint, request, jsonify
import os
import json
from datetime import datetime
from ..config import Config
from .explorer import loaded_snapshots

group_bp = Blueprint('group_bp', __name__)


@group_bp.route('/api/v1/groups/<snapshot_filename>', methods=['GET'])
def get_groups(snapshot_filename):
    """Get all groups for a specific snapshot with file and directory counts"""
    if snapshot_filename not in loaded_snapshots:
        # Load groups from file if not already loaded
        return jsonify({'error': 'Snapshot not loaded'}), 400

    snapshot_groups = loaded_snapshots.get(snapshot_filename, {}).get('groups', {})
    groups_with_counts = []

    for group_name, items in snapshot_groups.items():
        file_count = len(items.get('f', set()))
        dir_count = len(items.get('d', set()))

        groups_with_counts.append({
            'name': group_name,
            'file_count': file_count,
            'dir_count': dir_count,
            'total_count': file_count + dir_count
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
    if snapshot_filename not in loaded_snapshots:
        return jsonify({'error': 'Snapshot not loaded'}), 400

    if group_name not in loaded_snapshots[snapshot_filename]['groups']:
        loaded_snapshots[snapshot_filename]['groups'][group_name] = {'f': set(), 'd': set()}

    loaded_snapshots[snapshot_filename]['groups'][group_name]['f'].add(file_path)

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
    if snapshot_filename not in loaded_snapshots:
        return jsonify({'error': 'Snapshot not loaded'}), 400

    if group_name not in loaded_snapshots[snapshot_filename]['groups']:
        loaded_snapshots[snapshot_filename]['groups'][group_name] = {'f': set(), 'd': set()}

    loaded_snapshots[snapshot_filename]['groups'][group_name]['d'].add(dir_path)

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
    if snapshot_filename not in loaded_snapshots:
        return jsonify({'error': 'Snapshot not loaded'}), 400

    if (group_name in loaded_snapshots[snapshot_filename]['groups']):
        loaded_snapshots[snapshot_filename]['groups'][group_name]['f'].discard(file_path)

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
    if snapshot_filename not in loaded_snapshots:
        return jsonify({'error': 'Snapshot not loaded'}), 400

    if (group_name in loaded_snapshots[snapshot_filename]['groups']):
        loaded_snapshots[snapshot_filename]['groups'][group_name]['d'].discard(dir_path)

    # Save to file
    save_group_action(snapshot_filename, dir_path, 'd', group_name, 'del')

    return jsonify({'success': True})


@group_bp.route('/api/v1/group/<snapshot_filename>/files', methods=['GET'])
def get_files_in_group(snapshot_filename):
    """Get all files and directories in a specific group"""
    group_name = request.args.get('group_name', '')

    if not group_name:
        return jsonify({'error': 'Group name is required'}), 400

    if snapshot_filename not in loaded_snapshots:
        return jsonify({'error': 'Snapshot not loaded'}), 400

    group_data = loaded_snapshots.get(snapshot_filename, {})['groups'].get(group_name, {'f': set(), 'd': set()})

    files = list(group_data.get('f', set()))
    dirs = list(group_data.get('d', set()))

    return jsonify({
        'group_name': group_name,
        'files': files,
        'dirs': dirs
    })

def save_group_action(snapshot_filename, path, item_type, group_name, action_type):
    """Save a group action to file"""
    config = Config()
    snapshot_dir = os.path.join(config.data_path, 'snapshots')

    # Create the groups filename based on the snapshot filename
    base_name = snapshot_filename.replace('.jsonl.gz', '')
    groups_filename = f"{base_name}_groups.jl"
    groups_filepath = os.path.join(snapshot_dir, groups_filename)

    action = [path, item_type, group_name, action_type, int(datetime.now().timestamp())]

    with open(groups_filepath, 'a', encoding='utf-8') as f:
        f.write(json.dumps(action) + '\n')