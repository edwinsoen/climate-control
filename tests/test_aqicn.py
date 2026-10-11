import time
from copy import deepcopy
from datetime import UTC, datetime

import httpx2
import pytest

from climate_control.providers.aqicn import AQICNProvider, OutdoorError


@pytest.fixture
def feed_payload():
    return {
        "status": "ok",
        "data": {
            "idx": 1,
            "aqi": 42,
            "city": {
                "name": "Singapore",
                "url": "https://aqicn.org/city/singapore/",
                "geo": [1.35, 103.8],
            },
            "time": {"iso": datetime.fromtimestamp(time.time() - 3600, UTC).isoformat()},
            "iaqi": {"pm25": {"v": 42}, "pm10": {"v": 20}, "t": {"v": 30}},
            "attributions": [
                {"name": "EPA", "url": "https://example.com"},
                {"name": "Unsafe link", "url": "javascript:bad"},
            ],
            "forecast": {"daily": {"pm25": [{"avg": 999, "day": "2025-01-01"}]}},
        },
    }


async def test_feed_returns_aqi_not_concentrations_or_forecast_history(feed_payload, caplog):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=feed_payload)

    provider = AQICNProvider("private-token", httpx2.MockTransport(respond))
    with caplog.at_level("DEBUG"):
        feed = await provider.feed(1)
    assert [(r.metric, r.value) for r in feed.readings] == [("aqi", 42), ("pm25", 42), ("pm10", 20)]
    assert feed.readings[0].recorded_at < time.time() - 3500
    assert feed.details["attribution"][1]["url"] is None
    assert requests[0].url.path == "/feed/@1/"
    assert requests[0].url.params["token"] == "private-token"
    assert "private-token" not in caplog.text
    assert "999" not in str(feed.readings)


async def test_search_uses_aqicn_keyword_and_excludes_inactive_or_missing_aqi(caplog):
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "uid": 1,
        "aqi": "42",
        "station": {"name": "Singapore", "geo": [1.35, 103.8]},
        "time": {"stime": now},
    }
    inactive = deepcopy(row)
    inactive.update(uid=2, time={"stime": "2010-01-01 00:00:00"})
    missing = deepcopy(row)
    missing.update(uid=3, aqi="-")
    valid = deepcopy(row)
    valid.update(uid=4, aqi="0")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(
            200, json={"status": "ok", "data": [row, inactive, missing, valid, row]}
        )

    provider = AQICNProvider("private-token", httpx2.MockTransport(respond))
    with caplog.at_level("DEBUG"):
        stations = await provider.search("Singapore")
    assert [s["location_id"] for s in stations] == [1, 4]
    assert stations[1]["aqi"] == 0
    assert requests[0].url.path == "/v2/search/"
    assert requests[0].url.params["keyword"] == "Singapore"
    assert "private-token" not in caplog.text
    assert "Singapore" not in caplog.text


async def test_geolocation_uses_feed_and_accepts_provider_coordinate_strings(feed_payload):
    feed_payload["data"]["city"]["geo"] = ["1.35", "103.8"]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=feed_payload)

    stations = await AQICNProvider("secret", httpx2.MockTransport(respond)).nearby(1.35, 103.8)
    assert stations[0]["location_id"] == 1
    assert stations[0]["coordinates"] == {"latitude": 1.35, "longitude": 103.8}
    assert requests[0].url.path == "/feed/geo:1.35;103.8/"


@pytest.mark.parametrize("value", [True, None, "-", -1, "NaN", "Infinity"])
async def test_missing_or_invalid_overall_aqi_is_a_safe_failure(feed_payload, value):
    feed_payload["data"]["aqi"] = value
    provider = AQICNProvider(
        "secret", httpx2.MockTransport(lambda _: httpx2.Response(200, json=feed_payload))
    )
    with pytest.raises(OutdoorError):
        await provider.feed(1)


@pytest.mark.parametrize("change", ["id", "missing-time", "naive-time", "future-time", "name"])
async def test_malformed_feed_does_not_advance_current_timestamp(feed_payload, change):
    if change == "id":
        feed_payload["data"]["idx"] = 2
    elif change == "missing-time":
        feed_payload["data"]["time"] = {}
    elif change == "naive-time":
        feed_payload["data"]["time"] = {"iso": "2024-01-01T00:00:00"}
    elif change == "future-time":
        feed_payload["data"]["time"] = {"iso": "2999-01-01T00:00:00Z"}
    else:
        feed_payload["data"]["city"]["name"] = ""
    provider = AQICNProvider(
        "secret", httpx2.MockTransport(lambda _: httpx2.Response(200, json=feed_payload))
    )
    with pytest.raises(OutdoorError):
        await provider.feed(1)


async def test_feed_timezone_fallback_and_missing_pollutant_value(feed_payload):
    feed_payload["data"]["time"] = {"s": "2024-01-01 08:00:00", "tz": "+08:00"}
    feed_payload["data"]["iaqi"]["pm25"]["v"] = "-"
    provider = AQICNProvider(
        "secret", httpx2.MockTransport(lambda _: httpx2.Response(200, json=feed_payload))
    )
    feed = await provider.feed(1)
    assert [r.metric for r in feed.readings] == ["aqi", "pm10"]
    assert feed.readings[0].recorded_at == datetime(2024, 1, 1, tzinfo=UTC).timestamp()


@pytest.mark.parametrize(
    "status,body,kind",
    [
        (401, "secret upstream body", "auth"),
        (403, "secret upstream body", "auth"),
        (429, "secret upstream body", "rate_limit"),
        (500, "secret upstream body", "temporary"),
        (200, {"status": "error", "data": "Invalid key: secret"}, "auth"),
        (200, {"status": "error", "data": "Over quota secret"}, "rate_limit"),
        (200, {"status": "error", "data": "Unknown station secret"}, "configuration"),
        (200, {"status": "ok", "data": None}, "temporary"),
    ],
)
async def test_http_and_json_errors_are_redacted_and_classified(status, body, kind):
    response = httpx2.Response(
        status,
        headers={"Retry-After": "120"},
        **({"json": body} if isinstance(body, dict) else {"text": body}),
    )
    provider = AQICNProvider("secret", httpx2.MockTransport(lambda _: response))
    with pytest.raises(OutdoorError) as exc:
        await provider.feed(1)
    assert exc.value.kind == kind
    assert "secret" not in str(exc.value)
    if kind == "rate_limit":
        assert provider.retry_at > time.time() + 59


async def test_empty_search_and_malformed_search_are_distinct():
    responses = [{"status": "ok", "data": []}, {"status": "ok", "data": None}]
    provider = AQICNProvider(
        "secret", httpx2.MockTransport(lambda _: httpx2.Response(200, json=responses.pop(0)))
    )
    assert await provider.search("Unknown") == []
    with pytest.raises(OutdoorError):
        await provider.search("Unknown")
