"""Command-line entry point ``pvdx-ingest`` and the ingest loop that ``pvdx-serve`` runs in-process."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading

from pvdx_ground.config import ConfigError, Settings, load_dotenv
from pvdx_ground.ingest.client import NetworkClient, SatnogsError
from pvdx_ground.ingest.state import StateStore
from pvdx_ground.ingest.worker import IngestWorker
from pvdx_ground.timeutil import parse_iso8601

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pvdx-ingest",
        description="Pull demodulated frames for one satellite from the SatNOGS Network into SQLite.",
    )
    parser.add_argument("--env", default=".env", help="dotenv file to load (default: .env)")
    parser.add_argument("--norad", type=int, help="override NORAD_CAT_ID")
    parser.add_argument("--since", help="override INGEST_START (ISO-8601, UTC) for the first sweep")
    parser.add_argument("--state", help="override STATE_DB path")
    parser.add_argument("--status", help="override INGEST_STATUS (default good)")
    parser.add_argument("--overlap-hours", type=float, help="override INGEST_OVERLAP_HOURS")
    parser.add_argument("--min-interval", type=float, help="seconds between API list requests (default from throttle, min 15)")
    parser.add_argument("--max-pages", type=int, help="stop after N pages this run (sweep resumes next run)")
    parser.add_argument("--poll", type=float, metavar="SECONDS", help="loop forever, sleeping SECONDS between runs")
    parser.add_argument("--retry-failed", action="store_true", help="reset the attempt budget of failed frames before running")
    parser.add_argument("--stats", action="store_true", help="print state-database counters as JSON and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def run_ingest(
    settings: Settings,
    *,
    poll: float | None = None,
    max_pages: int | None = None,
    retry_failed: bool = False,
    stop: threading.Event | None = None,
) -> int:
    """Run one sweep, or sweep every ``poll`` seconds until ``stop`` is set. Returns an exit code.

    ``stop`` also ends a sweep early, between pages and between frame downloads (it resumes next run).
    """
    if settings.norad_cat_id is None:
        raise ConfigError("NORAD_CAT_ID is not set (put it in .env or pass --norad)")
    with StateStore(settings.state_db) as store:
        if retry_failed:
            n = store.reset_failed_frames(settings.norad_cat_id)
            log.info("reset %d failed frame(s) for another download attempt", n)
        with NetworkClient(
            settings.satnogs_network_url, settings.satnogs_api_token, min_interval=settings.ingest_min_interval,
            stop=stop,
        ) as client:
            if not settings.satnogs_api_token:
                log.warning("SATNOGS_API_TOKEN not set: using anonymous access (60 requests/hour)")
            worker = IngestWorker(
                client,
                store,
                norad_cat_id=settings.norad_cat_id,
                start=settings.ingest_start,
                overlap=settings.ingest_overlap,
                status=settings.ingest_status,
                max_pages=max_pages,
                stop=stop,
            )
            if poll:
                worker.run_forever(poll, stop=stop)
                return 0
            result = worker.run_once()
            return 0 if not result.errors else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    load_dotenv(args.env)

    overrides = {
        "NORAD_CAT_ID": args.norad,
        "INGEST_START": args.since,
        "STATE_DB": args.state,
        "INGEST_STATUS": args.status,
        "INGEST_OVERLAP_HOURS": args.overlap_hours,
        "INGEST_MIN_INTERVAL": args.min_interval,
    }
    env = dict(os.environ)
    env.update({k: str(v) for k, v in overrides.items() if v is not None})
    try:
        settings = Settings.from_env(env, require_norad=not args.stats)
        if args.since:
            parse_iso8601(args.since)
    except (ConfigError, ValueError) as exc:
        log.error("configuration error: %s", exc)
        return 2

    if args.stats:
        with StateStore(settings.state_db) as store:
            print(json.dumps(store.stats(settings.norad_cat_id), indent=2))
        return 0
    try:
        return run_ingest(settings, poll=args.poll, max_pages=args.max_pages, retry_failed=args.retry_failed)
    except KeyboardInterrupt:
        log.info("interrupted; progress is saved and will resume next run")
        return 130
    except SatnogsError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
