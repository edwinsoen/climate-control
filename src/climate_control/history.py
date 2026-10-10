import math

from sqlalchemy import Integer, cast, delete, func, select

from climate_control.db import DeviceReading

RETENTION_SECONDS = 30 * 86400
METRICS = ("pm25", "pm1", "pm10", "air_quality")
QUALITY_LEVELS = {"excellent": 1, "very good": 1, "good": 2, "moderate": 3, "poor": 4, "bad": 4}


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
        values[metric] = (
            float(value)
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
            else None
        )
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


def history_view(db, device_id: str, hours: int, current: float):
    start = current - hours * 3600
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
    }
