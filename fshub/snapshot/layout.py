"""On-disk layout and durable write primitives (design doc 5 and 13).

    snapshots/snapshot_<ts>_<rand>/
        manifests/manifest_rev_000000.json   immutable, one per commit
        current                              pointer file, pure cache
        base_gen_000000.jsonl.gz
        increments/inc_gen_000001_<ts>.jsonl.gz
        gc.jsonl
        writer.lock
        groups.jl

Publishing anything goes through commit(): write a uniquely named temp file,
fsync it, os.replace() it into place, then fsync the directory. Only files
referenced by a published manifest revision count as committed.
"""

import errno
import gzip
import hashlib
import io
import os
import platform
import re
import time
import uuid

from ..utils import UnsafePathError, ensure_within
from .errors import InvalidSnapshot, WriterLocked

IS_WINDOWS = platform.system() == 'Windows'

MANIFEST_DIR = 'manifests'
INCREMENT_DIR = 'increments'
CURRENT_FILE = 'current'
GC_FILE = 'gc.jsonl'
LOCK_FILE = 'writer.lock'
GROUPS_FILE = 'groups.jl'

SNAPSHOT_ID_RE = re.compile(r'^snapshot_\d{1,20}_[0-9a-f]{8}$')
MANIFEST_RE = re.compile(r'^manifest_rev_(\d{6,20})\.json$')
BASE_RE = re.compile(r'^base_gen_(\d{6,20})\.jsonl\.gz$')
INCREMENT_RE = re.compile(r'^inc_gen_(\d{6,20})_(\d{1,20})\.jsonl\.gz$')
TEMP_SUFFIX_RE = re.compile(r'\.\d+-[0-9a-f]{32}\.tmp$')


def new_snapshot_id(now=None):
    """A fresh snapshot id; the random tail keeps concurrent scans apart."""
    now = int(time.time()) if now is None else int(now)
    return f'snapshot_{now}_{uuid.uuid4().hex[:8]}'


def check_snapshot_id(snapshot_id):
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_RE.match(snapshot_id):
        raise UnsafePathError(f'Invalid snapshot id: {snapshot_id!r}')
    return snapshot_id


def manifest_filename(revision):
    return f'manifest_rev_{revision:06d}.json'


def base_filename(generation):
    return f'base_gen_{generation:06d}.jsonl.gz'


def increment_filename(generation, created_at):
    return f'{INCREMENT_DIR}/inc_gen_{generation:06d}_{int(created_at)}.jsonl.gz'


def parse_manifest_revision(name):
    match = MANIFEST_RE.match(name)
    return int(match.group(1)) if match else None


def parse_base_generation(name):
    """Generation encoded in a base file name, or None."""
    match = BASE_RE.match(os.path.basename(name))
    return int(match.group(1)) if match else None


def parse_increment_generation(name):
    """Generation encoded in an increment file name, or None."""
    match = INCREMENT_RE.match(os.path.basename(name))
    return int(match.group(1)) if match else None


def is_temp_name(name):
    return bool(TEMP_SUFFIX_RE.search(name))


class SnapshotLayout:
    """Resolves every path inside one snapshot directory."""

    def __init__(self, snapshot_dir):
        self.dir = os.path.abspath(snapshot_dir)
        self.snapshot_id = os.path.basename(self.dir.rstrip(os.sep))

    @classmethod
    def create(cls, snapshots_root, snapshot_id):
        check_snapshot_id(snapshot_id)
        layout = cls(os.path.join(snapshots_root, snapshot_id))
        os.makedirs(layout.manifest_dir, exist_ok=True)
        os.makedirs(layout.increment_dir, exist_ok=True)
        return layout

    @property
    def manifest_dir(self):
        return os.path.join(self.dir, MANIFEST_DIR)

    @property
    def increment_dir(self):
        return os.path.join(self.dir, INCREMENT_DIR)

    @property
    def current_path(self):
        return os.path.join(self.dir, CURRENT_FILE)

    @property
    def gc_path(self):
        return os.path.join(self.dir, GC_FILE)

    @property
    def lock_path(self):
        return os.path.join(self.dir, LOCK_FILE)

    @property
    def groups_path(self):
        return os.path.join(self.dir, GROUPS_FILE)

    def manifest_path(self, revision):
        return os.path.join(self.manifest_dir, manifest_filename(revision))

    def resolve(self, relative):
        """Resolve a manifest ``file`` entry, refusing to leave the snapshot.

        Manifests are data, not code: a relative path with '..' or a drive
        letter has to be rejected rather than followed.
        """
        if not isinstance(relative, str) or not relative:
            raise InvalidSnapshot('manifest file entry must be a non-empty string')
        if os.path.isabs(relative) or (len(relative) >= 2 and relative[1] == ':'):
            raise InvalidSnapshot(f'manifest file entry must be relative: {relative!r}')
        parts = relative.replace('\\', '/').split('/')
        if any(part in ('', '.', '..') for part in parts):
            raise InvalidSnapshot(f'unsafe manifest file entry: {relative!r}')
        try:
            return ensure_within(self.dir, os.path.join(self.dir, *parts),
                                 what='snapshot file')
        except UnsafePathError as error:
            raise InvalidSnapshot(str(error)) from error

    def list_manifest_revisions(self):
        """Every revision number present in manifests/, ascending."""
        try:
            entries = os.listdir(self.manifest_dir)
        except OSError:
            return []
        revisions = []
        for name in entries:
            revision = parse_manifest_revision(name)
            if revision is not None:
                revisions.append(revision)
        return sorted(revisions)

    def read_current(self):
        """The cached latest revision number, or None when unusable.

        The pointer is an accelerator only. Anything unexpected in it means
        "list the directory instead", never "fail the load".
        """
        try:
            with open(self.current_path, 'r', encoding='ascii') as handle:
                text = handle.read(64).strip()
        except (OSError, ValueError):
            return None
        return int(text) if text.isdigit() else None

    def write_current(self, revision):
        """Best-effort pointer update; failure is not a commit failure."""
        try:
            commit(self.current_path, f'{revision}\n'.encode('ascii'))
        except OSError:
            pass


