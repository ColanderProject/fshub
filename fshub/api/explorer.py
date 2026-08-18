"""File explorer API endpoints"""

import gzip
import json
import os
import threading
from datetime import datetime
from itertools import zip_longest

from flask import Blueprint, request, jsonify

from ..config import get_config
from ..utils import (
    UnsafePathError,
    format_bytes,
    join_snapshot_path,
    safe_join,
    snapshot_dirname,
)
from . import json_body, validate_group_filters

explorer_bp = Blueprint('explorer_bp', __name__)

# Loaded snapshots, shared with the search/group/backup/hash modules.
# Key: filename, Value: {'data': list, 'index': dict, 'groups': dict}
loaded_snapshots = {}

# Guards mutation of loaded_snapshots (Flask serves requests from many threads).
snapshots_lock = threading.RLock()

# Stand-in for a snapshot that was unloaded while a request was reading it.
EMPTY_SNAPSHOT = {'data': [], 'index': {}, 'groups': {}}

SNAPSHOT_PREFIX = 'snapshot_'
SNAPSHOT_SUFFIX = '.jsonl.gz'
# Snapshots written with `fshub scan --use-index` are split in two files:
# <base>_index.jsonl.gz holds the paths, <base>.bin.gz the rest of each record.
INDEX_SUFFIX = '_index.jsonl.gz'


def snapshot_path(snapshot_filename):
    """Resolve a snapshot filename to a path inside the snapshot directory."""
    if not snapshot_filename.endswith(SNAPSHOT_SUFFIX):
        raise UnsafePathError(f'Invalid snapshot name: {snapshot_filename!r}')
    return safe_join(get_config().snapshot_dir, snapshot_filename, what='snapshot name')


def groups_path(snapshot_filename):
    """Resolve the group-actions file that belongs to a snapshot."""
    if not snapshot_filename.endswith(SNAPSHOT_SUFFIX):
        raise UnsafePathError(f'Invalid snapshot name: {snapshot_filename!r}')
    base_name = snapshot_filename[: -len(SNAPSHOT_SUFFIX)]
    return safe_join(
        get_config().snapshot_dir,
        f'{base_name}_groups.jl',
        what='snapshot name',
    )


def _snapshot_view(snapshot_filename, copy_groups=False):
    """Capture a stable snapshot entry for work performed outside the lock."""
    with snapshots_lock:
        entry = loaded_snapshots.get(snapshot_filename)
        if entry is None:
            return None
        groups = entry['groups']
        if copy_groups:
            groups = {
                name: {'f': set(items.get('f', set())), 'd': set(items.get('d', set()))}
                for name, items in groups.items()
            }
        return {'data': entry['data'], 'index': entry['index'], 'groups': groups}


def get_snapshot_os(snapshot_filename):
    """Return the OS a snapshot was captured on, if known."""
    entry = _snapshot_view(snapshot_filename)
    if not entry or not entry['data']:
        return None
    return entry['data'][0].get('os_name')


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
def load_snapshot():
    """Load a snapshot file into memory"""
    data = json_body()
    snapshot_filename = data.get('filename', '')

    if not isinstance(snapshot_filename, str) or not snapshot_filename:
        return jsonify({'error': 'Snapshot filename must be a non-empty string'}), 400

    try:
        success = load_snapshot_file(snapshot_filename)
    except UnsafePathError as e:
        return jsonify({'error': str(e)}), 400
    except (OSError, EOFError, ValueError, TypeError, KeyError) as e:
        # Truncated, half-written or foreign files are a normal operator
        # mistake, not a server fault.
        return jsonify({'error': f'Invalid snapshot file: {e}'}), 400

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
    data = json_body()
    snapshot_filename = data.get('filename', '')
    if not isinstance(snapshot_filename, str) or not snapshot_filename:
        return jsonify({'error': 'Snapshot filename must be a non-empty string'}), 400

    with snapshots_lock:
        if snapshot_filename in loaded_snapshots:
            del loaded_snapshots[snapshot_filename]
            return jsonify({'success': True})

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

    filter_error = validate_group_filters(filter_in, filter_out)
    if filter_error:
        return jsonify({'error': filter_error}), 400

    if not snapshot_filename:
        return jsonify({'error': 'Snapshot filename is required'}), 400

    # Use either path or index, not both
    if path is not None and index is not None:
        return jsonify({'error': 'Cannot specify both path and index'}), 400

    # Capture one coherent view. Filtering also copies the mutable group sets,
    # so concurrent group changes cannot alter this request halfway through.
    entry = _snapshot_view(snapshot_filename, copy_groups=use_filter)
    if entry is None:
        return jsonify({'error': f'Snapshot not found: {snapshot_filename}'}), 400

    snapshot_data = entry['data']
    snapshot_os = snapshot_data[0].get('os_name') if snapshot_data else None

    path_obj = None
    if path is not None:
        lookup_path = from_web_path(path, snapshot_os)
        path_idx = entry['index'].get(lookup_path)
        if path_idx is not None:
            path_obj = snapshot_data[path_idx]
    elif index is not None:
        if 0 <= index < len(snapshot_data):
            path_obj = snapshot_data[index]

    if path_obj is None:
        return jsonify({'error': 'Path not found'}), 404

    # Apply filters if requested
    if use_filter:
        return jsonify(filter_path_content(
            path_obj, snapshot_filename, filter_in, filter_out, recursive_calc, entry=entry,
        ))
    return jsonify(format_path_content(path_obj, snapshot_filename, entry=entry))


