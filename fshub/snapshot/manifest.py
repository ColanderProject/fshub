"""Manifest revisions (design doc 7 and 12).

A manifest revision is the only authority on what a snapshot contains: which
base, which increments and in which order. Every revision file is immutable,
so a loader that captured revision N keeps a consistent view even while a
writer publishes N+1.
"""

import os

from .canonical import (
    CanonicalJSONError,
    canonical_dumps,
    check_ints,
    check_str,
    check_timestamp,
    check_uint,
    sha256_hex,
    strict_loads,
)
from .errors import InvalidSnapshot
from .layout import commit, manifest_filename, parse_base_generation, \
    parse_increment_generation, parse_manifest_revision

FORMAT_VERSION = 1

MANIFEST_KINDS = ('increment', 'compaction', 'full_rescan', 'metadata_only')
# kinds that must not move the source-of-truth version forward
GENERATION_PRESERVING_KINDS = ('compaction', 'metadata_only')

BASE_KINDS = ('full_rescan', 'checkpoint_compaction')

EVENT_CONTINUITY = ('complete', 'gap', 'not_applicable')
OBSERVATION_COVERAGE = ('complete', 'partial')
IDENTITY_STRENGTHS = ('strong', 'weak', 'none')

CONSISTENCY_COMPLETE = 'complete'
CONSISTENCY_PARTIAL = 'partial'
CONSISTENCY_STALE = 'stale_or_unknown'


def _require(condition, message):
    if not condition:
        raise InvalidSnapshot(message)


def _check_enum(value, allowed, what):
    _require(value in allowed, f'{what} must be one of {allowed}, got {value!r}')
    return value


def _check_bool(value, what):
    _require(isinstance(value, bool), f'{what} must be a boolean')
    return value


def _check_str_list(value, what):
    _require(isinstance(value, list) and all(isinstance(v, str) for v in value),
             f'{what} must be a list of strings')
    return value


def _get(mapping, key, what):
    _require(isinstance(mapping, dict), f'{what} must be an object')
    _require(key in mapping, f'{what} is missing {key!r}')
    return mapping[key]


def fold_continuity(base_continuity, increments):
    """Fold event continuity over the chain (design doc 12.1).

    A gap is sticky: later well-formed increments cannot restore knowledge of
    the events that were missed. Only a successful full rescan can.
    """
    continuity = base_continuity
    for increment in increments:
        if increment['event_continuity'] == 'gap':
            continuity = 'gap'
    return continuity


def derive_consistency(event_continuity, observation_coverage):
    """Collapse both axes into the single value shown in listings.

    Lossy on purpose; machine readers are expected to use both axes.
    """
    if event_continuity == 'gap':
        return CONSISTENCY_STALE
    if observation_coverage == 'partial':
        return CONSISTENCY_PARTIAL
    return CONSISTENCY_COMPLETE


class Manifest:
    """A validated manifest revision."""

    def __init__(self, payload, snapshot_id):
        self.payload = payload
        self.snapshot_id = snapshot_id

    # -- accessors --------------------------------------------------------

    @property
    def revision(self):
        return self.payload['manifest_revision']

    @property
    def generation(self):
        return self.payload['snapshot_generation']

    @property
    def kind(self):
        return self.payload['kind']

    @property
    def os_name(self):
        return self.payload['os_name']

    @property
    def base(self):
        return self.payload['base']

    @property
    def increments(self):
        return self.payload['increments']

    @property
    def scan_scope(self):
        return self.payload['scan_scope']

    @property
    def sources(self):
        return self.payload['sources']

    @property
    def root_path(self):
        return self.payload['scan_scope']['root_path']

    @property
    def current_digest(self):
        return self.payload['current_logical_state_digest']

    @property
    def providers(self):
        """Provider table of this revision, keyed by id.

        Records must always be resolved through the table of the revision
        they were loaded with; ids are only stable inside one segment.
        """
        return {provider['id']: provider
                for provider in self.payload['identity_providers']}

    @property
    def event_continuity(self):
        return fold_continuity(self.base['event_continuity'], self.increments)

    def files(self):
        """Every payload file this revision depends on, base first."""
        return [self.base['file']] + [inc['file'] for inc in self.increments]


