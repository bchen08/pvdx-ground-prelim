"""HTTP client for ``POST /ingest/frames`` on the cloud service.

This is the piece the ground station computer (GNU Radio side) runs: hand it raw demodulated frames and
it batches them into JSON requests, retries transient failures and reports what the service stored.
The contract, per request::

    {"norad_cat_id": 62394, "station": "BSE Providence",
     "frames": [{"raw": "<base64>", "received_at": "2026-09-27T14:03:05Z",
                 "frequency": 436500000, "rssi": -97.5, "meta": {...}}, ...]}

Resending the same frame is harmless: the service keys pushed frames by (satellite, station, receive
second, sha256) and reports them as duplicates.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import httpx

from pvdx_ground import __version__
from pvdx_ground.timeutil import to_iso_z

log = logging.getLogger(__name__)

USER_AGENT = f"pvdx-push/{__version__} (Brown Space Engineering)"


class PushError(RuntimeError):
    """The service rejected the frames or could not be reached after retries."""


@dataclass(frozen=True)
class PushFrame:
    """One frame to send."""

    raw: bytes
    received_at: dt.datetime
    frequency: int | None = None
    rssi: float | None = None
    meta: Mapping[str, Any] = dc_field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "raw": base64.b64encode(self.raw).decode("ascii"),
            "received_at": to_iso_z(self.received_at),
        }
        if self.frequency is not None:
            item["frequency"] = self.frequency
        if self.rssi is not None:
            item["rssi"] = self.rssi
        if self.meta:
            item["meta"] = dict(self.meta)
        return item


@dataclass
class PushResult:
    """Totals over every request of one ``send`` call."""

    sent: int = 0
    accepted: int = 0
    duplicates: int = 0
    frame_ids: list[int] = dc_field(default_factory=list)
    requests: int = 0


class PushClient:
    """Sends frames to ``<url>/ingest/frames`` in batches, with retries on 429/5xx/transport errors."""

    def __init__(
        self,
        url: str,
        *,
        station: str,
        token: str | None = None,
        batch_size: int = 200,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not station:
            raise ValueError("station is required")
        self.url = url.rstrip("/")
        self.station = station
        self.token = token or None
        self.batch_size = max(1, batch_size)
        self.max_retries = max_retries
        self._sleep = sleep
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            headers["X-Ingest-Token"] = self.token
        self._http = httpx.Client(timeout=timeout, transport=transport, headers=headers)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PushClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send(self, norad_cat_id: int, frames: Sequence[PushFrame]) -> PushResult:
        """Send ``frames`` (in batches); raises ``PushError`` on a definitive failure."""
        result = PushResult()
        for start in range(0, len(frames), self.batch_size):
            chunk = frames[start:start + self.batch_size]
            body = {"norad_cat_id": norad_cat_id, "station": self.station, "frames": [f.as_json() for f in chunk]}
            data = self._post(body)
            result.requests += 1
            result.sent += len(chunk)
            result.accepted += int(data.get("accepted", 0))
            result.duplicates += int(data.get("duplicates", 0))
            result.frame_ids.extend(int(item["id"]) for item in data.get("frames", []) if "id" in item)
        return result

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.url}/ingest/frames"
        attempt = 0
        while True:
            try:
                response = self._http.post(endpoint, json=body)
            except httpx.HTTPError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise PushError(f"POST {endpoint} failed after {self.max_retries} retries: {exc!r}") from exc
                self._backoff(attempt, f"{exc!r}")
                continue
            if response.status_code == 429 or response.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise PushError(f"POST {endpoint}: HTTP {response.status_code} after {self.max_retries} retries")
                self._backoff(attempt, f"HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise PushError(f"POST {endpoint}: HTTP {response.status_code} {response.text.strip()[:300]}")
            try:
                data = response.json()
            except ValueError as exc:
                raise PushError(f"POST {endpoint}: response is not JSON") from exc
            if not isinstance(data, dict):
                raise PushError(f"POST {endpoint}: unexpected response {data!r}")
            return data

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = min(2.0 ** (attempt - 1), 30.0)
        log.warning("push failed (%s); retry %d/%d in %.0fs", reason, attempt, self.max_retries, delay)
        self._sleep(delay)
