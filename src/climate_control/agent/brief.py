"""Compact planning brief sent with each planning request.

The planner almost always needs the same facts, so the worker gathers them up front
instead of letting the model fetch them through several tool calls. Tools remain for
cross-checks the brief does not cover, such as another device's history.
"""

import math
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from climate_control import automation_api as rules_api
from climate_control.agent.tools import device_actions
from climate_control.db import DeviceReading, OutdoorReading

PROFILE_DAYS = 7
INDOOR_METRICS = ("pm25", "pm1", "pm10", "air_quality", "temperature", "humidity")
OUTDOOR_METRICS = ("aqi", "pm25_aqi", "pm10_aqi")


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)]


def hourly_profile(samples, zone):
    """Hour-of-day statistics in local time: {metric: {hour: [mean, p90, max, count]}}."""
    buckets = defaultdict(lambda: defaultdict(list))
    for at, metric, value in samples:
        buckets[metric][datetime.fromtimestamp(at, zone).hour].append(value)
    return {
        metric: {
            str(hour): [
                round(sum(values) / len(values), 1),
                round(percentile(values, 0.9), 1),
                round(max(values), 1),
                len(values),
            ]
            for hour, values in sorted(hours.items())
        }
        for metric, hours in buckets.items()
    }


def setting_profile(rows, zone):
    """Most frequent observed setting per local hour, e.g. "on/manual/3" or "off"."""
    counts = defaultdict(lambda: defaultdict(int))
    for row in rows:
        if row.power is None:
            continue
        setting = "off" if row.power == "off" else f"on/{row.mode}/{row.fan_level}"
        counts[datetime.fromtimestamp(row.recorded_at, zone).hour][setting] += 1
    return {str(hour): max(values, key=values.get) for hour, values in sorted(counts.items())}


def compact_event(event):
    decision = event.get("decision") or {}
    keys = ("created_at", "outcome", "inhibition", "planning", "intent", "actor")
    return {
        **{key: event[key] for key in keys if event.get(key) is not None},
        **({"rule_id": decision.get("rule_id")} if decision else {}),
    }


def planning_brief(ctx, device_id: str, job: dict) -> dict:
    context = rules_api.context_view(ctx, device_id, history_limit=1)
    zone = ZoneInfo(context["timezone"])
    since = time.time() - PROFILE_DAYS * 86400
    target = next(d for d in context["devices"] if d["id"] == device_id)
    with ctx.sessions() as db:
        rows = list(
            db.scalars(
                select(DeviceReading).where(
                    DeviceReading.device_id == device_id, DeviceReading.recorded_at >= since
                )
            )
        )
        outdoor = []
        if context["outdoor_station_id"]:
            outdoor = list(
                db.scalars(
                    select(OutdoorReading).where(
                        OutdoorReading.location_id == context["outdoor_station_id"],
                        OutdoorReading.recorded_at >= since,
                    )
                )
            )
    indoor = [
        (row.recorded_at, metric, getattr(row, metric))
        for row in rows
        for metric in INDOOR_METRICS
        if getattr(row, metric) is not None
    ]
    station = context["outdoor"]
    return {
        "target": {
            "id": device_id,
            "name": target["name"],
            "available": target["available"],
            "state": target["state"],
            "allowed_actions": device_actions(target),
        },
        "automation": {
            "owner": context["owner"],
            "enabled": context["enabled"],
            "timezone": context["timezone"],
            "active_ruleset": context["ruleset"],
            "latest_decision": context["summary"],
        },
        "planning_reasons": job.get("reasons") or [],
        "current_inputs": context["inputs"],
        "other_devices": [
            {
                "id": d["id"],
                "name": d["name"],
                "available": d["available"],
                "metrics": device_actions(d)["metrics"],
                "readings": {m: d["state"].get(m) for m in device_actions(d)["metrics"]},
            }
            for d in context["devices"]
            if d["id"] != device_id
        ],
        "outdoor": {
            "configured": station["configured"],
            "station": (station["station"] or {}).get("name"),
            "metrics": station["metrics"],
        },
        "units": context["units"],
        "profiles": {
            "window_days": PROFILE_DAYS,
            "format": "local hour -> [mean, p90, max, samples]",
            "target": hourly_profile(indoor, zone),
            "target_setting": setting_profile(rows, zone),
            "outdoor": hourly_profile(
                [
                    (r.recorded_at, r.metric, r.value)
                    for r in outdoor
                    if r.metric in OUTDOOR_METRICS
                ],
                zone,
            ),
        },
        "recent_events": [
            compact_event(e) for e in rules_api.events_view(ctx, device_id, limit=10)
        ],
    }
