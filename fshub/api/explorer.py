"""File explorer API endpoints"""

import json
import os
import threading
from datetime import datetime

from flask import Blueprint, request, jsonify

from ..config import get_config
from ..snapshot import InvalidSnapshot, SnapshotBusy, SnapshotLayout, load_snapshot
from ..snapshot.layout import check_snapshot_id
from ..snapshot.loader import adopt_revision, can_skip_rebuild
from ..snapshot.manifest import newest_valid_manifest
from ..utils import (
    UnsafePathError,
    ensure_within,
    format_bytes,
    join_snapshot_path,
    snapshot_dirname,
)
from . import json_body, validate_group_filters

explorer_bp = Blueprint('explorer_bp', __name__)

# Loaded snapshots, shared with the search/group/backup/hash modules.
# Key: snapshot_id, Value: the entry published by fshub.snapshot.load_snapshot.
loaded_snapshots = {}

# Guards mutation of loaded_snapshots (Flask serves requests from many threads).
snapshots_lock = threading.RLock()

# Stand-in for a snapshot that was unloaded while a request was reading it.
EMPTY_SNAPSHOT = {
    'data': [], 'index': {}, 'groups': {},
    'os_name': None, 'root_path': '',
    'snapshot_generation': None, 'manifest_revision': None,
    'event_continuity': None, 'observation_coverage': None,
    'consistency': None,
}

# Entry fields every request handler may rely on, alongside data/index/groups.
VIEW_FIELDS = (
    'snapshot_id', 'os_name', 'root_path', 'snapshot_generation',
    'manifest_revision', 'event_continuity', 'observation_coverage',
    'consistency',
)


def snapshot_dir(snapshot_id):
    """Resolve a snapshot id to its directory inside the snapshot store."""
    check_snapshot_id(snapshot_id)
    return ensure_within(
        get_config().snapshot_dir,
        os.path.join(get_config().snapshot_dir, snapshot_id),
        what='snapshot id',
    )


def groups_path(snapshot_id):
    """Resolve the group-actions file that belongs to a snapshot.

    The log lives inside the snapshot directory and is keyed by the stable
    snapshot id, so it survives every compaction and increment.
    """
    return SnapshotLayout(snapshot_dir(snapshot_id)).groups_path


def _snapshot_view(snapshot_id, copy_groups=False):
    """Capture a stable snapshot entry for work performed outside the lock."""
    with snapshots_lock:
        entry = loaded_snapshots.get(snapshot_id)
        if entry is None:
            return None
        groups = entry['groups']
        if copy_groups:
            groups = {
                name: {'f': set(items.get('f', set())), 'd': set(items.get('d', set()))}
                for name, items in groups.items()
            }
        view = {'data': entry['data'], 'index': entry['index'], 'groups': groups}
        view.update({field: entry.get(field) for field in VIEW_FIELDS})
        return view


def get_snapshot_os(snapshot_id):
    """Return the OS a snapshot was captured on, if known."""
    entry = _snapshot_view(snapshot_id)
    return entry['os_name'] if entry else None


def _version_info(entry):
    """Version and completeness of the data a response was built from.

    Both completeness axes travel together: the derived value collapses
    "events were lost" and "some directories were never read" into one word,
    and those two need different remedies.
    """
    return {
        'snapshot_generation': entry.get('snapshot_generation'),
        'manifest_revision': entry.get('manifest_revision'),
        'event_continuity': entry.get('event_continuity'),
        'observation_coverage': entry.get('observation_coverage'),
        'consistency': entry.get('consistency'),
    }


def to_web_path(path, snapshot_os):
    """Normalize a snapshot path for display/navigation in the browser.

    Windows paths become forward-slash paths with a leading slash so the UI
    only ever deals with one format:  C:\\Users -> /C:/Users
    """
    if snapshot_os != 'Windows':
        return path

    if len(path) >= 2 and path[1] == ':':
        if path.rstrip('\\/') == path[:2]:
            return '/' + path[:2]
        return '/' + path.replace('\\', '/').rstrip('/')
    return path


def from_web_path(path, snapshot_os):
    """Inverse of to_web_path: turn a UI path back into a snapshot path."""
    if snapshot_os != 'Windows':
        return path

    lookup_path = path
    if lookup_path.startswith('/') and len(lookup_path) >= 3 and lookup_path[2] == ':':
        lookup_path = lookup_path[1:]

    if len(lookup_path) >= 2 and lookup_path[1] == ':':
        lookup_path = lookup_path.replace('/', '\\')
        while '\\\\' in lookup_path:
            lookup_path = lookup_path.replace('\\\\', '\\')
        if len(lookup_path) == 2:
            lookup_path += '\\'

    return lookup_path


