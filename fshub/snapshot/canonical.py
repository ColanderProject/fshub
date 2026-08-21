"""Canonical JSON encoding and strict decoding (design doc 7.5).

Python's json defaults are unsafe for a format whose bytes are hashed:

* ``json.loads('NaN')`` succeeds and yields a float,
* ``json.dumps(float('nan'))`` emits invalid JSON,
* duplicate object keys are silently resolved last-write-wins,
* floats round-trip through repr and are not reproducible across versions.

Every reader and writer in this package goes through the helpers below so
those defaults can never leak back in.
"""

import hashlib
import json

# Unix timestamps are signed; counters, sizes, generations and revisions are
# not. Python integers have unbounded precision, so nothing overflows on its
# own and both sides have to check explicitly.
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
UINT64_MAX = 2 ** 64 - 1


class CanonicalJSONError(ValueError):
    """The document is not valid canonical JSON for this format."""


def _reject_constant(name):
    raise CanonicalJSONError(f'invalid JSON constant: {name}')


def _reject_float(text):
    raise CanonicalJSONError(f'floating point numbers are not allowed: {text}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJSONError(f'duplicate object key: {key!r}')
        result[key] = value
    return result


def canonical_dumps(payload):
    """Serialise a payload to the one byte sequence that gets hashed."""
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def strict_loads(text):
    """Parse JSON, refusing every construct canonical_dumps cannot produce."""
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except CanonicalJSONError:
        raise
    except ValueError as error:
        raise CanonicalJSONError(f'invalid JSON: {error}') from error


def sha256_hex(data):
    """Hex digest of a byte string."""
    return hashlib.sha256(data).hexdigest()


def check_int(value, what, minimum=INT64_MIN, maximum=INT64_MAX):
    """Validate an integer and its range.

    ``bool`` is rejected even though it is an ``int`` subclass: accepting it
    would let ``True`` masquerade as generation 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalJSONError(f'{what} must be an integer, got {type(value).__name__}')
    if not minimum <= value <= maximum:
        raise CanonicalJSONError(f'{what} out of range: {value}')
    return value


def check_uint(value, what):
    """Validate an unsigned 64 bit integer."""
    return check_int(value, what, minimum=0, maximum=UINT64_MAX)


def check_timestamp(value, what):
    """Validate a signed 64 bit Unix timestamp."""
    return check_int(value, what)


def check_str(value, what):
    if not isinstance(value, str):
        raise CanonicalJSONError(f'{what} must be a string')
    return value


def check_ints(payload, what):
    """Recursively assert that a decoded payload holds no illegal numbers.

    Used on manifests read from disk: the parse hooks above already reject
    floats and NaN, this catches integers outside the declared ranges before
    they reach arithmetic or file names.
    """
    if isinstance(payload, bool):
        return
    if isinstance(payload, int):
        check_int(payload, what)
    elif isinstance(payload, dict):
        for key, value in payload.items():
            check_ints(value, f'{what}.{key}')
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            check_ints(value, f'{what}[{i}]')
