"""Open-Meteo geocoding and weather tool with validated upstream responses."""

from datetime import date
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minimal_agent.tools.base import ToolDefinition, ToolExecutionError, ToolSideEffect

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_CURRENT_VARIABLES = ",".join(
    (
        "temperature_2m",
        "relative_humidity_2m",
        "apparent_temperature",
        "precipitation",
        "weather_code",
        "cloud_cover",
        "wind_speed_10m",
        "wind_direction_10m",
        "is_day",
    )
)
_DAILY_VARIABLES = ",".join(
    (
        "weather_code",
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_probability_max",
        "precipitation_sum",
        "wind_speed_10m_max",
    )
)

_WEATHER_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    97: "Heavy thunderstorm",
    99: "Thunderstorm with heavy hail",
}


class WeatherArguments(BaseModel):
    """Strict tool arguments; nullable options must be sent explicitly."""

    model_config = ConfigDict(extra="forbid")

    location: Annotated[str, Field(min_length=2, max_length=200)]
    forecast_days: Annotated[int, Field(ge=1, le=7)] | None
    temperature_unit: Literal["celsius", "fahrenheit"] | None

    @field_validator("location")
    @classmethod
    def validate_location(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2 or not any(character.isalnum() for character in value):
            raise ValueError("location must contain at least two characters and a letter or digit")
        return value


class _UpstreamModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class GeocodingLocation(_UpstreamModel):
    id: int
    name: Annotated[str, Field(min_length=1)]
    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]
    timezone: Annotated[str, Field(min_length=1)]
    country_code: str | None = None
    country: str | None = None
    admin1: str | None = None


class GeocodingResponse(_UpstreamModel):
    results: list[GeocodingLocation] | None = None


class CurrentUnits(_UpstreamModel):
    temperature_2m: str
    relative_humidity_2m: str
    apparent_temperature: str
    precipitation: str
    weather_code: str
    cloud_cover: str
    wind_speed_10m: str
    wind_direction_10m: str
    is_day: str


class CurrentWeather(_UpstreamModel):
    time: Annotated[str, Field(min_length=1)]
    interval: Annotated[int, Field(gt=0)]
    temperature_2m: float
    relative_humidity_2m: Annotated[float, Field(ge=0, le=100)]
    apparent_temperature: float
    precipitation: Annotated[float, Field(ge=0)]
    weather_code: Annotated[int, Field(ge=0, le=99)]
    cloud_cover: Annotated[float, Field(ge=0, le=100)]
    wind_speed_10m: Annotated[float, Field(ge=0)]
    wind_direction_10m: Annotated[float, Field(ge=0, le=360)]
    is_day: Literal[0, 1]


class DailyUnits(_UpstreamModel):
    time: str
    weather_code: str
    temperature_2m_max: str
    temperature_2m_min: str
    precipitation_probability_max: str
    precipitation_sum: str
    wind_speed_10m_max: str


class DailyWeather(_UpstreamModel):
    time: list[date]
    weather_code: list[Annotated[int, Field(ge=0, le=99)]]
    temperature_2m_max: list[float]
    temperature_2m_min: list[float]
    precipitation_probability_max: list[Annotated[float, Field(ge=0, le=100)]]
    precipitation_sum: list[Annotated[float, Field(ge=0)]]
    wind_speed_10m_max: list[Annotated[float, Field(ge=0)]]

    @model_validator(mode="after")
    def validate_equal_lengths(self) -> "DailyWeather":
        expected = len(self.time)
        lengths = {
            len(self.weather_code),
            len(self.temperature_2m_max),
            len(self.temperature_2m_min),
            len(self.precipitation_probability_max),
            len(self.precipitation_sum),
            len(self.wind_speed_10m_max),
        }
        if lengths != {expected}:
            raise ValueError("daily weather arrays must have equal lengths")
        if expected == 0:
            raise ValueError("daily weather must contain at least one day")
        return self


class ForecastResponse(_UpstreamModel):
    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]
    timezone: Annotated[str, Field(min_length=1)]
    timezone_abbreviation: Annotated[str, Field(min_length=1)]
    utc_offset_seconds: int
    current_units: CurrentUnits
    current: CurrentWeather
    daily_units: DailyUnits
    daily: DailyWeather


