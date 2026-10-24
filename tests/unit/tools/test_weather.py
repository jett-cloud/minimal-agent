from collections.abc import Callable
from typing import Any

import httpx
import pytest

from minimal_agent.tools import (
    ToolRegistry,
    create_open_meteo_http_client,
    create_weather_tool,
)


def geocoding_payload() -> dict[str, Any]:
    return {
        "results": [
            {
                "id": 1816670,
                "name": "Beijing",
                "latitude": 39.9075,
                "longitude": 116.39723,
                "timezone": "Asia/Shanghai",
                "country_code": "CN",
                "country": "China",
                "admin1": "Beijing",
                "population": 11716620,
            }
        ],
        "generationtime_ms": 0.4,
    }


def forecast_payload() -> dict[str, Any]:
    return {
        "latitude": 39.9,
        "longitude": 116.4,
        "generationtime_ms": 0.2,
        "utc_offset_seconds": 28800,
        "timezone": "Asia/Shanghai",
        "timezone_abbreviation": "GMT+8",
        "elevation": 51.0,
        "current_units": {
            "time": "iso8601",
            "interval": "seconds",
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "apparent_temperature": "°C",
            "precipitation": "mm",
            "weather_code": "wmo code",
            "cloud_cover": "%",
            "wind_speed_10m": "km/h",
            "wind_direction_10m": "°",
            "is_day": "",
        },
        "current": {
            "time": "2026-10-22T14:15",
            "interval": 900,
            "temperature_2m": 18.4,
            "relative_humidity_2m": 42,
            "apparent_temperature": 17.1,
            "precipitation": 0.0,
            "weather_code": 2,
            "cloud_cover": 45,
            "wind_speed_10m": 12.3,
            "wind_direction_10m": 315,
            "is_day": 1,
        },
        "daily_units": {
            "time": "iso8601",
            "weather_code": "wmo code",
            "temperature_2m_max": "°C",
            "temperature_2m_min": "°C",
            "precipitation_probability_max": "%",
            "precipitation_sum": "mm",
            "wind_speed_10m_max": "km/h",
        },
        "daily": {
            "time": ["2026-10-22", "2026-10-23", "2026-10-24"],
            "weather_code": [2, 61, 0],
            "temperature_2m_max": [20.2, 17.8, 21.0],
            "temperature_2m_min": [9.1, 8.4, 7.9],
            "precipitation_probability_max": [10, 70, 5],
            "precipitation_sum": [0.0, 4.2, 0.0],
            "wind_speed_10m_max": [18.0, 22.4, 11.5],
        },
    }


async def execute_weather(
    handler: Callable[[httpx.Request], httpx.Response],
    arguments: dict[str, object] | None = None,
):
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, timeout=0.1) as client:
        registry = ToolRegistry([create_weather_tool(client, timeout_seconds=1)])
        return await registry.execute(
            "weather",
            arguments or {"location": "Beijing", "forecast_days": None, "temperature_unit": None},
        )


@pytest.mark.asyncio
async def test_weather_returns_validated_current_and_daily_forecast() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=geocoding_payload())
        return httpx.Response(200, json=forecast_payload())

    result = await execute_weather(handler)

    assert result.ok is True
    assert result.data["provider"] == "Open-Meteo"
    assert result.data["location"]["name"] == "Beijing"
    assert result.data["current"]["weather_description"] == "Partly cloudy"
    assert result.data["daily_forecast"][1]["weather_description"] == "Slight rain"
    assert result.data["units"]["temperature"] == "°C"
    assert len(requests) == 2
    assert requests[0].url.params["name"] == "Beijing"
    assert requests[0].url.params["count"] == "1"
    assert requests[1].url.params["forecast_days"] == "3"
    assert requests[1].url.params["temperature_unit"] == "celsius"
    assert requests[1].url.params["timezone"] == "auto"


