"""Tests for the incremental snapshot store (docs/design/incremental-snapshot.md).

The section numbers in the docstrings refer to that document.
"""

import gzip
import json
import os

import pytest

from fshub.scanning import run_incremental_scan, run_scan_to_snapshot
from fshub.snapshot import (
    DENIED,
    InvalidSnapshot,
    SYMLINK,
    ScopeChanged,
    SnapshotLayout,
    SnapshotWriter,
    TRAVERSED,
    build_scan_scope,
    load_snapshot,
    scope_equivalent,
)
from fshub.snapshot.canonical import CanonicalJSONError, canonical_dumps, strict_loads
from fshub.snapshot.identity import IdentityRegistry
from fshub.snapshot.loader import can_skip_rebuild
from fshub.snapshot.manifest import (
    decode_manifest,
    encode_manifest,
    fold_continuity,
    newest_valid_manifest,
    read_manifest,
)
from fshub.snapshot.producer import ProducerMismatch
from fshub.snapshot.records import validate_record
from fshub.snapshot.scope import scope_fold

from conftest import make_record, write_snapshot

STRONG_PROVIDERS = [
    {'id': 0, 'scheme': 'none', 'strength': 'none'},
    {'id': 1, 'scheme': 'test.handle', 'strength': 'strong'},
]
WEAK_PROVIDERS = [
    {'id': 0, 'scheme': 'none', 'strength': 'none'},
    {'id': 1, 'scheme': 'posix.devino', 'strength': 'weak'},
]


def _layout(config, snapshot_id):
    return SnapshotLayout(os.path.join(config.snapshot_dir, snapshot_id))


def _writer(config, snapshot_id):
    return SnapshotWriter(os.path.join(config.snapshot_dir, snapshot_id))


def _commit(writer, records, **kwargs):
    kwargs.setdefault('change_backend', 'test')
    kwargs.setdefault('event_continuity', 'complete')
    kwargs.setdefault('start_scan_time', 1700000100)
    kwargs.setdefault('finish_scan_time', 1700000101)
    with writer:
        return writer.commit_increment(records, **kwargs)


# -- basics (section 18.1-5) ----------------------------------------------


def test_base_only_snapshot_loads(config):
    snapshot = write_snapshot(config, [
        make_record('/root', files=[('a.txt', 10)], dirs=['sub']),
        make_record('/root/sub', files=[('b.txt', 20)]),
    ])
    entry = load_snapshot(_layout(config, snapshot).dir)

    assert entry['snapshot_generation'] == 0
    assert entry['data'][0]['S'] == 30
    assert sorted(entry['index']) == ['/root', '/root/sub']


def test_increments_override_the_same_path(config):
    snapshot = write_snapshot(config, [
        make_record('/root', files=[('a.txt', 10)], dirs=['sub']),
        make_record('/root/sub', files=[('b.txt', 20)]),
    ])
    writer = _writer(config, snapshot)

    _commit(writer, [make_record('/root/sub', files=[('b.txt', 99)])])
    _commit(writer, [make_record('/root/sub', files=[('b.txt', 7), ('c.txt', 1)])])

    entry = load_snapshot(writer.layout.dir)
    assert entry['snapshot_generation'] == 2
    assert entry['data'][entry['index']['/root/sub']]['s'] == [7, 1]
    assert entry['data'][0]['S'] == 18


def test_deleted_subtree_leaves_the_reachable_tree(config):
    """Deletions are expressed by rewriting the parent, not by tombstones."""
    snapshot = write_snapshot(config, [
        make_record('/root', dirs=['keep', 'gone']),
        make_record('/root/gone', files=[('x', 5)], dirs=['deeper']),
        make_record('/root/gone/deeper', files=[('y', 5)]),
        make_record('/root/keep', files=[('z', 1)]),
    ])
    writer = _writer(config, snapshot)
    _commit(writer, [make_record('/root', dirs=['keep'])])

    entry = load_snapshot(writer.layout.dir)
    assert sorted(entry['index']) == ['/root', '/root/keep']
    assert entry['data'][0]['S'] == 1
    # The superseded records are still on disk; they are simply unreachable.
    assert entry['unreachable_count'] == 2


