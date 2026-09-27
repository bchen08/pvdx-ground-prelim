"""HTTP client for the SatNOGS Network API.

Facts verified against the live API and the satnogs-network source (network/api/{filters,pagination,
throttling}.py, Sept 2026):

* ``GET /api/observations/`` returns a **bare JSON list** (25 per page) and paginates with a cursor;
  the next page is only advertised in the ``Link: <url>; rel="next"`` response header.
* Filters: ``norad_cat_id`` (the documented ``satellite__norad_cat_id`` is silently ignored),
  ``sat_id``, ``status`` (a *string*: failed/bad/unknown/future/good; integers are rejected with 400),
  ``start`` (>=), ``start__lt``, ``end`` (<=), ``end__gt``, ``observation_id`` (comma separated).
* Throttling on the list endpoint: 60 requests/hour anonymous, 240/hour with a valid token; a 429
  carries ``Retry-After``. Frame files live on object storage and are not throttled.

The client paces list requests proactively, backs off on 429/5xx/transport errors, and only sends the
token to the SatNOGS host (never to the object-storage host that serves frame files).
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import httpx

from pvdx_ground import __version__
from pvdx_ground.timeutil import to_iso_z

log = logging.getLogger(__name__)

USER_AGENT = f"pvdx-ground/{__version__} (Brown Space Engineering; +https://brownspace.org)"
ANON_REQUESTS_PER_HOUR = 60
AUTH_REQUESTS_PER_HOUR = 240
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 300.0
OBSERVATION_STATUSES = ("failed", "bad", "unknown", "future", "good")

_LINK_SPLIT = re.compile(r",\s*(?=<)")


class SatnogsError(RuntimeError):
    """A request to SatNOGS failed permanently (after retries) or returned an unexpected body."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SatnogsAuthError(SatnogsError):
    """The API rejected our credentials and anonymous access was not possible."""


@dataclass
class Page:
    """One page of the observations list."""

    url: str
    observations: list[dict] = field(default_factory=list)
    next_url: str | None = None


def parse_link_header(value: str | None) -> dict[str, str]:
    """Parse an RFC 8288 ``Link`` header into ``{rel: url}``."""
    links: dict[str, str] = {}
    if not value:
        return links
    for part in _LINK_SPLIT.split(value):
        target, _, params = part.strip().partition(";")
        target = target.strip()
        if not (target.startswith("<") and target.endswith(">")):
            continue
        url = target[1:-1]
        for param in params.split(";"):
            key, _, val = param.strip().partition("=")
            if key.strip().lower() == "rel":
                for rel in val.strip().strip('"').split():
                    links[rel] = url
    return links


