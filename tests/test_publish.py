"""RedisPublisher against an in-memory stand-in for the redis client."""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from pvdx_ground.config import parse_aliases
from pvdx_ground.ingest.state import LatestValue
from pvdx_ground.publish.redis import PublishError, RedisPublisher, value_to_str

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


class FakePipeline:
    def __init__(self, fake: "FakeRedis") -> None:
        self.fake = fake
        self.ops: list[tuple[str, str, int | None]] = []

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.ops.append((key, value, ex))

    def execute(self) -> None:
        if self.fake.fail:
            raise ConnectionError("redis down")
        for key, value, ex in self.ops:
            self.fake.data[key] = (value, ex)


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, tuple[str, int | None]] = {}
        self.fail = False
        self.closed = False

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        return True

    def close(self) -> None:
        self.closed = True


def latest() -> dict[str, LatestValue]:
    return {
        "psu_battery": LatestValue("psu_battery", 8160, 5, T0, "satnogs", "Station A"),
        "obc_temp_mcu": LatestValue("obc_temp_mcu", 539.0, 7, T0 + dt.timedelta(minutes=1), "groundstation", "BSE"),
        "mode": LatestValue("mode", "SAFE", 5, T0, "satnogs", "Station A"),
    }


def test_publish_writes_fields_aliases_and_summaries():
    fake = FakeRedis()
    publisher = RedisPublisher(
        "redis://unused", ttl=120, client=fake,
        aliases={"battery": "psu_battery", "temperature": "obc_temp_mcu", "signal_rssi": "uhf_rssi"},
    )
    written = publisher.publish(62394, latest(), now=T0 + dt.timedelta(minutes=5))
    assert fake.data["telemetry:psu_battery"] == ("8160", 120)
    assert fake.data["telemetry:battery"] == ("8160", 120)
    assert fake.data["telemetry:temperature"] == ("539", 120)  # float 539.0 without the trailing .0
    assert fake.data["telemetry:mode"] == ("SAFE", 120)
    assert "telemetry:signal_rssi" not in fake.data  # aliased field has no value yet: key absent = stale
    meta = json.loads(fake.data["telemetry:_meta"][0])
    assert meta["norad_cat_id"] == 62394 and meta["frame_time"] == "2026-09-27T12:01:00Z" and meta["frame_id"] == 7
    assert meta["source"] == "groundstation" and meta["station_name"] == "BSE" and meta["fields"] == 3
    assert meta["published_at"] == "2026-09-27T12:05:00Z" and meta["ttl_seconds"] == 120
    assert meta["aliases"]["battery"] == "psu_battery" and meta["units"] == {}  # plain aliases: raw values, no units
    everything = json.loads(fake.data["telemetry:_all"][0])
    assert everything["psu_battery"] == {"value": 8160, "frame_time": "2026-09-27T12:00:00Z", "frame_id": 5,
                                         "source": "satnogs", "station_name": "Station A"}
    assert written == 3 + 2 + 2
    assert publisher.publish(62394, {}, now=T0) == 0
    publisher.close()
    assert fake.closed


def test_converting_aliases_publish_engineering_units_and_raw_fields_stay_raw():
    fake = FakeRedis()
    aliases = parse_aliases(
        "battery=psu_battery*0.001:V,temperature=obc_temp_mcu*0.01:degC,signal_rssi=uhf_act_rssi_raw*0.5-134:dBm,"
        "uptime_seconds=obc_uptime:s,state=mode"
    )
    publisher = RedisPublisher("redis://unused", ttl=120, client=fake, aliases=aliases)
    values = {
        **latest(),
        "psu_battery": LatestValue("psu_battery", 7933, 5, T0, "satnogs", "Station A"),
        "uhf_act_rssi_raw": LatestValue("uhf_act_rssi_raw", 83, 6, T0, "satnogs", "Station A"),
        "obc_uptime": LatestValue("obc_uptime", 21345256, 7, T0, "satnogs", "Station A"),
    }
    assert publisher.publish(62394, values, now=T0) == 5 + 5 + 2
    assert fake.data["telemetry:battery"] == ("7.933", 120) and fake.data["telemetry:psu_battery"] == ("7933", 120)
    assert fake.data["telemetry:temperature"] == ("5.39", 120) and fake.data["telemetry:obc_temp_mcu"] == ("539", 120)
    assert fake.data["telemetry:signal_rssi"] == ("-92.5", 120) and fake.data["telemetry:uhf_act_rssi_raw"][0] == "83"
    assert fake.data["telemetry:uptime_seconds"][0] == "21345256" and fake.data["telemetry:state"][0] == "SAFE"
    meta = json.loads(fake.data["telemetry:_meta"][0])
    assert meta["units"] == {"battery": "V", "temperature": "degC", "signal_rssi": "dBm", "uptime_seconds": "s"}
    assert meta["aliases"] == {"battery": "psu_battery", "temperature": "obc_temp_mcu",
                               "signal_rssi": "uhf_act_rssi_raw", "uptime_seconds": "obc_uptime", "state": "mode"}
    everything = json.loads(fake.data["telemetry:_all"][0])
    assert everything["psu_battery"]["value"] == 7933 and "battery" not in everything  # the summary stays raw


def test_alias_problems_are_logged_once_and_never_fatal(caplog):
    fake = FakeRedis()
    aliases = parse_aliases("battery=psu_battery*0.001:V,status=mode*2,label=mode,signal_rssi=uhf_rssi*0.5-134:dBm")
    publisher = RedisPublisher("redis://unused", client=fake, aliases=aliases)
    with caplog.at_level(logging.DEBUG, logger="pvdx_ground.publish.redis"):
        publisher.publish(62394, latest(), now=T0)  # the startup warm-up
        publisher.publish(62394, latest(), now=T0 + dt.timedelta(minutes=1))
    assert fake.data["telemetry:battery"][0] == "8.16"
    assert "telemetry:status" not in fake.data  # a converting alias of a string field: left out, never raw
    assert fake.data["telemetry:label"][0] == "SAFE"  # a plain alias keeps the value, as before
    assert "telemetry:signal_rssi" not in fake.data  # no value yet: key absent
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [  # once each: missing fields at the first publish, a non-number once per alias
        "alias status: field mode holds 'SAFE', not a number; the alias is not published",
        "alias label: field mode holds 'SAFE', not a number",
        "alias signal_rssi: field uhf_rssi has no value yet for NORAD 62394",
    ]
    assert any("uhf_rssi has no value yet" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_prefix_and_failures():
    fake = FakeRedis()
    publisher = RedisPublisher("redis://unused", prefix="sat:62394:", client=fake)
    publisher.publish(62394, latest(), now=T0)
    assert "sat:62394:psu_battery" in fake.data and fake.data["sat:62394:_meta"][1] == 3600
    assert publisher.check() is None
    fake.fail = True
    assert "not reachable" in publisher.check()
    with pytest.raises(PublishError, match="redis down"):
        publisher.publish(62394, latest(), now=T0)


def test_value_to_str():
    assert value_to_str(True) == "1" and value_to_str(False) == "0"
    assert value_to_str(2.0) == "2" and value_to_str(2.5) == "2.5" and value_to_str(7) == "7"
    assert value_to_str("SAFE") == "SAFE"
