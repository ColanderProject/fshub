"""Directory record format (design doc 6).

One record describes one directory. base and increment files use exactly the
same record format; an increment simply repeats the full record of every
directory that changed.

Every field is mandatory and every parallel array must have the same length,
so a producer that forgets to fill one in fails at load time instead of
silently degrading some API into an empty result. Validation therefore runs
before overlay/rebuild, and the code after it indexes fields directly.
"""

from .canonical import (
    UINT64_MAX,
    CanonicalJSONError,
    canonical_dumps,
    check_int,
    check_timestamp,
    check_uint,
)

# Subdirectory traversal states (design doc 6.3).
TRAVERSED = 0
SYMLINK = 1
JUNCTION = 2
SKIPPED = 3
DENIED = 4
ERROR = 5
CROSS_DEVICE = 6

TRAVERSAL_STATES = {
    TRAVERSED: 'traversed',
    SYMLINK: 'symlink',
    JUNCTION: 'junction',
    SKIPPED: 'skipped',
    DENIED: 'denied',
    ERROR: 'error',
    CROSS_DEVICE: 'cross_device',
}

# Declared scan boundaries do not reduce completeness; these two do
# (design doc 12.2).
COVERAGE_DEGRADING_STATES = frozenset({DENIED, ERROR})

RECORD_FIELDS = ('p', 'i', 'f', 's', 't', 'c', 'd', 'T', 'D', 'x')

# Recomputed on every rebuild and never stored (design doc 11.5).
RUNTIME_FIELDS = ('S', 'C', 'LS', 'LC')


class InvalidRecord(CanonicalJSONError):
    """A directory record violates the format."""


def _check_timestamp_triple(value, what):
    if not isinstance(value, list) or len(value) != 3:
        raise InvalidRecord(f'{what} must be a [ctime, mtime, atime] triple')
    for i, item in enumerate(value):
        check_timestamp(item, f'{what}[{i}]')
    return value


