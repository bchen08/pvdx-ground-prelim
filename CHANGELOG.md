# Changelog

## [Unreleased] — branch `fix/audit-2026-10-04`

Fixes from a read-only audit of v0.2.0 (2026-10-04) that ran the service against live SatNOGS data and
reproduced every defect before it was fixed. Every code fix has a regression test (the docker-compose
and documentation changes are not tested); the suite grows from 80 to 151 tests. Telemetry that was
already recorded was accurate: provenance matched SatNOGS on every sampled observation and re-decoding
every stored frame gave identical values. The defects were in serving, deployment, presentation and
pipeline robustness (decode loop, shutdown); none corrupted recorded values.

### Upgrade notes (read before deploying)

- **Schema 3, automatic and one-way.** The first command that opens `data/state.db` migrates it to
  schema 3 (adds `family` and `primary_frame_id` to `frames` and groups stored frames into
  transmissions). Older code refuses a schema-3 database, so back it up first:
  `sqlite3 data/state.db ".backup state-v2.db"`. The migration does not recompute latest values: a
  field's latest value may keep naming a frame that is now a copy (same value, the copy's frame id,
  time, source and station) until a newer primary replaces it. See README → Setup.
- **Rebuild InfluxDB once.** Points written by 0.2.0 have no `primary` tag and would form separate
  series next to the new ones. Stop the service, delete the `telemetry` measurement (operations guide
  8.5), run `pvdx-decode --redo`, then start `pvdx-serve`.
- **Ports are published on 127.0.0.1 only.** Redis and InfluxDB are no longer reachable from other
  machines. The new docker-compose variables `GRAFANA_BIND` and `API_BIND` (default `127.0.0.1`) set
  the host address for Grafana's port and for the API port of the `--profile cloud` container; a
  host-run `pvdx-serve` still listens on `API_HOST`. Expose a port only behind a TLS reverse proxy and a
  network/cloud firewall: Docker-published ports bypass host firewalls such as ufw.
- **Engineering units are opt-in.** Copy the new `TELEMETRY_ALIASES` line from `.env.example`
  (`battery=psu_battery*0.001:V,temperature=obc_temp_mcu*0.01:degC,signal_rssi=uhf_act_rssi_raw*0.5-134:dBm,uptime_seconds=obc_uptime:s`).
  A plain `alias=field` list keeps serving raw values. With the new line, `telemetry:battery` holds
  volts, so the mission-control `TelemetryPanel` (which renders `battery` as a percentage with 60/30
  thresholds) needs its label and thresholds changed.
- **New alias grammar.** Existing `TELEMETRY_ALIASES` entries are parsed as
  `alias=field[*scale][(+|-)offset][:unit]`: a field name containing `*` or `:`, or ending in `+N`/`-N`,
  now fails at start-up or is read as a scale, offset or unit.
- **Stricter configuration.** Every command (`pvdx-serve`, `pvdx-ingest`, `pvdx-decode`, `pvdx-push`)
  now exits with status 2 at start-up when `REDIS_URL` is not `redis://`, `rediss://` or `unix://`,
  when `INFLUX_URL` is not `http(s)://`, when `INGEST_MIN_INTERVAL` (or `pvdx-ingest --min-interval`) is
  below 15 s (the authenticated SatNOGS rate), or when any numeric setting is `nan` or `inf`. This
  includes the `.env` that `pvdx-push` reads on the ground-station computer.
- **Stricter API input.** `/frames/{id}` and `/observations/{id}` answer 422 (was 404) for ids ≤ 0, and
  `/observations?ground_station=` answers 422 (was 200 with an empty list) for values ≤ 0. The `norad`
  query parameter and a pushed `norad_cat_id` must be 1–999 999 999. Pushed station names are stripped of
  surrounding whitespace and may not be blank. Out-of-range time parameters answer 400.
- **History is deduplicated by default.** `/telemetry/history` returns primaries only unless
  `copies=true`. With `source=groundstation`, pushed frames whose transmission has a SatNOGS primary are
  left out unless `copies=true` is added.
- **Log lines changed.** The decode summary reads `decode: N frame(s) read, N decoded (K duplicate
  reception(s)), …`; per-frame InfluxDB warnings read `not written to InfluxDB` (was `rejected by
  InfluxDB`); a decoder crash logs an ERROR line per frame (with one traceback per pass); the schema-3
  migration logs its transmission and copy counts.

### Fixed

- **API failed under concurrent requests.** Each request's SQLite connection was opened, used and
  closed on different threadpool threads, so overlapping requests raised `sqlite3.ProgrammingError`
  (HTTP 500; 2 concurrent requests failed 50–88% of the time) and leaked connections. The per-request
  connection now disables SQLite's same-thread check; it is still used by one request, one step at a
  time. 150 parallel requests against the live service all return 200.
- **Push authentication ran after the body was read.** With `INGEST_TOKEN` set, a middleware now
  rejects `POST /ingest/frames` with a missing or wrong `X-Ingest-Token` before reading the body
  (previously an 80 MB unauthenticated body was parsed first). The token is compared in constant time.
