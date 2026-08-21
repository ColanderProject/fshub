"""Producer identity (design doc 8.3).

A producer is one fshub installation. Its id lives in local_state_path, never
inside data_path, so that copying or restoring the data directory onto another
machine does not hand that machine the right to continue an increment chain it
never observed.

The existing calculate_thumbprint() is not used for this. It mixes in the
kernel release, the host name and uuid.getnode(), so a kernel upgrade, a
rename or a new network card would all break the chain; worse, getnode()
returns a random value when it cannot read a hardware address.
"""

import os
import threading
import uuid

from ..config import get_config
from ..utils import get_system_info
from .errors import SnapshotError

PRODUCER_ID_FILE = 'producer_id'

_lock = threading.Lock()


class ProducerMismatch(SnapshotError):
    """The snapshot was written by a different installation."""


def producer_id_path():
    return os.path.join(get_config().local_state_path, PRODUCER_ID_FILE)


def get_producer_id():
    """Return this installation's persistent id, creating it once."""
    path = producer_id_path()
    with _lock:
        try:
            with open(path, 'r', encoding='ascii') as handle:
                value = handle.read(64).strip()
            if value:
                return value
        except OSError:
            pass

        value = uuid.uuid4().hex
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # O_EXCL so two processes racing here settle on one id instead of
        # each overwriting the other's file.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            with open(path, 'r', encoding='ascii') as handle:
                return handle.read(64).strip()
        with os.fdopen(fd, 'w', encoding='ascii') as handle:
            handle.write(value + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        return value


def build_producer():
    """The producer block stored in a manifest.

    The device details are provenance for humans; nothing gates on them.
    """
    info = get_system_info()
    return {
        'producer_id': get_producer_id(),
        'device': {
            'device_name': info['device_name'],
            'host_name': info['host_name'],
            'thumbprint': info['thumbprint'],
            'cpu_model': info['cpu_model'],
            'memory_size': info['memory_size'],
            'ip_addr': info['ip_addr'],
            'mac_addr': info['mac_addr'],
        },
    }


def check_producer(manifest, producer_id=None, adopt=False):
    """Refuse to extend a chain written by another installation.

    A mismatch is not necessarily an attack or a bug: a systemd service user
    and an interactive user on the same machine have different local state.
    The right answer is therefore an explicit adopt, not a flat refusal.
    """
    producer_id = producer_id or get_producer_id()
    recorded = manifest.payload['producer']['producer_id']
    if recorded == producer_id or adopt:
        return producer_id
    raise ProducerMismatch(
        f'snapshot {manifest.snapshot_id} was produced by {recorded}, not by '
        f'this installation ({producer_id}); adopt it explicitly or run a '
        f'full rescan'
    )
