"""Bounded declarative rules. Evaluation is pure and never contacts a provider."""

from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Source(StrictModel):
    kind: Literal["device", "outdoor"]
    device_id: str | None = Field(default=None, min_length=1, max_length=36)
    metric: Literal["pm25", "pm1", "pm10", "air_quality", "aqi", "pm25_aqi", "pm10_aqi"]

    @model_validator(mode="after")
    def valid_source(self):
        metrics = (
            {"aqi", "pm25_aqi", "pm10_aqi"}
            if self.kind == "outdoor"
            else {"pm25", "pm1", "pm10", "air_quality"}
        )
        if self.metric not in metrics or (self.kind == "device") != bool(self.device_id):
            raise ValueError("Use a device ID and indoor metric, or an outdoor AQI metric.")
        return self

    def key(self):
        return f"{self.kind}:{self.device_id or 'selected'}:{self.metric}"


class Condition(StrictModel):
    source: Source
    test: Literal["range", "available", "unavailable"] = "range"
    lower: float | None = Field(default=None, ge=0)
    upper: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounds(self):
        if self.test == "range":
            if self.lower is None and self.upper is None:
                raise ValueError("A range needs at least one bound.")
            if self.lower is not None and self.upper is not None and self.lower >= self.upper:
                raise ValueError("The upper bound must exceed the lower bound.")
        elif self.lower is not None or self.upper is not None:
            raise ValueError("Availability conditions cannot have bounds.")
        return self


class Action(StrictModel):
    power: Literal["on", "off", "hold"]
    fan: int | Literal["auto", "sleep"] | None = None

    @model_validator(mode="after")
    def fan_setting(self):
        if (self.power == "on") != (self.fan is not None):
            raise ValueError("On requires a fan setting; off and hold cannot specify one.")
        if isinstance(self.fan, int) and self.fan < 1:
            raise ValueError("Fan levels must be positive.")
        return self


class Window(StrictModel):
    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(
        default_factory=lambda: list(range(7)), min_length=1, max_length=7
    )
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    def matches(self, local):
        clock = local.strftime("%H:%M")
        if self.start == self.end:
            return local.weekday() in self.days
        if self.start < self.end:
            return local.weekday() in self.days and self.start <= clock < self.end
        day = local if clock >= self.start else local - timedelta(days=1)
        return day.weekday() in self.days and (clock >= self.start or clock < self.end)


class Rule(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    explanation: str = Field(min_length=1, max_length=500)
    windows: list[Window] = Field(default_factory=list, max_length=14)
    conditions: list[Condition] = Field(default_factory=list, max_length=12)
    action: Action


class Trigger(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["above", "below", "sustained", "not_improving", "availability", "failures"]
    source: Source | None = None
    threshold: float | None = Field(default=None, ge=0)
    reset: float | None = Field(default=None, ge=0)
    readings: int = Field(default=3, ge=2, le=100)
    window_seconds: int = Field(default=1800, ge=60, le=86400)
    improvement: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def parameters(self):
        if self.kind != "failures" and self.source is None:
            raise ValueError("This trigger needs a source.")
        if self.kind in {"above", "below", "sustained", "not_improving"}:
            if self.threshold is None:
                raise ValueError("This trigger needs a threshold.")
        if self.kind in {"above", "below"}:
            if self.reset is None or (
                self.reset >= self.threshold
                if self.kind == "above"
                else self.reset <= self.threshold
            ):
                raise ValueError(
                    "Reset must be below an upper threshold or above a lower threshold."
                )
        if self.kind == "not_improving" and self.improvement is None:
            raise ValueError("Specify the minimum absolute improvement.")
        if self.kind == "failures" and self.source is not None:
            raise ValueError("Command failures do not use a reading source.")
        return self


class Ruleset(StrictModel):
    version: Literal[1] = 1
    timezone: str
    rules: list[Rule] = Field(default_factory=list, max_length=64)
    fallback: Action
    consecutive_readings: int = Field(default=2, ge=1, le=20)
    min_change_seconds: int = Field(default=120, ge=5, le=86400)
    review_after_seconds: int = Field(default=86400, ge=1800, le=2592000)
    reconsideration_cooldown_seconds: int = Field(default=1800, ge=60, le=86400)
    triggers: list[Trigger] = Field(default_factory=list, max_length=32)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA time zone.") from exc
        return value

    @model_validator(mode="after")
    def unique_ids(self):
        for items in (self.rules, self.triggers):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Rule and trigger IDs must be unique within their lists.")
        return self

    def sources(self):
        sources = [c.source for r in self.rules for c in r.conditions]
        sources.extend(t.source for t in self.triggers if t.source)
        return {s.key(): s for s in sources}


def evaluate(ruleset, inputs, current):
    local = datetime.fromtimestamp(current, ZoneInfo(ruleset.timezone))
    trace = []
    reading_based = False
    evidence_keys = []
    for rule in ruleset.rules:
        in_time = not rule.windows or any(w.matches(local) for w in rule.windows)
        matches = in_time
        if in_time:
            reading_based = reading_based or bool(rule.conditions)
            evidence_keys.extend(c.source.key() for c in rule.conditions)
        for condition in rule.conditions:
            value = inputs[condition.source.key()]
            available = value["available"]
            match = available if condition.test == "available" else not available
            if condition.test == "range":
                match = (
                    available
                    and (condition.lower is None or value["value"] >= condition.lower)
                    and (condition.upper is None or value["value"] < condition.upper)
                )
            matches = matches and match
        trace.append({"rule_id": rule.id, "matches": matches, "in_time": in_time})
        if matches:
            return {
                "rule_id": rule.id,
                "explanation": rule.explanation,
                "action": rule.action.model_dump(),
                "reading_based": reading_based,
                "evidence_keys": list(dict.fromkeys(evidence_keys)),
                "trace": trace,
            }
    return {
        "rule_id": None,
        "explanation": "Fallback: no rule matched.",
        "action": ruleset.fallback.model_dump(),
        "reading_based": reading_based,
        "evidence_keys": list(dict.fromkeys(evidence_keys)),
        "trace": trace,
    }
