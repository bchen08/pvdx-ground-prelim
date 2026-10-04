"""The HTTP API against a seeded temporary state database (FastAPI TestClient, no server)."""

from __future__ import annotations

import asyncio
import base64
import collections
import datetime as dt
import json
import logging
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from pvdx_ground.api import create_app
from pvdx_ground.config import Settings
from pvdx_ground.ingest.state import StateStore
from pvdx_ground.timeutil import to_iso_z, utcnow
from tests.conftest import NORAD, UTC, load_frames, load_page

T0 = dt.datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
TOKEN = "gs-secret"
BIG = 2**63  # one past the largest SQLite integer


def make_settings(tmp_path, **extra) -> Settings:
    env = {
        "NORAD_CAT_ID": str(NORAD),
        "STATE_DB": str(tmp_path / "state.db"),
        "TELEMETRY_ALIASES": "battery=batt,signal_rssi=rssi",
        "TELEMETRY_STALE_AFTER": "3600",
        "INGEST_TOKEN": TOKEN,
        "INFLUX_TOKEN": "",
        "REDIS_URL": "",
    }
    env.update(extra)
    return Settings.from_env(env, require_norad=False)


def seed(path) -> dict:
    """Recorded observations of page 1, their two frames decoded, one recent pushed frame decoded."""
    with StateStore(path) as store:
        page, _ = load_page(1)
        for obs in page:
            store.upsert_observation(obs, seen_at=T0)
        frames = load_frames()
        for ref in store.pending_frames():
            store.store_frame(ref.id, frames[ref.url], downloaded_at=T0)
        satnogs_ids = []  # in frame-time order (the recorded ids happen to be in the opposite order)
        for i, f in enumerate(sorted(store.frames_to_decode(), key=lambda f: f.timestamp)):
            store.mark_decoded(f.id, "ok", decoded_at=T0, fields={"batt": 8000 + i, "mode": "SAFE"}, decoder="test")
            satnogs_ids.append(f.id)
        now = utcnow()
        pushed, _ = store.add_pushed_frame(norad_cat_id=NORAD, station_name="BSE", raw=b"\x09\x08", frame_time=now,
                                           received_at=now, meta={"frequency": 145935000})
        store.mark_decoded(pushed, "ok", decoded_at=now, fields={"batt": 8100, "temp": 21.5}, decoder="test")
        obs_id = next(o["id"] for o in page if o["demoddata"])
        return {"satnogs_ids": satnogs_ids, "pushed": pushed, "obs_id": obs_id, "now": now}


@pytest.fixture
def seeded(tmp_path):
    settings = make_settings(tmp_path)
    info = seed(settings.state_db)
    wake = threading.Event()
    app = create_app(settings, wake=wake)
    with TestClient(app) as client:
        yield client, info, wake


def test_root_and_health(seeded):
    client, info, _ = seeded
    root = client.get("/").json()
    assert root["service"] == "pvdx-ground" and root["norad_cat_id"] == NORAD and "GET /health" in root["endpoints"]
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["problems"] == [] and health["schema_version"] == 3
    assert health["influxdb"] == "not configured" and health["redis"] == "not configured"
    assert health["satellites"][0]["norad_cat_id"] == NORAD and health["satellites"][0]["frames_from_groundstation"] == 1
    assert health["ingest"][str(NORAD)] == {"watermark": None, "last_sweep": None}
    assert health["workers"] == {}


def test_health_reports_dead_workers(tmp_path):
    settings = make_settings(tmp_path)
    dead = threading.Thread(target=lambda: None, name="decode")
    dead.start()
    dead.join()
    with TestClient(create_app(settings, workers={"decode": dead})) as client:
        health = client.get("/health").json()
    assert health["workers"] == {"decode": "stopped"} and health["status"] == "degraded" and health["problems"] == ["decode"]


def test_telemetry_compat_shape_with_aliases(seeded):
    client, info, _ = seeded
    body = client.get("/telemetry").json()
    assert body["telemetry"]["batt"] == 8100 and body["telemetry"]["battery"] == 8100  # alias of batt
    assert body["telemetry"]["mode"] == "SAFE" and body["telemetry"]["temp"] == 21.5
    assert body["telemetry"]["signal_rssi"] is None  # aliased field never decoded: None, like the web app's stale rule
    assert body["stale"] is False and body["norad_cat_id"] == NORAD and body["source"] == "groundstation"
    assert body["frame_time"] == to_iso_z(info["now"]) and body["units"] == {}  # plain aliases name no unit


