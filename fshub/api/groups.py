"""Group management API endpoints"""

import json
from datetime import datetime

from flask import Blueprint, request, jsonify

from ..utils import UnsafePathError
from . import json_body
from .explorer import (
    from_web_path,
    get_snapshot_os,
    groups_path,
    loaded_snapshots,
    snapshots_lock,
)

group_bp = Blueprint('group_bp', __name__)


def _read_group_request(snapshot_filename):
    """Validate a group mutation request.

    Returns (path, group_name, None) or (None, None, (payload, status)).
    """
    data = json_body()
    item_path = data.get('path', '')
    group_name = data.get('group_name', '')

    # Truthiness is not enough: a JSON list would reach the group dict as an
    # unhashable key and turn a client error into a 500.
    if not isinstance(item_path, str) or not isinstance(group_name, str):
        return None, None, ({'error': 'Path and group name must be strings'}, 400)

    if not item_path or not group_name:
        return None, None, ({'error': 'Path and group name are required'}, 400)

    if snapshot_filename not in loaded_snapshots:
        return None, None, ({'error': 'Snapshot not loaded'}, 400)

    # The UI navigates in web paths (/C:/Users), the snapshot is indexed by
    # native ones (C:\Users). Convert here so a group entry always matches.
    item_path = from_web_path(item_path, get_snapshot_os(snapshot_filename))

    return item_path, group_name, None


def _mutate_group(snapshot_filename, item_type, action_type):
    item_path, group_name, error = _read_group_request(snapshot_filename)
    if error:
        payload, status = error
        return jsonify(payload), status

    # The durable log and the in-memory state must move together, otherwise
    # concurrent add/remove calls can persist in the opposite order and the
    # group changes meaning after a reload. The log is written first so a
    # failed write never leaves an unpersisted in-memory change.
    with snapshots_lock:
        entry = loaded_snapshots.get(snapshot_filename)
        if entry is None:
            return jsonify({'error': 'Snapshot not loaded'}), 400

        try:
            save_group_action(snapshot_filename, item_path, item_type, group_name, action_type)
        except UnsafePathError as e:
            return jsonify({'error': str(e)}), 400
        except OSError as e:
            return jsonify({'error': f'Could not persist group change: {e}'}), 500

        group = entry['groups'].setdefault(group_name, {'f': set(), 'd': set()})
        if action_type == 'add':
            group[item_type].add(item_path)
        else:
            group[item_type].discard(item_path)

    return jsonify({'success': True})


@group_bp.route('/api/v1/groups/<snapshot_filename>', methods=['GET'])
def get_groups(snapshot_filename):
    """Get all groups for a specific snapshot with file and directory counts"""
    entry = loaded_snapshots.get(snapshot_filename)
    if entry is None:
        return jsonify({'error': 'Snapshot not loaded'}), 400

    groups_with_counts = []

    for group_name, items in entry['groups'].items():
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
    return _mutate_group(snapshot_filename, 'f', 'add')


@group_bp.route('/api/v1/group/<snapshot_filename>/add_dir', methods=['POST'])
def add_dir_to_group(snapshot_filename):
    """Add a directory to a group"""
    return _mutate_group(snapshot_filename, 'd', 'add')


@group_bp.route('/api/v1/group/<snapshot_filename>/remove_file', methods=['POST'])
def remove_file_from_group(snapshot_filename):
    """Remove a file from a group"""
    return _mutate_group(snapshot_filename, 'f', 'del')


@group_bp.route('/api/v1/group/<snapshot_filename>/remove_dir', methods=['POST'])
def remove_dir_from_group(snapshot_filename):
    """Remove a directory from a group"""
    return _mutate_group(snapshot_filename, 'd', 'del')


@group_bp.route('/api/v1/group/<snapshot_filename>/files', methods=['GET'])
def get_files_in_group(snapshot_filename):
    """Get all files and directories in a specific group"""
    group_name = request.args.get('group_name', '')

    if not group_name:
        return jsonify({'error': 'Group name is required'}), 400

    entry = loaded_snapshots.get(snapshot_filename)
    if entry is None:
        return jsonify({'error': 'Snapshot not loaded'}), 400

    group_data = entry['groups'].get(group_name, {'f': set(), 'd': set()})

    return jsonify({
        'group_name': group_name,
        'files': sorted(group_data.get('f', set())),
        'dirs': sorted(group_data.get('d', set()))
    })


def save_group_action(snapshot_filename, path, item_type, group_name, action_type):
    """Append a group action to the snapshot's group log"""
    action = [path, item_type, group_name, action_type, int(datetime.now().timestamp())]

    with open(groups_path(snapshot_filename), 'a', encoding='utf-8') as f:
        f.write(json.dumps(action) + '\n')
