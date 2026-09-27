# PVDX ground software: cloud telemetry service

Receive-path ground software for **PVDX** (Brown Space Engineering). It is the *cloud* box of the
communications design: it collects demodulated frames from the [SatNOGS Network](https://network.satnogs.org)
(other people's stations) and from **our own ground station** (pushed over HTTP), keeps the raw bytes with
full provenance in SQLite, decodes them with a pluggable decoder, and serves the result three ways:
an **HTTP API** for the mission-control web app, **Redis** keys the web app backend already reads, and
**InfluxDB + Grafana** for operators. Everything runs locally with Docker Compose plus one Python command.

Until PVDX flies, the pipeline runs against a **stand-in satellite** (CroCube, NORAD 62394) whose
frames SatNOGS already knows how to decode. Swapping in PVDX is a two-line config change plus one
decoder module (see [Swapping in PVDX](#swapping-in-pvdx)).

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
  ingest/               SatNOGS Network client, SQLite state store (schema v2), sweep worker, `pvdx-ingest`
  decode/               Decoder interface + registry, satnogs-decoders (Kaitai) wrapper, PVDX stub, `pvdx-decode`
  storage/              InfluxDB 2.x writer
  publish/              Redis latest-values publisher (what the web app backend reads)
  api/                  FastAPI app: telemetry, history, frames, observations, health, frame push endpoint
  push/                 `pvdx-push`: ground-station client for the push endpoint
  serve/                `pvdx-serve`: ingest + decode + publish + API in one process
grafana/
  provisioning/         datasource (InfluxDB/Flux) and dashboard provider, loaded by Grafana at start
  dashboards/           pvdx-telemetry.json (decoder-agnostic dashboard)
tests/                  pytest suite (80 tests); fixtures/ holds pages + frames recorded from the live API
docker-compose.yml      InfluxDB 2.7 + Grafana 11.3 + Redis 7; `--profile cloud` adds the service container
Dockerfile              image for the service (pvdx-serve)
docs/                   operations guide (PDF) and the script that builds it
.env.example            every setting with a comment; copy to .env
data/                   runtime files (state.db); git-ignored
```

## Setup

Requirements: Python 3.11+, Docker Desktop (Compose v2), and a SatNOGS account.

```bash
cp .env.example .env            # then edit: SATNOGS_API_TOKEN, NORAD_CAT_ID, INFLUX_TOKEN/passwords, INGEST_TOKEN
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"   # or: python -m venv .venv && pip install -e ".[dev]"
docker compose up -d            # InfluxDB :8086, Grafana :3000 (admin / GRAFANA_ADMIN_PASSWORD), Redis :6379
pvdx-serve                      # sweeps SatNOGS, decodes, writes InfluxDB + Redis, serves http://127.0.0.1:8080
open http://127.0.0.1:8080/docs          # interactive API docs
open http://localhost:3000/d/pvdx-telemetry
```

`pvdx-serve` is the normal way to run the service; it keeps going until Ctrl-C and survives transient
SatNOGS, InfluxDB and Redis failures. The one-shot commands still exist for operations and debugging:
`pvdx-ingest` (one sweep), `pvdx-decode` (decode what is new), both safe to re-run at any time, and both
accept `--poll SECONDS`. Do not run `pvdx-serve` and a polling `pvdx-ingest` on the same state database
at the same time; two sweepers would fight over the sweep cursor.

Upgrading from 0.1: the first start migrates `data/state.db` from schema 1 to 2 in place and queues the
frames that were already decoded for re-decoding, so their decoded fields get stored (the InfluxDB points
are simply rewritten, they are idempotent). Add the new `.env` keys from `.env.example` (service, Redis,
push sections); every one of them has a working default.

### Configuration (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `SATNOGS_API_TOKEN` | *(empty)* | SatNOGS **Network** API key (optional; see above) |
| `SATNOGS_NETWORK_URL` | `https://network.satnogs.org` | API base URL |
| `NORAD_CAT_ID` | *(required)* | satellite to ingest; SatNOGS temporary IDs (98xxx/99xxx) work too |
| `INGEST_START` | now − 7 days | earliest observation start (ISO-8601 UTC); every sweep starts at max(this, watermark − overlap), so normally only the first sweep is affected. Keep it a fixed date a few days back |
| `INGEST_OVERLAP_HOURS` | `48` | how far behind the watermark each later sweep re-scans |
| `INGEST_STATUS` | `good` | observation status filter (`good` is where demoddata lives) |
| `INGEST_MIN_INTERVAL` | from throttle | seconds between API list requests (63 anon / 15.8 with token) |
| `STATE_DB` | `data/state.db` | SQLite file holding observations, raw frames, decoded fields and sweep state |
| `DECODER` | *(empty)* | `satnogs:<struct>` (e.g. `satnogs:crocube`) or `pvdx` |
| `INFLUX_URL/ORG/BUCKET/TOKEN` | local defaults | InfluxDB 2.x connection; also read by docker compose |
| `INFLUX_USERNAME/PASSWORD`, `GRAFANA_ADMIN_PASSWORD` | local defaults | initial admin credentials for the containers (InfluxDB password must be 8+ characters); `INFLUX_RETENTION` (default `0` = forever) sets the bucket retention on first start |
| `API_HOST`, `API_PORT` | `127.0.0.1`, `8080` | where `pvdx-serve` listens (`0.0.0.0` in a container) |
| `API_CORS_ORIGINS` | `http://localhost:3000` | browser origins allowed to call the API directly (comma separated, or `*`) |
| `INGEST_TOKEN` | *(empty)* | shared secret required as `X-Ingest-Token` on `POST /ingest/frames`; empty accepts pushes from anyone |
| `INGEST_POLL`, `DECODE_POLL` | `600`, `60` | seconds between SatNOGS sweeps / decode passes in `pvdx-serve` (pushed frames are decoded at once) |
| `TELEMETRY_STALE_AFTER` | `3600` | `GET /telemetry` reports `stale: true` when the newest decoded frame is older than this many seconds |
| `TELEMETRY_ALIASES` | *(empty)* | extra names for decoded fields, `alias=field,alias=field` (see [Redis keys](#redis-keys-for-the-web-app)) |
| `REDIS_URL` | *(empty = off)* | e.g. `redis://localhost:6379/0`; latest values are published there after every decode pass |
| `REDIS_KEY_PREFIX`, `REDIS_TELEMETRY_TTL` | `telemetry:`, `3600` | key prefix and lifetime of the published keys |
| `PUSH_URL`, `PUSH_STATION`, `PUSH_TOKEN` | *(empty)* | for `pvdx-push` on the ground station computer: service URL, station name, and the service's `INGEST_TOKEN` |

CLI flags override the file: `pvdx-ingest --norad 62394 --since 2026-09-01T00:00:00Z --max-pages 5`,
`pvdx-decode --decoder satnogs:crocube --dry-run`, `pvdx-serve --port 9000 --no-ingest`.
`pvdx-ingest --stats` prints the state counters; `pvdx-ingest --retry-failed` re-queues frames that
exhausted their download attempts.

## The service (`pvdx-serve`)

One process, three parts sharing `data/state.db` (each with its own SQLite connection; WAL mode):

* the **ingest** thread runs a SatNOGS sweep every `INGEST_POLL` seconds (exactly what `pvdx-ingest --poll` does);
* the **decode** thread decodes new frames every `DECODE_POLL` seconds *or immediately* when the API
  stores pushed frames, writes InfluxDB points, stores the decoded fields and the latest value of every
  field in SQLite, and publishes the latest values to Redis;
* the **HTTP API** (uvicorn) in the main thread.

The database is created or migrated once before the threads start. `GET /health` reports whether each
thread is alive and whether InfluxDB and Redis answer; a dead thread makes the status `degraded`. Ctrl-C
(SIGTERM in Docker) stops the API, then the threads finish their current step and exit. Flags:
`--no-ingest` (frames arrive by push only), `--no-decode`, `--no-api`, `--host`, `--port`,
`--ingest-poll`, `--decode-poll`, `-v` (debug log + HTTP access log).

### Running in Docker

`docker compose up -d` starts InfluxDB, Grafana and Redis for a service running on the host.
`docker compose --profile cloud up -d --build` additionally builds `Dockerfile` and runs the service as
the `pvdx` container: it reads `.env`, overrides the InfluxDB and Redis URLs with the compose service
names, keeps its state in `./data`, publishes port `API_PORT` (default 8080) and restarts unless stopped.
Do not also run `pvdx-serve` on the host against the same `data/state.db`.

## HTTP API

Interactive documentation (OpenAPI) is served at `/docs`. Times are ISO-8601 UTC; `since`/`until`
parameters also accept relative forms such as `-6h` or `-2d`. `norad` defaults to `NORAD_CAT_ID`, or to
the only satellite in the database.

| Endpoint | Returns |
| --- | --- |
| `GET /health` | status (`ok`/`degraded`), per-satellite counters, watermark and last sweep, worker threads, InfluxDB and Redis reachability |
| `GET /telemetry` | `{"telemetry": {field: value, alias: value, ...}, "stale": bool, "frame_time", "source"}`: the flat shape the mission-control backend serves today, so the frontend can point straight at this service |
| `GET /telemetry/latest` | every field with `value`, `frame_time`, `frame_id`, `source` (`satnogs`/`groundstation`) and `station_name`, plus staleness and the alias map |
| `GET /telemetry/fields` | known field names, type and when each was last seen |
| `GET /telemetry/history?field=psu_battery&since=-1d&source=&limit=` | that field over time, oldest first (the newest `limit` points) |
| `GET /frames?norad=&since=&until=&source=&station=&decode_status=&limit=&offset=` | frame metadata, newest first |
| `GET /frames/{id}` | one frame: bytes (`raw_base64`, `raw_hex`), decoded fields, push metadata, observation summary |
| `GET /observations?norad=&since=&until=&ground_station=&limit=&offset=` | SatNOGS observations with per-observation frame counts |
| `GET /observations/{id}` | one observation including the complete SatNOGS record |
| `POST /ingest/frames` | store frames received by our ground station (see below) |

```bash
curl -s 'http://127.0.0.1:8080/telemetry' | jq .telemetry.battery
curl -s 'http://127.0.0.1:8080/telemetry/history?field=psu_battery&since=-3d' | jq '.points[-1]'
curl -s 'http://127.0.0.1:8080/frames?source=groundstation&limit=5' | jq '.frames[] | {id, frame_time, decode_status}'
```

Authentication is out of scope for now (it belongs to the frontend/backend layer). The only guard is
`INGEST_TOKEN` on the push endpoint; put the service behind the web app backend or a reverse proxy
before exposing it publicly.

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
Limits: 1000 frames per request, 64 KiB per frame. Frames are decoded within a second of arrival, with
`source = "groundstation"` in the API, Redis and InfluxDB.

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

After every decode pass the service writes the latest value of every field to Redis (`REDIS_URL`):

| Key | Value |
| --- | --- |
| `telemetry:<field>` | newest value of each decoded field as a string (`8160`, `SAFE`) |
| `telemetry:<alias>` | the same values under the names in `TELEMETRY_ALIASES` |
| `telemetry:_meta` | JSON: satellite, newest frame time/id/source/station, `published_at`, aliases, TTL |
| `telemetry:_all` | JSON: every field with value, `frame_time`, `frame_id`, `source`, `station_name` |

All keys carry `REDIS_TELEMETRY_TTL`, so a silent satellite makes them expire and the web app shows
"stale" by itself. The pvdx-mission-control backend reads `telemetry:battery`, `elevation`, `temperature`,
`signal_rssi` and `uptime_seconds`; with `TELEMETRY_ALIASES=battery=psu_battery,temperature=obc_temp_mcu,...`
(the CroCube example in `.env.example`) its `GET /telemetry` returns real data as soon as its Redis client
points at this Redis and the fake telemetry loop is removed. Values are the decoder's raw units (CroCube:
battery in mV, temperatures in tenths of a degree). A Redis outage never blocks decoding: the next pass
republishes everything from the `latest_values` table.

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
  page is persisted, so a crash, Ctrl-C or `--max-pages` leaves a resumable sweep and the next run
  continues from that page instead of starting over. Storing the last page marks the sweep complete and
  advances the per-satellite *watermark* in the same transaction — to the time the sweep was *opened*,
  not the time it was resumed, so a sweep paused for days cannot skip observations that turned `good`
  meanwhile. The next sweep starts at `watermark − INGEST_OVERLAP_HOURS`, which also catches frames
  uploaded after an observation was first seen. A saved cursor the API no longer accepts (400/404) aborts
  that sweep so the following run starts fresh.
* **Idempotency.** Observations are upserted by SatNOGS id; frames are keyed by their `payload_demod`
  URL and downloaded at most once. Failed downloads are retried on later runs (one attempt per run, up
  to 5; `--retry-failed` resets the budget after a long outage). A re-run over already-ingested data
  fetches pages but downloads nothing and loses nothing — this is covered by tests. SQLite runs in WAL
  mode with immediate write transactions, so the ingest, decode and API parts can share `state.db`.
* **What is stored.** `observations`: station id/name/lat/lng/alt, start/end, status, observer,
  transmitter, frequency, TLE0/1/2 + source, payload/waterfall URLs, the full API record as JSON.
  `frames`: source (`satnogs`/`groundstation`), satellite, station, raw bytes, sha256, size, the frame
  time (from the SatNOGS file name, or the pushed receive time), push metadata, download and decode
  bookkeeping, the decoder name and the decoded fields as JSON. `latest_values`: the newest value of
  every field per satellite. `sweeps` and `watermarks` hold the resume state.

## Decoding and storage

`pvdx_ground/decode` defines the interface `decode(raw_bytes) -> dict[str, float | int | str]`.

* `satnogs:<struct>` wraps a compiled Kaitai struct from the
  [`satnogs-decoders`](https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders) PyPI
  package (the same code SatNOGS DB runs), and flattens the fields the struct documents. Nothing is
  hand-ported; `pip install` brings 160+ satellites' decoders with a single `kaitaistruct` dependency.
* `pvdx` is the PVDX stub in `pvdx_ground/decode/pvdx.py`: it documents the planned USLP → Space Packet
  → fields path and raises `DecodeError` until the format is frozen.

The decode stage reads stored, not-yet-decoded frames from both sources, decodes them and writes one
InfluxDB point per frame to measurement `telemetry`: tags `norad_cat_id`, `sat_id`, `decoder`, `source`,
`observation_id`, `ground_station`, `station_name`; fields = decoded values (numbers as floats, strings as
strings) plus `frame_id`; time = frame time at nanosecond precision with the frame id as sub-second
offset, so frames received in the same second stay distinct and re-writes stay idempotent. A frame is
marked decoded only after its point is written: an InfluxDB outage (5xx, connection refused) or
credential/bucket problem (401/403/404, also caught by a startup check) leaves the batch to be retried,
while a point InfluxDB refuses because of its data (400/422, e.g. a field type conflict) is marked
`error` with the reason and does not block the rest of the batch. Frames the decoder rejects are marked
`error` too (re-run with `--redo` after fixing the decoder). The decoded fields are also kept in SQLite
(`frames.decoded_json`) and folded into `latest_values`, which only ever moves forward in frame time,
so a backfill of old observations never overwrites a newer reading. Raw frames never leave SQLite.

The Grafana dashboard (`PVDX / PVDX Telemetry (SatNOGS)`) is decoder-agnostic: NORAD id, station and
field names are dashboard variables discovered from InfluxDB, with "voltage" and "temperature" panels
pre-filtered by regex. It therefore works unchanged for PVDX. Frames pushed by our station appear under
their station name like any other.

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
4. Update `TELEMETRY_ALIASES` so the web app's names map to PVDX field names.
5. Grafana needs no change; adjust the regexes of the `voltage_fields` / `temperature_fields`
   variables if PVDX field names use other words.

The ground station push path is decoder-agnostic too: the GNU Radio side sends whatever bytes it
demodulated, and the same decoder handles frames from both sources.

## Tests

```bash
pytest
```

The suite never touches the network: `tests/fixtures/` holds two consecutive observation pages (with
their real `Link` headers), their 11 demoddata files and four real CroCube frames, recorded from the
live API on 2026-09-26, and served through `httpx.MockTransport` / FastAPI's test client. Covered:
Link-header pagination, pacing, 429/5xx/transport backoff, token fallback and host scoping, SQLite
upsert/frame/sweep/watermark semantics, the schema 1 → 2 migration, pushed-frame dedupe, latest values
that only move forward, history and listing queries, first run, idempotent re-run, resume after
`--max-pages` and after a crash, download retry, late-frame pickup, config parsing, decoder resolution,
InfluxDB point construction, every API endpoint including push validation and the token, the Redis
publisher, the decode loop's wake-up and Redis-outage behaviour, and `pvdx-push` end to end.

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
  a large "rejected by decoder" count after an image-download pass; it is not a pipeline fault.
* **Decoder validation.** Kaitai structs are permissive: garbage bytes can "decode" into nonsense
  values. The decode stage does not yet sanity-check field ranges; add checks in the PVDX decoder.
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
