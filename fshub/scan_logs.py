"""Durable, append-only logs for filesystem scan runs."""

from datetime import datetime
import json
import os
import threading
import time
import uuid

from .config import get_config

_LOG_SUFFIX = '.jsonl'
_PROGRESS_INTERVAL = 5.0


def _timestamp():
    return int(datetime.now().timestamp())


def _counter_snapshot(counters):
    """Return the bounded part of counters suitable for repeated log entries."""
    return {
        'current_path': counters.get('current_path', ''),
        'scanned_count': counters.get('scanned_count', 0),
        'scanned_size': counters.get('scanned_size', 0),
        'error_count': len(counters.get('errors', [])),
    }


def _valid_scan_id(scan_id):
    if not isinstance(scan_id, str):
        return False
    try:
        return str(uuid.UUID(scan_id)) == scan_id
    except (ValueError, AttributeError):
        return False


def _log_path(scan_id):
    if not _valid_scan_id(scan_id):
        raise ValueError('Invalid scan ID')
    return os.path.join(get_config().scan_log_dir, scan_id + _LOG_SUFFIX)


class ScanRunLog:
    """Append lifecycle, progress and errors for one scan to disk.

    A new file handle is opened for every event. Scan events are infrequent
    (progress is throttled), and closing each append makes records available
    after a process crash without relying on a long-lived buffered handle.
    """

    def __init__(self, scan_id=None):
        self.scan_id = scan_id or str(uuid.uuid4())
        self.path = _log_path(self.scan_id)
        self._lock = threading.Lock()
        self._last_progress = time.monotonic()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    @property
    def filename(self):
        return os.path.basename(self.path)

    def _append(self, event, **fields):
        record = {
            'timestamp': _timestamp(),
            'event': event,
            'scan_id': self.scan_id,
            **fields,
        }
        encoded = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.path, 'a', encoding='utf-8') as log_file:
                log_file.write(encoded + '\n')
                log_file.flush()
                os.fsync(log_file.fileno())

    def started(self, path, use_index=False, skip_paths=None):
        self._append(
            'started',
            path=path,
            use_index=use_index,
            skip_paths=list(skip_paths or []),
        )

    def progress(self, counters, force=False):
        now = time.monotonic()
        if not force and now - self._last_progress < _PROGRESS_INTERVAL:
            return
        self._last_progress = now
        self._append('progress', counters=_counter_snapshot(counters))

    def scan_error(self, message, counters):
        # Errors are never throttled: an interrupted scan must retain every
        # access failure that had already been observed.
        self._append(
            'scan_error',
            message=str(message),
            counters=_counter_snapshot(counters),
        )

    def completed(self, counters, result_file):
        self._append(
            'completed',
            counters=_counter_snapshot(counters),
            result_file=result_file,
        )

    def failed(self, error, counters):
        self._append(
            'failed',
            error=str(error),
            counters=_counter_snapshot(counters),
        )


def read_scan_log(scan_id):
    """Read valid records from one scan log.

    A truncated final line can be left by an abrupt process termination. It is
    ignored while all complete records remain available.
    """
    path = _log_path(scan_id)
    records = []
    try:
        with open(path, 'rb') as log_file:
            for line in log_file:
                try:
                    record = json.loads(line.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict) and record.get('scan_id') == scan_id:
                    records.append(record)
    except FileNotFoundError:
        return None
    return records


def summarize_scan_log(records):
    """Build a status response from records belonging to one scan."""
    if not records:
        return None

    first = next((record for record in records
                  if record.get('event') == 'started'), records[0])
    last = records[-1]
    terminal = next(
        (record for record in reversed(records)
         if record.get('event') in ('completed', 'failed')),
        None,
    )
    latest_counters = next(
        (record.get('counters') for record in reversed(records)
         if isinstance(record.get('counters'), dict)),
        {},
    )
    errors = [record.get('message', '') for record in records
              if record.get('event') == 'scan_error']

    if terminal and terminal.get('event') == 'completed':
        error_count = terminal.get('counters', {}).get('error_count', 0)
        status = 'completed_with_errors' if error_count else 'completed'
    elif terminal and terminal.get('event') == 'failed':
        status = 'error'
    else:
        # A disk-only non-terminal run belonged to a previous process. It
        # cannot still be running after that process has restarted.
        status = 'interrupted'

    counters = {
        'current_path': latest_counters.get('current_path', first.get('path', '')),
        'scanned_count': latest_counters.get('scanned_count', 0),
        'scanned_size': latest_counters.get('scanned_size', 0),
        'errors': errors,
    }
    return {
        'scan_id': first.get('scan_id', last.get('scan_id')),
        'path': first.get('path', ''),
        'status': status,
        'start_time': first.get('timestamp'),
        'finish_time': terminal.get('timestamp') if terminal else None,
        'counters': counters,
        'error': terminal.get('error') if terminal else None,
        'result_file': terminal.get('result_file') if terminal else None,
        'log_available': True,
    }


def list_scan_logs():
    """Return durable scan summaries, newest first."""
    log_dir = get_config().scan_log_dir
    try:
        names = os.listdir(log_dir)
    except FileNotFoundError:
        return []

    summaries = []
    for name in names:
        if not name.endswith(_LOG_SUFFIX):
            continue
        scan_id = name[:-len(_LOG_SUFFIX)]
        if not _valid_scan_id(scan_id):
            continue
        records = read_scan_log(scan_id)
        summary = summarize_scan_log(records or [])
        if summary:
            summaries.append(summary)
    summaries.sort(key=lambda item: item.get('start_time') or 0, reverse=True)
    return summaries
