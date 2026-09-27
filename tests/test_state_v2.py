"""Schema version 2: in-place migration from v1, pushed frames, latest values, history and listings."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3

import pytest

from pvdx_ground.ingest.state import SCHEMA_VERSION, StateStore, json_path
from tests.conftest import NORAD, UTC, load_frames, load_page

T0 = dt.datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
H = dt.timedelta(hours=1)

V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE observations (
    id INTEGER PRIMARY KEY, norad_cat_id INTEGER NOT NULL, sat_id TEXT, ground_station INTEGER, station_name TEXT,
    station_lat REAL, station_lng REAL, station_alt REAL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
    status TEXT, observer TEXT, transmitter_uuid TEXT, transmitter_description TEXT, transmitter_mode TEXT,
    observation_frequency INTEGER, tle0 TEXT, tle1 TEXT, tle2 TEXT, tle_source TEXT, payload_url TEXT,
    waterfall_url TEXT, demoddata_count INTEGER NOT NULL DEFAULT 0, raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, frames_complete INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_observations_norad_start ON observations (norad_cat_id, start_time);
CREATE TABLE frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id INTEGER NOT NULL REFERENCES observations (id),
    url TEXT NOT NULL UNIQUE, frame_time TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ok', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sha256 TEXT, size INTEGER, raw BLOB,
    downloaded_at TEXT, decoded_at TEXT, decode_status TEXT, decode_error TEXT
);
CREATE INDEX ix_frames_status ON frames (status);
CREATE INDEX ix_frames_observation ON frames (observation_id);
CREATE INDEX ix_frames_decode ON frames (status, decode_status);
CREATE TABLE sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, norad_cat_id INTEGER NOT NULL, status_filter TEXT NOT NULL,
    since TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'done', 'aborted')),
    next_url TEXT, pages_done INTEGER NOT NULL DEFAULT 0, observations_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE watermarks (norad_cat_id INTEGER PRIMARY KEY, swept_through TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO meta (key, value) VALUES ('schema_version', '1');
"""


def obs_with_frames() -> dict:
    page, _ = load_page(1)
    return next(o for o in page if o["demoddata"])


def make_v1_db(path) -> tuple[int, list[str]]:
    """A schema-1 database with one observation and three frames: decoded ok, pending, decode error."""
    obs = obs_with_frames()
    urls = [e["payload_demod"] for e in obs["demoddata"]][:2] + ["https://s3/x/data_1_2026-09-26T05-00-00_g0"]
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA)
    db.execute(
        "INSERT INTO observations (id, norad_cat_id, ground_station, station_name, start_time, end_time, raw_json, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (obs["id"], obs["norad_cat_id"], obs["ground_station"], obs["station_name"], obs["start"], obs["end"],
         json.dumps(obs), "2026-09-26T12:00:00Z", "2026-09-26T12:00:00Z"),
    )
    db.execute("INSERT INTO frames (observation_id, url, frame_time, status, attempts, raw, size, decode_status) "
               "VALUES (?, ?, ?, 'ok', 1, X'0102', 2, 'ok')", (obs["id"], urls[0], "2026-09-26T05:51:26Z"))
    db.execute("INSERT INTO frames (observation_id, url, frame_time) VALUES (?, ?, ?)", (obs["id"], urls[1], None))
    db.execute("INSERT INTO frames (observation_id, url, frame_time, status, attempts, raw, size, decode_status, "
               "decode_error) VALUES (?, ?, ?, 'ok', 1, X'FF', 1, 'error', 'bad')", (obs["id"], urls[2], "2026-09-26T05:00:00Z"))
    db.execute("INSERT INTO watermarks VALUES (?, '2026-09-26T00:00:00Z', '2026-09-26T00:00:00Z')", (obs["norad_cat_id"],))
    db.commit()
    db.close()
    return obs["id"], urls


