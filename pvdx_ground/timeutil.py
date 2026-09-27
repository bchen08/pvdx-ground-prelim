"""Small UTC datetime helpers shared by the ingest, decode and storage stages."""

from __future__ import annotations

import datetime as dt

ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> dt.datetime:
    """Current time as a timezone-aware UTC datetime (microseconds dropped)."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def parse_iso8601(value: str) -> dt.datetime:
    """Parse an ISO-8601 timestamp (``Z`` or offset); naive values are taken as UTC."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def to_iso_z(value: dt.datetime) -> str:
    """Format a datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC), the form the SatNOGS API uses."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime(ISO_Z)
