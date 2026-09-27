"""SQLite state: observations (provenance), frames (raw bytes and decoded fields), sweeps, watermarks and
the latest value of every telemetry field.

Idempotency rules:
* an observation is keyed by its SatNOGS id and upserted on every sighting;
* a SatNOGS frame is keyed by its ``payload_demod`` URL (UNIQUE) and downloaded at most once;
* a frame pushed by the ground station is keyed by (satellite, station, frame time, sha256) and stored once;
* a *sweep* is one pass over ``[since, now]``; its cursor is persisted per page so an interrupted run
  resumes where it stopped, and the per-satellite *watermark* only advances when a sweep completes;
* ``latest_values`` holds the newest decoded value per field and satellite and only moves forward in time.

Schema version 2. A version-1 database (SatNOGS frames only, no decoded fields) is migrated in place when
opened; its already-decoded frames are queued for re-decoding so the decoded fields get filled in.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from pvdx_ground.timeutil import parse_iso8601, to_iso_z

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SOURCES = ("satnogs", "groundstation")

_FRAMES_TABLE = """
CREATE TABLE IF NOT EXISTS frames (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL DEFAULT 'satnogs' CHECK (source IN ('satnogs', 'groundstation')),
    observation_id INTEGER REFERENCES observations (id),
    norad_cat_id   INTEGER,
    station_name   TEXT,
    url            TEXT UNIQUE,
    frame_time     TEXT,
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ok', 'failed')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    sha256         TEXT,
    size           INTEGER,
    raw            BLOB,
    meta_json      TEXT,
    downloaded_at  TEXT,
    decoded_at     TEXT,
    decode_status  TEXT,
    decoder        TEXT,
    decoded_json   TEXT,
    decode_error   TEXT
)
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id                      INTEGER PRIMARY KEY,
    norad_cat_id            INTEGER NOT NULL,
    sat_id                  TEXT,
    ground_station          INTEGER,
    station_name            TEXT,
    station_lat             REAL,
    station_lng             REAL,
    station_alt             REAL,
    start_time              TEXT NOT NULL,
    end_time                TEXT NOT NULL,
    status                  TEXT,
    observer                TEXT,
    transmitter_uuid        TEXT,
    transmitter_description TEXT,
    transmitter_mode        TEXT,
    observation_frequency   INTEGER,
    tle0                    TEXT,
    tle1                    TEXT,
    tle2                    TEXT,
    tle_source              TEXT,
    payload_url             TEXT,
    waterfall_url           TEXT,
    demoddata_count         INTEGER NOT NULL DEFAULT 0,
    raw_json                TEXT NOT NULL,
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    frames_complete         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_observations_norad_start ON observations (norad_cat_id, start_time);
{_FRAMES_TABLE};
CREATE INDEX IF NOT EXISTS ix_frames_status ON frames (status);
CREATE INDEX IF NOT EXISTS ix_frames_observation ON frames (observation_id);
CREATE INDEX IF NOT EXISTS ix_frames_decode ON frames (status, decode_status);
CREATE INDEX IF NOT EXISTS ix_frames_norad_time ON frames (norad_cat_id, frame_time);
CREATE UNIQUE INDEX IF NOT EXISTS ux_frames_pushed
    ON frames (norad_cat_id, station_name, frame_time, sha256) WHERE source = 'groundstation';
CREATE TABLE IF NOT EXISTS sweeps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    norad_cat_id      INTEGER NOT NULL,
    status_filter     TEXT NOT NULL,
    since             TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    state             TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'done', 'aborted')),
    next_url          TEXT,
    pages_done        INTEGER NOT NULL DEFAULT 0,
    observations_seen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sweeps_norad_state ON sweeps (norad_cat_id, state);
