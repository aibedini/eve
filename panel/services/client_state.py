"""The one normalized shape for a client, and the result of a mutation.

Every Eve operation that writes to an X-UI panel (create, edit, renew, enable,
disable, reset, delete, rotate identifiers) ends with the same question: what is
the client's state now, and may the UI adopt it? This module answers both with one
vocabulary so no route hand-rolls its own dict:

* `normalize_client_state()` — the canonical client shape (uuid, email, enable,
  total/used/remaining bytes, expiry, service state, inbound).
* `ClientMutationResult` — the operation's outcome plus that canonical state.

The rule the shape encodes: an X-UI write is not UI state until the panel has been
read back. `ClientMutationResult.to_payload()` therefore only exposes
`client_state` when `verified` (the caller read the panel) or `deleted` (a delete
needs no read-back -- absence is the state). Otherwise the field is null and the
browser must keep its normal polling path instead of adopting unverified values.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The fields every normalized client state carries. The last three are purely
#: presentational: they let a mutation response repaint the status badge instead of
#: blanking it until the next refresh.
CLIENT_STATE_FIELDS = (
    'uuid', 'email', 'enable', 'total_bytes', 'used_up', 'used_down',
    'remaining_bytes', 'expiry_time', 'service_state', 'inbound_id',
    'service_state_label', 'service_state_emoji', 'service_state_tag',
    'config_updated_at', 'telemetry_updated_at',
)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_client_state(*, raw=None, row=None, service_state=None, inbound_id=None,
                           used_up=None, used_down=None, remaining_bytes=None,
                           service_state_label=None, service_state_emoji=None,
                           service_state_tag=None):
    """Return the canonical client shape from a cached row and/or a raw client.

    `row` is a cached display row (it carries usage and the computed service
    state); `raw` is the panel's client object (it carries the configuration).
    Callers with a fresh panel read pass `raw` plus any usage they observed; the
    function never guesses a field it was not given.
    """
    raw = raw if isinstance(raw, dict) else {}
    row = row if isinstance(row, dict) else {}
    row_raw = row.get('raw_client') if isinstance(row.get('raw_client'), dict) else {}

    def pick(key, *sources):
        for source in sources:
            if isinstance(source, dict) and source.get(key) is not None:
                return source[key]
        return None

    total_bytes = _as_int(pick('totalGB', raw, row_raw, row), 0)
    up = _as_int(used_up if used_up is not None else pick('up', row, row_raw), 0)
    down = _as_int(used_down if used_down is not None else pick('down', row, row_raw), 0)
    if remaining_bytes is not None:
        remaining = _as_int(remaining_bytes, 0)
    elif row.get('remaining_bytes') is not None:
        remaining = _as_int(row.get('remaining_bytes'), -1)
    elif total_bytes > 0:
        remaining = max(total_bytes - (up + down), 0)
    else:
        remaining = -1

    return {
        'uuid': pick('id', raw, row_raw, row),
        'email': pick('email', raw, row_raw, row),
        'enable': bool(pick('enable', raw, row_raw, row) if pick(
            'enable', raw, row_raw, row) is not None else True),
        'total_bytes': total_bytes,
        'used_up': up,
        'used_down': down,
        'remaining_bytes': remaining,
        'expiry_time': _as_int(pick('expiryTime', raw, row_raw, row), 0),
        'service_state': (service_state if service_state is not None
                          else row.get('service_state')),
        'inbound_id': (inbound_id if inbound_id is not None
                       else row.get('inbound_id')),
        'service_state_label': (service_state_label if service_state_label is not None
                                else row.get('service_state_label')),
        'service_state_emoji': (service_state_emoji if service_state_emoji is not None
                                else row.get('service_state_emoji')),
        'service_state_tag': (service_state_tag if service_state_tag is not None
                              else row.get('service_state_tag')),
        # Phase 6: configuration and telemetry age independently; the UI can see which
        # layer a value came from instead of trusting one shared "last update".
        'config_updated_at': row.get('config_updated_at'),
        'telemetry_updated_at': row.get('telemetry_updated_at'),
    }


@dataclass(frozen=True)
class ClientMutationResult:
    """Outcome of one panel mutation, with the canonical state when verified.

    `changed` reports whether the write-through updated a cached row; it is also
    the truthiness of the object, so `if patch_cached_client(...)` keeps working.
    """

    server_id: int
    email: str
    operation: str
    client_id: str | None = None
    verified: bool = False
    deleted: bool = False
    client_state: dict | None = None
    server_revision: int = 0
    changed: bool = False
    #: The snapshot (delta-sync) revision this mutation landed at. The browser's poll
    #: cursor is a snapshot revision, not the per-server counter, so this is what it
    #: advances to.
    snapshot_revision: int = 0

    def __bool__(self) -> bool:
        return bool(self.changed)

    def to_payload(self) -> dict:
        """The wire shape the browser consumes. Unverified state stays null."""
        adoptable = self.client_state if (self.verified or self.deleted) else None
        return {
            'operation': self.operation,
            'server_id': self.server_id,
            'client_id': self.client_id,
            'email': self.email,
            'verified': bool(self.verified),
            'deleted': bool(self.deleted),
            'changed': bool(self.changed),
            'server_revision': self.server_revision,
            'snapshot_revision': self.snapshot_revision,
            'client_state': adoptable,
        }


def verified_state_from_panel(raw, *, up=0, down=0, service_state=None, inbound_id=None,
                              service_state_label=None, service_state_emoji=None,
                              service_state_tag=None) -> dict:
    """Normalize a client object read back from the panel after a write."""
    return normalize_client_state(
        raw=raw, service_state=service_state, inbound_id=inbound_id,
        service_state_label=service_state_label,
        service_state_emoji=service_state_emoji,
        service_state_tag=service_state_tag,
        used_up=up, used_down=down,
        remaining_bytes=(max(_as_int(raw.get('totalGB'), 0) - (up + down), 0)
                         if _as_int(raw.get('totalGB'), 0) > 0 else -1),
    )