def validate_payload(payload, snapshot_id, revision=None):
    """Validate a decoded manifest payload; returns a Manifest."""
    _require(isinstance(payload, dict), 'manifest payload must be an object')
    check_ints(payload, 'manifest')

    version = _get(payload, 'format_version', 'manifest')
    _require(version == FORMAT_VERSION,
             f'unsupported manifest format_version: {version!r}')

    _require(payload.get('snapshot_id') == snapshot_id,
             f'manifest snapshot_id {payload.get("snapshot_id")!r} does not '
             f'match directory {snapshot_id!r}')

    check_uint(_get(payload, 'manifest_revision', 'manifest'), 'manifest_revision')
    if revision is not None:
        _require(payload['manifest_revision'] == revision,
                 f'manifest revision {payload["manifest_revision"]} does not '
                 f'match its file name ({revision})')

    parent = _get(payload, 'parent_manifest_revision', 'manifest')
    if parent is None:
        _require(payload['manifest_revision'] == 0,
                 'only revision 0 may have a null parent_manifest_revision')
    else:
        check_uint(parent, 'parent_manifest_revision')
        _require(parent < payload['manifest_revision'],
                 'parent_manifest_revision must be lower than manifest_revision')

    _check_enum(_get(payload, 'kind', 'manifest'), MANIFEST_KINDS, 'manifest kind')
    check_uint(_get(payload, 'snapshot_generation', 'manifest'), 'snapshot_generation')

    digest = _get(payload, 'current_logical_state_digest', 'manifest')
    _require(digest is None or isinstance(digest, str),
             'current_logical_state_digest must be a string or null')

    check_str(_get(payload, 'os_name', 'manifest'), 'os_name')
    _validate_producer(_get(payload, 'producer', 'manifest'))
    _validate_scan_scope(_get(payload, 'scan_scope', 'manifest'))
    _validate_sources(_get(payload, 'sources', 'manifest'))
    _validate_providers(_get(payload, 'identity_providers', 'manifest'))

    base = _validate_base(_get(payload, 'base', 'manifest'))
    increments = _validate_increments(_get(payload, 'increments', 'manifest'), base)

    expected_generation = increments[-1]['snapshot_generation'] if increments \
        else base['snapshot_generation']
    _require(payload['snapshot_generation'] == expected_generation,
             f'manifest snapshot_generation {payload["snapshot_generation"]} '
             f'does not match the last generation on disk ({expected_generation})')

    if payload['kind'] == 'full_rescan':
        _require(base['kind'] == 'full_rescan' and not increments,
                 'a full_rescan revision must publish a full_rescan base and no increments')
    if payload['kind'] == 'compaction':
        _require(base['kind'] == 'checkpoint_compaction' and not increments,
                 'a compaction revision must publish a checkpoint base and no increments')

    _validate_cached_consistency(payload, base, increments)
    return Manifest(payload, snapshot_id)


def _validate_producer(producer):
    _require(isinstance(producer, dict), 'producer must be an object')
    check_str(_get(producer, 'producer_id', 'producer'), 'producer_id')
    device = producer.get('device')
    _require(device is None or isinstance(device, dict),
             'producer.device must be an object or null')


def _validate_scan_scope(scope):
    _require(isinstance(scope, dict), 'scan_scope must be an object')
    check_str(_get(scope, 'root_path', 'scan_scope'), 'scan_scope.root_path')
    _check_str_list(_get(scope, 'skip_prefixes_raw', 'scan_scope'),
                    'scan_scope.skip_prefixes_raw')
    _check_str_list(_get(scope, 'skip_prefixes_normalized', 'scan_scope'),
                    'scan_scope.skip_prefixes_normalized')
    _check_bool(_get(scope, 'cross_filesystems', 'scan_scope'),
                'scan_scope.cross_filesystems')


