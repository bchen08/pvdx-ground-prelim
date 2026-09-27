"""The HTTP API: latest telemetry, history, frames, observations, health, and the endpoint our own ground
station pushes raw frames to.

Every request opens its own short-lived SQLite connection, so the API can run beside the ingest and
decode threads of ``pvdx-serve`` or beside the standalone CLIs. Authentication is out of scope for now;
the only guard is the optional shared secret ``INGEST_TOKEN`` (header ``X-Ingest-Token``) on the push
endpoint, so that a public deployment does not accept frames from anyone.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import logging
import re
import threading
from collections.abc import Iterator
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pvdx_ground import __version__
from pvdx_ground.config import Settings
from pvdx_ground.ingest.state import SCHEMA_VERSION, StateStore, json_path
from pvdx_ground.timeutil import parse_iso8601, to_iso_z, utcnow

log = logging.getLogger(__name__)

MAX_PUSH_FRAMES = 1000
MAX_FRAME_BYTES = 64 * 1024
MAX_LIST = 1000
_RELATIVE = re.compile(r"^-(\d+(?:\.\d+)?)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
SOURCE_PATTERN = "^(satnogs|groundstation)$"


def parse_time_param(value: str | None, name: str) -> dt.datetime | None:
    """ISO-8601 (``2026-09-26T00:00:00Z``) or relative (``-6h``, ``-2d``) query parameter."""
    if value is None:
        return None
    match = _RELATIVE.match(value.strip())
    if match:
        return utcnow() - dt.timedelta(seconds=float(match.group(1)) * _UNITS[match.group(2)])
    try:
        return parse_iso8601(value)
    except ValueError as exc:
        raise HTTPException(400, f"{name} must be ISO-8601 or relative like -6h, got {value!r}") from exc


class PushedFrame(BaseModel):
    """One frame received by our ground station."""

    raw: str = Field(description="frame bytes, base64 (standard alphabet, padding optional)")
    received_at: dt.datetime = Field(description="when the station received the frame (UTC if no offset)")
    frequency: int | None = Field(default=None, description="downlink frequency in Hz, if known")
    rssi: float | None = Field(default=None, description="received signal strength in dBm, if known")
    meta: dict[str, Any] = Field(default_factory=dict, description="any other per-frame metadata")


class PushRequest(BaseModel):
    """Body of ``POST /ingest/frames``."""

    norad_cat_id: int = Field(description="satellite the frames belong to")
    station: str = Field(min_length=1, max_length=120, description="name of the receiving station")
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

    def get_store() -> Iterator[StateStore]:
        with StateStore(settings.state_db) as store:
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
            "aliases": dict(settings.telemetry_aliases),
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
    def telemetry(norad: int | None = None, store: StateStore = Depends(get_store)) -> dict[str, Any]:
        norad = resolve_norad(store, norad)
        payload = latest_payload(store, norad, utcnow())
        values: dict[str, Any] = {name: entry["value"] for name, entry in payload["fields"].items()}
        for alias, field in settings.telemetry_aliases.items():
            values[alias] = payload["fields"][field]["value"] if field in payload["fields"] else None
        return {
            "telemetry": values,
            "stale": payload["stale"],
            "norad_cat_id": norad,
            "frame_time": payload["newest_frame_time"],
            "source": payload["newest_source"],
        }

    @app.get("/telemetry/latest", summary="Latest value of every field with its provenance")
    def telemetry_latest(norad: int | None = None, store: StateStore = Depends(get_store)) -> dict[str, Any]:
        return latest_payload(store, resolve_norad(store, norad), utcnow())

    @app.get("/telemetry/fields", summary="Known field names and when each was last seen")
    def telemetry_fields(norad: int | None = None, store: StateStore = Depends(get_store)) -> dict[str, Any]:
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
        field: str = Query(min_length=1, max_length=200),
        norad: int | None = None,
        since: str | None = Query(None, description="ISO-8601 or relative (-6h, -2d)"),
        until: str | None = None,
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
        limit: int = Query(1000, ge=1, le=10_000),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        norad = resolve_norad(store, norad)
        try:
            json_path(field)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        points = store.history(
            field, norad_cat_id=norad, since=parse_time_param(since, "since"),
            until=parse_time_param(until, "until"), source=source, limit=limit,
        )
        return {"norad_cat_id": norad, "field": field, "count": len(points), "points": points}

    # -- frames and observations -------------------------------------------------------------------
    @app.get("/frames", summary="Stored frames (metadata only), newest first")
    def frames(
        norad: int | None = None,
        since: str | None = None,
        until: str | None = None,
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
        station: str | None = Query(None, description="station name, exact"),
        decode_status: str | None = Query(None, pattern="^(ok|error|empty|pending)$"),
        limit: int = Query(100, ge=1, le=MAX_LIST),
        offset: int = Query(0, ge=0),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        items = store.list_frames(
            norad_cat_id=norad, since=parse_time_param(since, "since"), until=parse_time_param(until, "until"),
            source=source, station_name=station, decode_status=decode_status, limit=limit, offset=offset,
        )
        return {"count": len(items), "limit": limit, "offset": offset, "frames": items}

    @app.get("/frames/{frame_id}", summary="One frame with its bytes and decoded fields")
    def frame(frame_id: int, store: StateStore = Depends(get_store)) -> dict[str, Any]:
        item = store.get_frame(frame_id)
        if item is None:
            raise HTTPException(404, f"frame {frame_id} not found")
        raw = item.pop("raw")
        item["raw_base64"] = base64.b64encode(raw).decode("ascii")
        item["raw_hex"] = raw.hex()
        return item

    @app.get("/observations", summary="SatNOGS observations (provenance), newest first")
    def observations(
        norad: int | None = None,
        since: str | None = None,
        until: str | None = None,
        ground_station: int | None = None,
        limit: int = Query(100, ge=1, le=MAX_LIST),
        offset: int = Query(0, ge=0),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        items = store.list_observations(
            norad_cat_id=norad, since=parse_time_param(since, "since"), until=parse_time_param(until, "until"),
            ground_station=ground_station, limit=limit, offset=offset,
        )
        return {"count": len(items), "limit": limit, "offset": offset, "observations": items}

    @app.get("/observations/{observation_id}", summary="One observation with the full SatNOGS record")
    def observation(observation_id: int, store: StateStore = Depends(get_store)) -> dict[str, Any]:
        item = store.get_observation(observation_id)
        if item is None:
            raise HTTPException(404, f"observation {observation_id} not found")
        return item

    # -- ingest from our own ground station --------------------------------------------------------
    @app.post("/ingest/frames", summary="Store raw frames received by our ground station")
    def push_frames(
        body: PushRequest,
        x_ingest_token: str | None = Header(default=None),
        store: StateStore = Depends(get_store),
    ) -> dict[str, Any]:
        if settings.ingest_token and x_ingest_token != settings.ingest_token:
            raise HTTPException(401, "missing or invalid X-Ingest-Token")
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
            meta = dict(pushed.meta)
            if pushed.frequency is not None:
                meta["frequency"] = pushed.frequency
            if pushed.rssi is not None:
                meta["rssi"] = pushed.rssi
            frame_id, is_new = store.add_pushed_frame(
                norad_cat_id=body.norad_cat_id, station_name=body.station, raw=raw,
                frame_time=received.astimezone(dt.timezone.utc), received_at=now, meta=meta,
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
