"""Publishing snapshots: base, increments, compaction, GC (design doc 13-15).

Every commit follows the same protocol: write payload files to uniquely named
temporaries, fsync them, replace them into place, then publish one immutable
manifest revision. Until that manifest exists nothing on disk is committed, so
a crash can only leave the previous generation or the new one visible.
"""

import json
import os
import time

from ..utils import join_snapshot_path
from .errors import InvalidSnapshot
from .layout import (
    SnapshotLayout,
    WriterLock,
    base_filename,
    commit,
    increment_filename,
    is_temp_name,
    new_snapshot_id,
    write_jsonl_gz,
)
from .loader import _read_overlay, rebuild
from .manifest import (
    FORMAT_VERSION,
    derive_consistency,
    fold_continuity,
    newest_valid_manifest,
    read_manifest,
    write_manifest,
)
from .producer import build_producer, check_producer
from .records import TRAVERSED, dump_record, sort_record, validate_record
from .scope import scope_equivalent

# How much of the "which generation went wrong" history survives compaction.
MAX_EVENT_HISTORY = 20

# Compaction thresholds (design doc 15.1).
MAX_INCREMENTS = 20
MAX_INCREMENT_SIZE_RATIO = 0.30
MAX_INCREMENT_RECORD_RATIO = 0.50
MAX_UNREACHABLE_RATIO = 0.30

# Nothing younger than this is ever collected: a temp file may belong to a
# writer that is still running, and a superseded file to a reader that is
# still streaming it.
MIN_GC_AGE_SECONDS = 3600
GC_KEEP_REVISIONS = 3


class ScopeChanged(InvalidSnapshot):
    """The scan scope no longer matches; an increment would be meaningless."""


def prepare_records(records):
    """Sort and validate a producer's records, keyed by path.

    Sorting happens before validation so that a producer never has to know
    the canonical order, and validation then rejects anything sort_record
    could not fix (a missing field, a bad timestamp, a duplicate path).
    """
    prepared = {}
    for record in records:
        sort_record(record)
        validate_record(record)
        if record['p'] in prepared:
            raise InvalidSnapshot(f'duplicate record for {record["p"]!r}')
        prepared[record['p']] = record
    return prepared


def assert_subtree_present(new_records, previous_records, os_name):
    """Producer-side check for design doc 10.2.

    Whenever a parent gains a directory entry, or a subdirectory is replaced
    by a new incarnation, that subdirectory's whole subtree must be part of
    the same increment. Without this a deleted subtree can become reachable
    again under a reused path, and nothing on the loading side would notice
    unless the identity provider happens to be strong.
    """
    for path, record in new_records.items():
        previous = previous_records.get(path)
        for k, name in enumerate(record['d']):
            if record['x'][k] != TRAVERSED:
                continue
            child_path = join_snapshot_path(path, name, snapshot_os=os_name)
            if child_path in new_records:
                continue

            if previous is None:
                raise InvalidSnapshot(
                    f'{path}: newly recorded directory {name!r} must be scanned '
                    f'in the same increment')
            try:
                old_index = previous['d'].index(name)
            except ValueError:
                raise InvalidSnapshot(
                    f'{path}: new subdirectory {name!r} must be scanned in the '
                    f'same increment') from None

            old_identity = previous['D'][old_index]
            new_identity = record['D'][k]
            if old_identity != new_identity and None not in (old_identity, new_identity):
                raise InvalidSnapshot(
                    f'{path}: subdirectory {name!r} has a new identity, so its '
                    f'subtree must be scanned in the same increment')
            if child_path not in previous_records:
                raise InvalidSnapshot(
                    f'{path}: subdirectory {name!r} is marked traversed but has '
                    f'no record in any generation')


def _extend_providers(existing, proposed):
    """Accept an append-only extension of the segment's provider table.

    Editing or reordering an entry would silently reinterpret every id that
    records already carry, so only appending is allowed (design doc 7.4).
    """
    if proposed is None:
        return existing
    if len(proposed) < len(existing) or \
            any(entry != proposed[i] for i, entry in enumerate(existing)):
        raise InvalidSnapshot(
            'identity provider table may only be appended to within a segment')
    return proposed


