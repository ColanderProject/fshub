"""Loading a snapshot: overlay, rebuild, verify, total (design doc 11).

Loading is deliberately a pure function of one immutable manifest revision.
Nothing here reads "the latest" state twice: the revision is captured once,
and if a file it references disappears underneath us (GC after a compaction)
the whole load restarts from a freshly read manifest. Retrying a single file
could mix a new base with an old increment list and produce a tree that was
never committed.
"""

import hashlib
import os

from ..utils import join_snapshot_path
from .canonical import CanonicalJSONError, canonical_dumps, strict_loads
from .errors import InvalidSnapshot, SnapshotBusy
from .layout import JsonlGzReader, SnapshotLayout
from .manifest import (
    derive_consistency,
    latest_revision,
    read_manifest,
)
from .records import (
    COVERAGE_DEGRADING_STATES,
    TRAVERSED,
    digest_payload,
    validate_record,
)

MAX_LOAD_ATTEMPTS = 3


def load_snapshot(snapshot_dir, max_attempts=MAX_LOAD_ATTEMPTS):
    """Load a snapshot directory into a ready-to-publish in-memory entry."""
    layout = SnapshotLayout(snapshot_dir)
    for attempt in range(max_attempts):
        revision = latest_revision(layout)
        try:
            return _load_revision(layout, revision)
        except FileNotFoundError:
            # A concurrent compaction plus GC removed a file this revision
            # referenced. Start over from whatever manifest is current now.
            continue
    raise SnapshotBusy(
        f'snapshot {layout.snapshot_id} changed under the loader '
        f'{max_attempts} times'
    )


def _load_revision(layout, revision):
    manifest = read_manifest(layout, revision)
    records = _read_overlay(layout, manifest)
    tree = rebuild(records, os_name=manifest.os_name,
                   root_path=manifest.root_path, providers=manifest.providers)

    consistency = derive_consistency(manifest.event_continuity,
                                     tree['observation_coverage'])
    cached = manifest.payload.get('consistency')
    if cached is not None and cached != consistency:
        raise InvalidSnapshot(
            f'manifest consistency {cached!r} does not match the rebuilt value '
            f'{consistency!r}'
        )

    return {
        'snapshot_id': layout.snapshot_id,
        'data': tree['data'],
        'index': tree['index'],
        'groups': {},
        'os_name': manifest.os_name,
        'root_path': manifest.root_path,
        'snapshot_generation': manifest.generation,
        'manifest_revision': manifest.revision,
        'logical_state_digest': tree['digest'],
        'event_continuity': manifest.event_continuity,
        'observation_coverage': tree['observation_coverage'],
        'consistency': consistency,
        'scan_scope': manifest.scan_scope,
        'sources': manifest.sources,
        'current_logical_state_digest': manifest.current_digest,
        'record_count': len(records),
        'unreachable_count': len(records) - len(tree['data']),
        'increment_count': len(manifest.increments),
    }


# -- overlay ---------------------------------------------------------------


def _read_overlay(layout, manifest):
    """Apply base and every increment in manifest order, last write wins."""
    records = {}
    _apply_file(layout, manifest.base, records)
    for increment in manifest.increments:
        _apply_file(layout, increment, records)
    return records


def _apply_file(layout, entry, records):
    """Merge one payload file into the overlay map, verifying it as we go."""
    path = layout.resolve(entry['file'])
    reader = JsonlGzReader(path)
    seen_here = set()
    count = 0

    try:
        for line in reader:
            record = _decode_record(line, entry['file'])
            key = record['p']
            if key in seen_here:
                # Two records for one path inside one file have no defined
                # order: the file itself is broken, not merely superseded.
                raise InvalidSnapshot(
                    f'duplicate record for {key!r} in {entry["file"]}')
            seen_here.add(key)
            records[key] = record
            count += 1
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise
        raise InvalidSnapshot(f'cannot read {entry["file"]}: {error}') from error
    except EOFError as error:
        raise InvalidSnapshot(
            f'{entry["file"]} is truncated: {error}') from error

    if count != entry['record_count']:
        raise InvalidSnapshot(
            f'{entry["file"]} has {count} records, manifest says '
            f'{entry["record_count"]}')
    if reader.size != entry['size']:
        raise InvalidSnapshot(
            f'{entry["file"]} is {reader.size} bytes, manifest says {entry["size"]}')
    if reader.sha256 != entry['sha256']:
        raise InvalidSnapshot(f'{entry["file"]} fails its sha256 check')


def _decode_record(line, where):
    try:
        record = strict_loads(line.decode('utf-8'))
    except (UnicodeDecodeError, CanonicalJSONError) as error:
        raise InvalidSnapshot(f'invalid record in {where}: {error}') from error
    try:
        return validate_record(record, where=where)
    except CanonicalJSONError as error:
        raise InvalidSnapshot(str(error)) from error


# -- rebuild ---------------------------------------------------------------