def test_traversed_child_without_a_record_is_rejected(config):
    """Section 18.6: a missing subtree is a loud failure, not a small tree."""
    # The writer runs the same rebuild, so the broken increment is refused
    # before it can ever be published.
    with pytest.raises(InvalidSnapshot, match='marked traversed'):
        write_snapshot(config, [make_record('/root', dirs=['sub'])])


def test_backslash_in_a_linux_name_is_not_a_separator(config):
    """Section 18.8: on POSIX a backslash is an ordinary file name character."""
    snapshot = write_snapshot(config, [
        make_record('/root', dirs=['a\\b']),
        make_record('/root/a\\b', files=[('f', 3)]),
    ])
    entry = load_snapshot(_layout(config, snapshot).dir)
    assert '/root/a\\b' in entry['index']


# -- traversal states and identity (section 18.15-22) ---------------------


def test_non_traversed_states_do_not_require_child_records(config):
    snapshot = write_snapshot(config, [
        make_record('/root', dirs=[('link', SYMLINK, None), ('secret', DENIED, None)]),
    ])
    entry = load_snapshot(_layout(config, snapshot).dir)

    # A symlink is a declared boundary; a denied directory is not.
    assert entry['observation_coverage'] == 'partial'
    assert entry['consistency'] == 'partial'


def test_denied_directory_recovers_without_a_full_rescan(config):
    """Section 18.25: coverage is a function of the current tree only."""
    snapshot = write_snapshot(config, [
        make_record('/root', dirs=[('secret', DENIED, None)]),
    ])
    writer = _writer(config, snapshot)
    _commit(writer, [
        make_record('/root', dirs=['secret']),
        make_record('/root/secret', files=[('a', 1)]),
    ])

    entry = load_snapshot(writer.layout.dir)
    assert entry['observation_coverage'] == 'complete'
    assert entry['event_continuity'] == 'not_applicable'


def test_deleting_a_denied_directory_restores_coverage(config):
    """Section 18.26: a denied directory that is gone can no longer hide data."""
    snapshot = write_snapshot(config, [
        make_record('/root', dirs=[('secret', DENIED, None)]),
    ])
    writer = _writer(config, snapshot)
    _commit(writer, [make_record('/root')])

    assert load_snapshot(writer.layout.dir)['observation_coverage'] == 'complete'


def test_unknown_traversal_state_is_rejected():
    """Section 18.22: an unknown state must not be read as "not traversed"."""
    record = make_record('/root', dirs=['sub'])
    record['x'] = [42]
    with pytest.raises(CanonicalJSONError, match='unknown traversal state'):
        validate_record(record)


def test_strong_identity_mismatch_is_rejected(config):
    """Section 8.4: a resurrected subtree under a reused path is caught."""
    records = [
        make_record('/root', dirs=[('sub', TRAVERSED, [1, 'handle-A'])],
                    identity=[1, 'handle-root']),
        make_record('/root/sub', files=[('a', 1)], identity=[1, 'handle-B']),
    ]
    with pytest.raises(InvalidSnapshot, match='identity of subdirectory'):
        write_snapshot(config, records, providers=STRONG_PROVIDERS)


def test_weak_identity_mismatch_is_not_decidable(config):
    """Section 18.18: only a strong provider licenses the closure check."""
    records = [
        make_record('/root', dirs=[('sub', TRAVERSED, [1, '41:1'])],
                    identity=[1, '41:9']),
        make_record('/root/sub', files=[('a', 1)], identity=[1, '41:2']),
    ]
    snapshot = write_snapshot(config, records, providers=WEAK_PROVIDERS)
    assert load_snapshot(_layout(config, snapshot).dir)['record_count'] == 2


