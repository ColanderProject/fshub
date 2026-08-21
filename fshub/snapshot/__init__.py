"""Incremental snapshot storage: one base plus increments per snapshot.

The format is defined in docs/design/incremental-snapshot.md. Module map:

    canonical.py  canonical JSON and strict decoding
    records.py    the directory record format
    layout.py     on-disk layout, durable writes, the writer lock
    manifest.py   immutable manifest revisions
    loader.py     overlay, rebuild, verification, totals
    writer.py     commits, compaction, garbage collection
    identity.py   incarnation identity providers
    scope.py      scan scope comparison
    producer.py   which installation may extend a chain
"""

from .errors import InvalidSnapshot, SnapshotBusy, SnapshotError, WriterLocked
from .identity import IdentityRegistry
from .layout import SnapshotLayout, new_snapshot_id
from .loader import load_snapshot, rebuild, snapshot_dirs
from .manifest import Manifest, newest_valid_manifest, read_manifest
from .producer import ProducerMismatch, get_producer_id
from .records import (
    CROSS_DEVICE,
    DENIED,
    ERROR,
    JUNCTION,
    SKIPPED,
    SYMLINK,
    TRAVERSED,
    new_record,
)
from .scope import build_scan_scope, scope_equivalent
from .writer import ScopeChanged, SnapshotWriter, should_compact

__all__ = [
    'CROSS_DEVICE', 'DENIED', 'ERROR', 'IdentityRegistry', 'InvalidSnapshot',
    'JUNCTION', 'Manifest', 'ProducerMismatch', 'SKIPPED', 'SYMLINK',
    'ScopeChanged', 'SnapshotBusy', 'SnapshotError', 'SnapshotLayout',
    'SnapshotWriter', 'TRAVERSED', 'WriterLocked', 'build_scan_scope',
    'get_producer_id', 'load_snapshot', 'new_record', 'new_snapshot_id',
    'newest_valid_manifest', 'read_manifest', 'rebuild', 'scope_equivalent',
    'should_compact', 'snapshot_dirs',
]
