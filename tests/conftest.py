"""Shared fixtures: recorded SatNOGS API pages served through ``httpx.MockTransport`` (no live calls)."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from pvdx_ground.ingest.client import NetworkClient, parse_link_header
from pvdx_ground.ingest.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
FRAMES = FIXTURES / "frames"
API_BASE = "https://network.satnogs.org"
NORAD = 39444  # FUNcube-1: the satellite the pages were recorded for
SINCE = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
UTC = dt.timezone.utc


def url_key(url: str) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """Encoding-insensitive URL identity (host, path, sorted query pairs)."""
    parts = urlsplit(url)
    return parts.netloc, parts.path, tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))


def load_page(n: int) -> tuple[list[dict], str]:
    body = json.loads((FIXTURES / f"observations_page{n}.json").read_text())
    link = (FIXTURES / f"observations_page{n}.link").read_text().strip()
    return body, link


def load_frames() -> dict[str, bytes]:
    manifest = json.loads((FRAMES / "manifest.json").read_text())
    return {url: (FRAMES / meta["file"]).read_bytes() for url, meta in manifest.items()}


class FakeSatnogs:
    """In-memory stand-in for network.satnogs.org + the S3 host, driven by the recorded fixtures.

    ``inject[url]`` is a FIFO of canned responses (``httpx.Response`` or an exception factory taking the
    request) returned before the recorded one, to simulate 429/5xx/401/transport failures.
    """

    def __init__(self) -> None:
        page1, link1 = load_page(1)
        page2, link2 = load_page(2)
        self.page1_url = f"{API_BASE}/api/observations/?norad_cat_id={NORAD}&start=2026-09-19T00%3A00%3A00Z&status=good"
        self.page2_url = parse_link_header(link1)["next"]
        self.page3_url = parse_link_header(link2)["next"]
        self.pages: dict[tuple, tuple[list[dict], str | None]] = {
            url_key(self.page1_url): (page1, link1),
            url_key(self.page2_url): (page2, link2),
            url_key(self.page3_url): ([], None),  # recorded set ends here: empty last page, no Link
        }
        self.frames: dict[str, bytes] = load_frames()
        self.calls: list[httpx.Request] = []
        self.first_page_queries: list[dict[str, str]] = []
        self.inject: dict[tuple, list[httpx.Response | Callable[[httpx.Request], BaseException]]] = {}

    # -- helpers ---------------------------------------------------------------------------------
    @property
    def api_calls(self) -> list[httpx.Request]:
        return [r for r in self.calls if r.url.host == "network.satnogs.org"]

    @property
    def frame_calls(self) -> list[httpx.Request]:
        return [r for r in self.calls if r.url.host != "network.satnogs.org"]

    def queue(self, url: str, *responses: httpx.Response | Callable[[httpx.Request], BaseException]) -> None:
        self.inject.setdefault(url_key(url), []).extend(responses)

    def set_page(self, url: str, observations: list[dict], link: str | None) -> None:
        self.pages[url_key(url)] = (observations, link)

    # -- transport -------------------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = url_key(str(request.url))
        params = dict(key[2])
        if key[1] == "/api/observations/" and "cursor" not in params and key[0] == "network.satnogs.org":
            # Any first-page query for the recorded satellite maps to recorded page 1, whatever the
            # `start` bound is (later sweeps start at watermark - overlap); the query is kept for asserts.
            self.first_page_queries.append(params)
            key = url_key(self.page1_url)
        queued = self.inject.get(key)
        if queued:
            item = queued.pop(0)
            if isinstance(item, httpx.Response):
                return item
            raise item(request)
        if key in self.pages:
            body, link = self.pages[key]
            headers = {"Link": link} if link else {}
            return httpx.Response(200, json=body, headers=headers)
        url = str(request.url)
        if url in self.frames:
            return httpx.Response(200, content=self.frames[url], headers={"Content-Type": "application/octet-stream"})
        return httpx.Response(404, text="not found")


class FakeClock:
    """Deterministic monotonic clock; ``sleep`` advances it and records every call."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeNow:
    """Wall-clock stand-in for the worker; each call advances by one second."""

    def __init__(self, start: dt.datetime) -> None:
        self.current = start

    def __call__(self) -> dt.datetime:
        self.current += dt.timedelta(seconds=1)
        return self.current


@pytest.fixture
def fake_api() -> FakeSatnogs:
    return FakeSatnogs()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_client(fake_api: FakeSatnogs, clock: FakeClock, *, token: str | None = None, **kw) -> NetworkClient:
    return NetworkClient(
        API_BASE,
        token,
        transport=httpx.MockTransport(fake_api.handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        jitter=lambda: 0.0,
        **kw,
    )


@pytest.fixture
def client(fake_api: FakeSatnogs, clock: FakeClock) -> NetworkClient:
    c = make_client(fake_api, clock)
    yield c
    c.close()


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()
