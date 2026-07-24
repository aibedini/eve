"""Shared deferred helpers for panel.models (cycle-safe)."""


def _format_jalali(dt):
    """Deferred import: format_jalali lives in app.py (calendar settings helpers),
    which imports this package — a module-level import would be circular."""
    from app import format_jalali
    return format_jalali(dt)


def _parse_allowed_servers(raw_value):
    """Deferred import: parse_allowed_servers lives in app.py (helper cluster)."""
    from app import parse_allowed_servers
    return parse_allowed_servers(raw_value)


def _server_is_v3(server):
    """Deferred import: server_is_v3 lives in app.py (panel capability probe)."""
    from app import server_is_v3
    return server_is_v3(server)
