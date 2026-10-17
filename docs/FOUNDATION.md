# Service foundation

## Architecture

Local `serve` hosts the compiled React UI and FastAPI routes and runs an in-process scheduler. On GCP, Firebase Hosting serves the UI, Cloud Run `serve-api` handles authentication, CRUD, and explicit controls, Cloud Scheduler calls its `POST /internal/poll` every minute to collect readings, evaluate rules, and prune expired data, and an on-demand `planner` Job runs the [rule planner](AGENT.md). Both modes use the same poller and provider adapter. SQLite supports a single local process; PostgreSQL provides shared state on GCP. Alembic owns schema changes. [Deployment details](DEPLOYMENT.md).

Normal UI status and history requests read the database. The poller owns provider requests, persistent account leases, pacing, backoff, and snapshot updates. Explicit account actions and device controls use the same protection. The VeSync and Xiaomi Cloud adapters normalize supported purifier readings and capabilities; provider-specific connection fields live in `frontend/src/accounts/providers/`.

## API

All `/api` routes require a session except `GET /api/session` and `POST /api/login`. Mutating routes also require `X-CSRF-Token`; `GET /health/live` and `/health/ready` are public. `/mcp` accepts only scoped bearer tokens and ignores cookies.

| Route | Purpose |
| --- | --- |
| `GET /api/session`, `POST /api/login`, `POST /api/logout` | Session, local login, logout |
| `GET /auth/google/login`, `GET /auth/google/callback` | Google OAuth browser flow |
| `GET /api/accounts`, `POST /api/accounts`, `PUT/DELETE /api/accounts/{id}` | Connections and credentials |
| `POST /api/accounts/{id}/refresh` | Explicit refresh using account cooldown |
| `GET /api/devices`, `POST /api/devices/{id}/status`, `POST /api/devices/{id}/control` | Cached devices, explicit status, manual controls |
| `GET/PUT /api/display-settings`, `PUT /api/devices/{id}/display-settings` | Global WHO warning threshold and per-device display mode/override |
| `GET /api/devices/{id}/history` | Bucketed or raw paired observations |
| `GET /api/outdoor`, `GET /api/outdoor/history` | Cached station, readings, and AQI history |
| `POST /api/outdoor/stations/search`, `POST /api/outdoor/stations/nearby` | AQICN search and browser-location lookup |
| `PUT/DELETE /api/outdoor/location` | Select or remove the household station |
| `/api/devices/{id}/automation/*` | [Rules, decisions, and planning](AUTOMATION.md) |
| `GET/POST /api/access-tokens`, `DELETE /api/access-tokens/{id}` | MCP access tokens; a new token is returned once |
| `GET /api/agent/status` | Planner provider, mode, and MCP URL |
| `POST /mcp` | [Agent tools over MCP](AGENT.md#mcp) |

`POST /internal/poll` runs one tick for an OIDC-authenticated scheduler and returns 404 unless `SCHEDULER_SERVICE_ACCOUNT` and `SCHEDULER_AUDIENCE` are set. Scheduled ticks poll accounts and stations due within five seconds so scheduler jitter skips no cycle; manual refresh and rate-limit backoff stay strict. `serve-api` runs no background polling. Account writes require `provider` (`vesync` or `xiaomi`), `label`, `email`, `password`, `country_code` (or Xiaomi Cloud server `SG`/`DE`/`US`/`IN`/`RU`/`TW`), and `time_zone`. Account uniqueness is scoped by `(provider, email, country_code)`. Reconnection keeps the provider and email; invalid credentials leave a correctable `auth_error`. Device responses include supported capabilities, controls, cached state, timestamps, staleness, and safe error messages. Unsupported measurements are null. Refresh cooldown returns `429` with `Retry-After`; authentication and request validation use `401`/`403`, conflicts `409`, and invalid input `422`. Credential fields are redacted from errors.

## Polling and controls

Each connected account polls every 60 seconds by default. Requests start at least one second apart; cycles cannot overlap. A five-minute tokenized account lease and four-minute poll timeout fence results across processes. Only its current holder may save a result. Successful complete discovery removes missing devices; incomplete polls retain previous snapshots. Offline or malformed reads preserve known sensor values and timestamps, marking connectivity or error separately. Expired provider tokens trigger at most one reauthentication per cycle. Rate limits pause the account for 5, 10, 20, then 30 minutes or a longer `Retry-After`; transient failures back off up to 15 minutes. Invalid credentials require reconnection. Logs use fixed outcomes and omit credentials and upstream request details.

Manual controls use the account lease and at least five seconds between commands, waiting up to 30 seconds for a busy account. They read the target immediately afterward. The response gives observed state and `control_confirmed`; an unconfirmed optimistic setting expires after 90 seconds while ordinary polls reconcile it. Failed or malformed readbacks never replace cached state or timestamps. Selected manual fan level and reported running speed are separate fields (Xiaomi models expose native favorite levels `1..14` or quantize stepless motor RPM models to `1..10`). Controls for unavailable modes or speeds are disabled by discovered capability. Power, mode, and fan controls pause device automation as described in [automation](AUTOMATION.md). Display controls (`on`/`off` on binary VeSync displays or `bright`/`dim`/`off` on 3-state Xiaomi displays, plus `manual` vs `warnings_only` or `warnings_only_bright`/`warnings_only_dim` display modes on purifiers with air quality sensors) skip provider calls when the physical display is already in the target state and do not pause automation rules; warning display modes evaluate incoming online readings against the selected WHO threshold (`who_2021_24h` by default, or `who_2021_annual`, `who_2005_24h`, `who_2005_annual`, with optional per-device override) and toggle the display to `on`, `bright`, or `dim` when exceeded (and `off` when safe) while the purifier is on.

## Indoor history

Fresh, valid online poll and status/control readbacks record available PM2.5, PM1, PM10, qualitative air quality, indoor temperature (°C), relative humidity (%), and observed power/mode/selected/running fan settings. Settings are recorded even without sensor values or while off. Unconfirmed requested states, failed/offline reads, and old snapshots create no new observations. Provider app history is not imported. Settings predating migration `0008` remain unknown.

`GET /api/devices/{id}/history?hours=24|168|720` reads only the database. Metric series have about 360 buckets; PM buckets provide mean/min/max/count and qualitative buckets the worst rank. The `settings` series preserves exact transitions, merges identical consecutive settings only within the freshness window (at least 180 seconds), and leaves missing periods blank. `raw=true` returns exact paired observations for planning: `limit=1..5000` (default 1000), and a non-null `next_after` becomes the next `after` cursor. Retrieval timestamps are not sensor or command times. History lasts 30 days and is removed with its account or device. Sensorless devices can still show setting history.

## Outdoor air quality

AQICN provides a household-selected station when `AQICN_API_TOKEN` is configured. Overall `aqi`, `pm25_aqi`, and `pm10_aqi` are **AQI indices**, distinct from indoor µg/m³ readings. `POST /api/outdoor/stations/search` accepts a 2–120 character query and returns up to 20 stations, excluding duplicates, missing AQI, and updates older than seven calendar days. `POST /api/outdoor/stations/nearby` uses browser coordinates. Both share the collector's durable lease, pacing, and backoff.

`PUT /api/outdoor/location` selects a station from a recent search. It saves search AQI as a preview but never records it as history. Combined mode fetches the full feed immediately; GCP `serve-api` leaves collection to the next tick. `DELETE` stops collection. `GET /api/outdoor` exposes cached values, source and measurement ages, attribution, staleness, and safe errors. A malformed or mismatched feed cannot overwrite valid values or timestamps. Missing pollutant fields retain their last valid value and age. The UI checks the cache every 15 seconds while visible, or every second during the initial selection load.

The worker polls a selected station every five minutes by default. A 120-second tokenized lease, 90-second request timeout, and two-second request spacing fence overlapping workers. Invalid tokens suspend collection until credentials or station change; other failures use bounded backoff and `Retry-After`. `GET /api/outdoor/history?hours=24|168|720` returns bucketed AQI mean/min/max/count from collected observations, retained for 30 days. There is no station-history backfill or weather ingestion. Changing from OpenAQ requires selecting an AQICN station again; old data remains separate until expiry. AQICN and originating agency attribution appear in the UI.

### Browser location

**Use my location** requests browser geolocation, then searches AQICN with the returned coordinates. It requires HTTPS or localhost, browser permission, and working device location services. The browser requests high accuracy, accepts a fix up to five minutes old, and waits up to 30 seconds. The UI distinguishes permission, positioning, timeout, and AQICN errors. City search works without geolocation. Local Chrome on some desktop systems may still time out when operating-system positioning is unavailable.
