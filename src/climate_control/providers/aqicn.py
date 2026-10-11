"""AQICN/WAQI station feeds. Pollutant values are AQI, never concentrations."""

import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx2

METRICS = ("aqi", "pm25", "pm10")
# AQICN authenticates in the query string. Never log requests or exception URLs.
logging.getLogger("httpx2").disabled = True
logging.getLogger("httpcore2").setLevel(logging.WARNING)


class OutdoorError(Exception):
    MESSAGES = {
        "auth": "AQICN credentials need attention. Update the deployment configuration.",
        "rate_limit": "AQICN is limiting requests. Updates will resume automatically.",
        "configuration": "The AQICN station is unavailable. Choose another station.",
        "temporary": "Outdoor data could not be updated. Updates will retry automatically.",
    }

    def __init__(self, kind="temporary", retry_after=0):
        self.kind = kind
        self.retry_after = retry_after
        super().__init__(self.MESSAGES[kind])


@dataclass
class Measurement:
    metric: str
    recorded_at: float
    value: float


@dataclass
class StationFeed:
    location_id: int
    details: dict
    readings: list[Measurement]


class OutdoorProvider(Protocol):
    retry_at: float

    async def search(self, query: str) -> list[dict]: ...

    async def nearby(self, latitude: float, longitude: float) -> list[dict]: ...

    async def feed(self, location_id: int) -> StationFeed: ...


def safe_url(value):
    try:
        if isinstance(value, str):
            parsed = urlsplit(value)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
                return value
    except ValueError:
        pass
    return None


def retry_seconds(headers) -> float:
    delay = 0.0
    for name in ("Retry-After", "x-ratelimit-reset"):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds):
            delay = max(delay, seconds)
    return max(0, delay)


def aqi_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, OverflowError):
        return None


def measured_at(data):
    try:
        value = data.get("iso") or f"{data['s']}{data['tz']}"
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        recorded = stamp.timestamp()
        if stamp.tzinfo is None or not 0 <= recorded <= time.time() + 60:
            raise OutdoorError()
        return recorded
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise OutdoorError() from None


def coordinates(geo):
    try:
        lat, lon = geo
        if any(isinstance(v, bool) for v in (lat, lon)):
            return None
        lat, lon = float(lat), float(lon)
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return {"latitude": lat, "longitude": lon}
    except (ValueError, TypeError, OverflowError):
        pass
    return None


class AQICNProvider:
    def __init__(self, api_token, transport=None):
        self._api_token = api_token
        self._transport = transport
        self.retry_at = 0.0

    async def _get(self, path, params=None):
        try:
            async with httpx2.AsyncClient(
                base_url="https://api.waqi.info/",
                timeout=15,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    path, params={**(params or {}), "token": self._api_token}
                )
            if response.status_code in {401, 403}:
                raise OutdoorError("auth")
            if response.status_code == 429:
                delay = max(60, retry_seconds(response.headers))
                self.retry_at = time.time() + delay
                raise OutdoorError("rate_limit", delay)
            if response.status_code in {404, 410}:
                raise OutdoorError("configuration")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise OutdoorError()
            if payload.get("status") != "ok":
                error = str(payload.get("data", "")).lower()
                if any(
                    part in error
                    for part in ("invalid key", "invalid token", "expired token", "unknown token")
                ):
                    raise OutdoorError("auth")
                if any(part in error for part in ("over quota", "rate limit", "too many requests")):
                    self.retry_at = time.time() + 60
                    raise OutdoorError("rate_limit", 60)
                if "unknown station" in error:
                    raise OutdoorError("configuration")
                raise OutdoorError()
            return payload["data"]
        except OutdoorError:
            raise
        except Exception:
            raise OutdoorError() from None

    def parse_feed(self, data, location_id=None):
        try:
            station_id = data["idx"]
            if type(station_id) is not int or station_id <= 0:
                raise OutdoorError()
            if location_id is not None and station_id != location_id:
                raise OutdoorError("configuration")
            city = data["city"]
            if not isinstance(city["name"], str) or not city["name"].strip():
                raise OutdoorError()
            overall = aqi_value(data["aqi"])
            if overall is None:
                raise OutdoorError()
            recorded = measured_at(data["time"])
            readings = [Measurement("aqi", recorded, overall)]
            iaqi = data.get("iaqi") or {}
            for metric in METRICS[1:]:
                value = aqi_value((iaqi.get(metric) or {}).get("v"))
                if value is not None:
                    readings.append(Measurement(metric, recorded, value))
            attribution = []
            for item in data.get("attributions") or []:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    attribution.append({"name": item["name"], "url": safe_url(item.get("url"))})
            return StationFeed(
                station_id,
                {
                    "name": city["name"],
                    "url": safe_url(city.get("url")),
                    "coordinates": coordinates(city.get("geo")),
                    "attribution": attribution,
                },
                readings,
            )
        except OutdoorError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise OutdoorError() from None

    async def feed(self, location_id):
        return self.parse_feed(await self._get(f"feed/@{location_id}/"), location_id)

    async def nearby(self, latitude, longitude):
        feed = self.parse_feed(await self._get(f"feed/geo:{latitude};{longitude}/"))
        return [
            {
                "location_id": feed.location_id,
                **feed.details,
                "aqi": feed.readings[0].value,
                "measured_at": datetime.fromtimestamp(
                    feed.readings[0].recorded_at, UTC
                ).isoformat(),
            }
        ]

    async def search(self, query):
        data = await self._get("v2/search/", {"keyword": query})
        if not isinstance(data, list):
            raise OutdoorError()
        stations = []
        seen = set()
        cutoff = (datetime.now(UTC) - timedelta(days=7)).date()
        for item in data:
            try:
                station_id = item["uid"]
                station = item["station"]
                value = aqi_value(item["aqi"])
                stamp = item["time"]["stime"]
                date = datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
                if (
                    type(station_id) is not int
                    or station_id <= 0
                    or station_id in seen
                    or not isinstance(station["name"], str)
                    or not station["name"].strip()
                    or value is None
                    or date < cutoff
                    or date > datetime.now(UTC).date() + timedelta(days=1)
                ):
                    continue
                seen.add(station_id)
                stations.append(
                    {
                        "location_id": station_id,
                        "name": station["name"],
                        "url": safe_url(station.get("url")),
                        "aqi": value,
                        "coordinates": coordinates(station.get("geo")),
                        "updated_at": stamp,
                    }
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        # Keep AQICN's result order; exclude inactive and missing-AQI stations.
        return stations[:20]