class OpenMeteoWeatherClient:
    """Small adapter around an injected HTTP client; it does not own client lifecycle."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        geocoding_url: str = GEOCODING_URL,
        forecast_url: str = FORECAST_URL,
    ) -> None:
        self._http_client = http_client
        self._geocoding_url = geocoding_url
        self._forecast_url = forecast_url

    async def get_weather(self, arguments: WeatherArguments) -> dict[str, object]:
        location = await self._geocode(arguments.location)
        forecast_days = arguments.forecast_days if arguments.forecast_days is not None else 3
        temperature_unit = arguments.temperature_unit or "celsius"
        forecast = await self._forecast(location, forecast_days, temperature_unit)
        return _format_result(arguments.location, location, forecast)

    async def _geocode(self, query: str) -> GeocodingLocation:
        response = await self._get(
            self._geocoding_url,
            service="geocoding",
            params={"name": query, "count": 1, "language": "en", "format": "json"},
        )
        payload = _validate_response(response, GeocodingResponse, service="geocoding")
        if not payload.results:
            raise ToolExecutionError(
                "LOCATION_NOT_FOUND",
                "No matching location was found",
                details={"location": query},
            )
        return payload.results[0]

    async def _forecast(
        self,
        location: GeocodingLocation,
        forecast_days: int,
        temperature_unit: str,
    ) -> ForecastResponse:
        response = await self._get(
            self._forecast_url,
            service="forecast",
            params={
                "latitude": location.latitude,
                "longitude": location.longitude,
                "current": _CURRENT_VARIABLES,
                "daily": _DAILY_VARIABLES,
                "forecast_days": forecast_days,
                "temperature_unit": temperature_unit,
                "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
                "timezone": "auto",
            },
        )
        return _validate_response(response, ForecastResponse, service="forecast")

    async def _get(
        self,
        url: str,
        *,
        service: str,
        params: dict[str, object],
    ) -> httpx.Response:
        try:
            response = await self._http_client.get(url, params=params)
        except httpx.TimeoutException as error:
            raise ToolExecutionError(
                "WEATHER_TIMEOUT",
                "Open-Meteo request timed out",
                retryable=True,
                details={"service": service},
            ) from error
        except httpx.RequestError as error:
            raise ToolExecutionError(
                "WEATHER_UNAVAILABLE",
                "Open-Meteo could not be reached",
                retryable=True,
                details={"service": service},
            ) from error

        if response.status_code == 429:
            raise ToolExecutionError(
                "WEATHER_RATE_LIMITED",
                "Open-Meteo rate limit was reached",
                retryable=True,
                details={"service": service, "status_code": 429},
            )
        if response.is_error:
            raise ToolExecutionError(
                "WEATHER_UPSTREAM_ERROR",
                "Open-Meteo returned an error response",
                retryable=response.status_code >= 500,
                details={"service": service, "status_code": response.status_code},
            )
        return response


def _validate_response[ResponseT: BaseModel](
    response: httpx.Response,
    model: type[ResponseT],
    *,
    service: str,
) -> ResponseT:
    try:
        return model.model_validate_json(response.content)
    except ValueError as error:
        raise ToolExecutionError(
            "INVALID_UPSTREAM_RESPONSE",
            "Open-Meteo returned an invalid response",
            retryable=True,
            details={"service": service},
        ) from error


def _weather_description(code: int) -> str:
    return _WEATHER_DESCRIPTIONS.get(code, "Unknown weather code")


def _format_result(
    query: str,
    location: GeocodingLocation,
    forecast: ForecastResponse,
) -> dict[str, object]:
    current = forecast.current
    current_units = forecast.current_units
    daily = forecast.daily
    daily_units = forecast.daily_units
    daily_forecast = [
        {
            "date": day.isoformat(),
            "weather_code": daily.weather_code[index],
            "weather_description": _weather_description(daily.weather_code[index]),
            "temperature_max": daily.temperature_2m_max[index],
            "temperature_min": daily.temperature_2m_min[index],
            "precipitation_probability_max": daily.precipitation_probability_max[index],
            "precipitation_sum": daily.precipitation_sum[index],
            "wind_speed_max": daily.wind_speed_10m_max[index],
        }
        for index, day in enumerate(daily.time)
    ]

    return {
        "provider": "Open-Meteo",
        "location": {
            "query": query,
            "name": location.name,
            "admin1": location.admin1,
            "country": location.country,
            "country_code": location.country_code,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timezone": forecast.timezone,
            "timezone_abbreviation": forecast.timezone_abbreviation,
            "utc_offset_seconds": forecast.utc_offset_seconds,
        },
        "current": {
            "time": current.time,
            "interval_seconds": current.interval,
            "temperature": current.temperature_2m,
            "apparent_temperature": current.apparent_temperature,
            "relative_humidity": current.relative_humidity_2m,
            "precipitation": current.precipitation,
            "weather_code": current.weather_code,
            "weather_description": _weather_description(current.weather_code),
            "cloud_cover": current.cloud_cover,
            "wind_speed": current.wind_speed_10m,
            "wind_direction_degrees": current.wind_direction_10m,
            "is_day": bool(current.is_day),
        },
        "daily_forecast": daily_forecast,
        "units": {
            "temperature": current_units.temperature_2m,
            "apparent_temperature": current_units.apparent_temperature,
            "relative_humidity": current_units.relative_humidity_2m,
            "precipitation": current_units.precipitation,
            "cloud_cover": current_units.cloud_cover,
            "wind_speed": current_units.wind_speed_10m,
            "wind_direction": current_units.wind_direction_10m,
            "daily_temperature_max": daily_units.temperature_2m_max,
            "daily_temperature_min": daily_units.temperature_2m_min,
            "daily_precipitation_probability": daily_units.precipitation_probability_max,
            "daily_precipitation_sum": daily_units.precipitation_sum,
            "daily_wind_speed_max": daily_units.wind_speed_10m_max,
        },
    }


def create_weather_tool(
    http_client: httpx.AsyncClient,
    *,
    timeout_seconds: float = 10.0,
) -> ToolDefinition[WeatherArguments]:
    """Create a registry-compatible weather tool around an injected HTTP client."""

    client = OpenMeteoWeatherClient(http_client)
    return ToolDefinition(
        name="weather",
        description=(
            "Resolve a place name and return current weather plus a 1-7 day forecast from "
            "Open-Meteo. Use null for forecast_days to request 3 days and null for "
            "temperature_unit to use Celsius."
        ),
        args_model=WeatherArguments,
        handler=client.get_weather,
        timeout_seconds=timeout_seconds,
        side_effect=ToolSideEffect.READ,
    )


def create_open_meteo_http_client(timeout_seconds: float = 5.0) -> httpx.AsyncClient:
    """Create the shared upstream client with explicit per-phase timeouts."""

    if timeout_seconds <= 0:
        raise ValueError("Weather HTTP timeout must be positive")
    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"Accept": "application/json", "User-Agent": "minimal-agent/0.1"},
    )
