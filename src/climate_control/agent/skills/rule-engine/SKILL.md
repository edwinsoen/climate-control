---
name: rule-engine
description: Configure Climate Control device rules. Use when reviewing, writing, validating, or submitting a ruleset for an air purifier, or when deciding how a device should respond to indoor readings, outdoor AQI, time of day, or review triggers.
---

# Configuring device rules

A ruleset is deterministic JSON that the app's rule engine evaluates every
scheduler cycle. You write the policy; the engine owns execution, provider
pacing, retries, and confirmation. There is no expression language.

## Workflow

1. Read the device's automation context: the planning brief when one is
   provided, otherwise `get_device_automation`. Note the target
   `allowed_actions`, the other devices and their metrics, the outdoor station,
   the current ruleset, `generation`, and `revision`.
2. Read evidence before changing policy: the planning reasons, recent events,
   and readings (the brief's hour-of-day profiles, or `list_rule_events`,
   `get_device_history`, and `get_outdoor_history`). Do not invent missing
   samples.
3. Draft a ruleset. Load `references/format.md` for the exact fields and
   `references/patterns.md` for proven structures.
4. Validate with `preview_rules` when available. Fix every reported error. A preview evaluates
   only the current moment, so reason through every time window and through
   missing outdoor and other-device data as well.
5. Check `references/checklist.md`, then submit.

## Core semantics

- **First matching rule wins.** All conditions in a rule must match. Rules are
  alternatives; order them from most to least specific.
- **The fallback is required** and applies when no rule matches.
- **Missing or stale readings never match a range.** Use `available` or
  `unavailable` tests to branch on data loss explicitly.
- **Units differ by source.** Indoor `pm25`, `pm1`, and `pm10` are µg/m³.
  Indoor `air_quality` is a rank: 1 excellent, 2 good, 3 moderate, 4 poor.
  Indoor `temperature` is °C and `humidity` is %.
  Outdoor `aqi`, `pm25_aqi`, and `pm10_aqi` are AQI index values. Never compare
  an outdoor AQI with an indoor concentration threshold.
- **Actions**: `{"power":"hold"}` keeps the current setting, `{"power":"off"}`,
  or `{"power":"on","fan":N}` where `N` is an allowed fan level, `"auto"`, or
  `"sleep"` when the device supports those modes.
- **Persistence**: reading-based changes need `consecutive_readings` distinct
  observation sets (default 2) and `min_change_seconds` (default 120) between
  completed changes. Time-only decisions apply without new readings.
- **Ownership**: agent-managed devices accept rules only through a claimed
  planning request. Manual devices accept direct saves with the current
  `generation` and `base_revision`. A conflict means something changed: reread
  before trying again.

## Policy principles

- The active ruleset is the household's current intent. Treat every change to it
  as deliberate; never restore or copy earlier rules. With no active rules,
  design a new ruleset from the evidence.
- Use only devices, metrics, and fan settings listed in the context.
- Ground each threshold in the device's observed readings or an established
  reference (WHO air quality guidelines, US AQI breakpoints) and name the source
  in the rule `explanation`.
- When improving existing rules, keep their quiet hours and allowed settings
  unless evidence shows they fail, and prefer small, explainable changes.
- Add review triggers for conditions that should wake the planner, such as a
  sustained high reading that the current rules do not improve.
