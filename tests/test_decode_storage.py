"""Decoder interface, field normalisation and InfluxDB point construction (no server needed)."""

from __future__ import annotations

import datetime as dt
import enum

import pytest

from pvdx_ground.decode import DecodedFrame, DecodeError, get_decoder, normalise_fields, register
from pvdx_ground.ingest.state import StoredFrame
from pvdx_ground.storage.influx import InfluxWriter

UTC = dt.timezone.utc


class Mode(enum.Enum):
    SAFE = 1


def test_normalise_fields_coerces_to_scalars():
    out = normalise_fields({"a": 1, "b": 2.5, "c": "x", "d": True, "e": Mode.SAFE, "f": b"\x01\xff", "g": None, "h": [1, 2], "n": float("nan")})
    assert out == {"a": 1, "b": 2.5, "c": "x", "d": 1, "e": "SAFE", "f": "01ff"}


def test_normalise_fields_drops_infinities():
    out = normalise_fields({"a": 1.5, "pinf": float("inf"), "ninf": float("-inf"), "big": 2**64, "max": 1.7976931348623157e308})
    assert out == {"a": 1.5, "big": 2**64, "max": 1.7976931348623157e308}


def test_get_decoder_specs():
    assert get_decoder("satnogs:geoscan").name == "satnogs:geoscan"
    assert get_decoder("satnogs:GeoScan").name == "satnogs:geoscan"
    assert get_decoder("pvdx").name == "pvdx"
    with pytest.raises(DecodeError):
        get_decoder("satnogs:no-such-satellite")
    with pytest.raises(DecodeError):
        get_decoder(None)
    with pytest.raises(DecodeError):
        get_decoder("bogus")

    class Custom:
        name = "custom"

        def decode(self, raw: bytes):
            return {"n": len(raw)}

    register("custom", Custom)
    assert get_decoder("custom").decode(b"abc") == {"n": 3}


def test_pvdx_stub_refuses_frames_clearly():
    with pytest.raises(DecodeError, match="PVDX decoder not implemented"):
        get_decoder("pvdx").decode(b"\x00" * 32)


def test_kaitai_decoder_rejects_empty_frame():
    with pytest.raises(DecodeError):
        get_decoder("satnogs:geoscan").decode(b"")


def make_frame(**kw) -> StoredFrame:
    base = dict(
        id=7, observation_id=15065374, url="https://s3/x", raw=b"\x00", sha256="00", frame_time=dt.datetime(2026, 9, 26, 5, 51, 26, tzinfo=UTC),
        norad_cat_id=39444, sat_id="OTOF-8025-8953-4756-4779", ground_station=2107, station_name="Foo Bar", observation_start=dt.datetime(2026, 9, 26, 5, 48, 28, tzinfo=UTC),
        observation_end=dt.datetime(2026, 9, 26, 6, 0, 0, tzinfo=UTC), transmitter_uuid="abc", tle0="FUNCUBE-1", tle1="1 ...", tle2="2 ...",
    )
    base.update(kw)
    return StoredFrame(**base)


def test_influx_point_tags_fields_and_time():
    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")
    try:
        decoded = DecodedFrame(frame=make_frame(), decoder="satnogs:geoscan", fields={"batt_v": 7, "temp": 21.5, "mode": "SAFE", "flag": True})
        line = writer.point(decoded).to_line_protocol()
    finally:
        writer.close()
    assert line.startswith("telemetry,")
    for tag in ("norad_cat_id=39444", "observation_id=15065374", "ground_station=2107", "decoder=satnogs:geoscan", "station_name=Foo\\ Bar"):
        assert tag in line
    assert "batt_v=7" in line and "temp=21.5" in line and 'mode="SAFE"' in line and "flag=1" in line
    assert line.endswith(str(int(dt.datetime(2026, 9, 26, 5, 51, 26, tzinfo=UTC).timestamp()) * 10**9 + 7))  # ns + frame id
    assert "frame_id=7" in line


def test_influx_point_falls_back_to_observation_start_without_frame_time():
    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")
    try:
        line = writer.point(DecodedFrame(frame=make_frame(frame_time=None), decoder="x", fields={"a": 1})).to_line_protocol()
    finally:
        writer.close()
    assert line.endswith(str(int(dt.datetime(2026, 9, 26, 5, 48, 28, tzinfo=UTC).timestamp()) * 10**9 + 7))


def test_two_frames_in_the_same_second_are_distinct_points():
    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")
    try:
        a = writer.point(DecodedFrame(frame=make_frame(id=1), decoder="x", fields={"a": 1})).to_line_protocol()
        b = writer.point(DecodedFrame(frame=make_frame(id=2), decoder="x", fields={"a": 2})).to_line_protocol()
    finally:
        writer.close()
    assert a.rsplit(" ", 1)[1] != b.rsplit(" ", 1)[1]


