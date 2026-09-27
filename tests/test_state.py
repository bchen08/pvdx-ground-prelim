"""StateStore: idempotent upserts, frame lifecycle, sweeps and watermarks (SQLite, temp file)."""

from __future__ import annotations

import datetime as dt
import hashlib

from pvdx_ground.ingest.state import frame_time_from_url
from tests.conftest import NORAD, UTC, load_page

T0 = dt.datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


def obs_with_frames() -> dict:
    page, _ = load_page(1)
    return next(o for o in page if o["demoddata"])


def test_frame_time_from_url_variants():
    base = "https://s3.eu-central-1.wasabisys.com/satnogs-network/data_obs/2026/9/26/5/15065374/"
    assert frame_time_from_url(base + "data_15065374_2026-09-26T05-51-26_g0") == dt.datetime(2026, 9, 26, 5, 51, 26, tzinfo=UTC)
    assert frame_time_from_url(base + "data_15065374_2026-09-26T05-51-26") == dt.datetime(2026, 9, 26, 5, 51, 26, tzinfo=UTC)
    assert frame_time_from_url(base + "data_obs_15065374_2026-09-26T05-51-26") == dt.datetime(2026, 9, 26, 5, 51, 26, tzinfo=UTC)
    assert frame_time_from_url(base + "something_else.bin") is None


def test_upsert_is_idempotent_and_queues_frames_once(store):
    obs = obs_with_frames()
    assert store.upsert_observation(obs, seen_at=T0) == (True, len(obs["demoddata"]))
    assert store.upsert_observation(obs, seen_at=T0 + dt.timedelta(hours=1)) == (False, 0)
    pending = store.pending_frames()
    assert [p.url for p in pending] == [e["payload_demod"] for e in obs["demoddata"]]
    stats = store.stats(NORAD)
    assert stats["observations"] == 1 and stats["frames_pending"] == len(obs["demoddata"])
    assert stats["observations_complete"] == 0


def test_late_frames_are_queued_and_completion_is_reset(store):
    obs = obs_with_frames()
    store.upsert_observation(obs, seen_at=T0)
    for ref in store.pending_frames():
        store.store_frame(ref.id, b"\x01\x02", downloaded_at=T0)
    assert store.stats(NORAD)["observations_complete"] == 1
    later = dict(obs, demoddata=obs["demoddata"] + [{"payload_demod": obs["demoddata"][0]["payload_demod"] + "_late"}])
    assert store.upsert_observation(later, seen_at=T0) == (False, 1)
    assert store.stats(NORAD)["observations_complete"] == 0
    assert [p.url for p in store.pending_frames()] == [obs["demoddata"][0]["payload_demod"] + "_late"]


def test_store_frame_records_bytes_hash_and_marks_complete(store):
    obs = obs_with_frames()
    store.upsert_observation(obs, seen_at=T0)
    raw = b"\xde\xad\xbe\xef" * 8
    for ref in store.pending_frames():
        store.store_frame(ref.id, raw, downloaded_at=T0)
    assert store.pending_frames() == []
    frames = store.frames_to_decode(norad_cat_id=NORAD)
    assert len(frames) == len(obs["demoddata"])
    f = frames[0]
    assert f.raw == raw and f.sha256 == hashlib.sha256(raw).hexdigest()
    assert f.observation_id == obs["id"] and f.norad_cat_id == NORAD and f.ground_station == obs["ground_station"]
    assert f.tle1 == obs["tle1"] and f.frame_time is not None and f.timestamp == f.frame_time
    assert store.stats(NORAD)["observations_complete"] == 1
    # re-storing the same frame is harmless (idempotent by URL; a second run never even asks)
    assert store.stats(NORAD)["frames_ok"] == len(obs["demoddata"])


def test_failed_downloads_are_retried_up_to_the_attempt_budget(store):
    obs = obs_with_frames()
    store.upsert_observation(obs, seen_at=T0)
    ref = store.pending_frames()[0]
    for _ in range(5):
        store.mark_frame_failed(ref.id, "HTTP 500")
    assert ref.id not in {p.id for p in store.pending_frames(max_attempts=5)}
    assert ref.id in {p.id for p in store.pending_frames(max_attempts=6)}
    assert store.stats(NORAD)["frames_failed"] == 1
    assert store.reset_failed_frames(NORAD) == 1  # --retry-failed
    assert ref.id in {p.id for p in store.pending_frames(max_attempts=5)}
    assert store.reset_failed_frames(NORAD + 1) == 0


def test_sweep_lifecycle_and_watermark(store):
    assert store.unfinished_sweep(NORAD, "good") is None
    assert store.get_watermark(NORAD) is None
    sid = store.open_sweep(norad_cat_id=NORAD, status_filter="good", since=T0, started_at=T0, first_url="https://x/1")
    sweep = store.unfinished_sweep(NORAD, "good")
    assert sweep is not None and sweep.id == sid and sweep.next_url == "https://x/1" and sweep.pages_done == 0
    store.record_page(sid, next_url="https://x/2", observations=25, at=T0)
    sweep = store.unfinished_sweep(NORAD, "good")
    assert sweep.next_url == "https://x/2" and sweep.pages_done == 1 and sweep.observations_seen == 25
    assert store.get_watermark(NORAD) is None
    # a second open_sweep aborts the running one
    t1 = T0 + dt.timedelta(hours=2)
    sid2 = store.open_sweep(norad_cat_id=NORAD, status_filter="good", since=T0, started_at=t1, first_url="https://x/1")
    assert store.unfinished_sweep(NORAD, "good").id == sid2
    # recording the last page completes the sweep and sets the watermark to the sweep's started_at
    store.record_page(sid2, next_url=None, observations=3, at=t1 + dt.timedelta(minutes=5))
    assert store.unfinished_sweep(NORAD, "good") is None
    assert store.get_watermark(NORAD) == t1
    # the watermark never moves backwards
    sid3 = store.open_sweep(norad_cat_id=NORAD, status_filter="good", since=T0, started_at=T0, first_url="https://x/1")
    store.complete_sweep(sid3, finished_at=t1)
    assert store.get_watermark(NORAD) == t1
    assert store.stats(NORAD)["sweeps"] == {"aborted": 1, "done": 2}
    assert store.unfinished_sweep(NORAD, "bad") is None  # keyed by status filter too


def test_decode_bookkeeping(store):
    obs = obs_with_frames()
    store.upsert_observation(obs, seen_at=T0)
    refs = store.pending_frames()
    for ref in refs:
        store.store_frame(ref.id, b"\x00", downloaded_at=T0)
    todo = store.frames_to_decode()
    assert len(todo) == len(refs)
    store.mark_decoded(todo[0].id, "ok", decoded_at=T0)
    store.mark_decoded(todo[1].id, "error", decoded_at=T0, error="bad frame")
    assert store.frames_to_decode() == []
    assert len(store.frames_to_decode(redo=True)) == len(refs)
    assert store.frames_to_decode(limit=1, redo=True)[0].id == todo[0].id
