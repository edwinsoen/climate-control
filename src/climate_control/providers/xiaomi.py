import asyncio
import base64
import hashlib
import json
import math
import os
import re
import secrets
import struct
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import aiohttp
from yarl import URL

from climate_control.providers.base import DeviceCommand, DeviceSnapshot, PollResult, ProviderError
from climate_control.providers.vesync import TokenExpired, retry_delay

XIAOMI_ACCOUNT_BASE = "https://account.xiaomi.com"
XIAOMI_LOGIN_URL = "https://account.xiaomi.com/pass/serviceLogin"
XIAOMI_AUTH_URL = "https://account.xiaomi.com/pass/serviceLoginAuth2"
XIAOMI_SERVERS = {
    "SG": "https://sg.api.io.mi.com/app",
    "DE": "https://de.api.io.mi.com/app",
    "US": "https://us.api.io.mi.com/app",
    "IN": "https://in.api.io.mi.com/app",
    "RU": "https://ru.api.io.mi.com/app",
    "TW": "https://tw.api.io.mi.com/app",
    "CN": "https://api.io.mi.com/app",
}
EUROPE_COUNTRIES = {
    "AD", "AL", "AT", "BA", "BE", "BG", "BY", "CH", "CY", "CZ", "DE", "DK", "EE", "ES",
    "FI", "FR", "GB", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MC",
    "MD", "ME", "MK", "MT", "NL", "NO", "PL", "PT", "RO", "RS", "SE", "SI", "SK", "SM",
    "UA", "VA",
}
AMERICAS_COUNTRIES = {
    "AR", "BO", "BR", "CA", "CL", "CO", "CR", "DO", "EC", "GT", "MX", "PA", "PE", "PR",
    "PY", "US", "UY", "VE",
}


def server_region(country_code: str) -> str:
    code = (country_code or "SG").upper()
    if code in XIAOMI_SERVERS:
        return code
    if code in EUROPE_COUNTRIES:
        return "DE"
    if code in AMERICAS_COUNTRIES:
        return "US"
    return "SG"


def server_base_url(country_code: str) -> str:
    return XIAOMI_SERVERS[server_region(country_code)]


def strip_xiaomi_prefix(text: str) -> dict:
    body = text.strip()
    if body.startswith("&&&START&&&"):
        body = body[len("&&&START&&&") :].strip()
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ProviderError() from exc
    if not isinstance(payload, dict):
        raise ProviderError()
    return payload


def generate_nonce(millis: int | None = None) -> str:
    now_minutes = int((millis if millis is not None else int(time.time() * 1000)) / 60000)
    return base64.b64encode(os.urandom(8) + struct.pack(">i", now_minutes)).decode()


def signed_nonce(ssecurity: str, nonce: str) -> str:
    digest = hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce)).digest()
    return base64.b64encode(digest).decode()


def rc4_crypt(key: bytes, payload: bytes) -> bytes:
    s = list(range(256))
    j = 0
    key_len = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % key_len]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = 0
    j = 0
    for _ in range(1024):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(payload))
    for idx, byte in enumerate(payload):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[idx] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def rc4_encrypt(snonce: str, text: str) -> str:
    raw = rc4_crypt(base64.b64decode(snonce), text.encode("utf-8"))
    return base64.b64encode(raw).decode("ascii")


def rc4_decrypt(snonce: str, text: str) -> str:
    raw = rc4_crypt(base64.b64decode(snonce), base64.b64decode(text))
    return raw.decode("utf-8")


