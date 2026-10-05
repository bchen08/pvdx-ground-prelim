"""Configuration for the pipeline and the service, read from environment variables and an optional ``.env``.

Nothing here talks to the network; ``Settings.from_env`` only validates and normalises values.
Secrets (``SATNOGS_API_TOKEN``, ``INFLUX_TOKEN``, ``INGEST_TOKEN``, ``PUSH_TOKEN``) are read from the
environment and never logged.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from pvdx_ground.timeutil import parse_iso8601, utcnow

_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
# The field part of a TELEMETRY_ALIASES entry (the unit is split off first): field[*scale][(+|-)offset].
# A field never contains "*" and never ends in "+", "-" or a space, so "x*abc" or "x+" is an error, not a name.
_ALIAS_TARGET = re.compile(
    rf"^(?P<field>[^*]*?[^*+\s-])\s*(?:\*\s*(?P<scale>[+-]?{_NUMBER}))?\s*(?P<offset>[+-]\s*{_NUMBER})?$"
)
# Fastest pacing SatNOGS allows: 240 list requests/hour with a token (AUTH_REQUESTS_PER_HOUR in ingest/client.py).
_MIN_INGEST_INTERVAL = 3600 / 240


class ConfigError(ValueError):
    """Raised when required configuration is missing or malformed."""


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Load ``KEY=VALUE`` pairs from a dotenv file into ``os.environ``.

    Supports ``#`` comment lines, an optional ``export`` prefix, single/double quoted values and
    trailing `` # comments`` on unquoted values. Existing environment variables win unless
    ``override`` is true. Returns the parsed pairs. A missing file is not an error.
    """
    file = Path(path)
    parsed: dict[str, str] = {}
    if not file.is_file():
        return parsed
    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _DOTENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if value[:1] in ("'", '"'):
            closing = value.find(value[0], 1)
            value = value[1:closing] if closing != -1 else value[1:]  # anything after the closing quote is a comment
        else:
            value = value.split(" #", 1)[0].rstrip()
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


