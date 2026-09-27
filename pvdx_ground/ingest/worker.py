"""Ingest worker: sweeps SatNOGS observations for one satellite and stores every demodulated frame.

A *sweep* walks the observations list (newest first) for ``start >= since``. Progress is persisted
after every page so a crash or ``--max-pages`` stop resumes from the saved cursor. When a sweep
finishes, the watermark advances to the sweep's start time; the next sweep begins at
``watermark - overlap`` so late-vetted observations and late-uploaded frames are still picked up.
Frames are downloaded after each page (data flows early) and any pending/failed frames are retried
at the end of every run. Re-running is idempotent: nothing is re-downloaded, nothing is skipped.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pvdx_ground.ingest.client import NetworkClient, SatnogsError
from pvdx_ground.ingest.state import StateStore
from pvdx_ground.timeutil import to_iso_z, utcnow

log = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """Counters for one ``run_once`` call."""

    norad_cat_id: int
    started_at: dt.datetime
    since: dt.datetime | None = None
    sweep_id: int | None = None
    resumed: bool = False
    completed: bool = False
    pages: int = 0
    observations_seen: int = 0
    observations_new: int = 0
    frames_queued: int = 0
    frames_downloaded: int = 0
    frames_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        state = "completed" if self.completed else "paused (will resume)"
        return (
            f"sweep {self.sweep_id} for NORAD {self.norad_cat_id} {state}: {self.pages} page(s), "
            f"{self.observations_seen} observations ({self.observations_new} new), "
            f"{self.frames_queued} frame(s) queued, {self.frames_downloaded} downloaded, "
            f"{self.frames_failed} failed"
        )


class IngestWorker:
    """Drives a ``NetworkClient`` and a ``StateStore`` for one satellite."""

    def __init__(
        self,
        client: NetworkClient,
        store: StateStore,
        *,
        norad_cat_id: int,
        start: dt.datetime,
        overlap: dt.timedelta = dt.timedelta(hours=48),
        status: str = "good",
        max_pages: int | None = None,
        max_frame_attempts: int = 5,
        download_delay: float = 0.25,
        now: Callable[[], dt.datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.store = store
        self.norad_cat_id = norad_cat_id
        self.start = start
        self.overlap = overlap
        self.status = status
        self.max_pages = max_pages
        self.max_frame_attempts = max_frame_attempts
        self.download_delay = download_delay
        self._now = now
        self._sleep = sleep
        self._failed_this_run: set[int] = set()

    # -- planning ----------------------------------------------------------------------------------
    def compute_since(self) -> dt.datetime:
        """Lower bound for the next sweep: configured start, or watermark minus overlap if later."""
        watermark = self.store.get_watermark(self.norad_cat_id)
        if watermark is None:
            return self.start
        return max(self.start, watermark - self.overlap)

    # -- one run -----------------------------------------------------------------------------------
    def run_once(self) -> SweepResult:
        """Run (or resume) one sweep, download frames, and return counters."""
        started_at = self._now()
        result = SweepResult(norad_cat_id=self.norad_cat_id, started_at=started_at)
        self._failed_this_run = set()

        pending = self.store.unfinished_sweep(self.norad_cat_id, self.status)
        if pending is not None and pending.next_url is None and pending.pages_done > 0:
            # Every page was stored but completion was never recorded (older DB or crash in between):
            # record it now from the sweep's own start time instead of re-walking the pages.
            log.info("sweep %d already stored all %d page(s); recording completion", pending.id, pending.pages_done)
            self.store.complete_sweep(pending.id, finished_at=self._now())
            result.sweep_id, result.since, result.resumed, result.completed = pending.id, pending.since, True, True
            self._download_pending(result)
            log.info(result.summary())
            return result

        if pending is not None and pending.next_url:
            result.resumed = True
            result.sweep_id = pending.id
            result.since = pending.since
            url = pending.next_url
            log.info(
                "resuming sweep %d (started %s, since %s, %d page(s) done) from saved cursor",
                pending.id, to_iso_z(pending.started_at), to_iso_z(pending.since), pending.pages_done,
            )
        else:
            since = self.compute_since()
            url = self.client.observations_url(norad_cat_id=self.norad_cat_id, start=since, status=self.status)
            result.sweep_id = self.store.open_sweep(
                norad_cat_id=self.norad_cat_id,
                status_filter=self.status,
                since=since,
                started_at=started_at,
                first_url=url,
            )
            result.since = since
            log.info(
                "starting sweep %d for NORAD %d: status=%s since %s (%s)",
                result.sweep_id, self.norad_cat_id, self.status, to_iso_z(since),
                "token" if self.client.token else "anonymous",
            )

        try:
            for page in self.client.iter_pages(url):
                result.pages += 1
                new_obs = new_frames = 0
                for obs in page.observations:
                    is_new, queued = self.store.upsert_observation(obs, seen_at=self._now())
                    new_obs += int(is_new)
                    new_frames += queued
                # Stores the cursor; on the last page this also marks the sweep done and advances the
                # watermark (to the sweep's own start time) in the same transaction, before any slow
                # frame downloads begin.
                self.store.record_page(
                    result.sweep_id, next_url=page.next_url, observations=len(page.observations), at=self._now()
                )
                result.observations_seen += len(page.observations)
                result.observations_new += new_obs
                result.frames_queued += new_frames
                log.info(
                    "page %d: %d observation(s), %d new, %d new frame(s) queued%s",
                    result.pages, len(page.observations), new_obs, new_frames,
                    "" if page.next_url else " (last page; sweep complete)",
                )
                if page.next_url is None:
                    result.completed = True
                self._download_pending(result)
                if result.completed:
                    break
                if self.max_pages is not None and result.pages >= self.max_pages:
                    log.info("--max-pages %d reached; sweep %d will resume next run", self.max_pages, result.sweep_id)
                    break
        except SatnogsError as exc:
            if result.resumed and result.pages == 0 and exc.status in (400, 404):
                # The saved cursor is no longer accepted: drop the sweep so the next run starts fresh.
                log.error("saved cursor for sweep %d rejected (%s); aborting it so the next run starts over", result.sweep_id, exc)
                self.store.finish_sweep(result.sweep_id, finished_at=self._now(), state="aborted")
            raise

        # Retry anything still pending/failed from this or earlier runs.
        self._download_pending(result)
        log.info(result.summary())
        return result

    def _download_pending(self, result: SweepResult) -> None:
        refs = self.store.pending_frames(max_attempts=self.max_frame_attempts)
        for ref in refs:
            if ref.id in self._failed_this_run:
                continue  # one download attempt (with HTTP retries) per frame per run; next run retries
            try:
                raw = self.client.download(ref.url)
            except SatnogsError as exc:
                self.store.mark_frame_failed(ref.id, str(exc))
                self._failed_this_run.add(ref.id)
                result.frames_failed += 1
                result.errors.append(f"frame {ref.id} ({ref.url}): {exc}")
                log.warning("frame %d download failed: %s", ref.id, exc)
                continue
            if not raw:
                log.warning("frame %d (%s) is empty; storing anyway", ref.id, ref.url)
            self.store.store_frame(ref.id, raw, downloaded_at=self._now())
            result.frames_downloaded += 1
            log.debug("frame %d stored (%d bytes) for observation %d", ref.id, len(raw), ref.observation_id)
            if self.download_delay:
                self._sleep(self.download_delay)

    # -- polling -----------------------------------------------------------------------------------
    def run_forever(self, interval: float, *, stop: "threading.Event | None" = None) -> None:
        """Repeat ``run_once`` every ``interval`` seconds until interrupted; errors are logged, not fatal.

        With ``stop`` (used by ``pvdx-serve``) the pause is interruptible and the loop ends once it is set.
        """
        while stop is None or not stop.is_set():
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - keep polling through transient failures
                log.exception("ingest run failed; retrying in %.0fs", interval)
            log.info("sleeping %.0fs until next poll", interval)
            if stop is None:
                self._sleep(interval)
            elif stop.wait(interval):
                return
