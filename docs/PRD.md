# Product scope

Climate Control helps a private household monitor air quality and control supported Levoit purifiers. It should make readings, device capabilities, automation decisions, and failures understandable without treating outdoor AQI as an indoor measurement.

## Shipped

- Connect VeSync accounts, discover purifiers, and control supported power and fan settings. Show fresh, stale, unavailable, and failed states without inventing measurements.
- Record up to 30 days of paired purifier readings and observed settings. Optionally select an AQICN station and collect outdoor AQI and pollutant indices; station history begins when selected.
- Edit per-device rules for schedules, readings, missing inputs, and actions. The deterministic worker evaluates rules independently of browser traffic. Manual controls pause automation; capability checks, pacing, persistence, and cooldowns guard changes.
- Choose manual or agent ownership. An authenticated planning worker can inspect context and propose versioned rules; the app validates proposals and retains the last valid ruleset when planning fails. No model is invoked by the app.
- Run locally with SQLite and a combined service, or deploy the UI to Firebase Hosting and the API and minute worker to Cloud Run using an external PostgreSQL database.

## Planned

Hosted model invocation and cost controls, weather and trend inputs, additional device and environmental providers, and richer household preferences. There is no promise of support for every purifier model, closed-loop control without an indoor sensor, or medical-grade guidance.

## Product requirements

1. Users can connect an account, inspect supported devices and available readings, choose an optional outdoor station, and see the source and age of every reading.
2. Users can preview, save, enable, pause, and resume rules. Decisions explain their matched rule, inputs, and outcome; manual controls remain available.
3. Device commands respect supported capabilities and provider limits. Failed or uncertain reads preserve the last valid observed state; missing data follows explicit rule branches or a hold fallback.
4. Credentials, sessions, and household data remain private. Provider secrets stay on the backend, and scheduled control continues without an open browser.
5. Planning proposals pass strict schema and revision checks before activation. Invalid or obsolete proposals leave existing rules active.

See [automation](AUTOMATION.md) for the rule and planning contract, [service behavior](FOUNDATION.md) for data handling, and [deployment](DEPLOYMENT.md) for setup.