def _get(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    value = env.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _number(env: Mapping[str, str], key: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):  # nan would pass any minimum (every comparison is false)
        raise ConfigError(f"{key} must be a finite number, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum:g}, got {raw!r}")
    return value


def is_number(value: Any) -> bool:
    """True for ints and floats; bools and strings are not numbers here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class TelemetryAlias:
    """One ``TELEMETRY_ALIASES`` target: a decoded field, optionally converted to an engineering unit."""

    field: str
    scale: float = 1.0
    offset: float = 0.0
    unit: str | None = None

    @property
    def converts(self) -> bool:
        """True when the alias scales or offsets the raw value instead of passing it through."""
        return self.scale != 1.0 or self.offset != 0.0

    def convert(self, value: Any) -> Any:
        """The alias value for the raw ``value``: ``value * scale + offset``.

        Without a scale or offset the value passes through unchanged (strings too). Converted values are
        floats rounded to 12 significant digits, which drops the binary rounding error of the arithmetic
        (``163 * 0.01`` is ``1.6300000000000001``) and keeps far more precision than any sensor has.
        Returns ``None`` when a converting alias gets something that is not a finite number.
        """
        if not self.converts:
            return value
        if not is_number(value):
            return None
        try:
            converted = float(f"{value * self.scale + self.offset:.12g}")
        except OverflowError:  # an int too large for a float
            return None
        return converted if math.isfinite(converted) else None


def parse_aliases(text: str | None) -> dict[str, TelemetryAlias]:
    """Parse ``TELEMETRY_ALIASES`` into ``{alias: TelemetryAlias}``.

    Entries are comma separated, each ``alias=field[*scale][(+|-)offset][:unit]``:
    ``battery=psu_battery*0.001:V`` serves ``psu_battery`` (mV) as ``battery`` in volts, and
    ``signal_rssi=uhf_act_rssi_raw*0.5-134:dBm`` applies an offset after the scale. A plain
    ``alias=field`` passes the raw value through unchanged, as before. Field names cannot contain ``*``
    or ``:``, and a trailing ``+<number>`` or ``-<number>`` is always read as an offset.
    """
    aliases: dict[str, TelemetryAlias] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        alias, sep, target = part.partition("=")
        target, colon, unit = target.partition(":")
        match = _ALIAS_TARGET.match(target.strip())
        alias, unit = alias.strip(), unit.strip()
        if not sep or not alias or match is None or (colon and not unit):
            raise ConfigError(f"TELEMETRY_ALIASES entry {part!r} is not alias=field[*scale][(+|-)offset][:unit]")
        scale = float(match.group("scale") or 1)
        offset = float("".join((match.group("offset") or "0").split()))  # "- 134" -> -134
        if scale == 0 or not math.isfinite(scale) or not math.isfinite(offset):
            raise ConfigError(f"TELEMETRY_ALIASES entry {part!r}: scale must be non-zero and both numbers finite")
        aliases[alias] = TelemetryAlias(match.group("field").strip(), scale, offset, unit or None)
    return aliases


def parse_origins(text: str | None) -> tuple[str, ...]:
    """Parse ``API_CORS_ORIGINS`` (comma separated; ``*`` allows every origin)."""
    return tuple(o.strip() for o in (text or "").split(",") if o.strip())


@dataclass(frozen=True)
class Settings:
    """All runtime settings. Construct with ``Settings.from_env()``."""

    norad_cat_id: int | None
    satnogs_api_token: str | None
    satnogs_network_url: str
    ingest_start: dt.datetime
    ingest_overlap: dt.timedelta
    ingest_status: str
    ingest_min_interval: float | None
    state_db: Path
    decoder: str | None
    influx_url: str
    influx_org: str
    influx_bucket: str
    influx_token: str | None
    # -- service (pvdx-serve / HTTP API) --
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    api_cors_origins: tuple[str, ...] = ("http://localhost:3000",)
    ingest_token: str | None = None
    ingest_poll: float = 600.0
    decode_poll: float = 60.0
    telemetry_stale_after: float = 3600.0
    telemetry_aliases: Mapping[str, TelemetryAlias] = dc_field(default_factory=dict)
    # -- Redis latest-values publisher --
    redis_url: str | None = None
    redis_key_prefix: str = "telemetry:"
    redis_telemetry_ttl: int = 3600
    # -- pvdx-push (ground station side) --
    push_url: str | None = None
    push_station: str | None = None
    push_token: str | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_norad: bool = True,
        now: dt.datetime | None = None,
    ) -> "Settings":
        """Build settings from ``env`` (default ``os.environ``); raises ``ConfigError`` on bad input."""
        env = os.environ if env is None else env
        now = now or utcnow()

        norad_raw = _get(env, "NORAD_CAT_ID")
        norad: int | None = None
        if norad_raw is not None:
            try:
                norad = int(norad_raw)
            except ValueError as exc:
                raise ConfigError(f"NORAD_CAT_ID must be an integer, got {norad_raw!r}") from exc
        elif require_norad:
            raise ConfigError("NORAD_CAT_ID is not set (put it in .env or pass --norad)")

        start_raw = _get(env, "INGEST_START")
        try:
            ingest_start = parse_iso8601(start_raw) if start_raw else now - dt.timedelta(days=7)
        except ValueError as exc:
            raise ConfigError(f"INGEST_START is not ISO-8601: {start_raw!r}") from exc

        overlap_raw = _get(env, "INGEST_OVERLAP_HOURS", "48")
        try:
            overlap = dt.timedelta(hours=float(overlap_raw))
        except ValueError as exc:
            raise ConfigError(f"INGEST_OVERLAP_HOURS must be a number, got {overlap_raw!r}") from exc
        if overlap < dt.timedelta(0):
            raise ConfigError("INGEST_OVERLAP_HOURS must be >= 0")

        min_interval: float | None = None
        if _get(env, "INGEST_MIN_INTERVAL") is not None:
            min_interval = _number(env, "INGEST_MIN_INTERVAL", 0.0, minimum=_MIN_INGEST_INTERVAL)

        status = _get(env, "INGEST_STATUS", "good") or "good"
        if status not in {"failed", "bad", "unknown", "future", "good"}:
            raise ConfigError(f"INGEST_STATUS must be one of failed/bad/unknown/future/good, got {status!r}")

        api_port = int(_number(env, "API_PORT", 8080, minimum=1))
        if api_port > 65535:
            raise ConfigError("API_PORT must be <= 65535")

        # Checked here so a typo stops the service at startup instead of killing the decode worker later.
        # The values are not echoed: a URL can carry a password.
        influx_url = _get(env, "INFLUX_URL", "http://localhost:8086") or "http://localhost:8086"
        if not influx_url.lower().startswith(("http://", "https://")):
            raise ConfigError("INFLUX_URL must start with http:// or https:// (e.g. http://localhost:8086)")
        redis_url = _get(env, "REDIS_URL")
        if redis_url is not None and not redis_url.lower().startswith(("redis://", "rediss://", "unix://")):
            raise ConfigError("REDIS_URL must start with redis://, rediss:// or unix:// (e.g. redis://localhost:6379/0)")

        return cls(
            norad_cat_id=norad,
            satnogs_api_token=_get(env, "SATNOGS_API_TOKEN"),
            satnogs_network_url=(_get(env, "SATNOGS_NETWORK_URL", "https://network.satnogs.org") or "").rstrip("/"),
            ingest_start=ingest_start,
            ingest_overlap=overlap,
            ingest_status=status,
            ingest_min_interval=min_interval,
            state_db=Path(_get(env, "STATE_DB", "data/state.db") or "data/state.db"),
            decoder=_get(env, "DECODER"),
            influx_url=influx_url,
            influx_org=_get(env, "INFLUX_ORG", "bse") or "bse",
            influx_bucket=_get(env, "INFLUX_BUCKET", "telemetry") or "telemetry",
            influx_token=_get(env, "INFLUX_TOKEN"),
            api_host=_get(env, "API_HOST", "127.0.0.1") or "127.0.0.1",
            api_port=api_port,
            api_cors_origins=parse_origins(_get(env, "API_CORS_ORIGINS", "http://localhost:3000")),
            ingest_token=_get(env, "INGEST_TOKEN"),
            ingest_poll=_number(env, "INGEST_POLL", 600.0, minimum=1),
            decode_poll=_number(env, "DECODE_POLL", 60.0, minimum=1),
            telemetry_stale_after=_number(env, "TELEMETRY_STALE_AFTER", 3600.0, minimum=0),
            telemetry_aliases=parse_aliases(_get(env, "TELEMETRY_ALIASES")),
            redis_url=redis_url,
            redis_key_prefix=_get(env, "REDIS_KEY_PREFIX", "telemetry:") or "telemetry:",
            redis_telemetry_ttl=int(_number(env, "REDIS_TELEMETRY_TTL", 3600, minimum=1)),
            push_url=(_get(env, "PUSH_URL") or "").rstrip("/") or None,
            push_station=_get(env, "PUSH_STATION"),
            push_token=_get(env, "PUSH_TOKEN"),
        )
