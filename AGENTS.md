# Climate Control

Private household climate monitoring and control app. Supports Levoit purifiers through VeSync, outdoor AQI through AQICN, deterministic device rules, an ADK rule planner, and agent tools over MCP; other providers and weather are planned. Read `docs/PRD.md` for scope, `docs/FOUNDATION.md` for service behavior, `docs/AUTOMATION.md` for rules, and `docs/AGENT.md` for the planner and MCP.

## Structure

- `src/climate_control/`: FastAPI API, authentication, database, history, and polling.
- `src/climate_control/providers/`: provider protocol and VeSync adapter using pyvesync.
- `src/climate_control/agent/`: shared agent tools, MCP endpoint and access tokens, ADK planner and worker, and the rule-engine skill.
- `deploy/agent_runtime/`: Agent Runtime entry point staged by `scripts/deploy_agent.py`.
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
- Agent tools wrap the same context functions as the routes (`ctx` is app state or a `ToolContext`); add behavior there, not in tool adapters. The planner gets read tools only and never sees leases or credentials. Tests use scripted models, never live model APIs.
- Use Alembic for schema changes. Keep README and service documentation aligned with implemented behavior; distinguish planned features.

## Commands

```sh
uv sync --frozen
uv run ruff check src tests migrations
uv run pytest
cd frontend && npm ci && npm run build && npm run test:e2e
```

Browser tests use installed Google Chrome and a fake provider. For backend development, configure `.env`, run `uv run python -m climate_control.cli migrate`, then `uv run python -m climate_control.cli serve`. The backend does not reload automatically. Ports come from `PORT` and `FRONTEND_PORT` in the root `.env`; Vite development uses `APP_URL=http://localhost:FRONTEND_PORT`. See README for setup.

Project license: AGPL-3.0-or-later. Include `LICENSE` in packaged distributions.