@explorer_bp.route('/api/v1/load_snapshot', methods=['POST'])
def load_snapshot_endpoint():
    """Load a snapshot into memory"""
    data = json_body()
    snapshot_id = data.get('snapshot_id', '')

    if not isinstance(snapshot_id, str) or not snapshot_id:
        return jsonify({'error': 'snapshot_id must be a non-empty string'}), 400

    try:
        success = load_snapshot_file(snapshot_id)
    except UnsafePathError as e:
        return jsonify({'error': str(e)}), 400
    except SnapshotBusy as e:
        # A writer kept moving the snapshot underneath the loader. Nothing is
        # broken; the caller should simply try again.
        return jsonify({'error': str(e), 'retry': True}), 503
    except (InvalidSnapshot, OSError, EOFError, ValueError, TypeError,
            KeyError, IndexError) as e:
        # Truncated, half-written or foreign files are a normal operator
        # mistake, not a server fault. IndexError is included because the
        # loader indexes parallel arrays directly once validation passed.
        return jsonify({'error': f'Invalid snapshot: {e}'}), 400

    if not success:
        return jsonify({'error': f'Failed to load snapshot: {snapshot_id}'}), 400

    return jsonify({
        'success': True,
        'message': f'Snapshot {snapshot_id} loaded successfully',
        'snapshot_info': get_snapshot_info(snapshot_id)
    })


@explorer_bp.route('/api/v1/unload_snapshot', methods=['POST'])
def unload_snapshot():
    """Unload a snapshot from memory"""
    data = json_body()
    snapshot_id = data.get('snapshot_id', '')
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return jsonify({'error': 'snapshot_id must be a non-empty string'}), 400

    with snapshots_lock:
        if snapshot_id in loaded_snapshots:
            del loaded_snapshots[snapshot_id]
            return jsonify({'success': True})

    return jsonify({'error': 'Snapshot not loaded'}), 400


@explorer_bp.route('/api/v1/getPath', methods=['GET'])
def get_path():
    """Get the content of a specific path from a snapshot"""
    snapshot_id = request.args.get('snapshot', '')
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

    filter_error = validate_group_filters(filter_in, filter_out)
    if filter_error:
        return jsonify({'error': filter_error}), 400

    if not snapshot_id:
        return jsonify({'error': 'snapshot id is required'}), 400

    # Use either path or index, not both
    if path is not None and index is not None:
        return jsonify({'error': 'Cannot specify both path and index'}), 400

    # Capture one coherent view. Filtering also copies the mutable group sets,
    # so concurrent group changes cannot alter this request halfway through.
    entry = _snapshot_view(snapshot_id, copy_groups=use_filter)
    if entry is None:
        return jsonify({'error': f'Snapshot not found: {snapshot_id}'}), 400

    snapshot_data = entry['data']

    path_obj = None
    if path is not None:
        lookup_path = from_web_path(path, entry['os_name'])
        path_idx = entry['index'].get(lookup_path)
        if path_idx is not None:
            path_obj = snapshot_data[path_idx]
    elif index is not None:
        # An index only identifies a directory within one generation, so the
        # response carries the generation it was resolved against.
        if 0 <= index < len(snapshot_data):
            path_obj = snapshot_data[index]

    if path_obj is None:
        return jsonify({'error': 'Path not found'}), 404

    if use_filter:
        content = filter_path_content(
            path_obj, snapshot_id, filter_in, filter_out, recursive_calc, entry=entry,
        )
    else:
        content = format_path_content(path_obj, snapshot_id, entry=entry)
    content.update(_version_info(entry))
    return jsonify(content)


@explorer_bp.route('/api/v1/snapshots', methods=['GET'])
def get_snapshots():
    """Get a list of all available snapshots"""
    return jsonify({'snapshots': list_snapshots()})


