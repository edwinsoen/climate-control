# Device automation

The deterministic engine runs after collection on every local scheduler or
authenticated `/internal/poll` tick. It evaluates cached data and dispatches
through the poller's account leases, spacing, backoff, and immediate readback.
It does not invoke an AI model. An external planning worker uses the authenticated
agent interface below.

Migration `0009` adds disabled automation records for existing devices, immutable
ruleset revisions, interpreter state, events, and durable planning inboxes. Apply
migrations before starting the updated service. New devices also start disabled.

## User controls and ownership

Select **Edit rules** on a device to open its dedicated rules page. Add named,
ordered rules using **When**, **If**, and **Then** for schedules, readings, and
device settings. Rule and review-condition IDs are generated automatically and
kept out of the form. Arrow icons beside the rule name change priority; the trash
icon removes a rule. Select the schedule/conditions/setting summary to expand or
collapse a rule.
**Preview rules** validates the draft and explains the current match without provider
requests. Save a valid ruleset, then **Enable rules**; saving alone does not enable
a disabled device. The page protects unsaved edits when leaving and preserves
drafts during background status refreshes. Timing, agent review conditions, and
JSON import/export are available in expandable sections.

Each device has one owner: **Manual** or **Agent-managed**. Agent rules are read-only
until **Switch to manual** is selected under **Management**. Switching to agent management retains
the existing rules and queues planning. Without existing rules, settings are held
until a valid proposal arrives. Ownership changes invalidate outstanding proposals.

Direct app power/fan controls pause enabled automation for one hour by default.
Choose another duration in the editor, or select **Pause** / **Resume**. The
pause persists before dispatch and remains even if the manual command fails.
Changes outside this app cannot reliably be distinguished from provider state
reconciliation and do not automatically pause automation.

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

To retain a fan setting between two thresholds, use separate rules and a hold
fallback. For example, set fan 4 at PM2.5 **at least 30**, set fan 2 **below 20**,
and choose **Keep current setting** when no rule matches. At 29, the device keeps
its existing setting; after reaching fan 4, it stays there until a below-20
transition passes the configured reading persistence and timing checks. The lower
bound includes 30; the upper bound excludes 20.

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

This illustrates syntax, not a recommended pollution policy. Replace `DEVICE_ID`,
thresholds, supported settings, timezone, and timings using actual capabilities,
household preferences, and observations. Outdoor data remains optional.

```json
{
  "version": 1,
  "timezone": "Asia/Singapore",
  "consecutive_readings": 2,
  "min_change_seconds": 120,
  "review_after_seconds": 86400,
  "reconsideration_cooldown_seconds": 1800,
  "rules": [
    {
      "id": "quiet-hours",
      "explanation": "Use Sleep during the household quiet period.",
      "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "23:00", "end": "07:00"}],
      "action": {"power": "on", "fan": "sleep"}
    },
    {
      "id": "outdoor-assisted",
      "explanation": "Boost when indoor particles and outdoor AQI are elevated.",
      "conditions": [
        {"source": {"kind": "device", "device_id": "DEVICE_ID", "metric": "pm25"}, "lower": 15},
        {"source": {"kind": "outdoor", "metric": "aqi"}, "lower": 100}
      ],
      "action": {"power": "on", "fan": 3}
    },
    {
      "id": "indoor-only",
      "explanation": "Continue cleaning when outdoor data is unavailable.",
      "conditions": [
        {"source": {"kind": "device", "device_id": "DEVICE_ID", "metric": "pm25"}, "lower": 15}
      ],
      "action": {"power": "on", "fan": 2}
    }
  ],
  "fallback": {"power": "hold"},
  "triggers": [
    {"id": "outdoor-rise", "kind": "above", "source": {"kind": "outdoor", "metric": "aqi"}, "threshold": 150, "reset": 100},
    {"id": "outdoor-recovery", "kind": "below", "source": {"kind": "outdoor", "metric": "aqi"}, "threshold": 50, "reset": 75},
    {"id": "stalled", "kind": "not_improving", "source": {"kind": "device", "device_id": "DEVICE_ID", "metric": "pm25"}, "threshold": 20, "readings": 5, "window_seconds": 1800, "improvement": 3},
    {"id": "outdoor-availability", "kind": "availability", "source": {"kind": "outdoor", "metric": "aqi"}},
    {"id": "command-failures", "kind": "failures", "readings": 3}
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
