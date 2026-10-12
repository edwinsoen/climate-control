# Device automation

The deterministic engine runs after collection in each local scheduler cycle or GCP minute `tick`. It evaluates cached observations and uses the poller's account leases, pacing, backoff, and immediate readback. It does not invoke a model; an external planning worker may use the authenticated API below. Migration `0009` creates disabled automation for existing devices, versioned rules, events, and durable planning jobs. New devices also start disabled.

## User controls and ownership

**Edit rules** opens a per-device editor for ordered **When**, **If**, and **Then** rules. **Preview rules** validates and explains a draft without provider requests; saving does not enable a disabled device. Timing, review conditions, and JSON import/export are available in the editor.

Each device has one owner: **Manual** or **Agent-managed**. Agent rules become editable after switching to manual ownership. Switching to agent ownership keeps existing rules and queues planning; without rules, the device holds its setting until a valid proposal arrives. Ownership changes invalidate outstanding proposals.

Direct power/fan controls persist an automation pause before dispatch, even if the command fails. The default pause is one hour; users can change its duration or pause/resume explicitly. Changes made outside the app do not automatically pause rules.

## Rule format, version 1

`GET /api/devices/{id}/automation/schema` returns the authoritative JSON Schema.
Unknown fields, nonfinite numbers, duplicate IDs, invalid timezones/ranges, and
unsupported controls are rejected. There is no executable expression language.
Limits: 64 rules, 12 conditions and 14 time windows per rule, 32 review triggers.

- **First matching rule wins.** Every condition in that rule must match. Multiple
  rules express alternatives. A required fallback handles no matches.
- Time windows within a rule are alternatives; omit windows for all times. Days
  are Monday `0` through Sunday `6`. Start is inclusive, end exclusive. Overnight
  windows belong to their starting weekday; equal start/end means the whole day.
  Windows use the ruleset's IANA timezone. Repeated DST hours match twice; skipped
  hours are not evaluated. Scheduler cadence bounds transition precision.
- Device sources use an internal `device_id` and `pm25`, `pm1`, `pm10`, or
  `air_quality`. PM units are µg/m³; quality ranks are 1 excellent/very good,
  2 good, 3 moderate, 4 poor/bad. Outdoor sources use the selected station and
  `aqi`, `pm25_aqi`, or `pm10_aqi`, all in AQI units. Never compare outdoor indices
  as indoor concentrations. Device freshness uses retrieval time; outdoor
  freshness uses measurement time, with the app's existing limits.
- A `range` needs `lower` (inclusive), `upper` (exclusive), or both. Missing/stale
  readings do not match. `available` and `unavailable` tests have no bounds.
  Design assisted branches followed by indoor-only rules or explicit unavailable
  branches and a fallback. Referenced devices/metrics must exist when saving;
  later removal makes the input unavailable.
- Actions are `{"power":"hold"}`, `{"power":"off"}`, or
  `{"power":"on","fan":3}`. Fan may also be supported `"auto"` or `"sleep"`.
  Rules own quiet-hour and allowed-setting policy; there is no separate limit
  layer. Model capabilities and provider pacing are always enforced.

For hysteresis, place a high-threshold action before a low-threshold action and use a hold fallback. For example, fan 4 at PM2.5 ≥30 and fan 2 below 20 keeps the current setting between thresholds.

An offline, stale, errored, or unsupported target inhibits commands. Missing
outdoor or other-device data does not inhibit the target: rules determine the
fallback. No indoor pollution threshold or fan policy is silently invented.

Reading-based transitions require two distinct observation sets by default. At
least one available source used to select the decision must advance; trigger-only
and unexamined lower-priority sources do not count. With all relevant inputs
unavailable, fresh target observations confirm the missing-data decision. Repeated
ticks on unchanged cached observations never count; stale gaps reset the candidate.
Time-only decisions require no new observations. `consecutive_readings` controls
this persistence; `min_change_seconds` defaults to 120 seconds between completed
changes. Persistence and dwell time prevent rapid switching.

On-plus-fan actions confirm power first, then set the fan after provider spacing
permits. The fan stage does not wait another minimum-change interval. Each dispatch
rechecks ownership, revision, pause, and current rule selection. Configured mode
and fan level determine confirmation, not the variable running speed in Auto mode.

Pending commands get 90 seconds to reconcile. Uncertain work requires fresh
observed status before retrying, including after a restart. Backoff is bounded;
the same decision/stage gets at most three attempts. A new decision or ruleset
can recover an exhausted decision. Provider delivery cannot be exactly once;
durable intent and observed-state reconciliation avoid blind replay.

Recent activity records command intents, outcomes, and changes in blocked status.
Decisions that keep the current setting or match an already-applied setting update
the current status without creating activity entries.

## Example for agent authors

This shows syntax, not a recommended pollution policy. Replace the device ID, threshold, fan setting, and timezone with validated household inputs and capabilities.

