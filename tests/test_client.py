"""NetworkClient: Link-header pagination, pacing, backoff and auth behaviour against recorded pages."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from pvdx_ground.ingest.client import (
    ANON_REQUESTS_PER_HOUR,
    AUTH_REQUESTS_PER_HOUR,
    SatnogsAuthError,
    SatnogsError,
    parse_link_header,
    parse_retry_after,
)
from tests.conftest import NORAD, SINCE, make_client


def test_parse_link_header_next_and_prev():
    header = '<https://x/api/observations/?cursor=abc&observation_id=1%2C2>; rel="next", <https://x/p>; rel="prev"'
    assert parse_link_header(header) == {"next": "https://x/api/observations/?cursor=abc&observation_id=1%2C2", "prev": "https://x/p"}
    assert parse_link_header(None) == {}
    assert parse_link_header('<https://x/only>; rel="prev"') == {"prev": "https://x/only"}


def test_parse_retry_after_seconds_and_http_date():
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after(None) is None
    now = dt.datetime(2026, 9, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert parse_retry_after("Sat, 26 Sep 2026 12:00:45 GMT", now=lambda: now) == 45.0
    assert parse_retry_after("garbage") is None


def test_observations_url_uses_verified_filter_names(client):
    url = client.observations_url(norad_cat_id=NORAD, start=SINCE, status="good")
    assert url == f"https://network.satnogs.org/api/observations/?norad_cat_id={NORAD}&start=2026-09-19T00%3A00%3A00Z&status=good"
    assert "satellite__norad_cat_id" not in url
    with pytest.raises(ValueError):
        client.observations_url(status="100")  # status is a string choice, integers are rejected upstream


def test_iter_pages_follows_link_header_until_last_page(client, fake_api):
    pages = list(client.iter_pages(fake_api.page1_url))
    assert [len(p.observations) for p in pages] == [25, 25, 0]
    assert pages[0].next_url == fake_api.page2_url
    assert pages[1].next_url == fake_api.page3_url
    assert pages[2].next_url is None
    assert len(fake_api.api_calls) == 3
    assert all(o["norad_cat_id"] == NORAD and o["status"] == "good" for p in pages for o in p.observations)


def test_pacing_between_list_requests_but_not_downloads(fake_api, clock):
    client = make_client(fake_api, clock)
    assert client.min_interval == pytest.approx(3600 / ANON_REQUESTS_PER_HOUR * 1.05)
    client.fetch_page(fake_api.page1_url)
    client.fetch_page(fake_api.page2_url)
    assert clock.sleeps == [pytest.approx(client.min_interval)]
    url = next(iter(fake_api.frames))
    client.download(url)
    client.download(url)
    assert len(clock.sleeps) == 1  # frame downloads are not paced
    client.close()


def test_token_changes_pacing_and_is_only_sent_to_the_api_host(fake_api, clock):
    client = make_client(fake_api, clock, token="secret")
    assert client.min_interval == pytest.approx(3600 / AUTH_REQUESTS_PER_HOUR * 1.05)
    client.fetch_page(fake_api.page1_url)
    frame_url = next(iter(fake_api.frames))
    client.download(frame_url)
    api_req, s3_req = fake_api.calls
    assert api_req.headers["Authorization"] == "Token secret"
    assert "authorization" not in {k.lower() for k in s3_req.headers}
    assert api_req.headers["User-Agent"].startswith("pvdx-ground/")
    client.close()


def test_429_honours_retry_after_then_succeeds(fake_api, clock):
    client = make_client(fake_api, clock, min_interval=0)  # isolate backoff sleeps from pacing sleeps
    fake_api.queue(fake_api.page1_url, httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "throttled"}))
    page = client.fetch_page(fake_api.page1_url)
    assert len(page.observations) == 25
    assert clock.sleeps == [7.0]
    assert len(fake_api.api_calls) == 2
    client.close()


def test_5xx_and_transport_errors_back_off_exponentially(fake_api, clock):
    client = make_client(fake_api, clock, min_interval=0)
    fake_api.queue(
        fake_api.page1_url,
        httpx.Response(502, text="bad gateway"),
        lambda req: httpx.ConnectError("boom", request=req),
        httpx.Response(503, text="unavailable"),
    )
    page = client.fetch_page(fake_api.page1_url)
    assert len(page.observations) == 25
    assert clock.sleeps == [2.0, 4.0, 8.0]
    client.close()


def test_retries_are_paced_like_any_other_api_request(fake_api, clock, client):
    fake_api.queue(fake_api.page1_url, httpx.Response(429, headers={"Retry-After": "7"}))
    client.fetch_page(fake_api.page1_url)
    assert clock.sleeps == [7.0, pytest.approx(client.min_interval - 7.0)]


def test_gives_up_after_max_retries(fake_api, clock):
    client = make_client(fake_api, clock, max_retries=2)
    fake_api.queue(fake_api.page1_url, *[httpx.Response(500)] * 3)
    with pytest.raises(SatnogsError):
        client.fetch_page(fake_api.page1_url)
    assert len(fake_api.api_calls) == 3
    client.close()


def test_4xx_other_than_429_401_is_fatal(fake_api, clock, client):
    fake_api.queue(fake_api.page1_url, httpx.Response(400, json={"status": ["Select a valid choice."]}))
    with pytest.raises(SatnogsError, match="400"):
        client.fetch_page(fake_api.page1_url)


def test_rejected_token_falls_back_to_anonymous_once(fake_api, clock, caplog):
    client = make_client(fake_api, clock, token="db-token-not-network-token")
    fake_api.queue(fake_api.page1_url, httpx.Response(401, json={"detail": "Invalid token."}))
    page = client.fetch_page(fake_api.page1_url)
    assert len(page.observations) == 25
    assert client.anonymous_fallback and client.token is None
    assert client.min_interval == pytest.approx(3600 / ANON_REQUESTS_PER_HOUR * 1.05)
    first, second = fake_api.api_calls
    assert "Authorization" in first.headers and "Authorization" not in second.headers
    assert any("rejected SATNOGS_API_TOKEN" in r.message for r in caplog.records)
    # a second 401 (now anonymous) is a hard error, not a loop
    fake_api.queue(fake_api.page2_url, httpx.Response(401, json={"detail": "nope"}))
    with pytest.raises(SatnogsAuthError):
        client.fetch_page(fake_api.page2_url)
    client.close()


def test_non_list_body_is_an_error(fake_api, clock, client):
    fake_api.queue(fake_api.page1_url, httpx.Response(200, json={"results": []}))
    with pytest.raises(SatnogsError, match="expected a JSON list"):
        client.fetch_page(fake_api.page1_url)
