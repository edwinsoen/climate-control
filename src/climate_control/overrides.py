"""Attribute observed setting changes to app commands or to changes made outside the app.

Every power, mode, or fan command the app dispatches is recorded as an intent on the
device before provider I/O. A change between two consecutive fresh online observations
that no recent intent explains was made on the device or in another app.
"""

INTENT_SECONDS = 600
# Matches the rule engine's confirmation window. A confirmed intent still explains its
# setting reappearing within this window.
CONFIRM_SECONDS = 90


def settings_of(state: dict) -> dict:
    """Configured settings only; running speed varies on its own in built-in Auto."""
    power, mode, fan = state.get("power"), state.get("mode"), state.get("fan_level")
    return {
        "power": power if isinstance(power, str) and power in {"on", "off"} else None,
        "mode": mode if isinstance(mode, str) and mode != "unknown" else None,
        "fan_level": fan if type(fan) is int else None,
    }


def recent(intents: list, current: float) -> list:
    return [item for item in intents if item["at"] > current - INTENT_SECONDS]


def record_intent(intents: list, command, current: float, state: dict) -> list:
    """Add a dispatched setting command with the setting it replaces."""
    values = {key: getattr(command, key) for key in ("power", "mode", "fan_level")}
    intent = {key: value for key, value in values.items() if value is not None}
    if not intent:
        return intents
    return [*intents, {**intent, "at": current, "from": settings_of(state)}]


def _same(a: dict, b: dict) -> bool:
    if a["power"] != b["power"] or a["power"] is None:
        return False
    if a["power"] == "off":
        return True
    return a["mode"] == b["mode"] and (a["mode"] != "manual" or a["fan_level"] == b["fan_level"])


def _applied(intent: dict, observed: dict) -> bool:
    if "power" in intent:
        return observed["power"] == ("on" if intent["power"] else "off")
    if observed["power"] != "on":
        return False
    if "mode" in intent:
        return observed["mode"] == intent["mode"]
    return observed["mode"] == "manual" and observed["fan_level"] == intent["fan_level"]


def outside_change(previous: dict, observed: dict, intents: list, current: float) -> dict | None:
    """The settings change between two fresh observations that no recent app intent explains.

    An exact match explains a change for the intent's lifetime. Partial and rollback
    observations are excused only for the newest command, unconfirmed and within the
    confirmation window, so a command that never confirms cannot hide a later change.
    """
    before, after = settings_of(previous), settings_of(observed)
    if before["power"] is None or after["power"] is None:
        return None
    newest = intents[-1] if intents else None
    settling = (
        newest
        if newest and "applied_at" not in newest and newest["at"] > current - CONFIRM_SECONDS
        else None
    )
    # An unconfirmed command may fail or roll back to the setting it replaced. Once
    # confirmed, a step back to that setting is a person reverting it.
    if settling and _same(settling["from"], after):
        return None
    if before["power"] != after["power"]:
        # Power commands also carry whatever mode and level the device resumes with.
        explained = any("power" in item and _applied(item, after) for item in intents)
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
        explained = any("power" not in item and _applied(item, after) for item in intents) or (
            # Providers may report the switch to manual before the new level.
            settling is not None
            and "fan_level" in settling
            and mode_changed
            and after["mode"] == "manual"
        )
    return None if explained else {"previous": before, "observed": after}


def settle_intents(intents: list, observed: dict, current: float) -> list:
    """Drop intents observed applied for longer than the confirmation window."""
    after = settings_of(observed)
    kept = []
    for item in intents:
        applied_at = item.get("applied_at")
        if applied_at is None and _applied(item, after):
            applied_at = current
            item = {**item, "applied_at": current}
        if applied_at is None or applied_at > current - CONFIRM_SECONDS:
            kept.append(item)
    return kept