def rebuild(records, *, os_name, root_path, providers):
    """Walk the overlay map from the root and build the published view.

    Records that are no longer reachable are not an error: they are what a
    deletion or a path change leaves behind, and compaction reclaims them.
    """
    snapshot_os = os_name

    data = []
    index = {}
    parents = []
    digest = hashlib.sha256()
    coverage_degraded = False

    # (path, parent index); popped depth-first so children keep listed order.
    pending = [(root_path, None)]
    while pending:
        path, parent_index = pending.pop()
        if path in index:
            raise InvalidSnapshot(
                f'directory reachable from two parents or cycle: {path}')

        record = records.get(path)
        if record is None:
            raise InvalidSnapshot(f'missing directory record: {path}')

        position = len(data)
        index[path] = position
        data.append(record)
        parents.append(parent_index)
        digest.update(canonical_dumps(_digest_payload(record, providers)) + b'\n')

        children = []
        for k in range(len(record['d'])):
            state = record['x'][k]
            if state in COVERAGE_DEGRADING_STATES:
                coverage_degraded = True
            if state != TRAVERSED:
                continue
            child_path = join_snapshot_path(path, record['d'][k],
                                            snapshot_os=snapshot_os)
            child = records.get(child_path)
            if child is None:
                raise InvalidSnapshot(
                    f'{path}: subdirectory {record["d"][k]!r} is marked traversed '
                    f'but its record is missing')
            _check_identity_closure(record, k, child, providers)
            children.append((child_path, position))

        pending.extend(reversed(children))

    _compute_totals(data, parents)

    return {
        'data': data,
        'index': index,
        'digest': f'sha256:{digest.hexdigest()}',
        'observation_coverage': 'partial' if coverage_degraded else 'complete',
    }


def _digest_payload(record, providers):
    try:
        return digest_payload(record, providers)
    except CanonicalJSONError as error:
        raise InvalidSnapshot(str(error)) from error


def _check_identity_closure(record, k, child, providers):
    """Catch a deleted subtree resurrected through a reused path (doc 8.4).

    This is defence in depth on top of cursor continuity, and only decidable
    when both sides name the same strong provider: across providers or across
    provider epochs identities are incomparable, not unequal.
    """
    parent_identity = record['D'][k]
    child_identity = child['i']
    if parent_identity is None or child_identity is None:
        return
    if parent_identity[0] != child_identity[0]:
        return

    provider = providers.get(parent_identity[0])
    if provider is None:
        raise InvalidSnapshot(f'unknown identity provider id: {parent_identity[0]}')
    if provider['strength'] != 'strong':
        return

    if parent_identity[1] != child_identity[1]:
        raise InvalidSnapshot(
            f'{record["p"]}: identity of subdirectory {record["d"][k]!r} '
            f'({parent_identity[1]!r}) does not match the record at '
            f'{child["p"]!r} ({child_identity[1]!r})')


def _compute_totals(data, parents):
    """Recursive size/count totals (design doc 11.5).

    Never stored: they are derived from the tree and would otherwise have to
    be kept correct in every increment. Iterating the pre-order list backwards
    visits every child before its parent, so one linear pass is enough and no
    recursion can overflow on a deep tree.
    """
    for record in data:
        sizes = record['s']
        states = record['c']
        record['S'] = sum(sizes)
        record['C'] = len(sizes)
        if states is None:
            record['LS'] = record['S']
            record['LC'] = record['C']
        else:
            local = [i for i, state in enumerate(states)
                     if state != 'not_fully_local']
            record['LS'] = sum(sizes[i] for i in local)
            record['LC'] = len(local)

    for position in range(len(data) - 1, -1, -1):
        parent_index = parents[position]
        if parent_index is None:
            continue
        child = data[position]
        parent = data[parent_index]
        parent['S'] += child['S']
        parent['C'] += child['C']
        parent['LS'] += child['LS']
        parent['LC'] += child['LC']


def can_skip_rebuild(entry, manifest):
    """Whether a loaded view may adopt a new revision without rebuilding.

    Only revisions that claim not to have changed the logical tree qualify,
    which in practice means compaction and metadata-only commits. Every
    condition has to hold: if the generation moved the tree really did
    change, and if either digest is missing the claim cannot be checked at
    all (design doc 14.2).
    """
    if entry.get('manifest_revision') == manifest.revision:
        return False
    if entry.get('snapshot_generation') != manifest.generation:
        return False

    digest = manifest.current_digest
    if digest is None or entry.get('current_logical_state_digest') is None:
        return False
    if digest != entry['current_logical_state_digest']:
        return False

    if entry.get('scan_scope') != manifest.scan_scope:
        return False
    if entry.get('sources') != manifest.sources:
        return False
    return (entry.get('event_continuity') == manifest.event_continuity
            and entry.get('observation_coverage')
            == manifest.base['observation_coverage'])


def adopt_revision(entry, manifest):
    """Return a copy of a loaded entry that points at a newer revision.

    A copy, not an in-place edit: another thread may be reading the entry
    right now, and it must keep seeing one coherent version.
    """
    updated = dict(entry)
    updated['manifest_revision'] = manifest.revision
    updated['increment_count'] = len(manifest.increments)
    return updated


def snapshot_dirs(snapshots_root):
    """Every directory under snapshots/ that looks like a snapshot."""
    try:
        entries = sorted(os.listdir(snapshots_root))
    except OSError:
        return []
    result = []
    for name in entries:
        path = os.path.join(snapshots_root, name)
        if os.path.isdir(os.path.join(path, 'manifests')):
            result.append(path)
    return result
