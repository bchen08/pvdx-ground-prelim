# PVDX ground software: cloud telemetry service

Receive-path ground software for **PVDX** (Brown Space Engineering). It is the *cloud* box of the
communications design: it collects demodulated frames from the [SatNOGS Network](https://network.satnogs.org)
(other people's stations) and from **our own ground station** (pushed over HTTP), keeps the raw bytes with
full provenance in SQLite, decodes them with a pluggable decoder, and serves the result three ways:
an **HTTP API** for the mission-control web app, **Redis** keys the web app backend already reads, and
**InfluxDB + Grafana** for operators. Everything runs locally with Docker Compose plus one Python command.

Until PVDX flies, the pipeline runs against a **stand-in satellite** (CroCube, NORAD 62394) whose
frames SatNOGS already knows how to decode. For single-frame, real-time beacons, swapping in PVDX is a
two-line config change plus one decoder module; multi-packet frames, on-board timestamps, reassembly
and CFDP files need decoder-interface work first (see [Swapping in PVDX](#swapping-in-pvdx)).

```
 SatNOGS Network ─────pull────┐                                  ┌──► InfluxDB 2.x ──► Grafana (operators)
 (other stations)             │    pvdx-serve                    │
                              ├──► ingest ─► SQLite ─► decode ───┼──► Redis  ──► mission-control backend
 BSE ground station ──push───┘             state.db             │              (GET /telemetry, unchanged)
 (pvdx-push / POST /ingest/frames)         raw frames,           └──► HTTP API ──► web app, ops, scripts
                                           decoded fields,
                                           provenance
```

## Layout

```
pvdx_ground/            Python package (install with `pip install -e .`)
  config.py             .env / environment -> Settings
  timeutil.py           UTC helpers
  ingest/               SatNOGS Network client, SQLite state store (schema v3), sweep worker, `pvdx-ingest`
  decode/               Decoder interface + registry, satnogs-decoders (Kaitai) wrapper, PVDX stub, `pvdx-decode`
  storage/              InfluxDB 2.x writer
  publish/              Redis latest-values publisher (what the web app backend reads)
  api/                  FastAPI app: telemetry, history, frames, observations, health, frame push endpoint
  push/                 `pvdx-push`: ground-station client for the push endpoint
  serve/                `pvdx-serve`: ingest + decode + publish + API in one process
grafana/
  provisioning/         datasource (InfluxDB/Flux) and dashboard provider, loaded by Grafana at start
  dashboards/           pvdx-telemetry.json (decoder-agnostic dashboard)
tests/                  pytest suite (151 tests); fixtures/ holds pages + frames recorded from the live API
docker-compose.yml      InfluxDB 2.7 + Grafana 11.3 + Redis 7; `--profile cloud` adds the service container
Dockerfile              image for the service (pvdx-serve)
docs/                   operations guide (PDF) and the script that builds it
.env.example            every setting with a comment; copy to .env
CHANGELOG.md            what changed between versions, with upgrade notes
data/                   runtime files (state.db); git-ignored
```

## Setup

Requirements: Python 3.11+, Docker Desktop (Compose v2), and a SatNOGS account.

```bash
cp .env.example .env            # then edit: SATNOGS_API_TOKEN, NORAD_CAT_ID, INFLUX_TOKEN/passwords, INGEST_TOKEN
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"   # or: python -m venv .venv && pip install -e ".[dev]"
docker compose up -d            # on 127.0.0.1: InfluxDB :8086, Grafana :3000 (admin / GRAFANA_ADMIN_PASSWORD), Redis :6379
pvdx-serve                      # sweeps SatNOGS, decodes, writes InfluxDB + Redis, serves http://127.0.0.1:8080
open http://127.0.0.1:8080/docs          # interactive API docs
open http://localhost:3000/d/pvdx-telemetry
```

`pvdx-serve` is the normal way to run the service; it keeps going until Ctrl-C and survives transient
SatNOGS, InfluxDB and Redis failures. The one-shot commands still exist for operations and debugging:
`pvdx-ingest` (one sweep), `pvdx-decode` (decode what is new), both safe to re-run at any time, and both
accept `--poll SECONDS`. Do not run `pvdx-serve` and a polling `pvdx-ingest` on the same state database
at the same time; two sweepers would fight over the sweep cursor.

Upgrading from 0.1: the first start migrates `data/state.db` in place from schema 1 to 2, queuing the
frames that were already decoded for re-decoding so their decoded fields get stored, and then on to
schema 3 as described below, so duplicate receptions are grouped too. The 0.2 writer adds
a `source` tag, so the rewritten InfluxDB points become new series next to the 0.1 points instead of
replacing them; delete the measurement first (operations guide, 8.5) to avoid double counts. Add the new
`.env` keys from `.env.example` (service, Redis, push sections); every one of them has a working default.

Upgrading a 0.2.0 database (schema 2) to schema 3 (see [Duplicate receptions](#duplicate-receptions)):
stop the service and back up `data/state.db` first (`sqlite3 data/state.db ".backup state-v2.db"`), and
the InfluxDB volume if you want to keep the old points (operations guide, 8.9): the migration is one-way
and older code refuses a schema-3 database. The first command that opens the database (`pvdx-serve`, or
`pvdx-ingest --stats`) migrates it in place and in one transaction: it adds each frame's `family` and
`primary_frame_id`, groups every stored frame into transmissions and logs `migration done: N stored
frame(s) grouped into M transmission(s), K duplicate reception(s)` (0.15 s for a 15 926-frame CroCube
database: 7 406 transmissions, 8 520 copies). It does not recompute the latest values: a field's latest
value may still name a frame that is now a copy (the same value, with the copy's frame id, time, source
and station) until a newer primary replaces it. Then rebuild InfluxDB once, because the 0.2.0 points
have no `primary` tag and would stay as separate series next to the rewritten points: hidden under
Receptions = Primary only / Copies only and counted twice under All. Delete the measurement (operations
guide, 8.5) and run `pvdx-decode --redo` before starting `pvdx-serve` again. Copy the new
`TELEMETRY_ALIASES` line from `.env.example` to get engineering units (see
[Telemetry aliases and units](#telemetry-aliases-and-units)); an existing plain `alias=field` list keeps
serving raw values.

### Configuration (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `SATNOGS_API_TOKEN` | *(empty)* | SatNOGS **Network** API key (optional; see [Known gaps](#known-gaps)) |
| `SATNOGS_NETWORK_URL` | `https://network.satnogs.org` | API base URL |
| `NORAD_CAT_ID` | *(required)* | satellite to ingest; SatNOGS temporary IDs (98xxx/99xxx) work too |
| `INGEST_START` | now − 7 days | earliest observation start (ISO-8601 UTC); every sweep starts at max(this, watermark − overlap), so normally only the first sweep is affected. Keep it a fixed date a few days back |
| `INGEST_OVERLAP_HOURS` | `48` | how far behind the watermark each later sweep re-scans |
| `INGEST_STATUS` | `good` | observation status filter (`good` is where demoddata lives) |
| `INGEST_MIN_INTERVAL` | from throttle | seconds between API list requests (63 anon / 15.8 with token); at least 15, the authenticated throttle (a lower value is a configuration error) |
| `STATE_DB` | `data/state.db` | SQLite file holding observations, raw frames, decoded fields and sweep state |
| `DECODER` | *(empty)* | `satnogs:<struct>` (e.g. `satnogs:crocube`) or `pvdx` |
| `INFLUX_URL/ORG/BUCKET/TOKEN` | local defaults | InfluxDB 2.x connection; also read by docker compose. `INFLUX_URL` must start with `http://` or `https://` |
| `INFLUX_USERNAME/PASSWORD`, `GRAFANA_ADMIN_PASSWORD` | local defaults | initial admin credentials for the containers (InfluxDB password must be 8+ characters), applied only when their data volumes are first created; change the Grafana password later in its UI or with `grafana cli admin reset-admin-password`. `INFLUX_RETENTION` (default `0` = forever) sets the bucket retention on first start |
| `GRAFANA_BIND`, `API_BIND` | `127.0.0.1` | host address docker compose publishes Grafana and the `pvdx` container's API on; `0.0.0.0` only behind a TLS reverse proxy or a network/cloud firewall or security group, not ufw (see [Running in Docker](#running-in-docker)) |
| `API_HOST`, `API_PORT` | `127.0.0.1`, `8080` | where `pvdx-serve` listens (`0.0.0.0` in a container); `API_PORT` is also the host port of the `pvdx` container |
| `API_CORS_ORIGINS` | `http://localhost:3000` | browser origins allowed to call the API directly (comma separated, or `*`) |
| `INGEST_TOKEN` | *(empty)* | shared secret required as `X-Ingest-Token` on `POST /ingest/frames`; empty accepts pushes from anyone |
| `INGEST_POLL`, `DECODE_POLL` | `600`, `60` | seconds between SatNOGS sweeps / decode passes in `pvdx-serve` (pushed frames are decoded at once) |
| `TELEMETRY_STALE_AFTER` | `3600` | `GET /telemetry` reports `stale: true` when the newest decoded frame is older than this many seconds |
| `TELEMETRY_ALIASES` | *(empty)* | extra names for decoded fields, comma separated, each `alias=field[*scale][(+\|-)offset][:unit]` (see [Telemetry aliases and units](#telemetry-aliases-and-units)) |
| `REDIS_URL` | *(empty = off)* | e.g. `redis://localhost:6379/0` (must start with `redis://`, `rediss://` or `unix://`); latest values are published there at startup and after every decode pass that writes at least one decoded frame to InfluxDB |
| `REDIS_KEY_PREFIX`, `REDIS_TELEMETRY_TTL` | `telemetry:`, `3600` | key prefix and lifetime of the published keys, counted from the last publish |
| `PUSH_URL`, `PUSH_STATION`, `PUSH_TOKEN` | *(empty)* | for `pvdx-push` on the ground station computer: service URL, station name, and the service's `INGEST_TOKEN` |

CLI flags override the file: `pvdx-ingest --norad 62394 --since 2026-09-01T00:00:00Z --max-pages 5`,
`pvdx-decode --decoder satnogs:crocube --dry-run`, `pvdx-serve --port 9000 --no-ingest`.
`pvdx-ingest --stats` prints the state counters; `pvdx-ingest --retry-failed` re-queues frames that
exhausted their download attempts. Invalid settings (a malformed alias, a URL with the wrong scheme, an
interval below its minimum) stop every command at startup with `configuration error: ...` and exit
code 2.

### Telemetry aliases and units

`TELEMETRY_ALIASES` gives decoded fields the names the web app reads and, optionally, converts them to
engineering units. Each comma-separated entry is `alias=field[*scale][(+|-)offset][:unit]`; the alias
value is `raw * scale + offset`, rounded to 12 significant digits, and the unit is a free-form label. A
plain `alias=field` passes the raw value through unchanged, strings included. Field names cannot contain
`*` or `:`, and a trailing `+<number>` or `-<number>` is always read as the offset. The CroCube example
in `.env.example`:

```
TELEMETRY_ALIASES=battery=psu_battery*0.001:V,temperature=obc_temp_mcu*0.01:degC,signal_rssi=uhf_act_rssi_raw*0.5-134:dBm,uptime_seconds=obc_uptime:s
```

turns `psu_battery` 7933 (mV) into `battery` 7.933 V, `obc_temp_mcu` 163 (hundredths of a degree C) into
`temperature` 1.63 degC, `uhf_act_rssi_raw` 83 into `signal_rssi` −92.5 dBm and passes `obc_uptime`
(already seconds) through as `uptime_seconds`. `signal_rssi` is the RSSI the **spacecraft's** UHF receiver
measured, not the downlink signal strength at a ground station; the RSSI our station reports with a
pushed frame is kept only in that frame's push metadata (`meta.rssi` in `GET /frames/{id}`).

Only the aliases are converted, and only in three places: the `telemetry:<alias>` keys in Redis, the
alias values of `GET /telemetry`, and `GET /telemetry/history?field=<alias>`. Units appear in `units`
(alias → unit) in `telemetry:_meta` and `GET /telemetry`, and as `unit` in the history response.
Everything else stays in the decoder's raw units: the `telemetry:<field>` keys, `telemetry:_all`,
`/telemetry/latest`, `/telemetry/fields`, `/frames`, SQLite, InfluxDB and Grafana. A converting alias
whose field holds a string is logged once and left out of Redis (`null` in the API); a plain alias
publishes the string as it is.

## The service (`pvdx-serve`)

One process, three parts sharing `data/state.db` (WAL mode; each thread has its own SQLite connection
and every API request opens its own, so concurrent requests are safe):

* the **ingest** thread runs a SatNOGS sweep every `INGEST_POLL` seconds (exactly what `pvdx-ingest --poll` does);
* the **decode** thread decodes new frames every `DECODE_POLL` seconds *or immediately* when the API
  stores pushed frames, writes InfluxDB points, stores the decoded fields and the latest value of every
  field in SQLite, and publishes the latest values to Redis;
* the **HTTP API** (uvicorn) in the main thread.

The database is created or migrated once before the threads start. `GET /health` reports whether each
thread is alive and whether InfluxDB and Redis answer; a dead thread makes the status `degraded`, but the
process and the API keep running (with `--no-api` the process exits with code 1 once every worker has
stopped). A restart policy (systemd, launchd, Docker's `unless-stopped`) acts only when the process
exits, so it does not notice a dead worker: watch `status` in `/health` and restart `pvdx-serve` after
fixing the cause.
Ctrl-C or SIGTERM (`docker stop`, `kill`, systemd) stops the API, then the threads finish their current
step and exit; a sweep in progress stops between pages or frame downloads and resumes on the next start.
Flags: `--no-ingest` (frames arrive by push only), `--no-decode`, `--no-api`, `--host`, `--port`,
`--ingest-poll`, `--decode-poll`, `-v` (debug log + HTTP access log).

### Running in Docker

`docker compose up -d` starts InfluxDB, Grafana and Redis for a service running on the host.
`docker compose --profile cloud up -d --build` additionally builds `Dockerfile` and runs the service as
the `pvdx` container: it reads `.env`, overrides the InfluxDB and Redis URLs with the compose service
names, keeps its state in `./data` and publishes port `API_PORT` (default 8080) on `API_BIND`.
Do not also run `pvdx-serve` on the host against the same `data/state.db`. All four containers restart
unless stopped (so they come back after a reboot), and Grafana has a healthcheck; Docker restarts a
container when its process exits, never because it is merely `unhealthy`.

**Network exposure.** Every port is published on `127.0.0.1` by default: InfluxDB and Redis always
(Redis has no password), Grafana and the API unless `GRAFANA_BIND` / `API_BIND` say otherwise. This is
deliberate: Docker writes its own iptables rules for published ports, so they bypass host firewalls
such as ufw, and the bind address is the control that holds. A host-run `pvdx-serve` reaches InfluxDB
and Redis on loopback as before, and listens on `API_HOST` (default `127.0.0.1`; a host process is subject
to ufw as usual). On a cloud host, put a TLS reverse proxy (Caddy, nginx) in front of the API and
Grafana; a proxy on the same host reaches them on `127.0.0.1`. Set `GRAFANA_BIND=0.0.0.0` or
`API_BIND=0.0.0.0` only when the proxy runs elsewhere and a network/cloud firewall or security group
(not ufw on the host) limits who can reach those ports.
Before Grafana is reachable by anyone else, change its admin password: `GRAFANA_ADMIN_PASSWORD` is only
applied when the `grafana-data` volume is first created, so on an existing volume use the Grafana UI or
`docker compose exec grafana grafana cli admin reset-admin-password <new>`. Set `INGEST_TOKEN` too (see
[HTTP API](#http-api)).

## HTTP API

Interactive documentation (OpenAPI) is served at `/docs`. Times are ISO-8601 UTC; `since`/`until`
parameters also accept relative forms such as `-6h` or `-2d`. On the `/telemetry` endpoints `norad`
defaults to `NORAD_CAT_ID`, or to the only satellite in the database; `/frames` and `/observations`
without `norad` list every satellite.

| Endpoint | Returns |
| --- | --- |
| `GET /health` | status (`ok`/`degraded`), per-satellite counters, watermark and last sweep, worker threads, InfluxDB and Redis reachability |
| `GET /telemetry` | `{"telemetry": {field: value, alias: value, ...}, "units": {alias: unit}, "stale": bool, "norad_cat_id", "frame_time", "source"}`: the flat shape the mission-control backend serves today; raw field values, alias values converted to their units |
| `GET /telemetry/latest` | every field with its raw `value`, `frame_time`, `frame_id`, `source` (`satnogs`/`groundstation`) and `station_name`, plus staleness and the alias map (alias → field) |
| `GET /telemetry/fields` | known field names, type and when each was last seen |
| `GET /telemetry/history?field=psu_battery&since=-1d&until=&source=&copies=&limit=` | that field over time, oldest first (the newest `limit` points), one point per transmission unless `copies=true`; `field` may be an alias (values converted, `unit` set); the response names the `stored_field` it read |
| `GET /frames?norad=&since=&until=&source=&station=&decode_status=&primary=&limit=&offset=` | frame metadata with `family`, `primary_frame_id` and `primary`, newest first; `primary=true` keeps primaries, `primary=false` copies and frames not yet assigned |
| `GET /frames/{id}` | one frame: bytes (`raw_base64`, `raw_hex`), decoded fields, push metadata, observation summary, `family` and `primary_frame_id` |
| `GET /observations?norad=&since=&until=&ground_station=&limit=&offset=` | SatNOGS observations with per-observation frame counts |
| `GET /observations/{id}` | one observation including the complete SatNOGS record |
| `POST /ingest/frames` | store frames received by our ground station (see below) |

```bash
curl -s 'http://127.0.0.1:8080/telemetry' | jq .telemetry.battery
curl -s 'http://127.0.0.1:8080/telemetry/history?field=psu_battery&since=-3d' | jq '.points[-1]'
curl -s 'http://127.0.0.1:8080/frames?source=groundstation&limit=5' | jq '.frames[] | {id, frame_time, decode_status}'
curl -s 'http://127.0.0.1:8080/telemetry/history?field=battery&since=-1d' | jq '{stored_field, unit, count}'
```

Bad parameters get 400 or 422, not a 500. A time that is neither ISO-8601 nor relative, or lies outside
the years 1–9999, an invalid field name, and a `/telemetry` endpoint called without `norad` while
`NORAD_CAT_ID` is unset and the database does not hold exactly one satellite give 400. Values outside
their range give 422: ids (`/frames/{id}`, `/observations/{id}`) and `ground_station` must be at least 1,
`norad` between 1 and 999 999 999, `field` 1 to 200 characters long, `limit` and `offset` within their
bounds, and `source`, `decode_status`, `primary` and `copies` one of their documented values. An id that
does not exist gives 404.

Authentication is out of scope for now (it belongs to the frontend/backend layer). The only guard is
`INGEST_TOKEN` on the push endpoint; put the service behind the web app backend or a TLS reverse proxy
before exposing it publicly (see [Running in Docker](#running-in-docker)).

## Ground station push

Our own ground station computer (the GNU Radio side) sends raw demodulated frames to
`POST /ingest/frames` with header `X-Ingest-Token: <INGEST_TOKEN>`:

```json
{"norad_cat_id": 62394, "station": "BSE Providence",
 "frames": [{"raw": "<base64>", "received_at": "2026-09-27T14:03:05Z",
             "frequency": 436500000, "rssi": -97.5, "meta": {"pass": 12}}]}
```

The response lists each frame's id and whether it was new. Pushed frames are keyed by (satellite,
station, receive second, sha256), so resending after a timeout is harmless and reported as a duplicate.
Limits: 1000 frames per request, 64 KiB per frame; `norad_cat_id` between 1 and 999 999 999 and a
non-blank `station` of at most 120 characters (a body that breaks these rules is refused with 422 before
anything is stored). When `INGEST_TOKEN` is set, a request without the right `X-Ingest-Token` gets 401
before its body is even read, and the token is compared in constant time. Frames are decoded within a
second of arrival, with `source = "groundstation"` in the API, Redis and InfluxDB. A pushed frame with the
same bytes as a SatNOGS frame up to 45 s apart counts as the same transmission (see
[Duplicate receptions](#duplicate-receptions)).

`pvdx-push` is the reference client (install this package on the ground station computer, set `PUSH_URL`,
`PUSH_STATION`, `PUSH_TOKEN` and `NORAD_CAT_ID` in its `.env`):

```bash
pvdx-push frame1.bin frame2.bin                 # one frame per file; receive time = file mtime
pvdx-push --time 2026-09-27T14:03:05Z --frequency 436500000 --meta pass=12 frame.bin
pvdx-push --watch 5 /var/lib/gnuradio/frames/   # rescan the directory every 5 s, send new files
printf '2026-09-27T14:03:05Z 86a2...\n' | pvdx-push --stdin   # "[ISO-time] hex" lines, one frame each
```

Any other client works the same way (`curl -H 'X-Ingest-Token: ...' -d @body.json ...`). If the team
standardises on RabbitMQ between the ground station and the backend, an AMQP consumer can feed the same
store; the HTTP contract stays.

## Redis keys for the web app

This is the telemetry half of GS3 in the comms design (a latest-value cache; there is no command queue).
Once at startup and after every decode pass that writes at least one decoded frame to InfluxDB, the
service writes the latest value of every field (from primaries only, see
[Duplicate receptions](#duplicate-receptions)) to Redis (`REDIS_URL`):

| Key | Value |
| --- | --- |
| `telemetry:<field>` | newest raw value of each decoded field as a string (`7933`, `SAFE`) |
| `telemetry:<alias>` | the value of an aliased field under its `TELEMETRY_ALIASES` name, converted when the alias has a scale or offset (`battery` = `7.933`) |
| `telemetry:_meta` | JSON: satellite, newest frame time/id/source/station, `published_at`, `aliases` (alias → field), `units` (alias → unit), TTL |
| `telemetry:_all` | JSON: every field with its raw value, `frame_time`, `frame_id`, `source`, `station_name` |

All keys carry `REDIS_TELEMETRY_TTL`, counted from the publish, not from the frame time. In steady state
a silent satellite makes them expire and the web app shows "stale" by itself, but a restart and a pass
that decodes only old, backfilled frames (or only duplicate copies) both re-arm the TTL on every key,
however old the values are (see [Known gaps](#known-gaps)).

The pvdx-mission-control backend reads `telemetry:battery`, `elevation`, `temperature`, `signal_rssi`
and `uptime_seconds`. With the CroCube aliases from `.env.example` (see
[Telemetry aliases and units](#telemetry-aliases-and-units)) its `GET /telemetry` returns real values in
volts, degrees C, dBm and seconds once its Redis client points at this Redis and its fake telemetry loop
is removed; it must not convert them again. It still reports stale because nothing publishes
`elevation`. Its `signal_rssi` is the spacecraft receiver's RSSI, not the downlink strength at our
station.

A Redis outage never blocks decoding and loses nothing (`latest_values` keeps every value): the failed
publish is logged as a warning, and the keys are written again by the next decode pass that writes at
least one decoded frame, or at the next start. Keys that expired meanwhile stay absent until then.

## How the ingest worker behaves

* **Pagination.** `GET /api/observations/` returns a bare JSON list of 25 observations and advertises
  the next page only in the `Link: <url>; rel="next"` header (cursor pagination, newest first). The
  client follows that header until it disappears.
* **Filters (verified against the live API and satnogs-network source, Sept 2026).** The satellite
  filter is `norad_cat_id` — the `satellite__norad_cat_id` name that older docs mention is silently
  ignored and returns every satellite. `status` takes a **string** (`failed|bad|unknown|future|good`);
  an integer such as `100` is rejected with HTTP 400. `start` means `start >= value`, `end` means
  `end <= value`; `start__lt`, `end__gt` and `observation_id=1,2,3` also exist.
* **Politeness.** List requests are paced proactively under the published throttle (with a 5 % margin),
  429s honour `Retry-After`, 5xx and transport errors back off exponentially with jitter (2 s … 5 min,
  6 retries), and frame files are fetched with a short delay between them. Every request carries a
  `pvdx-ground/<version>` User-Agent.
* **Sweeps and restarts.** A run performs one *sweep* over `[since, now]`. The cursor of every stored
  page is persisted, so a crash, Ctrl-C, stopping `pvdx-serve` or `--max-pages` leaves a resumable sweep
  and the next run continues from that page instead of starting over. Storing the last page marks the
  sweep complete and advances the per-satellite *watermark* in the same transaction — to the time the
  sweep was *opened*, not the time it was resumed, so a sweep paused for days cannot skip observations
  that turned `good` meanwhile. The next sweep starts at `watermark − INGEST_OVERLAP_HOURS`, which also
  catches frames uploaded after an observation was first seen. A saved cursor the API no longer accepts
  (400/404) aborts that sweep so the following run starts fresh.
* **Idempotency.** Observations are upserted by SatNOGS id; frames are keyed by their `payload_demod`
  URL and downloaded at most once. Failed downloads are retried on later runs (one attempt per run, up
  to 5; `--retry-failed` resets the budget after a long outage). A re-run over already-ingested data
  fetches pages but downloads nothing and loses nothing — this is covered by tests. SQLite runs in WAL
  mode with immediate write transactions, so the ingest, decode and API parts can share `state.db`.
* **What is stored.** `observations`: station id/name/lat/lng/alt, start/end, status, observer,
  transmitter, frequency, TLE0/1/2 + source, payload/waterfall URLs, the full API record as JSON.
  `frames`: source (`satnogs`/`groundstation`), satellite, station, raw bytes, sha256, size, the frame
  time (from the SatNOGS file name, or the pushed receive time), push metadata, download and decode
  bookkeeping, the decoder name, the decoded fields as JSON, the `family` and the `primary_frame_id`
  (see [Duplicate receptions](#duplicate-receptions)). `latest_values`: the newest value of every field
  per satellite. `sweeps` and `watermarks` hold the resume state.

## Decoding and storage

`pvdx_ground/decode` defines the interface `decode(raw_bytes) -> dict[str, float | int | str]`.

* `satnogs:<struct>` wraps a compiled Kaitai struct from the
  [`satnogs-decoders`](https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders) PyPI
  package (the same code SatNOGS DB runs), and flattens the fields the struct documents. Nothing is
  hand-ported; `pip install` brings 160+ satellites' decoders with a single `kaitaistruct` dependency.
* `pvdx` is the PVDX stub in `pvdx_ground/decode/pvdx.py`: it documents the planned USLP → Space Packet
  → fields path and raises `DecodeError` until the format is frozen.

A decoder should return plain Python scalars. The decode stage normalises its output: bools become ints,
enums their names, bytes a hex string; `None`, NaN, ±inf, lists, nested structs and other types (numpy
integers, for example) are dropped.

The decode stage reads stored, not-yet-decoded frames from both sources, groups duplicate receptions
under a primary (see [Duplicate receptions](#duplicate-receptions)), decodes them and writes one InfluxDB
point per frame to measurement `telemetry`: tags `norad_cat_id`, `sat_id`, `decoder`, `source`,
`observation_id`, `ground_station`, `station_name` and `primary` (`true`/`false`; pushed frames have no
observation, so their points carry no `sat_id`, `observation_id` or `ground_station` tag at all: absent,
not empty); fields = decoded values (numbers as floats, strings as strings) plus `frame_id`; time = frame
time at nanosecond precision with the frame's `state.db` row id as sub-second offset, so frames received
in the same second stay distinct. Re-writing a frame overwrites its point only while the tag set and the
row id are unchanged (a frame's primary never changes, so neither does its `primary` tag): a tag-set
change (a new tag such as `primary` for 0.2.0 points, a renamed decoder) or a rebuilt `state.db` (new row
ids) writes duplicate points, so delete the measurement before re-decoding in those cases. A frame is
marked decoded only after its point is written: an InfluxDB outage (5xx, connection refused) or
credential/bucket problem (401/403/404, also caught by a startup check) leaves the batch to be retried,
while a point InfluxDB refuses because of its data (400/422, e.g. a field type conflict) is marked
`error` with the reason and does not block the rest of the batch. Frames the decoder rejects are marked
`error` too, and so are a frame that makes the decoder raise anything other than `DecodeError`
(`decode_error` reads `<decoder>: unexpected <exception>: ...`; an ERROR log line per frame, one
traceback per pass) and a frame with neither a frame time nor an observation (`no InfluxDB point: ...`).
None of them blocks the frames behind it; re-run with `--redo` after fixing the decoder. The decoded
fields are also kept in SQLite (`frames.decoded_json`, for copies too), and those of primaries are folded
into `latest_values`, which only ever moves forward in frame time, so a backfill of old observations
never overwrites a newer reading. Raw frames never leave SQLite.

The Grafana dashboard (`PVDX / PVDX Telemetry (SatNOGS)`) is decoder-agnostic: NORAD id, station and
field names are dashboard variables discovered from InfluxDB, with "voltage" and "temperature" panels
pre-filtered by regex. It therefore works unchanged for PVDX. Values are the decoder's raw units (CroCube:
battery mV, temperatures in hundredths of a degree C, `uhf_act_rssi_raw` with dBm = raw/2 − 134); alias
conversion does not reach InfluxDB. The **Receptions** variable (default *Primary only*) selects primaries,
copies or all receptions for the telemetry panels, "Frames in range", "Frames per hour" and the
latest-values table; "Receptions in range" and "Frames by station" always count every copy (see
[Duplicate receptions](#duplicate-receptions)). Frames pushed by our station lack the `ground_station`
tag the station picker lists, so they show under station = All and by name in "Frames by station" and the
latest-values table, but cannot be selected and disappear as soon as specific stations are picked; the
observation and station counters count all pushed frames as one of each.

## Duplicate receptions

A **reception** is one stored frame (one row in `frames`); a **transmission** is one frame the satellite
sent. One transmission often arrives several times: several SatNOGS stations hear the same pass,
SatNOGS publishes a gr-satellites `_gN` file next to the station's own decoder output, and our ground
station may push the same bytes. In a 15 926-frame CroCube database 8 520 receptions (53.5 %) are copies;
of its 895 decoded frames, 402 are. Every reception is kept and decoded; each transmission has one
**primary** reception, and the others are copies that point at it (`primary_frame_id`).

* **Family.** Each frame records which decoder produced it: `native` (the station's own gr-satnogs
  decoder: a plain or `_N` file name), `grsat` (a SatNOGS `_gN` file written by gr-satellites) or `push`
  (our ground station).
* **Window rule.** Copies have the same satellite and identical bytes (sha256), and their effective time
  (the frame time, else the observation start) lies within 30 s of the primary when both come from one
  SatNOGS observation, within 45 s otherwise (station clocks have been seen up to 39 s late). The window
  is anchored on the primary, never chained: a third reception 40 s after the second but 80 s after the
  primary starts a new transmission. In the recorded CroCube data identical bytes recur at least 221 s
  apart, so a repeat is a new transmission.
* **Who becomes primary.** At the start of every decode pass, each frame it reads that has no primary yet
  is assigned one (also frames that then end `empty` or `error`). `--dry-run` assigns none, but opening a
  schema-2 database migrates it first, and the migration gives every stored frame its primary even then.
  Native and pushed frames are placed before gr-satellites copies (each group in time order), so they
  become the primary when they are read in the same pass; each frame joins the nearest primary in its
  window or becomes a primary itself.
* **Sticky.** An assignment never changes. If a `_gN` copy is assigned before its native twin is stored
  (its native file is downloaded after a decode pass already read the `_gN` file), the `_gN` copy stays
  the primary and the native one becomes its copy.

Timestamp accuracy: native files carry the station's UTC clock when the frame was written (reception
plus decoder latency, given an NTP-synced station), pushes carry the `received_at` our station reports,
and `_gN` copies are stamped 0–16 s early (gr-satellites stamps frames relative to a start time the
SatNOGS client takes before launching it; the offset is constant within one observation and differs per
station). Because primaries prefer native and pushed copies, a transmission's time is the native time
whenever both are read in the same pass; transmissions heard only as `_gN` (7 of the 493 decoded ones in
that database), and the sticky case above, keep the early time.

What uses what:

* **latest values** (`/telemetry`, `/telemetry/latest`, Redis) come from primaries only;
* **InfluxDB** gets a point for every decoded reception, tagged `primary=true` for the primary and
  `primary=false` for copies;
* **`/telemetry/history`** returns one point per transmission (its primary) by default; `copies=true`
  returns every reception, each point marked `primary`. With `source=`, the default keeps only
  primaries from that source, so `source=groundstation` leaves out pushed frames whose transmission has
  a SatNOGS primary; add `copies=true` for every reception from that source;
* **`/frames`** shows `family`, `primary_frame_id` and `primary` for each frame; `primary=true` lists
  primaries, `primary=false` copies plus frames not yet assigned (not yet downloaded or decoded);
* **Grafana**: the *Receptions* variable (*Primary only*, *Copies only*, *All*) filters the telemetry
  panels, "Frames in range", "Frames per hour" and the latest-values table. With a station selected and
  *Primary only*, those panels show only the transmissions this station supplied the primary for; choose
  *All* to see everything it heard. "Receptions in range" and "Frames by station" count every reception
  (one SatNOGS station often twice: its own file and the `_gN` file); "Observations in range",
  "Stations in range" and "Last frame received" ignore the variable.

## Stand-in satellite

Picked from SatNOGS DB by listing every in-orbit satellite with a registered decoder (86 as of
2026-09-26), then measuring (a) recent frame activity in DB and (b) good observations *with demoddata*
on the Network — the source this pipeline ingests from. Frames uploaded by independent stations
(SiDS) do not count, which rules out satellites like the Geoscan fleet or GRBBeta that look active in DB
but rarely appear on the Network.

Network check on 2026-09-26 (newest 25 `good` observations per satellite, last 7 days):

| Satellite | NORAD | decoder | good obs/day | obs with frames | frames in 25 obs | stations | launched |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **CroCube** | 62394 | `satnogs:crocube` | ~52 | 20/25 | 2433 | 13 | 2024-12 |
| **CANVAS** | 68635 | `satnogs:canvas` | ~190 | 25/25 | 1327 | 22 | 2026-04 |
| AEPEX | 68506 | `satnogs:aepex` | ~63 | 23/25 | 326 | 23 | 2026-03 |
| FrontierSat | 69015 | `satnogs:frontiersat` | ~58 | 23/25 | 755 | 25 | 2026-05 |
| COSMO | 68460 | `satnogs:cosmo` | ~57 | 25/25 | 752 | 24 | 2026-03 |
| KNACKSAT-2 | 67683 | `satnogs:knacksat2` | ~38 | 18/25 | 46 | 21 | 2025-10 |
| 239Alferov | 64881 | `satnogs:geoscan` | ~15 | 17/25 | 123 | 11 | 2025-07 |
| CatSat | 60246 | `satnogs:catsat` | ~10 | 15/25 | 284 | 12 | 2024-07 |
| CUBEBEL-2 | 57175 | `satnogs:cubebel2` | ~8 | 18/25 | 82 | 16 | 2023-06 |
| SAL-E | 68458 | `satnogs:cp16` | ~6 | 15/25 | 164 | 8 | 2026-03 |
| Sharjahsat-1 | 55104 | `satnogs:sharjahsat1` | ~5 | 18/25 | 29 | 12 | 2023-01 |
| Colibri-S | 61746 | `satnogs:geoscan` | ~5 | 22/25 | 348 | 12 | 2024-11 |

The pipeline was built and verified end-to-end against **CroCube (NORAD 62394, `satnogs:crocube`)**: a
Croatian 1U in a 500 km SSO since Dec 2024, ~50 good observations/day, AX.25 text beacons with 126
documented fields (battery mV, PSU/OBC/UHF temperatures, uptimes, RSSI, reset counters). Caveat: CroCube
also downlinks images, and image passes produce hundreds of non-telemetry frames per observation.

**CANVAS (NORAD 68635, `satnogs:canvas`)** is the strongest alternative and closest analogue to PVDX: a
CU Boulder/LASP student CubeSat launched April 2026, the most-observed satellite in the survey (~190 good
observations/day from 22 stations), CCSDS Space Packets over AX.25 (like PVDX's planned SPP), 58/60
sampled frames decode, and the APID 32 beacon carries 39 EPS/thermal fields (battery and panel
temperatures, per-rail currents and voltages, state of charge). It is only five months in orbit, so its
long-term availability is less proven than CroCube's. Switching is `NORAD_CAT_ID=68635` and
`DECODER=satnogs:canvas` in `.env`.

## Swapping in PVDX

1. `.env`: set `NORAD_CAT_ID` to PVDX's catalog number (SatNOGS assigns a temporary 98xxx/99xxx id
   before the real one exists — that works too) and `DECODER=pvdx`. Delete or keep `data/state.db`;
   sweeps and watermarks are per NORAD id.
2. Implement `PvdxDecoder.decode` in `pvdx_ground/decode/pvdx.py` (USLP → SPP → fields), returning a
   flat dict. If PVDX gets a Kaitai struct merged into satnogs-decoders instead, `DECODER=satnogs:pvdx`
   needs no code at all.
3. Add a recorded PVDX frame to `tests/fixtures/` and a decode test next to the existing ones.
4. Update `TELEMETRY_ALIASES` so the web app's names map to PVDX field names (fields the decoder
   already returns in engineering units need no scale or offset, only the `:unit`).
5. Grafana needs no change; adjust the regexes of the `voltage_fields` / `temperature_fields`
   variables if PVDX field names use other words.

The ground station push path is decoder-agnostic too: the GNU Radio side sends whatever bytes it
demodulated, and the same decoder handles frames from both sources.

These steps are enough only for single-frame, real-time beacons. The interface is `decode(raw)` → one
flat dict with no frame context; every frame becomes one point at its receive time, non-scalar fields
are dropped, and frames are decoded in row-id order with sources and stations interleaved, duplicate
receptions included (each copy is decoded; grouping looks only at bytes and time, see
[Duplicate receptions](#duplicate-receptions)). Interface work is needed first for:

* **multi-packet frames**: two packets with the same fields in one frame collapse into one value; the
  decoder has to return a list of records, each with its own timestamp and APID/VCID;
* **on-board timestamps** (stored or playback telemetry): the point time is the receive time, so the
  on-board time can only become a field;
* **stateful reassembly** of packets that span frames: decoder state lives only in memory, is lost on
  restart, and the decoder is not told the source, station or push metadata to keep streams apart;
* **CFDP files and photos**: there is no file intake, storage or endpoint (see [Known gaps](#known-gaps)).

## Tests

```bash
pytest
```

The suite never touches the network: `tests/fixtures/` holds two consecutive observation pages (with
their real `Link` headers), their 11 demoddata files and four real CroCube frames, recorded from the
live API on 2026-09-26, and served through `httpx.MockTransport` / FastAPI's test client. Covered:
Link-header pagination, pacing, 429/5xx/transport backoff, token fallback and host scoping, stopping a
sweep mid-way, SQLite upsert/frame/sweep/watermark semantics, the schema 1 → 2 and 2 → 3 migrations,
pushed-frame dedupe, duplicate receptions (families, both windows, anchoring, sticky assignments, latest
values from primaries, the `primary` tag, history `copies`, the `/frames` filter, the dashboard's
Receptions variable), latest values that only move forward, history and listing queries, first run,
idempotent re-run, resume after `--max-pages` and after a crash, download retry, late-frame pickup,
config parsing (alias scale/offset/unit syntax, URL schemes, the `INGEST_MIN_INTERVAL` floor), decoder
resolution, decoder crashes and frames without a time, InfluxDB point construction, every API endpoint
including converted aliases, out-of-range parameters, concurrent requests, push validation and the token
checked before the body, the Redis publisher, the decode loop's wake-up and Redis-outage behaviour,
SIGTERM shutdown of `pvdx-serve`, and `pvdx-push` end to end.

## Known gaps

* **Token.** The token supplied for this build is a SatNOGS DB token; the Network API rejects it, so
  ingest currently runs anonymously at 60 requests/hour. Generate a Network API key to get 240/hour.
* **Backfill speed.** With 25 observations per page and the anonymous throttle, a sweep covers about
  1 500 observations per hour; CroCube also delivers ~100 frames per pass, so the first sweep over
  several days takes a while. Use `--max-pages` to chunk it — every run resumes where the last stopped.
* **Late vetting.** Observations only appear once their status is `good`. The 48 h overlap re-scan
  covers auto-vetting (immediate when frames arrive) and typical manual vetting; anything vetted later
  than the overlap is missed. Moving `INGEST_START` back does not help once a watermark exists (every
  sweep starts at max(`INGEST_START`, watermark − overlap)); run one sweep with a larger
  `--overlap-hours`, or delete the satellite's row in the `watermarks` table.
* **Not every frame is telemetry.** CroCube also downlinks image/file chunks (160-byte frames) and
  digipeated packets; the Kaitai struct rejects those, the decoder marks them `error`, and SatNOGS DB
  leaves them undecoded too. Only the AX.25 beacons (`OBC,…`, `PSU,…`, `U,…`, …) become points. Expect
  a large "not telemetry/undecodable" count after an image-download pass; it is not a pipeline fault.
  Because `decode_status = error` mixes these with real parse failures, decoder crashes (`unexpected` in
  `decode_error`) and InfluxDB rejections, it cannot flag a decoder regression, and errored frames are
  only retried by a full `pvdx-decode --redo`.
* **No CRC or range validation.** Kaitai structs are permissive: garbage bytes can "decode" into nonsense
  values, and a flipped digit in a text beacon is accepted. The push endpoint checks only base64, emptiness
  and size (no CRC/FECF check or `crc_ok` field), the PVDX FECF check is a TODO in the stub, and no field
  is range-checked. Where CRC is verified (ground station or cloud) is undecided.
* **Duplicate grouping is by identical bytes and time only.** A copy whose time is more than 45 s from
  the primary's (a late station clock plus a `_gN` copy's early stamp can add up to that: the recorded data
  has one such split, 50 s apart), or one with a bit error (another sha256), stays a transmission of its
  own, and two genuine transmissions with identical bytes at most 45 s apart (30 s within one observation)
  can merge (in the recorded CroCube data identical bytes recur at least 221 s apart). Assignments are
  sticky and times are not corrected, so a `_gN` copy assigned before its native twin keeps its primary and
  its 0–16 s early time, as do transmissions heard only as `_gN`.
  Per-station views count receptions ("Frames by station"), or with *Primary only* just the
  transmissions a station supplied the primary for.
* **InfluxDB field types.** All numbers are written as floats to avoid type conflicts; strings stay
  strings. Enum-like fields therefore appear as strings in Grafana tables, not on time-series panels.
* **One satellite per service.** `pvdx-serve` sweeps and decodes one `NORAD_CAT_ID`, and the Redis keys
  are unprefixed by satellite; pushed frames for another satellite are stored but not decoded until that
  id is configured.
* **No auth.** Deliberately left to the web app layer; only the push endpoint has a shared secret.
* **Phase 2, not built:** scheduling observations on the SatNOGS Network (needs PVDX registered in
  SatNOGS DB and a Network token with scheduling rights), CFDP reassembly of downlinked files/photos,
  and the commanding/uplink path (moderation queue → ground station), which belongs to the web app
  backend and the ground station computer.

Confirmed open issues that await a decision before they are fixed:

* **Freshness.** The Redis TTL runs from publish time, so restarts and backfills make old values look
  current, and freshness is per satellite, not per field: one fresh beacon type re-arms every key and
  clears `stale` in `/telemetry` while other fields are hours old. With the 1 h TTL, CroCube's gaps
  between passes leave the keys absent most of the time.
* **Web-app contract.** Units are handled: the aliases convert to engineering units in Redis and
  `/telemetry` and name them in `units`, so the team backend must not convert again. Still open: nothing
  publishes `elevation` (what it should mean is undecided), so the team backend always reports stale; the
  team's fake telemetry loop writes the same `telemetry:*` keys (the key namespace is undecided); and
  freshness (above).
* **Future-dated pushes pin latest values.** A pushed `received_at` in the future (up to InfluxDB's year
  2262 limit) is accepted, and because `latest_values` only moves forward, those fields stay frozen in the
  API and Redis until a genuinely newer frame arrives. `--redo` does not clear it.
* **Partial push batches.** `POST /ingest/frames` stores frames one by one; a bad frame later in the
  batch (invalid base64, empty, over 64 KiB, a `received_at` out of range) returns 400/413 with the
  earlier frames already stored, no ids reported, and their decoding left to the next `DECODE_POLL`.
  Resending is safe.
* **InfluxDB is a hard gate.** While it is down, decoded frames stay pending and nothing new reaches
  `latest_values`, Redis or the API (it catches up afterwards; an outage longer than the Redis TTL lets
  the keys expire). A point InfluxDB refuses (e.g. a field type conflict) marks the whole frame `error`
  and drops all of its fields; `--redo` hits the same conflict.
* **`/health` is always HTTP 200**, also when `degraded`. The Dockerfile `HEALTHCHECK` checks only the
  status code, so the container stays healthy with a dead worker or InfluxDB down, a dead worker does not
  end the process (so no restart policy fires), and the body has no progress signals (last successful
  sweep or decode pass, pending backlog).
* **Ingest retries and reconciliation.** A frame whose download fails on 5 runs is abandoned until
  `pvdx-ingest --retry-failed` (`pvdx-serve` never resets it and `/health` does not show it); a saved
  cursor that keeps failing with anything but 400/404 leaves its sweep open, so new passes are never
  fetched; an observation re-vetted from good to bad after ingest keeps its status and frames.
* **`pvdx-push` robustness.** `--watch` exits on the first rejected or failed push, on an unreadable
  file, and on a file over 64 KiB (which then blocks the files sorted after it on every restart); a file
  still being written is sent as several partial frames; a restart resends the whole directory (harmless
  duplicates). `--stdin` sends nothing until EOF, and one malformed line or an outage at EOF loses the
  whole buffer.
* **BSE station not selectable in Grafana.** Pushed points carry no `ground_station` tag (see
  [Decoding and storage](#decoding-and-storage)); the identifier for our own station is undecided.
* **SiDS frames not ingested.** Only the SatNOGS Network is read; frames independent stations upload to
  SatNOGS DB (SiDS) are not, although for CroCube they carry a sizeable share of the decodable telemetry.
