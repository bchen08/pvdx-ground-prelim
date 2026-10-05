"""Duplicate receptions (schema version 3): every copy is kept, copies of one transmission share a primary frame,
and only primaries feed latest values, the default history and the ``primary=true`` InfluxDB points."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from pvdx_ground.api import create_app
from pvdx_ground.decode import DecodedFrame, get_decoder
from pvdx_ground.decode.__main__ import decode_once
from pvdx_ground.ingest.state import SCHEMA_VERSION, StateStore, frame_family
from pvdx_ground.storage.influx import InfluxWriter
from pvdx_ground.timeutil import to_iso_z
from tests.conftest import NORAD, UTC
from tests.test_api import make_settings
from tests.test_decode_storage import make_frame
from tests.test_serve_flow import CROCUBE, FRAMES, RecordingWriter, settings_for

T = dt.datetime(2026, 9, 26, 12, 5, 0, tzinfo=UTC)
S = dt.timedelta(seconds=1)
BEACON = b"\x8a\xa6\xa8\x82\x84\x86\x60static beacon"

V2_SCHEMA = """
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'satnogs' CHECK (source IN ('satnogs', 'groundstation')),
    observation_id INTEGER REFERENCES observations (id), norad_cat_id INTEGER, station_name TEXT, url TEXT UNIQUE,
    frame_time TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ok', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sha256 TEXT, size INTEGER, raw BLOB, meta_json TEXT,
    downloaded_at TEXT, decoded_at TEXT, decode_status TEXT, decoder TEXT, decoded_json TEXT, decode_error TEXT
);
CREATE INDEX ix_frames_status ON frames (status);
CREATE INDEX ix_frames_observation ON frames (observation_id);
CREATE INDEX ix_frames_decode ON frames (status, decode_status);
CREATE INDEX ix_frames_norad_time ON frames (norad_cat_id, frame_time);
CREATE UNIQUE INDEX ux_frames_pushed ON frames (norad_cat_id, station_name, frame_time, sha256) WHERE source = 'groundstation';
CREATE TABLE sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, norad_cat_id INTEGER NOT NULL, status_filter TEXT NOT NULL,
    since TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'done', 'aborted')),
    next_url TEXT, pages_done INTEGER NOT NULL DEFAULT 0, observations_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE watermarks (norad_cat_id INTEGER PRIMARY KEY, swept_through TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE latest_values (
    norad_cat_id INTEGER NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL, frame_id INTEGER NOT NULL,
    frame_time TEXT NOT NULL, source TEXT NOT NULL, station_name TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (norad_cat_id, field)
);
INSERT INTO meta (key, value) VALUES ('schema_version', '2');
"""


def demod_url(obs_id: int, when: dt.datetime, suffix: str = "") -> str:
    return f"https://s3.example/satnogs-network/data_obs/{obs_id}/data_{obs_id}_{when:%Y-%m-%dT%H-%M-%S}{suffix}"


def add_observation(store: StateStore, obs_id: int, station: str, receptions: list[tuple[dt.datetime, str, bytes]],
                    *, norad: int = NORAD) -> list[int]:
    """Store (or re-sight) a SatNOGS observation and download its new frames; returns the frame ids in order."""
    urls = [demod_url(obs_id, when, suffix) for when, suffix, _ in receptions]
    obs = {"id": obs_id, "norad_cat_id": norad, "ground_station": obs_id, "station_name": station,
           "start": to_iso_z(T - 300 * S), "end": to_iso_z(T + 600 * S),
           "demoddata": [{"payload_demod": url} for url in urls]}
    store.upsert_observation(obs, seen_at=T)
    pending = {ref.url: ref.id for ref in store.pending_frames()}
    for url, (_, _, raw) in zip(urls, receptions):
        if url in pending:
            store.store_frame(pending[url], raw, downloaded_at=T)
    ids = {f["url"]: f["id"] for f in store.list_frames(limit=1000)}
    return [ids[url] for url in urls]


def push(store: StateStore, station: str, when: dt.datetime, raw: bytes = BEACON, *, norad: int = NORAD) -> int:
    frame_id, _ = store.add_pushed_frame(norad_cat_id=norad, station_name=station, raw=raw, frame_time=when,
                                         received_at=when)
    return frame_id


def primaries(store: StateStore) -> dict[int, int | None]:
    return {f["id"]: f["primary_frame_id"] for f in store.list_frames(limit=1000)}


# -- families ---------------------------------------------------------------------------------------------
def test_frame_family_from_source_and_file_name():
    assert frame_family("groundstation", None) == "push"
    assert frame_family("satnogs", demod_url(1, T)) == "native"
    assert frame_family("satnogs", demod_url(1, T, "_3")) == "native"
    assert frame_family("satnogs", demod_url(1, T, "_g0")) == "grsat"
    assert frame_family("satnogs", demod_url(1, T, "_g12")) == "grsat"
    assert frame_family("satnogs", "https://s3.example/data_g1/data_1_2026-09-26T12-05-00") == "native"


# -- migration ----------------------------------------------------------------------------------------------
def make_v2_db(path) -> None:
    """A schema-2 database: one transmission heard four ways, a retransmission, a pending and an undecodable frame."""
    db = sqlite3.connect(path)
    db.executescript(V2_SCHEMA)
    for obs_id, station in ((100, "Alpha"), (200, "Beta")):
        db.execute(
            "INSERT INTO observations (id, norad_cat_id, ground_station, station_name, start_time, end_time, raw_json, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)",
            (obs_id, NORAD, obs_id, station, to_iso_z(T - 300 * S), to_iso_z(T + 600 * S), to_iso_z(T), to_iso_z(T)),
        )
    digest = "a" * 64
    rows = [  # id, source, observation, station, url, frame time, status, sha256, decode status
        (1, "satnogs", 100, "Alpha", demod_url(100, T, "_g0"), T, "ok", digest, "ok"),  # gr-satellites, 15 s early
        (2, "satnogs", 100, "Alpha", demod_url(100, T + 15 * S), T + 15 * S, "ok", digest, "ok"),
        (3, "satnogs", 200, "Beta", demod_url(200, T + 30 * S), T + 30 * S, "ok", digest, "ok"),
        (4, "satnogs", 100, "Alpha", demod_url(100, T + 255 * S, "_1"), T + 255 * S, "ok", digest, "ok"),  # repeat
        (5, "groundstation", None, "BSE", None, T + 20 * S, "ok", digest, "ok"),
        (6, "satnogs", 100, "Alpha", demod_url(100, T + 300 * S), T + 300 * S, "pending", None, None),
        (7, "satnogs", 200, "Beta", demod_url(200, T + 40 * S, "_g1"), T + 40 * S, "ok", "b" * 64, "error"),
    ]
    for frame_id, source, obs_id, station, url, when, status, sha, decoded in rows:
        db.execute(
            "INSERT INTO frames (id, source, observation_id, norad_cat_id, station_name, url, frame_time, status, sha256, "
            "decode_status, decoded_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (frame_id, source, obs_id, NORAD, station, url, to_iso_z(when), status, sha, decoded,
             json.dumps({"batt": 8000}) if decoded == "ok" else None),
        )
    db.execute("INSERT INTO latest_values VALUES (?, 'batt', '8000', 4, ?, 'satnogs', 'Alpha', ?)",
               (NORAD, to_iso_z(T + 255 * S), to_iso_z(T)))
    db.commit()
    db.close()


def test_v2_database_gets_families_and_primaries(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="pvdx_ground.ingest.state")
    path = tmp_path / "state.db"
    make_v2_db(path)
    with StateStore(path) as store:
        assert store.schema_version() == SCHEMA_VERSION == 3
        families = dict(store._db.execute("SELECT id, family FROM frames").fetchall())
        assert families == {1: "grsat", 2: "native", 3: "native", 4: "native", 5: "push", 6: "native", 7: "grsat"}
        # the native copy wins over the earlier '_g0' row; Beta and the push are copies; 240 s later is a new transmission
        assert primaries(store) == {1: 2, 2: 2, 3: 2, 4: 4, 5: 2, 6: None, 7: 7}
        indexes = {r[0] for r in store._db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "ix_frames_norad_sha256" in indexes
        assert store.latest_values(NORAD)["batt"].frame_id == 4  # untouched
        assert [p["frame_id"] for p in store.history("batt", norad_cat_id=NORAD)] == [2, 4]
        assert len(store.history("batt", norad_cat_id=NORAD, copies=True)) == 5
    assert any("2 to 3" in r.message for r in caplog.records)
    assert any("6 stored frame(s) grouped into 3 transmission(s), 3 duplicate" in r.message for r in caplog.records)
    caplog.clear()
    with StateStore(path) as store:  # a second open is a no-op
        assert store.schema_version() == 3
    assert not any("migrating" in r.message for r in caplog.records)


# -- assignment rules ---------------------------------------------------------------------------------------
def test_native_copy_wins_over_an_earlier_gr_satellites_copy_with_a_lower_id(store):
    grsat, native = add_observation(store, 100, "Alpha", [(T, "_g1", BEACON), (T + 15 * S, "", BEACON)])
    assert grsat < native
    assert store.assign_primaries() == {grsat: native, native: native}
    frames = {f["id"]: f for f in store.list_frames()}
    assert frames[grsat]["family"] == "grsat" and frames[native]["family"] == "native"
    assert frames[native]["primary"] is True and frames[grsat]["primary"] is False
    assert store.assign_primaries() == {}  # nothing left to assign


def test_window_is_30_s_within_one_observation_and_45_s_across_stations(store):
    same_a, same_b, cross_a, far_a = add_observation(
        store, 100, "Alpha", [(T, "", b"P"), (T + 40 * S, "_1", b"P"), (T, "_2", b"Q"), (T, "_3", b"R")]
    )
    cross_b, far_b = add_observation(store, 200, "Beta", [(T + 40 * S, "", b"Q"), (T + 46 * S, "_1", b"R")])
    assigned = store.assign_primaries()
    assert assigned[same_b] == same_b  # 40 s apart in one observation: two transmissions
    assert assigned[cross_b] == cross_a  # 40 s apart at another station: a copy (station clocks differ)
    assert assigned[far_b] == far_b and assigned[far_a] == far_a  # 46 s: beyond the cross-station window


def test_same_bytes_237_s_later_is_a_new_transmission(store):
    first, again = add_observation(store, 100, "Alpha", [(T, "", BEACON), (T + 237 * S, "", BEACON)])
    assert store.assign_primaries() == {first: first, again: again}


def test_pushed_copy_of_a_satnogs_frame_joins_its_primary(store):
    (native,) = add_observation(store, 100, "Alpha", [(T, "", BEACON)])
    store.assign_primaries()
    pushed = push(store, "BSE", T + 20 * S)
    other_satellite = push(store, "BSE", T + 20 * S, norad=NORAD + 1)
    assert store.assign_primaries([pushed, other_satellite]) == {pushed: native, other_satellite: other_satellite}
    frame = store.get_frame(pushed)
    assert frame["family"] == "push" and frame["primary_frame_id"] == native and frame["primary"] is False


def test_window_is_anchored_on_the_primary_never_chained(store):
    a = push(store, "Station A", T)
    b = push(store, "Station B", T + 40 * S)
    c = push(store, "Station C", T + 80 * S)  # 40 s after B, but 80 s after the primary A
    assert store.assign_primaries() == {a: a, b: a, c: c}


def test_assignments_are_sticky(store):
    (grsat,) = add_observation(store, 100, "Alpha", [(T, "_g0", BEACON)])
    assert store.assign_primaries() == {grsat: grsat}
    # the native file shows up later (a later sweep): it becomes a copy, the gr-satellites primary stays
    native = add_observation(store, 100, "Alpha", [(T, "_g0", BEACON), (T + 15 * S, "", BEACON)])[1]
    assert store.assign_primaries() == {native: grsat}
    assert primaries(store) == {grsat: grsat, native: grsat}


# -- decode stage, InfluxDB -------------------------------------------------------------------------------
def test_decode_folds_latest_values_from_primaries_only(tmp_path):
    settings = settings_for(tmp_path)
    raw = (FRAMES / "crocube_psu_beacon.bin").read_bytes()
    with StateStore(settings.state_db) as store:
        first = push(store, "BSE", T, raw, norad=CROCUBE)
        later = push(store, "Elsewhere", T + 30 * S, raw, norad=CROCUBE)  # newer, but a copy
        writer = RecordingWriter()
        counts = decode_once(store, get_decoder("satnogs:crocube"), writer, norad=CROCUBE, redo=False, limit=None,
                             dry_run=False)
        assert counts["decoded"] == 2 and counts["copies"] == 1 and counts["written"] == 2
        written = {d.frame.id: d.frame for d in writer.batches[0]}  # every reception is still written
        assert written[later].is_copy and written[later].primary_frame_id == first and not written[first].is_copy
        latest = store.latest_values(CROCUBE)
        assert latest["psu_battery"].frame_id == first and latest["psu_battery"].frame_time == T
        assert latest["psu_battery"].station_name == "BSE"
        assert store.get_frame(later)["decoded"] == store.get_frame(first)["decoded"]  # the copy keeps its fields
        # a redo keeps the assignments and still leaves the latest values on the primary
        decode_once(store, get_decoder("satnogs:crocube"), writer, norad=CROCUBE, redo=True, limit=None, dry_run=False)
        assert store.latest_values(CROCUBE)["psu_battery"].frame_id == first
        assert primaries(store) == {first: first, later: first}


def test_decode_assigns_primaries_for_every_outcome_but_not_in_a_dry_run(tmp_path):
    settings = settings_for(tmp_path)
    with StateStore(settings.state_db) as store:
        noise = push(store, "BSE", T, b"\x00noise", norad=CROCUBE)
        decode_once(store, get_decoder("satnogs:crocube"), None, norad=CROCUBE, redo=False, limit=None, dry_run=True)
        assert primaries(store) == {noise: None}
        decode_once(store, get_decoder("satnogs:crocube"), RecordingWriter(), norad=CROCUBE, redo=False, limit=None,
                    dry_run=False)
        assert store.get_frame(noise)["decode_status"] == "error" and primaries(store) == {noise: noise}


def test_influx_points_carry_the_primary_tag():
    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")
    try:
        def line(**kw) -> str:
            return writer.point(DecodedFrame(frame=make_frame(**kw), decoder="x", fields={"a": 1})).to_line_protocol()
        primary, copy, unassigned = line(id=7, primary_frame_id=7), line(id=8, primary_frame_id=7), line(id=9)
    finally:
        writer.close()
    assert "primary=true" in primary and "primary=false" in copy
    assert "primary=true" in unassigned  # only a known copy is tagged false
    assert copy.endswith(str(int(T.replace(hour=5, minute=51, second=26).timestamp()) * 10**9 + 8))  # same time scheme


# -- API ----------------------------------------------------------------------------------------------------
def seed_copies(path) -> dict[str, int]:
    """Two receptions of one transmission, a second transmission (all decoded) and a stored, undecoded frame."""
    with StateStore(path) as store:
        ids = {"first": push(store, "BSE", T, b"\x01\x02"), "copy": push(store, "Other", T + 10 * S, b"\x01\x02"),
               "next": push(store, "BSE", T + 600 * S, b"\x03")}
        store.assign_primaries(ids.values())
        for name, batt in (("first", 8000), ("copy", 8000), ("next", 8100)):
            store.mark_decoded(ids[name], "ok", decoded_at=T, fields={"batt": batt}, decoder="test")
        ids["undecoded"] = push(store, "BSE", T + 1200 * S, b"\x04")
    return ids


def test_history_is_one_point_per_transmission_unless_copies_are_asked_for(tmp_path):
    settings = make_settings(tmp_path)
    ids = seed_copies(settings.state_db)
    with TestClient(create_app(settings)) as client:
        body = client.get("/telemetry/history", params={"field": "batt"}).json()
        assert body["copies"] is False and body["count"] == 2
        assert [(p["frame_id"], p["primary"]) for p in body["points"]] == [(ids["first"], True), (ids["next"], True)]
        every = client.get("/telemetry/history", params={"field": "battery", "copies": "true"}).json()  # alias too
        assert every["copies"] is True and every["stored_field"] == "batt"
        assert [(p["frame_id"], p["primary"]) for p in every["points"]] == [
            (ids["first"], True), (ids["copy"], False), (ids["next"], True)]
        assert client.get("/telemetry/history", params={"field": "batt", "copies": "maybe"}).status_code == 422
        latest = client.get("/telemetry/latest").json()
        assert latest["fields"]["batt"]["frame_id"] == ids["next"]


def test_frames_expose_and_filter_on_primary(tmp_path):
    settings = make_settings(tmp_path)
    ids = seed_copies(settings.state_db)
    with TestClient(create_app(settings)) as client:
        listed = {f["id"]: f for f in client.get("/frames").json()["frames"]}
        assert listed[ids["copy"]]["family"] == "push" and listed[ids["copy"]]["primary_frame_id"] == ids["first"]
        assert listed[ids["copy"]]["primary"] is False and listed[ids["first"]]["primary"] is True
        assert listed[ids["undecoded"]]["primary_frame_id"] is None and listed[ids["undecoded"]]["primary"] is False
        only = client.get("/frames", params={"primary": "true"}).json()["frames"]
        assert [f["id"] for f in only] == [ids["next"], ids["first"]]  # newest first
        rest = client.get("/frames", params={"primary": "false"}).json()["frames"]
        assert [f["id"] for f in rest] == [ids["undecoded"], ids["copy"]]
        assert client.get("/frames", params={"primary": "sometimes"}).status_code == 422
        one = client.get(f"/frames/{ids['copy']}").json()
        assert one["family"] == "push" and one["primary_frame_id"] == ids["first"] and one["primary"] is False


# -- Grafana ------------------------------------------------------------------------------------------------
DASHBOARD = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "pvdx-telemetry.json"


def test_dashboard_filters_on_the_primary_variable():
    """Primaries by default, every reception with All, so a per-station view can show all that station heard."""
    dash = json.loads(DASHBOARD.read_text())
    variable = next(v for v in dash["templating"]["list"] if v["name"] == "primary")
    assert variable["current"]["value"] == "true" and variable["includeAll"] and variable["allValue"] == ".*"
    queries = {p["title"]: p["targets"][0]["query"] for p in dash["panels"]}
    filtered = {title for title, query in queries.items() if "r.primary =~ /^(${primary:regex})$/" in query}
    assert filtered == {"Frames in range", "Frames per hour", "Battery / bus voltages", "Temperatures",
                        "Any field ($field)", "Latest values (all fields)"}
    assert not any("r.primary ==" in query for query in queries.values())  # nothing hard-codes primaries only
    assert "primary" not in queries["Receptions in range"] + queries["Frames by station"]