def test_unknown_provider_id_is_rejected(config):
    """Section 18.19: an id with no table entry cannot be interpreted."""
    records = [make_record('/root', identity=[7, 'x'])]
    with pytest.raises(InvalidSnapshot, match='unknown identity provider'):
        write_snapshot(config, records, providers=WEAK_PROVIDERS)


def test_identity_provider_table_is_append_only(config):
    snapshot = write_snapshot(config, [make_record('/root')],
                              providers=WEAK_PROVIDERS)
    writer = _writer(config, snapshot)
    rewritten = [dict(WEAK_PROVIDERS[0]),
                 {'id': 1, 'scheme': 'other.scheme', 'strength': 'strong'}]

    with pytest.raises(InvalidSnapshot, match='append'):
        _commit(writer, [make_record('/root')], identity_providers=rewritten)


def test_new_provider_epoch_appends_an_id():
    """Section 18.20: a changed epoch gets a new id, never a new meaning."""
    registry = IdentityRegistry.from_providers(WEAK_PROVIDERS + [
        {'id': 2, 'scheme': 'posix.devino', 'strength': 'weak',
         'source': {'device': 41}},
    ], os_name='Linux')

    class Stat:
        st_dev, st_ino = 99, 5

    identity = registry.identity_for(Stat())
    assert identity[0] == 3
    assert registry.extends(WEAK_PROVIDERS)


# -- completeness (section 18.23-29) --------------------------------------


def test_a_gap_survives_later_healthy_increments(config):
    """Section 18.23: continuity folds over the chain, coverage does not."""
    snapshot = write_snapshot(config, [make_record('/root', files=[('a', 1)])])
    writer = _writer(config, snapshot)

    _commit(writer, [make_record('/root', files=[('a', 2)])],
            event_continuity='gap')
    _commit(writer, [make_record('/root', files=[('a', 3)])],
            event_continuity='complete')

    entry = load_snapshot(writer.layout.dir)
    assert entry['event_continuity'] == 'gap'
    assert entry['observation_coverage'] == 'complete'
    assert entry['consistency'] == 'stale_or_unknown'


def test_compaction_cannot_repair_a_gap(config):
    """Section 18.24: folding the encoding does not recover lost events."""
    snapshot = write_snapshot(config, [make_record('/root', files=[('a', 1)])])
    writer = _writer(config, snapshot)
    _commit(writer, [make_record('/root', files=[('a', 2)])],
            event_continuity='gap')

    with writer:
        writer.compact()

    entry = load_snapshot(writer.layout.dir)
    assert entry['event_continuity'] == 'gap'
    assert read_manifest(writer.layout, entry['manifest_revision']) \
        .base['event_continuity'] == 'gap'


def test_full_rescan_clears_a_gap(config, sample_tree):
    """Section 18.23: only a fresh full scan restores continuity."""
    snapshot = run_scan_to_snapshot(str(sample_tree))['snapshot_id']
    writer = _writer(config, snapshot)
    previous = load_snapshot(writer.layout.dir)
    _commit(writer, [make_record(str(sample_tree), files=[('a.txt', 10)],
                                dirs=['sub'])],
            event_continuity='gap',
            previous_records={r['p']: r for r in previous['data']})
    assert load_snapshot(writer.layout.dir)['event_continuity'] == 'gap'

    scanner = run_scan_to_snapshot(str(sample_tree), snapshot_id=None)
    assert load_snapshot(_layout(config, scanner['snapshot_id']).dir) \
        ['event_continuity'] == 'not_applicable'


def test_cursor_continuity_folding():
    """Section 12.1: the fold is over the whole chain, not the last value."""
    increments = [{'event_continuity': 'complete'}, {'event_continuity': 'gap'},
                  {'event_continuity': 'complete'}]
    assert fold_continuity('complete', increments) == 'gap'
    assert fold_continuity('complete', increments[:1]) == 'complete'