def list_snapshots():
    """Describe every snapshot directory that has a valid manifest.

    Reading one manifest per snapshot is cheap; reading its base is not, so
    nothing here touches the payload files.
    """
    root = get_config().snapshot_dir
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []

    snapshots = []
    for name in names:
        layout = SnapshotLayout(os.path.join(root, name))
        if not os.path.isdir(layout.manifest_dir):
            continue
        try:
            manifest = newest_valid_manifest(layout)
        except (InvalidSnapshot, OSError):
            continue

        base = manifest.base
        increments = manifest.increments
        total_size = base['size'] + sum(inc['size'] for inc in increments)
        record_count = base['record_count'] + sum(
            inc['record_count'] for inc in increments)
        created = increments[-1]['created_at'] if increments else base['created_at']

        snapshots.append({
            'snapshot_id': name,
            'root_path': manifest.root_path,
            'os_name': manifest.os_name,
            'snapshot_generation': manifest.generation,
            'manifest_revision': manifest.revision,
            'record_count': record_count,
            'inc_count': len(increments),
            'total_size': total_size,
            'size': total_size,
            'event_continuity': manifest.event_continuity,
            'observation_coverage': base['observation_coverage'],
            'modified': datetime.fromtimestamp(created).isoformat(),
            'timestamp': created,
            'loaded': name in loaded_snapshots,
        })

    snapshots.sort(key=lambda item: item['timestamp'], reverse=True)
    return snapshots


def _load_groups(snapshot_id):
    """Replay the group action log into {group: {'f': set, 'd': set}}."""
    try:
        path = groups_path(snapshot_id)
    except UnsafePathError:
        return {}

    groups_dict = {}
    if not os.path.exists(path):
        return groups_dict

    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                action = json.loads(line)
                if not isinstance(action, list) or len(action) != 5:
                    continue
                item_path, item_type, group_name, action_type, _ts = action
                if not isinstance(item_path, str) or not isinstance(group_name, str):
                    continue
                if not item_path or not group_name:
                    continue
                if item_type not in ('f', 'd') or action_type not in ('add', 'del'):
                    continue
            except (ValueError, TypeError):
                continue

            group = groups_dict.setdefault(group_name, {'f': set(), 'd': set()})
            if action_type == 'add':
                group[item_type].add(item_path)
            else:
                group[item_type].discard(item_path)

    return groups_dict


def _is_fully_local(path_obj, index):
    """True unless Windows reported that this file is not fully local."""
    states = path_obj['c']
    if states is None:
        return True
    return index >= len(states) or states[index] != 'not_fully_local'


def load_snapshot_file(snapshot_id):
    """Load a snapshot into memory and publish it as one complete entry."""
    directory = snapshot_dir(snapshot_id)
    if not os.path.isdir(directory):
        return False

    with snapshots_lock:
        loaded = loaded_snapshots.get(snapshot_id)

    if loaded is not None:
        # A compaction rewrites the files but promises the same logical tree.
        # Verifying that promise costs one manifest read; rebuilding the tree
        # would cost a full pass over the base.
        manifest = newest_valid_manifest(SnapshotLayout(directory))
        if can_skip_rebuild(loaded, manifest):
            with snapshots_lock:
                loaded_snapshots[snapshot_id] = adopt_revision(loaded, manifest)
            return True

    # Reading, rebuilding, verifying and totalling all happen outside the
    # global lock; only the finished entry is published, so a concurrent
    # request sees either the whole old version or the whole new one.
    entry = load_snapshot(directory)
    entry['groups'] = _load_groups(snapshot_id)

    with snapshots_lock:
        loaded_snapshots[snapshot_id] = entry

    return True


def get_snapshot_info(snapshot_id):
    """Get information about a loaded snapshot."""
    entry = _snapshot_view(snapshot_id)
    if entry is None:
        return None

    snapshot_data = entry['data']
    info = {
        'path_count': len(snapshot_data),
        'total_files': sum(len(path_obj['f']) for path_obj in snapshot_data),
        'total_dirs': sum(len(path_obj['d']) for path_obj in snapshot_data),
        'total_size': snapshot_data[0]['S'] if snapshot_data else 0,
        'root_path': entry['root_path'],
        'os_name': entry['os_name'],
    }
    info['total_size_formatted'] = format_bytes(info['total_size'])
    info.update(_version_info(entry))
    return info


def _file_entry(path_obj, i):
    states = path_obj['c']
    return {
        'name': path_obj['f'][i],
        'size': path_obj['s'][i],
        'size_formatted': format_bytes(path_obj['s'][i]),
        'cloud_state': None if states is None else states[i],
        'created': path_obj['t'][i][0],
        'modified': path_obj['t'][i][1],
        'accessed': path_obj['t'][i][2],
    }


