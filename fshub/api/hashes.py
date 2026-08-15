"""Hash management API endpoints for calculating file hashes"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os

from flask import Blueprint, jsonify

from . import json_body
from .explorer import get_filtered_files, loaded_snapshots

hash_bp = Blueprint('hash_bp', __name__)

ALLOWED_ALGORITHMS = {'md5', 'sha1', 'sha256', 'sha512', 'blake2b'}
MAX_WORKERS = 4
CHUNK_SIZE = 1024 * 1024

# One pool for the whole process. A pool per request would let a handful of
# concurrent callers start an unbounded number of readers and saturate the disks.
_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='fshub-hash')


def calculate_file_hash(file_path, algorithm='sha256'):
    """Calculate the hash of a file.

    Returns (hash_hex, None) on success or (None, error_message) on failure.
    """
    try:
        hash_func = hashlib.new(algorithm)
    except ValueError:
        return None, f'Unsupported algorithm: {algorithm}'

    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b''):
                hash_func.update(chunk)
    except OSError as e:
        return None, str(e)

    return hash_func.hexdigest(), None


def _hash_files(file_paths, algorithm):
    """Hash a list of paths on the shared, process-wide pool."""
    results = []
    errors = []

    def work(path):
        return path, calculate_file_hash(path, algorithm)

    for path, (file_hash, error) in _pool.map(work, file_paths):
        if error:
            errors.append({'file_path': path, 'error': error})
        else:
            results.append({
                'file_path': path,
                'hash': file_hash,
                'algorithm': algorithm,
                'size': _safe_size(path),
            })

    return results, errors


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _collect_files(data):
    """Resolve the request into a list of local file paths to hash."""
    snapshot_filename = data.get('snapshot_filename', '')
    if not snapshot_filename:
        return None, ('Snapshot filename is required', 400)

    if snapshot_filename not in loaded_snapshots:
        return None, (f'Snapshot not loaded: {snapshot_filename}', 400)

    files = get_filtered_files(
        snapshot_filename,
        data.get('filter_in', []),
        data.get('filter_out', []),
    )
    return [f['full_path'] for f in files], None


@hash_bp.route('/api/v1/hash/calculate', methods=['POST'])
def start_hash_calculation():
    """Calculate hashes for the files selected by the given group filters.

    Only files that exist on the machine running fshub can be hashed, so this
    is meant to be called on the device that owns the snapshot.
    """
    data = json_body()
    algorithm = data.get('algorithm', 'sha256')

    if algorithm not in ALLOWED_ALGORITHMS:
        return jsonify({'error': f'Unsupported algorithm: {algorithm}'}), 400

    file_paths, error = _collect_files(data)
    if error:
        message, status = error
        return jsonify({'error': message}), status

    if not file_paths:
        return jsonify({'error': 'No files to process with the given filters'}), 400

    results, errors = _hash_files(file_paths, algorithm)

    return jsonify({
        'success': True,
        'algorithm': algorithm,
        'files_processed': len(results),
        'files_failed': len(errors),
        'results': results,
        'errors': errors,
    })


@hash_bp.route('/api/v1/hash/duplicates', methods=['POST'])
def find_duplicates():
    """Find duplicate files by hashing the filtered file set.

    Files are grouped by size first so that only same-sized candidates are
    ever read from disk.
    """
    data = json_body()
    algorithm = data.get('algorithm', 'sha256')

    if algorithm not in ALLOWED_ALGORITHMS:
        return jsonify({'error': f'Unsupported algorithm: {algorithm}'}), 400

    file_paths, error = _collect_files(data)
    if error:
        message, status = error
        return jsonify({'error': message}), status

    # Group by size; only sizes shared by 2+ files can contain duplicates.
    # Zero-byte files are legitimate duplicates of each other, so only
    # entries whose size could not be read are skipped - and those are
    # reported, never silently dropped.
    by_size = {}
    errors = []
    for path in file_paths:
        size = _safe_size(path)
        if size is None:
            errors.append({'file_path': path, 'error': 'Could not read file size'})
            continue
        by_size.setdefault(size, []).append(path)

    candidates = [p for paths in by_size.values() if len(paths) > 1 for p in paths]
    if not candidates:
        return jsonify({
            'duplicates': [],
            'algorithm': algorithm,
            'files_compared': 0,
            'files_skipped': len(errors),
            'errors': errors,
        })

    results, hash_errors = _hash_files(candidates, algorithm)
    errors.extend(hash_errors)

    by_hash = {}
    for item in results:
        by_hash.setdefault(item['hash'], []).append(item)

    duplicates = [
        {
            'hash': file_hash,
            'size': items[0]['size'],
            'count': len(items),
            'files': [item['file_path'] for item in items],
        }
        for file_hash, items in by_hash.items()
        if len(items) > 1
    ]
    duplicates.sort(key=lambda d: (d['size'] or 0) * d['count'], reverse=True)

    return jsonify({
        'duplicates': duplicates,
        'algorithm': algorithm,
        'files_compared': len(candidates),
        'files_skipped': len(errors),
        'errors': errors,
    })