def sha1_sign(method: str, uri: str, params: Mapping[str, str], snonce: str) -> str:
    path = uri[4:] if uri.startswith("/app/") else uri
    parts = [method.upper(), path, *(f"{k}={v}" for k, v in params.items()), snonce]
    digest = hashlib.sha1("&".join(parts).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def encrypt_request(
    uri: str, data: dict, ssecurity: str, nonce: str | None = None
) -> dict[str, str]:
    nonce_val = nonce or generate_nonce()
    snonce = signed_nonce(ssecurity, nonce_val)
    encoded_data = json.dumps(data, separators=(",", ":"))
    params: dict[str, str] = {"data": encoded_data}
    params["rc4_hash__"] = sha1_sign("POST", uri, params, snonce)
    for key, value in list(params.items()):
        params[key] = rc4_encrypt(snonce, value)
    params.update(
        {
            "signature": sha1_sign("POST", uri, params, snonce),
            "ssecurity": ssecurity,
            "_nonce": nonce_val,
        }
    )
    return params


def decode_request_data(post: Mapping[str, str], ssecurity: str) -> dict:
    raw_data = str(post.get("data") or "")
    if "rc4_hash__" in post and post.get("_nonce"):
        snonce = signed_nonce(ssecurity, str(post["_nonce"]))
        raw_data = rc4_decrypt(snonce, raw_data)
    return json.loads(raw_data)


@dataclass(frozen=True)
class XiaomiModelSpec:
    model_name: str
    power: tuple[int, int]
    mode: tuple[int, int]
    filter_life: tuple[int, int]
    display: tuple[int, int]
    # Maps display brightness integer -> "bright" | "dim" | "off"
    display_map: dict[int, str]
    # Either discrete favorite_level (0..N or 1..N) or stepless favorite_rpm (min..max -> 1..10)
    favorite_level: tuple[int, int] | None = None
    max_fan_level: int = 14
    favorite_rpm: tuple[int, int] | None = None
    rpm_range: tuple[int, int] = (300, 2200)
    pm25: tuple[int, int] | None = (3, 4)
    pm10: tuple[int, int] | None = None
    air_quality: tuple[int, int] | None = None
    temperature: tuple[int, int] | None = (3, 7)
    humidity: tuple[int, int] | None = (3, 1)


STANDARD_DISPLAY_MAP = {0: "off", 1: "dim", 2: "bright"}
INVERTED_DISPLAY_MAP = {0: "bright", 1: "dim", 2: "off"}
MI_3C_DISPLAY_MAP = {0: "off", 1: "dim", 2: "bright", 4: "dim", 8: "bright"}

_SPEC_PURIFIER_4_AIRP = XiaomiModelSpec(
    model_name="Smart Air Purifier 4",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 5),
    max_fan_level=11,
)
_SPEC_PURIFIER_4_XIAOMI_AIRP = XiaomiModelSpec(
    model_name="Smart Air Purifier 4",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(7, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 1),
    max_fan_level=11,
    pm10=(3, 5),
    air_quality=(3, 3),
)
_SPEC_PURIFIER_4 = XiaomiModelSpec(
    model_name="Smart Air Purifier 4",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 11),
    max_fan_level=14,
)
_SPEC_PURIFIER_4_PRO_VB4 = XiaomiModelSpec(
    model_name="Smart Air Purifier 4 Pro",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 5),
    max_fan_level=11,
    pm10=(3, 8),
    air_quality=(3, 9),
)
_SPEC_PURIFIER_4_PRO_AIRP = XiaomiModelSpec(
    model_name="Smart Air Purifier 4 Pro",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 5),
    max_fan_level=11,
)
_SPEC_PURIFIER_4_PRO = XiaomiModelSpec(
    model_name="Smart Air Purifier 4 Pro",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 11),
    max_fan_level=14,
    pm10=(3, 8),
)
_SPEC_PURIFIER_4_LITE_RMB1 = XiaomiModelSpec(
    model_name="Smart Air Purifier 4 Lite",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 11),
    max_fan_level=14,
)
_SPEC_PURIFIER_4_LITE_RMA1_AIRP = XiaomiModelSpec(
    model_name="Smart Air Purifier 4 Lite",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 2),
    max_fan_level=14,
)
_SPEC_PURIFIER_4_LITE_RMB3 = XiaomiModelSpec(
    model_name="Smart Air Purifier 4 Lite",
    power=(2, 1),
    mode=(2, 3),
    filter_life=(4, 1),
    display=(6, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 1),
    max_fan_level=14,
    temperature=(3, 2),
    air_quality=(3, 3),
)
_SPEC_PURIFIER_4_LITE_RPM = XiaomiModelSpec(
    model_name="Smart Air Purifier 4 Lite",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_rpm=(9, 10),
    rpm_range=(300, 2000),
    max_fan_level=10,
)
_SPEC_PURIFIER_4_COMPACT = XiaomiModelSpec(
    model_name="Smart Air Purifier 4 Compact",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(13, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 11),
    max_fan_level=14,
    temperature=None,
    humidity=None,
)
_SPEC_PURIFIER_ELITE_F1 = XiaomiModelSpec(
    model_name="Smart Air Purifier Elite",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(7, 2),
    display_map=STANDARD_DISPLAY_MAP,
    favorite_level=(9, 2),
    max_fan_level=14,
    pm10=(3, 8),
)
_SPEC_PURIFIER_ELITE_VB2 = XiaomiModelSpec(
    model_name="Smart Air Purifier Elite",
    power=(2, 1),
    mode=(2, 3),
    filter_life=(4, 1),
    display=(6, 1),
    display_map=INVERTED_DISPLAY_MAP,
    favorite_level=(9, 1),
    max_fan_level=11,
    pm10=(3, 5),
    temperature=(3, 2),
    air_quality=(3, 3),
)
_SPEC_PURIFIER_3C = XiaomiModelSpec(
    model_name="Mi Air Purifier 3C",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(7, 2),
    display_map=MI_3C_DISPLAY_MAP,
    favorite_rpm=(9, 3),
    rpm_range=(300, 2200),
    max_fan_level=10,
    temperature=None,
    humidity=None,
)
_SPEC_PURIFIER_3C_XIAOMI = XiaomiModelSpec(
    model_name="Mi Air Purifier 3C",
    power=(2, 1),
    mode=(2, 3),
    filter_life=(4, 1),
    display=(6, 1),
    display_map=INVERTED_DISPLAY_MAP,
    favorite_level=(9, 1),
    max_fan_level=11,
    temperature=(3, 2),
    air_quality=(3, 3),
)
_SPEC_PURIFIER_3H = XiaomiModelSpec(
    model_name="Mi Air Purifier 3H",
    power=(2, 2),
    mode=(2, 5),
    filter_life=(4, 3),
    display=(6, 1),
    display_map=INVERTED_DISPLAY_MAP,
    favorite_level=(10, 10),
    max_fan_level=14,
    pm25=(3, 6),
    humidity=(3, 7),
    temperature=(3, 8),
)
_SPEC_PURIFIER_PRO_H = XiaomiModelSpec(
    model_name="Mi Air Purifier Pro H",
    power=(2, 2),
    mode=(2, 5),
    filter_life=(4, 3),
    display=(6, 1),
    display_map=INVERTED_DISPLAY_MAP,
    favorite_level=(10, 10),
    max_fan_level=9,
    pm25=(3, 6),
    humidity=(3, 7),
    temperature=(3, 8),
)
_SPEC_PURIFIER_2H = XiaomiModelSpec(
    model_name="Mi Air Purifier 2H",
    power=(2, 1),
    mode=(2, 4),
    filter_life=(4, 1),
    display=(6, 1),
    display_map=INVERTED_DISPLAY_MAP,
    favorite_level=(7, 1),
    max_fan_level=14,
)

MODEL_SPECS: dict[str, XiaomiModelSpec] = {
    # Xiaomi Smart Air Purifier 4
    "zhimi.airp.mb5": _SPEC_PURIFIER_4_AIRP,
    "zhimi.airp.mb5a": _SPEC_PURIFIER_4_AIRP,
    "xiaomi.airp.mb5": _SPEC_PURIFIER_4_XIAOMI_AIRP,
    "zhimi.airpurifier.mb5": _SPEC_PURIFIER_4,
    "xiaomi.airpurifier.mb5": _SPEC_PURIFIER_4,
    # Xiaomi Smart Air Purifier 4 Pro
    "zhimi.airp.vb4": _SPEC_PURIFIER_4_PRO_VB4,
    "zhimi.airp.vb4a": _SPEC_PURIFIER_4_PRO_AIRP,
    "zhimi.airp.va2": _SPEC_PURIFIER_4_PRO_AIRP,
    "zhimi.airp.va2a": _SPEC_PURIFIER_4_PRO_AIRP,
    "zhimi.airpurifier.vb4": _SPEC_PURIFIER_4_PRO,
    "xiaomi.airpurifier.4pro": _SPEC_PURIFIER_4_PRO,
    "zhimi.airpurifier.va2": _SPEC_PURIFIER_4_PRO,
    "zhimi.airpurifier.v7": _SPEC_PURIFIER_4_PRO,
    # Xiaomi Smart Air Purifier 4 Lite
    "zhimi.airp.rmb1": _SPEC_PURIFIER_4_LITE_RMB1,
    "zhimi.airpurifier.rmb1": _SPEC_PURIFIER_4_LITE_RMB1,
    "zhimi.airp.rma1": _SPEC_PURIFIER_4_LITE_RMA1_AIRP,
    "xiaomi.airp.rmb3": _SPEC_PURIFIER_4_LITE_RMB3,
    "zhimi.airpurifier.rma1": _SPEC_PURIFIER_4_LITE_RPM,
    "xiaomi.airpurifier.4lite": _SPEC_PURIFIER_4_LITE_RPM,
    # Xiaomi Smart Air Purifier 4 Compact
    "zhimi.airp.cpa4": _SPEC_PURIFIER_4_COMPACT,
    "zhimi.airp.cpa4a": _SPEC_PURIFIER_4_COMPACT,
    "xiaomi.airp.cpa4": _SPEC_PURIFIER_4_COMPACT,
    "xiaomi.airp.cpa4m": _SPEC_PURIFIER_4_COMPACT,
    "zhimi.airpurifier.cpa4": _SPEC_PURIFIER_4_COMPACT,
    "xiaomi.airpurifier.cpa4": _SPEC_PURIFIER_4_COMPACT,
    # Xiaomi Smart Air Purifier Elite
    "dmaker.airpurifier.f1": _SPEC_PURIFIER_ELITE_F1,
    "dmaker.airp.f1": _SPEC_PURIFIER_ELITE_F1,
    "xiaomi.airp.vb2b": _SPEC_PURIFIER_ELITE_VB2,
    "xiaomi.airp.vb2c": _SPEC_PURIFIER_ELITE_VB2,
    # Mi Air Purifier 3C
    "zhimi.airpurifier.mb4": _SPEC_PURIFIER_3C,
    "zhimi.airp.mb4": _SPEC_PURIFIER_3C,
    "zhimi.airp.mb4a": _SPEC_PURIFIER_3C,
    "xiaomi.airp.mb4": _SPEC_PURIFIER_3C_XIAOMI,
    # Mi Air Purifier 3H
    "zhimi.airpurifier.mb3": _SPEC_PURIFIER_3H,
    "zhimi.airp.mb3": _SPEC_PURIFIER_3H,
    "zhimi.airp.mb3a": _SPEC_PURIFIER_3H,
    "zhimi.airpurifier.ma4": _SPEC_PURIFIER_3H,
    "zhimi.airp.ma4": _SPEC_PURIFIER_3H,
    # Mi Air Purifier Pro H
    "zhimi.airpurifier.va1": _SPEC_PURIFIER_PRO_H,
    "zhimi.airp.va1": _SPEC_PURIFIER_PRO_H,
    "zhimi.airpurifier.vb2": _SPEC_PURIFIER_PRO_H,
    "zhimi.airp.vb2": _SPEC_PURIFIER_PRO_H,
    "zhimi.airp.vb2a": _SPEC_PURIFIER_PRO_H,
    # Mi Air Purifier 2H
    "zhimi.airpurifier.mc2": _SPEC_PURIFIER_2H,
    "zhimi.airp.mc2": _SPEC_PURIFIER_2H,
}