```json
{
  "version": 1,
  "timezone": "Asia/Singapore",
  "rules": [
    {
      "id": "particles-high",
      "explanation": "Increase cleaning when indoor PM2.5 is high.",
      "conditions": [
        {"source": {"kind": "device", "device_id": "DEVICE_ID", "metric": "pm25"}, "lower": 30}
      ],
      "action": {"power": "on", "fan": 3}
    }
  ],
  "fallback": {"power": "hold"},
  "triggers": [
    {"id": "particles-rise", "kind": "above", "source": {"kind": "device", "device_id": "DEVICE_ID", "metric": "pm25"}, "threshold": 40, "reset": 25}
  ]
}
```

`above`/`below` fire on entering or initially discovering a breached threshold and
rearm only after their reset threshold. Availability loss resets threshold state.
`sustained` needs the latest N distinct valid readings at/above its threshold within
`window_seconds`. `not_improving` additionally requires first-minus-last value to
be below `improvement` and the target to remain on with unchanged observed settings
through that window, without a pending command. Missing samples/freshness gaps
break continuity. `availability` detects loss/recovery. `failures` detects the
configured number of consecutive failed/unconfirmed commands. Conditions latch
until cleared to prevent repeated jobs from a continuing condition.

Periodic review defaults to 24 hours and automatic review cooldown to 30 minutes.
Entry into agent mode, capability/source changes, and explicit user requests also
queue planning. User requests bypass cooldown. Manual mode never queues agent work.
Existing rules continue while planning is pending or fails.

## Authenticated planning protocol

Workers use an authenticated session and `X-CSRF-Token`, like other API clients.
This release adds no model credentials, model invocation, or new auth scheme.

1. List `/api/devices`; inspect `automation.pending_review`, then fetch
   `/api/devices/{id}/automation` for status and job availability time.
2. `POST .../automation/jobs/claim` acquires a five-minute tokenized lease. Busy,
   absent, or deferred work returns `409`. Save `request_id`, `lease_token`,
   `generation`, and `base_revision` from the response.
3. `GET .../automation/context` supplies current indoor/outdoor data, units,
   freshness, capabilities, current rules, and history links. Target raw paired
   observations include `next_after`; follow the device history URL with
   `after=next_after` for further pages. Exact outdoor observations use
   `.../automation/outdoor-history?after=ID&limit=1000`. History is limited to
   available observations from 30 days; do not invent missing samples. Inspect
   `.../automation/events` for decision evidence.
4. Generate JSON against `.../automation/schema`. `POST .../automation/preview`
   accepts the ruleset itself and validates/evaluates without executing commands.
   Design for source loss and all time windows, not just the current preview.
5. Renew long work with `POST .../automation/jobs/renew` and
   `{"request_id":"...","lease_token":"..."}`. Expired/obsolete leases return `409`.
6. Submit `POST .../automation/jobs/complete` with lease fields, `generation`,
   `base_revision`, `reason`, and `ruleset`. Valid proposals activate atomically
   only for the claimed agent-owned version. Invalid proposals return `422` and
   preserve existing rules. On `409`, discard the obsolete proposal and reread
   context before claiming new work.
7. If planning fails, send lease fields to `POST .../automation/jobs/fail`.
   The inbox becomes pending with bounded exponential backoff. Crashed workers'
   leases expire and can be reclaimed; old tokens cannot complete new work.

One durable inbox per device coalesces reasons and evidence. New reasons arriving
after a worker claims a job are carried into a follow-up request when it completes.
A manual control
invalidates a claimed planning generation and makes outstanding work claimable
again. Ownership takeover cancels agent work. No provider credentials appear in
planning context or decision evidence.

## Other API operations and observability

Paths are relative to `/api/devices/{id}/automation`; writes require CSRF:

| Method / path | Purpose or body |
| --- | --- |
| `GET /` | Ownership, enabled state, revision/generation, pause, rules, latest decision, job |
| `PUT /` | `{owner: "manual" or "agent", enabled, pause_seconds, generation, base_revision}` |
| `PUT /ruleset` | Manual-only `{ruleset, reason, generation, base_revision}` |
| `POST /pause` | `{seconds: 3600}`; range 60–86400 |
| `POST /resume` | Clear pause, preserving ownership and enabled state |
| `POST /reconsider` | Explicit request in agent mode |
| `GET /revisions?before=N` | Up to 50 immutable revisions, descending; paginate by revision |
| `GET /events?before=ID` | Up to 50 events, descending; paginate by ID |

Decision changes that require commands or indicate blocked execution record
inputs/ages, selected rule, desired action, explanation, and inhibition. Already-applied
and hold decisions update current status without recording events. Dispatch records
intent and observed outcome separately. UI shows
current reason, pause expiry, pending planning, and recent events. Scheduler results
include `automation_checked`. Events expire after 30 days; ruleset revisions remain
until device deletion. Deleting a device/account cascades its automation data.