def parse_retry_after(value: str | None, *, now: Callable[[], dt.datetime] | None = None) -> float | None:
    """Interpret a ``Retry-After`` header (delta-seconds or HTTP-date) as seconds to wait."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    current = (now or (lambda: dt.datetime.now(dt.timezone.utc)))()
    return max(0.0, (when - current).total_seconds())


class NetworkClient:
    """Thin, polite wrapper around the SatNOGS Network REST API.

    ``sleep``/``monotonic``/``jitter`` are injectable so tests can run without waiting.
    """

    def __init__(
        self,
        base_url: str = "https://network.satnogs.org",
        token: str | None = None,
        *,
        min_interval: float | None = None,
        max_retries: int = 6,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self.anonymous_fallback = False
        self.max_retries = max_retries
        self._min_interval_override = min_interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter
        self._last_api_call: float | None = None
        self.requests_made = 0
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    # -- lifecycle -------------------------------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NetworkClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- pacing ------------------------------------------------------------------------------------
    @property
    def min_interval(self) -> float:
        """Seconds to keep between two list requests (derived from the published throttle rates)."""
        if self._min_interval_override is not None:
            return self._min_interval_override
        rate = AUTH_REQUESTS_PER_HOUR if self.token else ANON_REQUESTS_PER_HOUR
        return 3600.0 / rate * 1.05  # 5 % safety margin under the throttle

    def _pace(self) -> None:
        if self._last_api_call is None:
            return
        wait = self._last_api_call + self.min_interval - self._monotonic()
        if wait > 0:
            log.debug("pacing: sleeping %.1fs before next API request", wait)
            self._sleep(wait)

    # -- URLs --------------------------------------------------------------------------------------
    def observations_url(
        self,
        *,
        norad_cat_id: int | None = None,
        sat_id: str | None = None,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
        start_lt: dt.datetime | None = None,
        status: str | None = None,
        observation_ids: list[int] | None = None,
    ) -> str:
        """Build the first-page URL for ``/api/observations/`` with the given filters."""
        if status is not None and status not in OBSERVATION_STATUSES:
            raise ValueError(f"status must be one of {OBSERVATION_STATUSES}, got {status!r}")
        params: dict[str, str] = {}
        if norad_cat_id is not None:
            params["norad_cat_id"] = str(norad_cat_id)
        if sat_id:
            params["sat_id"] = sat_id
        if start is not None:
            params["start"] = to_iso_z(start)
        if start_lt is not None:
            params["start__lt"] = to_iso_z(start_lt)
        if end is not None:
            params["end"] = to_iso_z(end)
        if status is not None:
            params["status"] = status
        if observation_ids:
            params["observation_id"] = ",".join(str(i) for i in observation_ids)
        query = urlencode(params)
        return f"{self.base_url}/api/observations/" + (f"?{query}" if query else "")

    def _is_api_url(self, url: str) -> bool:
        return url.startswith(self.base_url + "/")

    # -- requests ----------------------------------------------------------------------------------
    def _backoff(self, attempt: int, response: httpx.Response | None, reason: str) -> None:
        retry_after = parse_retry_after(response.headers.get("Retry-After")) if response is not None else None
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
        delay += delay * 0.1 * self._jitter()
        log.warning("%s; retry %d/%d in %.1fs", reason, attempt, self.max_retries, delay)
        self._sleep(delay)

    def _request(self, url: str, *, paced: bool) -> httpx.Response:
        """GET ``url`` with pacing (API only), auth (API only), backoff and 401 fallback."""
        attempt = 0
        while True:
            headers: dict[str, str] = {}
            if self.token and self._is_api_url(url):
                headers["Authorization"] = f"Token {self.token}"
            if paced:
                self._pace()
            try:
                response = self._http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise SatnogsError(f"GET {url} failed after {self.max_retries} retries: {exc!r}") from exc
                self._backoff(attempt, None, f"GET {url}: {exc!r}")
                continue
            finally:
                if paced:
                    self._last_api_call = self._monotonic()
                    self.requests_made += 1

            status = response.status_code
            if status == 401 and self._is_api_url(url):
                if self.token and not self.anonymous_fallback:
                    log.error(
                        "SatNOGS Network rejected SATNOGS_API_TOKEN (401 %s). Falling back to anonymous "
                        "access at %d requests/hour; get a Network token at %s/users/edit/",
                        response.text.strip()[:120], ANON_REQUESTS_PER_HOUR, self.base_url,
                    )
                    self.token = None
                    self.anonymous_fallback = True
                    self._last_api_call = None  # the rejected request was not counted by the throttle
                    continue
                raise SatnogsAuthError(f"GET {url}: 401 {response.text.strip()[:200]}", status=401)
            if status == 429 or status >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise SatnogsError(f"GET {url}: HTTP {status} after {self.max_retries} retries", status=status)
                self._backoff(attempt, response, f"GET {url}: HTTP {status}")
                continue
            if status >= 400:
                raise SatnogsError(f"GET {url}: HTTP {status} {response.text.strip()[:300]}", status=status)
            return response

    def fetch_page(self, url: str) -> Page:
        """Fetch one observations page and its ``rel="next"`` link."""
        response = self._request(url, paced=True)
        try:
            body = response.json()
        except ValueError as exc:
            raise SatnogsError(f"GET {url}: response is not JSON") from exc
        if not isinstance(body, list):
            raise SatnogsError(f"GET {url}: expected a JSON list of observations, got {type(body).__name__}")
        links = parse_link_header(response.headers.get("Link"))
        return Page(url=url, observations=body, next_url=links.get("next"))

    def iter_pages(self, first_url: str) -> Iterator[Page]:
        """Follow ``Link: rel="next"`` from ``first_url`` until the last page."""
        url: str | None = first_url
        while url:
            page = self.fetch_page(url)
            yield page
            url = page.next_url

    def iter_observations(self, **filters: object) -> Iterator[dict]:
        """Yield observation records matching ``observations_url(**filters)`` across all pages."""
        for page in self.iter_pages(self.observations_url(**filters)):  # type: ignore[arg-type]
            yield from page.observations

    def download(self, url: str) -> bytes:
        """Download a demodulated-frame file (object storage; unauthenticated, not paced)."""
        return self._request(url, paced=False).content