MODE_FROM_MIOT = {0: "auto", 1: "sleep", 2: "manual", 3: "preset", 4: "preset", 5: "preset"}
MODE_TO_MIOT = {"auto": 0, "sleep": 1}
QUALITY_FROM_MIOT = {
    0: "excellent",
    1: "excellent",
    2: "good",
    3: "moderate",
    4: "poor",
    5: "poor",
    6: "poor",
}


def is_purifier_model(model: str) -> bool:
    lowered = (model or "").lower()
    return ".airpurifier." in lowered or ".airp." in lowered


def spec_capabilities(spec: XiaomiModelSpec) -> list[str]:
    result = ["power", "mode", "fan_level", "filter_life", "display", "air_quality"]
    if spec.pm25 is not None:
        result.append("pm25")
    if spec.pm10 is not None:
        result.append("pm10")
    if spec.temperature is not None:
        result.append("temperature")
    if spec.humidity is not None:
        result.append("humidity")
    return result


def spec_controls(spec: XiaomiModelSpec, supported: list[str] | None = None) -> dict:
    if supported is None:
        supported = spec_capabilities(spec)
    has_warning_sensor = any(
        metric in supported for metric in ("pm25", "pm10", "air_quality")
    )
    display_modes = [
        "manual",
        *(["warnings_only_bright", "warnings_only_dim"] if has_warning_sensor else []),
    ]
    return {
        "power": True,
        "modes": ["auto", "sleep"],
        "fan_levels": list(range(1, spec.max_fan_level + 1)),
        "display": "display" in supported,
        "display_states": ["bright", "dim", "off"] if "display" in supported else [],
        "display_modes": display_modes if "display" in supported else [],
    }


def level_to_rpm(level: int, rpm_range: tuple[int, int]) -> int:
    low, high = rpm_range
    clamped = max(1, min(10, level))
    return round((low + (clamped - 1) * (high - low) / 9) / 10) * 10


def rpm_to_level(rpm: int | float, rpm_range: tuple[int, int]) -> int:
    low, high = rpm_range
    if high <= low:
        return 1
    return max(1, min(10, round(1 + (float(rpm) - low) * 9 / (high - low))))


def derive_air_quality(pm25: int | float | None, raw_aq: int | str | None) -> str | None:
    if isinstance(raw_aq, int) and not isinstance(raw_aq, bool):
        mapped = QUALITY_FROM_MIOT.get(raw_aq)
        if mapped is not None:
            return mapped
    if isinstance(raw_aq, str) and raw_aq.strip().lower() in {
        "excellent",
        "very good",
        "good",
        "moderate",
        "poor",
        "bad",
    }:
        return raw_aq.strip().lower()
    if (
        isinstance(pm25, (int, float))
        and not isinstance(pm25, bool)
        and math.isfinite(pm25)
        and pm25 >= 0
    ):
        if pm25 <= 12:
            return "excellent"
        if pm25 <= 35:
            return "good"
        if pm25 <= 55:
            return "moderate"
        return "poor"
    return None


def prop_list_for_spec(did: str, spec: XiaomiModelSpec) -> list[dict]:
    coords = [
        spec.power,
        spec.mode,
        spec.filter_life,
        spec.display,
    ]
    for optional in (
        spec.favorite_level,
        spec.favorite_rpm,
        spec.pm25,
        spec.pm10,
        spec.air_quality,
        spec.temperature,
        spec.humidity,
    ):
        if optional is not None:
            coords.append(optional)
    return [{"did": did, "siid": siid, "piid": piid} for siid, piid in coords]


