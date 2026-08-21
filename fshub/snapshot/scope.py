"""Scan scope comparison (design doc 9).

Two different questions are answered with two different functions, and they
must never borrow each other's implementation:

* ``normalize_on_source`` produces the value stored in the manifest. It runs
  on the machine being scanned, because only that machine can resolve a
  relative path or '..' correctly.
* ``scope_fold`` answers "were these two scans configured the same way", using
  the snapshot's own OS rules. It is never used as a record key, an index key
  or anything that merges records.
"""

import os


def normalize_on_source(path):
    """Normalise one configured prefix on the host being scanned.

    Case is deliberately preserved: folding belongs in the comparison step,
    where the snapshot's OS is known. Doing it here would bake the scanning
    host's rules into stored data.
    """
    return os.path.abspath(os.path.expanduser(path))


def normalize_prefixes(prefixes):
    """Normalise configured skip prefixes, dropping empty entries."""
    return [normalize_on_source(prefix) for prefix in prefixes if prefix]


def scope_fold(path, os_name):
    """The comparison key for one scope path.

    Windows folding is deliberately conservative: separators are unified,
    ASCII letters are upper-cased and a trailing separator is dropped, but
    non-ASCII characters are left alone. str.casefold() is not used; it has
    expanding mappings and does not match the volume's $UpCase table, so it
    could report two genuinely different scopes as equal. The failure mode of
    this rule is an unnecessary full rescan, which is the safe direction.
    """
    if os_name != 'Windows':
        # POSIX paths are case sensitive and backslash is an ordinary
        # character, so the only safe fold is the identity.
        return path.rstrip('/') or '/'

    folded = path.replace('/', '\\')
    folded = ''.join(ch.upper() if 'a' <= ch <= 'z' else ch for ch in folded)

    # Keep a bare root as-is: 'C:\' and '\' have no shorter form.
    stripped = folded.rstrip('\\')
    if not stripped or (len(stripped) == 2 and stripped[1] == ':'):
        return stripped + '\\'
    return stripped


def scope_equivalent(scope_a, scope_b, os_name):
    """True when two scan scopes describe the same observation range.

    skip prefixes compare as a set: their order is a config detail, not part
    of the meaning.
    """
    if scope_fold(scope_a['root_path'], os_name) != \
            scope_fold(scope_b['root_path'], os_name):
        return False
    if bool(scope_a['cross_filesystems']) != bool(scope_b['cross_filesystems']):
        return False
    return (
        {scope_fold(p, os_name) for p in scope_a['skip_prefixes_normalized']}
        == {scope_fold(p, os_name) for p in scope_b['skip_prefixes_normalized']}
    )


def build_scan_scope(root_path, skip_prefixes, cross_filesystems=True):
    """The scan_scope object stored in a manifest.

    Raw and normalised prefixes are stored separately so that someone who
    prettifies the displayed value cannot accidentally change what the
    equivalence check compares.
    """
    raw = [prefix for prefix in (skip_prefixes or []) if prefix]
    return {
        'root_path': root_path,
        'skip_prefixes_raw': list(raw),
        'skip_prefixes_normalized': normalize_prefixes(raw),
        'cross_filesystems': bool(cross_filesystems),
    }
