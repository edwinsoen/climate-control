# Climate Control — Product Requirements

## Purpose

Improve home air quality and comfort by controlling connected climate devices with clear, adaptable rules. The first release controls supported Levoit air purifiers through the Python `pyvesync` library. The system is a container with a web UI and runs locally or on Google Cloud Run.

## Current implementation

The app supports VeSync account connections, purifier controls, recorded indoor history, and optional AQICN outdoor monitoring. Users select a shared household station by city/station search in the app; current readings and recorded history use the database cache. AQICN's public station-feed API does not provide a documented history endpoint, so outdoor history accumulates from new observations without automatic backfill. Browser location is also available, with an unresolved local Chrome timeout documented in [the service foundation](FOUNDATION.md#browser-location). Weather data, automation, and AI planning remain planned.

## Target first release

The user connects a VeSync account, selects purifiers, sets a home location and preferences, and enables automation. The app reads device state and available indoor measurements, fetches nearby outdoor air quality and weather data, then chooses appropriate purifier settings. Users can see why a setting was chosen and take manual control at any time.

Outdoor readings provide context; they must not be treated as measurements inside the home. Device features vary by model, so the app discovers supported controls and readings instead of assuming every purifier has a sensor or every fan mode.

## Requirements

### Setup and web UI

- Connect and validate a VeSync account; discover supported Levoit purifiers and show unsupported devices clearly.
- Configure home location, time zone, selected devices, quiet hours, preferred noise/cleaning balance, and per-device automation state.
- Show current device state, available sensor readings, outdoor data with source and age, active rules, recent actions with reasons, and connection errors.
- Allow a manual device setting or automation pause. A manual change suspends automation for that device until a visible, configurable expiry or explicit resume.
- Protect the UI and API with authentication. Keep VeSync and data-provider credentials out of logs and UI responses.

### Data and control

- Poll device state and external data on configurable intervals, respecting provider limits. Use timestamps and freshness limits; mark stale or unavailable data visibly.
- Support power, mode, and fan speed only where the model exposes them. Confirm commands against subsequent device state and retry transient failures with bounded backoff.
- Apply explicit minimum and maximum settings, quiet-hour limits, minimum time between changes, and hysteresis to prevent rapid switching.
- Use indoor measurements, when available, as the primary feedback for indoor air cleaning. Use outdoor air quality, weather, and trends to adjust targets and anticipate changes; never infer an indoor reading from outdoor data.
- When data, AI, or a device connection fails, retain the last valid safe rule set where possible. Avoid speculative changes, surface the fault, and resume when inputs recover.

### Rules and AI

- Run a deterministic control loop that evaluates the active rules and issues device commands. Every action records its inputs, rule, and reason.
- Periodically ask a configurable AI provider to propose a rule set and the input-change thresholds that should trigger early reconsideration. Also reconsider when a threshold is crossed or the user changes preferences.
- Give AI only the data needed to plan. It may propose targets, schedules, and thresholds; it cannot send device commands or bypass user limits.
- Validate proposed rules against a strict schema and safety bounds before activation. Reject invalid or contradictory proposals and keep the previous rules. Store rule versions and the reason for each update.
- Provide a usable default rule set when AI is unconfigured or unavailable. AI use and its cost controls are configurable.

## Deployment and data

- Ship one container image with documented local and Cloud Run setup. Configuration and secrets come from environment variables or a secret store; user settings and rule history persist across restarts.
- Use a local persistent volume for local deployment and an external persistent store on Cloud Run. Do not depend on the container filesystem for durable state.
- Schedule control ticks independently of UI traffic. Local deployment may use an in-process scheduler; Cloud Run uses an external scheduler calling an authenticated endpoint. Make ticks idempotent so retries or overlapping instances do not duplicate commands.
- Log decisions and failures without credentials or precise location. Provide a health endpoint and basic operational metrics.

## Acceptance criteria

1. A user can connect VeSync, select a supported purifier, set a location and preferences, and enable automation through the UI.
2. The app shows the data and active rule behind each setting change; stale or missing data is identifiable.
3. Automation respects model capabilities, user limits, quiet hours, cooldowns, and manual overrides.
4. AI can replace a valid rule set on a schedule or significant input change; invalid output or provider failure leaves deterministic control working.
5. The same image runs locally and on Cloud Run, with settings surviving restart and scheduled control continuing without UI visits.

## Later scope

Add other climate devices and ecosystems through device adapters, plus additional sensor and environmental-data providers. The first release does not promise support for every Levoit model, closed-loop control without an indoor sensor, or medical-grade air quality guidance.

## References

- [pyvesync supported devices and API](https://github.com/webdjoe/pyvesync)
- [AQICN / WAQI API and data-use terms](https://aqicn.org/api/)
- [Cloud Run service runtime and persistence](https://docs.cloud.google.com/run/docs/developing), [instance scaling](https://docs.cloud.google.com/run/docs/about-instance-autoscaling)
