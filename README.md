# Climate Control

A private household app for monitoring and controlling home climate devices. Currently supports Levoit air purifiers through VeSync, with multiple accounts, power and fan controls, and recorded PM2.5 history.

Runs as a container locally or on Cloud Run. The React UI and FastAPI service share one origin, with Google or local administrator login and encrypted provider credentials. Outdoor climate data and rule/AI automation are planned; see the [PRD](docs/PRD.md).

Overview shows device cards. Manage connections on Accounts, connect an account from the top bar, and sign out through the user avatar menu.

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

Collapsible PM2.5 charts cover 24 hours, 7 days, and 30 days, with readings shown on hover. Recording uses existing polls and valid control readbacks; it does not import past VeSync app data. History persists in the database, with 30-day retention.

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