def normalize_xiaomi_device(
    descriptor: dict,
    spec: XiaomiModelSpec,
    props: dict[tuple[int, int], object],
) -> DeviceSnapshot:
    did = str(descriptor.get("did") or "")
    name = str(descriptor.get("name") or spec.model_name)
    raw_power = props.get(spec.power)
    raw_mode = props.get(spec.mode)
    if (
        not isinstance(raw_power, bool)
        or not isinstance(raw_mode, int)
        or isinstance(raw_mode, bool)
    ):
        raise ProviderError()
    mode = MODE_FROM_MIOT.get(raw_mode)
    if mode is None:
        raise ProviderError()

    fan_level: int | None = None
    if spec.favorite_level is not None:
        raw_fav = props.get(spec.favorite_level)
        if isinstance(raw_fav, int) and not isinstance(raw_fav, bool):
            fan_level = max(1, min(spec.max_fan_level, raw_fav))
    elif spec.favorite_rpm is not None:
        raw_rpm = props.get(spec.favorite_rpm)
        if (
            isinstance(raw_rpm, (int, float))
            and not isinstance(raw_rpm, bool)
            and math.isfinite(raw_rpm)
        ):
            fan_level = rpm_to_level(raw_rpm, spec.rpm_range)
    if fan_level is None:
        raise ProviderError()

    raw_display = props.get(spec.display)
    display = (
        spec.display_map.get(raw_display)
        if isinstance(raw_display, int) and not isinstance(raw_display, bool)
        else None
    )

    raw_filter = props.get(spec.filter_life)
    filter_life = (
        int(raw_filter)
        if isinstance(raw_filter, (int, float))
        and not isinstance(raw_filter, bool)
        and 0 <= raw_filter <= 100
        else None
    )

    def numeric_prop(
        coord: tuple[int, int] | None,
        *,
        min_val: float = 0,
        max_val: float = 10000,
    ):
        if coord is None:
            return None
        val = props.get(coord)
        if (
            isinstance(val, (int, float))
            and not isinstance(val, bool)
            and min_val <= val <= max_val
        ):
            return round(float(val), 1) if isinstance(val, float) or min_val < 0 else int(val)
        return None

    pm25 = numeric_prop(spec.pm25)
    pm10 = numeric_prop(spec.pm10)
    temperature = numeric_prop(spec.temperature, min_val=-50, max_val=100)
    humidity = numeric_prop(spec.humidity, min_val=0, max_val=100)
    raw_aq = props.get(spec.air_quality) if spec.air_quality is not None else None
    supported = spec_capabilities(spec)

    state = {
        "connectivity": "online",
        "power": "on" if raw_power else "off",
        "mode": mode,
        "fan_level": fan_level,
        "reported_fan_level": fan_level,
        "filter_life": filter_life,
        "display": display,
        "air_quality": derive_air_quality(pm25, raw_aq),
        "pm25": pm25,
        "pm1": None,
        "pm10": pm10,
        "temperature": temperature,
        "humidity": humidity,
    }
    return DeviceSnapshot(
        provider_id=did,
        name=name,
        model=spec.model_name,
        capabilities=supported,
        controls=spec_controls(spec, supported),
        state=state,
        retrieved_at=time.time(),
    )


class TwoFactorRequired(Exception):
    def __init__(self, pending_tokens: dict, *, invalid_code: bool = False):
        self.pending_tokens = pending_tokens
        self.invalid_code = invalid_code