class SnapshotWriter:
    """Single-writer access to one snapshot directory."""

    def __init__(self, snapshot_dir):
        self.layout = SnapshotLayout(snapshot_dir)
        self.lock = WriterLock(self.layout.lock_path)

    @classmethod
    def create(cls, snapshots_root, snapshot_id=None, now=None):
        """Create an empty snapshot directory ready for its first base."""
        snapshot_id = snapshot_id or new_snapshot_id(now)
        layout = SnapshotLayout.create(snapshots_root, snapshot_id)
        return cls(layout.dir)

    def __enter__(self):
        self.lock.acquire()
        return self

    def __exit__(self, *exc_info):
        self.lock.release()
        return False

    # -- reading the current state ---------------------------------------

    def current_manifest(self):
        """The newest valid manifest, or None for a fresh directory."""
        if not self.layout.list_manifest_revisions():
            return None
        return newest_valid_manifest(self.layout)

    # -- publishing -------------------------------------------------------

    def publish_full_rescan(self, records, *, scan_scope, os_name, sources,
                            identity_providers, start_scan_time, finish_scan_time,
                            event_continuity='not_applicable', adopt=False,
                            created_at=None, reason='full_rescan'):
        """Write a new base and publish it as a new revision.

        A full rescan is the only thing that can clear a gap in the event
        chain, so it always advances the generation: it describes a freshly
        observed state of the source, not a re-encoding of an old one.
        """
        prepared = prepare_records(records)
        parent = self.current_manifest()
        if parent is not None:
            check_producer(parent, adopt=adopt)
            revision = parent.revision + 1
            generation = parent.generation + 1
            history = list(parent.payload.get('event_history', []))
        else:
            revision, generation, history = 0, 0, []

        providers = {provider['id']: provider for provider in identity_providers}
        tree = rebuild(prepared, os_name=os_name,
                       root_path=scan_scope['root_path'], providers=providers)

        created_at = int(time.time()) if created_at is None else int(created_at)
        base_entry = self._write_payload(
            base_filename(generation), tree['data'],
            snapshot_generation=generation, created_at=created_at,
            start_scan_time=start_scan_time, finish_scan_time=finish_scan_time,
            event_continuity=event_continuity)
        base_entry['kind'] = 'full_rescan'
        base_entry['logical_state_digest'] = tree['digest']
        base_entry['observation_coverage'] = tree['observation_coverage']

        payload = self._payload(
            revision=revision,
            parent_revision=None if parent is None else parent.revision,
            kind='full_rescan',
            generation=generation,
            os_name=os_name,
            scan_scope=scan_scope,
            sources=sources,
            identity_providers=identity_providers,
            base=base_entry,
            increments=[],
            current_digest=tree['digest'],
            observation_coverage=tree['observation_coverage'],
            history=history,
            event={'generation': generation, 'kind': 'full_rescan',
                   'reason': reason, 'at': created_at},
        )
        write_manifest(self.layout, payload)
        return tree

    def commit_increment(self, records, *, change_backend, event_continuity,
                         start_scan_time, finish_scan_time, begin_cursor=None,
                         end_cursor=None, denied_count=0, error_count=0,
                         scan_scope=None, previous_records=None,
                         identity_providers=None, sources=None,
                         current_digest=None, adopt=False, created_at=None):
        """Publish one increment on top of the current revision."""
        manifest = self.current_manifest()
        if manifest is None:
            raise InvalidSnapshot('cannot commit an increment before a base')
        check_producer(manifest, adopt=adopt)

        if scan_scope is not None and not scope_equivalent(
                manifest.scan_scope, scan_scope, manifest.os_name):
            raise ScopeChanged(
                'scan scope changed; a full rescan is required before the next '
                'increment')

        prepared = prepare_records(records)
        if previous_records is not None:
            assert_subtree_present(prepared, previous_records, manifest.os_name)

        providers = _extend_providers(manifest.payload['identity_providers'],
                                      identity_providers)

        generation = manifest.generation + 1
        created_at = int(time.time()) if created_at is None else int(created_at)
        entry = self._write_payload(
            increment_filename(generation, created_at), list(prepared.values()),
            snapshot_generation=generation, created_at=created_at,
            start_scan_time=start_scan_time, finish_scan_time=finish_scan_time,
            event_continuity=event_continuity)
        entry.update({
            'change_backend': change_backend,
            'begin_cursor': begin_cursor,
            'end_cursor': end_cursor,
            'denied_count': int(denied_count),
            'error_count': int(error_count),
        })

        increments = list(manifest.increments) + [entry]
        history = list(manifest.payload.get('event_history', []))
        payload = self._payload(
            revision=manifest.revision + 1,
            parent_revision=manifest.revision,
            kind='increment',
            generation=generation,
            os_name=manifest.os_name,
            scan_scope=manifest.scan_scope,
            sources=sources if sources is not None else manifest.sources,
            identity_providers=providers,
            base=manifest.base,
            increments=increments,
            # Never inherited: a stale digest would claim the tree is
            # unchanged when it is not (design doc 7.3).
            current_digest=current_digest,
            observation_coverage=None,
            history=history,
            event={'generation': generation, 'kind': 'increment',
                   'reason': event_continuity, 'at': created_at},
        )
        write_manifest(self.layout, payload)
        return entry

    def compact(self, created_at=None):
        """Fold base + increments into a new base, keeping the generation.

        Compaction changes the encoding, never the logical tree, so it must
        not reset either completeness axis: the folded values of the range it
        replaces are carried over verbatim.
        """
        manifest = self.current_manifest()
        if manifest is None:
            raise InvalidSnapshot('nothing to compact')
        if not manifest.increments:
            return None

        records = _read_overlay(self.layout, manifest)
        tree = rebuild(records, os_name=manifest.os_name,
                       root_path=manifest.root_path,
                       providers=manifest.providers)

        continuity = fold_continuity(manifest.base['event_continuity'],
                                     manifest.increments)
        created_at = int(time.time()) if created_at is None else int(created_at)
        generation = manifest.generation
        name = base_filename(generation)
        if os.path.exists(os.path.join(self.layout.dir, name)):
            # Only possible if a base for this generation is still published,
            # which means there is nothing to fold into it.
            raise InvalidSnapshot(f'base file {name} already exists')

        base_entry = self._write_payload(
            name, tree['data'],
            snapshot_generation=generation, created_at=created_at,
            start_scan_time=manifest.base['start_scan_time'],
            finish_scan_time=manifest.increments[-1]['finish_scan_time'],
            event_continuity=continuity)
        base_entry['kind'] = 'checkpoint_compaction'
        base_entry['logical_state_digest'] = tree['digest']
        base_entry['observation_coverage'] = tree['observation_coverage']

        superseded = manifest.files()
        history = list(manifest.payload.get('event_history', []))
        payload = self._payload(
            revision=manifest.revision + 1,
            parent_revision=manifest.revision,
            kind='compaction',
            generation=generation,
            os_name=manifest.os_name,
            scan_scope=manifest.scan_scope,
            sources=manifest.sources,
            # Provider ids stay as they are. Renumbering is allowed at a
            # segment boundary but would mean rewriting every identity in
            # every record for no gain.
            identity_providers=manifest.payload['identity_providers'],
            base=base_entry,
            increments=[],
            current_digest=tree['digest'],
            observation_coverage=tree['observation_coverage'],
            history=history,
            event={'generation': generation, 'kind': 'compaction',
                   'reason': continuity, 'at': created_at},
        )
        write_manifest(self.layout, payload)
        self._record_garbage(superseded)
        return tree

    # -- internals --------------------------------------------------------

    def _write_payload(self, relative_name, records, **fields):
        """Write one gzip JSONL payload file and describe it for the manifest."""
        path = self.layout.resolve(relative_name)
        size, sha256 = write_jsonl_gz(
            path, (dump_record(record) for record in records),
            must_not_exist=True)
        entry = {
            'file': relative_name,
            'record_count': len(records),
            'size': size,
            'sha256': sha256,
        }
        entry.update(fields)
        return entry

    def _payload(self, *, revision, parent_revision, kind, generation, os_name,
                 scan_scope, sources, identity_providers, base, increments,
                 current_digest, observation_coverage, history, event):
        history = (history + [event])[-MAX_EVENT_HISTORY:]
        continuity = fold_continuity(base['event_continuity'], increments)
        payload = {
            'format_version': FORMAT_VERSION,
            'snapshot_id': self.layout.snapshot_id,
            'manifest_revision': revision,
            'parent_manifest_revision': parent_revision,
            'kind': kind,
            'snapshot_generation': generation,
            'current_logical_state_digest': current_digest,
            'os_name': os_name,
            'producer': build_producer(),
            'scan_scope': scan_scope,
            'sources': sources,
            'identity_providers': identity_providers,
            'base': base,
            'increments': increments,
            'event_history': history,
        }
        if observation_coverage is not None:
            payload['consistency'] = derive_consistency(continuity,
                                                        observation_coverage)
        return payload

    # -- garbage collection ----------------------------------------------

    def _record_garbage(self, files):
        """Append superseded payload files to the retry log.

        gc.jsonl is a cache, not an authority: it can be rebuilt by diffing
        neighbouring revisions, so losing it costs disk space, not
        correctness.
        """
        if not files:
            return
        lines = ''.join(
            json.dumps({'file': name, 'at': int(time.time())},
                       ensure_ascii=True) + '\n'
            for name in files)
        with open(self.layout.gc_path, 'a', encoding='ascii') as handle:
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())

    def protected_files(self, keep_revisions=GC_KEEP_REVISIONS):
        """Files the current and the last few valid revisions still need.

        Keeping recent revisions alive is what gives a slow reader time to
        finish: it captured one of them and is still streaming its files.
        """
        protected = set()
        revisions = self.layout.list_manifest_revisions()
        for revision in reversed(revisions[-keep_revisions:] or revisions):
            try:
                manifest = read_manifest(self.layout, revision)
            except InvalidSnapshot:
                continue
            protected.update(manifest.files())
        return protected

    def collect_garbage(self, keep_revisions=GC_KEEP_REVISIONS,
                        min_age=MIN_GC_AGE_SECONDS, limit=64, now=None):
        """Delete a bounded batch of superseded files.

        Never "everything on disk minus the live set": a file this code does
        not recognise is a file some other version of it might depend on.
        Only files explicitly recorded as superseded are candidates.
        """
        now = time.time() if now is None else now
        protected = self.protected_files(keep_revisions)
        entries = self._read_garbage()

        remaining = []
        deleted = []
        for entry in entries:
            if len(deleted) >= limit:
                remaining.append(entry)
                continue
            name = entry.get('file')
            if not isinstance(name, str):
                continue
            if name in protected:
                # Still needed by a revision a slow reader may hold; try again
                # once that revision ages out.
                remaining.append(entry)
                continue
            if now - entry.get('at', 0) < min_age:
                remaining.append(entry)
                continue
            try:
                path = self.layout.resolve(name)
            except InvalidSnapshot:
                continue
            try:
                os.remove(path)
                deleted.append(name)
            except FileNotFoundError:
                deleted.append(name)
            except OSError:
                # Windows refuses to unlink a file a reader still has open;
                # keep it for the next batch instead of failing the run.
                remaining.append(entry)

        self._write_garbage(remaining)
        deleted += self._collect_temp_files(min_age, now)
        return deleted

    def _collect_temp_files(self, min_age, now):
        """Remove abandoned temporaries by naming rule plus a minimum age."""
        deleted = []
        for directory in (self.layout.dir, self.layout.manifest_dir,
                          self.layout.increment_dir):
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not is_temp_name(name):
                    continue
                path = os.path.join(directory, name)
                try:
                    if now - os.stat(path).st_mtime < min_age:
                        continue
                    os.remove(path)
                    deleted.append(name)
                except OSError:
                    continue
        return deleted

    def _read_garbage(self):
        try:
            with open(self.layout.gc_path, 'r', encoding='ascii') as handle:
                lines = handle.readlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def _write_garbage(self, entries):
        data = ''.join(json.dumps(entry, ensure_ascii=True) + '\n'
                       for entry in entries).encode('ascii')
        commit(self.layout.gc_path, data)


def should_compact(entry, manifest):
    """Whether a loaded snapshot has accumulated enough to be worth folding."""
    increments = manifest.increments
    if not increments:
        return False
    if len(increments) >= MAX_INCREMENTS:
        return True

    base = manifest.base
    inc_size = sum(inc['size'] for inc in increments)
    if base['size'] and inc_size > base['size'] * MAX_INCREMENT_SIZE_RATIO:
        return True

    inc_records = sum(inc['record_count'] for inc in increments)
    if base['record_count'] and \
            inc_records > base['record_count'] * MAX_INCREMENT_RECORD_RATIO:
        return True

    total = entry['record_count']
    return bool(total) and entry['unreachable_count'] > total * MAX_UNREACHABLE_RATIO


__all__ = [
    'MAX_INCREMENTS', 'MIN_GC_AGE_SECONDS', 'ScopeChanged', 'SnapshotWriter',
    'assert_subtree_present', 'prepare_records', 'should_compact',
]