# -- durable writes -------------------------------------------------------


def fsync_dir(path):
    """Persist a directory entry.

    Windows cannot open a directory handle through os.open, so the rename is
    left to the NTFS log there (design doc 13.3).
    """
    if IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def temp_path_for(path):
    """A temp name that no concurrent writer or crashed run can collide with."""
    return f'{path}.{os.getpid()}-{uuid.uuid4().hex}.tmp'


def commit(path, data, must_not_exist=False):
    """Atomically publish ``data`` at ``path``."""
    def write(handle):
        handle.write(data)

    return commit_stream(path, write, must_not_exist=must_not_exist)


def commit_stream(path, write_fn, must_not_exist=False):
    """Atomically publish whatever ``write_fn`` writes into a binary file.

    must_not_exist asserts the immutability of manifest revisions. It is not
    concurrency control: between the check and the replace there is a window
    that only the writer lock closes.
    """
    temp = temp_path_for(path)
    try:
        with open(temp, 'wb') as handle:
            write_fn(handle)
            handle.flush()
            os.fsync(handle.fileno())

        if must_not_exist and os.path.exists(path):
            raise InvalidSnapshot(f'refusing to overwrite existing file: {path}')

        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise

    fsync_dir(os.path.dirname(path) or '.')
    return path


class _CountingHasher:
    """Accumulates size and sha256 of the compressed bytes as they stream by."""

    def __init__(self):
        self.size = 0
        self.hash = hashlib.sha256()

    def update(self, chunk):
        self.size += len(chunk)
        self.hash.update(chunk)

    @property
    def hexdigest(self):
        return self.hash.hexdigest()


class _HashingReader(io.RawIOBase):
    """Reads a file while hashing the raw bytes, so one pass verifies both."""

    def __init__(self, raw, hasher):
        self._raw = raw
        self._hasher = hasher

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self._raw.read(len(buffer))
        if not chunk:
            return 0
        buffer[: len(chunk)] = chunk
        self._hasher.update(chunk)
        return len(chunk)


class _HashingWriter:
    """Forwards compressed bytes to the file while hashing them."""

    def __init__(self, raw, hasher):
        self._raw = raw
        self._hasher = hasher

    def write(self, data):
        self._hasher.update(data)
        return self._raw.write(data)

    def flush(self):
        self._raw.flush()


def write_jsonl_gz(path, lines, must_not_exist=False):
    """Write gzip-compressed JSONL and return (size, sha256).

    gzip.open() hides the underlying descriptor, so the gzip layer has to be
    closed before the raw file is fsynced. Doing it the other way round
    fsyncs a file whose gzip trailer is still buffered.
    """
    hasher = _CountingHasher()
    temp = temp_path_for(path)
    try:
        with open(temp, 'wb') as handle:
            # mtime=0 keeps the output byte-identical for identical input,
            # which is what makes increments reproducible across runs.
            sink = _HashingWriter(handle, hasher)
            with gzip.GzipFile(fileobj=sink, mode='wb', mtime=0) as compressor:
                for line in lines:
                    compressor.write(line)
            handle.flush()
            os.fsync(handle.fileno())

        if must_not_exist and os.path.exists(path):
            raise InvalidSnapshot(f'refusing to overwrite existing file: {path}')
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise

    fsync_dir(os.path.dirname(path) or '.')
    return hasher.size, hasher.hexdigest


class JsonlGzReader:
    """Iterates a gzip JSONL file, exposing size/sha256 of the raw bytes.

    Verification data comes from the same pass that decompresses, so the file
    is never read twice; ``size`` and ``sha256`` are only meaningful once
    iteration has run to completion.
    """

    def __init__(self, path):
        self.path = path
        self._hasher = _CountingHasher()
        self.complete = False

    @property
    def size(self):
        return self._hasher.size

    @property
    def sha256(self):
        return self._hasher.hexdigest

    def __iter__(self):
        with open(self.path, 'rb') as raw:
            reader = io.BufferedReader(_HashingReader(raw, self._hasher))
            with gzip.GzipFile(fileobj=reader, mode='rb') as decompressor:
                for line in decompressor:
                    if line.strip():
                        yield line
            # Drain whatever the gzip member did not consume so the digest
            # covers the entire file, including any appended garbage.
            while reader.read(1 << 16):
                pass
        self.complete = True


# -- writer lock ----------------------------------------------------------


class WriterLock:
    """Cross-process exclusion for the single supported writer.

    flock (POSIX) and msvcrt.locking (Windows) both release when the process
    dies, so a SIGKILLed writer never leaves a stale lock behind. POSIX
    fcntl(F_SETLK) is deliberately not used: its per-process semantics drop
    every lock as soon as any descriptor to the file is closed.
    """

    def __init__(self, lock_path):
        self.path = lock_path
        self._handle = None

    def acquire(self, blocking=False):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        handle = open(self.path, 'a+b')
        try:
            self._lock(handle, blocking)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    @staticmethod
    def _lock(handle, blocking):
        if IS_WINDOWS:
            import msvcrt

            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
            except OSError as error:
                raise WriterLocked('snapshot writer lock is held') from error
            return

        import fcntl

        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise WriterLocked('snapshot writer lock is held') from error
            raise

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt

                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc_info):
        self.release()
        return False
