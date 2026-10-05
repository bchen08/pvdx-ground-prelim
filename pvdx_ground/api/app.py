"""The HTTP API: latest telemetry, history, frames, observations, health, and the endpoint our own ground
station pushes raw frames to.

Every request opens its own short-lived SQLite connection and closes it when the request ends; no two
requests share one, and WAL mode lets them run beside the ingest and decode threads of ``pvdx-serve`` or
beside the standalone CLIs. FastAPI opens, uses and closes that connection on different threadpool
threads (one step after another), so it is opened without SQLite's same-thread check (see ``get_store``).
Authentication is out of scope for now; the only guard is the optional shared secret ``INGEST_TOKEN``
(header ``X-Ingest-Token``) on the push endpoint, checked before the request body is read, so that a
public deployment does not accept frames from anyone.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hmac
import logging
import re
import threading
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints
from starlette.types import ASGIApp, Receive, Scope, Send

from pvdx_ground import __version__
from pvdx_ground.config import Settings, TelemetryAlias
from pvdx_ground.ingest.state import SCHEMA_VERSION, StateStore, json_path
from pvdx_ground.timeutil import parse_iso8601, to_iso_z, utcnow

log = logging.getLogger(__name__)

MAX_PUSH_FRAMES = 1000
MAX_FRAME_BYTES = 64 * 1024
MAX_LIST = 1000
MAX_SQL_INT = 2**63 - 1  # largest SQLite INTEGER; sqlite3 raises OverflowError beyond it
MAX_NORAD = 999_999_999  # NORAD catalog numbers have at most nine digits
PUSH_PATH = "/ingest/frames"
_BAD_TOKEN = "missing or invalid X-Ingest-Token"
_RELATIVE = re.compile(r"^-(\d+(?:\.\d+)?)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
SOURCE_PATTERN = "^(satnogs|groundstation)$"
NoradQuery = Annotated[int | None, Query(ge=1, le=MAX_NORAD)]


def parse_time_param(value: str | None, name: str) -> dt.datetime | None:
    """ISO-8601 (``2026-09-26T00:00:00Z``) or relative (``-6h``, ``-2d``) query parameter."""
    if value is None:
        return None
    try:
        match = _RELATIVE.match(value.strip())
        if match:
            return utcnow() - dt.timedelta(seconds=float(match.group(1)) * _UNITS[match.group(2)])
        return parse_iso8601(value)
    except (ValueError, OverflowError) as exc:  # OverflowError: outside datetime's range (years 1-9999)
        raise HTTPException(400, f"{name} must be ISO-8601 or relative like -6h, got {value!r}") from exc


def token_matches(given: str | None, expected: str) -> bool:
    """Constant-time comparison of a presented ingest token with the configured one."""
    return given is not None and hmac.compare_digest(given.encode(), expected.encode())


class IngestTokenMiddleware:
    """Reject ``POST /ingest/frames`` without the right ``X-Ingest-Token`` before its body is read.

    The endpoint checks the token too, but FastAPI reads and validates the whole body first. The 401 is the
    same one the endpoint returns.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == PUSH_PATH:
            given = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-ingest-token"), None)
            if not token_matches(given, self.token):
                await JSONResponse({"detail": _BAD_TOKEN}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class PushedFrame(BaseModel):
    """One frame received by our ground station."""

    raw: str = Field(description="frame bytes, base64 (standard alphabet, padding optional)")
    received_at: dt.datetime = Field(description="when the station received the frame (UTC if no offset)")
    frequency: int | None = Field(default=None, description="downlink frequency in Hz, if known")
    rssi: float | None = Field(default=None, description="received signal strength in dBm, if known")
    meta: dict[str, Any] = Field(default_factory=dict, description="any other per-frame metadata")


class PushRequest(BaseModel):
    """Body of ``POST /ingest/frames``."""

    norad_cat_id: int = Field(gt=0, le=MAX_NORAD, description="satellite the frames belong to")
    station: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] = Field(
        description="name of the receiving station (surrounding whitespace is stripped)"
    )
    frames: list[PushedFrame] = Field(min_length=1, max_length=MAX_PUSH_FRAMES)


def check_influx(settings: Settings) -> str:
    if not settings.influx_token:
        return "not configured"
    try:
        from pvdx_ground.storage import InfluxWriter

        writer = InfluxWriter(
            settings.influx_url, settings.influx_token, settings.influx_org, settings.influx_bucket, timeout_ms=3000
        )
        try:
            problem = writer.check_access()
        finally:
            writer.close()
    except Exception as exc:  # noqa: BLE001 - report, never crash the health endpoint
        return f"error: {exc}"
    return "ok" if problem is None else f"error: {problem}"


