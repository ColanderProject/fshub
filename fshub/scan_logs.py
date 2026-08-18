"""Durable logs and small status files for filesystem scan runs."""

from datetime import datetime
import json
import os
import sys
import time
import uuid

from .config import get_config

LOG_SUFFIX = '.jsonl'
STATUS_SUFFIX = '.status.json'
MAX_LISTED_SCANS = 50
MAX_REPORTED_ERRORS = 20
_PROGRESS_INTERVAL = 5.0


def _timestamp():
    return int(datetime.now().timestamp())


def _counter_snapshot(counters):
    errors = list(counters.get('errors', []))[:MAX_REPORTED_ERRORS]
    return {
        'current_path': counters.get('current_path', ''),
        'scanned_count': counters.get('scanned_count', 0),
        'scanned_size': counters.get('scanned_size', 0),
        'error_count': counters.get('error_count', len(errors)),
        'errors': errors,
    }


def _valid_scan_id(scan_id):
    if not isinstance(scan_id, str):
        return False
    try:
        return str(uuid.UUID(scan_id)) == scan_id
    except (ValueError, AttributeError):
        return False


def _log_available(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _paths(scan_id):
    if not _valid_scan_id(scan_id):
        raise ValueError('Invalid scan ID')
    base = os.path.join(get_config().scan_log_dir, scan_id)
    return base + LOG_SUFFIX, base + STATUS_SUFFIX


class ScanRunLog:
    """Write one scan's detailed log and compact latest-status sidecar.

    The JSONL handle stays open for the run and is flushed without fsync.
    Logging failures are reported once to stderr and never fail the scan.
    """

    def __init__(self, scan_id=None):
        self.scan_id = scan_id or str(uuid.uuid4())
        self.path, self.status_path = _paths(self.scan_id)
        self._log_file = None
        self._reported_failure = False
        self._last_progress = 0.0
        self.start_time = None
        self.scan_path = ''

        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._log_file = open(self.path, 'a', encoding='utf-8')
        except OSError as error:
            self._report_failure(error)

    @property
    def available(self):
        return _log_available(self.path)

    def _report_failure(self, error):
        if not self._reported_failure:
            print(f'Could not persist scan log {self.path}: {error}', file=sys.stderr)
            self._reported_failure = True

    def _disable_log(self, error):
        self._report_failure(error)
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
        self._log_file = None

    def _append(self, event, flush=False, **fields):
        if self._log_file is None:
            return
        record = {
            'timestamp': _timestamp(),
            'event': event,
            'scan_id': self.scan_id,
            **fields,
        }
        try:
            # ASCII escaping also makes unusual filesystem names safe to write.
            self._log_file.write(json.dumps(record) + '\n')
            if flush:
                self._log_file.flush()
        except (OSError, TypeError, ValueError, UnicodeError) as error:
            self._disable_log(error)

    def _write_status(self, status, counters, *, finish_time=None,
                      error=None, result_file=None):
        payload = {
            'scan_id': self.scan_id,
            'path': self.scan_path,
            'status': status,
            'start_time': self.start_time,
            'finish_time': finish_time,
            'counters': _counter_snapshot(counters),
            'error': error,
            'result_file': result_file,
            'log_available': self.available,
        }
        temp_path = self.status_path + '.tmp'
        try:
            with open(temp_path, 'w', encoding='utf-8') as status_file:
                json.dump(payload, status_file)
            os.replace(temp_path, self.status_path)
        except (OSError, TypeError, ValueError, UnicodeError) as write_error:
            self._report_failure(write_error)
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def started(self, path, use_index=False, skip_paths=None):
        self.start_time = _timestamp()
        self.scan_path = path
        counters = {
            'current_path': path,
            'scanned_count': 0,
            'scanned_size': 0,
            'error_count': 0,
            'errors': [],
        }
        self._append(
            'started',
            flush=True,
            path=path,
            use_index=use_index,
            skip_paths=list(skip_paths or []),
        )
        self._write_status('running', counters)

    def progress(self, counters, force=False):
        now = time.monotonic()
        if not force and now - self._last_progress < _PROGRESS_INTERVAL:
            return
        self._last_progress = now
        self._append('progress', flush=True, counters=_counter_snapshot(counters))
        self._write_status('running', counters)

    def scan_error(self, message, _counters):
        self._append('scan_error', flush=True, message=str(message))

    def completed(self, counters, result_file):
        finish_time = _timestamp()
        status = ('completed_with_errors'
                  if counters.get('error_count', len(counters.get('errors', [])))
                  else 'completed')
        self._append(
            'completed',
            flush=True,
            counters=_counter_snapshot(counters),
            result_file=result_file,
        )
        self._write_status(
            status,
            counters,
            finish_time=finish_time,
            result_file=result_file,
        )
        self.close()

    def failed(self, error, counters):
        finish_time = _timestamp()
        self._append(
            'failed',
            flush=True,
            error=str(error),
            counters=_counter_snapshot(counters),
        )
        self._write_status(
            'error',
            counters,
            finish_time=finish_time,
            error=str(error),
        )
        self.close()

    def close(self):
        if self._log_file is not None:
            try:
                self._log_file.flush()
                self._log_file.close()
            except OSError as error:
                self._report_failure(error)
            finally:
                self._log_file = None


def _decode_log_record(line, scan_id):
    try:
        record = json.loads(line.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(record, dict) and record.get('scan_id') == scan_id:
        return record
    return None


def read_scan_log(scan_id):
    """Read valid records from one detailed log, ignoring a truncated line."""
    path, _status_path = _paths(scan_id)
    records = []
    try:
        with open(path, 'rb') as log_file:
            for line in log_file:
                record = _decode_log_record(line, scan_id)
                if record is not None:
                    records.append(record)
    except FileNotFoundError:
        return None
    return records or None


def read_scan_log_page(scan_id, cursor=0, limit=200):
    """Read a bounded page and return (records, next byte cursor, has_more)."""
    path, _status_path = _paths(scan_id)
    records = []
    try:
        with open(path, 'rb') as log_file:
            log_file.seek(cursor)
            while len(records) < limit:
                line = log_file.readline()
                if not line:
                    break
                record = _decode_log_record(line, scan_id)
                if record is not None:
                    records.append(record)
            next_cursor = log_file.tell()

            has_more = False
            for line in log_file:
                if _decode_log_record(line, scan_id) is not None:
                    has_more = True
                    break
    except (FileNotFoundError, OSError):
        return None
    if not records and cursor == 0:
        return None
    return records, next_cursor, has_more


def read_scan_status(scan_id):
    """Read one compact status sidecar; a stale running task is interrupted."""
    _log_path, status_path = _paths(scan_id)
    try:
        with open(status_path, encoding='utf-8') as status_file:
            status = json.load(status_file)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(status, dict) or status.get('scan_id') != scan_id:
        return None
    if status.get('status') == 'running':
        status['status'] = 'interrupted'
    status['log_available'] = _log_available(_log_path)
    return status


def list_scan_statuses(limit=MAX_LISTED_SCANS):
    """Read at most ``limit`` small sidecars, newest first."""
    log_dir = get_config().scan_log_dir
    try:
        names = [name for name in os.listdir(log_dir)
                 if name.endswith(STATUS_SUFFIX)]
    except FileNotFoundError:
        return []

    def modified(name):
        try:
            return os.path.getmtime(os.path.join(log_dir, name))
        except OSError:
            return 0

    names.sort(key=modified, reverse=True)
    statuses = []
    for name in names[:limit]:
        scan_id = name[:-len(STATUS_SUFFIX)]
        if not _valid_scan_id(scan_id):
            continue
        status = read_scan_status(scan_id)
        if status:
            statuses.append(status)
    return statuses
