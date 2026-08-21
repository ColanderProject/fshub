"""Shared scan logic for API and CLI usage.

A scan produces one directory record per directory (see
docs/design/incremental-snapshot.md section 6) and publishes them as the base
of a snapshot. Device and scan metadata belong to the manifest, never to the
first record.
"""

from datetime import datetime
import os
import platform
import string

from .config import get_config
from .scan_logs import MAX_REPORTED_ERRORS, ScanRunLog, scan_duration
from .snapshot import (
    CROSS_DEVICE,
    DENIED,
    ERROR,
    IdentityRegistry,
    InvalidSnapshot,
    JUNCTION,
    SKIPPED,
    SYMLINK,
    SnapshotWriter,
    TRAVERSED,
    build_scan_scope,
    load_snapshot,
)
from .snapshot.records import RECORD_FIELDS, new_record


# These values are returned in os.stat_result.st_file_attributes on Windows.
# Keep them here because Python does not expose the newer constants on every
# supported version.
_FILE_ATTRIBUTE_OFFLINE = 0x00001000
_FILE_ATTRIBUTE_PINNED = 0x00080000
_FILE_ATTRIBUTE_UNPINNED = 0x00100000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def get_cloud_state(stat_result):
    """Return a coarse Windows cloud-file state without another system call."""
    attributes = getattr(stat_result, 'st_file_attributes', 0)
    if attributes & (_FILE_ATTRIBUTE_OFFLINE |
                     _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
        return 'not_fully_local'
    if attributes & _FILE_ATTRIBUTE_PINNED:
        return 'pinned'
    if attributes & _FILE_ATTRIBUTE_UNPINNED:
        return 'evictable'
    return None


def _normalize_prefix(path):
    """Normalize a path for prefix comparisons (no symlink resolution)."""
    return os.path.normcase(os.path.abspath(path))


def _canonical_path(path):
    """Like _normalize_prefix, but resolves symlinks so aliases compare equal."""
    return os.path.normcase(os.path.realpath(path))


def is_related_path(path_a, path_b):
    """True when one path is the other, or contains it.

    Compares whole path components so ``/home/a`` and ``/home/ab`` are not
    considered related, and resolves symlinks so two aliases of one tree are
    not scanned concurrently. On Windows ``/`` means "every drive", so it is
    related to any path.
    """
    if platform.system() == 'Windows' and '/' in (path_a, path_b):
        return True

    a = _canonical_path(path_a).rstrip(os.sep)
    b = _canonical_path(path_b).rstrip(os.sep)
    if a == b:
        return True
    return a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _init_counters(counters, current_path):
    """Initialise shared counters once per scan run."""
    counters.setdefault('scanned_count', 0)
    counters.setdefault('scanned_size', 0)
    counters.setdefault('errors', [])
    counters.setdefault('error_count', len(counters['errors']))
    counters['current_path'] = current_path


def _normalize_skip_prefixes(skip_prefixes):
    """Normalize configured skip prefixes for local matching."""
    return [_normalize_prefix(path).rstrip(os.sep) or os.sep
            for path in skip_prefixes if path]


def _should_skip_path(path, normalized_skip_prefixes):
    """Return True when a path is, or lives under, a configured skip prefix.

    Whole path components are compared, mirroring is_related_path: skipping
    ``/home/a`` must not also skip the unrelated sibling ``/home/ab``.
    """
    normalized_path = _normalize_prefix(path)
    for prefix in normalized_skip_prefixes:
        if normalized_path == prefix:
            return True
        if normalized_path.startswith(prefix.rstrip(os.sep) + os.sep):
            return True
    return False


def _record_error(counters, message, error_callback=None):
    """Count every error but keep only a bounded sample in task status."""
    counters['error_count'] += 1
    if len(counters['errors']) < MAX_REPORTED_ERRORS:
        counters['errors'].append(message)
    if error_callback:
        error_callback(message, counters)


def _timestamps(stat_result):
    return [int(stat_result.st_ctime), int(stat_result.st_mtime),
            int(stat_result.st_atime)]


def _is_junction(entry, stat_result):
    """Windows junctions are reparse points but not symlinks."""
    if platform.system() != 'Windows':
        return False
    tag = getattr(stat_result, 'st_reparse_tag', None)
    if tag is not None:
        return tag == _IO_REPARSE_TAG_MOUNT_POINT
    attributes = getattr(stat_result, 'st_file_attributes', 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) and not entry.is_symlink()


class TreeScanner:
    """Walks a tree and produces one record per directory.

    An explicit stack replaces os.walk because the traversal state of every
    subdirectory has to be recorded in its parent: os.walk can only report
    that it skipped something, not why, and the difference between "excluded
    by configuration" and "permission denied" is the difference between a
    complete snapshot and an incomplete one.
    """

    def __init__(self, counters, skip_prefixes=None, cross_filesystems=True,
                 result_callback=None, error_callback=None, os_name=None,
                 identity_providers=None):
        self.counters = counters
        self.skip_prefixes = _normalize_skip_prefixes(skip_prefixes or [])
        self.cross_filesystems = cross_filesystems
        self.result_callback = result_callback
        self.error_callback = error_callback
        self.os_name = os_name or platform.system()
        if identity_providers is None:
            self.identities = IdentityRegistry(self.os_name)
        else:
            self.identities = IdentityRegistry.from_providers(identity_providers,
                                                              self.os_name)
        self.records = []
        self.sources = []
        self._devices = {}
        self.denied_count = 0
        self.error_count = 0

    # -- results ----------------------------------------------------------

    def result(self):
        return {
            'records': self.records,
            'identity_providers': self.identities.providers(),
            'sources': self.sources,
            'denied_count': self.denied_count,
            'error_count': self.error_count,
        }

    def _error(self, message):
        _record_error(self.counters, message, self.error_callback)

    def _note_source(self, path, stat_result):
        """Remember every distinct filesystem the scan touched.

        st_dev only distinguishes filesystems within this one scan; it is not
        a stable source identity, so no source_id is claimed here.
        """
        device = getattr(stat_result, 'st_dev', None)
        if device is None or device in self._devices:
            return
        self._devices[device] = path
        self.sources.append({
            'path': path,
            'source_id': None,
            'kind': 'filesystem' if len(self._devices) == 1 else 'mount',
        })

    # -- traversal --------------------------------------------------------

    def scan(self, root_path):
        """Scan one tree, or every drive when root_path is the Windows root."""
        _init_counters(self.counters, root_path)
        if self.os_name == 'Windows' and root_path == '/':
            self._scan_windows_root()
        else:
            self._walk(root_path, None, None)
        return self.result()

    def _scan_windows_root(self):
        """Build the synthetic "This PC" record and scan each drive.

        The root is not a real directory: it has no parent to rescan when a
        drive appears or disappears, so it is simply re-enumerated.
        """
        record = new_record('/')
        self.records.append(record)

        drives = [f'{letter}:\\' for letter in string.ascii_uppercase
                  if os.path.exists(f'{letter}:\\')]
        pending = []
        for drive in drives:
            skipped = _should_skip_path(drive, self.skip_prefixes)
            try:
                stat_result = os.stat(drive)
            except OSError as error:
                self._error(f'Error accessing directory {drive}: {error}')
                self._append_dir(record, drive[:2], [0, 0, 0], None, ERROR)
                continue
            state = SKIPPED if skipped else TRAVERSED
            self._append_dir(record, drive[:2], _timestamps(stat_result),
                             self.identities.identity_for(stat_result), state)
            if state == TRAVERSED:
                pending.append((drive, record, len(record['d']) - 1))

        for drive, parent, index in pending:
            self._walk(drive, parent, index)

    def _walk(self, root_path, root_parent, root_index):
        stack = [(root_path, root_parent, root_index)]
        while stack:
            path, parent, index = stack.pop()
            children = self._scan_directory(path, parent, index)
            # Reversed so the natural listing order is preserved on the way
            # down; the loader re-sorts anyway, but a deterministic order
            # keeps repeated scans byte-identical.
            stack.extend(reversed(children))

    def _scan_directory(self, path, parent, index):
        """Record one directory and return its traversable children."""
        self.counters['current_path'] = path
        if self.result_callback:
            self.result_callback(self.counters)

        try:
            entries = self._read_entries(path)
        except PermissionError as error:
            self._fail_child(path, parent, index, DENIED, error)
            return []
        except OSError as error:
            self._fail_child(path, parent, index, ERROR, error)
            return []

        try:
            stat_result = os.stat(path)
        except OSError as error:
            self._fail_child(path, parent, index, ERROR, error)
            return []

        self._note_source(path, stat_result)
        record = new_record(path)
        record['i'] = self.identities.identity_for(stat_result)
        self.records.append(record)

        children = []
        for entry in entries:
            full_path = os.path.join(path, entry.name)
            if self._is_directory(entry, full_path):
                child_index = self._add_directory(record, entry, full_path,
                                                  stat_result)
                if child_index is not None:
                    children.append((full_path, record, child_index))
            else:
                self._add_file(record, entry, full_path)

        return children

    @staticmethod
    def _read_entries(path):
        with os.scandir(path) as scanner:
            return sorted(scanner, key=lambda entry: entry.name)

    def _fail_child(self, path, parent, index, state, error):
        """Mark an unreadable directory in its parent.

        The root has no parent to carry the state, and a snapshot rooted at
        an unreadable directory would be an empty tree rather than an
        incomplete one, so it fails the scan instead.
        """
        self._error(f'Error accessing directory {path}: {error}')
        if state == DENIED:
            self.denied_count += 1
        else:
            self.error_count += 1
        if parent is None:
            raise error
        parent['x'][index] = state

    def _is_directory(self, entry, full_path):
        try:
            return entry.is_dir()
        except OSError as error:
            self._error(f'Error accessing {full_path}: {error}')
            return False

    def _append_dir(self, record, name, timestamps, identity, state):
        record['d'].append(name)
        record['T'].append(timestamps)
        record['D'].append(identity)
        record['x'].append(state)

    def _add_directory(self, record, entry, full_path, parent_stat):
        """Add a subdirectory to its parent record, returning its index.

        None means "do not descend": the reason is already recorded in x.
        """
        try:
            stat_result = os.stat(full_path)
        except OSError as error:
            self._error(f'Error accessing directory {full_path}: {error}')
            self._append_dir(record, entry.name, [0, 0, 0], None, ERROR)
            return None

        state = TRAVERSED
        if entry.is_symlink():
            state = SYMLINK
        elif _is_junction(entry, stat_result):
            state = JUNCTION
        elif _should_skip_path(full_path, self.skip_prefixes):
            state = SKIPPED
        elif not self.cross_filesystems and \
                stat_result.st_dev != parent_stat.st_dev:
            state = CROSS_DEVICE

        self._append_dir(record, entry.name, _timestamps(stat_result),
                         self.identities.identity_for(stat_result), state)
        return len(record['d']) - 1 if state == TRAVERSED else None

    def _add_file(self, record, entry, full_path):
        if _should_skip_path(full_path, self.skip_prefixes):
            return
        try:
            stat_result = os.stat(full_path)
        except OSError as error:
            self._error(f'Error accessing file {full_path}: {error}')
            return

        record['f'].append(entry.name)
        record['s'].append(stat_result.st_size)
        record['t'].append(_timestamps(stat_result))
        cloud_state = get_cloud_state(stat_result)
        if cloud_state is not None:
            # The compact form is the only legal encoding for an all-null
            # directory, so the list is materialised lazily.
            if record['c'] is None:
                record['c'] = [None] * (len(record['f']) - 1)
            record['c'].append(cloud_state)
        elif record['c'] is not None:
            record['c'].append(None)

        self.counters['scanned_count'] += 1
        self.counters['scanned_size'] += stat_result.st_size
        if self.result_callback:
            self.result_callback(self.counters)


def scan(path, counters, result_callback=None, skip_prefixes=None,
         error_callback=None, cross_filesystems=True, identity_providers=None):
    """Scan a directory tree and return its directory records."""
    scanner = TreeScanner(counters, skip_prefixes=skip_prefixes,
                          cross_filesystems=cross_filesystems,
                          result_callback=result_callback,
                          error_callback=error_callback,
                          identity_providers=identity_providers)
    return scanner.scan(path)


def run_scan_to_snapshot(scan_path, counters=None, result_callback=None,
                         skip_prefixes=None, scan_id=None, run_log=None,
                         cross_filesystems=True, snapshot_id=None):
    """Run a full scan and publish it as the base of a new snapshot."""
    counters = counters if counters is not None else {}
    start_time = datetime.now()
    counters['skip_prefixes'] = list(skip_prefixes or [])
    _init_counters(counters, scan_path)

    if run_log is None:
        run_log = ScanRunLog(scan_id)
        run_log.started(scan_path, skip_paths=skip_prefixes)

    def report_progress(current_counters):
        run_log.progress(current_counters)
        if result_callback:
            result_callback(current_counters)

    try:
        scanner = TreeScanner(counters, skip_prefixes=skip_prefixes,
                              cross_filesystems=cross_filesystems,
                              result_callback=report_progress,
                              error_callback=run_log.scan_error)
        scan_result = scanner.scan(scan_path)
        traversal_finish_time = datetime.now()

        config = get_config()
        os.makedirs(config.snapshot_dir, exist_ok=True)
        writer = SnapshotWriter.create(config.snapshot_dir,
                                       snapshot_id=snapshot_id)
        with writer:
            tree = writer.publish_full_rescan(
                scan_result['records'],
                scan_scope=build_scan_scope(scan_path, skip_prefixes,
                                            cross_filesystems),
                os_name=platform.system(),
                sources=scan_result['sources'],
                identity_providers=scan_result['identity_providers'],
                start_scan_time=int(start_time.timestamp()),
                finish_scan_time=int(traversal_finish_time.timestamp()),
            )

        counters['current_path'] = scan_path
        finish_time = run_log.completed(counters, writer.layout.snapshot_id)
    except Exception as error:
        run_log.failed(error, counters)
        raise

    return {
        'scan_id': run_log.scan_id,
        'scan_log': run_log.path,
        'snapshot_id': writer.layout.snapshot_id,
        'snapshot_path': writer.layout.dir,
        'entry_count': len(tree['data']),
        'logical_state_digest': tree['digest'],
        'observation_coverage': tree['observation_coverage'],
        'counters': counters,
        'start_time': run_log.start_time,
        'finish_time': finish_time,
        'duration': scan_duration(run_log.start_time, finish_time),
    }


# The change-detection backend used by run_incremental_scan. It walks the whole
# scope again instead of subscribing to events, so it observes the final state
# directly and can never be behind a lost event; the price is a full metadata
# walk per generation. Watcher backends (USN, inotify) plug in here later and
# must supply real cursors.
RESCAN_BACKEND = 'rescan_diff'


def changed_records(new_records, previous_records):
    """Directories whose stored record would change (design doc 10).

    A deleted file, subdirectory or whole subtree shows up as a change of the
    parent record, which is exactly what the format expects: there are no
    delete or tombstone entries, only rewritten parents.
    """
    changed = []
    for record in new_records:
        previous = previous_records.get(record['p'])
        if previous is None or not _same_record(previous, record):
            changed.append(record)
    return changed


def _same_record(previous, current):
    """Compare the stored fields, ignoring atime.

    atime is recorded but never treated as a change: one `grep -r` touches
    the access time of every file it reads, and letting that mark thousands
    of directories dirty would make every increment as large as a full scan.
    The atime in a record may therefore be stale by design.
    """
    for field in RECORD_FIELDS:
        if field in ('t', 'T'):
            continue
        if previous[field] != current[field]:
            return False
    return all(
        previous[field][i][:2] == stamps[:2]
        for field in ('t', 'T')
        for i, stamps in enumerate(current[field])
    )


def run_incremental_scan(snapshot_id, counters=None, result_callback=None,
                         scan_id=None, run_log=None, adopt=False):
    """Rescan a snapshot's scope and commit only the directories that changed.

    The scope is taken from the manifest rather than from the caller: an
    increment that silently used a different root or a different skip list
    would describe a tree nobody ever observed.
    """
    counters = counters if counters is not None else {}
    start_time = datetime.now()

    config = get_config()
    writer = SnapshotWriter(os.path.join(config.snapshot_dir, snapshot_id))
    manifest = writer.current_manifest()
    if manifest is None:
        raise InvalidSnapshot(f'snapshot {snapshot_id} has no manifest')

    scope = manifest.scan_scope
    scan_path = scope['root_path']
    counters['skip_prefixes'] = list(scope['skip_prefixes_raw'])
    _init_counters(counters, scan_path)

    if run_log is None:
        run_log = ScanRunLog(scan_id)
        run_log.started(scan_path, skip_paths=scope['skip_prefixes_raw'])

    def report_progress(current_counters):
        run_log.progress(current_counters)
        if result_callback:
            result_callback(current_counters)

    try:
        scanner = TreeScanner(
            counters,
            skip_prefixes=scope['skip_prefixes_raw'],
            cross_filesystems=scope['cross_filesystems'],
            result_callback=report_progress,
            error_callback=run_log.scan_error,
            identity_providers=manifest.payload['identity_providers'],
        )
        scan_result = scanner.scan(scan_path)
        finish_scan = datetime.now()

        previous = load_snapshot(writer.layout.dir)
        previous_records = {record['p']: record for record in previous['data']}
        dirty = changed_records(scan_result['records'], previous_records)

        entry = None
        if dirty:
            with writer:
                entry = writer.commit_increment(
                    dirty,
                    change_backend=RESCAN_BACKEND,
                    event_continuity='complete',
                    start_scan_time=int(start_time.timestamp()),
                    finish_scan_time=int(finish_scan.timestamp()),
                    denied_count=scan_result['denied_count'],
                    error_count=scan_result['error_count'],
                    scan_scope=build_scan_scope(scan_path,
                                                scope['skip_prefixes_raw'],
                                                scope['cross_filesystems']),
                    previous_records=previous_records,
                    identity_providers=scan_result['identity_providers'],
                    sources=scan_result['sources'],
                    adopt=adopt,
                )

        counters['current_path'] = scan_path
        finish_time = run_log.completed(counters, snapshot_id)
    except Exception as error:
        run_log.failed(error, counters)
        raise

    return {
        'scan_id': run_log.scan_id,
        'scan_log': run_log.path,
        'snapshot_id': snapshot_id,
        'snapshot_path': writer.layout.dir,
        'changed_count': len(dirty),
        'entry_count': len(scan_result['records']),
        'snapshot_generation': entry['snapshot_generation'] if entry
                               else manifest.generation,
        'counters': counters,
        'start_time': run_log.start_time,
        'finish_time': finish_time,
        'duration': scan_duration(run_log.start_time, finish_time),
    }