@explorer_bp.route('/api/v1/snapshots', methods=['GET'])
def get_snapshots():
    """Get a list of all available snapshots"""
    snapshot_dir = get_config().snapshot_dir

    available_snapshots = []
    for file in list_snapshot_files(snapshot_dir):
        filepath = os.path.join(snapshot_dir, file)
        try:
            stat = os.stat(filepath)
        except OSError:
            continue

        # Extract timestamp from filename: snapshot_<ts>_<count>.jsonl.gz
        parts = file.split('_')
        timestamp = int(parts[1]) if len(parts) >= 3 and parts[1].isdigit() else 0

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


def list_snapshot_files(snapshot_dir):
    """List snapshot files, tolerating a missing data directory."""
    try:
        entries = os.listdir(snapshot_dir)
    except OSError:
        return []
    return sorted(
        f for f in entries
        if f.startswith(SNAPSHOT_PREFIX) and f.endswith(SNAPSHOT_SUFFIX)
    )


def _load_groups(snapshot_filename):
    """Replay the group action log into {group: {'f': set, 'd': set}}."""
    try:
        path = groups_path(snapshot_filename)
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
    states = path_obj.get('c', [])
    return index >= len(states) or states[index] != 'not_fully_local'


def _compute_recursive_totals(snapshot_data, path_index, snapshot_os):
    """Fill in logical and fully-local totals for every directory.

    Done bottom-up over an explicit child->parent map so it stays iterative:
    a recursive walk blows the Python stack on deep trees, and a purely
    string-based parent lookup cannot express the Windows "This PC" root.
    """
    for path_obj in snapshot_data:
        sizes = path_obj.get('s', [])
        path_obj['S'] = sum(sizes)
        path_obj['C'] = len(path_obj.get('f', []))
        local_indices = [i for i in range(len(path_obj.get('f', [])))
                         if _is_fully_local(path_obj, i)]
        path_obj['LS'] = sum(sizes[i] for i in local_indices if i < len(sizes))
        path_obj['LC'] = len(local_indices)

    # child index -> parent index, derived from the recorded directory lists
    parent_of = {}
    for i, path_obj in enumerate(snapshot_data):
        for dirname in path_obj.get('d', []):
            child_path = join_snapshot_path(path_obj['p'], dirname, snapshot_os=snapshot_os)
            child_idx = path_index.get(child_path)
            if child_idx is not None and child_idx != i and child_idx not in parent_of:
                parent_of[child_idx] = i

    # Depth of each node, memoised, so children are always summed first.
    depth = {}

    def depth_of(idx):
        stack = []
        cur = idx
        while cur is not None and cur not in depth and cur not in stack:
            stack.append(cur)
            cur = parent_of.get(cur)

        # -1 so the top-most ancestor in the chain ends up at depth 0.
        base = depth[cur] if cur is not None and cur in depth else -1
        for node in reversed(stack):
            base += 1
            depth[node] = base
        return depth[idx]

    order = sorted(range(len(snapshot_data)), key=depth_of, reverse=True)

    for idx in order:
        parent_idx = parent_of.get(idx)
        if parent_idx is None:
            continue
        snapshot_data[parent_idx]['S'] += snapshot_data[idx]['S']
        snapshot_data[parent_idx]['C'] += snapshot_data[idx]['C']
        snapshot_data[parent_idx]['LS'] += snapshot_data[idx]['LS']
        snapshot_data[parent_idx]['LC'] += snapshot_data[idx]['LC']


def _validate_snapshot_record(record):
    """Validate the structural fields used by the explorer."""
    if not isinstance(record, dict) or not isinstance(record.get('p'), str):
        raise ValueError('snapshot records must be objects with a string path')

    for field in ('f', 'd'):
        values = record.get(field, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f'snapshot field {field!r} must be a list of strings')

    for field in ('s', 't', 'T', 'c'):
        if not isinstance(record.get(field, []), list):
            raise ValueError(f'snapshot field {field!r} must be a list')

    return record