@pytest.mark.asyncio
async def test_weather_passes_explicit_days_and_temperature_unit() -> None:
    forecast_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal forecast_request
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=geocoding_payload())
        forecast_request = request
        payload = forecast_payload()
        payload["current_units"]["temperature_2m"] = "°F"
        return httpx.Response(200, json=payload)

    result = await execute_weather(
        handler,
        {"location": "Beijing, China", "forecast_days": 7, "temperature_unit": "fahrenheit"},
    )

    assert result.ok is True
    assert forecast_request is not None
    assert forecast_request.url.params["forecast_days"] == "7"
    assert forecast_request.url.params["temperature_unit"] == "fahrenheit"


@pytest.mark.asyncio
async def test_weather_returns_location_not_found_without_forecast_request() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"generationtime_ms": 0.1})

    result = await execute_weather(handler)

    assert result.error is not None
    assert result.error.code == "LOCATION_NOT_FOUND"
    assert result.error.retryable is False
    assert request_count == 1


@pytest.mark.asyncio
async def test_weather_normalizes_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=geocoding_payload())
        return httpx.Response(429, json={"error": True, "reason": "rate limited"})

    result = await execute_weather(handler)

    assert result.error is not None
    assert result.error.code == "WEATHER_RATE_LIMITED"
    assert result.error.retryable is True
    assert result.error.details == {"service": "forecast", "status_code": 429}


@pytest.mark.asyncio
async def test_weather_normalizes_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="private upstream response")

    result = await execute_weather(handler)

    assert result.error is not None
    assert result.error.code == "WEATHER_UPSTREAM_ERROR"
    assert result.error.retryable is True
    assert result.error.details == {"service": "geocoding", "status_code": 503}
    assert "private" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_weather_normalizes_timeout_without_leaking_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private upstream details", request=request)

    result = await execute_weather(handler)

    assert result.error is not None
    assert result.error.code == "WEATHER_TIMEOUT"
    assert result.error.retryable is True
    assert "private" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_service", ["geocoding", "forecast"])
async def test_weather_rejects_malformed_upstream_responses(malformed_service: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        is_geocoding = request.url.host == "geocoding-api.open-meteo.com"
        if malformed_service == "geocoding" and is_geocoding:
            return httpx.Response(200, json={"results": [{"name": "Missing coordinates"}]})
        if is_geocoding:
            return httpx.Response(200, json=geocoding_payload())
        payload = forecast_payload()
        payload["daily"]["weather_code"] = [2]
        return httpx.Response(200, json=payload)

    result = await execute_weather(handler)

    assert result.error is not None
    assert result.error.code == "INVALID_UPSTREAM_RESPONSE"
    assert result.error.retryable is True
    assert result.error.details == {"service": malformed_service}


@pytest.mark.asyncio
async def test_weather_rejects_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", headers={"content-type": "text/plain"})

    result = await execute_weather(handler)

    assert result.error is not None
    assert result.error.code == "INVALID_UPSTREAM_RESPONSE"


@pytest.mark.asyncio
async def test_weather_validates_arguments_and_exports_strict_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid arguments must not start HTTP requests")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        registry = ToolRegistry([create_weather_tool(client)])
        schema = registry.openai_tools()[0]["parameters"]
        invalid_days = await registry.execute(
            "weather",
            {"location": "Beijing", "forecast_days": 8, "temperature_unit": None},
        )
        invalid_location = await registry.execute(
            "weather",
            {"location": "--", "forecast_days": None, "temperature_unit": None},
        )

    assert schema["required"] == ["location", "forecast_days", "temperature_unit"]
    assert invalid_days.error is not None
    assert invalid_days.error.code == "INVALID_ARGUMENTS"
    assert invalid_location.error is not None
    assert invalid_location.error.code == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_open_meteo_client_factory_sets_explicit_timeouts() -> None:
    client = create_open_meteo_http_client(timeout_seconds=2.5)
    try:
        assert client.timeout.connect == 2.5
        assert client.timeout.read == 2.5
        assert client.timeout.write == 2.5
        assert client.timeout.pool == 2.5
        assert client.follow_redirects is False
        assert client.headers["accept"] == "application/json"
    finally:
        await client.aclose()

    with pytest.raises(ValueError, match="positive"):
        create_open_meteo_http_client(timeout_seconds=0)
