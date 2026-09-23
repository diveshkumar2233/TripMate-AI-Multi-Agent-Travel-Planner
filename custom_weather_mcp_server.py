import os
import logging
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

mcp = FastMCP("Weather MCP Server")
logger = logging.getLogger(__name__)

OPENWEATHER_API_KEY = os.getenv(
    "OPENWEATHER_API_KEY"
)

REQUEST_TIMEOUT_SECONDS = 20
WEATHER_UNAVAILABLE = "Live weather currently unavailable. Pack for seasonal averages."


def _get_api_key() -> str:
    if not OPENWEATHER_API_KEY:
        raise RuntimeError(
            "OPENWEATHER_API_KEY is missing "
            "from the project .env file."
        )

    return OPENWEATHER_API_KEY


def _request_json(
    url: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    try:
        response = requests.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        if response.status_code >= 400:
            logger.warning("OpenWeather returned HTTP %s.", response.status_code)
        response.raise_for_status()

        return response.json()

    except Exception as exc:
        # Never include exception text: provider URLs may carry the API key.
        logger.warning("OpenWeather request failed (%s).", type(exc).__name__)
        raise RuntimeError(WEATHER_UNAVAILABLE) from None


@mcp.tool()
def get_current_weather(
    city: str,
) -> dict[str, Any] | str:
    """Return the current weather for a city."""

    try:
        city = city.strip()
        if not city:
            return WEATHER_UNAVAILABLE
        data = _request_json(
            "https://api.openweathermap.org/data/2.5/weather",
            {"q": city, "appid": _get_api_key(), "units": "metric"},
        )
        return {
            "city": data["name"],
            "temperature_c": data["main"]["temp"],
            "feels_like_c": data["main"]["feels_like"],
            "humidity": data["main"]["humidity"],
            "condition": data["weather"][0]["description"],
            "wind_speed": data["wind"]["speed"],
        }
    except Exception:
        return WEATHER_UNAVAILABLE


@mcp.tool()
def get_forecast(
    city: str,
) -> dict[str, Any] | str:
    """
    Return the first five three-hour
    forecast entries for a city.
    """

    try:
        city = city.strip()
        if not city:
            return WEATHER_UNAVAILABLE
        data = _request_json(
            "https://api.openweathermap.org/data/2.5/forecast",
            {"q": city, "appid": _get_api_key(), "units": "metric"},
        )
        forecast = [
            {
                "datetime": item["dt_txt"],
                "temperature_c": item["main"]["temp"],
                "condition": item["weather"][0]["description"],
            }
            for item in data.get("list", [])[:5]
        ]
        return {"city": data.get("city", {}).get("name", city), "forecast": forecast}
    except Exception:
        return WEATHER_UNAVAILABLE


if __name__ == "__main__":
    # mcp_client.py launches this as a stdio subprocess.
    mcp.run(
        transport="stdio",
    )