class XiaomiProvider:
    def __init__(self, spacing: float = 1):
        self.spacing = spacing
        self.last_request = 0.0
        self.lock = asyncio.Lock()

    async def pace(self):
        async with self.lock:
            remaining = self.last_request + self.spacing - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.last_request = time.monotonic()

    @staticmethod
    def _cookie_value(session: aiohttp.ClientSession, url: str, name: str) -> str | None:
        filtered = session.cookie_jar.filter_cookies(URL(url))
        if name in filtered and filtered[name].value:
            return filtered[name].value
        for morsel in session.cookie_jar:
            if morsel.key == name and morsel.value:
                return morsel.value
        return None

    @staticmethod
    def _merge_cookies(
        session: aiohttp.ClientSession,
        cookies: dict[str, str],
        resp: aiohttp.ClientResponse | None = None,
    ):
        if resp is not None:
            for hop in (*resp.history, resp):
                for name, morsel in hop.cookies.items():
                    if morsel.value:
                        cookies[name] = morsel.value
        for morsel in session.cookie_jar:
            if morsel.value:
                cookies[morsel.key] = morsel.value

    async def start_2fa_flow(
        self,
        session: aiohttp.ClientSession,
        notification_url: str,
        device_id: str,
        cookies: dict,
        headers: dict,
    ):
        if notification_url.startswith("/"):
            notification_url = XIAOMI_ACCOUNT_BASE + notification_url
        parsed = urlsplit(notification_url)
        context_vals = parse_qs(parsed.query).get("context")
        if not context_vals or not context_vals[0]:
            raise ProviderError("auth")
        context = context_vals[0]
        merged_cookies = {str(k): str(v) for k, v in cookies.items() if v}

        await self.pace()
        async with session.get(
            notification_url, cookies=merged_cookies, headers=headers
        ) as resp:
            if resp.status == 429:
                raise ProviderError("rate_limit", retry_delay(resp.headers.get("Retry-After")))
            if resp.status in (401, 403):
                raise ProviderError("auth")
            if resp.status != 200:
                raise ProviderError()
            await resp.read()
            self._merge_cookies(session, merged_cookies, resp)

        list_params = {"sid": "xiaomiio", "context": context, "_locale": "en_US"}
        await self.pace()
        async with session.get(
            f"{XIAOMI_ACCOUNT_BASE}/identity/list",
            params=list_params,
            cookies=merged_cookies,
            headers=headers,
        ) as resp:
            if resp.status == 429:
                raise ProviderError("rate_limit", retry_delay(resp.headers.get("Retry-After")))
            if resp.status in (401, 403):
                raise ProviderError("auth")
            if resp.status != 200:
                raise ProviderError()
            list_data = strip_xiaomi_prefix(await resp.text())
            self._merge_cookies(session, merged_cookies, resp)

        options = list_data.get("options") or []
        flag = "4" if list_data.get("flag") == 4 and 8 not in options else "8"
        external_id = str(list_data.get("externalId") or "")
        ick = (
            merged_cookies.get("ick")
            or self._cookie_value(session, f"{XIAOMI_ACCOUNT_BASE}/", "ick")
            or ""
        )
        send_url = (
            f"{XIAOMI_ACCOUNT_BASE}/identity/auth/sendPhoneTicket"
            if flag == "4"
            else f"{XIAOMI_ACCOUNT_BASE}/identity/auth/sendEmailTicket"
        )
        send_params = {
            "_dc": str(int(time.time() * 1000)),
            "sid": "xiaomiio",
            "context": context,
            "mask": "0",
            "_locale": "en_US",
        }
        send_data = {
            "retry": "0",
            "icode": "",
            "_json": "true",
            "ick": ick,
        }
        await self.pace()
        async with session.post(
            send_url,
            params=send_params,
            data=send_data,
            cookies=merged_cookies,
            headers=headers,
        ) as resp:
            if resp.status == 429:
                raise ProviderError("rate_limit", retry_delay(resp.headers.get("Retry-After")))
            if resp.status in (401, 403):
                raise ProviderError("auth")
            if resp.status != 200:
                raise ProviderError()
            send_body = strip_xiaomi_prefix(await resp.text())
            if send_body.get("code") not in (None, 0):
                raise ProviderError()
            self._merge_cookies(session, merged_cookies, resp)

        serialized_cookies = [
            {
                "name": morsel.key,
                "value": morsel.value,
                "domain": morsel["domain"] or "account.xiaomi.com",
                "path": morsel["path"] or "/",
            }
            for morsel in session.cookie_jar
        ]
        known_names = {item["name"] for item in serialized_cookies}
        for name, value in merged_cookies.items():
            if name not in known_names and value:
                serialized_cookies.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": "account.xiaomi.com",
                        "path": "/",
                    }
                )
        raise TwoFactorRequired(
            {
                "pending_2fa": {
                    "context": context,
                    "device_id": device_id,
                    "flag": flag,
                    "external_id": external_id,
                    "cookies": serialized_cookies,
                }
            }
        )

    async def complete_2fa_flow(
        self,
        session: aiohttp.ClientSession,
        credentials: dict,
        pending: dict,
    ) -> dict:
        device_id = str(pending.get("device_id") or secrets.token_hex(8).upper())
        context = str(pending.get("context") or "")
        flag = str(pending.get("flag") or "8")
        code = str(credentials.get("verification_code") or "").strip()
        if not context or not code:
            raise TwoFactorRequired({"pending_2fa": pending})

        cookies: dict[str, str] = {
            "sdkVersion": "accountsdk-18.8.15",
            "deviceId": device_id,
        }
        for item in pending.get("cookies") or []:
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                name = str(item["name"])
                value = str(item["value"])
                if name == "userId" and "@" in value:
                    continue
                cookies[name] = value
                domain = str(item.get("domain") or "account.xiaomi.com").lstrip(".")
                path = str(item.get("path") or "/")
                if not path.endswith("/"):
                    path = path + "/"
                session.cookie_jar.update_cookies(
                    {name: value},
                    URL(f"https://{domain}{path}"),
                )

        headers = {
            "User-Agent": "Android-7.1.1-1.0.0-ONEPLUS A3010-136-"
            + device_id
            + " APP/xiaomi.smarthome APPV/62830",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        verify_url = (
            f"{XIAOMI_ACCOUNT_BASE}/identity/auth/verifyPhone"
            if flag == "4"
            else f"{XIAOMI_ACCOUNT_BASE}/identity/auth/verifyEmail"
        )
        ick = (
            cookies.get("ick")
            or self._cookie_value(session, f"{XIAOMI_ACCOUNT_BASE}/", "ick")
            or ""
        )
        verify_params = {
            "_flag": flag,
            "_json": "true",
            "sid": "xiaomiio",
            "context": context,
            "mask": "0",
            "_locale": "en_US",
        }
        verify_data = {
            "_flag": flag,
            "ticket": code,
            "trust": "false",
            "_json": "true",
            "ick": ick,
        }
        await self.pace()
        async with session.post(
            verify_url,
            params=verify_params,
            data=verify_data,
            cookies=cookies,
            headers=headers,
        ) as resp:
            if resp.status == 429:
                raise ProviderError("rate_limit", retry_delay(resp.headers.get("Retry-After")))
            if resp.status in (400, 401, 403):
                raise TwoFactorRequired({"pending_2fa": pending}, invalid_code=True)
            if resp.status != 200:
                raise ProviderError()
            raw_text = await resp.text()
            finish_loc = resp.headers.get("Location")
            self._merge_cookies(session, cookies, resp)

        try:
            jr = strip_xiaomi_prefix(raw_text)
        except ProviderError:
            jr = None
        if isinstance(jr, dict):
            if jr.get("code") not in (None, 0):
                raise TwoFactorRequired({"pending_2fa": pending}, invalid_code=True)
            finish_loc = str(jr.get("location") or finish_loc or "")
        elif not finish_loc and raw_text:
            match = re.search(
                r'https?://[^"\']+/identity/result/check\?[^"\']+', raw_text
            )
            if match:
                finish_loc = match.group(0)

        if not finish_loc:
            await self.pace()
            async with session.get(
                f"{XIAOMI_ACCOUNT_BASE}/identity/result/check",
                params={"sid": "xiaomiio", "context": context, "_locale": "en_US"},
                cookies=cookies,
                headers=headers,
                allow_redirects=False,
            ) as r0:
                self._merge_cookies(session, cookies, r0)
                if r0.status in (301, 302, 303, 307, 308) and r0.headers.get("Location"):
                    finish_loc = r0.headers["Location"]
        if not finish_loc:
            raise TwoFactorRequired({"pending_2fa": pending}, invalid_code=True)
        if finish_loc.startswith("/"):
            finish_loc = XIAOMI_ACCOUNT_BASE + finish_loc

        ext_prag = None
        if "identity/result/check" in finish_loc:
            await self.pace()
            async with session.get(
                finish_loc, cookies=cookies, headers=headers, allow_redirects=False
            ) as r_check:
                self._merge_cookies(session, cookies, r_check)
                ext_prag = r_check.headers.get("extension-pragma") or ext_prag
                end_url = r_check.headers.get("Location")
        else:
            end_url = finish_loc
        if not end_url:
            raise ProviderError("auth")
        if end_url.startswith("/"):
            end_url = XIAOMI_ACCOUNT_BASE + end_url

        await self.pace()
        async with session.get(
            end_url, cookies=cookies, headers=headers, allow_redirects=False
        ) as r_end:
            self._merge_cookies(session, cookies, r_end)
            end_status = r_end.status
            end_text = await r_end.text()
            ext_prag = r_end.headers.get("extension-pragma") or ext_prag
            sts_url = r_end.headers.get("Location")
        if end_status == 200 and "Xiaomi Account - Tips" in end_text:
            await self.pace()
            async with session.get(
                end_url, cookies=cookies, headers=headers, allow_redirects=False
            ) as r_end2:
                self._merge_cookies(session, cookies, r_end2)
                end_text = await r_end2.text()
                ext_prag = r_end2.headers.get("extension-pragma") or ext_prag
                sts_url = r_end2.headers.get("Location") or sts_url

        if sts_url and sts_url.startswith("/"):
            sts_url = XIAOMI_ACCOUNT_BASE + sts_url
        if sts_url and "/fe/" in sts_url:
            skip_vals = parse_qs(urlsplit(sts_url).query).get("skipUrl")
            if skip_vals and skip_vals[0]:
                skip_url = skip_vals[0]
                if skip_url.startswith("/"):
                    skip_url = XIAOMI_ACCOUNT_BASE + skip_url
                await self.pace()
                async with session.get(
                    skip_url, cookies=cookies, headers=headers, allow_redirects=False
                ) as r_skip:
                    self._merge_cookies(session, cookies, r_skip)
                    ext_prag = r_skip.headers.get("extension-pragma") or ext_prag
                    sts_url = r_skip.headers.get("Location") or sts_url

        ssecurity = None
        nonce = None
        if ext_prag:
            try:
                prag_data = json.loads(ext_prag)
                ssecurity = prag_data.get("ssecurity")
                nonce = prag_data.get("nonce")
            except ValueError:
                ssecurity = None

        if not sts_url and end_text:
            idx = end_text.find("https://sts.api.io.mi.com/sts")
            if idx != -1:
                end_idx = end_text.find('"', idx)
                sts_url = end_text[idx : end_idx if end_idx != -1 else idx + 300]

        if not ssecurity or not sts_url:
            await self.pace()
            async with session.get(
                XIAOMI_LOGIN_URL,
                params={"sid": "xiaomiio", "_json": "true"},
                cookies=cookies,
                headers=headers,
            ) as r_relogin:
                if r_relogin.status == 200:
                    self._merge_cookies(session, cookies, r_relogin)
                    try:
                        relogin_data = strip_xiaomi_prefix(await r_relogin.text())
                    except ProviderError:
                        relogin_data = {}
                    ssecurity = ssecurity or relogin_data.get("ssecurity")
                    nonce = nonce if nonce is not None else relogin_data.get("nonce")
                    sts_url = sts_url or relogin_data.get("location")
                    if relogin_data.get("userId"):
                        cookies["userId"] = str(relogin_data["userId"])
                    if relogin_data.get("passToken"):
                        cookies["passToken"] = str(relogin_data["passToken"])

        if not ssecurity or not sts_url:
            raise ProviderError("auth")
        if sts_url.startswith("/"):
            sts_url = XIAOMI_ACCOUNT_BASE + sts_url

        sts_params = None
        if nonce is not None and "clientSign=" not in sts_url:
            sign_raw = f"nonce={nonce}&{ssecurity}".encode()
            sts_params = {
                "clientSign": base64.b64encode(hashlib.sha1(sign_raw).digest()).decode()
            }
        await self.pace()
        async with session.get(
            sts_url,
            params=sts_params,
            cookies=cookies,
            headers=headers,
            allow_redirects=True,
        ) as r_sts:
            if r_sts.status == 429:
                raise ProviderError("rate_limit", retry_delay(r_sts.headers.get("Retry-After")))
            if r_sts.status in (401, 403):
                raise ProviderError("auth")
            if r_sts.status != 200:
                raise ProviderError()
            await r_sts.read()
            self._merge_cookies(session, cookies, r_sts)
            service_token = (
                r_sts.cookies["serviceToken"].value
                if "serviceToken" in r_sts.cookies
                else (
                    cookies.get("serviceToken")
                    or self._cookie_value(session, str(r_sts.url), "serviceToken")
                )
            )
        if not service_token:
            raise ProviderError("auth")

        raw_email = str(credentials.get("email") or "").strip()
        cookie_user_id = (
            cookies.get("userId")
            or self._cookie_value(session, f"{XIAOMI_ACCOUNT_BASE}/", "userId")
            or self._cookie_value(session, str(r_sts.url), "userId")
            or ""
        )
        if "@" in cookie_user_id:
            cookie_user_id = ""
        user_id = (
            cookie_user_id
            or str(pending.get("external_id") or "")
            or (raw_email if raw_email.isdigit() else "")
        )
        pass_token = (
            cookies.get("passToken")
            or self._cookie_value(session, f"{XIAOMI_ACCOUNT_BASE}/", "passToken")
            or ""
        )
        if not user_id:
            raise ProviderError("auth")
        return {
            "user_id": str(user_id),
            "ssecurity": str(ssecurity),
            "service_token": str(service_token),
            "pass_token": str(pass_token),
            "device_id": device_id,
            "country_code": server_region(credentials.get("country_code", "SG")),
        }

    async def login(
        self,
        session: aiohttp.ClientSession,
        credentials: dict,
        tokens: dict | None = None,
    ) -> dict:
        pending_2fa = tokens.get("pending_2fa") if isinstance(tokens, dict) else None
        code = str(credentials.get("verification_code") or "").strip()
        if isinstance(pending_2fa, dict) and code:
            return await self.complete_2fa_flow(session, credentials, pending_2fa)

        device_id = (
            str(tokens["device_id"])
            if isinstance(tokens, dict) and tokens.get("device_id")
            else secrets.token_hex(8).upper()
        )
        raw_email = str(credentials.get("email") or "").strip()
        initial_user_id = (
            str(tokens["user_id"])
            if isinstance(tokens, dict) and tokens.get("user_id")
            else (raw_email if raw_email.isdigit() else "")
        )
        cookies = {
            "sdkVersion": "accountsdk-18.8.15",
            "deviceId": device_id,
        }
        if initial_user_id:
            cookies["userId"] = initial_user_id
        if isinstance(tokens, dict) and tokens.get("pass_token") and tokens.get("user_id"):
            cookies["passToken"] = str(tokens["pass_token"])
        headers = {
            "User-Agent": "Android-7.1.1-1.0.0-ONEPLUS A3010-136-" + device_id
            + " APP/xiaomi.smarthome APPV/62830"
        }
        await self.pace()
        async with session.get(
            XIAOMI_LOGIN_URL,
            params={"sid": "xiaomiio", "_json": "true"},
            cookies=cookies,
            headers=headers,
        ) as resp:
            if resp.status == 429:
                raise ProviderError("rate_limit", retry_delay(resp.headers.get("Retry-After")))
            if resp.status != 200:
                raise ProviderError()
            step1 = strip_xiaomi_prefix(await resp.text())
            self._merge_cookies(session, cookies, resp)

        if not (step1.get("ssecurity") and step1.get("location") and step1.get("userId")):
            cookies.pop("passToken", None)
            session.cookie_jar.clear()
            password_hash = hashlib.md5(credentials["password"].encode()).hexdigest().upper()
            form = {
                "sid": step1.get("sid") or "xiaomiio",
                "hash": password_hash,
                "callback": step1.get("callback") or "https://sts.api.io.mi.com/sts",
                "qs": step1.get("qs") or "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                "user": credentials["email"],
                "_sign": step1.get("_sign") or "",
                "_json": "true",
            }
            await self.pace()
            async with session.post(
                XIAOMI_AUTH_URL,
                data=form,
                cookies=cookies,
                headers=headers,
            ) as resp:
                if resp.status == 429:
                    raise ProviderError("rate_limit", retry_delay(resp.headers.get("Retry-After")))
                if resp.status in (401, 403):
                    raise ProviderError("auth")
                if resp.status != 200:
                    raise ProviderError()
                step2 = strip_xiaomi_prefix(await resp.text())
                self._merge_cookies(session, cookies, resp)
        else:
            step2 = step1

        if step2.get("userId"):
            cookies["userId"] = str(step2["userId"])

        if step2.get("notificationUrl"):
            await self.start_2fa_flow(
                session, str(step2["notificationUrl"]), device_id, cookies, headers
            )

        if (
            step2.get("captchaUrl")
            or not step2.get("ssecurity")
            or not step2.get("location")
            or not step2.get("userId")
        ):
            raise ProviderError("auth")

        location_url = str(step2["location"])
        location_params = None
        if step2.get("nonce") is not None and "clientSign=" not in location_url:
            sign_raw = f"nonce={step2['nonce']}&{step2['ssecurity']}".encode()
            location_params = {
                "clientSign": base64.b64encode(hashlib.sha1(sign_raw).digest()).decode()
            }
        await self.pace()
        async with session.get(
            location_url, params=location_params, cookies=cookies, headers=headers
        ) as resp:
            if resp.status == 429:
                raise ProviderError("rate_limit", retry_delay(resp.headers.get("Retry-After")))
            if resp.status in (401, 403):
                raise ProviderError("auth")
            if resp.status != 200:
                raise ProviderError()
            self._merge_cookies(session, cookies, resp)
            service_token = resp.cookies.get("serviceToken")
            if service_token is not None:
                token_value = service_token.value
            else:
                token_value = (
                    cookies.get("serviceToken")
                    or self._cookie_value(session, str(resp.url), "serviceToken")
                )
            if not token_value:
                raise ProviderError("auth")

        return {
            "user_id": str(step2["userId"]),
            "ssecurity": str(step2["ssecurity"]),
            "service_token": token_value,
            "pass_token": str(
                step2.get("passToken")
                or cookies.get("passToken")
                or (tokens.get("pass_token") if isinstance(tokens, dict) else "")
                or ""
            ),
            "device_id": device_id,
            "country_code": server_region(credentials.get("country_code", "SG")),
        }

    async def api_request(
        self,
        session: aiohttp.ClientSession,
        tokens: dict,
        country_code: str,
        uri: str,
        payload: dict,
    ) -> dict:
        base_url = server_base_url(country_code or tokens.get("country_code", "SG"))
        nonce_val = generate_nonce()
        snonce = signed_nonce(tokens["ssecurity"], nonce_val)
        encrypted = encrypt_request(uri, payload, tokens["ssecurity"], nonce_val)
        cookies = {
            "userId": str(tokens["user_id"]),
            "yetAnotherServiceToken": str(tokens["service_token"]),
            "serviceToken": str(tokens["service_token"]),
            "locale": "en_US",
            "timezone": "GMT+00:00",
            "is_daylight": "0",
            "dst_offset": "0",
            "channel": "MI_APP_STORE",
        }
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": "Android-7.1.1-1.0.0-ONEPLUS A3010-136-"
            + str(tokens.get("device_id") or "XIAOMI0001")
            + " APP/xiaomi.smarthome APPV/62830",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }
        await self.pace()
        async with session.post(
            base_url + uri,
            data=encrypted,
            cookies=cookies,
            headers=headers,
        ) as resp:
            if resp.status == 429:
                raise ProviderError("rate_limit", retry_delay(resp.headers.get("Retry-After")))
            if resp.status in (401, 403):
                raise TokenExpired()
            if resp.status != 200:
                raise ProviderError()
            try:
                raw_text = (await resp.text()).strip()
            except aiohttp.ClientError as exc:
                raise ProviderError() from exc
        if not raw_text:
            raise ProviderError()
        if not raw_text.startswith("{"):
            try:
                raw_text = rc4_decrypt(snonce, raw_text).strip()
            except Exception as exc:
                raise ProviderError() from exc
        if raw_text.startswith("&&&START&&&"):
            raw_text = raw_text[len("&&&START&&&") :].strip()
        try:
            body = json.loads(raw_text)
        except ValueError as exc:
            raise ProviderError() from exc
        if not isinstance(body, dict):
            raise ProviderError()
        code = body.get("code")
        if code in (2, 3, 401, 403, -4001, -4002, -4003):
            raise TokenExpired()
        if code == 429:
            raise ProviderError("rate_limit")
        if code != 0:
            raise ProviderError()
        return body

    async def run(self, credentials: dict, tokens: dict | None, operation) -> PollResult:
        country_code = credentials.get("country_code", "SG")
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=25, connect=10, sock_read=15),
        ) as session:
            active_tokens = (
                tokens
                if tokens
                and all(
                    tokens.get(k) for k in ("user_id", "ssecurity", "service_token")
                )
                else None
            )
            had_cached_tokens = active_tokens is not None
            current_credentials = dict(credentials)
            current_tokens = tokens
            for attempt in range(2):
                if active_tokens is None:
                    try:
                        active_tokens = await self.login(
                            session, current_credentials, current_tokens
                        )
                        current_tokens = active_tokens
                        current_credentials["verification_code"] = ""
                    except TwoFactorRequired as exc:
                        kind = "auth_2fa_invalid" if exc.invalid_code else "auth_2fa"
                        return PollResult(
                            [],
                            tokens=exc.pending_tokens,
                            failure=ProviderError(kind),
                        )
                    except (ProviderError, aiohttp.ClientError, TimeoutError) as exc:
                        preserved = (
                            current_tokens
                            if isinstance(current_tokens, dict) and "pending_2fa" in current_tokens
                            else None
                        )
                        return PollResult(
                            [],
                            tokens=preserved,
                            failure=exc if isinstance(exc, ProviderError) else ProviderError(),
                        )
                try:
                    return await operation(session, active_tokens, country_code)
                except TokenExpired:
                    if attempt or not had_cached_tokens:
                        return PollResult([], failure=ProviderError("auth"))
                    active_tokens = None
                    current_tokens = (
                        {
                            k: tokens[k]
                            for k in ("device_id", "user_id", "pass_token")
                            if tokens.get(k)
                        }
                        if isinstance(tokens, dict)
                        else None
                    ) or None
                except (ProviderError, aiohttp.ClientError, TimeoutError) as exc:
                    return PollResult(
                        [],
                        tokens=active_tokens,
                        failure=exc if isinstance(exc, ProviderError) else ProviderError(),
                    )
        return PollResult([], failure=ProviderError())

    async def list_devices(
        self, session: aiohttp.ClientSession, tokens: dict, country_code: str
    ) -> list[dict]:
        body = await self.api_request(
            session,
            tokens,
            country_code,
            "/home/device_list",
            {
                "getVirtualModel": True,
                "getHuamiDevices": 1,
                "get_split_device": False,
                "support_smart_home": True,
            },
        )
        result = body.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise ProviderError()
        return [d for d in result["list"] if isinstance(d, dict) and d.get("did")]

    async def read_device_snapshot(
        self,
        session: aiohttp.ClientSession,
        tokens: dict,
        country_code: str,
        descriptor: dict,
        spec: XiaomiModelSpec,
        failure_kind: str = "temporary",
    ) -> DeviceSnapshot:
        if descriptor.get("isOnline") is False:
            raise ProviderError("offline")
        did = str(descriptor["did"])
        try:
            body = await self.api_request(
                session,
                tokens,
                country_code,
                "/miotspec/prop/get",
                {"params": prop_list_for_spec(did, spec)},
            )
        except ProviderError as exc:
            if exc.kind == "temporary" and failure_kind != "temporary":
                raise ProviderError(failure_kind) from exc
            raise
        items = body.get("result")
        if not isinstance(items, list):
            raise ProviderError(failure_kind)
        props: dict[tuple[int, int], object] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code", 0)
            if code in (-704042011, -704042000, -4004):
                raise ProviderError("offline")
            if (
                code == 0
                and isinstance(item.get("siid"), int)
                and isinstance(item.get("piid"), int)
            ):
                props[(item["siid"], item["piid"])] = item.get("value")
        try:
            return normalize_xiaomi_device(descriptor, spec, props)
        except ProviderError as exc:
            if exc.kind == "temporary" and failure_kind != "temporary":
                raise ProviderError(failure_kind) from exc
            raise

    async def poll(self, credentials: dict, tokens: dict | None) -> PollResult:
        async def operation(session, active_tokens, country_code):
            descriptors = await self.list_devices(session, active_tokens, country_code)
            snapshots: list[DeviceSnapshot] = []
            failure: ProviderError | None = None
            for descriptor in descriptors:
                model = str(descriptor.get("model") or "")
                spec = MODEL_SPECS.get(model)
                if spec is None:
                    if is_purifier_model(model):
                        snapshots.append(
                            DeviceSnapshot(
                                provider_id=str(descriptor["did"]),
                                name=str(descriptor.get("name") or model),
                                model=model,
                                supported=False,
                                error="This purifier model is not supported.",
                            )
                        )
                    continue
                try:
                    snapshots.append(
                        await self.read_device_snapshot(
                            session, active_tokens, country_code, descriptor, spec
                        )
                    )
                except TokenExpired:
                    raise
                except (ProviderError, aiohttp.ClientError, TimeoutError) as exc:
                    error = exc if isinstance(exc, ProviderError) else ProviderError()
                    snapshots.append(
                        DeviceSnapshot(
                            provider_id=str(descriptor["did"]),
                            name=str(descriptor.get("name") or spec.model_name),
                            model=spec.model_name,
                            capabilities=spec_capabilities(spec),
                            controls=spec_controls(spec),
                            error=str(error),
                            state={"connectivity": "offline"} if error.kind == "offline" else {},
                        )
                    )
                    if error.kind in {"rate_limit", "auth"}:
                        return PollResult(snapshots, active_tokens, error)
                    if error.kind != "offline":
                        failure = error
            return PollResult(snapshots, active_tokens, failure)

        return await self.run(credentials, tokens, operation)

    async def status(self, credentials: dict, tokens: dict | None, provider_id: str) -> PollResult:
        async def operation(session, active_tokens, country_code):
            descriptors = await self.list_devices(session, active_tokens, country_code)
            descriptor = next(
                (d for d in descriptors if str(d.get("did")) == provider_id), None
            )
            if descriptor is None:
                raise ProviderError("invalid_control")
            spec = MODEL_SPECS.get(str(descriptor.get("model") or ""))
            if spec is None:
                raise ProviderError("invalid_control")
            snapshot = await self.read_device_snapshot(
                session, active_tokens, country_code, descriptor, spec
            )
            return PollResult([snapshot], active_tokens)

        return await self.run(credentials, tokens, operation)

    async def control(
        self, credentials: dict, tokens: dict | None, provider_id: str, command: DeviceCommand
    ) -> PollResult:
        async def operation(session, active_tokens, country_code):
            descriptors = await self.list_devices(session, active_tokens, country_code)
            descriptor = next(
                (d for d in descriptors if str(d.get("did")) == provider_id), None
            )
            if descriptor is None:
                raise ProviderError("invalid_control")
            spec = MODEL_SPECS.get(str(descriptor.get("model") or ""))
            if spec is None:
                raise ProviderError("invalid_control")
            options = spec_controls(spec)
            if command.mode is not None and command.mode not in options["modes"]:
                raise ProviderError("invalid_control")
            if command.fan_level is not None and command.fan_level not in options["fan_levels"]:
                raise ProviderError("invalid_control")
            target_display = self.normalize_display_target(command.display)
            if target_display is not None and target_display not in options["display_states"]:
                raise ProviderError("invalid_control")

            current = await self.read_device_snapshot(
                session,
                active_tokens,
                country_code,
                descriptor,
                spec,
                failure_kind="control_failed",
            )
            if command.power is None and current.state.get("power") != "on":
                raise ProviderError("control_failed")
            if target_display is not None and self.matches_command(current, command):
                return PollResult([current], active_tokens, control_confirmed=True)

            writes = self.build_writes(provider_id, spec, command, target_display)
            rejected = False
            try:
                body = await self.api_request(
                    session,
                    active_tokens,
                    country_code,
                    "/miotspec/prop/set",
                    {"params": writes},
                )
                results = body.get("result")
                if not isinstance(results, list) or any(
                    not isinstance(item, dict) or item.get("code", 0) != 0 for item in results
                ):
                    rejected = True
            except TokenExpired:
                raise
            except (ProviderError, aiohttp.ClientError, TimeoutError) as exc:
                if isinstance(exc, ProviderError) and exc.kind in {"auth", "rate_limit"}:
                    raise
                rejected = True

            observed = await self.read_device_snapshot(
                session, active_tokens, country_code, descriptor, spec
            )
            confirmed = self.matches_command(observed, command)
            if rejected and not confirmed:
                raise ProviderError("control_failed")
            return PollResult([observed], active_tokens, control_confirmed=confirmed)

        return await self.run(credentials, tokens, operation)

    @staticmethod
    def normalize_display_target(display: bool | str | None) -> str | None:
        if display is None:
            return None
        if isinstance(display, bool):
            return "bright" if display else "off"
        if display == "on":
            return "bright"
        if display in {"bright", "dim", "off"}:
            return display
        return "invalid"

    @staticmethod
    def build_writes(
        did: str,
        spec: XiaomiModelSpec,
        command: DeviceCommand,
        target_display: str | None,
    ) -> list[dict]:
        if command.power is not None:
            siid, piid = spec.power
            return [{"did": did, "siid": siid, "piid": piid, "value": bool(command.power)}]
        if command.mode is not None:
            siid, piid = spec.mode
            return [{"did": did, "siid": siid, "piid": piid, "value": MODE_TO_MIOT[command.mode]}]
        if command.fan_level is not None:
            mode_siid, mode_piid = spec.mode
            writes = [{"did": did, "siid": mode_siid, "piid": mode_piid, "value": 2}]
            if spec.favorite_level is not None:
                fav_siid, fav_piid = spec.favorite_level
                writes.append(
                    {
                        "did": did,
                        "siid": fav_siid,
                        "piid": fav_piid,
                        "value": int(command.fan_level),
                    }
                )
            elif spec.favorite_rpm is not None:
                rpm_siid, rpm_piid = spec.favorite_rpm
                writes.append(
                    {
                        "did": did,
                        "siid": rpm_siid,
                        "piid": rpm_piid,
                        "value": level_to_rpm(command.fan_level, spec.rpm_range),
                    }
                )
            return writes
        if target_display is not None:
            disp_siid, disp_piid = spec.display
            reverse_map: dict[str, int] = {}
            for raw, state in spec.display_map.items():
                reverse_map.setdefault(state, raw)
            return [
                {
                    "did": did,
                    "siid": disp_siid,
                    "piid": disp_piid,
                    "value": reverse_map[target_display],
                }
            ]
        return []

    @classmethod
    def matches_command(cls, snapshot: DeviceSnapshot, command: DeviceCommand) -> bool:
        state = snapshot.state
        if command.power is not None:
            return state.get("power") == ("on" if command.power else "off")
        if command.mode is not None:
            return state.get("mode") == command.mode
        if command.display is not None:
            return state.get("display") == cls.normalize_display_target(command.display)
        return state.get("mode") == "manual" and state.get("fan_level") == command.fan_level