def test_v1_database_is_migrated_in_place(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="pvdx_ground.ingest.state")
    path = tmp_path / "state.db"
    obs_id, urls = make_v1_db(path)
    with StateStore(path) as store:
        assert store.schema_version() == SCHEMA_VERSION == 2
        columns = {r[1] for r in store._db.execute("PRAGMA table_info(frames)")}
        assert {"source", "norad_cat_id", "station_name", "meta_json", "decoder", "decoded_json"} <= columns
        assert "frames_v1" not in {r[0] for r in store._db.execute("SELECT name FROM sqlite_master")}
        # satellite and station were copied from the observation, bytes and bookkeeping kept
        todo = store.frames_to_decode()
        assert [f.url for f in todo] == [urls[0]]  # the decoded frame is queued again to fill decoded fields
        assert todo[0].norad_cat_id == NORAD and todo[0].station_name and todo[0].raw == b"\x01\x02"
        assert todo[0].source == "satnogs" and todo[0].observation_id == obs_id
        assert [p.url for p in store.pending_frames()] == [urls[1]]  # still waiting for download
        stats = store.stats(NORAD)
        assert stats["frames_ok"] == 2 and stats["frames_pending"] == 1 and stats["frames_decode_error"] == 1
        assert store.get_watermark(NORAD) == dt.datetime(2026, 9, 26, tzinfo=UTC)
        # ids survive, so InfluxDB points written before the migration still match their frames
        assert {f.id for f in store.frames_to_decode(redo=True)} == {1, 3}
    assert any("migrating" in r.message for r in caplog.records)
    caplog.clear()
    with StateStore(path) as store:  # a second open is a no-op
        assert store.schema_version() == 2
    assert not any("migrating" in r.message for r in caplog.records)


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "state.db"
    with StateStore(path) as store:
        store._db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(RuntimeError, match="newer"):
        StateStore(path)


def test_pushed_frames_are_stored_once_and_need_no_download(store):
    fid, new = store.add_pushed_frame(norad_cat_id=NORAD, station_name="BSE", raw=b"\x01\x02", frame_time=T0,
                                      received_at=T0, meta={"rssi": -90})
    assert new
    assert store.add_pushed_frame(norad_cat_id=NORAD, station_name="BSE", raw=b"\x01\x02", frame_time=T0,
                                  received_at=T0 + H, meta={}) == (fid, False)
    fid2, new2 = store.add_pushed_frame(norad_cat_id=NORAD, station_name="BSE", raw=b"\x01\x02", frame_time=T0 + H,
                                        received_at=T0 + H)
    assert new2 and fid2 != fid
    other, _ = store.add_pushed_frame(norad_cat_id=NORAD, station_name="Other station", raw=b"\x01\x02",
                                      frame_time=T0, received_at=T0)
    assert other not in (fid, fid2)
    assert store.pending_frames() == []
    frames = store.frames_to_decode(norad_cat_id=NORAD)
    assert [f.id for f in frames] == [fid, fid2, other]
    first = frames[0]
    assert first.source == "groundstation" and first.observation_id is None and first.url is None
    assert first.station_name == "BSE" and first.timestamp == T0 and first.meta == {"rssi": -90}
    assert first.sha256 is not None and first.observation_start is None
    stats = store.stats(NORAD)
    assert stats["frames_from_groundstation"] == 3 and stats["frames_ok"] == 3 and stats["frames_to_decode"] == 3
    with pytest.raises(ValueError):
        store.add_pushed_frame(norad_cat_id=NORAD, station_name="", raw=b"\x01", frame_time=T0, received_at=T0)


def test_latest_values_only_move_forward_in_frame_time(store):
    older, _ = store.add_pushed_frame(norad_cat_id=NORAD, station_name="BSE", raw=b"\x01", frame_time=T0, received_at=T0)
    newer, _ = store.add_pushed_frame(norad_cat_id=NORAD, station_name="BSE", raw=b"\x02", frame_time=T0 + H, received_at=T0)
    store.mark_decoded(newer, "ok", decoded_at=T0, fields={"batt": 8100, "mode": "SAFE"}, decoder="test")
    store.mark_decoded(older, "ok", decoded_at=T0, fields={"batt": 8000, "temp": 21.5}, decoder="test")  # backfill
    latest = store.latest_values(NORAD)
    assert latest["batt"].value == 8100 and latest["batt"].frame_id == newer and latest["batt"].frame_time == T0 + H
    assert latest["temp"].value == 21.5 and latest["temp"].frame_id == older  # a field only the older frame has
    assert latest["mode"].value == "SAFE" and latest["mode"].source == "groundstation"
    assert latest["batt"].as_dict()["frame_time"] == "2026-09-26T13:00:00Z"
    # errors and empty results never touch the latest values; re-decoding the newest frame replaces them
    store.mark_decoded(newer, "error", decoded_at=T0, error="broken", decoder="test")
    assert store.latest_values(NORAD)["batt"].value == 8100
    store.mark_decoded(newer, "ok", decoded_at=T0, fields={"batt": 8200}, decoder="test")
    assert store.latest_values(NORAD)["batt"].value == 8200
    assert store.latest_values(NORAD + 1) == {}
    assert store.frames_to_decode() == []
    decoded = store.get_frame(newer)
    assert decoded["decoded"] == {"batt": 8200} and decoded["decoder"] == "test" and decoded["decode_status"] == "ok"


