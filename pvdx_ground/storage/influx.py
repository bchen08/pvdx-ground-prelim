"""Write decoded frames to InfluxDB 2.x.

One point per frame in the ``telemetry`` measurement (configurable), timestamped with the frame time
SatNOGS encodes in the file name or the receive time our own ground station reported (falling back to
the observation start), tagged by satellite, decoder, source, ground station and observation id. All numeric fields are written as
floats so a field that is sometimes int and sometimes float never trips InfluxDB's per-shard field
type check; strings are written as string fields. Timestamps are nanoseconds: the frame's second plus a
deterministic sub-second offset derived from the frame's row id, so two frames received in the same
second stay distinct points while re-writes of the same frame remain idempotent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from pvdx_ground.decode import DecodedFrame

if TYPE_CHECKING:  # pragma: no cover - typing only
    from influxdb_client import Point

log = logging.getLogger(__name__)


class InfluxWriter:
    """Synchronous writer; construct once per process and ``close()`` when done."""

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket: str,
        *,
        measurement: str = "telemetry",
        timeout_ms: int = 30_000,
    ) -> None:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        self.org = org
        self.bucket = bucket
        self.measurement = measurement
        self._client = InfluxDBClient(url=url, token=token, org=org, timeout=timeout_ms)
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    def close(self) -> None:
        self._write_api.close()
        self._client.close()

    def __enter__(self) -> "InfluxWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ping(self) -> bool:
        """True when the server answers ``/ping`` (no authentication involved)."""
        return bool(self._client.ping())

    def check_access(self) -> str | None:
        """Verify token, org and bucket with one authenticated call; returns an error message or ``None``."""
        from influxdb_client.rest import ApiException

        try:
            bucket = self._client.buckets_api().find_bucket_by_name(self.bucket)
        except ApiException as exc:
            return f"InfluxDB rejected the credentials ({exc.status} {exc.reason}); check INFLUX_TOKEN / INFLUX_ORG"
        except Exception as exc:  # noqa: BLE001 - connection refused, DNS, timeout
            return f"InfluxDB not reachable: {exc}"
        if bucket is None:
            return f"bucket {self.bucket!r} does not exist in org {self.org!r}; check INFLUX_BUCKET / INFLUX_ORG"
        return None

    @staticmethod
    def timestamp_ns(decoded: DecodedFrame) -> int:
        """Nanosecond timestamp: whole seconds from the frame time, sub-second from the frame id."""
        frame = decoded.frame
        return int(frame.timestamp.timestamp()) * 1_000_000_000 + (frame.id % 1_000_000_000)

    def point(self, decoded: DecodedFrame) -> "Point":
        """Build the InfluxDB point for one decoded frame."""
        from influxdb_client import Point, WritePrecision

        frame = decoded.frame
        point = (
            Point(self.measurement)
            .tag("norad_cat_id", str(frame.norad_cat_id))
            .tag("sat_id", frame.sat_id or "")
            .tag("decoder", decoded.decoder)
            .tag("source", frame.source)
            .tag("observation_id", str(frame.observation_id) if frame.observation_id is not None else "")
            .tag("ground_station", str(frame.ground_station) if frame.ground_station is not None else "")
            .tag("station_name", frame.station_name or "")
            .time(self.timestamp_ns(decoded), WritePrecision.NS)
        )
        point.field("frame_id", float(frame.id))
        for name, value in decoded.fields.items():
            if isinstance(value, bool):
                point.field(name, float(int(value)))
            elif isinstance(value, (int, float)):
                point.field(name, float(value))
            else:
                point.field(name, str(value))
        return point

    def write_frames(self, frames: Iterable[DecodedFrame]) -> list[tuple[DecodedFrame, str]]:
        """Write points for ``frames``; returns the frames InfluxDB *rejected* (4xx), with the reason.

        A rejected batch (for example a field type conflict) is retried point by point so one bad frame
        cannot block the rest; 5xx and connection errors propagate so the caller retries the whole batch
        later without marking anything decoded.
        """
        from influxdb_client.rest import ApiException

        items: Sequence[DecodedFrame] = list(frames)
        if not items:
            return []
        points = [self.point(f) for f in items]
        try:
            self._write_api.write(bucket=self.bucket, org=self.org, record=points)
            return []
        except ApiException as exc:
            if not _is_point_rejection(exc):
                raise  # 5xx, 401/403/404 (credentials, org, bucket): the whole batch is retried later
            log.warning("InfluxDB rejected a batch of %d point(s) (%s); retrying one by one", len(points), exc.status)
        rejected: list[tuple[DecodedFrame, str]] = []
        for frame, point in zip(items, points):
            try:
                self._write_api.write(bucket=self.bucket, org=self.org, record=point)
            except ApiException as exc:
                if not _is_point_rejection(exc):
                    raise
                rejected.append((frame, f"InfluxDB {exc.status}: {str(exc.body)[:300]}"))
        return rejected


def _is_point_rejection(exc: "Exception") -> bool:
    """400/413/422 mean InfluxDB refused the *data*; auth, org and bucket errors (401/403/404) do not."""
    status = getattr(exc, "status", None)
    return status in (400, 413, 422)