def _validate_sources(sources):
    _require(isinstance(sources, list), 'sources must be a list')
    for source in sources:
        check_str(_get(source, 'path', 'source'), 'source.path')
        source_id = _get(source, 'source_id', 'source')
        _require(source_id is None or isinstance(source_id, str),
                 'source.source_id must be a string or null')
        check_str(_get(source, 'kind', 'source'), 'source.kind')


def _validate_providers(providers):
    _require(isinstance(providers, list) and providers,
             'identity_providers must be a non-empty list')
    seen = set()
    for provider in providers:
        provider_id = check_uint(_get(provider, 'id', 'identity provider'),
                                 'identity provider id')
        _require(provider_id not in seen,
                 f'duplicate identity provider id: {provider_id}')
        seen.add(provider_id)
        check_str(_get(provider, 'scheme', 'identity provider'),
                  'identity provider scheme')
        _check_enum(_get(provider, 'strength', 'identity provider'),
                    IDENTITY_STRENGTHS, 'identity provider strength')


def _validate_payload_file(entry, what):
    """Fields shared by the base entry and every increment entry."""
    check_str(_get(entry, 'file', what), f'{what}.file')
    check_uint(_get(entry, 'snapshot_generation', what), f'{what}.snapshot_generation')
    check_timestamp(_get(entry, 'created_at', what), f'{what}.created_at')
    check_timestamp(_get(entry, 'start_scan_time', what), f'{what}.start_scan_time')
    check_timestamp(_get(entry, 'finish_scan_time', what), f'{what}.finish_scan_time')
    check_uint(_get(entry, 'record_count', what), f'{what}.record_count')
    check_uint(_get(entry, 'size', what), f'{what}.size')
    check_str(_get(entry, 'sha256', what), f'{what}.sha256')
    _check_enum(_get(entry, 'event_continuity', what), EVENT_CONTINUITY,
                f'{what}.event_continuity')
    return entry


def _validate_base(base):
    _validate_payload_file(base, 'base')
    _check_enum(_get(base, 'kind', 'base'), BASE_KINDS, 'base.kind')
    _check_enum(_get(base, 'observation_coverage', 'base'), OBSERVATION_COVERAGE,
                'base.observation_coverage')
    check_str(_get(base, 'logical_state_digest', 'base'), 'base.logical_state_digest')

    # The file name carries the generation for humans and for GC; the manifest
    # stays the authority, so a disagreement is a bug rather than a tie-break.
    named = parse_base_generation(base['file'])
    _require(named is not None and named == base['snapshot_generation'],
             f'base file name {base["file"]!r} does not match generation '
             f'{base["snapshot_generation"]}')
    return base


def _validate_increments(increments, base):
    _require(isinstance(increments, list), 'increments must be a list')
    expected = base['snapshot_generation']
    for increment in increments:
        _validate_payload_file(increment, 'increment')
        check_str(_get(increment, 'change_backend', 'increment'),
                  'increment.change_backend')
        for field in ('begin_cursor', 'end_cursor'):
            cursor = _get(increment, field, 'increment')
            _require(cursor is None or isinstance(cursor, str),
                     f'increment.{field} must be a string or null')
        check_uint(_get(increment, 'denied_count', 'increment'),
                   'increment.denied_count')
        check_uint(_get(increment, 'error_count', 'increment'),
                   'increment.error_count')

        expected += 1
        _require(increment['snapshot_generation'] == expected,
                 f'increment generation {increment["snapshot_generation"]} breaks '
                 f'the sequence (expected {expected})')
        named = parse_increment_generation(increment['file'])
        _require(named is not None and named == expected,
                 f'increment file name {increment["file"]!r} does not match '
                 f'generation {expected}')
    return increments