- **A decoder crash blocked all decoding.** An exception other than `DecodeError` was retried on every
  poll forever while `/health` said ok. Such a frame is now marked `error`
  (`<decoder>: unexpected <Exception>: …`) and decoding continues; a frame whose InfluxDB point cannot
  be built is handled the same way. The decode stage now normalises every decoder's output: NaN and
  `±inf` are dropped, and values that are not plain scalars (numpy integers, lists, nested structs) are
  dropped instead of being written as strings.
- **Inputs that returned 500.** Huge relative times (`since=-99999999999d`), ISO times at the datetime
  limits, integers ≥ 2⁶³ in ids, offsets and NORAD ids, and pushed `received_at` values outside the
  datetime range now answer 400 or 422.
- **`/telemetry/history` ignored aliases.** `field=battery` now reads `psu_battery`. The response adds
  `stored_field`, `unit` (the alias's unit, or null for a raw field or an alias without one) and
  `copies`.
- **SIGTERM outside Docker skipped shutdown.** `kill`, launchd or systemd now stop the workers like
  Ctrl-C does. The ingest worker checks for shutdown between pages and frame downloads and paces its
  SatNOGS requests with an interruptible wait.
- **Exposed services.** `docker-compose.yml` publishes InfluxDB, Redis and Grafana on 127.0.0.1 only
  (Redis has no password), gives all three `restart: unless-stopped` so they come back after a reboot,
  and adds a Grafana healthcheck.
- **Documentation.** CroCube temperatures are hundredths of a degree C (the docs said tenths); RSSI in
  dBm is `raw/2 − 134`; the Redis cache is the telemetry half of GS3, not its queue; the republish,
  expiry and InfluxDB idempotency descriptions now match the code; "two-line swap for PVDX" is
  scoped to single-frame beacons; the ops-guide PDF is regenerated.

### Added

- **Duplicate-reception grouping.** SatNOGS publishes most frames twice per observation (a native
  gr-satnogs file and a gr-satellites `_gN` file stamped 0–16 s early) and several stations hear the
  same transmission: 53% of stored frames were copies, each plotted as its own sample. Every frame now
  has a `family` (`native`, `grsat`, `push`), and every stored (downloaded) frame gets a
  `primary_frame_id`. Copies share identical bytes and lie within 30 s of the primary in the same
  observation, or 45 s otherwise; native and pushed copies are preferred as primaries; assignments never
  change once made. Only primaries feed the latest values. Every decoded reception is still stored and
  written to InfluxDB with a `primary` tag.
  - API: `/telemetry/history` returns primaries by default (`copies=true` for every reception; each
    point carries `primary`); `/frames` and `/frames/{id}` expose `family`, `primary` and
    `primary_frame_id`, and `/frames` accepts a `primary` filter (`false` also returns frames not yet
    assigned).
  - Grafana: a "Receptions" variable (Primary only, the default / Copies only / All) filters "Frames in
    range" (transmissions under the default), "Frames per hour", the telemetry time series and the
    latest-values table; with a station selected and Primary only, panels show only the transmissions
    that station supplied the primary for (choose All to see everything it heard). A new "Receptions in
    range" stat and "Frames by station" always count every reception.
- **Engineering units for aliases.** `TELEMETRY_ALIASES` entries accept
  `alias=field[*scale][(+|-)offset][:unit]`. Converted values appear in the alias Redis keys and in
  `/telemetry` (new `units` object) and `/telemetry/history`; units are published in `telemetry:_meta`.
  Raw fields stay unchanged in SQLite, InfluxDB, Grafana, `/telemetry/latest` and `/telemetry/fields`.
  A converting alias whose field holds a non-number is left out of Redis (its key goes stale) and served
  as null by the API; a plain alias still passes strings through. When `REDIS_URL` is set, the first
  publish for a satellite warns about aliases whose field has no value yet, and a non-numeric field is
  warned about once per alias.

### Known open issues

Confirmed but deliberately not changed, because each needs a decision from the team. Details are in
README → Known gaps unless another section is named:

- Redis TTL runs from publish time, so restarts make old values look fresh, and staleness is judged per
  satellite rather than per field (waiting for the web team).
- The web-app contract is incomplete: nothing publishes `elevation`, and the team's fake telemetry loop
  writes the same `telemetry:*` keys.
- `INGEST_TOKEN` is empty by default, request bodies have no size cap, Grafana's default admin password
  is `admin`, and Grafana uses the InfluxDB operator token (README → Running in Docker).
- A future-dated push pins the latest values; a push batch that fails part-way keeps its earlier frames.
- InfluxDB must be up for decoded fields to reach SQLite and Redis; `/health` always answers HTTP 200
  and dead worker threads are not restarted.
- Ingest abandons a frame after 5 failed runs and never re-checks an observation's status.
- `pvdx-push --watch` exits on the first failed push; `--stdin` buffers until end of input.
- BSE's own station cannot be selected in Grafana (pushed points have no `ground_station` tag).
- Frames uploaded to the SatNOGS DB by independent receivers (SiDS) are not ingested; satellite photos
  (CFDP) are not built, and PVDX's own USLP/SPP decoding is not built (README → Swapping in PVDX).
