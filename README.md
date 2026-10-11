# Climate Control

A private household app for monitoring and controlling home climate devices. Supports Levoit air purifiers through VeSync, with multiple accounts, power and fan controls, paired air-quality and setting history, and optional outdoor AQI monitoring through AQICN.

Runs as a container locally or on Cloud Run. The React UI and FastAPI service share one origin, with Google or local administrator login and encrypted provider credentials. Outdoor weather data and rule/AI automation are planned; see the [PRD](docs/PRD.md).

Overview shows device cards and a configured outdoor station. Manage connections on Accounts, connect an account from the top bar, and sign out through the user avatar menu.

## Run locally

Requires Docker Compose. To generate configuration values without installing Python locally:

```sh
docker build -t climate-control:dev .
docker run --rm climate-control:dev python -m climate_control.cli key
docker run --rm -it climate-control:dev python -m climate_control.cli hash-password
cp .env.example .env
```

Put the generated key and administrator password hash in `.env`. Keep the hash inside single quotes. The administrator password must have at least 12 characters. Set the Google configuration too if desired.

```sh
docker compose up --build -d
```

Open [localhost:8080](http://localhost:8080), sign in, then connect your VeSync account. Choose its country and time zone in the connection form. Devices must already be paired in the VeSync app.

The named volume keeps settings and readings across container replacements. Keep the encryption key alongside your database backup: changing it prevents decryption of stored VeSync credentials.

## Development

Requires Python 3.12, uv, and Node 24.

```sh
uv sync --frozen
uv run python -m climate_control.cli key
uv run python -m climate_control.cli hash-password
cp .env.example .env
```

Fill in the key and hash, then:

```sh
cd frontend
npm ci
npm run build
cd ..
uv run python -m climate_control.cli migrate
uv run python -m climate_control.cli serve
```

For UI development, run `npm run dev` in `frontend/` and set `APP_URL=http://localhost:5173` before starting the backend. Vite proxies the API and authentication routes to port 8080. Set Google's callback URL to the same frontend origin during development.

## Device controls and history

Each supported purifier has a power switch and one fan selector: Auto, Sleep, and the model's numbered manual levels. Controls update optimistically, then read status immediately. Unconfirmed settings expire after 90 seconds and show the latest reported state. Only the device being changed disables its controls; other commands wait for the account's shared request pacing.

Collapsible **History** charts cover 24 hours, 7 days, and 30 days. Shaded bands and a coloured strip show Manual, Auto, Sleep, and Off; a dashed fan-speed step line uses the right-hand Fan axis alongside PM2.5 history. Hover or touch shows readings in the space above the plot without a popup covering the chart. Touch selections remain visible after release. Selected and reported running speeds are stored separately. Settings are recorded even without sensor readings, including while off; missing observations leave gaps. Recording uses existing polls and valid status/control readbacks; it does not import past VeSync app data or treat an unconfirmed command as an observed setting. History persists in the database, with 30-day retention.

For future rule planning, `/api/devices/{id}/history?hours=24&raw=true` returns exact timestamped observations pairing settings with available air-quality readings. It requires the same session authentication as the chart. Pages contain up to 1,000 observations by default (`limit=1..5000`); pass a non-null `next_after` as `after` to get the next page.

Existing installations must run `uv run python -m climate_control.cli migrate` through migration `0008` and restart the backend before using setting history (Compose migrates automatically). The backend does not reload code automatically. Existing air-quality history is preserved; past settings remain unknown and new setting history starts on the next valid observation. Initially the overlay is a small marker at the right edge of the chart, then grows as observations accumulate. See the [upgrade instructions](docs/DEPLOYMENT.md#purifier-setting-history-upgrade-0008).

## Outdoor air quality

Outdoor monitoring uses [AQICN / World Air Quality Index](https://aqicn.org/api/) and works without connected purifiers. Register for your own [AQICN API token](https://aqicn.org/data-platform/token/) and set it in `.env` or deployment configuration:

```dotenv
AQICN_API_TOKEN=your-api-token
OUTDOOR_POLL_INTERVAL_SECONDS=300
OUTDOOR_STALE_AFTER_SECONDS=10800
```

The token stays on the backend and is never returned to the browser or stored in the application database. Restart the backend after changing it. `OPENAQ_API_KEY` is no longer used; OpenAQ tokens cannot authenticate with AQICN.

On Overview, choose **Set location**, search for a city or station name, select an AQICN result, and **Save location**. Results show the station name, AQI, and reported update time. Search excludes stations with missing AQI or updates older than seven calendar days and shows up to 20 matches. The shared household selection persists in the database. **Change location** and **Remove location** are in the station card's **…** menu. While choosing a replacement, the current card is hidden; **Cancel** restores it, and **Save location** displays the new station. These changes take effect without restarting the service.

**Use my location** first requests the browser's location, then asks AQICN for the local station using those coordinates. It requires HTTPS or localhost, browser permission, and working device location services. A loader shows progress; permission denial, unavailable positioning, timeout, and AQICN errors have separate messages. Location lookup can use a browser fix up to five minutes old and allows 30 seconds to acquire a fix. A Chrome location timeout remains unresolved in local development; city search works independently. See [browser location behavior and the known issue](docs/FOUNDATION.md#browser-location).

Saving shows the search AQI immediately as a preview and starts a full station fetch through the collector. A loader stays visible while readings are fetched, with the dashboard checking its cache every second until the first fetch succeeds or reports an error. Search previews are not recorded as history because search timestamps may lack a timezone.

Outdoor and device cards share the same header and reading layout. The outdoor card shows source attribution below the station name and the measurement age beside each AQI value, separated by a middot. Device readings show their retrieval age beside the unit. AQICN returns indices, not µg/m³ concentrations. Outdoor indices remain separate from indoor purifier concentrations. Current reads and charts use cached database data; opening the dashboard does not contact AQICN. The collector refreshes the station every five minutes by default. Measurement age determines staleness; failures retain valid cached readings and timestamps. Persistent leases, request pacing, and backoff serialize collection and explicit searches across workers. Invalid-token failures pause collection until the token or selection changes.

Each fetched observation is recorded using the source timestamp, with duplicate timestamps corrected rather than appended. History starts collapsed; expanding it shows the AQI/pollutant selector beside the 24h, 7d, and 30d period controls. Records have 30-day retention. AQICN's public feed API has no documented station-history endpoint, so earlier readings cannot be backfilled automatically. Forecast values and its separate city-aggregate historical datasets are not stored as station observations. History starts accumulating after selection.

Migration `0007` clears the previous OpenAQ selection and search results and creates separate AQICN tables. When upgrading from OpenAQ, select an AQICN station again. Legacy OpenAQ history remains separate in the old tables and expires under normal 30-day pruning; overlapping provider station IDs cannot mix indices with concentrations.

AQICN and the originating agencies are attributed in the UI. This is a private household app; see [AQICN's data-use terms](https://aqicn.org/api/) before changing how its data is shared or deploying a public or paid service.

## Authentication

- Local login uses `ADMIN_USERNAME` and `ADMIN_PASSWORD_HASH`. Leave the hash empty to disable it when Google login is configured.
- Google login requires `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and a comma-separated `GOOGLE_ALLOWED_EMAILS` allowlist. Register a web OAuth client with the exact callback `APP_URL/auth/google/callback` and configure its consent screen/test users.
- All approved users share one household and can manage all connected accounts. There is no public registration.
- Sessions expire after 24 hours. Passwords and VeSync tokens are encrypted in the database; the encryption key belongs in environment configuration or a secret store.

## Polling

The service retrieves each account's device list and then each supported purifier's details every 60 seconds, with a minimum one-second gap between outgoing requests. The cooldown is measured between cycle starts, and cycles cannot overlap. Network delays and large accounts can make updates take longer. It reuses stored VeSync tokens and reauthenticates at most once per cycle on token expiry.

The browser checks cached status every 15 seconds while visible; refresh is automatic. Opening a page never directly contacts VeSync. Polls and commands share an account lease, and commands are spaced at least five seconds apart. Status reads and polls do not extend the command cooldown.

Rate limiting pauses that account for 5, 10, 20, then 30 minutes; a longer `Retry-After` takes precedence. Temporary failures retry with increasing delays up to 15 minutes. Invalid credentials require reconnection. Failed or malformed responses preserve previous readings and timestamps. Readings become stale after three minutes; longer configured intervals use three times that interval.

At a 60-second interval, one discovery request and one per purifier total approximately `1440 × (1 + purifier count)` requests/day, before additional model, login, and control requests. Increase `POLL_INTERVAL_SECONDS` for larger accounts or when other apps poll the same account.

## Verification

```sh
uv run ruff check src tests migrations
uv run pytest
cd frontend
npm run build
npm run test:e2e
```

Browser tests use installed Google Chrome and an isolated backend with a fake provider. To use Playwright's Chromium instead, install it with `npx playwright install chromium` and remove `channel: 'chrome'` from `playwright.config.ts`.

Frontend interactions use `data-testid`; assert behavior and semantic classes rather than exact colors, SVG details, or CSS values. Backend tests cover provider parsing, polling, persistence, controls, and authentication without live credentials.

To run the database tests against both SQLite and PostgreSQL, supply `TEST_POSTGRES_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/TEST_DATABASE` when running pytest. The test user needs permission to create schemas; every test gets a temporary schema which is dropped afterward.

Before using a real account, check that the dashboard matches the VeSync app, that model-specific readings are available, and that refresh timestamps advance without repeated login or rate-limit errors. Live VeSync and Google credentials are not used by automated tests.

See [deployment instructions](docs/DEPLOYMENT.md) and the [service overview](docs/FOUNDATION.md).

## License

Licensed under the GNU Affero General Public License, version 3 or later (`AGPL-3.0-or-later`). See [LICENSE](LICENSE).
