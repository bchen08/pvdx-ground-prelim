"""pvdx-push: the PushClient against a mock service, stdin/file parsing and the command itself."""

from __future__ import annotations

import base64
import datetime as dt
import json
from functools import partial
from pathlib import Path

import httpx
import pytest

from pvdx_ground.config import ConfigError
from pvdx_ground.push import PushClient, PushError, PushFrame
from pvdx_ground.push.__main__ import frames_from_stdin, main, scan

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 27, 14, 3, 5, tzinfo=UTC)


class FakeService:
    """Answers POST /ingest/frames like the real API; ``queue`` holds canned responses to return first."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.queue: list[httpx.Response | Exception] = []
        self.seen: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        body = json.loads(request.content)
        frames, accepted = [], 0
        for item in body["frames"]:
            key = item["raw"] + item["received_at"]
            new = key not in self.seen
            self.seen.add(key)
            accepted += int(new)
            frames.append({"id": len(self.seen), "new": new, "size": len(base64.b64decode(item["raw"]))})
        return httpx.Response(200, json={"accepted": accepted, "duplicates": len(frames) - accepted, "frames": frames})


@pytest.fixture
def service() -> FakeService:
    return FakeService()


def make_client(service: FakeService, **kw) -> PushClient:
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    client = PushClient("http://cloud.test:8080/", station="BSE", transport=httpx.MockTransport(service.handler), **kw)
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def test_send_batches_and_reports_totals(service):
    frames = [PushFrame(raw=bytes([i]), received_at=T0 + dt.timedelta(seconds=i), frequency=436500000, meta={"k": "v"}) for i in range(5)]
    with make_client(service, token="secret", batch_size=2) as client:
        result = client.send(62394, frames)
    assert result.sent == 5 and result.accepted == 5 and result.duplicates == 0 and result.requests == 3
    assert result.frame_ids == [1, 2, 3, 4, 5]
    assert [len(json.loads(r.content)["frames"]) for r in service.requests] == [2, 2, 1]
    first = service.requests[0]
    assert str(first.url) == "http://cloud.test:8080/ingest/frames" and first.headers["X-Ingest-Token"] == "secret"
    assert first.headers["User-Agent"].startswith("pvdx-push/")
    body = json.loads(first.content)
    assert body["norad_cat_id"] == 62394 and body["station"] == "BSE"
    assert body["frames"][0] == {"raw": base64.b64encode(b"\x00").decode(), "received_at": "2026-09-27T14:03:05Z",
                                 "frequency": 436500000, "meta": {"k": "v"}}
    with make_client(service) as client:  # resend: the service reports duplicates
        again = client.send(62394, frames)
    assert again.accepted == 0 and again.duplicates == 5
    assert "X-Ingest-Token" not in service.requests[-1].headers


def test_retries_transient_failures_and_reports_rejections(service):
    frame = PushFrame(raw=b"\x01", received_at=T0)
    service.queue = [httpx.Response(503, text="busy"), httpx.ConnectError("boom"), httpx.Response(429)]
    with make_client(service) as client:
        assert client.send(1, [frame]).accepted == 1
        assert client.sleeps == [1.0, 2.0, 4.0]
        service.queue = [httpx.Response(401, json={"detail": "missing or invalid X-Ingest-Token"})]
        with pytest.raises(PushError, match="401"):
            client.send(1, [frame])
        service.queue = [httpx.Response(500)] * 4
        with pytest.raises(PushError, match="after 3 retries"):
            client.send(1, [frame])
        service.queue = [httpx.Response(200, text="not json")]
        with pytest.raises(PushError, match="not JSON"):
            client.send(1, [frame])
    with pytest.raises(ValueError):
        PushClient("http://x", station="")


def test_frames_from_stdin_accepts_time_and_hex():
    lines = ["# comment", "", "2026-09-27T14:03:05Z 0102ff", "aabb", "2026-09-27T14:03:07Z,cc"]
    frames = frames_from_stdin(lines, frequency=None, meta={"x": "1"})
    assert [f.raw for f in frames] == [b"\x01\x02\xff", b"\xaa\xbb", b"\xcc"]
    assert frames[0].received_at == T0 and frames[2].received_at == T0 + dt.timedelta(seconds=2)
    assert abs((frames[1].received_at - dt.datetime.now(UTC)).total_seconds()) < 60 and frames[1].meta == {"x": "1"}
    with pytest.raises(ConfigError, match="hexadecimal"):
        frames_from_stdin(["zz"], frequency=None, meta={})
    with pytest.raises(ConfigError, match="ISO-8601"):
        frames_from_stdin(["today 0102"], frequency=None, meta={})
    with pytest.raises(ConfigError):
        frames_from_stdin(["a b c"], frequency=None, meta={})


def test_scan_finds_new_files_once(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"\x01")
    (tmp_path / "b.bin").write_bytes(b"\x02")
    (tmp_path / "empty.bin").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    seen: set = set()
    assert [p.name for p in scan([tmp_path], seen)] == ["a.bin", "b.bin"]
    assert scan([tmp_path], seen) == []
    (tmp_path / "c.bin").write_bytes(b"\x03")
    assert [p.name for p in scan([tmp_path], seen)] == ["c.bin"]
    assert [p.name for p in scan([tmp_path / "a.bin"], set())] == ["a.bin"]


def test_main_sends_files_and_stdin(tmp_path, monkeypatch, service, capsys):
    import pvdx_ground.push.__main__ as push_main

    monkeypatch.setattr(push_main, "PushClient", partial(PushClient, transport=httpx.MockTransport(service.handler)))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f1.bin").write_bytes(b"\x01\x02")
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "f2.bin").write_bytes(b"\x03")
    args = ["--env", "none.env", "--url", "http://cloud.test", "--station", "BSE", "--norad", "62394", "--token", "t"]
    assert main(args + ["--time", "2026-09-27T14:03:05Z", "--frequency", "437000000", "--meta", "pass=7", "f1.bin", "frames"]) == 0
    body = json.loads(service.requests[-1].content)
    assert body["norad_cat_id"] == 62394 and body["station"] == "BSE"
    assert [base64.b64decode(f["raw"]) for f in body["frames"]] == [b"\x01\x02", b"\x03"]
    assert body["frames"][0]["received_at"] == "2026-09-27T14:03:05Z" and body["frames"][0]["frequency"] == 437000000
    assert body["frames"][0]["meta"] == {"pass": "7", "file": "f1.bin"} and body["frames"][1]["meta"]["file"] == "f2.bin"
    assert service.requests[-1].headers["X-Ingest-Token"] == "t"

    monkeypatch.setattr("sys.stdin", iter(["2026-09-27T14:03:05Z aabb\n"]))
    assert main(args + ["--stdin"]) == 0
    assert base64.b64decode(json.loads(service.requests[-1].content)["frames"][0]["raw"]) == b"\xaa\xbb"

    assert main(args) == 2  # nothing to send
    assert main(args + ["missing.bin"]) == 2
    assert main(["--env", "none.env", "--norad", "1", "f1.bin"]) == 2  # no PUSH_URL
    service.queue = [httpx.Response(401, json={"detail": "nope"})]
    assert main(args + ["f1.bin"]) == 1
