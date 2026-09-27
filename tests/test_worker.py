"""IngestWorker end-to-end against the recorded pages: first run, idempotent rerun, resume, failures."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from pvdx_ground.ingest.worker import IngestWorker
from tests.conftest import NORAD, SINCE, UTC, FakeNow, make_client

T0 = dt.datetime(2026, 9, 26, 20, 0, 0, tzinfo=UTC)
OVERLAP = dt.timedelta(hours=48)


def make_worker(fake_api, clock, store, **kw):
    client = make_client(fake_api, clock)
    return IngestWorker(
        client, store, norad_cat_id=NORAD, start=SINCE, overlap=OVERLAP, status="good",
        now=FakeNow(T0), sleep=clock.sleep, download_delay=0.0, **kw,
    )


def total_frames(fake_api) -> int:
    return len(fake_api.frames)


def test_first_run_sweeps_all_pages_and_downloads_every_frame(fake_api, clock, store):
    worker = make_worker(fake_api, clock, store)
    result = worker.run_once()
    assert result.completed and not result.resumed
    assert result.pages == 3 and result.observations_seen == 50 and result.observations_new == 50
    assert result.frames_queued == total_frames(fake_api) == result.frames_downloaded
    assert result.frames_failed == 0 and result.errors == []
    assert [str(r.url) for r in fake_api.api_calls][0].startswith(fake_api.page1_url.split("?")[0])
    assert len(fake_api.frame_calls) == total_frames(fake_api)
    stats = store.stats(NORAD)
    assert stats["observations"] == 50 and stats["frames_ok"] == total_frames(fake_api) and stats["frames_pending"] == 0
    assert stats["observations_complete"] == 50
    assert store.get_watermark(NORAD) == result.started_at
    assert store.unfinished_sweep(NORAD, "good") is None


def test_rerun_is_idempotent_no_redownload_no_skip(fake_api, clock, store):
    make_worker(fake_api, clock, store).run_once()
    frame_calls_before = len(fake_api.frame_calls)
    worker2 = make_worker(fake_api, clock, store)
    result = worker2.run_once()
    assert result.completed
    assert result.observations_seen == 50 and result.observations_new == 0
    assert result.frames_queued == 0 and result.frames_downloaded == 0
    assert len(fake_api.frame_calls) == frame_calls_before  # nothing re-downloaded
    assert store.stats(NORAD)["frames_ok"] == total_frames(fake_api)  # nothing lost


def test_second_sweep_starts_at_watermark_minus_overlap(fake_api, clock, store):
    worker = make_worker(fake_api, clock, store)
    first = worker.run_once()
    assert worker.compute_since() == first.started_at - OVERLAP
    make_worker(fake_api, clock, store).run_once()
    assert fake_api.first_page_queries[-1]["start"] == (first.started_at - OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
    # configured start wins when it is later than watermark - overlap
    worker.start = first.started_at - dt.timedelta(hours=1)
    assert worker.compute_since() == worker.start


def test_max_pages_pauses_and_next_run_resumes_from_saved_cursor(fake_api, clock, store):
    paused = make_worker(fake_api, clock, store, max_pages=1).run_once()
    assert not paused.completed and paused.pages == 1 and paused.observations_seen == 25
    assert store.get_watermark(NORAD) is None
    sweep = store.unfinished_sweep(NORAD, "good")
    assert sweep is not None and sweep.next_url == fake_api.page2_url and sweep.pages_done == 1
    frames_after_page1 = len(fake_api.frame_calls)
    assert frames_after_page1 == 2  # page 1 frames already flowed

    fake_api.calls.clear()
    resumed = make_worker(fake_api, clock, store).run_once()
    assert resumed.resumed and resumed.completed and resumed.sweep_id == sweep.id
    assert resumed.pages == 2  # pages 2 and 3 only
    requested = [str(r.url) for r in fake_api.api_calls]
    assert fake_api.page1_url.split("?")[0] in requested[0] and "cursor=" in requested[0]  # not page 1 again
    assert len(fake_api.frame_calls) == total_frames(fake_api) - frames_after_page1
    assert store.get_watermark(NORAD) == paused.started_at  # the sweep's own start, not the resume time
    assert store.stats(NORAD)["frames_ok"] == total_frames(fake_api)


def test_resume_days_later_keeps_original_sweep_start_as_watermark(fake_api, clock, store):
    t0 = dt.datetime(2026, 9, 25, tzinfo=UTC)  # late enough that watermark - overlap is after SINCE
    first = make_worker(fake_api, clock, store, max_pages=1)
    first._now = FakeNow(t0)
    paused = first.run_once()
    later = make_worker(fake_api, clock, store)
    later._now = FakeNow(t0 + dt.timedelta(days=3))
    resumed = later.run_once()
    assert resumed.resumed and resumed.completed
    assert store.get_watermark(NORAD) == paused.started_at
    assert later.compute_since() == paused.started_at - OVERLAP  # nothing between t0 and the resume is skipped


def test_completion_is_recorded_before_downloads_so_a_crash_there_does_not_repage(fake_api, clock, store):
    worker = make_worker(fake_api, clock, store)
    original = worker._download_pending

    def crash_during_final_download_pass(result):
        if result.completed:  # the last page is stored; the trailing download pass is where we die
            raise RuntimeError("disk full")
        original(result)

    worker._download_pending = crash_during_final_download_pass
    # leave one frame undownloaded so the next run has something left to fetch
    held_url = list(fake_api.frames)[0]
    fake_api.queue(held_url, httpx.Response(503, text="s3 hiccup"))
    worker.client.max_retries = 0
    with pytest.raises(RuntimeError):
        worker.run_once()
    assert store.unfinished_sweep(NORAD, "good") is None  # sweep already recorded as done
    assert store.get_watermark(NORAD) is not None
    assert store.stats(NORAD)["frames_failed"] == 1
    fake_api.inject.clear()
    fake_api.calls.clear()
    result = make_worker(fake_api, clock, store).run_once()  # a *new* sweep from watermark - overlap
    assert not result.resumed and result.completed
    assert store.stats(NORAD)["frames_ok"] == total_frames(fake_api) and store.stats(NORAD)["frames_pending"] == 0


def test_legacy_sweep_row_with_all_pages_stored_is_completed_not_rewalked(fake_api, clock, store):
    make_worker(fake_api, clock, store, max_pages=1).run_once()
    sweep = store.unfinished_sweep(NORAD, "good")
    store._db.execute("UPDATE sweeps SET next_url = NULL, pages_done = 3 WHERE id = ?", (sweep.id,))
    fake_api.calls.clear()
    result = make_worker(fake_api, clock, store).run_once()
    assert result.completed and result.resumed and result.pages == 0
    assert fake_api.api_calls == []  # no list requests spent
    assert store.get_watermark(NORAD) == sweep.started_at


def test_rejected_saved_cursor_aborts_the_sweep_so_next_run_starts_fresh(fake_api, clock, store):
    make_worker(fake_api, clock, store, max_pages=1).run_once()
    fake_api.queue(fake_api.page2_url, httpx.Response(404, text="gone"))
    with pytest.raises(Exception):
        make_worker(fake_api, clock, store).run_once()
    assert store.unfinished_sweep(NORAD, "good") is None
    result = make_worker(fake_api, clock, store).run_once()
    assert not result.resumed and result.completed and result.pages == 3


def test_crash_mid_sweep_resumes_without_reprocessing_stored_pages(fake_api, clock, store):
    fake_api.queue(fake_api.page2_url, *[httpx.Response(500)] * 10)
    worker = make_worker(fake_api, clock, store)
    worker.client.max_retries = 1
    with pytest.raises(Exception):
        worker.run_once()
    sweep = store.unfinished_sweep(NORAD, "good")
    assert sweep is not None and sweep.next_url == fake_api.page2_url and sweep.pages_done == 1
    assert store.stats(NORAD)["observations"] == 25

    fake_api.inject.clear()
    fake_api.calls.clear()
    result = make_worker(fake_api, clock, store).run_once()
    assert result.resumed and result.completed and result.pages == 2
    assert store.stats(NORAD)["observations"] == 50 and store.stats(NORAD)["frames_ok"] == total_frames(fake_api)


def test_frame_download_failure_is_retried_on_the_next_run(fake_api, clock, store):
    bad_url = next(iter(fake_api.frames))
    fake_api.queue(bad_url, *[httpx.Response(500, text="s3 hiccup")] * 10)
    worker = make_worker(fake_api, clock, store)
    worker.client.max_retries = 1
    result = worker.run_once()
    assert result.completed and result.frames_failed == 1
    assert result.frames_downloaded == total_frames(fake_api) - 1
    stats = store.stats(NORAD)
    assert stats["frames_failed"] == 1 and stats["observations_complete"] == 49

    fake_api.inject.clear()
    result2 = make_worker(fake_api, clock, store).run_once()
    assert result2.frames_downloaded == 1 and result2.frames_failed == 0
    assert store.stats(NORAD)["frames_ok"] == total_frames(fake_api) and store.stats(NORAD)["observations_complete"] == 50


def test_late_uploaded_frames_are_picked_up_by_the_overlap_rescan(fake_api, clock, store):
    make_worker(fake_api, clock, store).run_once()
    page1, link1 = fake_api.pages[next(k for k in fake_api.pages if "cursor" not in dict(k[2]))]
    obs = next(o for o in page1 if not o["demoddata"])
    late_url = f"https://s3.eu-central-1.wasabisys.com/satnogs-network/data_obs/x/{obs['id']}/data_{obs['id']}_2026-09-26T15-30-00_g0"
    obs["demoddata"] = [{"payload_demod": late_url}]
    fake_api.frames[late_url] = b"\x10\x20\x30"
    result = make_worker(fake_api, clock, store).run_once()
    assert result.observations_new == 0 and result.frames_queued == 1 and result.frames_downloaded == 1
    assert store.stats(NORAD)["frames_ok"] == total_frames(fake_api)


def test_run_forever_keeps_polling_through_errors(fake_api, clock, store):
    fake_api.queue(fake_api.page1_url, *[httpx.Response(500)] * 10)
    worker = make_worker(fake_api, clock, store)
    worker.client.max_retries = 0
    runs = []

    def sleep(seconds: float) -> None:
        runs.append(seconds)
        if len(runs) == 2:
            raise KeyboardInterrupt
        fake_api.inject.clear()  # second iteration succeeds

    worker._sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        worker.run_forever(30)
    assert runs == [30, 30]
    assert store.stats(NORAD)["frames_ok"] == total_frames(fake_api)