def test_atime_only_change_produces_no_increment(config, sample_tree):
    """Section 18.29: one `grep -r` must not rewrite the whole snapshot."""
    snapshot = run_scan_to_snapshot(str(sample_tree))['snapshot_id']
    target = sample_tree / 'a.txt'
    stat = os.stat(target)
    os.utime(target, ns=(stat.st_atime_ns + 10 ** 9, stat.st_mtime_ns))

    result = run_incremental_scan(snapshot)
    assert result['changed_count'] == 0
    assert result['snapshot_generation'] == 0


def test_content_change_produces_one_increment(config, sample_tree):
    snapshot = run_scan_to_snapshot(str(sample_tree))['snapshot_id']
    (sample_tree / 'sub' / 'b.txt').write_bytes(b'b' * 40)

    result = run_incremental_scan(snapshot)
    assert result['changed_count'] == 1

    entry = load_snapshot(_layout(config, snapshot).dir)
    assert entry['snapshot_generation'] == 1
    assert entry['data'][0]['S'] == 10 + 40 + 30


# -- scope, producer and identity of a snapshot (section 18.30-36) --------


def test_skip_prefix_spelling_does_not_change_the_scope():
    """Section 18.30: order and trailing separators are not meaning."""
    a = build_scan_scope('/root', ['/mnt', '/proc'])
    b = build_scan_scope('/root', ['/proc/', '/mnt'])
    assert scope_equivalent(a, b, 'Linux')


def test_scope_case_rules_follow_the_snapshot_os():
    """Section 18.32-33: the snapshot's OS decides, never the loader's."""
    assert scope_fold('/MNT', 'Linux') != scope_fold('/mnt', 'Linux')
    assert scope_fold('C:\\Data', 'Windows') == scope_fold('c:/data/', 'Windows')


def test_changed_scope_blocks_an_increment(config):
    """Section 18.31: a different scope needs a new base, not an increment."""
    snapshot = write_snapshot(config, [make_record('/root')], skip_prefixes=['/root/a'])
    writer = _writer(config, snapshot)

    with pytest.raises(ScopeChanged):
        _commit(writer, [make_record('/root')],
                scan_scope=build_scan_scope('/root', ['/root/b']))


def test_a_foreign_producer_may_not_extend_the_chain(config, monkeypatch):
    """Section 18.34: a copied data directory must not continue a chain."""
    snapshot = write_snapshot(config, [make_record('/root')])
    monkeypatch.setattr('fshub.snapshot.producer.get_producer_id',
                        lambda: 'another-installation')

    with pytest.raises(ProducerMismatch):
        _commit(_writer(config, snapshot), [make_record('/root')])


def test_full_rescan_advances_the_generation(config):
    snapshot = write_snapshot(config, [make_record('/root')])
    writer = _writer(config, snapshot)
    with writer:
        writer.publish_full_rescan(
            [make_record('/root', files=[('a', 1)])],
            scan_scope=build_scan_scope('/root', []),
            os_name='Linux', sources=[],
            identity_providers=WEAK_PROVIDERS,
            start_scan_time=1, finish_scan_time=2)

    entry = load_snapshot(writer.layout.dir)
    assert entry['snapshot_generation'] == 1
    assert entry['manifest_revision'] == 1
    assert entry['increment_count'] == 0


# -- commit protocol, concurrency and GC (section 18.38-45) ---------------


def test_uncommitted_files_are_ignored(config, sample_tree):
    """Section 18.10: only files a manifest references are committed."""
    snapshot = run_scan_to_snapshot(str(sample_tree))['snapshot_id']
    layout = _layout(config, snapshot)
    orphan = os.path.join(layout.increment_dir, 'inc_gen_000009_1700000000.jsonl.gz')
    with gzip.open(orphan, 'wb') as handle:
        handle.write(b'{"garbage": true}\n')
    with open(os.path.join(layout.dir, 'base_gen_000000.jsonl.gz.1-'
                           + '0' * 32 + '.tmp'), 'wb') as handle:
        handle.write(b'not gzip')

    entry = load_snapshot(layout.dir)
    assert entry['snapshot_generation'] == 0


