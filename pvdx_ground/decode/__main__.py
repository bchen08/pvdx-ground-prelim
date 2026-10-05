"""Command-line entry point ``pvdx-decode`` and the decode loop that ``pvdx-serve`` runs in-process.

Reads stored, not-yet-decoded frames (from SatNOGS or pushed by our ground station), groups duplicate
receptions of one transmission under a primary frame, decodes them with the configured decoder, writes one
InfluxDB point per frame (copies tagged ``primary=false``), keeps the decoded fields and the latest value of
every field (from primaries only) in SQLite, and publishes the latest values to Redis when ``REDIS_URL`` is set.
A frame is only marked decoded after its point has been written, so a failed write is retried on the
next run; frames the decoder rejects, or that make it raise unexpectedly, are marked ``error`` (re-run
with ``--redo`` after fixing the decoder), so one bad frame never blocks the ones behind it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from pvdx_ground.config import ConfigError, Settings, load_dotenv
from pvdx_ground.decode import DecodedFrame, DecodeError, Decoder, get_decoder, normalise_fields
from pvdx_ground.ingest.state import StateStore
from pvdx_ground.timeutil import to_iso_z, utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pvdx_ground.publish.redis import RedisPublisher
    from pvdx_ground.storage import InfluxWriter

log = logging.getLogger(__name__)
BATCH = 500


class FrameSink(Protocol):
    """What the decode stage needs from a storage backend (``InfluxWriter`` or a test double)."""

    def write_frames(self, frames: Any) -> list[tuple[DecodedFrame, str]]: ...

    def close(self) -> None: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pvdx-decode", description="Decode stored frames and write them to InfluxDB.")
    parser.add_argument("--env", default=".env", help="dotenv file to load (default: .env)")
    parser.add_argument("--decoder", help="override DECODER (e.g. satnogs:geoscan, pvdx)")
    parser.add_argument("--norad", type=int, help="override NORAD_CAT_ID (only decode this satellite's frames)")
    parser.add_argument("--state", help="override STATE_DB path")
    parser.add_argument("--measurement", default="telemetry", help="InfluxDB measurement name")
    parser.add_argument("--limit", type=int, help="decode at most N frames this run")
    parser.add_argument("--redo", action="store_true", help="re-decode frames already marked decoded")
    parser.add_argument("--dry-run", action="store_true", help="decode and print, do not write to InfluxDB or Redis")
    parser.add_argument("--poll", type=float, metavar="SECONDS", help="loop forever, sleeping SECONDS between runs")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def publish_latest(store: StateStore, publisher: "RedisPublisher", norad_cat_ids: set[int]) -> int:
    """Push the latest values of the given satellites to Redis; a Redis outage is logged, not fatal."""
    from pvdx_ground.publish.redis import PublishError

    keys = 0
    for norad in sorted(norad_cat_ids):
        latest = store.latest_values(norad)
        if not latest:
            continue
        try:
            keys += publisher.publish(norad, latest, now=utcnow())
        except PublishError as exc:
            log.warning("could not publish latest telemetry for NORAD %d to Redis: %s", norad, exc)
            break
    return keys


def decode_once(
    store: StateStore,
    decoder: Decoder,
    writer: FrameSink | None,
    *,
    norad: int | None,
    redo: bool,
    limit: int | None,
    dry_run: bool,
    publisher: "RedisPublisher | None" = None,
) -> dict[str, Any]:
    """Decode one batch of stored frames, write and publish them; returns the counters that were logged."""
    frames = store.frames_to_decode(norad_cat_id=norad, redo=redo, limit=limit)
    unassigned = [f.id for f in frames if f.primary_frame_id is None]
    if unassigned and not dry_run:  # the whole batch at once, so native copies win over gr-satellites ones
        assigned = store.assign_primaries(unassigned)
        frames = [replace(f, primary_frame_id=assigned.get(f.id, f.primary_frame_id)) for f in frames]
    counts: dict[str, Any] = {
        "frames": len(frames), "decoded": 0, "copies": 0, "written": 0, "rejected": 0, "empty": 0, "errors": 0,
        "published": 0,
    }
    batch: list[DecodedFrame] = []
    touched: set[int] = set()
    crashes = 0

    def flush() -> None:
        if not batch:
            return
        if dry_run:
            for d in batch[:3]:
                try:
                    when: str | None = to_iso_z(d.frame.timestamp)
                except ValueError:  # neither frame time nor observation; a real run marks it error
                    when = None
                print(json.dumps({"frame_id": d.frame.id, "source": d.frame.source, "observation_id": d.frame.observation_id,
                                  "time": when, "fields": d.fields}, indent=2))
            counts["written"] += len(batch)
        else:
            assert writer is not None
            rejected = writer.write_frames(batch)
            bad = {frame.frame.id: reason for frame, reason in rejected}
            now = utcnow()
            for d in batch:
                if d.frame.id in bad:
                    store.mark_decoded(d.frame.id, "error", decoded_at=now, error=bad[d.frame.id], decoder=d.decoder)
                    log.warning("frame %d not written to InfluxDB: %s", d.frame.id, bad[d.frame.id])
                else:
                    store.mark_decoded(d.frame.id, "ok", decoded_at=now, fields=d.fields, decoder=d.decoder)
                    touched.add(d.frame.norad_cat_id)
            counts["written"] += len(batch) - len(bad)
            counts["rejected"] += len(bad)
        batch.clear()

    for frame in frames:
        try:
            fields = normalise_fields(decoder.decode(frame.raw))  # also covers decoders that skip it
        except DecodeError as exc:
            counts["errors"] += 1
            log.debug("frame %d: %s", frame.id, exc)
            if not dry_run:
                store.mark_decoded(frame.id, "error", decoded_at=utcnow(), error=str(exc), decoder=decoder.name)
            continue
        except Exception as exc:  # noqa: BLE001 - a decoder bug must not wedge the loop on this frame
            counts["errors"] += 1
            crashes += 1
            error = f"{decoder.name}: unexpected {type(exc).__name__}: {exc}"
            log.error("frame %d: %s", frame.id, error, exc_info=crashes == 1)  # one traceback per pass
            if not dry_run:
                store.mark_decoded(frame.id, "error", decoded_at=utcnow(), error=error, decoder=decoder.name)
            continue
        if not fields:
            counts["empty"] += 1
            if not dry_run:
                store.mark_decoded(frame.id, "empty", decoded_at=utcnow(), decoder=decoder.name)
            continue
        counts["decoded"] += 1
        counts["copies"] += int(frame.is_copy)
        batch.append(DecodedFrame(frame=frame, decoder=decoder.name, fields=fields))
        if len(batch) >= BATCH:
            flush()
    flush()
    if publisher is not None and touched and not dry_run:
        counts["published"] = publish_latest(store, publisher, touched)
    log.info(
        "decode: %d frame(s) read, %d decoded (%d duplicate reception(s)), %d written%s, %d rejected by InfluxDB, "
        "%d empty, %d not telemetry/undecodable, %d Redis key(s) published",
        counts["frames"], counts["decoded"], counts["copies"], counts["written"], " (dry run)" if dry_run else "",
        counts["rejected"], counts["empty"], counts["errors"], counts["published"],
    )
    return counts


def open_writer(settings: Settings, *, measurement: str = "telemetry", poll: float | None = None) -> "InfluxWriter":
    """Connect to InfluxDB and verify the credentials; raises ``ConfigError`` when they are unusable.

    Under ``poll`` an unreachable or misconfigured InfluxDB is only logged, so a service can keep
    polling until it comes back (every batch is retried until its points are written).
    """
    if not settings.influx_token:
        raise ConfigError("INFLUX_TOKEN is not set (see .env.example)")
    from pvdx_ground.storage import InfluxWriter

    writer = InfluxWriter(
        settings.influx_url, settings.influx_token, settings.influx_org, settings.influx_bucket, measurement=measurement
    )
    problem = writer.check_access()
    if problem is not None:
        message = f"{problem} (InfluxDB at {settings.influx_url}; is `docker compose up -d` running and .env correct?)"
        if not poll:
            writer.close()
            raise ConfigError(message)
        log.error("%s", message)
        log.warning("continuing to poll; will retry every %.0fs", poll)
    return writer


def open_publisher(settings: Settings) -> "RedisPublisher | None":
    """The Redis publisher when ``REDIS_URL`` is configured, else ``None``."""
    if not settings.redis_url:
        return None
    from pvdx_ground.publish.redis import RedisPublisher

    publisher = RedisPublisher(
        settings.redis_url, prefix=settings.redis_key_prefix, ttl=settings.redis_telemetry_ttl,
        aliases=settings.telemetry_aliases,
    )
    problem = publisher.check()
    if problem is not None:
        log.warning("Redis publisher: %s; latest values will be published once Redis is reachable", problem)
    return publisher


def run_decode(
    settings: Settings,
    *,
    decoder: Decoder,
    writer: FrameSink | None,
    publisher: "RedisPublisher | None" = None,
    redo: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    poll: float | None = None,
    stop: threading.Event | None = None,
    wake: threading.Event | None = None,
) -> int:
    """Run the decode stage once, or every ``poll`` seconds until ``stop`` is set.

    ``wake`` (optional) cuts a pause short, for example when the API just stored pushed frames.
    """
    stop = stop or threading.Event()
    with StateStore(settings.state_db) as store:
        if publisher is not None and not dry_run:
            norads = {settings.norad_cat_id} if settings.norad_cat_id else {s["norad_cat_id"] for s in store.satellites()}
            publish_latest(store, publisher, norads)  # warm the cache after a restart
        while True:
            try:
                decode_once(store, decoder, writer, norad=settings.norad_cat_id, redo=redo, limit=limit,
                            dry_run=dry_run, publisher=publisher)
            except Exception:  # noqa: BLE001 - keep polling through transient InfluxDB failures
                if not poll:
                    raise
                log.exception("decode run failed; retrying in %.0fs", poll)
            if not poll:
                return 0
            log.debug("sleeping %.0fs until next poll", poll)
            waiter = wake if wake is not None else stop
            waiter.wait(poll)
            if stop.is_set():
                return 0
            if wake is not None:
                wake.clear()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    load_dotenv(args.env)
    env = dict(os.environ)
    for key, value in {"NORAD_CAT_ID": args.norad, "STATE_DB": args.state, "DECODER": args.decoder}.items():
        if value is not None:
            env[key] = str(value)
    try:
        settings = Settings.from_env(env, require_norad=False)
        decoder = get_decoder(settings.decoder)
    except (ConfigError, DecodeError) as exc:
        log.error("configuration error: %s", exc)
        return 2

    writer: FrameSink | None = None
    publisher = None
    if not args.dry_run:
        try:
            writer = open_writer(settings, measurement=args.measurement, poll=args.poll)
        except ConfigError as exc:
            log.error("%s", exc)
            return 2 if "INFLUX_TOKEN" in str(exc) else 1
        publisher = open_publisher(settings)

    log.info("decoder %s, state db %s%s", decoder.name, settings.state_db,
             f", publishing to {settings.redis_url}" if publisher else "")
    try:
        return run_decode(settings, decoder=decoder, writer=writer, publisher=publisher, redo=args.redo,
                          limit=args.limit, dry_run=args.dry_run, poll=args.poll)
    except KeyboardInterrupt:
        return 130
    finally:
        if writer is not None:
            writer.close()
        if publisher is not None:
            publisher.close()


if __name__ == "__main__":
    sys.exit(main())
