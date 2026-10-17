"""Display mode configuration and WHO air quality warning threshold evaluation."""

import math
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError

from climate_control.db import Device, DisplayConfiguration
from climate_control.history import QUALITY_LEVELS

DEFAULT_WARNING_THRESHOLD = "who_2021_24h"
WarningThresholdId = Literal[
    "who_2021_annual",
    "who_2021_24h",
    "who_2005_annual",
    "who_2005_24h",
]
DisplayMode = Literal[
    "manual",
    "warnings_only",
    "warnings_only_bright",
    "warnings_only_dim",
]
DisplaySelection = Literal[
    "on",
    "off",
    "bright",
    "dim",
    "manual",
    "warnings_only",
    "warnings_only_bright",
    "warnings_only_dim",
]
WARNING_DISPLAY_MODES: dict[str, str] = {
    "warnings_only": "on",
    "warnings_only_bright": "bright",
    "warnings_only_dim": "dim",
}
PHYSICAL_DISPLAY_STATES = {"on", "off", "bright", "dim"}

WARNING_THRESHOLDS: dict[str, dict[str, Any]] = {
    "who_2021_annual": {
        "id": "who_2021_annual",
        "label": "WHO 2021 Annual",
        "pm25": 5.0,
        "pm10": 15.0,
    },
    "who_2021_24h": {
        "id": "who_2021_24h",
        "label": "WHO 2021 24-hour",
        "pm25": 15.0,
        "pm10": 45.0,
    },
    "who_2005_annual": {
        "id": "who_2005_annual",
        "label": "WHO 2005 Annual",
        "pm25": 10.0,
        "pm10": 20.0,
    },
    "who_2005_24h": {
        "id": "who_2005_24h",
        "label": "WHO 2005 24-hour",
        "pm25": 25.0,
        "pm10": 50.0,
    },
}


def global_warning_threshold(db) -> str:
    row = db.get(DisplayConfiguration, 1)
    if row and row.warning_threshold in WARNING_THRESHOLDS:
        return row.warning_threshold
    return DEFAULT_WARNING_THRESHOLD


def set_global_warning_threshold(db, warning_threshold: WarningThresholdId) -> str:
    row = db.get(DisplayConfiguration, 1)
    if row is None:
        db.add(DisplayConfiguration(id=1, warning_threshold=warning_threshold))
        try:
            db.commit()
            return warning_threshold
        except IntegrityError:
            db.rollback()
            row = db.get(DisplayConfiguration, 1)
    row.warning_threshold = warning_threshold
    db.commit()
    return warning_threshold


def display_settings_view(db) -> dict[str, Any]:
    return {
        "warning_threshold": global_warning_threshold(db),
        "thresholds": list(WARNING_THRESHOLDS.values()),
    }


def device_display_view(device: Device, global_threshold: str) -> dict[str, Any]:
    override = device.warning_threshold
    mode = device.display_mode if device.display_mode in WARNING_DISPLAY_MODES else "manual"
    return {
        "state": (device.state or {}).get("display"),
        "mode": mode if (device.controls or {}).get("display") else None,
        "warning_threshold": override,
        "effective_warning_threshold": override or global_threshold,
    }


def should_display_warning(
    capabilities: list[str] | tuple[str, ...],
    state: dict[str, Any],
    threshold_id: str | None,
) -> bool | None:
    """Return True/False if a pollutant reading evaluates against the threshold, else None."""
    preset = WARNING_THRESHOLDS.get(
        threshold_id or DEFAULT_WARNING_THRESHOLD,
        WARNING_THRESHOLDS[DEFAULT_WARNING_THRESHOLD],
    )
    pm_metrics = [metric for metric in ("pm25", "pm10") if metric in capabilities]
    if pm_metrics:
        evaluated = []
        for metric in pm_metrics:
            raw = state.get(metric)
            if type(raw) in {int, float} and math.isfinite(raw) and 0 <= raw <= 10000:
                evaluated.append(float(raw) > preset[metric])
        if not evaluated:
            return None
        return any(evaluated)
    if "air_quality" in capabilities:
        raw = state.get("air_quality")
        if isinstance(raw, str):
            rank = QUALITY_LEVELS.get(raw.strip().lower())
            if rank is not None:
                return rank >= 3
        return None
    return None
