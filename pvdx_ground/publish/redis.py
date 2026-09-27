"""Publish the latest decoded telemetry values to Redis for the web app backend (GS3 in the comms design).

Keys (default prefix ``telemetry:``), all written with the same TTL so a silent satellite makes them expire:

* ``telemetry:<field>``  the newest value of every decoded field as a string (``"8160"``, ``"SAFE"``)
* ``telemetry:<alias>``  the same values under the names from ``TELEMETRY_ALIASES`` (``battery=psu_battery``)
* ``telemetry:_meta``    JSON: satellite, newest frame time/id/source/station, published_at, aliases, ttl
* ``telemetry:_all``     JSON: every field with value, frame_time, frame_id, source and station_name

The pvdx-mission-control backend reads ``telemetry:<name>`` for its display fields and reports "stale"
when a key is missing, so publishing under the aliases it expects makes it show real data unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Mapping
from typing import Any

from pvdx_ground.ingest.state import LatestValue
from pvdx_ground.timeutil import to_iso_z

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Redis could not be updated (connection refused, timeout, auth ...)."""


def value_to_str(value: Any) -> str:
    """Redis stores strings; keep integers free of a trailing ``.0`` and booleans as 1/0."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class RedisPublisher:
    """Writes the latest values of one satellite to Redis in a single transaction."""

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "telemetry:",
        ttl: int = 3600,
        aliases: Mapping[str, str] | None = None,
        client: Any | None = None,
        socket_timeout: float = 3.0,
    ) -> None:
        self.url = url
        self.prefix = prefix
        self.ttl = int(ttl)
        self.aliases = dict(aliases or {})
        if client is None:
            import redis

            client = redis.Redis.from_url(
                url, decode_responses=True, socket_timeout=socket_timeout, socket_connect_timeout=socket_timeout
            )
        self._client = client

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    def check(self) -> str | None:
        """Ping Redis; returns an error message or ``None`` when it answers."""
        try:
            self._client.ping()
        except Exception as exc:  # noqa: BLE001 - any client error means "not usable"
            return f"Redis at {self.url} not reachable: {exc}"
        return None

    def publish(self, norad_cat_id: int, latest: Mapping[str, LatestValue], *, now: dt.datetime) -> int:
        """Write every field, the aliases and the two JSON summaries; returns the number of keys written."""
        if not latest:
            return 0
        newest = max(latest.values(), key=lambda v: (v.frame_time, v.frame_id))
        values = {name: value_to_str(v.value) for name, v in latest.items()}
        for alias, field in self.aliases.items():
            if field in values:
                values[alias] = values[field]
            else:
                log.debug("alias %s: field %s has no value yet", alias, field)
        meta = {
            "norad_cat_id": norad_cat_id,
            "frame_time": to_iso_z(newest.frame_time),
            "frame_id": newest.frame_id,
            "source": newest.source,
            "station_name": newest.station_name,
            "published_at": to_iso_z(now),
            "ttl_seconds": self.ttl,
            "fields": len(latest),
            "aliases": self.aliases,
        }
        everything = {name: v.as_dict() for name, v in latest.items()}
        try:
            pipe = self._client.pipeline(transaction=True)
            for name, text in values.items():
                pipe.set(self.prefix + name, text, ex=self.ttl)
            pipe.set(self.prefix + "_meta", json.dumps(meta, sort_keys=True), ex=self.ttl)
            pipe.set(self.prefix + "_all", json.dumps(everything, sort_keys=True), ex=self.ttl)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001 - redis.exceptions.* or a test double's error
            raise PublishError(f"{type(exc).__name__}: {exc}") from exc
        log.debug("published %d key(s) for NORAD %d (newest frame %s)", len(values) + 2, norad_cat_id, meta["frame_time"])
        return len(values) + 2

    def __repr__(self) -> str:
        return f"RedisPublisher({self.url!r}, prefix={self.prefix!r}, ttl={self.ttl})"