def test_converting_aliases_in_telemetry_and_history_raw_views_unchanged(tmp_path, caplog):
    settings = make_settings(tmp_path, TELEMETRY_ALIASES=(
        "battery=batt*0.001:V,temperature=temp*0.01:degC,signal_rssi=rssi*0.5-134:dBm,state=mode*2,label=mode"
    ))
    info = seed(settings.state_db)
    with TestClient(create_app(settings)) as client, caplog.at_level(logging.WARNING, logger="pvdx_ground.api.app"):
        body = client.get("/telemetry").json()
        telemetry = body["telemetry"]
        assert telemetry["battery"] == 8.1 and telemetry["batt"] == 8100  # the raw field stays raw beside its alias
        assert telemetry["temperature"] == 0.215 and telemetry["temp"] == 21.5 and telemetry["label"] == "SAFE"
        assert telemetry["signal_rssi"] is None  # no value yet
        assert telemetry["state"] is None  # a converting alias of a string field: null, never the raw string
        assert body["units"] == {"battery": "V", "temperature": "degC", "signal_rssi": "dBm"}
        assert body["stale"] is False and body["frame_time"] == to_iso_z(info["now"]) and body["source"] == "groundstation"
        client.get("/telemetry")
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
            "alias state: field mode holds 'SAFE', not a number; serving it as null"]  # once, not per request

        alias = client.get("/telemetry/history", params={"field": "battery"}).json()
        assert alias["stored_field"] == "batt" and alias["unit"] == "V" and alias["count"] == 3
        assert [p["value"] for p in alias["points"]] == [8.0, 8.001, 8.1]
        raw = client.get("/telemetry/history", params={"field": "batt"}).json()
        assert raw["unit"] is None and [p["value"] for p in raw["points"]] == [8000, 8001, 8100]
        assert [p["frame_id"] for p in alias["points"]] == [p["frame_id"] for p in raw["points"]]
        state = client.get("/telemetry/history", params={"field": "state"}).json()
        assert state["count"] == 2 and [p["value"] for p in state["points"]] == [None, None]
        label = client.get("/telemetry/history", params={"field": "label"}).json()
        assert label["unit"] is None and [p["value"] for p in label["points"]] == ["SAFE", "SAFE"]

        latest = client.get("/telemetry/latest").json()  # provenance views keep the stored values
        assert latest["fields"]["batt"]["value"] == 8100 and latest["aliases"]["battery"] == "batt"
        fields = {f["field"] for f in client.get("/telemetry/fields").json()["fields"]}
        assert fields == {"batt", "mode", "temp"}


def test_telemetry_latest_fields_and_staleness(seeded, tmp_path):
    client, info, _ = seeded
    latest = client.get("/telemetry/latest").json()
    assert latest["stale"] is False and 0 <= latest["age_seconds"] < 120
    assert latest["fields"]["batt"] == {"value": 8100, "frame_time": to_iso_z(info["now"]), "frame_id": info["pushed"],
                                       "source": "groundstation", "station_name": "BSE"}
    assert latest["fields"]["mode"]["source"] == "satnogs" and latest["aliases"] == {"battery": "batt", "signal_rssi": "rssi"}
    fields = client.get("/telemetry/fields").json()["fields"]
    assert {f["field"]: f["type"] for f in fields} == {"batt": "number", "mode": "string", "temp": "number"}
    # a tiny staleness window flips the flag
    strict = make_settings(tmp_path, TELEMETRY_STALE_AFTER="0")
    with TestClient(create_app(strict)) as strict_client:
        assert strict_client.get("/telemetry").json()["stale"] is True
    # an unknown satellite has nothing and is stale
    empty = client.get("/telemetry/latest", params={"norad": 1}).json()
    assert empty["stale"] is True and empty["fields"] == {} and empty["newest_frame_time"] is None


