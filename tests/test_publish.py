"""RedisPublisher against an in-memory stand-in for the redis client."""

from __future__ import annotations

import datetime as dt
import json

import pytest

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
    assert meta["aliases"]["battery"] == "psu_battery"
    everything = json.loads(fake.data["telemetry:_all"][0])
    assert everything["psu_battery"] == {"value": 8160, "frame_time": "2026-09-27T12:00:00Z", "frame_id": 5,
                                         "source": "satnogs", "station_name": "Station A"}
    assert written == 3 + 2 + 2
    assert publisher.publish(62394, {}, now=T0) == 0
    publisher.close()
    assert fake.closed


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
