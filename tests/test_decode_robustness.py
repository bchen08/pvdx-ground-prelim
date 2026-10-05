"""Decode loop robustness: a decoder bug or an unbuildable point parks that one frame, the rest go through."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import threading
import time

import pytest

from pvdx_ground.decode import get_decoder, register
from pvdx_ground.decode.__main__ import decode_once, run_decode
from pvdx_ground.ingest.state import StateStore
from pvdx_ground.publish.redis import RedisPublisher
from pvdx_ground.storage.influx import InfluxWriter
from tests.test_publish import FakeRedis
from tests.test_serve_flow import CROCUBE, RecordingWriter, push, settings_for

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 27, 10, 0, 0, tzinfo=UTC)


class Fragile:
    """A decoder with a bug: the CroCube decoder, but it indexes past the end of a short frame."""

    name = "fragile"

    def __init__(self) -> None:
        self._inner = get_decoder("satnogs:crocube")

    def decode(self, raw: bytes):
        _ = raw[40]  # every CroCube beacon is longer; noise frames are not
        return self._inner.decode(raw)


class Unnormalised:
    """A decoder that returns raw values without calling ``normalise_fields``."""

    name = "unnormalised"

    def decode(self, raw: bytes):
        return {"volts": 7.9, "hot": float("inf"), "cold": float("-inf"), "nan": float("nan"), "blob": b"\x01\xff"}


register("fragile", Fragile)
register("unnormalised", Unnormalised)


def push_raw(path, raw: bytes, when: dt.datetime) -> int:
    with StateStore(path) as store:
        frame_id, _ = store.add_pushed_frame(norad_cat_id=CROCUBE, station_name="BSE", raw=raw, frame_time=when, received_at=when)
    return frame_id


def test_decoder_crash_marks_only_that_frame_error(tmp_path, caplog):
    settings = settings_for(tmp_path)
    good1 = push(settings.state_db, "crocube_psu_beacon", T0)
    bad1 = push_raw(settings.state_db, b"\x01\x02\x03", T0 + dt.timedelta(minutes=1))
    good2 = push(settings.state_db, "crocube_obc_beacon", T0 + dt.timedelta(minutes=2))
    bad2 = push_raw(settings.state_db, b"\x00junk", T0 + dt.timedelta(minutes=3))
    writer = RecordingWriter()
    caplog.set_level(logging.INFO)
    with StateStore(settings.state_db) as store:
        counts = decode_once(store, get_decoder("fragile"), writer, norad=CROCUBE, redo=False, limit=None, dry_run=False)
        assert counts["frames"] == 4 and counts["decoded"] == 2 and counts["written"] == 2 and counts["errors"] == 2
        assert [[d.frame.id for d in b] for b in writer.batches] == [[good1, good2]]  # frames before the crash too
        for fid in (bad1, bad2):
            frame = store.get_frame(fid)
            assert frame["decode_status"] == "error" and frame["decoder"] == "fragile"
            assert frame["decode_error"].startswith("fragile: unexpected IndexError: ")
        assert store.get_frame(good2)["decode_status"] == "ok" and store.latest_values(CROCUBE)["obc_bat"].value == 8158
        assert store.frames_to_decode() == []
        assert decode_once(store, get_decoder("fragile"), writer, norad=CROCUBE, redo=False, limit=None,
                           dry_run=False)["frames"] == 0  # the next pass does not read the crashed frames again
    crashes = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [bool(r.exc_info) for r in crashes] == [True, False]  # one traceback per pass, then one line each
    assert f"frame {bad1}: fragile: unexpected IndexError" in crashes[0].getMessage()


def test_run_decode_keeps_polling_past_a_decoder_crash(tmp_path):
    settings = settings_for(tmp_path)
    bad = push_raw(settings.state_db, b"\x01\x02\x03", T0)  # oldest, so read first
    good = push(settings.state_db, "crocube_psu_beacon", T0 + dt.timedelta(minutes=1))
    writer = RecordingWriter()
    stop, wake = threading.Event(), threading.Event()
    worker = threading.Thread(
        target=run_decode,
        kwargs=dict(settings=settings, decoder=get_decoder("fragile"), writer=writer, poll=600, stop=stop, wake=wake),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 10
    while len(writer.batches) < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [[d.frame.id for d in b] for b in writer.batches] == [[good]]
    later = push(settings.state_db, "crocube_obc_beacon", T0 + dt.timedelta(minutes=2))
    wake.set()
    while len(writer.batches) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [d.frame.id for d in writer.batches[1]] == [later]
    stop.set()
    wake.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    with StateStore(settings.state_db) as store:
        assert store.get_frame(bad)["decode_status"] == "error" and store.frames_to_decode() == []


def test_store_errors_while_parking_a_crashed_frame_still_propagate(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    push_raw(settings.state_db, b"\x01\x02\x03", T0)
    with StateStore(settings.state_db) as store:
        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(store, "mark_decoded", locked)
        with pytest.raises(sqlite3.OperationalError):
            decode_once(store, get_decoder("fragile"), RecordingWriter(), norad=CROCUBE, redo=False, limit=None,
                        dry_run=False)
        assert len(store.frames_to_decode()) == 1  # still pending, retried on the next pass


def test_dry_run_counts_a_decoder_crash_without_marking_it(tmp_path, capsys):
    settings = settings_for(tmp_path)
    push_raw(settings.state_db, b"\x01\x02\x03", T0)
    push(settings.state_db, "crocube_psu_beacon", T0 + dt.timedelta(minutes=1))
    with StateStore(settings.state_db) as store:
        counts = decode_once(store, get_decoder("fragile"), None, norad=CROCUBE, redo=False, limit=None, dry_run=True)
        assert counts["decoded"] == 1 and counts["errors"] == 1 and len(store.frames_to_decode()) == 2
    assert '"psu_battery": 8160' in capsys.readouterr().out


def test_frame_without_any_time_is_parked_and_the_rest_of_the_batch_written(tmp_path):
    settings = settings_for(tmp_path)
    first = push(settings.state_db, "crocube_psu_beacon", T0)
    timeless = push(settings.state_db, "crocube_obc_beacon", T0 + dt.timedelta(minutes=1))
    last = push(settings.state_db, "crocube_uhf_beacon", T0 + dt.timedelta(minutes=2))
    writer = InfluxWriter("http://localhost:8086", "t", "bse", "telemetry")
    sent = []
    writer._write_api.write = lambda *, bucket, org, record: sent.append(record)
    with StateStore(settings.state_db) as store:
        store._db.execute("UPDATE frames SET frame_time = NULL WHERE id = ?", (timeless,))  # no observation either
        try:
            counts = decode_once(store, get_decoder("satnogs:crocube"), writer, norad=CROCUBE, redo=False, limit=None,
                                 dry_run=False)
        finally:
            writer.close()
        assert counts["decoded"] == 3 and counts["written"] == 2 and counts["rejected"] == 1
        assert len(sent) == 1
        assert [int(re.search(r"frame_id=(\d+)", p.to_line_protocol()).group(1)) for p in sent[0]] == [first, last]
        frame = store.get_frame(timeless)
        assert frame["decode_status"] == "error"
        assert frame["decode_error"] == f"no InfluxDB point: ValueError: frame {timeless} has neither a frame time nor an observation"
        assert store.get_frame(last)["decode_status"] == "ok" and store.frames_to_decode() == []


def test_non_finite_and_raw_values_never_reach_sqlite_or_redis(tmp_path):
    settings = settings_for(tmp_path)
    fid = push_raw(settings.state_db, b"\x01\x02\x03", T0)
    fake = FakeRedis()
    publisher = RedisPublisher("redis://unused", client=fake)
    with StateStore(settings.state_db) as store:
        counts = decode_once(store, get_decoder("unnormalised"), RecordingWriter(), norad=CROCUBE, redo=False,
                             limit=None, dry_run=False, publisher=publisher)
        assert counts["written"] == 1
        assert store.get_frame(fid)["decoded"] == {"volts": 7.9, "blob": "01ff"}
        assert set(store.latest_values(CROCUBE)) == {"volts", "blob"}
        stored = store._db.execute("SELECT decoded_json FROM frames WHERE id = ?", (fid,)).fetchone()[0]
    for text in (stored, fake.data["telemetry:_all"][0]):
        assert "Infinity" not in text and "NaN" not in text
        json.loads(text, parse_constant=lambda token: pytest.fail(f"non-standard JSON token {token}"))
