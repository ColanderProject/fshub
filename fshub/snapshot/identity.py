"""Incarnation identity providers (design doc 6.4 and 7.4).

An identity answers "is this the same directory object as before", so that a
subtree deleted and replaced under the same path cannot quietly stay
reachable. It is defence in depth, never a substitute for event continuity.

Everything this module produces is registered as ``weak``. Both st_ino on
Windows and (dev, ino) on POSIX are reused after deletion, so calling them
strong would turn a guard into a false assurance. A strong provider needs a
real handle (name_to_handle_at, or an NTFS file reference with its sequence
number) and is left to a later platform backend.
"""

import platform

NONE_PROVIDER = {'id': 0, 'scheme': 'none', 'strength': 'none'}


class IdentityRegistry:
    """Assigns provider ids to the sources seen during one scan.

    Ids are only meaningful inside the segment they were written for, which
    is why the table is published with every manifest revision and a loader
    must resolve records against the table of their own revision.
    """

    def __init__(self, os_name=None):
        self.os_name = os_name or platform.system()
        self._by_source = {}
        self._providers = [dict(NONE_PROVIDER)]

    @classmethod
    def from_providers(cls, providers, os_name=None):
        """Continue an existing table instead of renumbering it.

        An increment is written into the segment its base belongs to, so the
        ids already used by stored records must keep their meaning; unseen
        devices are appended with fresh ids.
        """
        registry = cls(os_name)
        registry._providers = [dict(provider) for provider in providers]
        for provider in registry._providers:
            source = provider.get('source') or {}
            device = source.get('device')
            if device is not None and provider['scheme'] == registry.scheme:
                registry._by_source[device] = provider['id']
        return registry

    def extends(self, providers):
        """True when this table only appended to ``providers``."""
        if len(self._providers) < len(providers):
            return False
        return all(existing == self._providers[i]
                   for i, existing in enumerate(providers))

    @property
    def scheme(self):
        return 'windows.fileid' if self.os_name == 'Windows' else 'posix.devino'

    def providers(self):
        """The provider table, in id order."""
        return [dict(provider) for provider in self._providers]

    def _provider_id(self, device):
        """One provider per device, appended the first time it is seen.

        Appending rather than editing matters: an id that already appears in
        a record must never be reinterpreted.
        """
        provider_id = self._by_source.get(device)
        if provider_id is not None:
            return provider_id

        provider_id = max((provider['id'] for provider in self._providers),
                          default=-1) + 1
        self._providers.append({
            'id': provider_id,
            'scheme': self.scheme,
            'strength': 'weak',
            # The source doubles as the provider epoch: if the identifying
            # device changes, a new id is appended and identities across the
            # two are incomparable rather than unequal.
            'source': {'device': device},
        })
        self._by_source[device] = provider_id
        return provider_id

    def identity_for(self, stat_result):
        """Return ``[provider_id, value]`` for a stat result, or None.

        os.stat is required here; DirEntry.stat() returns zeroed st_ino and
        st_dev on Windows, which would silently produce one shared identity
        for every directory on a volume.
        """
        if stat_result is None:
            return None
        device = getattr(stat_result, 'st_dev', 0)
        inode = getattr(stat_result, 'st_ino', 0)
        if not inode:
            return None
        return [self._provider_id(int(device)), f'{int(device)}:{int(inode)}']
