"""``pvdx-serve``: the whole cloud telemetry service in one process.

Three parts share one SQLite state database:

* an **ingest** thread sweeps the SatNOGS Network every ``INGEST_POLL`` seconds (``pvdx-ingest --poll``);
* a **decode** thread decodes new frames every ``DECODE_POLL`` seconds, or as soon as the API stores
  pushed frames, writes InfluxDB points and publishes latest values to Redis (``pvdx-decode --poll``);
* the **HTTP API** (uvicorn) in the main thread.

Each part has its own SQLite connection; the database is created or migrated once before the threads
start. Ctrl-C (or SIGTERM in Docker) stops the API, then the threads finish their current step and exit.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from typing import Any

from pvdx_ground import __version__
from pvdx_ground.config import ConfigError, Settings, load_dotenv
from pvdx_ground.decode import DecodeError, Decoder, get_decoder
from pvdx_ground.decode.__main__ import open_publisher, open_writer, run_decode
from pvdx_ground.ingest.__main__ import run_ingest
from pvdx_ground.ingest.state import StateStore

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pvdx-serve", description="Run ingest, decode, publish and the HTTP API together.")
    parser.add_argument("--env", default=".env", help="dotenv file to load (default: .env)")
    parser.add_argument("--host", help="override API_HOST (default 127.0.0.1; use 0.0.0.0 in a container)")
    parser.add_argument("--port", type=int, help="override API_PORT (default 8080)")
    parser.add_argument("--ingest-poll", type=float, metavar="SECONDS", help="override INGEST_POLL (default 600)")
    parser.add_argument("--decode-poll", type=float, metavar="SECONDS", help="override DECODE_POLL (default 60)")
    parser.add_argument("--measurement", default="telemetry", help="InfluxDB measurement name")
    parser.add_argument("--no-ingest", action="store_true", help="do not sweep SatNOGS (frames arrive by push only)")
    parser.add_argument("--no-decode", action="store_true", help="do not decode/write/publish (API and ingest only)")
    parser.add_argument("--no-api", action="store_true", help="do not serve HTTP (ingest and decode only)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging and HTTP access log")
    return parser


def _ingest_thread(settings: Settings, poll: float, stop: threading.Event) -> None:
    try:
        run_ingest(settings, poll=poll, stop=stop)
    except Exception:  # noqa: BLE001 - keep the process alive; health reports the dead worker
        log.exception("ingest worker stopped after an unrecoverable error")


def _decode_thread(
    settings: Settings, decoder: Decoder, measurement: str, poll: float, stop: threading.Event, wake: threading.Event
) -> None:
    writer: Any = None
    publisher = None
    try:
        writer = open_writer(settings, measurement=measurement, poll=poll)
        publisher = open_publisher(settings)
        run_decode(settings, decoder=decoder, writer=writer, publisher=publisher, poll=poll, stop=stop, wake=wake)
    except Exception:  # noqa: BLE001
        log.exception("decode worker stopped after an unrecoverable error")
    finally:
        if writer is not None:
            writer.close()
        if publisher is not None:
            publisher.close()


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
    for key, value in {"API_HOST": args.host, "API_PORT": args.port, "INGEST_POLL": args.ingest_poll,
                       "DECODE_POLL": args.decode_poll}.items():
        if value is not None:
            env[key] = str(value)

    decoder: Decoder | None = None
    try:
        settings = Settings.from_env(env, require_norad=not args.no_ingest)
        if not args.no_decode:
            decoder = get_decoder(settings.decoder)
            if not settings.influx_token:
                raise ConfigError("INFLUX_TOKEN is not set (see .env.example)")
    except (ConfigError, DecodeError) as exc:
        log.error("configuration error: %s", exc)
        return 2

    with StateStore(settings.state_db) as store:  # create or migrate once, before any thread opens it
        stats = store.stats(settings.norad_cat_id)
        log.info(
            "pvdx-serve %s: state db %s (schema %s), %s observation(s), %s frame(s) stored, %s decoded, "
            "%s waiting for decode", __version__, settings.state_db, stats["schema_version"],
            stats["observations"], stats["frames_ok"], stats["frames_decoded"], stats["frames_to_decode"],
        )

    stop, wake = threading.Event(), threading.Event()
    workers: dict[str, threading.Thread] = {}
    if not args.no_ingest:
        workers["ingest"] = threading.Thread(
            target=_ingest_thread, args=(settings, settings.ingest_poll, stop), name="ingest", daemon=True
        )
    if not args.no_decode:
        assert decoder is not None
        workers["decode"] = threading.Thread(
            target=_decode_thread, args=(settings, decoder, args.measurement, settings.decode_poll, stop, wake),
            name="decode", daemon=True,
        )
    for name, thread in workers.items():
        thread.start()
        log.info("%s worker started", name)

    try:
        if args.no_api:
            while any(t.is_alive() for t in workers.values()):
                time.sleep(1)
            log.error("all workers stopped; exiting")
            return 1
        import uvicorn

        from pvdx_ground.api import create_app

        app = create_app(settings, wake=wake, workers=workers)
        log.info("API listening on http://%s:%d (docs at /docs)", settings.api_host, settings.api_port)
        uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_config=None, access_log=args.verbose)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting down workers")
        stop.set()
        wake.set()
        for thread in workers.values():
            thread.join(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