def test_a_manifest_revision_is_never_rewritten(config):
    """Section 18.41: revisions are immutable, so publishing twice fails."""
    snapshot = write_snapshot(config, [make_record('/root')])
    layout = _layout(config, snapshot)
    manifest = read_manifest(layout, 0)

    payload = dict(manifest.payload)
    with pytest.raises(InvalidSnapshot, match='refusing to overwrite'):
        from fshub.snapshot.manifest import write_manifest

        write_manifest(layout, payload)


def test_a_lost_current_pointer_falls_back_to_listing(config):
    """The pointer file is an accelerator; losing it is not an error."""
    snapshot = write_snapshot(config, [make_record('/root')])
    writer = _writer(config, snapshot)
    _commit(writer, [make_record('/root', files=[('a', 1)])])
    os.remove(writer.layout.current_path)

    assert load_snapshot(writer.layout.dir)['manifest_revision'] == 1


def test_gc_keeps_files_recent_revisions_still_need(config):
    """Section 18.45: only superseded files outside the protected set go."""
    snapshot = write_snapshot(config, [make_record('/root', files=[('a', 1)])])
    writer = _writer(config, snapshot)
    _commit(writer, [make_record('/root', files=[('a', 2)])])
    with writer:
        writer.compact()

    with writer:
        assert writer.collect_garbage(min_age=0, keep_revisions=3) == []
    with writer:
        deleted = writer.collect_garbage(min_age=0, keep_revisions=1)

    assert 'base_gen_000000.jsonl.gz' in deleted
    assert load_snapshot(writer.layout.dir)['data'][0]['s'] == [2]


def test_gc_respects_the_minimum_age(config):
    snapshot = write_snapshot(config, [make_record('/root', files=[('a', 1)])])
    writer = _writer(config, snapshot)
    _commit(writer, [make_record('/root', files=[('a', 2)])])
    with writer:
        writer.compact()
    with writer:
        assert writer.collect_garbage(min_age=3600, keep_revisions=1) == []


def test_the_writer_lock_excludes_a_second_writer(config):
    """Section 13.1: the lock is per snapshot and released with the process."""
    from fshub.snapshot.errors import WriterLocked

    snapshot = write_snapshot(config, [make_record('/root')])
    first = _writer(config, snapshot)
    second = _writer(config, snapshot)

    with first:
        with pytest.raises(WriterLocked):
            second.lock.acquire()
    # Releasing the first lock makes the snapshot writable again.
    with second:
        pass


# -- compaction and digest (section 18.14, 54-56) -------------------------


def test_compaction_preserves_the_tree_and_the_digest(config, sample_tree):
    """Section 18.14 and 18.21: compaction changes bytes, not meaning."""
    snapshot = run_scan_to_snapshot(str(sample_tree))['snapshot_id']
    (sample_tree / 'sub' / 'new.txt').write_bytes(b'n' * 4)
    run_incremental_scan(snapshot)

    writer = _writer(config, snapshot)
    before = load_snapshot(writer.layout.dir)
    with writer:
        writer.compact()
    after = load_snapshot(writer.layout.dir)

    assert after['logical_state_digest'] == before['logical_state_digest']
    assert after['index'] == before['index']
    assert [r['S'] for r in after['data']] == [r['S'] for r in before['data']]
    assert after['snapshot_generation'] == before['snapshot_generation']
    assert after['manifest_revision'] == before['manifest_revision'] + 1


