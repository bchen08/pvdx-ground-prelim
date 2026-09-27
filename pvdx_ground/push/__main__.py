"""``pvdx-push``: send raw frames from files, a directory or stdin to the cloud service.

Examples (run on the ground station computer)::

    pvdx-push --norad 62394 frame1.bin frame2.bin           # one frame per file, time = file mtime
    pvdx-push --norad 62394 --time 2026-09-27T14:03:05Z f.bin
    pvdx-push --norad 62394 --watch 5 /var/lib/gnuradio/frames/   # keep sending new files every 5 s
    printf '2026-09-27T14:03:05Z 86a2...\\n' | pvdx-push --norad 62394 --stdin   # "[ISO-time] hex" lines

``PUSH_URL``, ``PUSH_STATION`` and ``PUSH_TOKEN`` come from ``.env`` or the flags ``--url``,
``--station`` and ``--token``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

from pvdx_ground.config import ConfigError, Settings, load_dotenv
from pvdx_ground.push.client import PushClient, PushError, PushFrame
from pvdx_ground.timeutil import parse_iso8601, utcnow

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pvdx-push", description="Push raw frames to the PVDX cloud service.")
    parser.add_argument("paths", nargs="*", help="frame files (raw bytes) or directories of them")
    parser.add_argument("--env", default=".env", help="dotenv file to load (default: .env)")
    parser.add_argument("--url", help="service base URL (PUSH_URL), e.g. http://cloud.example.org:8080")
    parser.add_argument("--station", help="receiving station name (PUSH_STATION)")
    parser.add_argument("--token", help="shared secret for X-Ingest-Token (PUSH_TOKEN)")
    parser.add_argument("--norad", type=int, help="satellite NORAD id (default NORAD_CAT_ID)")
    parser.add_argument("--time", help="receive time for every file (ISO-8601); default: file modification time")
    parser.add_argument("--frequency", type=int, help="downlink frequency in Hz recorded with every frame")
    parser.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE", help="extra metadata (repeatable)")
    parser.add_argument("--stdin", action="store_true", help="read '[ISO-time] hex' lines from stdin instead of files")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="rescan the given directories forever")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def parse_meta(items: list[str]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"--meta expects KEY=VALUE, got {item!r}")
        meta[key.strip()] = value.strip()
    return meta


def frames_from_stdin(lines, *, frequency: int | None, meta: dict[str, str]) -> list[PushFrame]:
    """Parse ``[ISO-time] hex`` lines; blank lines and ``#`` comments are skipped."""
    frames: list[PushFrame] = []
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.replace(",", " ").split()
        when = utcnow()
        if len(parts) == 2:
            try:
                when = parse_iso8601(parts[0])
            except ValueError as exc:
                raise ConfigError(f"stdin line {number}: {parts[0]!r} is not an ISO-8601 time") from exc
            text = parts[1]
        elif len(parts) != 1:
            raise ConfigError(f"stdin line {number}: expected '[ISO-time] hex'")
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            raise ConfigError(f"stdin line {number}: not hexadecimal") from exc
        if raw:
            frames.append(PushFrame(raw=raw, received_at=when, frequency=frequency, meta=meta))
    return frames


def file_frame(path: Path, *, when: dt.datetime | None, frequency: int | None, meta: dict[str, str]) -> PushFrame:
    raw = path.read_bytes()
    received = when or dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).replace(microsecond=0)
    return PushFrame(raw=raw, received_at=received, frequency=frequency, meta={**meta, "file": path.name})


def scan(paths: list[Path], seen: set[tuple[str, int, int]]) -> list[Path]:
    """Regular files under ``paths`` (directories are scanned one level deep) not yet in ``seen``."""
    found: list[Path] = []
    for path in paths:
        candidates = sorted(p for p in path.iterdir() if p.is_file()) if path.is_dir() else [path]
        for candidate in candidates:
            try:
                stat = candidate.stat()
            except OSError:
                continue
            key = (str(candidate), int(stat.st_mtime), stat.st_size)
            if key in seen or stat.st_size == 0:
                continue
            seen.add(key)
            found.append(candidate)
    return found


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(args.env)
    env = dict(os.environ)
    for key, value in {"PUSH_URL": args.url, "PUSH_STATION": args.station, "PUSH_TOKEN": args.token,
                       "NORAD_CAT_ID": args.norad}.items():
        if value is not None:
            env[key] = str(value)
    try:
        settings = Settings.from_env(env, require_norad=True)
        if not settings.push_url:
            raise ConfigError("PUSH_URL is not set (or pass --url)")
        if not settings.push_station:
            raise ConfigError("PUSH_STATION is not set (or pass --station)")
        when = parse_iso8601(args.time) if args.time else None
        meta = parse_meta(args.meta)
        if args.stdin and args.paths:
            raise ConfigError("--stdin cannot be combined with file paths")
        if not args.stdin and not args.paths:
            raise ConfigError("nothing to send: give file/directory paths or --stdin")
        if args.watch and args.stdin:
            raise ConfigError("--watch only applies to directories")
    except (ConfigError, ValueError) as exc:
        log.error("configuration error: %s", exc)
        return 2
    assert settings.norad_cat_id is not None

    paths = [Path(p) for p in args.paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        log.error("no such file or directory: %s", ", ".join(missing))
        return 2

    with PushClient(settings.push_url, station=settings.push_station, token=settings.push_token) as client:
        try:
            if args.stdin:
                frames = frames_from_stdin(sys.stdin, frequency=args.frequency, meta=meta)
                if not frames:
                    log.warning("no frames on stdin")
                    return 0
                result = client.send(settings.norad_cat_id, frames)
                log.info("sent %d frame(s): %d stored, %d duplicate(s)", result.sent, result.accepted, result.duplicates)
                return 0
            seen: set[tuple[str, int, int]] = set()
            while True:
                files = scan(paths, seen)
                if files:
                    frames = [file_frame(p, when=when, frequency=args.frequency, meta=meta) for p in files]
                    result = client.send(settings.norad_cat_id, frames)
                    log.info("sent %d file(s): %d stored, %d duplicate(s)", result.sent, result.accepted, result.duplicates)
                elif not args.watch:
                    log.warning("no frame files found under %s", ", ".join(map(str, paths)))
                if not args.watch:
                    return 0
                time.sleep(args.watch)
        except ConfigError as exc:
            log.error("%s", exc)
            return 2
        except PushError as exc:
            log.error("%s", exc)
            return 1
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    sys.exit(main())
