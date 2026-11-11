"""Attribute observed setting changes to app commands or to changes made outside the app.

Every power, mode, or fan command the app dispatches is recorded as an intent on the
device before provider I/O. A change between two consecutive fresh online observations
that no recent intent explains was made on the device or in another app. Intents stay
valid for a bounded window, so a failed command (no change observed) or a delayed
acknowledgment (the commanded setting appears on a later poll) is never mistaken for an
outside change.
"""

INTENT_SECONDS = 600


def settings_of(state: dict) -> dict:
    """Configured settings only; running speed varies on its own in built-in Auto."""
    power, mode, fan = state.get("power"), state.get("mode"), state.get("fan_level")
    return {
        "power": power if isinstance(power, str) and power in {"on", "off"} else None,
        "mode": mode if isinstance(mode, str) and mode != "unknown" else None,
        "fan_level": fan if type(fan) is int else None,
    }


def setting_intent(command) -> dict | None:
    values = {key: getattr(command, key) for key in ("power", "mode", "fan_level")}
    return {k: v for k, v in values.items() if v is not None} or None


def record_intent(intents: list | None, command, current: float) -> list:
    intent = setting_intent(command)
    kept = [item for item in intents or [] if item["at"] > current - INTENT_SECONDS]
    return [*kept[-15:], {**intent, "at": current}] if intent else kept


def _matches(intent: dict, observed: dict) -> bool:
    if "power" in intent:
        return observed["power"] == ("on" if intent["power"] else "off")
    if observed["power"] != "on":
        return False
    if "mode" in intent:
        return observed["mode"] == intent["mode"]
    return observed["mode"] == "manual" and observed["fan_level"] == intent["fan_level"]


def outside_change(previous: dict, observed: dict, intents: list, current: float) -> dict | None:
    """The settings change between two fresh observations that no recent app intent explains."""
    before, after = settings_of(previous), settings_of(observed)
    intents = [item for item in intents if item["at"] > current - INTENT_SECONDS]
    if before["power"] is None or after["power"] is None:
        return None
    if before["power"] != after["power"]:
        # Power commands also carry whatever mode and level the device resumes with.
        candidates = [item for item in intents if "power" in item]
    elif after["power"] == "off":
        return None
    else:
        mode_changed = None not in (before["mode"], after["mode"]) and (
            before["mode"] != after["mode"]
        )
        level_changed = (
            before["mode"] == after["mode"] == "manual"
            and None not in (before["fan_level"], after["fan_level"])
            and before["fan_level"] != after["fan_level"]
        )
        if not (mode_changed or level_changed):
            return None
        candidates = [item for item in intents if "power" not in item]
    if any(_matches(item, after) for item in candidates):
        return None
    return {"previous": before, "observed": after}


def settle_intents(intents: list | None, observed: dict, current: float) -> list:
    """Drop intents that are expired or already reflected in the observed settings."""
    after = settings_of(observed)
    return [
        item
        for item in intents or []
        if item["at"] > current - INTENT_SECONDS and not _matches(item, after)
    ]
