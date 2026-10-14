# Climate Control

Private household app for Levoit purifiers through VeSync, optional outdoor AQI from AQICN, and per-device rules. The React UI shows cached status and history; a poller collects readings and runs rules. Rules can be edited in the UI or maintained by a built-in ADK agent for devices marked agent-managed, and external agents can use the same tools over MCP. Weather and other providers are planned. See the [product scope](docs/PRD.md), [service behavior](docs/FOUNDATION.md), [automation contract](docs/AUTOMATION.md), and [agents](docs/AGENT.md).

## Run locally

Docker Compose serves the UI and API at [localhost:8080](http://localhost:8080) and stores SQLite data in a named volume.

```sh
cp .env.example .env
docker build -t climate-control:dev .
docker run --rm climate-control:dev python -m climate_control.cli key
docker run --rm -it climate-control:dev python -m climate_control.cli hash-password
```

Put the generated `ENCRYPTION_KEY` and `ADMIN_PASSWORD_HASH` in `.env` (single-quote the hash). The administrator password must have at least 12 characters. Then run:

```sh
docker compose up --build -d
```

Sign in and connect an account already paired with purifiers in the VeSync app. Select its country and time zone. Keep the encryption key with database backups; stored credentials cannot be decrypted without it. Compose runs migrations automatically.

## Deploy to GCP

After the one-time Terraform bootstrap and Secret Manager setup in the [deployment guide](docs/DEPLOYMENT.md), deploy or update everything with:

```sh
invoke deploy
```

The command builds the API and UI, runs migrations, publishes Firebase Hosting, and enables the minute worker and five-minute planner schedules. The planner uses Gemini on Agent Platform by default; `invoke deploy-agent` moves it to Agent Runtime. Use `uv run invoke deploy` if Invoke is not installed globally, or `invoke deploy --dry-run` to preview the target and secret versions. The deployment uses an external PostgreSQL URI.

## Develop and verify

Requires Python 3.12, uv, and Node 24. Configure `.env` as above, then:

```sh
uv sync --frozen
cd frontend && npm ci && npm run build && cd ..
uv run python -m climate_control.cli migrate
uv run python -m climate_control.cli serve
```

The backend does not reload automatically. Both servers read ports from the root `.env`: the backend listens on `PORT` (default 8080), and `npm run dev` in `frontend/` serves on `FRONTEND_PORT` (default 5173) and proxies `/api`, `/auth`, and `/mcp` to `PORT`. For Vite development, set `APP_URL=http://localhost:FRONTEND_PORT` before starting the backend. If using Google login, register the Vite origin and callback for that URL.

```sh
uv run ruff check src tests migrations
uv run pytest
cd frontend && npm run build && npm run test:e2e
```

Tests use a fake provider and installed Google Chrome; they do not contact live providers. Set `TEST_POSTGRES_URL` to also run database tests against PostgreSQL.

## Optional services

For outdoor AQI, get an [AQICN token](https://aqicn.org/data-platform/token/) and set `AQICN_API_TOKEN` in `.env` or Secret Manager. Restart or redeploy after changing it. Search for a station in the UI, or use browser location over HTTPS or localhost. AQICN values are AQI indices, separate from indoor concentrations; history starts with newly collected readings and retains 30 days. [Outdoor behavior and API](docs/FOUNDATION.md#outdoor-air-quality).

Local administrator login uses `ADMIN_USERNAME` and `ADMIN_PASSWORD_HASH`. Google login needs `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_ALLOWED_EMAILS`. Register `APP_URL` as the OAuth JavaScript origin and `APP_URL/auth/google/callback` as the redirect URI. Approved users share one household; there is no public registration. [Deployment and OAuth details](docs/DEPLOYMENT.md#authentication-and-operations).

For the rule planner, set `OPENROUTER_API_KEY` locally, or rely on Gemini through Agent Platform on GCP. Mark a device *Agent-managed* in its rule editor. To connect an external agent, create an access token under **Accounts → Agent access** and add `APP_URL/mcp` as an HTTP MCP server. [Agent setup](docs/AGENT.md).

Licensed under [AGPL-3.0-or-later](LICENSE).