CREATE TABLE IF NOT EXISTS watermarks (
    norad_cat_id  INTEGER PRIMARY KEY,
    swept_through TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS latest_values (
    norad_cat_id INTEGER NOT NULL,
    field        TEXT NOT NULL,
    value        TEXT NOT NULL,
    frame_id     INTEGER NOT NULL,
    frame_time   TEXT NOT NULL,
    source       TEXT NOT NULL,
    station_name TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (norad_cat_id, field)
);
"""

# .../data_obs/2026/9/26/19/15060641/data_15060641_2026-09-26T19-37-23
_FRAME_TIME_RE = re.compile(r"data_(?:obs_)?(\d+)_(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})(?:\D|$)")
_FIELD_NAME_RE = re.compile(r'^[^"\\\x00]{1,200}$')

# effective time of a frame: the time in the SatNOGS file name (or the pushed receive time), else the
# start of the observation it came from
_EFFECTIVE_TIME = "COALESCE(f.frame_time, o.start_time)"


def frame_time_from_url(url: str) -> dt.datetime | None:
    """Recover the frame timestamp SatNOGS encodes in the demoddata filename, if present."""
    match = _FRAME_TIME_RE.search(url.rsplit("/", 1)[-1])
    if not match:
        return None
    _, day, hh, mm, ss = match.groups()
    try:
        return parse_iso8601(f"{day}T{hh}:{mm}:{ss}Z")
    except ValueError:
        return None


def json_path(field: str) -> str:
    """JSON path for ``json_extract`` on ``decoded_json``; rejects names that cannot be quoted safely."""
    if not _FIELD_NAME_RE.match(field):
        raise ValueError(f"invalid field name {field!r}")
    return f'$."{field}"'


def _parse_time(value: str | None) -> dt.datetime | None:
    return parse_iso8601(value) if value else None


def _load_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class FrameRef:
    """A frame row that still needs downloading."""

    id: int
    observation_id: int
    url: str


@dataclass(frozen=True)
class StoredFrame:
    """A stored frame joined with its provenance (input to the decode stage).

    SatNOGS frames carry their observation; frames pushed by the ground station have no observation and
    take satellite and station from the push itself.
    """

    id: int
    observation_id: int | None
    url: str | None
    raw: bytes
    sha256: str | None
    frame_time: dt.datetime | None
    norad_cat_id: int
    sat_id: str | None
    ground_station: int | None
    station_name: str | None
    observation_start: dt.datetime | None
    observation_end: dt.datetime | None
    transmitter_uuid: str | None
    tle0: str | None
    tle1: str | None
    tle2: str | None
    source: str = "satnogs"
    meta: Mapping[str, Any] = dc_field(default_factory=dict)

    @property
    def timestamp(self) -> dt.datetime:
        """Best available time for the frame: the frame time, else the observation start."""
        when = self.frame_time or self.observation_start
        if when is None:
            raise ValueError(f"frame {self.id} has neither a frame time nor an observation")
        return when


@dataclass(frozen=True)
class LatestValue:
    """The newest decoded value of one field for one satellite."""

    field: str
    value: float | int | str
    frame_id: int
    frame_time: dt.datetime
    source: str
    station_name: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "frame_time": to_iso_z(self.frame_time),
            "frame_id": self.frame_id,
            "source": self.source,
            "station_name": self.station_name,
        }


@dataclass(frozen=True)
class Sweep:
    """A persisted sweep row."""

    id: int
    norad_cat_id: int
    status_filter: str
    since: dt.datetime
    started_at: dt.datetime
    state: str
    next_url: str | None
    pages_done: int
    observations_seen: int


class StateStore:
    """Thin wrapper over a SQLite database file. One instance per thread; not thread-safe."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)  # autocommit; explicit txns
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._open_schema()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- schema and migrations -------------------------------------------------------------------
    def _open_schema(self) -> None:
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        version = self.schema_version()
        if version is None:
            self._db.executescript(SCHEMA)
            self._db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            return
        if version == 1:
            self._migrate_v1_to_v2()
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path}: schema version {version} is newer than this code supports ({SCHEMA_VERSION})"
            )
        self._db.executescript(SCHEMA)  # idempotent: adds any table or index that is missing

    def schema_version(self) -> int | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else None

    def _migrate_v1_to_v2(self) -> None:
        """Rebuild ``frames`` with the version-2 columns; re-queue decoded frames so decoded fields get filled."""
        self._db.execute("PRAGMA foreign_keys=OFF")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            if self.schema_version() != 1:  # another process migrated while we waited for the lock
                self._db.execute("COMMIT")
                return
            log.info("migrating %s from schema version 1 to 2", self.path)
            self._db.execute("ALTER TABLE frames RENAME TO frames_v1")
            self._db.execute(_FRAMES_TABLE)
            self._db.execute(
                """
                INSERT INTO frames (id, source, observation_id, norad_cat_id, station_name, url, frame_time, status,
                                    attempts, last_error, sha256, size, raw, downloaded_at, decoded_at,
                                    decode_status, decode_error)
                SELECT f.id, 'satnogs', f.observation_id, o.norad_cat_id, o.station_name, f.url, f.frame_time,
                       f.status, f.attempts, f.last_error, f.sha256, f.size, f.raw, f.downloaded_at,
                       f.decoded_at, f.decode_status, f.decode_error
                FROM frames_v1 f LEFT JOIN observations o ON o.id = f.observation_id
                """
            )
            requeued = self._db.execute(
                "UPDATE frames SET decode_status = NULL, decoded_at = NULL WHERE decode_status IN ('ok', 'empty')"
            ).rowcount
            self._db.execute("DROP TABLE frames_v1")
            self._db.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        finally:
            self._db.execute("PRAGMA foreign_keys=ON")
        log.info("migration done: %d previously decoded frame(s) queued for re-decoding to fill decoded fields", requeued)

    # -- observations ------------------------------------------------------------------------------
    def upsert_observation(self, obs: dict[str, Any], *, seen_at: dt.datetime) -> tuple[bool, int]:
        """Insert or refresh an observation and queue any not-yet-seen demoddata URLs.

        Returns ``(is_new_observation, number_of_new_frame_urls)``.
        """
        obs_id = int(obs["id"])
        seen = to_iso_z(seen_at)
        demod_urls = [
            entry["payload_demod"]
            for entry in (obs.get("demoddata") or [])
            if isinstance(entry, dict) and entry.get("payload_demod")
        ]
        row = {
            "id": obs_id,
            "norad_cat_id": obs.get("norad_cat_id"),
            "sat_id": obs.get("sat_id"),
            "ground_station": obs.get("ground_station"),
            "station_name": obs.get("station_name"),
            "station_lat": obs.get("station_lat"),
            "station_lng": obs.get("station_lng"),
            "station_alt": obs.get("station_alt"),
            "start_time": obs.get("start"),
            "end_time": obs.get("end"),
            "status": obs.get("status"),
            "observer": obs.get("observer"),
            "transmitter_uuid": obs.get("transmitter_uuid") or obs.get("transmitter"),
            "transmitter_description": obs.get("transmitter_description"),
            "transmitter_mode": obs.get("transmitter_mode"),
            "observation_frequency": obs.get("observation_frequency"),
            "tle0": obs.get("tle0"),
            "tle1": obs.get("tle1"),
            "tle2": obs.get("tle2"),
            "tle_source": obs.get("tle_source"),
            "payload_url": obs.get("payload"),
            "waterfall_url": obs.get("waterfall"),
            "demoddata_count": len(demod_urls),
            "raw_json": json.dumps(obs, sort_keys=True, separators=(",", ":")),
            "seen": seen,
        }
        with self._db:  # one transaction
            self._db.execute("BEGIN IMMEDIATE")  # take the write lock up front (WAL-safe with a concurrent decoder)
            is_new = self._db.execute("SELECT 1 FROM observations WHERE id = ?", (obs_id,)).fetchone() is None
            self._db.execute(
                """
                INSERT INTO observations (
                    id, norad_cat_id, sat_id, ground_station, station_name, station_lat, station_lng,
                    station_alt, start_time, end_time, status, observer, transmitter_uuid,
                    transmitter_description, transmitter_mode, observation_frequency, tle0, tle1, tle2,
                    tle_source, payload_url, waterfall_url, demoddata_count, raw_json,
                    first_seen_at, last_seen_at
                ) VALUES (
                    :id, :norad_cat_id, :sat_id, :ground_station, :station_name, :station_lat,
                    :station_lng, :station_alt, :start_time, :end_time, :status, :observer,
                    :transmitter_uuid, :transmitter_description, :transmitter_mode,
                    :observation_frequency, :tle0, :tle1, :tle2, :tle_source, :payload_url,
                    :waterfall_url, :demoddata_count, :raw_json, :seen, :seen
                )
                ON CONFLICT (id) DO UPDATE SET
                    norad_cat_id = excluded.norad_cat_id, sat_id = excluded.sat_id,
                    ground_station = excluded.ground_station, station_name = excluded.station_name,
                    station_lat = excluded.station_lat, station_lng = excluded.station_lng,
                    station_alt = excluded.station_alt, start_time = excluded.start_time,
                    end_time = excluded.end_time, status = excluded.status, observer = excluded.observer,
                    transmitter_uuid = excluded.transmitter_uuid,
                    transmitter_description = excluded.transmitter_description,
                    transmitter_mode = excluded.transmitter_mode,
                    observation_frequency = excluded.observation_frequency,
                    tle0 = excluded.tle0, tle1 = excluded.tle1, tle2 = excluded.tle2,
                    tle_source = excluded.tle_source, payload_url = excluded.payload_url,
                    waterfall_url = excluded.waterfall_url, demoddata_count = excluded.demoddata_count,
                    raw_json = excluded.raw_json, last_seen_at = excluded.last_seen_at
                """,
                row,
            )
            new_frames = 0
            for url in demod_urls:
                frame_time = frame_time_from_url(url)
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO frames (source, observation_id, norad_cat_id, station_name, url, frame_time) "
                    "VALUES ('satnogs', ?, ?, ?, ?, ?)",
                    (obs_id, row["norad_cat_id"], row["station_name"], url, to_iso_z(frame_time) if frame_time else None),
                )
                new_frames += cur.rowcount if cur.rowcount > 0 else 0
            self._refresh_completion(obs_id)
            self._db.execute("COMMIT")
        return is_new, new_frames

    def _refresh_completion(self, observation_id: int) -> None:
        self._db.execute(
            """
            UPDATE observations SET frames_complete = NOT EXISTS (
                SELECT 1 FROM frames WHERE observation_id = ? AND status != 'ok'
            ) WHERE id = ?
            """,
            (observation_id, observation_id),
        )

    # -- SatNOGS frames ----------------------------------------------------------------------------
    def pending_frames(self, *, max_attempts: int = 5, limit: int | None = None) -> list[FrameRef]:
        """SatNOGS frames not yet downloaded successfully and still under the attempt budget (oldest first)."""
        sql = (
            "SELECT id, observation_id, url FROM frames "
            "WHERE source = 'satnogs' AND status != 'ok' AND attempts < ? ORDER BY id"
        )
        params: tuple[Any, ...] = (max_attempts,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [FrameRef(r["id"], r["observation_id"], r["url"]) for r in self._db.execute(sql, params)]

    def store_frame(self, frame_id: int, raw: bytes, *, downloaded_at: dt.datetime) -> None:
        """Persist the downloaded bytes for a frame and mark it ``ok``."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")  # take the write lock up front (WAL-safe with a concurrent decoder)
            self._db.execute(
                """
                UPDATE frames SET status = 'ok', raw = ?, sha256 = ?, size = ?, downloaded_at = ?,
                                  attempts = attempts + 1, last_error = NULL
                WHERE id = ?
                """,
                (raw, hashlib.sha256(raw).hexdigest(), len(raw), to_iso_z(downloaded_at), frame_id),
            )
            row = self._db.execute("SELECT observation_id FROM frames WHERE id = ?", (frame_id,)).fetchone()
            if row is not None and row["observation_id"] is not None:
                self._refresh_completion(row["observation_id"])
            self._db.execute("COMMIT")

    def reset_failed_frames(self, norad_cat_id: int | None = None) -> int:
        """Give every ``failed`` frame a fresh attempt budget (``pvdx-ingest --retry-failed``); returns the count."""
        sql = "UPDATE frames SET status = 'pending', attempts = 0 WHERE status = 'failed'"
        params: tuple[Any, ...] = ()
        if norad_cat_id is not None:
            sql += " AND norad_cat_id = ?"
            params = (norad_cat_id,)
        return self._db.execute(sql, params).rowcount

    def mark_frame_failed(self, frame_id: int, error: str) -> None:
        """Record a failed download attempt; the frame is retried on later runs up to the budget."""
        self._db.execute(
            "UPDATE frames SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error[:500], frame_id),
        )

    # -- ground-station frames ---------------------------------------------------------------------
    def add_pushed_frame(
        self,
        *,
        norad_cat_id: int,
        station_name: str,
        raw: bytes,
        frame_time: dt.datetime,
        received_at: dt.datetime,
        meta: Mapping[str, Any] | None = None,
    ) -> tuple[int, bool]:
        """Store a frame received by our own ground station; returns ``(frame_id, is_new)``.

        The same bytes received by the same station at the same second are stored once, so a client
        may safely resend after a timeout.
        """
        if not station_name:
            raise ValueError("station_name is required for pushed frames")
        digest = hashlib.sha256(raw).hexdigest()
        when = to_iso_z(frame_time)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            cur = self._db.execute(
                """
                INSERT OR IGNORE INTO frames (source, norad_cat_id, station_name, frame_time, status, attempts,
                                              sha256, size, raw, meta_json, downloaded_at)
                VALUES ('groundstation', ?, ?, ?, 'ok', 1, ?, ?, ?, ?, ?)
                """,
                (
                    norad_cat_id, station_name, when, digest, len(raw), raw,
                    json.dumps(dict(meta), sort_keys=True) if meta else None, to_iso_z(received_at),
                ),
            )
            if cur.rowcount == 1:
                frame_id, is_new = int(cur.lastrowid), True  # type: ignore[arg-type]
            else:
                row = self._db.execute(
                    "SELECT id FROM frames WHERE source = 'groundstation' AND norad_cat_id = ? AND station_name = ? "
                    "AND frame_time = ? AND sha256 = ?",
                    (norad_cat_id, station_name, when, digest),
                ).fetchone()
                frame_id, is_new = int(row["id"]), False
            self._db.execute("COMMIT")
        return frame_id, is_new

    # -- decode stage ------------------------------------------------------------------------------
    _FRAME_SELECT = (
        "SELECT f.id, f.source, f.observation_id, f.url, f.raw, f.sha256, f.frame_time, f.meta_json, "
        "f.norad_cat_id, f.station_name, o.sat_id, o.ground_station, o.start_time, o.end_time, "
        "o.transmitter_uuid, o.tle0, o.tle1, o.tle2 "
        "FROM frames f LEFT JOIN observations o ON o.id = f.observation_id"
    )

    @staticmethod
    def _stored_frame(r: sqlite3.Row) -> StoredFrame:
        return StoredFrame(
            id=r["id"],
            observation_id=r["observation_id"],
            url=r["url"],
            raw=bytes(r["raw"] or b""),
            sha256=r["sha256"],
            frame_time=_parse_time(r["frame_time"]),
            norad_cat_id=r["norad_cat_id"],
            sat_id=r["sat_id"],
            ground_station=r["ground_station"],
            station_name=r["station_name"],
            observation_start=_parse_time(r["start_time"]),
            observation_end=_parse_time(r["end_time"]),
            transmitter_uuid=r["transmitter_uuid"],
            tle0=r["tle0"],
            tle1=r["tle1"],
            tle2=r["tle2"],
            source=r["source"],
            meta=_load_json(r["meta_json"]) or {},
        )

    def frames_to_decode(
        self, *, norad_cat_id: int | None = None, redo: bool = False, limit: int | None = None
    ) -> list[StoredFrame]:
        """Stored frames not yet decoded (or all stored frames when ``redo``), oldest first."""
        sql = self._FRAME_SELECT + " WHERE f.status = 'ok'"
        params: tuple[Any, ...] = ()
        if not redo:
            sql += " AND f.decode_status IS NULL"
        if norad_cat_id is not None:
            sql += " AND f.norad_cat_id = ?"
            params += (norad_cat_id,)
        sql += " ORDER BY f.id"
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [self._stored_frame(r) for r in self._db.execute(sql, params)]

    def mark_decoded(
        self,
        frame_id: int,
        status: str,
        *,
        decoded_at: dt.datetime,
        error: str | None = None,
        fields: Mapping[str, Any] | None = None,
        decoder: str | None = None,
    ) -> None:
        """Record the decode outcome (``ok``, ``empty`` or ``error``) for a frame.

        For ``ok`` the decoded ``fields`` are stored with the frame and folded into ``latest_values``
        (a value only replaces the stored one when its frame is newer, so backfills never regress it).
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "UPDATE frames SET decode_status = ?, decode_error = ?, decoded_at = ?, "
                "decoder = COALESCE(?, decoder), decoded_json = ? WHERE id = ?",
                (
                    status, error[:500] if error else None, to_iso_z(decoded_at), decoder,
                    json.dumps(dict(fields), sort_keys=True) if status == "ok" and fields else None, frame_id,
                ),
            )
            if status == "ok" and fields:
                row = self._db.execute(
                    f"SELECT f.norad_cat_id, f.source, f.station_name, {_EFFECTIVE_TIME} AS t "
                    "FROM frames f LEFT JOIN observations o ON o.id = f.observation_id WHERE f.id = ?",
                    (frame_id,),
                ).fetchone()
                if row is not None and row["norad_cat_id"] is not None and row["t"] is not None:
                    self._db.executemany(
                        """
                        INSERT INTO latest_values (norad_cat_id, field, value, frame_id, frame_time, source,
                                                   station_name, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (norad_cat_id, field) DO UPDATE SET
                            value = excluded.value, frame_id = excluded.frame_id, frame_time = excluded.frame_time,
                            source = excluded.source, station_name = excluded.station_name,
                            updated_at = excluded.updated_at
                        WHERE excluded.frame_time > latest_values.frame_time
                           OR (excluded.frame_time = latest_values.frame_time
                               AND excluded.frame_id >= latest_values.frame_id)
                        """,
                        [
                            (row["norad_cat_id"], name, json.dumps(value), frame_id, row["t"], row["source"],
                             row["station_name"], to_iso_z(decoded_at))
                            for name, value in fields.items()
                        ],
                    )
            self._db.execute("COMMIT")

    # -- latest values, history, listings (what the API serves) ------------------------------------
    def latest_values(self, norad_cat_id: int) -> dict[str, LatestValue]:
        """Newest decoded value of every field for one satellite, keyed by field name."""
        out: dict[str, LatestValue] = {}
        for r in self._db.execute(
            "SELECT field, value, frame_id, frame_time, source, station_name FROM latest_values "
            "WHERE norad_cat_id = ? ORDER BY field",
            (norad_cat_id,),
        ):
            out[r["field"]] = LatestValue(
                field=r["field"], value=json.loads(r["value"]), frame_id=r["frame_id"],
                frame_time=parse_iso8601(r["frame_time"]), source=r["source"], station_name=r["station_name"],
            )
        return out

    def history(
        self,
        field: str,
        *,
        norad_cat_id: int,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        source: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Decoded values of one field over time (oldest first, at most the newest ``limit`` points)."""
        path = json_path(field)
        sql = (
            f"SELECT f.id, {_EFFECTIVE_TIME} AS t, f.source, f.station_name, json_extract(f.decoded_json, ?) AS v "
            "FROM frames f LEFT JOIN observations o ON o.id = f.observation_id "
            "WHERE f.decode_status = 'ok' AND f.norad_cat_id = ? AND json_extract(f.decoded_json, ?) IS NOT NULL"
        )
        params: list[Any] = [path, norad_cat_id, path]
        if since is not None:
            sql += f" AND {_EFFECTIVE_TIME} >= ?"
            params.append(to_iso_z(since))
        if until is not None:
            sql += f" AND {_EFFECTIVE_TIME} <= ?"
            params.append(to_iso_z(until))
        if source is not None:
            sql += " AND f.source = ?"
            params.append(source)
        sql += f" ORDER BY {_EFFECTIVE_TIME} DESC, f.id DESC LIMIT ?"
        params.append(limit)
        rows = [
            {"time": r["t"], "value": r["v"], "frame_id": r["id"], "source": r["source"], "station_name": r["station_name"]}
            for r in self._db.execute(sql, params)
        ]
        rows.reverse()
        return rows

    def list_frames(
        self,
        *,
        norad_cat_id: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        source: str | None = None,
        station_name: str | None = None,
        decode_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Frame metadata (no bytes), newest first."""
        sql = (
            f"SELECT f.id, f.source, f.observation_id, f.norad_cat_id, f.station_name, f.url, "
            f"{_EFFECTIVE_TIME} AS t, f.status, f.size, f.sha256, f.downloaded_at, f.decode_status, f.decoder, "
            "f.decode_error, f.decoded_at FROM frames f LEFT JOIN observations o ON o.id = f.observation_id WHERE 1 = 1"
        )
        params: list[Any] = []
        if norad_cat_id is not None:
            sql += " AND f.norad_cat_id = ?"
            params.append(norad_cat_id)
        if since is not None:
            sql += f" AND {_EFFECTIVE_TIME} >= ?"
            params.append(to_iso_z(since))
        if until is not None:
            sql += f" AND {_EFFECTIVE_TIME} <= ?"
            params.append(to_iso_z(until))
        if source is not None:
            sql += " AND f.source = ?"
            params.append(source)
        if station_name is not None:
            sql += " AND f.station_name = ?"
            params.append(station_name)
        if decode_status is not None:
            sql += " AND f.decode_status = ?" if decode_status != "pending" else " AND f.decode_status IS NULL"
            if decode_status != "pending":
                params.append(decode_status)
        sql += f" ORDER BY {_EFFECTIVE_TIME} DESC, f.id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [self._frame_summary(r) for r in self._db.execute(sql, params)]

    @staticmethod
    def _frame_summary(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "source": r["source"],
            "norad_cat_id": r["norad_cat_id"],
            "observation_id": r["observation_id"],
            "station_name": r["station_name"],
            "frame_time": r["t"],
            "url": r["url"],
            "status": r["status"],
            "size": r["size"],
            "sha256": r["sha256"],
            "downloaded_at": r["downloaded_at"],
            "decode_status": r["decode_status"] or "pending",
            "decoder": r["decoder"],
            "decode_error": r["decode_error"],
            "decoded_at": r["decoded_at"],
        }

    def get_frame(self, frame_id: int) -> dict[str, Any] | None:
        """One frame with its bytes, decoded fields, push metadata and observation summary."""
        r = self._db.execute(
            f"SELECT f.*, {_EFFECTIVE_TIME} AS t, o.ground_station, o.start_time, o.end_time, o.transmitter_uuid, "
            "o.observation_frequency, o.transmitter_mode FROM frames f LEFT JOIN observations o ON o.id = f.observation_id "
            "WHERE f.id = ?",
            (frame_id,),
        ).fetchone()
        if r is None:
            return None
        out = self._frame_summary(r)
        out["raw"] = bytes(r["raw"] or b"")
        out["decoded"] = _load_json(r["decoded_json"])
        out["meta"] = _load_json(r["meta_json"]) or {}
        out["observation"] = None
        if r["observation_id"] is not None:
            out["observation"] = {
                "id": r["observation_id"],
                "ground_station": r["ground_station"],
                "station_name": r["station_name"],
                "start": r["start_time"],
                "end": r["end_time"],
                "transmitter_uuid": r["transmitter_uuid"],
                "transmitter_mode": r["transmitter_mode"],
                "observation_frequency": r["observation_frequency"],
            }
        return out

    _OBS_COLUMNS = (
        "id, norad_cat_id, sat_id, ground_station, station_name, station_lat, station_lng, station_alt, "
        "start_time, end_time, status, observer, transmitter_uuid, transmitter_description, transmitter_mode, "
        "observation_frequency, tle_source, payload_url, waterfall_url, demoddata_count, first_seen_at, "
        "last_seen_at, frames_complete"
    )

    def list_observations(
        self,
        *,
        norad_cat_id: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        ground_station: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Observation provenance rows (newest first) with per-observation frame counts."""
        sql = (
            f"SELECT {self._OBS_COLUMNS}, "
            "(SELECT COUNT(*) FROM frames f WHERE f.observation_id = observations.id) AS frames_total, "
            "(SELECT COUNT(*) FROM frames f WHERE f.observation_id = observations.id AND f.status = 'ok') AS frames_stored, "
            "(SELECT COUNT(*) FROM frames f WHERE f.observation_id = observations.id AND f.decode_status = 'ok') AS frames_decoded "
            "FROM observations WHERE 1 = 1"
        )
        params: list[Any] = []
        if norad_cat_id is not None:
            sql += " AND norad_cat_id = ?"
            params.append(norad_cat_id)
        if since is not None:
            sql += " AND start_time >= ?"
            params.append(to_iso_z(since))
        if until is not None:
            sql += " AND start_time <= ?"
            params.append(to_iso_z(until))
        if ground_station is not None:
            sql += " AND ground_station = ?"
            params.append(ground_station)
        sql += " ORDER BY start_time DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in self._db.execute(sql, params)]

    def get_observation(self, observation_id: int) -> dict[str, Any] | None:
        """One observation: stored columns, frame counts and the full SatNOGS record."""
        r = self._db.execute(
            f"SELECT {self._OBS_COLUMNS}, tle0, tle1, tle2, raw_json, "
            "(SELECT COUNT(*) FROM frames f WHERE f.observation_id = observations.id) AS frames_total, "
            "(SELECT COUNT(*) FROM frames f WHERE f.observation_id = observations.id AND f.status = 'ok') AS frames_stored, "
            "(SELECT COUNT(*) FROM frames f WHERE f.observation_id = observations.id AND f.decode_status = 'ok') AS frames_decoded "
            "FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if r is None:
            return None
        out = dict(r)
        out["record"] = _load_json(out.pop("raw_json"))
        return out

    def satellites(self) -> list[dict[str, Any]]:
        """Per-satellite counters: frames by source, decoded frames, newest frame time, watermark."""
        rows = self._db.execute(
            f"""
            SELECT f.norad_cat_id,
                   COUNT(*) AS frames,
                   SUM(f.status = 'ok') AS frames_stored,
                   SUM(f.decode_status = 'ok') AS frames_decoded,
                   SUM(f.source = 'groundstation') AS frames_from_groundstation,
                   MAX(CASE WHEN f.status = 'ok' THEN {_EFFECTIVE_TIME} END) AS latest_frame_time,
                   (SELECT COUNT(*) FROM observations x WHERE x.norad_cat_id = f.norad_cat_id) AS observations,
                   (SELECT swept_through FROM watermarks w WHERE w.norad_cat_id = f.norad_cat_id) AS watermark
            FROM frames f LEFT JOIN observations o ON o.id = f.observation_id
            WHERE f.norad_cat_id IS NOT NULL
            GROUP BY f.norad_cat_id ORDER BY f.norad_cat_id
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # -- sweeps ------------------------------------------------------------------------------------
    @staticmethod
    def _sweep_from_row(row: sqlite3.Row) -> Sweep:
        return Sweep(
            id=row["id"],
            norad_cat_id=row["norad_cat_id"],
            status_filter=row["status_filter"],
            since=parse_iso8601(row["since"]),
            started_at=parse_iso8601(row["started_at"]),
            state=row["state"],
            next_url=row["next_url"],
            pages_done=row["pages_done"],
            observations_seen=row["observations_seen"],
        )

    def unfinished_sweep(self, norad_cat_id: int, status_filter: str) -> Sweep | None:
        """The most recent still-running sweep for this satellite/status, if any."""
        row = self._db.execute(
            "SELECT * FROM sweeps WHERE norad_cat_id = ? AND status_filter = ? AND state = 'running' "
            "ORDER BY id DESC LIMIT 1",
            (norad_cat_id, status_filter),
        ).fetchone()
        return self._sweep_from_row(row) if row else None

    def last_sweep(self, norad_cat_id: int) -> Sweep | None:
        """The most recent sweep row of any state (for status reporting)."""
        row = self._db.execute(
            "SELECT * FROM sweeps WHERE norad_cat_id = ? ORDER BY id DESC LIMIT 1", (norad_cat_id,)
        ).fetchone()
        return self._sweep_from_row(row) if row else None

    def open_sweep(
        self, *, norad_cat_id: int, status_filter: str, since: dt.datetime, started_at: dt.datetime, first_url: str
    ) -> int:
        """Start a new sweep (aborting any other running sweep for the same satellite/status)."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")  # take the write lock up front (WAL-safe with a concurrent decoder)
            self._db.execute(
                "UPDATE sweeps SET state = 'aborted', finished_at = ? "
                "WHERE norad_cat_id = ? AND status_filter = ? AND state = 'running'",
                (to_iso_z(started_at), norad_cat_id, status_filter),
            )
            cur = self._db.execute(
                "INSERT INTO sweeps (norad_cat_id, status_filter, since, started_at, next_url) VALUES (?, ?, ?, ?, ?)",
                (norad_cat_id, status_filter, to_iso_z(since), to_iso_z(started_at), first_url),
            )
            self._db.execute("COMMIT")
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def record_page(self, sweep_id: int, *, next_url: str | None, observations: int, at: dt.datetime) -> None:
        """Persist progress after a page has been fully stored (so a crash resumes from ``next_url``).

        When ``next_url`` is ``None`` the page was the last one, and the same transaction marks the
        sweep ``done`` and advances the satellite's watermark to the sweep's *own* start time (the
        moment its first page was requested), never to a later resume time.
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "UPDATE sweeps SET next_url = ?, pages_done = pages_done + 1, "
                "observations_seen = observations_seen + ? WHERE id = ?",
                (next_url, observations, sweep_id),
            )
            if next_url is None:
                self._complete_sweep(sweep_id, finished_at=at)
            self._db.execute("COMMIT")

    def complete_sweep(self, sweep_id: int, *, finished_at: dt.datetime) -> None:
        """Mark a sweep whose pages are all stored as ``done`` and advance the watermark (one transaction)."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._complete_sweep(sweep_id, finished_at=finished_at)
            self._db.execute("COMMIT")

    def _complete_sweep(self, sweep_id: int, *, finished_at: dt.datetime) -> None:
        row = self._db.execute("SELECT norad_cat_id, started_at FROM sweeps WHERE id = ?", (sweep_id,)).fetchone()
        if row is None:
            raise KeyError(f"sweep {sweep_id} does not exist")
        self._db.execute(
            "UPDATE sweeps SET state = 'done', finished_at = ?, next_url = NULL WHERE id = ?",
            (to_iso_z(finished_at), sweep_id),
        )
        self._db.execute(
            "INSERT INTO watermarks (norad_cat_id, swept_through, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (norad_cat_id) DO UPDATE SET "
            "swept_through = MAX(excluded.swept_through, watermarks.swept_through), updated_at = excluded.updated_at",
            (row["norad_cat_id"], row["started_at"], to_iso_z(finished_at)),
        )

    def finish_sweep(self, sweep_id: int, *, finished_at: dt.datetime, state: str = "aborted") -> None:
        """Close a sweep without touching the watermark (used to abort an unresumable sweep)."""
        self._db.execute(
            "UPDATE sweeps SET state = ?, finished_at = ?, next_url = NULL WHERE id = ?",
            (state, to_iso_z(finished_at), sweep_id),
        )

    # -- watermarks --------------------------------------------------------------------------------
    def get_watermark(self, norad_cat_id: int) -> dt.datetime | None:
        row = self._db.execute("SELECT swept_through FROM watermarks WHERE norad_cat_id = ?", (norad_cat_id,)).fetchone()
        return parse_iso8601(row["swept_through"]) if row else None

    def set_watermark(self, norad_cat_id: int, swept_through: dt.datetime, *, updated_at: dt.datetime) -> None:
        self._db.execute(
            "INSERT INTO watermarks (norad_cat_id, swept_through, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (norad_cat_id) DO UPDATE SET swept_through = excluded.swept_through, "
            "updated_at = excluded.updated_at",
            (norad_cat_id, to_iso_z(swept_through), to_iso_z(updated_at)),
        )

    # -- reporting ---------------------------------------------------------------------------------
    def stats(self, norad_cat_id: int | None = None) -> dict[str, Any]:
        """Summary counters for ``--stats``, log lines and the health endpoint."""
        where, params = ("", ()) if norad_cat_id is None else (" WHERE norad_cat_id = ?", (norad_cat_id,))
        obs = self._db.execute(
            f"SELECT COUNT(*) AS n, MIN(start_time) AS oldest, MAX(start_time) AS newest, "
            f"SUM(frames_complete) AS complete FROM observations{where}",
            params,
        ).fetchone()
        frames = {
            r["status"]: r["n"]
            for r in self._db.execute(f"SELECT status, COUNT(*) AS n FROM frames{where} GROUP BY status", params)
        }
        decoded = self._db.execute(
            f"SELECT SUM(decode_status = 'ok') AS ok, SUM(decode_status = 'error') AS error, "
            f"SUM(status = 'ok' AND decode_status IS NULL) AS pending, "
            f"SUM(source = 'groundstation') AS pushed FROM frames{where}",
            params,
        ).fetchone()
        latest = self._db.execute(
            f"SELECT MAX({_EFFECTIVE_TIME}) AS t FROM frames f LEFT JOIN observations o ON o.id = f.observation_id "
            "WHERE f.status = 'ok'" + (" AND f.norad_cat_id = ?" if norad_cat_id is not None else ""),
            params,
        ).fetchone()
        sweeps = {
            r["state"]: r["n"]
            for r in self._db.execute(f"SELECT state, COUNT(*) AS n FROM sweeps{where} GROUP BY state", params)
        }
        watermark = self.get_watermark(norad_cat_id) if norad_cat_id is not None else None
        return {
            "db": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "norad_cat_id": norad_cat_id,
            "observations": obs["n"],
            "observations_complete": obs["complete"] or 0,
            "observations_oldest_start": obs["oldest"],
            "observations_newest_start": obs["newest"],
            "frames_ok": frames.get("ok", 0),
            "frames_pending": frames.get("pending", 0),
            "frames_failed": frames.get("failed", 0),
            "frames_decoded": decoded["ok"] or 0,
            "frames_decode_error": decoded["error"] or 0,
            "frames_to_decode": decoded["pending"] or 0,
            "frames_from_groundstation": decoded["pushed"] or 0,
            "latest_frame_time": latest["t"],
            "sweeps": sweeps,
            "watermark": to_iso_z(watermark) if watermark else None,
        }