def check_redis(settings: Settings) -> str:
    if not settings.redis_url:
        return "not configured"
    try:
        from pvdx_ground.publish.redis import RedisPublisher

        publisher = RedisPublisher(settings.redis_url, prefix=settings.redis_key_prefix, socket_timeout=2.0)
        try:
            problem = publisher.check()
        finally:
            publisher.close()
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"
    return "ok" if problem is None else f"error: {problem}"


def create_app(
    settings: Settings,
    *,
    wake: threading.Event | None = None,
    workers: dict[str, threading.Thread] | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``wake`` is set whenever frames are pushed so an in-process decode loop can run at once; ``workers``
    are the background threads whose liveness the health endpoint reports.
    """
    app = FastAPI(
        title="PVDX ground telemetry service",
        version=__version__,
        description=(
            "Cloud side of the PVDX ground software: frames from the SatNOGS Network and from the BSE ground "
            "station, decoded telemetry, provenance and health. Brown Space Engineering."
        ),
    )
    if settings.ingest_token:  # added first so CORS wraps it and its 401 carries the same CORS headers
        app.add_middleware(IngestTokenMiddleware, token=settings.ingest_token)
    if settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.api_cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )
    app.state.settings = settings
    app.state.wake = wake
    app.state.workers = workers if workers is not None else {}
    if not settings.ingest_token:
        log.warning("INGEST_TOKEN is not set: POST /ingest/frames accepts frames from anyone")
    alias_fields = {alias: spec.field for alias, spec in settings.telemetry_aliases.items()}
    alias_units = {alias: spec.unit for alias, spec in settings.telemetry_aliases.items() if spec.unit}
    not_numbers: set[str] = set()  # aliases already logged for a non-numeric field

    def get_store() -> Iterator[StateStore]:
        # FastAPI runs a sync generator dependency's setup, the endpoint and the teardown on different
        # threadpool threads, but one after another, so this per-request connection is never used
        # concurrently. The worker threads keep SQLite's same-thread check.
        with StateStore(settings.state_db, check_same_thread=False) as store:
            yield store

    def resolve_norad(store: StateStore, norad: int | None) -> int:
        if norad is not None:
            return norad
        if settings.norad_cat_id is not None:
            return settings.norad_cat_id
        satellites = store.satellites()
        if len(satellites) == 1:
            return int(satellites[0]["norad_cat_id"])
        raise HTTPException(
            400,
            f"norad query parameter is required (NORAD_CAT_ID is not configured and the database holds "
            f"{len(satellites)} satellites)",
        )

    def alias_value(alias: str, spec: TelemetryAlias, value: Any) -> Any:
        """``value`` converted for ``alias``; ``None`` (logged once per alias) when it cannot be converted."""
        converted = spec.convert(value)
        if converted is None and value is not None and alias not in not_numbers:
            not_numbers.add(alias)
            log.warning("alias %s: field %s holds %r, not a number; serving it as null", alias, spec.field, value)
        return converted

    def latest_payload(store: StateStore, norad: int, now: dt.datetime) -> dict[str, Any]:
        latest = store.latest_values(norad)
        newest = max(latest.values(), key=lambda v: (v.frame_time, v.frame_id)) if latest else None
        age = (now - newest.frame_time).total_seconds() if newest else None
        return {
            "norad_cat_id": norad,
            "stale": newest is None or age is None or age >= settings.telemetry_stale_after,
            "stale_after_seconds": settings.telemetry_stale_after,
            "newest_frame_time": to_iso_z(newest.frame_time) if newest else None,
            "newest_frame_id": newest.frame_id if newest else None,
            "newest_source": newest.source if newest else None,
            "newest_station": newest.station_name if newest else None,
            "age_seconds": age,
            "fields": {name: value.as_dict() for name, value in latest.items()},
            "aliases": alias_fields,
        }

    # -- service -----------------------------------------------------------------------------------
    @app.get("/", summary="Service description")
    def root() -> dict[str, Any]:
        return {
            "service": "pvdx-ground",
            "version": __version__,
            "norad_cat_id": settings.norad_cat_id,
            "decoder": settings.decoder,
            "endpoints": [
                "GET /health", "GET /telemetry", "GET /telemetry/latest", "GET /telemetry/fields",
                "GET /telemetry/history?field=", "GET /frames", "GET /frames/{id}", "GET /observations",
                "GET /observations/{id}", "POST /ingest/frames", "GET /docs",
            ],
        }

    @app.get("/health", summary="Liveness of the service and its dependencies")
    def health(store: StateStore = Depends(get_store)) -> dict[str, Any]:
        satellites = store.satellites()
        ingest: dict[str, Any] = {}
        for sat in satellites:
            sweep = store.last_sweep(int(sat["norad_cat_id"]))
            ingest[str(sat["norad_cat_id"])] = {
                "watermark": sat["watermark"],
                "last_sweep": None if sweep is None else {
                    "id": sweep.id, "state": sweep.state, "started_at": to_iso_z(sweep.started_at),
                    "since": to_iso_z(sweep.since), "pages_done": sweep.pages_done,
                    "observations_seen": sweep.observations_seen,
                },
            }
        workers = {
            name: ("running" if thread.is_alive() else "stopped") for name, thread in app.state.workers.items()
        }
        influx, redis = check_influx(settings), check_redis(settings)
        degraded = [w for w, s in workers.items() if s == "stopped"]
        degraded += [n for n, s in (("influxdb", influx), ("redis", redis)) if s.startswith("error")]
        return {
            "status": "degraded" if degraded else "ok",
            "problems": degraded,
            "time": to_iso_z(utcnow()),
            "version": __version__,
            "state_db": str(settings.state_db),
            "schema_version": SCHEMA_VERSION,
            "satellites": satellites,
            "ingest": ingest,
            "workers": workers,
            "influxdb": influx,
            "redis": redis,
        }

    # -- telemetry ---------------------------------------------------------------------------------
    @app.get("/telemetry", summary="Latest values, flat (compatible with the mission-control backend)")
    def telemetry(norad: NoradQuery = None, store: StateStore = Depends(get_store)) -> dict[str, Any]:
        """Raw field values plus the ``TELEMETRY_ALIASES`` aliases, converted to their units (``units``)."""
        norad = resolve_norad(store, norad)
        payload = latest_payload(store, norad, utcnow())
        values: dict[str, Any] = {name: entry["value"] for name, entry in payload["fields"].items()}
        for alias, spec in settings.telemetry_aliases.items():
            entry = payload["fields"].get(spec.field)
            values[alias] = alias_value(alias, spec, entry["value"]) if entry else None
        return {
            "telemetry": values,
            "units": alias_units,
            "stale": payload["stale"],
            "norad_cat_id": norad,
            "frame_time": payload["newest_frame_time"],
            "source": payload["newest_source"],
        }

    @app.get("/telemetry/latest", summary="Latest value of every field with its provenance")
    def telemetry_latest(norad: NoradQuery = None, store: StateStore = Depends(get_store)) -> dict[str, Any]:
        """Raw decoded values as stored (a provenance view).

        Aliases are converted to their units only in ``/telemetry`` and ``/telemetry/history``.
        """
        return latest_payload(store, resolve_norad(store, norad), utcnow())

    @app.get("/telemetry/fields", summary="Known field names and when each was last seen")
    def telemetry_fields(norad: NoradQuery = None, store: StateStore = Depends(get_store)) -> dict[str, Any]:
        """Decoded field names with the type of their raw value (a provenance view; aliases are not listed)."""
        norad = resolve_norad(store, norad)
        latest = store.latest_values(norad)
        return {
            "norad_cat_id": norad,
            "fields": [
                {"field": name, "last_frame_time": to_iso_z(v.frame_time), "source": v.source,
                 "type": "string" if isinstance(v.value, str) else "number"}
                for name, v in latest.items()
            ],
        }

    @app.get("/telemetry/history", summary="One field over time")
    def telemetry_history(
        field: str = Query(min_length=1, max_length=200, description=(
            "field name (raw values) or a TELEMETRY_ALIASES alias (values converted to its unit)"
        )),
        norad: NoradQuery = None,
        since: str | None = Query(None, description="ISO-8601 or relative (-6h, -2d)"),
        until: str | None = None,
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
        copies: bool = Query(False, description=(
            "include every reception, not one point per transmission; with source, also the receptions from "
            "that source whose transmission has its primary elsewhere"
        )),
        limit: int = Query(1000, ge=1, le=10_000),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        norad = resolve_norad(store, norad)
        spec = settings.telemetry_aliases.get(field)  # an alias wins, as in /telemetry
        stored_field = spec.field if spec is not None else field
        try:
            json_path(stored_field)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        points = store.history(
            stored_field, norad_cat_id=norad, since=parse_time_param(since, "since"),
            until=parse_time_param(until, "until"), source=source, copies=copies, limit=limit,
        )
        if spec is not None:
            for point in points:
                point["value"] = alias_value(field, spec, point["value"])
        return {
            "norad_cat_id": norad, "field": field, "stored_field": stored_field,
            "unit": spec.unit if spec is not None else None, "copies": copies, "count": len(points), "points": points,
        }

    # -- frames and observations -------------------------------------------------------------------
    @app.get("/frames", summary="Stored frames (metadata only), newest first")
    def frames(
        norad: NoradQuery = None,
        since: str | None = None,
        until: str | None = None,
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
        station: str | None = Query(None, description="station name, exact"),
        decode_status: str | None = Query(None, pattern="^(ok|error|empty|pending)$"),
        primary: bool | None = Query(None, description="true: primaries only; false: copies and unassigned frames"),
        limit: int = Query(100, ge=1, le=MAX_LIST),
        offset: int = Query(0, ge=0, le=MAX_SQL_INT),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        items = store.list_frames(
            norad_cat_id=norad, since=parse_time_param(since, "since"), until=parse_time_param(until, "until"),
            source=source, station_name=station, decode_status=decode_status, primary=primary, limit=limit,
            offset=offset,
        )
        return {"count": len(items), "limit": limit, "offset": offset, "frames": items}

    @app.get("/frames/{frame_id}", summary="One frame with its bytes and decoded fields")
    def frame(frame_id: int = Path(ge=1, le=MAX_SQL_INT), store: StateStore = Depends(get_store)) -> dict[str, Any]:
        item = store.get_frame(frame_id)
        if item is None:
            raise HTTPException(404, f"frame {frame_id} not found")
        raw = item.pop("raw")
        item["raw_base64"] = base64.b64encode(raw).decode("ascii")
        item["raw_hex"] = raw.hex()
        return item

    @app.get("/observations", summary="SatNOGS observations (provenance), newest first")
    def observations(
        norad: NoradQuery = None,
        since: str | None = None,
        until: str | None = None,
        ground_station: int | None = Query(None, ge=1, le=MAX_SQL_INT),
        limit: int = Query(100, ge=1, le=MAX_LIST),
        offset: int = Query(0, ge=0, le=MAX_SQL_INT),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        items = store.list_observations(
            norad_cat_id=norad, since=parse_time_param(since, "since"), until=parse_time_param(until, "until"),
            ground_station=ground_station, limit=limit, offset=offset,
        )
        return {"count": len(items), "limit": limit, "offset": offset, "observations": items}

    @app.get("/observations/{observation_id}", summary="One observation with the full SatNOGS record")
    def observation(
        observation_id: int = Path(ge=1, le=MAX_SQL_INT),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        item = store.get_observation(observation_id)
        if item is None:
            raise HTTPException(404, f"observation {observation_id} not found")
        return item

    # -- ingest from our own ground station --------------------------------------------------------
    @app.post(PUSH_PATH, summary="Store raw frames received by our ground station")
    def push_frames(
        body: PushRequest,
        x_ingest_token: str | None = Header(default=None),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        if settings.ingest_token and not token_matches(x_ingest_token, settings.ingest_token):
            raise HTTPException(401, _BAD_TOKEN)
        now = utcnow()
        results: list[dict[str, Any]] = []
        accepted = duplicates = 0
        for index, pushed in enumerate(body.frames):
            try:
                raw = base64.b64decode(pushed.raw + "=" * (-len(pushed.raw) % 4), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(400, f"frames[{index}].raw is not valid base64") from exc
            if not raw:
                raise HTTPException(400, f"frames[{index}].raw is empty")
            if len(raw) > MAX_FRAME_BYTES:
                raise HTTPException(413, f"frames[{index}].raw is {len(raw)} bytes; the limit is {MAX_FRAME_BYTES}")
            received = pushed.received_at
            if received.tzinfo is None:
                received = received.replace(tzinfo=dt.timezone.utc)
            try:
                frame_time = received.astimezone(dt.timezone.utc)
            except OverflowError as exc:  # e.g. 9999-12-31T23:59:59-01:00 is past datetime.max in UTC
                raise HTTPException(400, f"frames[{index}].received_at is out of range") from exc
            meta = dict(pushed.meta)
            if pushed.frequency is not None:
                meta["frequency"] = pushed.frequency
            if pushed.rssi is not None:
                meta["rssi"] = pushed.rssi
            frame_id, is_new = store.add_pushed_frame(
                norad_cat_id=body.norad_cat_id, station_name=body.station, raw=raw,
                frame_time=frame_time, received_at=now, meta=meta,
            )
            accepted += int(is_new)
            duplicates += int(not is_new)
            results.append({"id": frame_id, "new": is_new, "size": len(raw)})
        if settings.norad_cat_id is not None and body.norad_cat_id != settings.norad_cat_id:
            log.warning(
                "stored %d pushed frame(s) for NORAD %d, but this service decodes NORAD %d only",
                len(body.frames), body.norad_cat_id, settings.norad_cat_id,
            )
        if accepted and app.state.wake is not None:
            app.state.wake.set()
        log.info("push from %s: %d frame(s) stored, %d duplicate(s) (NORAD %d)",
                 body.station, accepted, duplicates, body.norad_cat_id)
        return {"accepted": accepted, "duplicates": duplicates, "frames": results}

    return app
