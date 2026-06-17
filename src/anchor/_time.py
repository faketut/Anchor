"""Shared datetime helpers.

Lives in its own module (no `config` dep) because it's used by three
unrelated trust boundaries — CLI, MCP server, and HTTP skill server — and
we don't want a circular import.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone


def ensure_aware(dt: datetime) -> datetime:
    """Return a timezone-aware datetime.

    Naive datetimes are silently promoted to UTC with a stderr warning. We
    don't reject because demo flows happily pass `2026-06-14T12:00` (no
    offset) and we'd rather the demo work than 400 the caller; in
    production the warning ends up in container logs so the operator can
    fix the source.
    """
    if dt.tzinfo is None:
        print(
            f"[anchor._time] warn: naive datetime {dt.isoformat()!r} promoted to UTC; "
            "callers should pass aware datetimes (ISO 8601 with offset or 'Z').",
            file=sys.stderr,
        )
        return dt.replace(tzinfo=timezone.utc)
    return dt