def test_history(seeded):
    client, info, _ = seeded
    body = client.get("/telemetry/history", params={"field": "batt"}).json()
    assert body["field"] == body["stored_field"] == "batt" and body["count"] == 3
    assert [p["frame_id"] for p in body["points"]] == info["satnogs_ids"] + [info["pushed"]]
    assert [p["value"] for p in body["points"]] == [8000, 8001, 8100]
    alias = client.get("/telemetry/history", params={"field": "battery"}).json()  # TELEMETRY_ALIASES battery=batt
    assert alias["field"] == "battery" and alias["stored_field"] == "batt" and alias["points"] == body["points"]
    recent = client.get("/telemetry/history", params={"field": "batt", "since": "-1h"}).json()
    assert [p["frame_id"] for p in recent["points"]] == [info["pushed"]]
    satnogs = client.get("/telemetry/history", params={"field": "batt", "source": "satnogs", "until": "2026-09-26T23:59:59Z"}).json()
    assert [p["frame_id"] for p in satnogs["points"]] == info["satnogs_ids"]
    assert client.get("/telemetry/history", params={"field": "nope"}).json()["points"] == []
    assert client.get("/telemetry/history", params={"field": 'a"b'}).status_code == 400
    assert client.get("/telemetry/history", params={"field": "batt", "since": "yesterday"}).status_code == 400
    assert client.get("/telemetry/history", params={"field": "batt", "source": "mars"}).status_code == 422
    assert client.get("/telemetry/history").status_code == 422  # field is required


