"""Decode loop end to end: real CroCube frames pushed in, latest values in SQLite, aliases in Redis, wake-up."""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

from pvdx_ground.config import Settings
from pvdx_ground.decode import get_decoder
from pvdx_ground.decode.__main__ import decode_once, run_decode
from pvdx_ground.ingest.state import StateStore
from pvdx_ground.publish.redis import RedisPublisher
from tests.test_publish import FakeRedis

UTC = dt.timezone.utc
FRAMES = Path(__file__).parent / "fixtures" / "frames"
CROCUBE = 62394


class RecordingWriter:
    """Stands in for InfluxWriter: keeps every batch, rejects nothing."""

    def __init__(self) -> None:
        self.batches: list[list] = []
        self.closed = False

    def write_frames(self, frames):
        self.batches.append(list(frames))
        return []

    def close(self) -> None:
        self.closed = True


def settings_for(tmp_path, **extra) -> Settings:
    env = {"NORAD_CAT_ID": str(CROCUBE), "STATE_DB": str(tmp_path / "state.db"), "DECODER": "satnogs:crocube",
           "TELEMETRY_ALIASES": "battery=psu_battery,temperature=obc_temp_mcu", "INFLUX_TOKEN": "", "REDIS_URL": ""}
    env.update(extra)
    return Settings.from_env(env)


def push(path, name: str, when: dt.datetime) -> int:
    with StateStore(path) as store:
        frame_id, _ = store.add_pushed_frame(norad_cat_id=CROCUBE, station_name="BSE", raw=(FRAMES / f"{name}.bin").read_bytes(),
                                             frame_time=when, received_at=when)
    return frame_id


def test_decode_once_stores_fields_and_publishes_aliases(tmp_path):
    settings = settings_for(tmp_path)
    t0 = dt.datetime(2026, 9, 27, 10, 0, 0, tzinfo=UTC)
    push(settings.state_db, "crocube_psu_beacon", t0)
    push(settings.state_db, "crocube_obc_beacon", t0 + dt.timedelta(minutes=1))
    push(settings.state_db, "crocube_image_chunk", t0 + dt.timedelta(minutes=2))  # not telemetry
    fake = FakeRedis()
    publisher = RedisPublisher("redis://unused", client=fake, aliases=settings.telemetry_aliases, ttl=99)
    writer = RecordingWriter()
    with StateStore(settings.state_db) as store:
        counts = decode_once(store, get_decoder("satnogs:crocube"), writer, norad=CROCUBE, redo=False, limit=None,
                             dry_run=False, publisher=publisher)
        assert counts["decoded"] == 2 and counts["written"] == 2 and counts["errors"] == 1 and counts["published"] > 0
        assert len(writer.batches) == 1 and {d.frame.source for d in writer.batches[0]} == {"groundstation"}
        latest = store.latest_values(CROCUBE)
        assert latest["psu_battery"].value == 8160 and latest["obc_bat"].value == 8158
        assert latest["psu_battery"].source == "groundstation" and latest["obc_temp_mcu"].frame_time == t0 + dt.timedelta(minutes=1)
        assert store.stats(CROCUBE)["frames_decode_error"] == 1 and store.frames_to_decode() == []
    assert fake.data["telemetry:battery"][0] == "8160" and fake.data["telemetry:temperature"] == ("539", 99)
    assert fake.data["telemetry:psu_uptime"][0] == "20622381"
    # a Redis outage does not undo the decode; the next pass republishes everything
    fake.fail = True
    push(settings.state_db, "crocube_uhf_beacon", t0 + dt.timedelta(minutes=3))
    with StateStore(settings.state_db) as store:
        counts = decode_once(store, get_decoder("satnogs:crocube"), writer, norad=CROCUBE, redo=False, limit=None,
                             dry_run=False, publisher=publisher)
        assert counts["decoded"] == 1 and counts["published"] == 0 and store.frames_to_decode() == []
    fake.fail = False
    with StateStore(settings.state_db) as store:
        decode_once(store, get_decoder("satnogs:crocube"), writer, norad=CROCUBE, redo=True, limit=None, dry_run=False,
                    publisher=publisher)
    assert fake.data["telemetry:uhf_pa_temp"][0] == "431" and fake.data["telemetry:battery"][0] == "8160"


def test_run_decode_wakes_up_for_pushed_frames_and_stops(tmp_path):
    settings = settings_for(tmp_path)
    fake = FakeRedis()
    publisher = RedisPublisher("redis://unused", client=fake, aliases=settings.telemetry_aliases)
    writer = RecordingWriter()
    stop, wake = threading.Event(), threading.Event()
    t0 = dt.datetime(2026, 9, 27, 10, 0, 0, tzinfo=UTC)
    push(settings.state_db, "crocube_psu_beacon", t0)  # already there when the loop starts
    worker = threading.Thread(
        target=run_decode,
        kwargs=dict(settings=settings, decoder=get_decoder("satnogs:crocube"), writer=writer, publisher=publisher,
                    poll=600, stop=stop, wake=wake),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 10
    while len(writer.batches) < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(writer.batches) == 1
    push(settings.state_db, "crocube_obc_beacon", t0 + dt.timedelta(minutes=1))
    wake.set()  # what the API does after storing a push
    while len(writer.batches) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(writer.batches) == 2 and writer.batches[1][0].fields["obc_bat"] == 8158
    assert fake.data["telemetry:temperature"][0] == "539"
    stop.set()
    wake.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    with StateStore(settings.state_db) as store:
        assert store.frames_to_decode() == [] and len(store.latest_values(CROCUBE)) > 20


def test_run_decode_once_warms_redis_from_stored_latest_values(tmp_path):
    settings = settings_for(tmp_path)
    t0 = dt.datetime(2026, 9, 27, 10, 0, 0, tzinfo=UTC)
    fid = push(settings.state_db, "crocube_psu_beacon", t0)
    with StateStore(settings.state_db) as store:
        store.mark_decoded(fid, "ok", decoded_at=t0, fields={"psu_battery": 8000}, decoder="x")
    fake = FakeRedis()
    publisher = RedisPublisher("redis://unused", client=fake, aliases=settings.telemetry_aliases)
    assert run_decode(settings, decoder=get_decoder("satnogs:crocube"), writer=RecordingWriter(), publisher=publisher) == 0
    assert fake.data["telemetry:battery"][0] == "8000"  # nothing new to decode, cache warmed anyway
