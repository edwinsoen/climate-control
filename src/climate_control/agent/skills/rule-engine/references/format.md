# Ruleset format, version 1

`get_rule_schema` returns the authoritative JSON Schema. Unknown fields,
non-finite numbers, duplicate IDs, invalid time zones or ranges, and unsupported
controls are rejected. Types are strict: `1` is not `true`, `"3"` is not `3`.

```json
{
  "version": 1,
  "timezone": "Asia/Singapore",
  "rules": [Rule],
  "fallback": Action,
  "consecutive_readings": 2,
  "min_change_seconds": 120,
  "review_after_seconds": 86400,
  "reconsideration_cooldown_seconds": 1800,
  "triggers": [Trigger]
}
```

| Field | Range | Meaning |
| --- | --- | --- |
| `timezone` | IANA name | Time zone for windows |
| `rules` | ≤ 64 | Ordered; first match wins |
| `fallback` | Action | Applies when no rule matches |
| `consecutive_readings` | 1–20 | Distinct observation sets that must agree before a reading-based change |
| `min_change_seconds` | 5–86400 | Minimum time between completed changes |
| `review_after_seconds` | 1800–2592000 | Periodic planner review interval |
| `reconsideration_cooldown_seconds` | 60–86400 | Minimum gap between automatic review requests |
| `triggers` | ≤ 32 | Conditions that request a planner review |

## Rule

```json
{
  "id": "particles-high",
  "explanation": "Increase cleaning when indoor PM2.5 is high.",
  "windows": [{"days": [0, 1, 2, 3, 4], "start": "22:00", "end": "07:00"}],
  "conditions": [Condition],
  "action": Action
}
```

- `id`: 1–80 characters, unique among rules.
- `explanation`: 1–500 characters, shown to the household.
- `windows` (≤ 14): alternatives; omit for all times. Days are Monday `0`
  through Sunday `6`. `start` is inclusive, `end` exclusive, both `HH:MM`.
  Overnight windows belong to their starting weekday. Equal start and end mean
  the whole day. Repeated DST hours match twice; skipped hours never match.
- `conditions` (≤ 12): all must match. Omit for an unconditional rule.

## Condition

```json
{"source": Source, "test": "range", "lower": 30, "upper": 60}
```

- `test`: `range` (default), `available`, or `unavailable`.
- `range` needs `lower` (inclusive), `upper` (exclusive), or both, each ≥ 0 and
  `lower < upper`. `available`/`unavailable` take no bounds.

## Source

- Indoor: `{"kind": "device", "device_id": "<id>", "metric": "pm25" | "pm1" | "pm10" | "air_quality" | "temperature" | "humidity"}`
- Outdoor (selected station): `{"kind": "outdoor", "metric": "aqi" | "pm25_aqi" | "pm10_aqi"}`

Any device may be a source, not only the target. Referenced devices and metrics
must exist when saving; if one is removed later, it becomes unavailable.
Device freshness uses retrieval time. Outdoor freshness uses measurement time.

## Action

- `{"power": "hold"}`: keep the current setting.
- `{"power": "off"}`
- `{"power": "on", "fan": 3}`: `fan` is required with `on` and must be an
  allowed level, `"auto"`, or `"sleep"` for the target.

On-plus-fan actions confirm power first, then set the fan. Auto mode is
confirmed by configured mode, not by the variable running speed.

## Trigger

| `kind` | Fields | Fires when |
| --- | --- | --- |
| `above` | `source`, `threshold`, `reset` < `threshold` | Value enters or is first seen at/above threshold; rearms below `reset` |
| `below` | `source`, `threshold`, `reset` > `threshold` | Value enters or is first seen below threshold; rearms above `reset` |
| `sustained` | `source`, `threshold`, `readings` (2–100), `window_seconds` | The latest `readings` distinct valid readings within the window are all at/above threshold |
| `not_improving` | `sustained` fields plus `improvement` > 0 | Sustained, first-minus-last is below `improvement`, and the target stayed on with unchanged settings and no pending command |
| `availability` | `source` | The source is lost or recovers |
| `failures` | `readings` (2–100), no `source` | That many consecutive failed or unconfirmed commands |

Availability loss resets threshold state. Missing samples or freshness gaps
break continuity. Conditions latch until cleared, so a continuing condition
requests one review, not many.