def test_identical_content_scanned_twice_produces_identical_records(config):
    """Section 18.50: canonical ordering makes records reproducible."""
    records_a = [make_record('/root', files=[('b', 2), ('a', 1)], dirs=['y', 'x']),
                 make_record('/root/x'), make_record('/root/y')]
    records_b = [make_record('/root', files=[('a', 1), ('b', 2)], dirs=['x', 'y']),
                 make_record('/root/y'), make_record('/root/x')]

    first = write_snapshot(config, records_a)
    second = write_snapshot(config, records_b)

    def base_bytes(snapshot):
        path = os.path.join(_layout(config, snapshot).dir, 'base_gen_000000.jsonl.gz')
        with open(path, 'rb') as handle:
            return handle.read()

    assert base_bytes(first) == base_bytes(second)
    assert load_snapshot(_layout(config, first).dir)['logical_state_digest'] == \
        load_snapshot(_layout(config, second).dir)['logical_state_digest']


def test_compaction_lets_a_loaded_view_skip_the_rebuild(config):
    """Section 18.55: a generation-preserving revision needs no rebuild."""
    snapshot = write_snapshot(config, [make_record('/root', files=[('a', 1)])])
    writer = _writer(config, snapshot)
    _commit(writer, [make_record('/root', files=[('a', 2)])],
            current_digest=None)
    with writer:
        writer.compact()

    entry = load_snapshot(writer.layout.dir)
    manifest = newest_valid_manifest(writer.layout)
    assert can_skip_rebuild(entry, manifest) is False  # same revision

    stale = dict(entry)
    stale['manifest_revision'] = manifest.revision - 1
    assert can_skip_rebuild(stale, manifest) is True

    moved = dict(stale)
    moved['snapshot_generation'] = manifest.generation - 1
    assert can_skip_rebuild(moved, manifest) is False

    unknown = dict(stale)
    unknown['current_logical_state_digest'] = None
    assert can_skip_rebuild(unknown, manifest) is False


def test_an_increment_never_inherits_the_previous_digest(config):
    """Section 7.3: an unknown current digest must be written as null."""
    snapshot = write_snapshot(config, [make_record('/root', files=[('a', 1)])])
    writer = _writer(config, snapshot)
    _commit(writer, [make_record('/root', files=[('a', 2)])])

    manifest = newest_valid_manifest(writer.layout)
    assert manifest.current_digest is None
    assert manifest.base['logical_state_digest'].startswith('sha256:')


# -- format and serialisation (section 18.46-53) --------------------------


def test_undecodable_file_names_survive_the_round_trip(config, tmp_path):
    """Section 18.46: POSIX names are bytes, not guaranteed UTF-8."""
    tree = tmp_path / 'tree'
    tree.mkdir()
    raw_name = b'bad\xff.txt'
    with open(os.path.join(os.fsdecode(bytes(tree)), os.fsdecode(raw_name)), 'wb') as f:
        f.write(b'x' * 7)

    snapshot = run_scan_to_snapshot(str(tree))['snapshot_id']
    entry = load_snapshot(_layout(config, snapshot).dir)
    stored = entry['data'][0]['f'][0]

    assert os.fsencode(stored) == raw_name


def test_payload_lines_are_pure_ascii(config, tmp_path):
    """Section 18.47: surrogates must be escaped, never encoded."""
    tree = tmp_path / 'tree'
    tree.mkdir()
    with open(os.path.join(os.fsdecode(bytes(tree)), os.fsdecode(b'bad\xff.txt')), 'wb') as f:
        f.write(b'x')

    snapshot = run_scan_to_snapshot(str(tree))['snapshot_id']
    layout = _layout(config, snapshot)
    with gzip.open(os.path.join(layout.dir, 'base_gen_000000.jsonl.gz'), 'rb') as handle:
        payload = handle.read()
    with open(layout.manifest_path(0), 'rb') as handle:
        manifest = handle.read()

    payload.decode('ascii')
    manifest.decode('ascii')


def test_all_null_cloud_state_must_use_the_scalar_form():
    """Section 18.49: one logical value, one legal encoding."""
    record = make_record('/root', files=[('a', 1)])
    record['c'] = [None]
    with pytest.raises(CanonicalJSONError, match='all-null'):
        validate_record(record)

    empty = make_record('/root')
    empty['c'] = []
    with pytest.raises(CanonicalJSONError, match='must be null'):
        validate_record(empty)


