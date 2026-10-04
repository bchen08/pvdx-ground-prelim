"""Settings and the minimal dotenv loader."""

from __future__ import annotations

import datetime as dt

import pytest

from pvdx_ground.config import ConfigError, Settings, TelemetryAlias, load_dotenv


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
    assert s.telemetry_aliases == {"battery": TelemetryAlias("psu_battery"), "temperature": TelemetryAlias("obc_temp")}
    assert s.redis_url == "redis://r:6379/1" and s.redis_key_prefix == "sat:" and s.redis_telemetry_ttl == 90
    assert s.push_url == "http://cloud" and s.push_station == "BSE" and s.push_token == "t"
    assert s.ingest_poll == 30 and s.decode_poll == 5 and s.telemetry_stale_after == 120 and s.ingest_token == "t"
    for bad in ({"API_PORT": "0"}, {"API_PORT": "70000"}, {"API_PORT": "http"}, {"INGEST_POLL": "0"},
                {"TELEMETRY_ALIASES": "battery"}, {"TELEMETRY_ALIASES": "=x"}, {"REDIS_TELEMETRY_TTL": "0"}):
        with pytest.raises(ConfigError):
            Settings.from_env({"NORAD_CAT_ID": "1", **bad})


def test_telemetry_aliases_with_scale_offset_and_unit():
    aliases = Settings.from_env({"NORAD_CAT_ID": "1", "TELEMETRY_ALIASES": (
        "battery=psu_battery*0.001:V, temperature = obc_temp_mcu * 0.01 : degC ,"
        "signal_rssi=uhf_act_rssi_raw*0.5-134:dBm,uptime_seconds=obc_uptime:s,mode=obc-mode,kelvin=t+273.15:K,"
        "milli=x*1e-3,flip=y*-2 + 1"
    )}).telemetry_aliases
    assert aliases["battery"] == TelemetryAlias("psu_battery", 0.001, 0.0, "V")
    assert aliases["temperature"] == TelemetryAlias("obc_temp_mcu", 0.01, 0.0, "degC")
    assert aliases["signal_rssi"] == TelemetryAlias("uhf_act_rssi_raw", 0.5, -134.0, "dBm")
    assert aliases["uptime_seconds"] == TelemetryAlias("obc_uptime", unit="s")  # a unit alone converts nothing
    assert not aliases["uptime_seconds"].converts and aliases["battery"].converts
    assert aliases["mode"] == TelemetryAlias("obc-mode")  # a dash inside a name is not an offset
    assert aliases["kelvin"] == TelemetryAlias("t", 1.0, 273.15, "K")
    assert aliases["milli"] == TelemetryAlias("x", 0.001)
    assert aliases["flip"] == TelemetryAlias("y", -2.0, 1.0)


@pytest.mark.parametrize("entry", [
    "battery", "=x", "b=", "b=x*abc", "b=x*", "b=x**2", "b=x*2e", "b=x+", "b=x*2-", "b=*0.5", "b=:V", "b=x:",
    "b=x*0.001: ", "b=x*0", "b=x*1e999", "b=x+1e999",
])
def test_malformed_telemetry_aliases_are_config_errors(entry):
    with pytest.raises(ConfigError, match="TELEMETRY_ALIASES entry"):
        Settings.from_env({"NORAD_CAT_ID": "1", "TELEMETRY_ALIASES": f"ok=psu_battery,{entry}"})


def test_alias_conversion_rounds_away_float_noise():
    battery, temperature = TelemetryAlias("b", 0.001, unit="V"), TelemetryAlias("t", 0.01, unit="degC")
    rssi = TelemetryAlias("r", 0.5, -134, "dBm")
    assert 163 * 0.01 != 1.63 and temperature.convert(163) == 1.63  # 12 significant digits drop the noise
    assert temperature.convert(-1757) == -17.57 and temperature.convert(539.0) == 5.39
    assert battery.convert(7933) == 7.933 and battery.convert(8000) == 8.0 and battery.convert(7933.5) == 7.9335
    assert rssi.convert(83) == -92.5 and rssi.convert(268) == 0.0 and isinstance(rssi.convert(268), float)
    # a plain alias, or one with only a unit, passes every value through unchanged (no float coercion)
    for plain in (TelemetryAlias("x"), TelemetryAlias("x", unit="s")):
        assert type(plain.convert(21345256)) is int and plain.convert(21345256) == 21345256
        assert plain.convert("SAFE") == "SAFE" and plain.convert(True) is True
    # a converting alias never makes a value out of something that is not a finite number
    for bad in ("7933", "SAFE", True, None, 10**400):
        assert battery.convert(bad) is None, bad
    assert TelemetryAlias("x", 1e300).convert(1e300) is None


def test_url_schemes_are_checked_at_startup():
    for url in ("redis://r:6379/1", "rediss://:pw@r:6380/0", "unix:///run/redis.sock", "REDIS://r"):
        assert Settings.from_env({"NORAD_CAT_ID": "1", "REDIS_URL": url}).redis_url == url
    assert Settings.from_env({"NORAD_CAT_ID": "1", "INFLUX_URL": "https://influx.test"}).influx_url == "https://influx.test"
    for bad in ({"REDIS_URL": "localhost:6379"}, {"REDIS_URL": "http://r:6379"}, {"INFLUX_URL": "localhost:8086"},
                {"INFLUX_URL": "redis://r:6379"}):
        key = next(iter(bad))
        with pytest.raises(ConfigError, match=f"{key} must start with"):
            Settings.from_env({"NORAD_CAT_ID": "1", **bad})
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"NORAD_CAT_ID": "1", "REDIS_URL": "default:secret@r:6379"})
    assert "secret" not in str(excinfo.value)  # a URL can carry a password; it is never echoed


def test_ingest_min_interval_is_at_least_the_authenticated_throttle():
    assert Settings.from_env({"NORAD_CAT_ID": "1", "INGEST_MIN_INTERVAL": "15"}).ingest_min_interval == 15.0
    for bad in ("0", "-5", "0.001", "14.9"):
        with pytest.raises(ConfigError, match="INGEST_MIN_INTERVAL must be >= 15"):
            Settings.from_env({"NORAD_CAT_ID": "1", "INGEST_MIN_INTERVAL": bad})
    with pytest.raises(ConfigError, match="INGEST_MIN_INTERVAL must be a number"):
        Settings.from_env({"NORAD_CAT_ID": "1", "INGEST_MIN_INTERVAL": "fast"})
    for bad in ("nan", "inf", "-inf"):  # nan slipped past the minimum and turned pacing off
        with pytest.raises(ConfigError, match="INGEST_MIN_INTERVAL must be a finite number"):
            Settings.from_env({"NORAD_CAT_ID": "1", "INGEST_MIN_INTERVAL": bad})
    with pytest.raises(ConfigError, match="DECODE_POLL must be a finite number"):
        Settings.from_env({"NORAD_CAT_ID": "1", "DECODE_POLL": "inf"})