def _dir_entry(path_obj, i):
    timestamps = path_obj['T'][i]
    return {
        'name': path_obj['d'][i],
        'created': timestamps[0],
        'modified': timestamps[1],
        'accessed': timestamps[2],
        'traversal': path_obj['x'][i],
    }


def _apply_totals(dir_info, size, count, local_size=None, local_count=None):
    local_size = size if local_size is None else local_size
    local_count = count if local_count is None else local_count
    dir_info['S'] = size
    dir_info['C'] = count
    dir_info['local_size'] = local_size
    dir_info['local_file_count'] = local_count
    dir_info['size_formatted'] = format_bytes(size)
    dir_info['file_count'] = count


def format_path_content(path_obj, snapshot_id, entry=None):
    """Format path content for API response."""
    entry = entry or _snapshot_view(snapshot_id) or EMPTY_SNAPSHOT
    data = entry['data']
    index = entry['index']
    snapshot_os = entry['os_name']

    files = [_file_entry(path_obj, i) for i in range(len(path_obj['f']))]

    dirs = []
    for i, dirname in enumerate(path_obj['d']):
        dir_info = _dir_entry(path_obj, i)
        subdir_path = join_snapshot_path(path_obj['p'], dirname, snapshot_os=snapshot_os)
        subdir_idx = index.get(subdir_path)
        if subdir_idx is not None:
            subdir_obj = data[subdir_idx]
            _apply_totals(
                dir_info,
                subdir_obj['S'],
                subdir_obj['C'],
                subdir_obj['LS'],
                subdir_obj['LC'],
            )
        dirs.append(dir_info)

    return {
        'current_path': to_web_path(path_obj['p'], snapshot_os),
        'files': files,
        'dirs': dirs,
        'S': path_obj['S'],
        'C': path_obj['C'],
        'local_size': path_obj['LS'],
        'local_file_count': path_obj['LC'],
        'total_size_formatted': format_bytes(path_obj['S'])
    }


def _in_any_group(groups_dict, group_names, item_type, item_path):
    for group_name in group_names:
        group = groups_dict.get(group_name)
        if group and item_path in group[item_type]:
            return True
    return False


def filter_path_content(path_obj, snapshot_id, filter_in, filter_out,
                        recursive_calc=False, entry=None):
    """Filter path content based on groups."""
    entry = entry or _snapshot_view(snapshot_id, copy_groups=True) or EMPTY_SNAPSHOT
    groups_dict = entry['groups']
    index = entry['index']
    data = entry['data']
    snapshot_os = entry['os_name']

    filtered_files = []
    for i, filename in enumerate(path_obj['f']):
        file_path = join_snapshot_path(path_obj['p'], filename, snapshot_os=snapshot_os)

        if _in_any_group(groups_dict, filter_out, 'f', file_path):
            continue
        if filter_in and not _in_any_group(groups_dict, filter_in, 'f', file_path):
            continue

        filtered_files.append(_file_entry(path_obj, i))

    filtered_dirs = []
    for i, dirname in enumerate(path_obj['d']):
        dir_path = join_snapshot_path(path_obj['p'], dirname, snapshot_os=snapshot_os)

        if _in_any_group(groups_dict, filter_out, 'd', dir_path):
            continue
        if filter_in and not _in_any_group(groups_dict, filter_in, 'd', dir_path):
            continue

        dir_info = _dir_entry(path_obj, i)

        subdir_idx = index.get(dir_path)
        if subdir_idx is not None:
            subdir_obj = data[subdir_idx]
            if recursive_calc:
                totals = calculate_filtered_recursive_totals(
                    subdir_obj, snapshot_id, filter_in, filter_out,
                    entry=entry,
                )
                _apply_totals(dir_info, *totals)
            else:
                _apply_totals(
                    dir_info,
                    subdir_obj['S'],
                    subdir_obj['C'],
                    subdir_obj['LS'],
                    subdir_obj['LC'],
                )

        filtered_dirs.append(dir_info)

    size = sum(file['size'] for file in filtered_files)
    local_files = [file for file in filtered_files
                   if file['cloud_state'] != 'not_fully_local']
    local_size = sum(file['size'] for file in local_files)
    return {
        'current_path': to_web_path(path_obj['p'], snapshot_os),
        'files': filtered_files,
        'dirs': filtered_dirs,
        'S': size + sum(directory.get('S', 0) for directory in filtered_dirs),
        'C': len(filtered_files) + sum(directory.get('C', 0)
                                       for directory in filtered_dirs),
        'local_size': local_size + sum(directory.get('local_size', 0)
                                       for directory in filtered_dirs),
        'local_file_count': len(local_files) + sum(
            directory.get('local_file_count', 0) for directory in filtered_dirs),
        'filtered': True
    }