@pytest.mark.parametrize("value", ["-99999999999d", "-" + "9" * 400 + "d", "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_out_of_range_time_parameters_are_400(seeded, value):
    client, _, _ = seeded  # before year 1 or after year 9999 (OverflowError, formerly a 500)
    for path, params in (("/frames", {"since": value}), ("/observations", {"until": value}),
                         ("/telemetry/history", {"field": "batt", "since": value})):
        response = client.get(path, params=params)
        assert response.status_code == 400 and "ISO-8601" in response.json()["detail"], (path, response.text)


@pytest.mark.parametrize("path, params", [
    ("/frames", {"offset": BIG}), (f"/frames/{BIG}", {}), (f"/observations/{BIG}", {}), ("/observations", {"ground_station": BIG}),
    ("/telemetry", {"norad": BIG}), ("/telemetry/latest", {"norad": BIG}), ("/telemetry/fields", {"norad": BIG}),
    ("/telemetry/history", {"field": "batt", "norad": BIG}), ("/frames", {"norad": BIG}), ("/observations", {"norad": BIG}),
    ("/telemetry", {"norad": 0}), ("/frames", {"norad": 1_000_000_000}),
])
def test_out_of_range_integers_are_422(seeded, path, params):
    client, _, _ = seeded  # 2**63 and up overflowed sqlite3 (formerly a 500); norad must be 1..999999999
    assert client.get(path, params=params).status_code == 422


def test_largest_integers_still_accepted(seeded):
    client, _, _ = seeded
    assert client.get("/frames", params={"offset": BIG - 1}).json()["count"] == 0
    assert client.get(f"/frames/{BIG - 1}").status_code == 404 and client.get(f"/observations/{BIG - 1}").status_code == 404
    assert client.get("/telemetry/latest", params={"norad": 999_999_999}).json()["fields"] == {}


def test_frames_and_observations(seeded):
    client, info, _ = seeded
    frames = client.get("/frames").json()
    assert frames["count"] == 3 and frames["frames"][0]["id"] == info["pushed"] and "raw" not in frames["frames"][0]
    assert [f["id"] for f in client.get("/frames", params={"source": "satnogs"}).json()["frames"]] == info["satnogs_ids"][::-1]
    assert client.get("/frames", params={"station": "BSE"}).json()["count"] == 1
    assert client.get("/frames", params={"decode_status": "pending"}).json()["count"] == 0
    assert client.get("/frames", params={"limit": 0}).status_code == 422
    one = client.get(f"/frames/{info['pushed']}").json()
    assert base64.b64decode(one["raw_base64"]) == b"\x09\x08" and one["raw_hex"] == "0908"
    assert one["decoded"] == {"batt": 8100, "temp": 21.5} and one["meta"] == {"frequency": 145935000}
    assert one["observation"] is None and one["source"] == "groundstation"
    sat = client.get(f"/frames/{info['satnogs_ids'][0]}").json()
    assert sat["observation"]["id"] == info["obs_id"] and sat["url"].startswith("https://")
    assert client.get("/frames/99999").status_code == 404

    observations = client.get("/observations", params={"norad": NORAD}).json()
    assert observations["count"] == 25 and observations["observations"][0]["start_time"] >= observations["observations"][-1]["start_time"]
    with_frames = next(o for o in observations["observations"] if o["id"] == info["obs_id"])
    assert with_frames["frames_total"] == with_frames["frames_decoded"] == 2
    assert client.get("/observations", params={"since": "2026-09-26T15:25:44Z"}).json()["count"] < 25
    one = client.get(f"/observations/{info['obs_id']}").json()
    assert one["record"]["id"] == info["obs_id"] and one["tle1"].startswith("1 ")
    assert client.get("/observations/1").status_code == 404


def test_push_endpoint_stores_dedupes_and_wakes_the_decoder(seeded):
    client, info, wake = seeded
    when = "2026-09-27T14:03:05Z"
    body = {"norad_cat_id": NORAD, "station": "BSE Providence",
            "frames": [{"raw": base64.b64encode(b"\xaa\xbb\xcc").decode(), "received_at": when, "frequency": 436500000, "rssi": -97.5},
                       {"raw": base64.b64encode(b"\xdd").decode().rstrip("="), "received_at": "2026-09-27T14:03:06", "meta": {"pass": 12}}]}
    assert client.post("/ingest/frames", json=body).status_code == 401
    assert client.post("/ingest/frames", json=body, headers={"X-Ingest-Token": "wrong"}).status_code == 401
    response = client.post("/ingest/frames", json=body, headers={"X-Ingest-Token": TOKEN})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"] == 2 and result["duplicates"] == 0 and all(f["new"] for f in result["frames"])
    assert wake.is_set()
    first = client.get(f"/frames/{result['frames'][0]['id']}").json()
    assert first["raw_hex"] == "aabbcc" and first["frame_time"] == when and first["station_name"] == "BSE Providence"
    assert first["meta"] == {"frequency": 436500000, "rssi": -97.5} and first["decode_status"] == "pending"
    second = client.get(f"/frames/{result['frames'][1]['id']}").json()
    assert second["frame_time"] == "2026-09-27T14:03:06Z" and second["meta"] == {"pass": 12}  # naive time = UTC
    # resending is harmless
    wake.clear()
    again = client.post("/ingest/frames", json=body, headers={"X-Ingest-Token": TOKEN}).json()
    assert again["accepted"] == 0 and again["duplicates"] == 2 and not wake.is_set()
    assert [f["id"] for f in again["frames"]] == [f["id"] for f in result["frames"]]
    assert client.get("/frames", params={"decode_status": "pending"}).json()["count"] == 2


def test_push_endpoint_validation(seeded):
    client, _, _ = seeded
    headers = {"X-Ingest-Token": TOKEN}
    ok = {"raw": base64.b64encode(b"\x01").decode(), "received_at": "2026-09-27T14:03:05Z"}
    assert client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "BSE", "frames": []}, headers=headers).status_code == 422
    assert client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "", "frames": [ok]}, headers=headers).status_code == 422
    bad = client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "BSE", "frames": [dict(ok, raw="!!!")]}, headers=headers)
    assert bad.status_code == 400 and "base64" in bad.json()["detail"]
    empty = client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "BSE", "frames": [dict(ok, raw="")]}, headers=headers)
    assert empty.status_code == 400
    huge = base64.b64encode(b"\x00" * (64 * 1024 + 1)).decode()
    assert client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "BSE", "frames": [dict(ok, raw=huge)]}, headers=headers).status_code == 413
    assert client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "BSE", "frames": [dict(ok, received_at="soon")]}, headers=headers).status_code == 422
    for edge in ("9999-12-31T23:59:59-01:00", "0001-01-01T00:00:00+01:00"):  # OverflowError in UTC (formerly a 500)
        assert client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "BSE", "frames": [dict(ok, received_at=edge)]}, headers=headers).status_code == 400
    for norad in (BIG, 1_000_000_000, 0, -5):  # 2**63 overflowed sqlite3 (formerly a 500)
        assert client.post("/ingest/frames", json={"norad_cat_id": norad, "station": "BSE", "frames": [ok]}, headers=headers).status_code == 422
    assert client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "   ", "frames": [ok]}, headers=headers).status_code == 422
    padded = client.post("/ingest/frames", json={"norad_cat_id": NORAD, "station": "  BSE Providence ", "frames": [ok]}, headers=headers)
    assert client.get(f"/frames/{padded.json()['frames'][0]['id']}").json()["station_name"] == "BSE Providence"


