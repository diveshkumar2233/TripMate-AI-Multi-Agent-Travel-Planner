import os
import re
from typing import Any

try:
    from tools.tavily_tool import tavily_search as _tavily_search
except Exception:  # pragma: no cover - optional dependency fallback
    _tavily_search = None

try:
    from tools.flight_tool import search_flights as _search_flights
except Exception:  # pragma: no cover - optional dependency fallback
    _search_flights = None


async def tavily_mcp_search(query: str) -> str:
    """Return hotel/search results using the local tavily helper when available."""
    if _tavily_search is not None:
        try:
            return _tavily_search(query)
        except Exception as exc:
            return f"Hotel search unavailable: {exc}"

    return (
        "Hotel search is unavailable because the Tavily API is not configured. "
        "Please add TAVILY_API_KEY to your environment."
    )


async def aviation_mcp_call(method: str, query: str | None = None) -> Any:
    """Provide lightweight aviation data for the travel workflow."""
    if method == "list_airports":
        return [
            {"iata": "DAC", "name": "Hazrat Shahjalal International Airport", "city": "Dhaka", "country": "Bangladesh"},
            {"iata": "NRT", "name": "Narita International Airport", "city": "Tokyo", "country": "Japan"},
            {"iata": "JFK", "name": "John F. Kennedy International Airport", "city": "New York", "country": "United States"},
            {"iata": "LHR", "name": "Heathrow Airport", "city": "London", "country": "United Kingdom"},
            {"iata": "DXB", "name": "Dubai International Airport", "city": "Dubai", "country": "United Arab Emirates"},
        ]

    if method == "list_airlines":
        return [
            {"name": "Biman Bangladesh Airlines", "iata": "BG"},
            {"name": "Japan Airlines", "iata": "JL"},
            {"name": "Emirates", "iata": "EK"},
            {"name": "British Airways", "iata": "BA"},
            {"name": "Qatar Airways", "iata": "QR"},
        ]

    if method == "search_flights":
        if _search_flights is not None:
            try:
                return _search_flights(query or "", limit=5)
            except Exception as exc:
                return f"Flight search unavailable: {exc}"
        return "Flight search is unavailable because the AviationStack API is not configured."

    return {"error": f"Unsupported aviation method: {method}"}


def extract_destination(text: str) -> str:
    """Extract a likely destination city from a user query."""
    if not text:
        return "Dhaka"

    cleaned = text.strip()

    patterns = [
        r"\bto\s+([A-Za-z][A-Za-z\s]+?)(?:\s+from|\s+for|\s+on|\s+in|$)",
        r"\bfor\s+([A-Za-z][A-Za-z\s]+?)(?:\s+trip|\s+travel|\s+vacation|$)",
        r"\bin\s+([A-Za-z][A-Za-z\s]+?)(?:\s+from|\s+for|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            candidate = re.sub(r"\s+", " ", candidate)
            if candidate:
                return candidate

    # Fallback: capture a simple city-like word near the end.
    words = re.findall(r"[A-Za-z]+", cleaned)
    if words:
        return words[-1]

    return "Dhaka"


async def weather_mcp_search(city: str) -> str:
    """Return a simple weather placeholder when no weather provider is configured."""
    city_name = (city or "your destination").strip() or "your destination"
    return (
        f"Weather information for {city_name} is not available yet. "
        "Add a weather provider integration to enable live forecasts."
    )


async def forecast_mcp_search(city: str) -> str:
    """Return a simple forecast placeholder when no weather provider is configured."""
    city_name = (city or "your destination").strip() or "your destination"
    return (
        f"Forecast information for {city_name} is not available yet. "
        "Add a weather provider integration to enable live forecasts."
    )
