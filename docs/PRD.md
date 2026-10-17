# Product scope

Climate Control helps a private household monitor air quality and indoor environment readings and control supported Levoit and Xiaomi (international) purifiers. It should make readings, device capabilities, automation decisions, and failures understandable without treating outdoor AQI as an indoor measurement.

## Shipped

- Connect VeSync and Xiaomi Home (international cloud) accounts, discover purifiers, and control supported power, fan, and display settings. Show fresh, stale, unavailable, and failed states without inventing measurements.
- Record up to 30 days of paired purifier readings (PM2.5, PM1, PM10, qualitative air quality, indoor temperature, and relative humidity) and observed settings. Optionally select an AQICN station and collect outdoor AQI and pollutant indices; station history begins when selected.
- Edit per-device rules for schedules, readings, missing inputs, and actions. The deterministic worker evaluates rules independently of browser traffic. Manual controls pause automation; capability checks, pacing, persistence, and cooldowns guard changes.
- Choose manual or agent ownership. A built-in ADK planner using Gemini on Agent Platform or OpenRouter maintains agent-managed devices' rules, running in-app or on Agent Runtime. External agents use the same device, outdoor, and rule tools over MCP with scoped tokens. The app validates proposals and retains the last valid ruleset when planning fails.
- Run locally with SQLite and a combined service, or deploy the UI to Firebase Hosting and the API and minute worker to Cloud Run using an external PostgreSQL database.

## Planned

Planner optimization settings and cost controls, weather and trend inputs, additional device and environmental providers, and richer household preferences. There is no promise of support for every purifier model, closed-loop control without an indoor sensor, or medical-grade guidance.

## Product requirements

1. Users can connect an account, inspect supported devices and available readings, choose an optional outdoor station, and see the source and age of every reading.
2. Users can preview, save, enable, pause, and resume rules. Decisions explain their matched rule, inputs, and outcome; manual controls remain available.
3. Device commands respect supported capabilities and provider limits. Failed or uncertain reads preserve the last valid observed state; missing data follows explicit rule branches or a hold fallback.
4. Credentials, sessions, and household data remain private. Provider secrets stay on the backend, and scheduled control continues without an open browser.
5. Planning proposals pass strict schema and revision checks before activation. Invalid or obsolete proposals leave existing rules active.
6. Agents act only through scoped tools. Model context never contains provider credentials or planning leases, and agent changes are attributed in revisions and activity.

See [automation](AUTOMATION.md) for the rule and planning contract, [agents](AGENT.md) for the planner and MCP, [service behavior](FOUNDATION.md) for data handling, and [deployment](DEPLOYMENT.md) for setup.
