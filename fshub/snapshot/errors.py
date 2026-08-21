"""Errors raised by the incremental snapshot store.

The two failure classes are kept apart on purpose (design doc 11.1): a busy
store must be retried, a corrupt one must be repaired by an operator. Mapping
both onto one exception would make that operational difference invisible.
"""


class SnapshotError(Exception):
    """Base class for every snapshot store failure."""


class InvalidSnapshot(SnapshotError):
    """The snapshot on disk violates the format or is corrupt."""


class SnapshotBusy(SnapshotError):
    """A concurrent writer kept the loader from capturing a stable view."""


class WriterLocked(SnapshotError):
    """Another writer holds the snapshot writer lock."""
