# Climate Control

Private household climate monitoring and control app. Supports Levoit purifiers through VeSync, outdoor AQI through AQICN, and deterministic device rules with an agent planning interface; other providers, weather, and model invocation are planned. Read `docs/PRD.md` for scope, `docs/FOUNDATION.md` for service behavior, and `docs/AUTOMATION.md` for rules and agent integration.

## Structure

- `src/climate_control/`: FastAPI API, authentication, database, history, and polling.
- `src/climate_control/providers/`: provider protocol and VeSync adapter using pyvesync.
- `frontend/src/`: React/TypeScript UI; provider connection fields live in `accounts/providers/`.
- `migrations/`: Alembic schema changes; SQLite locally, PostgreSQL on Cloud Run.
- `tests/`: backend tests and isolated browser-test server; browser tests live in `frontend/e2e/`.
- `Dockerfile`, `compose.yaml`, `docs/DEPLOYMENT.md`: container and deployment setup.

## Working conventions

- Keep the application shell provider-neutral. Use adapter capabilities for supported controls and readings.
- UI status/history reads use cached database data. The poller owns provider requests, account leases, pacing, and backoff; preserve these across processes.
- Commands read status immediately. Optimistic overrides are bounded; malformed or failed reads must preserve cached state and timestamps.
- Keep authentication, CSRF, encryption, and secret redaction intact. Never commit `.env`, credentials, tokens, or databases. Automated tests must not contact live providers.
- Keep UI text brief and functional. Put presentation styles in CSS and select controls in frontend tests with `data-testid`. Assert behavior or semantic classes, not palette values or SVG internals.
- Add focused regression tests for meaningful failures. Avoid redundant assertions, implementation mirroring, and tests for trivial presentation edits.
- Use Alembic for schema changes. Keep README and service documentation aligned with implemented behavior; distinguish planned features.

## Commands

```sh
uv sync --frozen
uv run ruff check src tests migrations
uv run pytest
cd frontend && npm ci && npm run build && npm run test:e2e
```

Browser tests use installed Google Chrome and a fake provider. For backend development, configure `.env`, run `uv run python -m climate_control.cli migrate`, then `uv run python -m climate_control.cli serve`. The backend does not reload automatically. Vite development uses `APP_URL=http://localhost:5173`; see README for setup.

Project license: AGPL-3.0-or-later. Include `LICENSE` in packaged distributions.