def _read_snapshot_records(snapshot_filename):
    """Read the path records of a snapshot, in either storage format."""
    path = snapshot_path(snapshot_filename)
    if not os.path.exists(path):
        return None

    if not snapshot_filename.endswith(INDEX_SUFFIX):
        records = []
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    records.append(_validate_snapshot_record(json.loads(line)))
        return records

    base_name = snapshot_filename[: -len(INDEX_SUFFIX)]
    data_path = safe_join(
        get_config().snapshot_dir, f'{base_name}.bin.gz', what='snapshot name'
    )
    if not os.path.exists(data_path):
        return None

    records = []
    with gzip.open(path, 'rt', encoding='utf-8') as index_file, \
            gzip.open(data_path, 'rt', encoding='utf-8') as data_file:
        # zip_longest, not zip: a missing tail in either stream must be an
        # error instead of a snapshot that silently ends early.
        for index_line, data_line in zip_longest(index_file, data_file):
            if not index_line or not data_line or not index_line.strip():
                raise ValueError('index and data files do not match')
            record = json.loads(data_line)
            index_record = json.loads(index_line)
            if not isinstance(record, dict) or not isinstance(index_record, dict):
                raise ValueError('index and data records must be objects')
            record['p'] = index_record.get('p')
            records.append(_validate_snapshot_record(record))
    return records


def load_snapshot_file(snapshot_filename):
    """Load a snapshot file into memory"""
    snapshot_data = _read_snapshot_records(snapshot_filename)
    if snapshot_data is None:
        return False

    # Build an index for faster path lookups
    path_index = {}
    for i, path_obj in enumerate(snapshot_data):
        path_index[path_obj['p']] = i

    snapshot_os = snapshot_data[0].get('os_name') if snapshot_data else None
    _compute_recursive_totals(snapshot_data, path_index, snapshot_os)

    # Do disk I/O outside the global lock, then publish one complete entry.
    groups = _load_groups(snapshot_filename)
    with snapshots_lock:
        loaded_snapshots[snapshot_filename] = {
            'data': snapshot_data,
            'index': path_index,
            'groups': groups,
        }

    return True


def get_snapshot_info(snapshot_filename):
    """Get information about a loaded snapshot."""
    entry = _snapshot_view(snapshot_filename)
    if entry is None:
        return None

    snapshot_data = entry['data']

    if not snapshot_data:
        return {
            'path_count': 0,
            'total_files': 0,
            'total_dirs': 0,
            'total_size': 0,
            'total_size_formatted': format_bytes(0),
            'root_path': '',
        }

    root_obj = snapshot_data[0]

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


def _file_entry(path_obj, i):
    size = path_obj['s'][i] if i < len(path_obj.get('s', [])) else 0
    timestamps = path_obj['t'][i] if i < len(path_obj.get('t', [])) else [None, None, None]
    cloud_state = path_obj['c'][i] if i < len(path_obj.get('c', [])) else None
    return {
        'name': path_obj['f'][i],
        'size': size,
        'size_formatted': format_bytes(size),
        'cloud_state': cloud_state,
        'created': timestamps[0],
        'modified': timestamps[1],
        'accessed': timestamps[2],
    }