def seed(store) -> tuple[list[int], int]:
    """Two SatNOGS frames from the recorded observation, decoded, plus one pushed frame."""
    obs = obs_with_frames()
    store.upsert_observation(obs, seen_at=T0)
    frames = load_frames()
    for ref in store.pending_frames():
        store.store_frame(ref.id, frames[ref.url], downloaded_at=T0)
    ids = []  # in frame-time order (the recorded ids happen to be in the opposite order)
    for i, f in enumerate(sorted(store.frames_to_decode(), key=lambda f: f.timestamp)):
        store.mark_decoded(f.id, "ok", decoded_at=T0, fields={"batt": 8000 + i, "mode": "SAFE"}, decoder="test")
        ids.append(f.id)
    pushed, _ = store.add_pushed_frame(norad_cat_id=NORAD, station_name="BSE", raw=b"\x09", frame_time=T0 + 2 * H,
                                       received_at=T0 + 2 * H, meta={"frequency": 145935000})
    store.mark_decoded(pushed, "ok", decoded_at=T0, fields={"batt": 8100}, decoder="test")
    return ids, pushed


def test_history_is_ascending_and_filterable(store):
    ids, pushed = seed(store)
    points = store.history("batt", norad_cat_id=NORAD)
    assert [p["frame_id"] for p in points] == ids + [pushed]  # oldest first (frame times 05:50, 05:51, 14:00)
    assert [p["value"] for p in points] == [8000, 8001, 8100]
    assert points[-1]["source"] == "groundstation" and points[0]["source"] == "satnogs"
    assert points[0]["time"] == "2026-09-26T05:50:41Z"
    assert [p["frame_id"] for p in store.history("batt", norad_cat_id=NORAD, source="satnogs")] == ids
    assert store.history("batt", norad_cat_id=NORAD, since=T0)[0]["frame_id"] == pushed
    assert store.history("batt", norad_cat_id=NORAD, until=T0)[-1]["frame_id"] == ids[-1]
    assert [p["frame_id"] for p in store.history("batt", norad_cat_id=NORAD, limit=1)] == [pushed]  # newest kept
    assert store.history("mode", norad_cat_id=NORAD)[0]["value"] == "SAFE"
    assert store.history("nope", norad_cat_id=NORAD) == []
    with pytest.raises(ValueError):
        json_path('bad"name')


def test_listings_and_lookups(store):
    ids, pushed = seed(store)
    listed = store.list_frames(norad_cat_id=NORAD)
    assert [f["id"] for f in listed] == [pushed] + ids[::-1]  # newest first
    assert listed[0]["source"] == "groundstation" and listed[0]["decode_status"] == "ok" and "raw" not in listed[0]
    assert [f["id"] for f in store.list_frames(source="satnogs")] == ids[::-1]
    assert store.list_frames(station_name="BSE")[0]["id"] == pushed
    assert store.list_frames(decode_status="pending") == []
    assert store.list_frames(limit=1, offset=1)[0]["id"] == ids[-1]
    frame = store.get_frame(pushed)
    assert frame["raw"] == b"\x09" and frame["meta"] == {"frequency": 145935000} and frame["observation"] is None
    frame = store.get_frame(ids[0])
    assert frame["observation"]["id"] == obs_with_frames()["id"] and frame["observation"]["station_name"]
    assert store.get_frame(10_000) is None

    observations = store.list_observations(norad_cat_id=NORAD)
    assert len(observations) == 1
    assert observations[0]["frames_total"] == observations[0]["frames_stored"] == observations[0]["frames_decoded"] == len(ids)
    assert store.list_observations(ground_station=observations[0]["ground_station"])[0]["id"] == observations[0]["id"]
    assert store.list_observations(ground_station=-1) == []
    one = store.get_observation(observations[0]["id"])
    assert one["record"]["id"] == observations[0]["id"] and one["tle1"] and one["frames_decoded"] == len(ids)
    assert store.get_observation(1) is None

    sats = store.satellites()
    assert len(sats) == 1 and sats[0]["norad_cat_id"] == NORAD
    assert sats[0]["frames"] == len(ids) + 1 and sats[0]["frames_decoded"] == len(ids) + 1
    assert sats[0]["frames_from_groundstation"] == 1 and sats[0]["observations"] == 1
    assert sats[0]["latest_frame_time"] == "2026-09-26T14:00:00Z" and sats[0]["watermark"] is None
    assert store.last_sweep(NORAD) is None
