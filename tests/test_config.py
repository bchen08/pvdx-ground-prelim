"""Settings and the minimal dotenv loader."""

from __future__ import annotations

import datetime as dt

import pytest

from pvdx_ground.config import ConfigError, Settings, load_dotenv


def test_load_dotenv_parses_quotes_comments_and_export(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nSATNOGS_API_TOKEN=abc123 # trailing comment\nexport NORAD_CAT_ID='64875'\n"
        'INGEST_START="2026-09-01T00:00:00Z"\nNOT A LINE\nEMPTY=\nQUOTED="abc def" # comment\n'
    )
    monkeypatch.delenv("SATNOGS_API_TOKEN", raising=False)
    monkeypatch.setenv("NORAD_CAT_ID", "1")  # pre-existing env wins unless override
    parsed = load_dotenv(env_file)
    assert parsed == {"SATNOGS_API_TOKEN": "abc123", "NORAD_CAT_ID": "64875", "INGEST_START": "2026-09-01T00:00:00Z", "EMPTY": "", "QUOTED": "abc def"}
    import os

    assert os.environ["SATNOGS_API_TOKEN"] == "abc123" and os.environ["NORAD_CAT_ID"] == "1"
    load_dotenv(env_file, override=True)
    assert os.environ["NORAD_CAT_ID"] == "64875"
    assert load_dotenv(tmp_path / "missing.env") == {}


def test_settings_defaults_and_validation():
    now = dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc)
    s = Settings.from_env({"NORAD_CAT_ID": "64875", "SATNOGS_API_TOKEN": ""}, now=now)
    assert s.norad_cat_id == 64875 and s.satnogs_api_token is None
    assert s.ingest_start == now - dt.timedelta(days=7) and s.ingest_overlap == dt.timedelta(hours=48)
    assert s.ingest_status == "good" and str(s.state_db) == "data/state.db" and s.ingest_min_interval is None
    with pytest.raises(ConfigError, match="NORAD_CAT_ID"):
        Settings.from_env({})
    assert Settings.from_env({}, require_norad=False).norad_cat_id is None
    with pytest.raises(ConfigError):
        Settings.from_env({"NORAD_CAT_ID": "abc"})
    with pytest.raises(ConfigError):
        Settings.from_env({"NORAD_CAT_ID": "1", "INGEST_STATUS": "100"})
    with pytest.raises(ConfigError):
        Settings.from_env({"NORAD_CAT_ID": "1", "INGEST_START": "yesterday"})
    s = Settings.from_env({"NORAD_CAT_ID": "1", "INGEST_START": "2026-09-01", "INGEST_OVERLAP_HOURS": "6", "INGEST_MIN_INTERVAL": "20"})
    assert s.ingest_start == dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    assert s.ingest_overlap == dt.timedelta(hours=6) and s.ingest_min_interval == 20.0


def test_service_settings_defaults_and_parsing():
    s = Settings.from_env({"NORAD_CAT_ID": "1"})
    assert s.api_host == "127.0.0.1" and s.api_port == 8080 and s.api_cors_origins == ("http://localhost:3000",)
    assert s.ingest_token is None and s.ingest_poll == 600 and s.decode_poll == 60 and s.telemetry_stale_after == 3600
    assert s.telemetry_aliases == {} and s.redis_url is None and s.redis_key_prefix == "telemetry:"
    assert s.redis_telemetry_ttl == 3600 and s.push_url is None and s.push_station is None
    s = Settings.from_env({
        "NORAD_CAT_ID": "1", "API_HOST": "0.0.0.0", "API_PORT": "9000", "API_CORS_ORIGINS": "*, http://a.test ",
        "INGEST_TOKEN": "t", "INGEST_POLL": "30", "DECODE_POLL": "5", "TELEMETRY_STALE_AFTER": "120",
        "TELEMETRY_ALIASES": " battery = psu_battery ,temperature=obc_temp, ", "REDIS_URL": "redis://r:6379/1",
        "REDIS_KEY_PREFIX": "sat:", "REDIS_TELEMETRY_TTL": "90", "PUSH_URL": "http://cloud/", "PUSH_STATION": "BSE",
        "PUSH_TOKEN": "t",
    })
    assert s.api_host == "0.0.0.0" and s.api_port == 9000 and s.api_cors_origins == ("*", "http://a.test")
    assert s.telemetry_aliases == {"battery": "psu_battery", "temperature": "obc_temp"}
    assert s.redis_url == "redis://r:6379/1" and s.redis_key_prefix == "sat:" and s.redis_telemetry_ttl == 90
    assert s.push_url == "http://cloud" and s.push_station == "BSE" and s.push_token == "t"
    assert s.ingest_poll == 30 and s.decode_poll == 5 and s.telemetry_stale_after == 120 and s.ingest_token == "t"
    for bad in ({"API_PORT": "0"}, {"API_PORT": "70000"}, {"API_PORT": "http"}, {"INGEST_POLL": "0"},
                {"TELEMETRY_ALIASES": "battery"}, {"TELEMETRY_ALIASES": "=x"}, {"REDIS_TELEMETRY_TTL": "0"}):
        with pytest.raises(ConfigError):
            Settings.from_env({"NORAD_CAT_ID": "1", **bad})
