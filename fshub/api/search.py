"""Search API endpoints"""

from flask import Blueprint, request, jsonify

from ..utils import join_snapshot_path
from .explorer import loaded_snapshots, snapshots_lock, to_web_path

search_bp = Blueprint('search_bp', __name__)

DEFAULT_LIMIT = 1000
MAX_LIMIT = 10000


def _parse_query(query):
    """Split the query into (mode, term). Supports 'starts:' and 'ends:'."""
    if query.startswith('ends:'):
        return 'endswith', query[len('ends:'):]
    if query.startswith('starts:'):
        return 'startswith', query[len('starts:'):]
    return 'contains', query


def _matches(name, mode, term):
    lowered = name.lower()
    if mode == 'endswith':
        return lowered.endswith(term)
    if mode == 'startswith':
        return lowered.startswith(term)
    return term in lowered


@search_bp.route('/api/v1/search', methods=['POST'])
def search_files():
    """Search for files/folders by name across the requested snapshots"""
    data = request.get_json(silent=True) or {}
    query = data.get('query', '').lower()
    snapshot_files = data.get('snapshots', [])

    try:
        limit = int(data.get('limit', DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return jsonify({'error': 'limit must be an integer'}), 400
    limit = max(1, min(limit, MAX_LIMIT))

    if not query:
        return jsonify({'error': 'Query is required'}), 400

    # If no specific snapshots provided, search all loaded snapshots.
    # Grab the entries themselves under the lock: a concurrent
    # /api/v1/unload_snapshot must not make this request blow up.
    with snapshots_lock:
        if not snapshot_files:
            snapshot_files = list(loaded_snapshots.keys())

        unloaded = [name for name in snapshot_files if name not in loaded_snapshots]
        if unloaded:
            return jsonify({
                'error': f'The following snapshots are not loaded: {", ".join(unloaded)}'
            }), 400

        targets = [(name, loaded_snapshots[name]) for name in snapshot_files]

    mode, term = _parse_query(query)
    results = []
    truncated = False

    # Collect one extra result so "exactly `limit` matches" is not reported
    # as truncated.
    hard_stop = limit + 1

    for snapshot_filename, entry in targets:
        snapshot_data = entry['data']
        snapshot_os = snapshot_data[0].get('os_name') if snapshot_data else None

        for path_obj in snapshot_data:
            if truncated:
                break
            current_path = path_obj['p']
            web_path = to_web_path(current_path, snapshot_os)

            for i, filename in enumerate(path_obj.get('f', [])):
                if not _matches(filename, mode, term):
                    continue

                result = {
                    'type': 'file',
                    'name': filename,
                    'path': web_path,
                    'full_path': join_snapshot_path(current_path, filename, snapshot_os=snapshot_os),
                    'size': path_obj['s'][i] if i < len(path_obj.get('s', [])) else 0,
                    'snapshot': snapshot_filename
                }
                if i < len(path_obj.get('t', [])):
                    result['timestamps'] = path_obj['t'][i]
                results.append(result)

                if len(results) >= hard_stop:
                    truncated = True
                    break

            if truncated:
                break

            for dirname in path_obj.get('d', []):
                if not _matches(dirname, mode, term):
                    continue

                results.append({
                    'type': 'directory',
                    'name': dirname,
                    'path': web_path,
                    'full_path': join_snapshot_path(current_path, dirname, snapshot_os=snapshot_os),
                    'snapshot': snapshot_filename
                })

                if len(results) >= hard_stop:
                    truncated = True
                    break

        if truncated:
            break

    del results[limit:]

    return jsonify({
        'results': results,
        'count': len(results),
        'truncated': truncated,
        'limit': limit,
    })