def _dir_entry(path_obj, i):
    timestamps = path_obj['T'][i] if i < len(path_obj.get('T', [])) else [None, None, None]
    return {
        'name': path_obj['d'][i],
        'created': timestamps[0],
        'modified': timestamps[1],
        'accessed': timestamps[2],
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


def format_path_content(path_obj, snapshot_filename, entry=None):
    """Format path content for API response."""
    entry = entry or _snapshot_view(snapshot_filename) or EMPTY_SNAPSHOT
    data = entry['data']
    index = entry['index']
    snapshot_os = data[0].get('os_name') if data else None

    files = [_file_entry(path_obj, i) for i in range(len(path_obj.get('f', [])))]

    dirs = []
    for i, dirname in enumerate(path_obj.get('d', [])):
        dir_info = _dir_entry(path_obj, i)
        subdir_path = join_snapshot_path(path_obj['p'], dirname, snapshot_os=snapshot_os)
        subdir_idx = index.get(subdir_path)
        if subdir_idx is not None:
            subdir_obj = data[subdir_idx]
            _apply_totals(
                dir_info,
                subdir_obj.get('S', 0),
                subdir_obj.get('C', 0),
                subdir_obj.get('LS', subdir_obj.get('S', 0)),
                subdir_obj.get('LC', subdir_obj.get('C', 0)),
            )
        dirs.append(dir_info)

    return {
        'current_path': to_web_path(path_obj['p'], snapshot_os),
        'files': files,
        'dirs': dirs,
        'S': path_obj.get('S', 0),
        'C': path_obj.get('C', 0),
        'local_size': path_obj.get('LS', path_obj.get('S', 0)),
        'local_file_count': path_obj.get('LC', path_obj.get('C', 0)),
        'total_size_formatted': format_bytes(path_obj.get('S', 0))
    }


def _in_any_group(groups_dict, group_names, item_type, item_path):
    for group_name in group_names:
        group = groups_dict.get(group_name)
        if group and item_path in group[item_type]:
            return True
    return False


def filter_path_content(path_obj, snapshot_filename, filter_in, filter_out,
                        recursive_calc=False, entry=None):
    """Filter path content based on groups."""
    entry = entry or _snapshot_view(snapshot_filename, copy_groups=True) or EMPTY_SNAPSHOT
    groups_dict = entry['groups']
    index = entry['index']
    data = entry['data']
    snapshot_os = data[0].get('os_name') if data else None

    filtered_files = []
    for i, filename in enumerate(path_obj.get('f', [])):
        file_path = join_snapshot_path(path_obj['p'], filename, snapshot_os=snapshot_os)

        if _in_any_group(groups_dict, filter_out, 'f', file_path):
            continue
        if filter_in and not _in_any_group(groups_dict, filter_in, 'f', file_path):
            continue

        filtered_files.append(_file_entry(path_obj, i))

    filtered_dirs = []
    for i, dirname in enumerate(path_obj.get('d', [])):
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
                size, count = calculate_filtered_recursive_totals(
                    subdir_obj, snapshot_filename, filter_in, filter_out, entry=entry,
                )
                local_size, local_count = calculate_filtered_recursive_totals(
                    subdir_obj, snapshot_filename, filter_in, filter_out,
                    entry=entry, local_only=True,
                )
                _apply_totals(dir_info, size, count, local_size, local_count)
            else:
                _apply_totals(
                    dir_info,
                    subdir_obj.get('S', 0),
                    subdir_obj.get('C', 0),
                    subdir_obj.get('LS', subdir_obj.get('S', 0)),
                    subdir_obj.get('LC', subdir_obj.get('C', 0)),
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
                       local_only=False):
    """Total a directory tree, honouring the group filters.

    Returns (total_size, total_count) and, when ``files`` is provided,
    appends every matching file to it.

    Uses an explicit stack rather than recursion: snapshots routinely nest
    deeper than Python's recursion limit.
    """
    total_size = 0
    total_count = 0

    snapshot_os = data[0].get('os_name') if data else None

    # (path_obj, allIncluded) pairs still to visit.
    stack = [(path_obj, allIncluded)]
    visited = set()

    while stack:
        current, inherited_include = stack.pop()

        current_path = current['p']
        if current_path in visited:
            continue
        visited.add(current_path)

        for i, filename in enumerate(current.get('f', [])):
            file_path = join_snapshot_path(current_path, filename, snapshot_os=snapshot_os)

            if _in_any_group(groups_dict, filter_out, 'f', file_path):
                continue

            should_include = True
            if filter_in and not inherited_include:
                should_include = _in_any_group(groups_dict, filter_in, 'f', file_path)

            if not should_include:
                continue
            if local_only and not _is_fully_local(current, i):
                continue

            size = current['s'][i] if i < len(current.get('s', [])) else 0
            total_size += size
            total_count += 1
            if files is not None:
                files.append({
                    'name': filename,
                    'full_path': file_path,
                    'size': size,
                    'created': current['t'][i][0] if i < len(current.get('t', [])) else None,
                })

        # Reversed so that popping the stack visits children in listed order.
        for dirname in reversed(current.get('d', [])):
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

    return total_size, total_count


def calculate_filtered_recursive_totals(path_obj, snapshot_filename, filter_in, filter_out,
                                        entry=None, local_only=False):
    entry = entry or _snapshot_view(snapshot_filename, copy_groups=True) or EMPTY_SNAPSHOT
    return filter_on_snapshot(
        path_obj, entry['data'], entry['index'],
        filter_in, filter_out, entry['groups'], None,
        local_only=local_only,
    )


def get_filtered_files(snapshot_filename, filter_in, filter_out):
    """Get files from a snapshot that match the filter criteria"""
    entry = _snapshot_view(snapshot_filename, copy_groups=True)
    if not entry or not entry['data']:
        return []

    snapshot_data = entry['data']
    index = entry['index']
    groups_dict = entry['groups']
    snapshot_os = snapshot_data[0].get('os_name')

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
    )
    return filtered_files