def test_write_frames_falls_back_per_point_on_4xx_and_reports_rejections():
    from influxdb_client.rest import ApiException

    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")
    frames = [DecodedFrame(frame=make_frame(id=i), decoder="x", fields={"a": i}) for i in (1, 2, 3)]
    calls = []

    def fake_write(*, bucket, org, record):
        calls.append(record)
        if isinstance(record, list) or record.to_line_protocol().startswith("telemetry") and "frame_id=2" in record.to_line_protocol():
            raise ApiException(status=422, reason="Unprocessable Entity")
    writer._write_api.write = fake_write
    try:
        rejected = writer.write_frames(frames)
    finally:
        writer.close()
    assert [f.frame.id for f, _ in rejected] == [2] and "422" in rejected[0][1]
    assert len(calls) == 4  # one batch attempt + three single points


def test_write_frames_reports_a_frame_whose_point_cannot_be_built():
    from influxdb_client.rest import ApiException

    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")
    timeless = DecodedFrame(frame=make_frame(id=2, frame_time=None, observation_start=None), decoder="x", fields={"a": 2})
    frames = [DecodedFrame(frame=make_frame(id=1), decoder="x", fields={"a": 1}), timeless,
              DecodedFrame(frame=make_frame(id=3), decoder="x", fields={"a": 3})]
    calls = []

    def fake_write(*, bucket, org, record):
        calls.append(record)
    writer._write_api.write = fake_write
    try:
        rejected = writer.write_frames(frames)
        assert rejected == [(timeless, "no InfluxDB point: ValueError: frame 2 has neither a frame time nor an observation")]
        assert len(calls) == 1 and ["frame_id=1" in p.to_line_protocol() for p in calls[0]] == [True, False]
        calls.clear()
        assert writer.write_frames([timeless]) == rejected and calls == []  # nothing left to send

        def down(*, bucket, org, record):
            raise ApiException(status=503, reason="down")
        writer._write_api.write = down
        with pytest.raises(ApiException):  # the rest of the batch is still retried as a whole later
            writer.write_frames(frames)
    finally:
        writer.close()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_write_frames_propagates_auth_and_bucket_errors(status):
    from influxdb_client.rest import ApiException

    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")

    def fake_write(*, bucket, org, record):
        raise ApiException(status=status, reason="nope")
    writer._write_api.write = fake_write
    try:
        with pytest.raises(ApiException):
            writer.write_frames([DecodedFrame(frame=make_frame(), decoder="x", fields={"a": 1})])
    finally:
        writer.close()


def test_check_access_reports_bad_token_and_missing_bucket():
    from influxdb_client.rest import ApiException

    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")

    class FakeBuckets:
        def __init__(self, result): self.result = result
        def find_bucket_by_name(self, name):
            if isinstance(self.result, Exception): raise self.result
            return self.result
    try:
        writer._client.buckets_api = lambda: FakeBuckets(ApiException(status=401, reason="Unauthorized"))
        assert "401" in writer.check_access()
        writer._client.buckets_api = lambda: FakeBuckets(None)
        assert "does not exist" in writer.check_access()
        writer._client.buckets_api = lambda: FakeBuckets(object())
        assert writer.check_access() is None
    finally:
        writer.close()


def test_write_frames_propagates_5xx():
    from influxdb_client.rest import ApiException

    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")

    def fake_write(*, bucket, org, record):
        raise ApiException(status=503, reason="down")
    writer._write_api.write = fake_write
    try:
        with pytest.raises(ApiException):
            writer.write_frames([DecodedFrame(frame=make_frame(), decoder="x", fields={"a": 1})])
    finally:
        writer.close()


# --- real CroCube frames recorded from SatNOGS (tests/fixtures/frames/crocube_*.bin) -----------------
import json
from pathlib import Path

CROCUBE = json.loads((Path(__file__).parent / "fixtures" / "frames" / "crocube_manifest.json").read_text())


def crocube_frame(name: str) -> bytes:
    return (Path(__file__).parent / "fixtures" / "frames" / f"{name}.bin").read_bytes()


@pytest.mark.parametrize("name", ["crocube_psu_beacon", "crocube_obc_beacon", "crocube_uhf_beacon"])
def test_crocube_beacons_decode_to_documented_fields(name):
    fields = get_decoder("satnogs:crocube").decode(crocube_frame(name))
    assert fields == CROCUBE[name]["fields"]
    assert fields["src_callsign"].strip() == "9A0CC" and fields["dest_callsign"].strip() == "CQ"
    assert all(isinstance(v, (int, float, str)) for v in fields.values())


def test_crocube_psu_beacon_carries_battery_millivolts():
    fields = get_decoder("satnogs:crocube").decode(crocube_frame("crocube_psu_beacon"))
    assert 6000 < fields["psu_battery"] < 9000  # 2S Li-ion pack, mV
    assert fields["psu_uptime"] > 0


def test_crocube_image_chunk_is_rejected_like_satnogs_db_does():
    with pytest.raises(DecodeError):
        get_decoder("satnogs:crocube").decode(crocube_frame("crocube_image_chunk"))