def test_push_without_token_configured_is_open(tmp_path, caplog):
    settings = make_settings(tmp_path, INGEST_TOKEN="")
    with TestClient(create_app(settings)) as client:
        body = {"norad_cat_id": NORAD, "station": "BSE", "frames": [{"raw": base64.b64encode(b"\x01").decode(), "received_at": "2026-09-27T14:03:05Z"}]}
        assert client.post("/ingest/frames", json=body).json()["accepted"] == 1
    assert any("accepts frames from anyone" in r.message for r in caplog.records)


def test_norad_resolution_without_configuration(tmp_path):
    settings = make_settings(tmp_path, NORAD_CAT_ID="")
    with StateStore(settings.state_db) as store:
        store.add_pushed_frame(norad_cat_id=1, station_name="A", raw=b"\x01", frame_time=T0, received_at=T0)
    with TestClient(create_app(settings)) as client:
        assert client.get("/telemetry/latest").json()["norad_cat_id"] == 1  # the only satellite in the database
        with StateStore(settings.state_db) as store:
            store.add_pushed_frame(norad_cat_id=2, station_name="A", raw=b"\x01", frame_time=T0, received_at=T0)
        assert client.get("/telemetry/latest").status_code == 400
        assert client.get("/telemetry/latest", params={"norad": 2}).json()["norad_cat_id"] == 2


def push_body(raw: bytes) -> dict:
    return {"norad_cat_id": NORAD, "station": "BSE", "frames": [{"raw": base64.b64encode(raw).decode(), "received_at": "2026-09-27T14:03:05Z"}]}


async def raw_push(app, headers: list[tuple[bytes, bytes]], receive) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """``POST /ingest/frames`` straight through ASGI with the caller's ``receive``; returns (status, headers, body)."""
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/ingest/frames", "raw_path": b"/ingest/frames", "query_string": b"", "root_path": "",
             "client": ("test", 1), "server": ("test", 80),
             "headers": [(b"host", b"test"), (b"origin", b"http://localhost:3000"), (b"content-type", b"application/json"), *headers]}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], start["headers"], b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_push_without_valid_token_is_rejected_before_the_body_is_read(tmp_path):
    app = create_app(make_settings(tmp_path))

    async def must_not_read() -> dict:
        raise AssertionError("the request body was read before the token check")

    for headers in ([], [(b"x-ingest-token", b"wrong")], [(b"x-ingest-token", b"")], [(b"x-ingest-token", b"gs-secr\xe9t")]):
        status, response_headers, body = asyncio.run(raw_push(app, headers, must_not_read))
        assert status == 401 and json.loads(body) == {"detail": "missing or invalid X-Ingest-Token"}, (headers, status, body)
        assert (b"content-type", b"application/json") in response_headers
        assert (b"access-control-allow-origin", b"http://localhost:3000") in response_headers  # still inside CORS

    messages = [{"type": "http.request", "body": json.dumps(push_body(b"\x42")).encode(), "more_body": False}]

    async def receive() -> dict:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    status, _, body = asyncio.run(raw_push(app, [(b"x-ingest-token", TOKEN.encode())], receive))
    assert status == 200 and json.loads(body)["accepted"] == 1


def test_overlapping_requests_all_succeed(tmp_path):
    # Regression: FastAPI opens, uses and closes each request's SQLite connection on different threadpool
    # threads; with SQLite's same-thread check on, most overlapping requests failed with a 500.
    settings = make_settings(tmp_path)
    seed(settings.state_db)
    app = create_app(settings)
    paths = ["/telemetry", "/health", "/frames", "/telemetry/latest"] * 10

    async def burst() -> list:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            gets = [client.get(path) for path in paths]
            posts = [client.post("/ingest/frames", json=push_body(bytes([i])), headers={"X-Ingest-Token": TOKEN}) for i in range(5)]
            return await asyncio.gather(*gets, *posts, return_exceptions=True)

    results = asyncio.run(burst())
    outcomes = collections.Counter(r.status_code if isinstance(r, httpx.Response) else type(r).__name__ for r in results)
    assert outcomes == {200: len(paths) + 5}, outcomes
    assert [r.json()["accepted"] for r in results[len(paths):]] == [1] * 5
