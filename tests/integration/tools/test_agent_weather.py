from pathlib import Path
from typing import Any

import httpx
import pytest

from minimal_agent.tools import ToolRegistry, create_weather_tool
from tests.integration.tools.helpers import run_tool_scenario


def _geocoding() -> dict[str, Any]:
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


def _forecast() -> dict[str, Any]:
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
            "time": "2026-10-24T14:15",
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
            "time": ["2026-10-24"],
            "weather_code": [2],
            "temperature_2m_max": [20.2],
            "temperature_2m_min": [9.1],
            "precipitation_probability_max": [10],
            "precipitation_sum": [0.0],
            "wind_speed_10m_max": [18.0],
        },
    }


@pytest.mark.asyncio
async def test_agent_weather_success_is_persisted_and_returned_to_model(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            _geocoding() if request.url.host == "geocoding-api.open-meteo.com" else _forecast()
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as weather_client:
        stream, messages, llm = await run_tool_scenario(
            tmp_path,
            tool_name="weather",
            arguments=('{"location":"Beijing","forecast_days":1,"temperature_unit":"celsius"}'),
            registry=ToolRegistry([create_weather_tool(weather_client)]),
        )

    assert "event: tool.completed" in stream
    assert messages[2]["content"]["result"]["data"]["provider"] == "Open-Meteo"
    assert "Beijing" in str(llm.requests[1].input)
