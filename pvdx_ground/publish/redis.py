"""Publish the latest decoded telemetry values to Redis for the web app backend.

This is the telemetry half of GS3 in the comms design (a latest-value cache); there is no command queue.

Keys (default prefix ``telemetry:``), all written with the same TTL so a silent satellite makes them expire:

* ``telemetry:<field>``  the newest value of every decoded field as a string, raw (``"8160"``, ``"SAFE"``)
* ``telemetry:<alias>``  the value of an aliased field under its name from ``TELEMETRY_ALIASES``, converted
  when the alias has a scale or offset (``battery=psu_battery*0.001:V`` publishes ``"8.16"``)
* ``telemetry:_meta``    JSON: satellite, newest frame time/id/source/station, published_at, aliases
  (alias -> field), units (alias -> unit, for aliases that name one), ttl
* ``telemetry:_all``     JSON: every field with value, frame_time, frame_id, source and station_name (raw)

The TTL counts from the publish, not from the frame time: the startup warm-up and every decode pass that
writes at least one decoded frame to InfluxDB (even an old, backfilled one or a duplicate copy) re-arm it
on every key, however old the values are.

The pvdx-mission-control backend reads ``telemetry:<name>`` for its display fields and reports "stale"
when a key is missing, so publishing under the aliases it expects makes it show real values, in the
units the aliases convert to. Nothing publishes its ``elevation`` key, so it still reports "stale".
A converting alias whose field holds a string is left out (and logged once), never published raw.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Mapping
from typing import Any

from pvdx_ground.config import TelemetryAlias, is_number
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
        aliases: Mapping[str, TelemetryAlias | str] | None = None,
        client: Any | None = None,
        socket_timeout: float = 3.0,
    ) -> None:
        self.url = url
        self.prefix = prefix
        self.ttl = int(ttl)
        # a plain field name is an alias without conversion, as TELEMETRY_ALIASES=alias=field
        self.aliases = {
            alias: TelemetryAlias(spec) if isinstance(spec, str) else spec for alias, spec in (aliases or {}).items()
        }
        self._checked: set[int] = set()  # satellites published at least once (missing aliases logged)
        self._not_numbers: set[str] = set()  # aliases already logged for a non-numeric field
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

    def alias_values(self, norad_cat_id: int, latest: Mapping[str, LatestValue]) -> dict[str, Any]:
        """The value of every alias whose field has one, converted to the alias's unit.

        The first call for a satellite (normally the startup warm-up) warns about aliases whose field has no
        value yet. A field that is not a number is warned about once per alias: a converting alias is then left
        out, a plain one keeps the raw value.
        """
        first = norad_cat_id not in self._checked
        self._checked.add(norad_cat_id)
        values: dict[str, Any] = {}
        for alias, spec in self.aliases.items():
            current = latest.get(spec.field)
            if current is None:
                log.log(logging.WARNING if first else logging.DEBUG,
                        "alias %s: field %s has no value yet for NORAD %d", alias, spec.field, norad_cat_id)
                continue
            value = spec.convert(current.value)
            if not is_number(current.value) and alias not in self._not_numbers:
                self._not_numbers.add(alias)
                log.warning("alias %s: field %s holds %r, not a number%s", alias, spec.field, current.value,
                            "; the alias is not published" if value is None else "")
            if value is not None:
                values[alias] = value
        return values

    def publish(self, norad_cat_id: int, latest: Mapping[str, LatestValue], *, now: dt.datetime) -> int:
        """Write every field, the aliases and the two JSON summaries; returns the number of keys written."""
        if not latest:
            return 0
        newest = max(latest.values(), key=lambda v: (v.frame_time, v.frame_id))
        values = {name: value_to_str(v.value) for name, v in latest.items()}
        values.update({alias: value_to_str(v) for alias, v in self.alias_values(norad_cat_id, latest).items()})
        meta = {
            "norad_cat_id": norad_cat_id,
            "frame_time": to_iso_z(newest.frame_time),
            "frame_id": newest.frame_id,
            "source": newest.source,
            "station_name": newest.station_name,
            "published_at": to_iso_z(now),
            "ttl_seconds": self.ttl,
            "fields": len(latest),
            "aliases": {alias: spec.field for alias, spec in self.aliases.items()},
            "units": {alias: spec.unit for alias, spec in self.aliases.items() if spec.unit},
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