def _validate_cached_consistency(payload, base, increments):
    """The cached consistency value is a derived cache, never an input."""
    cached = payload.get('consistency')
    if cached is None:
        return
    continuity = fold_continuity(base['event_continuity'], increments)
    # Coverage is a function of the rebuilt tree, so the manifest can only be
    # checked against the recorded base value here; the loader re-derives the
    # authoritative value and compares again.
    expected = derive_consistency(continuity, base['observation_coverage'])
    _require(cached == expected or continuity == 'gap',
             f'cached consistency {cached!r} does not match the folded value '
             f'{expected!r}')


def encode_manifest(payload):
    """Serialise a payload with its checksum wrapper."""
    body = canonical_dumps(payload)
    document = {'payload': payload, 'sha256': sha256_hex(body)}
    return canonical_dumps(document)


def decode_manifest(data, snapshot_id, revision=None):
    """Parse and fully validate manifest bytes."""
    try:
        document = strict_loads(data.decode('utf-8'))
    except (UnicodeDecodeError, CanonicalJSONError) as error:
        raise InvalidSnapshot(f'invalid manifest JSON: {error}') from error

    _require(isinstance(document, dict), 'manifest must be a JSON object')
    _require('payload' in document and 'sha256' in document,
             'manifest must have payload and sha256')

    payload = document['payload']
    try:
        actual = sha256_hex(canonical_dumps(payload))
    except (ValueError, TypeError) as error:
        raise InvalidSnapshot(f'manifest payload is not serialisable: {error}') from error
    _require(actual == document['sha256'],
             'manifest checksum mismatch (truncated or modified file)')

    return validate_payload(payload, snapshot_id, revision=revision)


def read_manifest(layout, revision):
    """Read one revision from disk."""
    path = layout.manifest_path(revision)
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except FileNotFoundError as error:
        raise InvalidSnapshot(f'manifest revision {revision} is missing') from error
    return decode_manifest(data, layout.snapshot_id, revision=revision)


def write_manifest(layout, payload):
    """Publish a new immutable revision.

    The must_not_exist assertion states the protocol invariant "a revision is
    never rewritten". It is not a compare-and-swap: only the writer lock makes
    it race free.
    """
    manifest = validate_payload(payload, layout.snapshot_id,
                               revision=payload['manifest_revision'])
    path = layout.manifest_path(manifest.revision)
    commit(path, encode_manifest(payload), must_not_exist=True)
    layout.write_current(manifest.revision)
    return manifest


def latest_revision(layout):
    """The newest revision that parses, preferring the ``current`` pointer.

    ``current`` is a cache: when it is missing, stale or damaged the answer
    comes from listing manifests/ instead of failing.
    """
    revisions = layout.list_manifest_revisions()
    if not revisions:
        raise InvalidSnapshot(f'no manifest in {layout.manifest_dir}')

    cached = layout.read_current()
    if cached is not None and cached == revisions[-1]:
        return cached

    for revision in reversed(revisions):
        path = layout.manifest_path(revision)
        if os.path.exists(path):
            return revision
    raise InvalidSnapshot(f'no readable manifest in {layout.manifest_dir}')


def newest_valid_manifest(layout):
    """Read the newest revision that validates, skipping unreadable tails."""
    last_error = None
    for revision in reversed(layout.list_manifest_revisions()):
        try:
            return read_manifest(layout, revision)
        except InvalidSnapshot as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise InvalidSnapshot(f'no manifest in {layout.manifest_dir}')


def manifest_exists(layout, revision):
    return os.path.exists(os.path.join(layout.manifest_dir,
                                       manifest_filename(revision)))


__all__ = [
    'FORMAT_VERSION', 'MANIFEST_KINDS', 'GENERATION_PRESERVING_KINDS',
    'BASE_KINDS', 'EVENT_CONTINUITY', 'OBSERVATION_COVERAGE',
    'CONSISTENCY_COMPLETE', 'CONSISTENCY_PARTIAL', 'CONSISTENCY_STALE',
    'Manifest', 'decode_manifest', 'derive_consistency', 'encode_manifest',
    'fold_continuity', 'latest_revision', 'manifest_exists',
    'newest_valid_manifest', 'parse_manifest_revision', 'read_manifest',
    'validate_payload', 'write_manifest',
]