def filter_on_snapshot(path_obj, data, path_index, filter_in, filter_out, groups_dict,
                       files=None, dirinFilterSet=None, allIncluded=False,
                       snapshot_os=None):
    """Total a directory tree, honouring the group filters.

    Returns total and fully-local size/count values and, when ``files`` is
    provided, appends every matching file to it.

    Uses an explicit stack rather than recursion: snapshots routinely nest
    deeper than Python's recursion limit.
    """
    total_size = 0
    total_count = 0
    local_size = 0
    local_count = 0

    # (path_obj, allIncluded) pairs still to visit.
    stack = [(path_obj, allIncluded)]
    visited = set()

    while stack:
        current, inherited_include = stack.pop()

        current_path = current['p']
        if current_path in visited:
            continue
        visited.add(current_path)

        for i, filename in enumerate(current['f']):
            file_path = join_snapshot_path(current_path, filename, snapshot_os=snapshot_os)

            if _in_any_group(groups_dict, filter_out, 'f', file_path):
                continue

            should_include = True
            if filter_in and not inherited_include:
                should_include = _in_any_group(groups_dict, filter_in, 'f', file_path)

            if not should_include:
                continue

            size = current['s'][i]
            total_size += size
            total_count += 1
            if _is_fully_local(current, i):
                local_size += size
                local_count += 1
            if files is not None:
                files.append({
                    'name': filename,
                    'full_path': file_path,
                    'size': size,
                    'created': current['t'][i][0],
                })

        # Reversed so that popping the stack visits children in listed order.
        for dirname in reversed(current['d']):
            subdir_include = inherited_include
            subdir_path = join_snapshot_path(current_path, dirname, snapshot_os=snapshot_os)

            if _in_any_group(groups_dict, filter_out, 'd', subdir_path):
                continue

            should_include = True
            if filter_in and not subdir_include:
                should_include = False
                if dirinFilterSet is not None and subdir_path in dirinFilterSet:
                    should_include = True
                    # A directory listed in filter_in selects its whole subtree.
                    if _in_any_group(groups_dict, filter_in, 'd', subdir_path):
                        subdir_include = True

            if not should_include:
                continue

            subdir_idx = path_index.get(subdir_path)
            if subdir_idx is None:
                continue

            stack.append((data[subdir_idx], subdir_include))

    return total_size, total_count, local_size, local_count


def calculate_filtered_recursive_totals(path_obj, snapshot_id, filter_in, filter_out,
                                        entry=None):
    entry = entry or _snapshot_view(snapshot_id, copy_groups=True) or EMPTY_SNAPSHOT
    return filter_on_snapshot(
        path_obj, entry['data'], entry['index'],
        filter_in, filter_out, entry['groups'], None,
        snapshot_os=entry['os_name'],
    )


def get_filtered_files(snapshot_id, filter_in, filter_out):
    """Get files from a snapshot that match the filter criteria"""
    entry = _snapshot_view(snapshot_id, copy_groups=True)
    if not entry or not entry['data']:
        return []

    snapshot_data = entry['data']
    index = entry['index']
    groups_dict = entry['groups']
    snapshot_os = entry['os_name']

    # Every ancestor of a selected file or directory must stay walkable,
    # otherwise the recursion stops before it reaches the selection.
    dirinFilterSet = set()
    for group_name in filter_in:
        group = groups_dict.get(group_name)
        if not group:
            continue
        for item_path in list(group['d']) + list(group['f']):
            parent = snapshot_dirname(item_path, snapshot_os=snapshot_os)
            while parent:
                if parent in dirinFilterSet:
                    break
                dirinFilterSet.add(parent)
                parent = snapshot_dirname(parent, snapshot_os=snapshot_os)
        dirinFilterSet.update(group['d'])

    filtered_files = []
    filter_on_snapshot(
        snapshot_data[0], snapshot_data, index, filter_in,
        filter_out, groups_dict, filtered_files, dirinFilterSet,
        snapshot_os=snapshot_os,
    )
    return filtered_files