def _check_identity(value, what):
    """Validate one incarnation identity: ``[provider_id, value]`` or null."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2:
        raise InvalidRecord(f'{what} must be [provider_id, value] or null')
    check_uint(value[0], f'{what}[0]')
    if not isinstance(value[1], str):
        raise InvalidRecord(f'{what}[1] must be a string')
    return value


def _check_sorted(names, what):
    """Names must be in strict code point order (design doc 6.5).

    No normcase and no Unicode normalization: two names that differ only in
    case or in composition are different directory entries.
    """
    for i in range(1, len(names)):
        if names[i - 1] >= names[i]:
            raise InvalidRecord(
                f'{what} is not sorted by exact name: '
                f'{names[i - 1]!r} before {names[i]!r}'
            )


def validate_record(record, where=''):
    """Validate one decoded record; returns it unchanged.

    Raises InvalidRecord on any violation, so callers past this point may
    index every field directly instead of using .get(field, []).
    """
    location = f' in {where}' if where else ''
    if not isinstance(record, dict):
        raise InvalidRecord(f'record must be a JSON object{location}')

    missing = [field for field in RECORD_FIELDS if field not in record]
    if missing:
        raise InvalidRecord(f'record is missing {", ".join(missing)}{location}')

    path = record['p']
    if not isinstance(path, str) or not path:
        raise InvalidRecord(f'record "p" must be a non-empty string{location}')

    for field in ('f', 'd'):
        names = record[field]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise InvalidRecord(f'{path}: {field!r} must be a list of strings')
        _check_sorted(names, f'{path}: {field!r}')

    files = record['f']
    dirs = record['d']

    for field in ('s', 't', 'T', 'D', 'x'):
        if not isinstance(record[field], list):
            raise InvalidRecord(f'{path}: {field!r} must be a list')

    for field, expected in (('s', files), ('t', files),
                            ('T', dirs), ('D', dirs), ('x', dirs)):
        if len(record[field]) != len(expected):
            raise InvalidRecord(
                f'{path}: {field!r} has {len(record[field])} items, '
                f'expected {len(expected)}'
            )

    for i, size in enumerate(record['s']):
        check_uint(size, f'{path}: s[{i}]')
    for i, stamps in enumerate(record['t']):
        _check_timestamp_triple(stamps, f'{path}: t[{i}]')
    for i, stamps in enumerate(record['T']):
        _check_timestamp_triple(stamps, f'{path}: T[{i}]')

    _check_identity(record['i'], f'{path}: i')
    for i, identity in enumerate(record['D']):
        _check_identity(identity, f'{path}: D[{i}]')

    for i, state in enumerate(record['x']):
        check_int(state, f'{path}: x[{i}]', minimum=0, maximum=UINT64_MAX)
        if state not in TRAVERSAL_STATES:
            # Never treat an unknown state as "not traversed": that would
            # silently hide a subtree a newer producer did record.
            raise InvalidRecord(f'{path}: unknown traversal state x[{i}]={state}')

    _validate_cloud_states(record, path)
    return record


def _validate_cloud_states(record, path):
    """Enforce the single canonical encoding of ``c`` (design doc 6.2)."""
    states = record['c']
    if states is None:
        return
    if not isinstance(states, list):
        raise InvalidRecord(f'{path}: "c" must be null or a list')
    if not record['f']:
        raise InvalidRecord(f'{path}: "c" must be null when "f" is empty')
    if len(states) != len(record['f']):
        raise InvalidRecord(
            f'{path}: "c" has {len(states)} items, expected {len(record["f"])}'
        )
    if all(state is None for state in states):
        raise InvalidRecord(f'{path}: all-null "c" must be written as null')
    for i, state in enumerate(states):
        if state is not None and not isinstance(state, str):
            raise InvalidRecord(f'{path}: c[{i}] must be a string or null')


def sort_record(record):
    """Sort a record's parallel arrays into canonical order, in place.

    Asserting the lengths first is deliberate: a failed os.stat that appended
    to one array but not its neighbour is the most likely producer bug here,
    and permuting mismatched arrays would scramble the record instead of
    reporting it.
    """
    files = record['f']
    for field in ('s', 't'):
        if len(record[field]) != len(files):
            raise InvalidRecord(
                f'{record["p"]}: {field!r} has {len(record[field])} items, '
                f'expected {len(files)}'
            )
    states = record['c']
    if isinstance(states, list) and len(states) != len(files):
        raise InvalidRecord(
            f'{record["p"]}: "c" has {len(states)} items, expected {len(files)}'
        )

    dirs = record['d']
    for field in ('T', 'D', 'x'):
        if len(record[field]) != len(dirs):
            raise InvalidRecord(
                f'{record["p"]}: {field!r} has {len(record[field])} items, '
                f'expected {len(dirs)}'
            )

    file_order = sorted(range(len(files)), key=files.__getitem__)
    record['f'] = [files[i] for i in file_order]
    record['s'] = [record['s'][i] for i in file_order]
    record['t'] = [record['t'][i] for i in file_order]
    if isinstance(states, list):
        states = [states[i] for i in file_order]
        record['c'] = states if any(state is not None for state in states) else None
    elif states is not None:
        raise InvalidRecord(f'{record["p"]}: "c" must be null or a list')

    dir_order = sorted(range(len(dirs)), key=dirs.__getitem__)
    record['d'] = [dirs[i] for i in dir_order]
    for field in ('T', 'D', 'x'):
        record[field] = [record[field][i] for i in dir_order]
    return record


def new_record(path):
    """An empty record with every mandatory field present."""
    return {
        'p': path,
        'i': None,
        'f': [], 's': [], 't': [], 'c': None,
        'd': [], 'T': [], 'D': [], 'x': [],
    }


def dump_record(record):
    """Serialise one record to its JSONL line bytes.

    ensure_ascii is mandatory: on Linux an undecodable file name is carried
    as unpaired surrogates, and encoding those to UTF-8 raises. Escaping them
    as \\uXXXX keeps the file writable and fully reversible.
    """
    return canonical_dumps(record) + b'\n'


def resolve_identity(identity, providers):
    """Map ``[provider_id, value]`` onto ``[scheme, value]``.

    The digest uses the resolved form so that renumbering provider ids during
    compaction cannot change the digest of an unchanged tree.
    """
    if identity is None:
        return None
    provider_id, value = identity
    provider = providers.get(provider_id)
    if provider is None:
        raise InvalidRecord(f'unknown identity provider id: {provider_id}')
    return [provider['scheme'], value]


def digest_payload(record, providers):
    """The part of a record that participates in logical_state_digest.

    Runtime totals are excluded because they are derived, and provider ids
    are resolved to schemes for the reason given in resolve_identity.
    """
    return {
        'p': record['p'],
        'i': resolve_identity(record['i'], providers),
        'f': record['f'],
        's': record['s'],
        't': record['t'],
        'c': record['c'],
        'd': record['d'],
        'T': record['T'],
        'D': [resolve_identity(identity, providers) for identity in record['D']],
        'x': record['x'],
    }