def test_records_must_be_sorted_by_exact_name():
    record = make_record('/root', files=[('b', 1), ('a', 2)])
    record['f'], record['s'] = ['b', 'a'], [1, 2]
    with pytest.raises(CanonicalJSONError, match='not sorted'):
        validate_record(record)


def test_manifest_rejects_duplicate_keys_and_floats():
    """Section 18.48: the JSON defaults that silently accept these are unsafe."""
    with pytest.raises(CanonicalJSONError, match='duplicate object key'):
        strict_loads('{"snapshot_generation":10,"snapshot_generation":11}')
    with pytest.raises(CanonicalJSONError, match='floating point'):
        strict_loads('{"size":1.5}')
    with pytest.raises(CanonicalJSONError, match='invalid JSON constant'):
        strict_loads('{"size":NaN}')


def test_a_tampered_manifest_checksum_is_rejected(config):
    snapshot = write_snapshot(config, [make_record('/root')])
    layout = _layout(config, snapshot)
    document = strict_loads(open(layout.manifest_path(0), 'rb').read().decode())
    document['payload']['snapshot_generation'] = 5

    with pytest.raises(InvalidSnapshot, match='checksum mismatch'):
        decode_manifest(canonical_dumps(document), snapshot, revision=0)


def test_manifest_file_entries_may_not_escape_the_snapshot(config):
    """Section 18.53: a manifest is data, so its paths are not trusted."""
    snapshot = write_snapshot(config, [make_record('/root')])
    layout = _layout(config, snapshot)

    for bad in ('../../etc/passwd', '/etc/passwd', 'C:\\secrets'):
        with pytest.raises(InvalidSnapshot):
            layout.resolve(bad)


def test_record_count_and_checksum_are_verified(config):
    """Section 18.51: the manifest's own numbers must match the file."""
    snapshot = write_snapshot(config, [make_record('/root', files=[('a', 1)])])
    layout = _layout(config, snapshot)
    base = os.path.join(layout.dir, 'base_gen_000000.jsonl.gz')

    record = make_record('/root', files=[('a', 2)])
    with gzip.open(base, 'wb') as handle:
        handle.write(json.dumps(record, sort_keys=True).encode() + b'\n')

    with pytest.raises(InvalidSnapshot, match='sha256|bytes'):
        load_snapshot(layout.dir)


def test_duplicate_paths_inside_one_file_are_rejected(config):
    snapshot = write_snapshot(config, [make_record('/root')])
    writer = _writer(config, snapshot)
    with pytest.raises(InvalidSnapshot, match='duplicate record'):
        _commit(writer, [make_record('/root'), make_record('/root')])


def test_a_broken_generation_sequence_is_rejected(config):
    """Section 18.9: increments load in manifest order, never file order."""
    snapshot = write_snapshot(config, [make_record('/root')])
    layout = _layout(config, snapshot)
    manifest = read_manifest(layout, 0)
    payload = json.loads(json.dumps(manifest.payload))
    payload['snapshot_generation'] = 4

    with pytest.raises(InvalidSnapshot, match='does not match the last generation'):
        decode_manifest(encode_manifest(payload), snapshot, revision=0)


# -- producer-side invariants (section 10.2) ------------------------------


def test_a_new_directory_must_be_scanned_in_the_same_increment(config):
    """Section 10.2: otherwise a reused path can resurrect an old subtree."""
    snapshot = write_snapshot(config, [make_record('/root')])
    writer = _writer(config, snapshot)
    previous = {r['p']: r for r in load_snapshot(writer.layout.dir)['data']}

    with pytest.raises(InvalidSnapshot, match='same increment'):
        _commit(writer, [make_record('/root', dirs=['fresh'])],
                previous_records=previous)
