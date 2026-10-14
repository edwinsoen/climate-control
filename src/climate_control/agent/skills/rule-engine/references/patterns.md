# Rule patterns

These show structure, not recommended thresholds. Take thresholds, settings,
and hours from the existing rules, household evidence, and device capabilities.

## Hysteresis

Put the high-threshold action first, the low-threshold action second, and
`hold` as the fallback. Between thresholds, the device keeps its setting.

```json
"rules": [
  {"id": "high", "explanation": "Clean faster when PM2.5 is high.",
   "conditions": [{"source": {"kind": "device", "device_id": "D", "metric": "pm25"}, "lower": 30}],
   "action": {"power": "on", "fan": 4}},
  {"id": "low", "explanation": "Run quietly once PM2.5 is low.",
   "conditions": [{"source": {"kind": "device", "device_id": "D", "metric": "pm25"}, "upper": 20}],
   "action": {"power": "on", "fan": 2}}
],
"fallback": {"power": "hold"}
```

## Quiet hours

Place the time-window rule first so it overrides reading rules during those
hours. Use a separate window per day group when weekends differ.

```json
{"id": "night", "explanation": "Sleep mode overnight.",
 "windows": [{"start": "22:00", "end": "07:00"}],
 "action": {"power": "on", "fan": "sleep"}}
```

If very poor air should still override quiet hours, put that rule before the
night rule with both a window and a condition.

## Outdoor-assisted decisions

Outdoor data can be missing or stale. Follow each outdoor branch with an
indoor-only branch so the device still responds when the station is down.

```json
"rules": [
  {"id": "outdoor-poor-indoor-rising", "explanation": "Outdoor air is poor and indoor PM2.5 is rising.",
   "conditions": [
     {"source": {"kind": "outdoor", "metric": "pm25_aqi"}, "lower": 150},
     {"source": {"kind": "device", "device_id": "D", "metric": "pm25"}, "lower": 15}],
   "action": {"power": "on", "fan": 4}},
  {"id": "indoor-high", "explanation": "Indoor PM2.5 is high.",
   "conditions": [{"source": {"kind": "device", "device_id": "D", "metric": "pm25"}, "lower": 30}],
   "action": {"power": "on", "fan": 4}}
]
```

## Explicit data loss

When the target's own reading is unavailable, choose deliberately instead of
relying on the fallback.

```json
{"id": "no-sensor", "explanation": "Hold a moderate setting without indoor readings.",
 "conditions": [{"source": {"kind": "device", "device_id": "D", "metric": "pm25"}, "test": "unavailable"}],
 "action": {"power": "on", "fan": "auto"}}
```

An offline, stale, or errored target inhibits all commands regardless of rules.

## Review triggers

```json
"triggers": [
  {"id": "pm-spike", "kind": "above", "source": {"kind": "device", "device_id": "D", "metric": "pm25"}, "threshold": 50, "reset": 30},
  {"id": "not-clearing", "kind": "not_improving", "source": {"kind": "device", "device_id": "D", "metric": "pm25"},
   "threshold": 25, "readings": 6, "window_seconds": 3600, "improvement": 5},
  {"id": "commands-failing", "kind": "failures", "readings": 3}
]
```
