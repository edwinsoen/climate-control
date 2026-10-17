import math

from sqlalchemy import Integer, cast, delete, func, select

from climate_control.db import DeviceReading

RETENTION_SECONDS = 30 * 86400
METRICS = ("pm25", "pm1", "pm10", "air_quality", "temperature", "humidity")
QUALITY_LEVELS = {"excellent": 1, "very good": 1, "good": 2, "moderate": 3, "poor": 4, "bad": 4}
SETTINGS = ("power", "mode", "fan_level", "reported_fan_level")


def observed_settings(snapshot):
    state = snapshot.state
    power = state.get("power") if "power" in snapshot.capabilities else None
    mode = state.get("mode") if "mode" in snapshot.capabilities else None
    modes = {"manual", *snapshot.controls.get("modes", [])}
    levels = snapshot.controls.get("fan_levels", [])

    def fan_level(key):
        value = state.get(key) if "fan_level" in snapshot.capabilities else None
        # Zero may be reported while off; selected speed and running speed stay separate.
        return value if type(value) is int and value >= 0 and value in [0, *levels] else None

    return {
        "power": power if isinstance(power, str) and power in {"on", "off"} else None,
        "mode": mode if isinstance(mode, str) and mode in modes else None,
        "fan_level": fan_level("fan_level"),
        "reported_fan_level": fan_level("reported_fan_level"),
    }


def record_reading(db, device, snapshot, current):
    recorded = snapshot.retrieved_at
    if (
        not snapshot.supported
        or snapshot.error
        or recorded is None
        or not math.isfinite(recorded)
        or recorded < current - RETENTION_SECONDS
        or recorded > current + 60
        or snapshot.state.get("connectivity") != "online"
    ):
        return
    values = {}
    for metric in METRICS:
        value = snapshot.state.get(metric) if metric in snapshot.capabilities else None
        if metric == "air_quality":
            value = QUALITY_LEVELS.get(str(value).lower())
        min_val = -50 if metric == "temperature" else 0
        max_val = 100 if metric in {"temperature", "humidity"} else 10000
        values[metric] = (
            float(value)
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and min_val <= value <= max_val
            else None
        )
    values.update(observed_settings(snapshot))
    if all(value is None for value in values.values()):
        return
    db.flush()
    latest = db.scalar(
        select(func.max(DeviceReading.recorded_at)).where(DeviceReading.device_id == device.id)
    )
    if latest is None or recorded > latest:
        db.add(DeviceReading(device_id=device.id, recorded_at=recorded, **values))


def prune_readings(db, current):
    db.execute(delete(DeviceReading).where(DeviceReading.recorded_at < current - RETENTION_SECONDS))


def history_view(
    db,
    device_id: str,
    hours: int,
    current: float,
    *,
    raw=False,
    gap_seconds=180,
    after=None,
    limit=1000,
):
    start = current - hours * 3600
    query = (
        select(DeviceReading)
        .where(
            DeviceReading.device_id == device_id,
            DeviceReading.recorded_at >= start,
            DeviceReading.recorded_at <= current,
        )
        .order_by(DeviceReading.recorded_at)
    )
    if raw:
        if after is not None:
            query = query.where(DeviceReading.recorded_at > after)
        observations = list(db.scalars(query.limit(limit + 1)))
        return {
            "source": "recorded",
            "start": start,
            "end": current,
            "observations": [
                {
                    "recorded_at": row.recorded_at,
                    **{key: getattr(row, key) for key in (*METRICS, *SETTINGS)},
                }
                for row in observations[:limit]
            ],
            "next_after": observations[limit - 1].recorded_at
            if len(observations) > limit
            else None,
        }

    observations = db.execute(
        query.with_only_columns(
            DeviceReading.recorded_at, *(getattr(DeviceReading, key) for key in SETTINGS)
        ).execution_options(yield_per=1000)
    )
    settings = []
    previous = None
    for row in observations:
        values = {key: getattr(row, key) for key in SETTINGS}
        contiguous = previous is not None and row.recorded_at - previous.recorded_at <= gap_seconds
        if contiguous and settings and any(getattr(previous, key) is not None for key in SETTINGS):
            # Hold an observed setting only to the next nearby observation, never across gaps.
            settings[-1]["end_at"] = row.recorded_at
            if all(settings[-1][key] == value for key, value in values.items()):
                previous = row
                continue
        if any(value is not None for value in values.values()):
            settings.append({"recorded_at": row.recorded_at, "end_at": row.recorded_at, **values})
        previous = row

    interval = max(60, math.ceil(hours * 3600 / 360))
    # Bound chart responses to approximately 360 time buckets. Never fill missing buckets.
    bucket = cast(func.floor(DeviceReading.recorded_at / interval), Integer)
    columns = [bucket.label("bucket")]
    for metric in METRICS:
        column = getattr(DeviceReading, metric)
        value = func.max(column) if metric == "air_quality" else func.avg(column)
        columns.extend(
            [
                value.label(metric),
                func.min(column).label(f"{metric}_min"),
                func.max(column).label(f"{metric}_max"),
                func.count(column).label(f"{metric}_count"),
            ]
        )
    rows = db.execute(
        select(*columns)
        .where(
            DeviceReading.device_id == device_id,
            DeviceReading.recorded_at >= start,
            DeviceReading.recorded_at <= current,
        )
        .group_by(bucket)
        .order_by(bucket)
    ).mappings()
    metrics = {metric: [] for metric in METRICS}
    for row in rows:
        for metric in METRICS:
            if row[metric] is not None:
                metrics[metric].append(
                    {
                        "recorded_at": min(current, max(start, (row["bucket"] + 0.5) * interval)),
                        "value": round(row[metric], 2),
                        "min": row[f"{metric}_min"],
                        "max": row[f"{metric}_max"],
                        "count": row[f"{metric}_count"],
                    }
                )
    return {
        "source": "recorded",
        "start": start,
        "end": current,
        "interval_seconds": interval,
        "metrics": metrics,
        "settings": settings,
    }
