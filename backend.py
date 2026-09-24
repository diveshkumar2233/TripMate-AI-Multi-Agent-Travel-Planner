import os
import certifi
from dotenv import load_dotenv

load_dotenv()

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from typing import TypedDict, Annotated, List
import operator
import uuid
import re
import time
from urllib.parse import quote_plus
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    AIMessage,
    SystemMessage,
)
from langchain_groq import ChatGroq
from groq import APIConnectionError, APIStatusError
from pydantic import BaseModel, Field, model_validator
from tools.tavily_tool import tavily_search
from tools.flight_tool import (
    AIRPORTS,
    DEFAULT_ORIGIN_IATA,
    find_location_mentions,
    parse_route,
    resolve_location_to_iata,
    FLIGHT_UNAVAILABLE,
    search_flights,
)
from custom_weather_mcp_server import get_current_weather, get_forecast


def get_database_url():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return None

    if "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"

    return database_url


GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY is missing. Please add it to your .env file.")


# =========================
# LLM
# =========================

llm = ChatGroq(
    # This is a current Groq production model. Override it in .env when needed.
    # Set GROQ_MODEL in .env to use another model enabled for your account.
    model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
    api_key=GROQ_API_KEY,
    # 600 was too low for longer itineraries (day-by-day tables were
    # getting cut off mid-row). Override with GROQ_MAX_TOKENS in .env
    # if you need more/less headroom.
    max_tokens=int(os.getenv("GROQ_MAX_TOKENS", "3000")),
)

_groq_retry_after = 0.0


class DayPlan(BaseModel):
    day: int = Field(ge=1)
    title: str
    morning: str
    afternoon: str
    evening: str
    pro_tip: str


class TripItineraryOutput(BaseModel):
    target_days: int = Field(ge=1)
    origin: str
    destination: str
    daily_itinerary: List[DayPlan]

    @model_validator(mode="after")
    def validate_day_count(self):
        expected_days = list(range(1, self.target_days + 1))
        actual_days = [item.day for item in self.daily_itinerary]
        if actual_days != expected_days:
            raise ValueError("daily_itinerary must contain exactly target_days, numbered from Day 1")
        return self


def _shorten(value, limit: int = 2500) -> str:
    """Keep external search output from consuming the LLM token budget."""
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}\n[Additional results omitted]"


def _rate_limit_delay(error: Exception) -> float:
    match = re.search(
        r"Please try again in (?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?",
        str(error),
    )
    if not match:
        return 60.0
    return (int(match.group(1) or 0) * 60) + float(match.group(2) or 0) + 1


# =========================
# Result normalization helpers
# =========================
#
# tavily_search() (and similar) can come back in a few different shapes
# depending on the tool version:
#   - a plain string (already-formatted text)
#   - a list of dicts, e.g. [{"title": ..., "url": ..., "content": ...}, ...]
#   - a dict with a "results" key holding that list
#
# Everything below normalizes to a list[dict] with "title"/"url"/"content"
# so we can render clean markdown links instead of a wall of raw text.

def _normalize_search_results(raw) -> list[dict]:
    if raw is None:
        return []

    if isinstance(raw, dict) and "results" in raw:
        raw = raw["results"]

    if isinstance(raw, list):
        normalized = []
        for item in raw:
            if isinstance(item, dict):
                normalized.append({
                    "title": item.get("title") or item.get("name") or "Untitled result",
                    "url": item.get("url") or item.get("link") or "",
                    "content": item.get("content") or item.get("snippet") or "",
                })
            else:
                normalized.append({"title": str(item), "url": "", "content": ""})
        return normalized

    # tavily_search currently formats its result list as numbered markdown.
    # Parse that shape back into records so titles, links, and snippets survive
    # into the hotel recommendations instead of becoming one opaque text blob.
    text = str(raw).strip()
    tavily_items = re.findall(
        r"(?ms)^\s*\d+\.\s+\*\*(.*?)\*\*\s*\n\s*(https?://\S+)\s*\n\s*(.*?)(?=^\s*\d+\.\s+\*\*|\Z)",
        text,
    )
    if tavily_items:
        return [
            {"title": title.strip(), "url": url.strip(), "content": content.strip()}
            for title, url, content in tavily_items
        ]

    # Fallback: treat as a single opaque text blob (older tool versions).
    return [{"title": text, "url": "", "content": ""}] if text else []


def _results_to_markdown_links(results: list[dict], limit: int = 5) -> str:
    """Render normalized results as clean markdown bullet links.

    Falls back to a plain bullet (no link) when a result has no URL, so
    the UI never shows a raw, unclickable source dump.
    """
    if not results:
        return "No results found — check availability directly on a booking site."

    lines = []
    for item in results[:limit]:
        title = item["title"].strip()
        url = item["url"].strip()
        snippet = item["content"].strip()
        snippet = _shorten(snippet, 140) if snippet else ""

        if url:
            line = f"- [{title}]({url})"
        else:
            line = f"- {title}"
        if snippet:
            line += f" — {snippet}"
        lines.append(line)

    return "\n".join(lines)


# =========================
# Trip length / destination extraction (used by the offline fallback)
# =========================

def _extract_trip_days(query: str) -> int | None:
    """Pull an explicit trip length out of the user's text, however it's
    phrased: '18-day', '7 days', '5day trip', '9 nights', '2 weeks'.
    Returns None if nothing usable was stated.

    Trip length is user-declared, so there is no artificial cap here — a
    genuine 60-day backpacking trip gets a full 60-day itinerary, not a
    silently truncated one.
    """
    day_match = re.search(r"(\d{1,3})\s*[-\s]?\s*days?\b", query, re.IGNORECASE)
    if day_match:
        days = int(day_match.group(1))
        return days if days >= 1 else None

    # "9 nights" (and the common "9N/10D" shorthand) means a 10-day trip.
    night_match = re.search(r"(\d{1,3})\s*[-\s]?\s*nights?\b", query, re.IGNORECASE)
    if night_match:
        nights = int(night_match.group(1))
        return nights + 1 if nights >= 1 else None

    week_match = re.search(r"(\d{1,3})\s*[-\s]?\s*weeks?\b", query, re.IGNORECASE)
    if week_match:
        weeks = int(week_match.group(1))
        return weeks * 7 if weeks >= 1 else None

    return None


# Curated real places used when live itinerary generation is unavailable.
# Each tuple is (title, morning, afternoon, evening, pro_tip).
_CURATED_FALLBACK_DAYS = {
    "london": [
        ("Hyde Park & Soho", "Walk through Hyde Park and visit Speakers' Corner.", "Explore Soho's streets and have lunch at Dishoom Soho.", "See a West End show or dine around Chinatown.", "Use an Oyster or contactless card for Tube and bus travel."),
        ("Westminster, London Eye & Borough Market", "See Big Ben, the Houses of Parliament and Westminster Abbey from the outside.", "Ride the London Eye, then cross to Borough Market for lunch.", "Walk the South Bank to Tate Modern, then have dinner at Padella Borough Market.", "Book timed-entry tickets for the London Eye in advance."),
        ("British Museum & Covent Garden", "Visit the British Museum's Egyptian and Greek galleries.", "Walk to Covent Garden, see the market building and street performers.", "Have dinner at Dishoom Covent Garden.", "The British Museum is free; reserve a timed slot when available."),
        ("Tower of London & Leadenhall Market", "Explore the Tower of London and Crown Jewels.", "Walk across Tower Bridge and visit Leadenhall Market.", "Have dinner at Rules in Covent Garden.", "Arrive early at the Tower to avoid the longest queues."),
        ("St Paul's & South Bank", "Visit St Paul's Cathedral and climb to the Whispering Gallery if open.", "Cross the Millennium Bridge to Shakespeare's Globe and Tate Modern.", "Have dinner at Padella near Borough Market.", "Check St Paul's service and visitor hours before going."),
        ("Camden & Regent's Park", "Browse Camden Market and its canal-side stalls.", "Walk through Regent's Park and visit Primrose Hill.", "Have dinner at The Cheese Bar in Camden.", "Camden Market is busiest at weekends; go earlier for space."),
        ("Greenwich", "Take a Thames boat or DLR to Greenwich and visit the Royal Observatory.", "Explore Greenwich Park and Greenwich Market.", "Walk by the Cutty Sark and have pie and mash at Goddards at Greenwich.", "Check the observatory's timed entry and boat timetable."),
    ],
    "paris": [
        ("Eiffel Tower & Trocadéro", "Visit the Eiffel Tower and Champ de Mars.", "Walk across to Trocadéro and have lunch nearby.", "Stroll the Seine and dine in the 7th arrondissement.", "Reserve Eiffel Tower entry ahead of time."),
        ("Louvre & Tuileries", "Visit the Louvre Museum.", "Walk through Jardin des Tuileries to Place de la Concorde.", "Explore Saint-Germain-des-Prés for dinner.", "Choose a few Louvre wings in advance; the museum is very large."),
        ("Île de la Cité & Le Marais", "See Notre-Dame Cathedral and Sainte-Chapelle.", "Walk through Île Saint-Louis into Le Marais.", "Have dinner in Le Marais.", "Reserve Sainte-Chapelle tickets for a timed visit."),
    ],
    "tokyo": [
        ("Arrival in Tokyo & Asakusa", "Arrive at Narita International Airport and take the Narita Express or airport bus into Tokyo.", "Check in, then visit Sensō-ji and Nakamise-dori in Asakusa.", "Have tempura at Daikokuya Tempura in Asakusa.", "Allow extra time for the Narita-to-Tokyo transfer and keep your hotel address available offline."),
        ("Asakusa & Ueno", "Visit Sensō-ji before the busiest hours and walk Nakamise-dori.", "Explore Ueno Park and the Tokyo National Museum.", "Try tonkatsu at Tonkatsu Yamabe near Ueno-Okachimachi.", "Use a Suica or PASMO transit card for Tokyo trains and buses."),
        ("Meiji Shrine, Harajuku & Shibuya", "Walk the forest approach to Meiji Jingu Shrine.", "Browse Takeshita Street and Omotesando in Harajuku.", "See Shibuya Crossing and have dinner at Uobei Shibuya Dogenzaka.", "Visit Shibuya Sky only with a booked entry time."),
        ("Shinjuku Gyoen & Omoide Yokocho", "Walk through Shinjuku Gyoen National Garden.", "See the Tokyo Metropolitan Government Building observatory in Nishi-Shinjuku.", "Explore Omoide Yokocho's small yakitori restaurants.", "Check Shinjuku Gyoen's weekly closing day before visiting."),
        ("Tsukiji, teamLab & Ginza", "Browse the food stalls at Tsukiji Outer Market.", "Visit teamLab Planets TOKYO in Toyosu with a timed ticket.", "Have sushi at Sushi Daiwa in Toyosu or explore Ginza's restaurants.", "Book teamLab entry ahead and check the venue's current admission rules."),
        ("Kamakura Day Trip", "Take the JR Yokosuka Line from Tokyo to Kamakura and visit Tsurugaoka Hachimangu.", "Walk Komachi-dori and visit the Great Buddha at Kōtoku-in.", "See Hasedera Temple before returning to Tokyo.", "Check the last JR train back to Tokyo before starting the day trip."),
        ("Tokyo Station & Departure", "Visit the Imperial Palace East Gardens if your flight schedule allows.", "Collect luggage and travel to Narita International Airport using the Narita Express or airport bus.", "Depart from Narita; keep a meal stop at Tokyo Station only if your transfer buffer allows.", "Confirm your airline terminal and leave central Tokyo with a generous airport buffer."),
    ],    "new delhi": [
        ("Old Delhi & Red Fort", "Visit Jama Masjid and walk through Chandni Chowk.", "Explore the Red Fort and eat at Karim's near Jama Masjid.", "Try parathas at Paranthe Wali Gali in Chandni Chowk.", "Use a licensed guide in the busy Old Delhi lanes."),
        ("Humayun's Tomb & Khan Market", "Visit Humayun's Tomb.", "See India Gate and walk through Lodi Garden.", "Dine at Khan Chacha in Khan Market.", "Carry water and plan outdoor visits for cooler hours."),
        ("Qutub Minar & Hauz Khas", "Explore the Qutub Minar complex.", "Walk around Hauz Khas Village and Deer Park.", "Have dinner at Social in Hauz Khas Village.", "Check monument hours and traffic before setting out."),
        ("Akshardham & Connaught Place", "Visit Swaminarayan Akshardham and its temple complex.", "Walk around Connaught Place and Janpath Market.", "Have dinner at Saravana Bhavan in Connaught Place.", "Check Akshardham's visitor schedule and security rules before travelling."),
        ("National Museum & Lodhi Art District", "Visit the National Museum on Janpath.", "Explore Lodhi Art District and Lodhi Garden.", "Have dinner at Wenger's in Connaught Place or return to Khan Market.", "Group central Delhi stops together to reduce cross-city travel."),
    ],
    "dubai": [
        ("Downtown Dubai", "Visit Burj Khalifa (book an observation deck slot).", "Explore Dubai Mall and the Dubai Aquarium area.", "Watch the Dubai Fountain and dine in Downtown.", "Reserve Burj Khalifa tickets for sunset well ahead."),
        ("Old Dubai & the Creek", "Explore Al Fahidi Historical Neighbourhood.", "Cross Dubai Creek by abra and browse the Gold and Spice Souks.", "Have Emirati food in Al Seef.", "Carry cash for small souk purchases and agree prices clearly."),
        ("Jumeirah", "Visit Jumeirah Mosque on a guided tour.", "Walk the Jumeirah beach area and see Burj Al Arab from public viewpoints.", "Dine at Madinat Jumeirah.", "Dress modestly when visiting religious sites."),
        ("Dubai Frame & Zabeel Park", "Visit Dubai Frame and see old and new Dubai from the observation deck.", "Walk in Zabeel Park and have lunch in Karama.", "Explore Al Seef and dine beside Dubai Creek.", "Check Dubai Frame ticket availability before travelling."),
        ("Dubai Marina & Palm Jumeirah", "Walk the Dubai Marina promenade.", "Visit Palm Jumeirah and the Pointe waterfront.", "Have dinner at a restaurant along Jumeirah Beach Residence (JBR) Walk.", "Use the Dubai Tram and Metro to avoid peak-hour traffic."),
        ("Museum of the Future & DIFC", "Visit the Museum of the Future with a reserved time slot.", "Explore Emirates Towers and the DIFC Gate Village art district.", "Have dinner in DIFC.", "Museum of the Future tickets often need advance booking."),
        ("Desert Conservation Reserve", "Take a guided outing to the Dubai Desert Conservation Reserve.", "Continue the desert excursion and return to the city before evening.", "Dine at Al Seef.", "Book a licensed tour and confirm its pickup point and return time."),
    ],
    "bangkok": [
        ("Grand Palace & Wat Pho", "Visit the Grand Palace and Wat Phra Kaew.", "Walk to Wat Pho and see the Reclining Buddha.", "Have pad thai at Thip Samai on Maha Chai Road.", "Dress modestly at temple sites and check visitor hours."),
        ("Wat Arun & the Chao Phraya", "Take a river ferry to Wat Arun.", "Ride the Chao Phraya Express Boat and explore ICONSIAM.", "Have dinner at Supanniga Eating Room near the river.", "Use the public ferry piers to avoid road traffic."),
        ("Chatuchak & Jim Thompson House", "Browse Chatuchak Weekend Market if it is a weekend; otherwise visit Jim Thompson House.", "Explore the Bangkok Art and Culture Centre near Siam.", "Eat at Somtum Der in Silom.", "Chatuchak is mainly a weekend market; check the day before planning around it."),
        ("Chinatown & Golden Buddha", "Visit Wat Traimit and the Golden Buddha in Yaowarat.", "Walk through Talat Noi and see its street art and old shophouses.", "Try Nai Ek Roll Noodle in Chinatown.", "Go to Yaowarat in the evening for food stalls and lively streets."),
        ("Lumphini Park & Siam", "Walk around Lumphini Park in the morning.", "Visit Siam Paragon and the nearby Siam Square district.", "Have dinner at Baan Somtum in Sathorn.", "Use BTS Skytrain for the Siam and Silom areas."),
        ("Ayutthaya Day Trip", "Take an early train from Krung Thep Aphiwat Central Terminal to Ayutthaya.", "Visit Wat Mahathat and Wat Chaiwatthanaram in Ayutthaya Historical Park.", "Return to Bangkok and dine near your hotel.", "Check the last return train before leaving Bangkok."),
        ("Bang Krachao & Asiatique", "Take a ferry to Bang Krachao and cycle through Sri Nakhon Khuean Khan Park.", "Return to Bangkok and rest along the Chao Phraya.", "Visit Asiatique The Riverfront for dinner and a riverside walk.", "Bring water and confirm ferry times for Bang Krachao."),
    ],
}


def _build_daily_plan_data(days: int, destination: str) -> List[DayPlan]:
    """Create exactly `days` structured, destination-grounded fallback days."""
    day_plans = []
    key = re.sub(r"[^a-z ]", "", destination.lower()).strip()
    key = _LOCATION_ALIASES.get(key, destination).lower()
    if key in {"england", "great britain", "britain", "united kingdom", "uk"}:
        key = "london"
    curated = _CURATED_FALLBACK_DAYS.get(key)
    for day_num in range(1, max(days, 1) + 1):
        if curated:
            values = curated[(day_num - 1) % len(curated)]
        elif day_num == 1:
            values = (
                f"Arrival & {destination} Orientation",
                f"Arrive in {destination}, collect your bags, and transfer to your accommodation.",
                f"Check in and use destination research to choose a named, verified attraction in {destination}.",
                f"Choose a named local restaurant in {destination} from current local recommendations.",
                "Check transfer options and local SIM or transit-card availability before you land.",
            )
        else:
            values = (f"{destination}: local research needed",
                      f"No verified named attraction is available in the local fallback data for {destination}; use current local tourism sources to select one.",
                      f"Choose a specifically named neighborhood and restaurant in {destination} using current local sources.",
                      f"Confirm a named local venue in {destination} before travelling.",
                      "Verify place names, opening days and travel times with local or official sources.")
        title, morning, afternoon, evening, pro_tip = values
        day_plans.append(DayPlan(
            day=day_num,
            title=title,
            morning=morning,
            afternoon=afternoon,
            evening=evening,
            pro_tip=pro_tip,
        ))
    return day_plans


def _daily_budget_breakdowns(days: List[DayPlan], budget_results: str, currency_code: str) -> List[tuple[str, str]]:
    """Allocate the trip estimate by day and show the main expense categories."""
    currency_code = currency_code.upper()
    symbol = _CURRENCY_SYMBOLS.get(currency_code, f"{currency_code} ")
    duration = len(days)
    if not duration:
        return []
    budget_match = re.search(r"Budget range:\s*([\d,]+)-([\d,]+)", budget_results or "", re.I)
    if budget_match:
        trip_low, trip_high = (int(value.replace(",", "")) for value in budget_match.groups())
    else:
        budget_match = re.search(r"Planning budget input:\s*[A-Z]{3}\s+([\d,]+)", budget_results or "", re.I)
        if budget_match:
            trip_low = trip_high = int(budget_match.group(1).replace(",", ""))
        else:
            daily_low, daily_high = _DAILY_BUDGET_RANGES.get(currency_code, (100, 250))
            trip_low, trip_high = daily_low * duration, daily_high * duration

    activity_weights = []
    for day in days:
        text = " ".join((day.title, day.morning, day.afternoon, day.evening, day.pro_tip)).lower()
        weight = 1.0
        if re.search(r"desert safari|day trip|excursion|theme park|skiing|gondola|cable car|cruise|\bflight\b|\bairport\b", text):
            weight += 0.35
        if re.search(r"ticket|museum|guided tour|aquarium|observation deck|boat ride|fort|palace|temple", text):
            weight += 0.18
        if re.search(r"free|walk|park|beach|market|self-guided", text):
            weight -= 0.12
        activity_weights.append(max(weight, 0.7))

    flight_weights = [1.0 if index in {0, duration - 1} else 0.0 for index in range(duration)]
    stay_weights = [1.0 if duration == 1 or index < duration - 1 else 0.0 for index in range(duration)]
    categories = [
        ("Flight share", 35, flight_weights),
        ("Hotel / stay", 25, stay_weights),
        ("Food", 12, activity_weights),
        ("Local transit", 8, activity_weights),
        ("Activities", 15, activity_weights),
        ("Buffer", 5, activity_weights),
    ]
    daily = [{"low": 0, "high": 0, "parts": []} for _ in days]

    def allocate(amount: int, weights: List[float]) -> List[int]:
        active = [index for index, weight in enumerate(weights) if weight > 0]
        total_weight = sum(weights[index] for index in active) or 1
        values = [0] * duration
        allocated = 0
        for index in active[:-1]:
            values[index] = round(amount * weights[index] / total_weight)
            allocated += values[index]
        values[active[-1]] = amount - allocated
        return values

    for label, percent, weights in categories:
        category_low = round(trip_low * percent / 100)
        category_high = round(trip_high * percent / 100)
        lows, highs = allocate(category_low, weights), allocate(category_high, weights)
        for index in range(duration):
            daily[index]["low"] += lows[index]
            daily[index]["high"] += highs[index]
            daily[index]["parts"].append(
                f"{label}: {symbol}{lows[index]:,}–{symbol}{highs[index]:,}"
                if lows[index] != highs[index] else f"{label}: {symbol}{lows[index]:,}"
            )

    result = []
    for item in daily:
        total = f"{symbol}{item['low']:,}–{symbol}{item['high']:,}" if item["low"] != item["high"] else f"{symbol}{item['low']:,}"
        result.append((total, " · ".join(item["parts"])))
    return result


def _daily_plan_markdown(
    days: List[DayPlan], budget_results: str = "", currency_code: str = "USD", total_days: int | None = None
) -> str:
    daily_budgets = _daily_budget_breakdowns(days, budget_results, currency_code)

    return "\n\n".join(
        f"### Day {item.day}: {item.title}\n"
        f"- **Morning:** {item.morning}\n"
        f"- **Afternoon:** {item.afternoon}\n"
        f"- **Evening:** {item.evening}\n"
        f"- **Pro-Tip:** {item.pro_tip}\n"
        f"- **Estimated daily budget:** {daily_budgets[index][0]} (approximate allocation)\n"
        f"- **Daily cost breakdown:** {daily_budgets[index][1]}\n"
        f"  Prices vary by dates, actual bookings, and travel style."
        for index, item in enumerate(days)
    )


_GENERIC_ITINERARY_PHRASES = (
    "top landmark", "food market", "well-reviewed option", "nearby attraction",
    "local restaurant", "leading museum", "second cultural site", "major attraction",
    "local favorite", "near your hotel", "nearby walk", "local meal",
    "day-trip destination", "popular local market", "regional specialty",
)


def _itinerary_day_needs_grounding(day: DayPlan, index: int, destination: str) -> bool:
    content = " ".join((day.title, day.morning, day.afternoon, day.evening, day.pro_tip)).lower()
    if any(phrase in content for phrase in _GENERIC_ITINERARY_PHRASES):
        return True
    if destination.lower() == "london":
        required_places = {
            0: ("hyde park", "soho"),
            1: ("big ben", "london eye", "borough market"),
            2: ("british museum", "covent garden"),
        }
        return any(place not in content for place in required_places.get(index, ()))
    required_by_city = {
        "tokyo": (
            ("sensō-ji", "asakusa"), ("ueno", "tokyo national museum"),
            ("meiji jingu", "shibuya crossing"), ("shinjuku gyoen", "omoide yokocho"),
            ("tsukiji", "teamlab"), ("kamakura", "kōtoku-in"),
            ("narita international airport",),
        ),
        "new delhi": (
            ("red fort", "chandni chowk"), ("humayun's tomb", "khan market"),
            ("qutub minar",), ("akshardham", "connaught place"),
            ("national museum", "lodhi"),
        ),
        "dubai": (
            ("burj khalifa",), ("al fahidi",), ("jumeirah mosque",),
            ("dubai frame",), ("dubai marina",), ("museum of the future",),
            ("dubai desert conservation reserve",),
        ),
        "bangkok": (
            ("grand palace", "wat pho"), ("wat arun",), ("chatuchak", "jim thompson house"),
            ("wat traimit", "yaowarat"), ("lumphini park",), ("ayutthaya",), ("bang krachao",),
        ),
    }
    required_places = required_by_city.get(destination.lower(), ())
    if index < len(required_places):
        return any(place not in content for place in required_places[index])
    return False


def _day_block(day_num: int, title: str, morning: str, afternoon: str, evening: str, protip: str) -> str:
    return (
        f"### Day {day_num}: {title}\n"
        f"- **Morning:** {morning}\n"
        f"- **Afternoon:** {afternoon}\n"
        f"- **Evening:** {evening}\n"
        f"- **Pro-Tip:** {protip}"
    )


def _build_day_by_day(days: int, destination: str, budget_results: str = "", currency_code: str = "USD") -> str:
    """Build exact-length destination-grounded days for the offline response."""
    if not days or days < 1:
        return ""
    return _daily_plan_markdown(_build_daily_plan_data(days, destination), budget_results, currency_code, days)


# ---- Logistics + highlights lookup for the Executive Summary --------

_LOGISTICS_BY_DESTINATION = {
    "Tokyo": {"currency": "Japanese Yen (JPY)", "timezone": "UTC+9"},
    "Bangkok": {"currency": "Thai Baht (THB)", "timezone": "UTC+7"},
    "Dubai": {"currency": "UAE Dirham (AED)", "timezone": "UTC+4"},
    "New Delhi": {"currency": "Indian Rupee (INR)", "timezone": "UTC+5:30"},
    "Paris": {"currency": "Euro (EUR)", "timezone": "UTC+1"},
    "London": {"currency": "British Pound (GBP)", "timezone": "UTC+0 (UTC+1 in summer)"},
    "Rome": {"currency": "Euro (EUR)", "timezone": "UTC+1"},
    "Madrid": {"currency": "Euro (EUR)", "timezone": "UTC+1"},
    "Singapore": {"currency": "Singapore Dollar (SGD)", "timezone": "UTC+8"},
    "Bali": {"currency": "Indonesian Rupiah (IDR)", "timezone": "UTC+8"},
    "Istanbul": {"currency": "Turkish Lira (TRY)", "timezone": "UTC+3"},
}

_HIGHLIGHTS_BY_DESTINATION = {
    "Tokyo": ["Senso-ji Temple", "Shibuya Crossing", "Mount Fuji day trip", "Sushi & ramen culture"],
    "Bangkok": ["Grand Palace", "Floating markets", "Street food scene", "Wat Arun temple"],
    "Dubai": ["Burj Khalifa", "Desert safari", "Dubai Mall & Fountain", "Palm Jumeirah"],
    "New Delhi": ["Red Fort", "India Gate", "Qutub Minar", "Chandni Chowk street food"],
    "Paris": ["Eiffel Tower", "Louvre Museum", "Seine river cruise", "Montmartre"],
    "London": ["Big Ben & Parliament", "British Museum", "Tower of London", "West End theatre"],
    "Rome": ["Colosseum", "Vatican Museums", "Trevi Fountain", "Roman street food"],
    "Madrid": ["Prado Museum", "Royal Palace", "Retiro Park", "Tapas culture"],
    "Singapore": ["Gardens by the Bay", "Marina Bay Sands", "Sentosa Island", "Hawker food centers"],
    "Bali": ["Tanah Lot Temple", "Rice terraces", "Uluwatu sunset", "Beach clubs"],
    "Istanbul": ["Hagia Sophia", "Blue Mosque", "Grand Bazaar", "Bosphorus cruise"],
}
_GENERIC_HIGHLIGHTS = ["Top local landmarks", "Local cuisine", "Culture & history", "Leisure time"]


def _budget_amount(budget_results: str) -> int | None:
    """Return a supplied single-currency budget amount when present."""
    match = re.search(r"Planning budget input:\s*[A-Z]{3}\s+([\d,]+)", budget_results or "", re.I)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _executive_summary_section(destination: str, days: int | None, budget_results: str, currency_code: str = "USD") -> str:
    duration_line = f"{days} days / {max(days - 1, 0)} nights" if days else "Duration not specified"
    amount = _budget_amount(budget_results)
    symbol = _CURRENCY_SYMBOLS.get(currency_code, currency_code + " ")
    if amount:
        budget_line = f"{symbol}{amount:,}"
    else:
        low, high = _DAILY_BUDGET_RANGES.get(currency_code, (100, 250))
        duration = max(days or 1, 1)
        budget_line = f"{symbol}{low * duration:,} - {symbol}{high * duration:,} estimated"
    highlights = _HIGHLIGHTS_BY_DESTINATION.get(destination, _GENERIC_HIGHLIGHTS)
    return f"## Executive Summary\n- **Destination:** {destination}\n- **Duration:** {duration_line}\n- **Budget Allocation:** {budget_line}\n- **Key Highlights:** {', '.join(highlights[:4])}"


# ---- Synthesis helpers: turn raw research into prose, not link dumps -

def _plain_from_markdown_links(markdown_text: str, limit: int = 3) -> list[str]:
    """Strip '[Title](url) — snippet' bullets down to plain 'Title —
    snippet' text, so a section can synthesize research into prose
    instead of repeating a raw link dump."""
    if not markdown_text:
        return []
    plain = []
    for line in markdown_text.splitlines():
        line = line.strip().lstrip("- ").strip()
        if not line:
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        plain.append(line)
        if len(plain) >= limit:
            break
    return plain


def _flight_search_link(origin: str, destination: str) -> str:
    origin_code = re.search(r"\b[A-Z]{3}\b", origin or "")
    destination_code = re.search(r"\b[A-Z]{3}\b", destination or "")
    route = f"{origin_code.group(0) if origin_code else origin} to {destination_code.group(0) if destination_code else destination}"
    return "https://www.google.com/travel/flights?q=" + quote_plus(f"flights from {route}")


def _flight_recommendations_section(flight_results: str, destination: str, origin: str = "your city") -> str:
    if isinstance(flight_results, dict):
        flight_results = str(flight_results.get("message", FLIGHT_UNAVAILABLE["message"]))
    lowered_flights = (flight_results or "").lower()
    if "access_key" in lowered_flights or any(
        marker in lowered_flights
        for marker in ("httpsconnectionpool", "flight api request failed", "connection error")
    ):
        flight_results = FLIGHT_UNAVAILABLE["message"]
    if (flight_results or "").startswith(("Live flight details unavailable.", "Live flight details offline.")):
        return (
            f"## Flight Recommendations\n- **Route:** {origin} -> {destination}\n"
            "- **Live status:** The flight-tracking provider returned no route records.\n"
            f"- **Compare current schedules and fares:** [Search this route on Google Flights]({_flight_search_link(origin, destination)})\n"
            "- Select your travel dates there; fares, connections, and airline availability depend on the dates."
        )

    records = [block.strip() for block in re.split(r"\n\s*---\s*\n", flight_results or "") if "Airline:" in block]
    if records:
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = []
        for block in records[:5]:
            fields, section = {}, ""
            for raw_line in block.splitlines():
                line = raw_line.strip().lstrip("- ")
                if line in ("Departure:", "Arrival:"):
                    section = line[:-1].lower()
                elif ":" in line:
                    key, value = line.split(":", 1)
                    field_name = key.strip().lower()
                    fields[f"{section}_{field_name}" if section else field_name] = value.strip()
            route = f"{fields.get('departure_iata', '?')} -> {fields.get('arrival_iata', '?')}"
            lines.append(
                f"- **{fields.get('airline', 'Airline unavailable')} {fields.get('flight', '')}** | "
                f"Date: {fields.get('flight date', 'not provided')} | Route: {route} | "
                f"Status: {fields.get('status', 'unknown')} | "
                f"Departs: {fields.get('departure_scheduled local time', 'not provided')} | "
                f"Arrives: {fields.get('arrival_scheduled local time', 'not provided')}"
            )
        return (
            f"## Flight Recommendations\n- **Requested route:** {origin} -> {destination}\n"
            f"- **Provider checked:** {checked_at} (UTC; status may change)\n" + "\n".join(lines) +
            "\n- **Fare note:** Flight tracking data is not a ticket quote. Confirm date, times and fares with the airline before booking."
        )

    plain = _plain_from_markdown_links(flight_results, limit=5)
    note = " / ".join(plain) if plain else "No matching live flight records were returned."
    return (
        f"## Flight Recommendations\n- **Route:** {origin} -> {destination}\n"
        f"- **Live status:** {_shorten(note, 500)}\n"
        f"- **Compare current schedules and fares:** [Search this route on Google Flights]({_flight_search_link(origin, destination)})\n"
        "- Confirm dates, baggage rules, connections, and final price before booking."
    )


_HOTEL_TIER_LABELS = ["Budget", "Mid-Range", "Luxury"]
_LUXURY_HOTEL_KEYWORDS = ["luxury", "five-star", "5-star", "resort", "grand", "palace", "ritz", "hyatt",
                          "st. regis", "four seasons", "shangri-la", "taj", "oberoi"]
_BUDGET_HOTEL_KEYWORDS = ["hostel", "budget", "backpacker", "guesthouse", "guest house", "inn", "dorm", "capsule"]


def _classify_hotel_tier(name: str) -> str:
    """Guess a tier from the listing's own title so a hostel never gets
    mislabeled 'Luxury' just because of where it happened to appear in
    the search results."""
    lower = name.lower()
    if any(k in lower for k in _BUDGET_HOTEL_KEYWORDS):
        return "Budget"
    if any(k in lower for k in _LUXURY_HOTEL_KEYWORDS):
        return "Luxury"
    return "Mid-Range"


_ACCOMMODATION_TIER_LABELS = ["Budget / Backpacker", "Mid-Range / Comfort", "Luxury / Premium"]
_HOTEL_NEIGHBORHOODS = {
    "london": ("Paddington / King's Cross hostel area", "Bloomsbury / Kensington", "Mayfair / South Bank"),
    "paris": ("Latin Quarter / Canal Saint-Martin", "Le Marais / Opéra", "Saint-Germain-des-Prés / 7th arrondissement"),
    "tokyo": ("Ueno / Asakusa", "Shinjuku / Shibuya", "Ginza / Marunouchi"),
    "dubai": ("Deira / Bur Dubai", "Al Barsha / Business Bay", "Downtown Dubai / Dubai Marina"),
    "new delhi": ("Paharganj / Karol Bagh", "Connaught Place / South Delhi", "Aerocity / Lutyens' Delhi"),
    "bangkok": ("Khao San Road / Silom", "Sukhumvit / Siam", "Riverside / Sukhumvit"),
    "thailand": ("Khao San Road / Silom", "Sukhumvit / Siam", "Riverside / Sukhumvit"),
    "singapore": ("Little India / Geylang", "Bugis / Chinatown", "Marina Bay / Orchard"),
    "rome": ("Termini / San Lorenzo", "Monti / Prati", "Centro Storico / Via Veneto"),
    "madrid": ("Lavapiés / La Latina", "Malasaña / Retiro", "Salamanca / Gran Vía"),
    "bali": ("Kuta / Canggu", "Ubud / Seminyak", "Nusa Dua / Uluwatu"),
    "istanbul": ("Sirkeci / Aksaray", "Sultanahmet / Galata", "Beşiktaş / Bosphorus shore"),
}


def _hotel_recommendations_section(hotel_results: str, destination: str) -> str:
    entries = [
        line.strip() for line in (hotel_results or "").splitlines()
        if line.strip().startswith("-")
        and not re.search(r"no results found|check availability directly", line, re.IGNORECASE)
    ][:4]
    lines = ["## \U0001f3e8 Accommodation Recommendations", f"Options researched for **{destination}**:"]
    neighborhoods = _HOTEL_NEIGHBORHOODS.get(destination.lower().strip())
    if entries:
        lines.extend(entries)
    if neighborhoods:
        labels = ("Budget-friendly base", "Convenient sightseeing base", "Upscale base")
        for label, area in zip(labels, neighborhoods):
            map_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"hotels in {area}, {destination}")
            lines.append(f"- **{label}:** {area} — [see current hotel listings]({map_url})")
    else:
        map_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"hotels in {destination}")
        lines.append(f"- **Browse stays:** [See current hotels in {destination}]({map_url})")
    if not entries:
        lines.append("- Live property search returned no verified names or prices; the links above open current map listings for comparison.")
    lines.append("- Before booking, compare total cost with taxes, transit access, recent guest reviews, and cancellation terms.")
    return "\n".join(lines)

def _weather_packing_section(weather_results: str, destination: str) -> str:
    weather_line = weather_results.strip() if weather_results else (
        f"Seasonal weather data for {destination} is unavailable right now; check a forecast closer to your travel dates."
    )
    climate_packing = {
        "dubai": "Breathable clothing, sun protection, and a light layer for strong indoor air-conditioning.",
        "bangkok": "Breathable clothing, sun protection, and a compact rain jacket or umbrella for tropical showers.",
        "london": "A light waterproof layer, comfortable shoes, and an extra layer for changeable weather.",
        "paris": "Comfortable walking shoes and a light layer; check the forecast before packing for rain.",
        "tokyo": "Comfortable walking shoes, a compact umbrella, and layers suited to your travel month.",
    }.get(destination.lower().strip())
    packing_tip = f"\n  4. {climate_packing}" if climate_packing else ""
    return (
        "## Weather & Packing Guide\n"
        f"- **Forecast:** {_shorten(weather_line, 400)}\n"
        "- **Packing Checklist:**\n"
        "  1. Comfortable walking shoes\n"
        f"  2. Weather-appropriate layers for {destination}\n"
        f"  3. Universal power adapter / power bank{packing_tip}"
    )


_BUDGET_SPLIT = [
    ("Flights", 35), ("Hotels / Stay", 25), ("Food & Local Transport", 20),
    ("Sightseeing & Activities", 15), ("Emergency / Miscellaneous", 5),
]


_CURRENCY_SYMBOLS = {"BDT": "\u09f3", "INR": "\u20b9", "USD": "$", "GBP": "\u00a3", "EUR": "\u20ac", "AED": "AED ", "JPY": "\u00a5", "CAD": "C$", "AUD": "A$", "SGD": "S$", "THB": "\u0e3f", "IDR": "Rp ", "TRY": "\u20ba"}
_DAILY_BUDGET_RANGES = {"BDT": (12000, 25000), "INR": (8000, 20000), "USD": (100, 250), "GBP": (90, 220), "EUR": (85, 210), "AED": (350, 850), "JPY": (12000, 30000), "CAD": (130, 300), "AUD": (140, 320), "SGD": (130, 300), "THB": (2500, 6500), "IDR": (900000, 2300000), "TRY": (2500, 6500)}


def _budget_table_section(budget_results: str, currency_code: str = "USD", days: int = 1) -> str:
    currency_code = currency_code.upper()
    symbol = _CURRENCY_SYMBOLS.get(currency_code, f"{currency_code} ")
    match = re.search(r"Budget range:\s*([\d,]+)-([\d,]+)", budget_results or "", re.I)
    if match:
        minimum, maximum = (int(value.replace(",", "")) for value in match.groups())
    else:
        amount_match = re.search(r"Planning budget input:\s*[A-Z]{3}\s+([\d,]+)", budget_results or "", re.I)
        if amount_match:
            minimum = maximum = int(amount_match.group(1).replace(",", ""))
        else:
            daily_min, daily_max = _DAILY_BUDGET_RANGES.get(currency_code, (100, 250))
            minimum, maximum = daily_min * max(days, 1), daily_max * max(days, 1)

    def money(value: int) -> str:
        return f"{symbol}{value:,}"

    rows = ["## Estimated Cost Breakdown", "", f"| Category | Cost Range ({currency_code}) | % of Total Budget |", "| :--- | :--- | :--- |"]
    for label, pct in _BUDGET_SPLIT:
        cost_cell = f"{money(int(minimum * pct / 100))} - {money(int(maximum * pct / 100))}"
        rows.append(f"| {label} | {cost_cell} | {pct}% |")
    rows.append(f"| **Total Estimated Cost** | **{money(minimum)} - {money(maximum)}** | **100%** |")
    return "\n".join(rows)


def _budget_snapshot_section(budget_results: str, currency_code: str = "USD", days: int = 1) -> str:
    """Give the traveler a quick total and daily budget beside the itinerary."""
    currency_code = currency_code.upper()
    symbol = _CURRENCY_SYMBOLS.get(currency_code, f"{currency_code} ")
    duration = max(days, 1)
    match = re.search(r"Budget range:\s*([\d,]+)-([\d,]+)", budget_results or "", re.I)
    if match:
        low, high = (int(value.replace(",", "")) for value in match.groups())
        total_label = f"{symbol}{low:,}–{symbol}{high:,}"
        daily_label = f"{symbol}{low // duration:,}–{symbol}{high // duration:,}"
        context = "Estimated trip total and per-day average"
    else:
        match = re.search(r"Planning budget input:\s*[A-Z]{3}\s+([\d,]+)", budget_results or "", re.I)
        if match:
            total = int(match.group(1).replace(",", ""))
            total_label = f"{symbol}{total:,}"
            daily_label = f"{symbol}{total // duration:,}"
            context = "Your stated trip budget and per-day average"
        else:
            low, high = _DAILY_BUDGET_RANGES.get(currency_code, (100, 250))
            total_label = f"{symbol}{low * duration:,}–{symbol}{high * duration:,}"
            daily_label = f"{symbol}{low:,}–{symbol}{high:,}"
            context = "Estimated trip total and per-day average"
    return (
        "## Budget Snapshot\n"
        f"- **{context}:** {total_label} total for {duration} days ({daily_label} per day).\n"
        "- Keep a 10–15% buffer for price changes and unexpected costs; flights and lodging can vary most by date."
    )


def _final_recommendations_section(destination: str) -> str:
    neighborhoods = _HOTEL_NEIGHBORHOODS.get(destination.lower().strip())
    area_tip = (
        f"For lodging, compare {neighborhoods[0]} for value with {neighborhoods[1]} for sightseeing access."
        if neighborhoods else
        "Choose lodging near the places you plan to visit and check the nearest transit stop before booking."
    )
    return (
        "## Final Recommendations\n"
        f"- {area_tip}\n"
        "- Book cancellable lodging and transport first, then reserve timed-entry attractions; recheck prices and opening hours for your dates.\n"
        "- Keep the daily plan flexible: group nearby sights together and leave one lighter block for delays or rest."
    )



def offline_itinerary(state) -> str:
    """Provide a useful plan while the external LLM is temporarily
    unavailable. Mirrors the exact section order/headers the live LLM is
    instructed to produce (Executive Summary → Day-by-Day → Flight
    Recommendations → Accommodation → Weather & Packing → Cost
    Breakdown), synthesized from research instead of dumping raw links."""
    query = state["user_query"]
    days = state.get("target_days") or _extract_trip_days(query) or 1
    route = _extract_trip_route(query)
    destination = _destination_from_query(query)
    origin = state.get("origin") or route["origin"]

    if days:
        day_section = f"## 📅 Day-by-Day Detailed Itinerary\n\n{_build_day_by_day(days, destination, state.get('budget_results', ''), state.get('currency', 'USD'))}"
    else:
        day_section = """## 📅 Day-by-Day Detailed Itinerary

### Day 1: Arrival
- **Morning:** Transfer to your hotel and check in.
- **Afternoon:** Orientation walk near your hotel.
- **Evening:** Casual dinner close by to recover from travel.
- **Pro-Tip:** Add a trip length (e.g. "5 days") to your request for a full Day 1-to-N breakdown.

*(Middle days rotate sightseeing, culture, food and rest by neighborhood; the final day keeps a buffer before departure.)*"""

    return f"""# ✈️ TripPilot AI: Complete Travel Plan

{_executive_summary_section(destination, days, state.get('budget_results', ''), state.get('currency', 'USD'))}

---

{_budget_snapshot_section(state.get('budget_results', ''), state.get('currency', 'USD'), days)}

---

{day_section}

---

{_flight_recommendations_section(state.get('flight_results', ''), route['destination'], origin)}

---

{_hotel_recommendations_section(state.get('hotel_results', ''), destination)}

---

{_weather_packing_section(state.get('weather_results', ''), destination)}

---

{_budget_table_section(state.get('budget_results', ''), state.get('currency', 'USD'), state.get('target_days', days))}

---

{_final_recommendations_section(destination)}

_The live AI itinerary service is temporarily rate-limited. This is a synthesized fallback plan — verify all prices, availability, and timings before booking._"""


def invoke_llm(messages, fallback_text: str) -> AIMessage:
    """Return a useful response when a Groq model is unavailable or rate-limited."""
    global _groq_retry_after

    remaining = _groq_retry_after - time.monotonic()
    if remaining > 0:
        return AIMessage(
            content=(
                f"{fallback_text}\n\n"
                f"Groq's token quota is temporarily exhausted. Please try again "
                f"in about {max(1, round(remaining / 60))} minute(s)."
            ),
            additional_kwargs={"groq_fallback": True},
        )

    try:
        return llm.invoke(messages)
    except APIConnectionError as exc:
        print(
            f"GROQ CONNECTION ERROR ({type(exc).__name__}); using the offline itinerary fallback.",
            flush=True,
        )
        return AIMessage(
            content=fallback_text,
            additional_kwargs={"groq_fallback": True},
        )
    except APIStatusError as exc:
        if getattr(exc, "status_code", None) == 429:
            _groq_retry_after = time.monotonic() + _rate_limit_delay(exc)
        print(f"GROQ API ERROR: {exc}", flush=True)
        return AIMessage(
            content=(
                f"{fallback_text}\n\n"
                "The configured Groq model is unavailable for this API key or "
                "the account has reached its rate limit. Set GROQ_MODEL in .env "
                "to a model available to your account, then restart the server."
            ),
            additional_kwargs={"groq_fallback": True},
        )


# =========================
# State
# =========================

class TravelState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    user_query: str
    flight_results: str
    hotel_results: str
    weather_results: str
    restaurant_results: str
    budget_results: str
    itinerary: str
    itinerary_data: dict
    target_days: int
    origin: str
    destination: str
    currency: str
    llm_calls: int


# =========================
# Flight Agent
# =========================

def flight_agent(state: TravelState):
    query = state["user_query"]
    try:
        raw_flight_data = search_flights(query)
    except Exception:
        raw_flight_data = FLIGHT_UNAVAILABLE

    # search_flights() may return a plain status string (e.g. "Live route
    # lookup...") or a list of flight-option dicts, depending on the tool
    # version. Normalize the same way hotel/restaurant results are, so
    # structured results render as clean markdown instead of a raw dump —
    # and a plain string simply passes through untouched.
    if isinstance(raw_flight_data, dict) and raw_flight_data.get("status") == "offline":
        flight_results = str(raw_flight_data.get("message", FLIGHT_UNAVAILABLE["message"]))
    elif isinstance(raw_flight_data, (list, dict)):
        results = _normalize_search_results(raw_flight_data)
        flight_results = _results_to_markdown_links(results, limit=5)
    else:
        flight_results = str(raw_flight_data).strip() or "No live flight data available — check fares directly with an airline or OTA."

    lowered_flights = flight_results.lower()
    if "access_key" in lowered_flights or any(
        marker in lowered_flights
        for marker in ("httpsconnectionpool", "flight api request failed", "connection error")
    ):
        flight_results = FLIGHT_UNAVAILABLE["message"]

    return {
        "flight_results": flight_results,
        "messages": [
            AIMessage(content="Flight results fetched.")
        ],
        "llm_calls": state.get("llm_calls", 0) + 1
    }



# =========================
# Hotel Agent
# =========================

def hotel_agent(state: TravelState):
    destination = _destination_from_query(state["user_query"])
    preferences = re.sub(r"\b(?:plan|planning|trip|travel|itinerary|hotel|hotels|stay|accommodation|including|with|for)\b", " ", state["user_query"], flags=re.IGNORECASE)
    preferences = re.sub(r"\s+", " ", preferences).strip(" ,.-")
    query = f"best places to stay hotels in {destination} {preferences} neighborhoods price recent reviews"
    try:
        raw_results = tavily_search(query)
    except Exception:
        raw_results = []

    results = _normalize_search_results(raw_results)
    hotel_results = _results_to_markdown_links(results, limit=5)

    return {
        "hotel_results": hotel_results,
        "messages": [
            AIMessage(content="Hotel information fetched.")
        ],
        "llm_calls": state.get("llm_calls", 0) + 1
    }


# Country names AND common city names both map here, because users often
# type the city itself ("Dubai trip") rather than the country ("UAE trip").
_LOCATION_ALIASES = {
    "japan": "Tokyo", "tokyo": "Tokyo",
    "thailand": "Bangkok", "bangkok": "Bangkok",
    "uae": "Dubai", "dubai": "Dubai",
    "india": "New Delhi", "delhi": "New Delhi", "new delhi": "New Delhi",
    "france": "Paris", "paris": "Paris",
    "uk": "London", "united kingdom": "London", "great britain": "London", "britain": "London", "england": "London", "london": "London",
    "italy": "Rome", "rome": "Rome",
    "spain": "Madrid", "madrid": "Madrid",
    "singapore": "Singapore",
    "indonesia": "Bali", "bali": "Bali",
    "turkey": "Istanbul", "istanbul": "Istanbul",
}


def _clean_location_phrase(value: str) -> str:
    value = re.split(
        r"\s+(?:for|under|with|including|budget|on|during|travellers?|travelers?|itinerary|trip|travel|plan)\b|[,;.!?]",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return value.strip(" \t-")


def _extract_trip_route(query: str) -> dict[str, str]:
    """Extract route labels and IATA codes, using the default only without an origin."""
    stop = r"(?=\s+(?:for|under|with|including|budget|on|during)\b|[.!?;]|$)"
    origin_name = destination_name = None

    match = re.search(rf"\bfrom\s+(.+?)\s+to\s+(.+?){stop}", query, re.IGNORECASE)
    if match:
        origin_name = _clean_location_phrase(match.group(1))
        destination_name = _clean_location_phrase(match.group(2))
    else:
        match = re.search(rf"\bto\s+(.+?)\s+from\s+(.+?){stop}", query, re.IGNORECASE)
        if match:
            destination_name = _clean_location_phrase(match.group(1))
            origin_name = _clean_location_phrase(match.group(2))

    if not origin_name:
        match = re.search(rf"\bfrom\s+(.+?){stop}", query, re.IGNORECASE)
        if match:
            origin_name = _clean_location_phrase(match.group(1))
            prior_places = find_location_mentions(query[:match.start()])
            if prior_places:
                destination_name = prior_places[-1].title()

    if not destination_name:
        match = re.search(rf"\bto\s+(.+?){stop}", query, re.IGNORECASE)
        if match:
            destination_name = _clean_location_phrase(match.group(1))

    if not destination_name:
        places = find_location_mentions(query)
        if places:
            destination_name = places[-1].title()
            if not origin_name and len(places) > 1:
                origin_name = places[0].title()

    origin_code = resolve_location_to_iata(origin_name) if origin_name else None
    destination_code = resolve_location_to_iata(destination_name) if destination_name else None
    if not origin_name and destination_name:
        origin_code = DEFAULT_ORIGIN_IATA
        origin_name = AIRPORTS.get(origin_code, {}).get("city") or "Delhi"

    def label(name: str | None, code: str | None) -> str:
        if name and re.fullmatch(r"[A-Za-z]{3}", name.strip()):
            code = code or name.upper()
            name = AIRPORTS.get(code, {}).get("city") or name.upper()
        if not name and code:
            name = AIRPORTS.get(code, {}).get("city") or code
        if not name:
            return "Not specified"
        return f"{name.strip().title()} ({code})" if code else name.strip().title()

    return {
        "origin_name": origin_name or "",
        "destination_name": destination_name or "",
        "origin_code": origin_code or "",
        "destination_code": destination_code or "",
        "origin": label(origin_name, origin_code),
        "destination": label(destination_name, destination_code),
    }



_CURRENCY_BY_COUNTRY = {"BD": "BDT", "IN": "INR", "US": "USD", "GB": "GBP", "UK": "GBP", "IE": "EUR", "FR": "EUR", "DE": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR", "PT": "EUR", "AE": "AED", "JP": "JPY", "CA": "CAD", "AU": "AUD", "SG": "SGD", "TH": "THB", "ID": "IDR", "TR": "TRY", "CH": "CHF", "NZ": "NZD", "MY": "MYR", "PH": "PHP", "CN": "CNY", "KR": "KRW", "LK": "LKR", "NP": "NPR", "PK": "PKR", "ZA": "ZAR"}


def _origin_currency(route: dict[str, str]) -> str:
    country = AIRPORTS.get(route.get("origin_code", ""), {}).get("country", "")
    return _CURRENCY_BY_COUNTRY.get(str(country).upper(), "USD")


_CURRENCY_REQUESTS = {
    "BDT": ("BDT", "৳", "taka", "bangladeshi taka"),
    "INR": ("INR", "₹", "rupee", "rupees"),
    "USD": ("USD", "$", "dollar", "dollars"),
    "GBP": ("GBP", "£", "pound", "pounds"),
    "EUR": ("EUR", "€", "euro", "euros"),
    "AED": ("AED", "dirham", "dirhams"), "JPY": ("JPY", "¥", "yen"),
    "CAD": ("CAD", "canadian dollar"), "AUD": ("AUD", "australian dollar"),
    "SGD": ("SGD", "singapore dollar"), "THB": ("THB", "baht"),
    "IDR": ("IDR", "rupiah"), "TRY": ("TRY", "lira"), "CHF": ("CHF", "swiss franc"),
    "NZD": ("NZD", "new zealand dollar"), "CNY": ("CNY", "yuan", "renminbi"),
    "KRW": ("KRW", "won"), "MYR": ("MYR", "ringgit"), "PHP": ("PHP", "peso", "pesos"),
    "LKR": ("LKR",), "NPR": ("NPR",), "PKR": ("PKR",), "ZAR": ("ZAR",),
}


def _requested_currency(query: str, route: dict[str, str]) -> str:
    """Use an explicitly requested currency, else infer from the origin."""
    lowered = query.lower()
    for code, names in _CURRENCY_REQUESTS.items():
        for name in names:
            if name.isalpha() and len(name) <= 4:
                found = re.search(rf"\b{re.escape(name.lower())}\b", lowered)
            else:
                found = name.lower() in lowered
            if found:
                return code
    return _origin_currency(route)


def _destination_from_query(query: str) -> str:
    route = _extract_trip_route(query)
    if route["destination_name"]:
        destination_name = route["destination_name"].strip().lower()
        return _LOCATION_ALIASES.get(destination_name, route["destination_name"].title())
    """Pick the DESTINATION city out of a query that usually also mentions
    the origin ("... trip from Delhi"), without depending on the exact
    ordering find_location_mentions() happens to return.

    Strategy: find every known place name's *position* directly in the
    query text, then skip whichever one falls inside a "from <origin>"
    phrase. This is what fixes "Dubai trip from Delhi" resolving to
    "Delhi" (wrong, that's the origin) instead of "Dubai" (right).
    """
    q = query.lower()

    origin_match = re.search(r"from\s+([a-z][a-z\s]{1,25})", q)
    origin_text = origin_match.group(1) if origin_match else ""

    candidates = []
    for name, canonical in _LOCATION_ALIASES.items():
        idx = q.find(name)
        if idx != -1:
            candidates.append((idx, name, canonical))
    candidates.sort()  # order of first appearance in the query

    if candidates:
        for idx, name, canonical in candidates:
            if origin_text and name in origin_text:
                continue  # this one is the origin, not the destination
            return canonical
        # Every known place looked like the origin (rare) — fall back to
        # the first one found rather than "your destination".
        return candidates[0][2]

    mentions = find_location_mentions(query)
    return mentions[-1].title() if mentions else "your destination"


def weather_agent(state: TravelState):
    city = _destination_from_query(state["user_query"])
    current = None
    forecast_data = None
    try:
        result = get_current_weather(city)
        if isinstance(result, dict):
            current = result
    except Exception:
        current = None
    try:
        result = get_forecast(city)
        if isinstance(result, dict):
            forecast_data = result
    except Exception:
        forecast_data = None

    if not current:
        weather_results = "Live weather currently unavailable. Pack for seasonal averages."
    else:
        forecast = (forecast_data or {}).get("forecast", [])
        forecast_text = "; ".join(
            f"{item.get('datetime', 'Upcoming')}: {item.get('temperature_c', '?')}?C, {item.get('condition', 'conditions unavailable')}"
            for item in forecast[:3]
        ) or "Forecast unavailable; current conditions were retrieved."
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        weather_results = (
            f"Live OpenWeather update for {current.get('city', city)} (checked {checked_at}): "
            f"{current.get('temperature_c', '?')}?C (feels {current.get('feels_like_c', '?')}?C), "
            f"{current.get('condition', 'conditions unavailable')}, humidity {current.get('humidity', '?')}%.\n"
            f"Near-term forecast: {forecast_text}"
        )

    return {
        "weather_results": weather_results,
        "messages": [AIMessage(content="Weather research completed.")],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }


# =========================
# Restaurant Agent
# =========================

def restaurant_agent(state: TravelState):
    """Research well-reviewed restaurants for the destination, same pattern
    as hotel_agent: normalize whatever tavily_search returns and render as
    clean, clickable markdown links instead of a raw text dump."""
    destination = _destination_from_query(state["user_query"])
    query = f"Best restaurants to eat in {destination}"
    try:
        raw_results = tavily_search(query)
    except Exception:
        raw_results = []

    results = _normalize_search_results(raw_results)
    restaurant_results = _results_to_markdown_links(results, limit=5)

    return {
        "restaurant_results": restaurant_results,
        "messages": [AIMessage(content="Restaurant research completed.")],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }


def budget_agent(state: TravelState):
    query = state["user_query"]
    currency = _requested_currency(query, _extract_trip_route(query))
    lakh_match = re.search(r"([\d.]+)\s*(?:lakhs?|lacs?)\b", query, re.IGNORECASE)
    amount_match = re.search(r"(?:\u20b9|\u09f3|\$|\u00a3|\u20ac|\u00a5|\b(?:BDT|INR|USD|GBP|EUR|AED|JPY|CAD|AUD|SGD|THB|IDR|TRY|CHF|NZD|CNY|KRW|MYR|PHP|LKR|NPR|PKR|ZAR)\b|rs\.?|budget(?: of| is|:)?\s*)\s*([\d,]+)", query, re.IGNORECASE)
    if lakh_match:
        amount = int(float(lakh_match.group(1)) * 100000)
        budget_results = f"Planning budget input: {currency} {amount:,}."
    elif amount_match:
        amount = int(amount_match.group(1).replace(",", ""))
        budget_results = f"Planning budget input: {currency} {amount:,}."
    else:
        days = state.get("target_days") or _extract_trip_days(query) or 1
        low, high = _DAILY_BUDGET_RANGES.get(currency, (100, 250))
        budget_results = f"Budget range: {low * days:,}-{high * days:,} ({currency}) for {days} days."
    budget_results += "\nSuggested split: flights 35-45%, hotel 30-40%, food/local transport 15-20%, activities 10-15%, plus a 10-15% contingency.\nThis is a planning estimate, not a live price quote."
    return {"budget_results": budget_results, "messages": [AIMessage(content="Budget research completed.")], "llm_calls": state.get("llm_calls", 0) + 1}



# =========================
# Itinerary Agent
# =========================

# Structured itinerary prompt and node.
TRIPPILOT_SYSTEM_PROMPT = """You are a global travel itinerary planner.
Return a structured itinerary for any valid city or country worldwide.
Use the supplied origin and destination exactly; never substitute a familiar
city. Create exactly target_days daily entries, numbered consecutively from
1 through target_days. Every day must contain a useful Morning, Afternoon,
Evening, and Pro-Tip. Do not collapse, omit, or summarize any requested day.
Every activity must name a real attraction, neighborhood, market, park, or
restaurant located in the requested destination. Never use generic filler such
as "top landmark", "food market", "well-reviewed option", "nearby attraction",
or "local restaurant". Use supplied research and established place names; never
claim a venue is open or available without evidence. If no named place is
grounded in the context, state local verification is needed rather than inventing.
For London/England, include Hyde Park and Soho on Day 1; Big Ben, London Eye,
and Borough Market on Day 2; British Museum and Covent Garden on Day 3 when
those days are requested. Avoid inventing live prices, hours, or availability.
For Delhi/New Delhi, name real places on every day; include Red Fort and
Chandni Chowk, Humayun's Tomb and Khan Market, Qutub Minar, and when those days
are requested Akshardham, Connaught Place, National Museum, and Lodhi Art
District. Use real streets and eateries instead of generic food descriptions.
For Japan requests resolved to Tokyo, use named Tokyo venues each day; include
Sensō-ji and Asakusa, Ueno Park and Tokyo National Museum, Meiji Jingu and
Shibuya Crossing, Shinjuku Gyoen and Omoide Yokocho, Tsukiji Outer Market and
teamLab Planets, and use Kamakura and Narita International Airport on the
final days when those days are requested.
"""


def itinerary_agent(state: TravelState):
    query = state["user_query"]
    target_days = _extract_trip_days(query) or state.get("target_days") or 1
    route = _extract_trip_route(query)
    origin = state.get("origin") or route["origin"]
    destination = state.get("destination") or route["destination"]
    destination_name = _destination_from_query(query)

    fallback_days = _build_daily_plan_data(target_days, destination_name)
    output = None
    prompt = f"""Create a day-by-day plan for this request.

Required values (do not change these):
- target_days: {target_days}
- origin: {origin}
- destination: {destination}

Use specific real places in the requested destination; no generic template activities. For London, Day 1 includes Hyde Park and Soho; Day 2 includes Big Ben, London Eye and Borough Market; Day 3 includes British Museum and Covent Garden. For New Delhi, name real venues every day, including Red Fort and Chandni Chowk, Humayun's Tomb and Khan Market, Qutub Minar, and then Akshardham, Connaught Place, National Museum, or Lodhi Art District on later days. For Tokyo, use the day-specific named Tokyo and Kamakura places from the system instructions.

The daily_itinerary must contain exactly {target_days} items, with day values
1 through {target_days}. Every item needs a title, morning, afternoon, evening,
and pro_tip. Support the destination even if it is not a major tourist city.

User request: {query}

Research context:
Flights: {_shorten(state.get('flight_results', ''))}
Hotels: {_shorten(state.get('hotel_results', ''))}
Weather: {_shorten(state.get('weather_results', ''))}
Restaurants: {_shorten(state.get('restaurant_results', ''))}
Budget: {_shorten(state.get('budget_results', ''))}
"""
    try:
        if time.monotonic() >= _groq_retry_after:
            output = llm.with_structured_output(TripItineraryOutput).invoke([
                SystemMessage(content=TRIPPILOT_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ])
            if isinstance(output, dict):
                output = TripItineraryOutput.model_validate(output)
    except Exception as exc:
        print(
            f"Structured itinerary generation unavailable ({type(exc).__name__}); using the exact-length fallback.",
            flush=True,
        )
        output = None

    generated_days = output.daily_itinerary if isinstance(output, TripItineraryOutput) else []
    daily_itinerary = list(generated_days[:target_days])
    daily_itinerary.extend(fallback_days[len(daily_itinerary):target_days])
    for index, day in enumerate(daily_itinerary):
        if _itinerary_day_needs_grounding(day, index, destination_name):
            daily_itinerary[index] = fallback_days[index]
    daily_itinerary = [
        item.model_copy(update={"day": index})
        for index, item in enumerate(daily_itinerary, start=1)
    ]

    plan = TripItineraryOutput(
        target_days=target_days,
        origin=origin,
        destination=destination,
        daily_itinerary=daily_itinerary,
    )
    itinerary_data = plan.model_dump()
    answer = f"""# TripPilot AI: Complete Travel Plan

## Executive Summary
- **Origin:** {plan.origin}
- **Destination:** {plan.destination}
- **Duration:** {plan.target_days} days

{_budget_snapshot_section(state.get('budget_results', ''), state.get('currency', 'USD'), plan.target_days)}

## Day-by-Day Detailed Itinerary

{_daily_plan_markdown(plan.daily_itinerary, state.get('budget_results', ''), state.get('currency', 'USD'), plan.target_days)}

---

{_flight_recommendations_section(state.get('flight_results', ''), plan.destination, plan.origin)}

---

{_hotel_recommendations_section(state.get('hotel_results', ''), destination_name)}

---

{_weather_packing_section(state.get('weather_results', ''), destination_name)}

---

{_budget_table_section(state.get('budget_results', ''), state.get('currency', 'USD'), plan.target_days)}

---

{_final_recommendations_section(destination_name)}
"""

    return {
        "itinerary": answer,
        "itinerary_data": itinerary_data,
        "messages": [AIMessage(content=answer)],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }


# =========================
# Final Response Agent
# =========================

def final_agent(state: TravelState):
    return {
        # The itinerary agent already produces the plan. Avoid a second, nearly
        # identical LLM request that doubles token consumption.
        "messages": [AIMessage(content=state["itinerary"])],
        "llm_calls": state.get("llm_calls", 0)
    }


# =========================
# Build Graph
# =========================

graph = StateGraph(TravelState)

graph.add_node("flight_agent", flight_agent)
graph.add_node("hotel_agent", hotel_agent)
graph.add_node("weather_agent", weather_agent)
graph.add_node("restaurant_agent", restaurant_agent)
graph.add_node("budget_agent", budget_agent)
graph.add_node("itinerary_agent", itinerary_agent)
graph.add_node("final_agent", final_agent)

graph.add_edge(START, "flight_agent")
graph.add_edge("flight_agent", "hotel_agent")
graph.add_edge("hotel_agent", "weather_agent")
graph.add_edge("weather_agent", "restaurant_agent")
graph.add_edge("restaurant_agent", "budget_agent")
graph.add_edge("budget_agent", "itinerary_agent")
graph.add_edge("itinerary_agent", "final_agent")
graph.add_edge("final_agent", END)


# =========================
# PostgreSQL Checkpointer
# =========================
DATABASE_URL = get_database_url()
_conn = None

if DATABASE_URL:
    try:
        _conn = psycopg.connect(
            DATABASE_URL,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=5,
        )
        checkpointer = PostgresSaver(_conn)
        checkpointer.setup()
    except psycopg.OperationalError as exc:
        print(
            "Warning: PostgreSQL is unavailable; using temporary in-memory "
            f"state instead. ({exc})",
            flush=True,
        )
        checkpointer = MemorySaver()
else:
    print(
        "Warning: DATABASE_URL is not set; using temporary in-memory state instead.",
        flush=True,
    )
    checkpointer = MemorySaver()

travel_graph = graph.compile(checkpointer=checkpointer)



# =========================
# Function for FastAPI
# =========================

def run_travel_agent(user_input: str, thread_id: str | None = None):
    if not thread_id:
        thread_id = f"user_{uuid.uuid4().hex}"

    route = _extract_trip_route(user_input)
    target_days = _extract_trip_days(user_input) or 1

    config = {
        "configurable": {
            "thread_id": thread_id
        }
    }

    result = travel_graph.invoke(
        {
            "messages": [
                HumanMessage(content=user_input)
            ],
            "user_query": user_input,
            "flight_results": "",
            "hotel_results": "",
            "weather_results": "",
            "restaurant_results": "",
            "budget_results": "",
            "itinerary": "",
            "itinerary_data": {},
            "target_days": target_days,
            "origin": route["origin"],
            "destination": route["destination"],
            "currency": _requested_currency(user_input, route),
            "llm_calls": 0
        },
        config=config
    )

    final_answer = result["messages"][-1].content

    return {
        "thread_id": thread_id,
        "answer": final_answer,
        "flight_results": result.get("flight_results", ""),
        "hotel_results": result.get("hotel_results", ""),
        "weather_results": result.get("weather_results", ""),
        "restaurant_results": result.get("restaurant_results", ""),
        "budget_results": result.get("budget_results", ""),
        "itinerary": result.get("itinerary", ""),
        "target_days": result.get("target_days", target_days),
        "origin": result.get("origin", route["origin"]),
        "destination": result.get("destination", route["destination"]),
        "currency": result.get("currency", _requested_currency(user_input, route)),
        "itinerary_data": result.get("itinerary_data", {}),
        "llm_calls": result.get("llm_calls", 0),
    }


# =====================================================================
# DESTINATION CATALOG — structured, click-a-destination trip planner
# =====================================================================
#
# This is a SEPARATE flow from the free-text chat agent above. Instead of
# a user typing "plan a 7-day Japan trip", they pick a destination card
# and TripPilot deterministically builds a full structured plan matching
# the app's data schema (flights, hotels, attractions, restaurants,
# transportation, day-wise itinerary, budget, best time to visit, tips).
#
# IMPORTANT — data accuracy (per product spec, section 20):
# No live flight/hotel booking API is wired up yet. Every generated
# flight and hotel entry is clearly labelled "estimated" and must never
# be shown to the user as a bookable, confirmed fare. Attractions are
# real, well-known landmarks (general knowledge, not live data).
# Restaurants use live web search when available and are labelled
# accordingly; otherwise they fall back to a clearly-labelled cuisine
# suggestion instead of inventing a specific restaurant name/rating.

from datetime import date, timedelta


def _unsplash_image(keywords: str, width: int = 1600, height: int = 900) -> str:
    """Destination-aware image URL with no API key required.

    source.unsplash.com/{w}x{h}/-{keywords} redirects to a random photo
    matching the given keywords, which is enough to make every
    destination/hotel/attraction visually distinct without needing a
    paid image API. Swap this for a real Unsplash/Pexels API call
    (using their documented search endpoint + your own API key) when
    you're ready for production-grade, guaranteed-relevant images.
    """
    safe_keywords = keywords.replace(" ", ",")
    return f"https://source.unsplash.com/{width}x{height}/-{safe_keywords}"


# ---- Destination master data ---------------------------------------
# One entry per destination. Keep this the single source of truth —
# the UI never hardcodes destination facts; it reads everything from
# here (and the builder functions below) via build_trip_plan().

DESTINATIONS: dict[str, dict] = {
    "usa": {
        "name": "USA", "country": "United States", "capital": "Washington, D.C.",
        "currency": "USD", "language": "English", "timezone": "UTC-5 to UTC-10 (varies by state)",
        "hero_keywords": "new york city skyline statue of liberty",
        "description": "From New York's skyline to the Grand Canyon, the USA packs city energy, national parks and coastline into one trip.",
        "best_months": ["March", "April", "May", "September", "October", "November"],
        "peak_months": ["June", "July", "August"], "low_months": ["January", "February"],
        "budget_min_inr": 130000, "budget_max_inr": 260000, "default_nights": 7,
        "attractions": [
            ("Statue of Liberty", "Iconic harbor monument and symbol of the USA.", "$25", "2-3 hours"),
            ("Times Square", "Neon-lit heart of Manhattan with shows and shopping.", "Free to visit", "1-2 hours"),
            ("Grand Canyon National Park", "Vast, layered desert canyon carved by the Colorado River.", "$35/vehicle", "Half day"),
            ("Golden Gate Bridge", "San Francisco's landmark suspension bridge.", "Free to visit", "1-2 hours"),
        ],
    },
    "dubai": {
        "name": "Dubai", "country": "United Arab Emirates", "capital": "Abu Dhabi",
        "currency": "AED", "language": "Arabic (English widely spoken)", "timezone": "UTC+4",
        "hero_keywords": "dubai burj khalifa skyline",
        "description": "Dubai mixes record-breaking skyscrapers, desert adventure and duty-free shopping in a compact, easy-to-navigate city.",
        "best_months": ["November", "December", "January", "February", "March"],
        "peak_months": ["December", "January"], "low_months": ["June", "July", "August"],
        "budget_min_inr": 70000, "budget_max_inr": 150000, "default_nights": 5,
        "attractions": [
            ("Burj Khalifa", "World's tallest building with an observation deck.", "~$40", "2 hours"),
            ("Dubai Mall & Dubai Fountain", "Mega-mall with an aquarium and choreographed fountain shows.", "Free to visit", "2-3 hours"),
            ("Palm Jumeirah", "Palm-shaped artificial island with resorts and beach clubs.", "Free to visit", "Half day"),
            ("Desert Safari (Al Marmoom/Lahbab)", "Dune bashing, camel rides and a Bedouin-style dinner.", "~$60", "5-6 hours"),
        ],
    },
    "paris": {
        "name": "Paris", "country": "France", "capital": "Paris",
        "currency": "EUR", "language": "French", "timezone": "UTC+1",
        "hero_keywords": "paris eiffel tower",
        "description": "Paris pairs world-class art and architecture with cafe culture along the Seine.",
        "best_months": ["April", "May", "June", "September", "October"],
        "peak_months": ["July", "August"], "low_months": ["January", "February"],
        "budget_min_inr": 120000, "budget_max_inr": 230000, "default_nights": 6,
        "attractions": [
            ("Eiffel Tower", "Paris's defining 19th-century iron landmark.", "~€28 (summit)", "2 hours"),
            ("Louvre Museum", "World's largest art museum, home to the Mona Lisa.", "~€22", "3-4 hours"),
            ("Notre-Dame Cathedral (exterior)", "Gothic cathedral on Île de la Cité, still under restoration.", "Free (exterior)", "1 hour"),
            ("Montmartre & Sacré-Cœur", "Hilltop artist quarter with sweeping city views.", "Free to visit", "2-3 hours"),
        ],
    },
    "london": {
        "name": "London", "country": "United Kingdom", "capital": "London",
        "currency": "GBP", "language": "English", "timezone": "UTC+0/+1 (BST)",
        "hero_keywords": "london big ben skyline",
        "description": "London blends royal history, world-class museums and a buzzing food and theatre scene.",
        "best_months": ["May", "June", "September"],
        "peak_months": ["July", "August"], "low_months": ["December", "January", "February"],
        "budget_min_inr": 130000, "budget_max_inr": 250000, "default_nights": 6,
        "attractions": [
            ("Big Ben & Houses of Parliament", "London's most photographed landmark on the Thames.", "Free (exterior)", "1 hour"),
            ("Tower of London", "Historic castle housing the Crown Jewels.", "~£33", "2-3 hours"),
            ("British Museum", "Free world-history museum with the Rosetta Stone.", "Free (donation)", "2-3 hours"),
            ("London Eye", "Giant observation wheel on the South Bank.", "~£30", "1 hour"),
        ],
    },
    "switzerland": {
        "name": "Switzerland", "country": "Switzerland", "capital": "Bern",
        "currency": "CHF", "language": "German/French/Italian", "timezone": "UTC+1",
        "hero_keywords": "swiss alps mountains lake",
        "description": "Switzerland is scenic-train country: glacier peaks, alpine lakes and postcard villages.",
        "best_months": ["June", "July", "August", "December", "January"],
        "peak_months": ["July", "August"], "low_months": ["November"],
        "budget_min_inr": 180000, "budget_max_inr": 320000, "default_nights": 6,
        "attractions": [
            ("Jungfraujoch", "\"Top of Europe\" rail station amid glaciers.", "~CHF 220 (round trip)", "Full day"),
            ("Lake Lucerne", "Alpine lake ringed by mountains and lakeside towns.", "Free to visit", "Half day"),
            ("Matterhorn / Zermatt", "Switzerland's most famous peak and car-free village.", "Cable car ~CHF 220", "Full day"),
            ("Rhine Falls", "Largest plain waterfall in Europe, near Schaffhausen.", "Free (boat extra)", "2 hours"),
        ],
    },
    "japan": {
        "name": "Japan", "country": "Japan", "capital": "Tokyo",
        "currency": "JPY", "language": "Japanese", "timezone": "UTC+9",
        "hero_keywords": "tokyo mount fuji",
        "description": "Japan balances ultramodern Tokyo, temple-filled Kyoto and Mount Fuji's classic silhouette.",
        "best_months": ["March", "April", "October", "November"],
        "peak_months": ["April"], "low_months": ["June"],
        "budget_min_inr": 140000, "budget_max_inr": 260000, "default_nights": 8,
        "attractions": [
            ("Senso-ji Temple, Asakusa", "Tokyo's oldest and most iconic Buddhist temple.", "Free to visit", "1-2 hours"),
            ("Mount Fuji (Fuji Five Lakes)", "Japan's tallest peak with classic lake-reflection views.", "Free to view", "Full day"),
            ("Fushimi Inari Shrine, Kyoto", "Thousands of vermilion torii gates up the mountainside.", "Free to visit", "2-3 hours"),
            ("Shibuya Crossing & Shibuya Sky", "World-famous scramble crossing with a rooftop observation deck.", "~¥2,000 (Sky)", "1-2 hours"),
        ],
    },
    "singapore": {
        "name": "Singapore", "country": "Singapore", "capital": "Singapore",
        "currency": "SGD", "language": "English/Malay/Mandarin/Tamil", "timezone": "UTC+8",
        "hero_keywords": "singapore marina bay sands gardens by the bay",
        "description": "Singapore is a compact, ultra-clean city-state with futuristic gardens and world-class food courts.",
        "best_months": ["February", "March", "April"], "peak_months": ["December"], "low_months": ["November"],
        "budget_min_inr": 90000, "budget_max_inr": 170000, "default_nights": 5,
        "attractions": [
            ("Gardens by the Bay", "Futuristic Supertree Grove and climate-domed conservatories.", "~S$20 (domes)", "3-4 hours"),
            ("Marina Bay Sands SkyPark", "Rooftop infinity pool and observation deck.", "~S$32", "1-2 hours"),
            ("Sentosa Island", "Beach resort island with theme parks and Universal Studios.", "Varies by attraction", "Full day"),
            ("Merlion Park", "Iconic half-lion, half-fish statue on Marina Bay.", "Free to visit", "30-45 min"),
        ],
    },
    "thailand": {
        "name": "Thailand", "country": "Thailand", "capital": "Bangkok",
        "currency": "THB", "language": "Thai", "timezone": "UTC+7",
        "hero_keywords": "bangkok temple tropical beach thailand",
        "description": "Thailand mixes Bangkok's temples and street food with the beaches and islands further south.",
        "best_months": ["November", "December", "January", "February"],
        "peak_months": ["December"], "low_months": ["June", "September"],
        "budget_min_inr": 55000, "budget_max_inr": 120000, "default_nights": 6,
        "attractions": [
            ("Grand Palace & Wat Phra Kaew, Bangkok", "Former royal residence and Thailand's most sacred temple.", "~500 THB", "2-3 hours"),
            ("Wat Arun (Temple of Dawn)", "Riverside temple famous for its porcelain-inlaid spire.", "~100 THB", "1 hour"),
            ("Phi Phi Islands", "Limestone-cliff islands with turquoise water, day trip from Phuket/Krabi.", "Tour ~1,000-1,500 THB", "Full day"),
            ("Chatuchak Weekend Market, Bangkok", "One of the world's largest weekend markets.", "Free to browse", "2-3 hours"),
        ],
    },
    "bali": {
        "name": "Bali", "country": "Indonesia", "capital": "Jakarta (Bali capital: Denpasar)",
        "currency": "IDR", "language": "Indonesian/Balinese", "timezone": "UTC+8",
        "hero_keywords": "bali beach temple rice terrace",
        "description": "Bali blends terraced rice fields, sea temples and surf beaches with a relaxed island pace.",
        "best_months": ["April", "May", "June", "September"], "peak_months": ["July", "August"], "low_months": ["January"],
        "budget_min_inr": 65000, "budget_max_inr": 130000, "default_nights": 6,
        "attractions": [
            ("Tanah Lot Temple", "Sea temple perched on a rock, famous for sunset views.", "~IDR 60,000", "1-2 hours"),
            ("Tegallalang Rice Terraces", "Iconic stepped green rice paddies near Ubud.", "Donation-based", "1-2 hours"),
            ("Uluwatu Temple", "Clifftop temple with a traditional Kecak fire dance at sunset.", "~IDR 50,000", "2-3 hours"),
            ("Mount Batur Sunrise Trek", "Guided pre-dawn volcano hike for sunrise views.", "Tour ~IDR 350,000", "Half day"),
        ],
    },
    "australia": {
        "name": "Australia", "country": "Australia", "capital": "Canberra",
        "currency": "AUD", "language": "English", "timezone": "UTC+8 to UTC+10.5",
        "hero_keywords": "sydney opera house harbour",
        "description": "Australia covers Sydney's harbour icons, the Great Barrier Reef and vast outback scenery.",
        "best_months": ["September", "October", "November", "March"], "peak_months": ["December", "January"], "low_months": ["July"],
        "budget_min_inr": 180000, "budget_max_inr": 320000, "default_nights": 9,
        "attractions": [
            ("Sydney Opera House & Harbour Bridge", "Australia's most photographed landmark duo.", "Tour ~A$45", "2-3 hours"),
            ("Great Barrier Reef (Cairns)", "World's largest coral reef system, snorkelling/diving tours.", "Tour ~A$230", "Full day"),
            ("Bondi Beach", "Famous city beach with a coastal walk to Coogee.", "Free to visit", "Half day"),
            ("Uluru (Ayers Rock)", "Sacred sandstone monolith in the Red Centre.", "~A$38 park pass", "Full day"),
        ],
    },
    "italy": {
        "name": "Italy", "country": "Italy", "capital": "Rome",
        "currency": "EUR", "language": "Italian", "timezone": "UTC+1",
        "hero_keywords": "rome colosseum venice",
        "description": "Italy layers ancient Rome, Renaissance Florence and canal-laced Venice into one classic trip.",
        "best_months": ["April", "May", "September", "October"], "peak_months": ["July", "August"], "low_months": ["January"],
        "budget_min_inr": 130000, "budget_max_inr": 240000, "default_nights": 7,
        "attractions": [
            ("Colosseum, Rome", "Ancient Roman amphitheatre and gladiator arena.", "~€18", "2-3 hours"),
            ("Vatican Museums & Sistine Chapel", "Michelangelo's ceiling and the Vatican's art collection.", "~€21", "3 hours"),
            ("Grand Canal, Venice", "Gondola-lined waterway through the floating city.", "Gondola ~€90", "Half day"),
            ("Duomo di Firenze, Florence", "Renaissance cathedral with a climbable dome.", "~€20 (dome)", "1-2 hours"),
        ],
    },
    "canada": {
        "name": "Canada", "country": "Canada", "capital": "Ottawa",
        "currency": "CAD", "language": "English/French", "timezone": "UTC-3.5 to UTC-8",
        "hero_keywords": "banff canada mountains lake",
        "description": "Canada offers Rocky Mountain lakes, Niagara Falls and cosmopolitan cities like Toronto and Vancouver.",
        "best_months": ["June", "July", "August", "September"], "peak_months": ["July"], "low_months": ["January"],
        "budget_min_inr": 160000, "budget_max_inr": 280000, "default_nights": 8,
        "attractions": [
            ("Niagara Falls", "One of the world's most powerful waterfalls, on the Ontario border.", "Boat tour ~C$28", "Half day"),
            ("Banff National Park", "Turquoise glacial lakes and Rocky Mountain peaks.", "Park pass ~C$11/day", "Full day"),
            ("CN Tower, Toronto", "Iconic tower with a glass floor and observation deck.", "~C$43", "1-2 hours"),
            ("Stanley Park, Vancouver", "Seawall park with forest trails and skyline/ocean views.", "Free to visit", "Half day"),
        ],
    },
    "new_york": {
        "name": "New York", "country": "United States", "capital": "Albany (state)",
        "currency": "USD", "language": "English", "timezone": "UTC-5 (EST)",
        "hero_keywords": "new york city times square manhattan",
        "description": "New York City packs Broadway, iconic skyline views and world-class museums into five boroughs.",
        "best_months": ["April", "May", "September", "October"], "peak_months": ["December"], "low_months": ["January", "February"],
        "budget_min_inr": 140000, "budget_max_inr": 270000, "default_nights": 6,
        "attractions": [
            ("Statue of Liberty & Ellis Island", "Ferry out to the harbor's iconic monument.", "~$24 (ferry)", "3-4 hours"),
            ("Times Square", "Neon billboards and Broadway theatre district.", "Free to visit", "1-2 hours"),
            ("Central Park", "843-acre park in the middle of Manhattan.", "Free to visit", "2-4 hours"),
            ("Empire State Building", "Art Deco skyscraper with two observation decks.", "~$44", "1-2 hours"),
        ],
    },
    "tokyo": {
        "name": "Tokyo", "country": "Japan", "capital": "Tokyo",
        "currency": "JPY", "language": "Japanese", "timezone": "UTC+9",
        "hero_keywords": "tokyo shibuya skyline night",
        "description": "Tokyo layers neon-lit Shibuya and Shinjuku over centuries-old temples and gardens.",
        "best_months": ["March", "April", "October", "November"], "peak_months": ["April"], "low_months": ["June"],
        "budget_min_inr": 120000, "budget_max_inr": 220000, "default_nights": 6,
        "attractions": [
            ("Senso-ji Temple, Asakusa", "Tokyo's oldest Buddhist temple with a lively market street.", "Free to visit", "1-2 hours"),
            ("Shibuya Crossing & Sky", "World's busiest pedestrian crossing plus rooftop views.", "~¥2,000 (Sky)", "1-2 hours"),
            ("Meiji Shrine", "Forested Shinto shrine near Harajuku.", "Free to visit", "1-2 hours"),
            ("teamLab Planets / Borderless", "Immersive digital art museum.", "~¥3,800", "2 hours"),
        ],
    },
    "delhi": {
        "name": "Delhi", "country": "India", "capital": "New Delhi",
        "currency": "INR", "language": "Hindi/English", "timezone": "UTC+5:30",
        "hero_keywords": "delhi india gate red fort",
        "description": "Delhi layers Mughal-era monuments over a modern capital, with markets and food at every turn.",
        "best_months": ["October", "November", "February", "March"], "peak_months": ["December"], "low_months": ["May", "June"],
        "budget_min_inr": 15000, "budget_max_inr": 40000, "default_nights": 3,
        "attractions": [
            ("Red Fort", "Mughal-era fortified palace, UNESCO World Heritage Site.", "₹35 (Indians)", "2 hours"),
            ("Qutub Minar", "Tallest brick minaret in the world, 12th century.", "₹35 (Indians)", "1-2 hours"),
            ("Humayun's Tomb", "Precursor to the Taj Mahal, set in Persian gardens.", "₹35 (Indians)", "1-2 hours"),
            ("India Gate", "War memorial arch on Rajpath, popular at sunset.", "Free to visit", "1 hour"),
        ],
    },
    "goa": {
        "name": "Goa", "country": "India", "capital": "Panaji",
        "currency": "INR", "language": "Konkani/English", "timezone": "UTC+5:30",
        "hero_keywords": "goa beach india palm trees",
        "description": "Goa mixes Portuguese-era churches and old towns with beach shacks along the Arabian Sea.",
        "best_months": ["November", "December", "January", "February"], "peak_months": ["December"], "low_months": ["June", "July"],
        "budget_min_inr": 18000, "budget_max_inr": 45000, "default_nights": 4,
        "attractions": [
            ("Basilica of Bom Jesus, Old Goa", "UNESCO-listed baroque church holding St. Francis Xavier's remains.", "Free to visit", "1 hour"),
            ("Baga & Calangute Beach", "North Goa's most popular beach and nightlife strip.", "Free to visit", "Half day"),
            ("Fort Aguada", "17th-century Portuguese fort with a lighthouse and sea views.", "Free to visit", "1-2 hours"),
            ("Dudhsagar Waterfalls", "Four-tiered waterfall on the Goa-Karnataka border.", "Jeep safari ~₹500", "Half day"),
        ],
    },
    "rajasthan": {
        "name": "Rajasthan", "country": "India", "capital": "Jaipur",
        "currency": "INR", "language": "Hindi/Rajasthani/English", "timezone": "UTC+5:30",
        "hero_keywords": "jaipur rajasthan fort palace",
        "description": "Rajasthan is India's land of forts and palaces — Jaipur's Pink City, Udaipur's lakes and Jaisalmer's desert.",
        "best_months": ["October", "November", "February", "March"], "peak_months": ["December"], "low_months": ["May", "June"],
        "budget_min_inr": 25000, "budget_max_inr": 65000, "default_nights": 6,
        "attractions": [
            ("Amber Fort, Jaipur", "Hilltop fort-palace with elephant rides and mirror-work halls.", "₹100 (Indians)", "2-3 hours"),
            ("City Palace & Lake Pichola, Udaipur", "Lakeside royal palace complex, Rajasthan's most romantic city.", "₹300 (Indians)", "2-3 hours"),
            ("Mehrangarh Fort, Jodhpur", "Massive hilltop fort overlooking the Blue City.", "₹200 (Indians)", "2-3 hours"),
            ("Jaisalmer Fort & Sam Sand Dunes", "Living desert fort plus a camel-safari sunset in the Thar Desert.", "Safari ~₹800", "Half day"),
        ],
    },
    "manali": {
        "name": "Manali", "country": "India", "capital": "Shimla (state)",
        "currency": "INR", "language": "Hindi/Himachali/English", "timezone": "UTC+5:30",
        "hero_keywords": "manali himachal mountains snow",
        "description": "Manali is a Himalayan hill town for snow, river valleys, and easy access to high mountain passes.",
        "best_months": ["March", "April", "May", "June", "October"], "peak_months": ["May", "June"], "low_months": ["January"],
        "budget_min_inr": 15000, "budget_max_inr": 38000, "default_nights": 4,
        "attractions": [
            ("Solang Valley", "Adventure hub for paragliding, zorbing and (winter) skiing.", "Activities extra", "Half day"),
            ("Rohtang Pass / Atal Tunnel", "High mountain pass with snow views (permit needed in season).", "Permit ~₹500", "Full day"),
            ("Hadimba Temple", "Wooden cave temple set in a cedar forest.", "Free to visit", "1 hour"),
            ("Old Manali & Vashisht Hot Springs", "Cafe-lined riverside lanes and natural hot-water springs.", "Free to visit", "Half day"),
        ],
    },
    "kashmir": {
        "name": "Kashmir", "country": "India", "capital": "Srinagar (summer capital)",
        "currency": "INR", "language": "Kashmiri/Urdu/English", "timezone": "UTC+5:30",
        "hero_keywords": "kashmir srinagar dal lake houseboat",
        "description": "Kashmir is known as \"Paradise on Earth\" — Dal Lake houseboats, Mughal gardens and alpine meadows.",
        "best_months": ["April", "May", "June", "September"], "peak_months": ["May", "June"], "low_months": ["January"],
        "budget_min_inr": 22000, "budget_max_inr": 55000, "default_nights": 5,
        "attractions": [
            ("Dal Lake & Houseboats, Srinagar", "Iconic houseboat stays and shikara boat rides.", "Shikara ~₹500/hr", "Half day"),
            ("Gulmarg", "Meadow-and-ski destination with the Gulmarg Gondola cable car.", "Gondola ~₹1,200", "Full day"),
            ("Pahalgam", "Valley town gateway to Betaab Valley and Aru Valley.", "Free to visit", "Full day"),
            ("Mughal Gardens (Shalimar Bagh)", "Terraced 17th-century Mughal garden by Dal Lake.", "₹24", "1-2 hours"),
        ],
    },
}


def list_destinations() -> list[dict]:
    """Data for the destination-selection grid (section 1 of the spec)."""
    return [
        {
            "key": key,
            "name": info["name"],
            "country": info["country"],
            "heroImage": _unsplash_image(info["hero_keywords"]),
            "shortDescription": info["description"],
        }
        for key, info in DESTINATIONS.items()
    ]


# ---- Flights (estimated — no live booking API wired up yet) --------

_FLIGHT_TEMPLATES = [
    ("Cheapest", 1, 0.80),
    ("Fastest", 0, 1.15),
    ("Recommended", 1, 1.0),
    ("Premium", 0, 1.75),
]

_AIRLINES_BY_REGION = {
    "India": ["IndiGo", "Air India", "Vistara"],
    "default": ["Emirates", "Qatar Airways", "Singapore Airlines", "Air India"],
}


def _build_flights(dest: dict, flight_class: str, travelers: int) -> list[dict]:
    """Clearly-labelled ESTIMATED flight options in four tiers. Real fares
    must come from a live flight-search API before these are ever shown
    to a user as bookable."""
    base_fare = 15000 if dest["country"] == "India" else 45000
    class_multiplier = {"economy": 1.0, "premium_economy": 1.6, "business": 3.2}.get(flight_class, 1.0)
    airlines = _AIRLINES_BY_REGION.get(dest["country"], _AIRLINES_BY_REGION["default"])

    flights = []
    for i, (label, stops, price_mult) in enumerate(_FLIGHT_TEMPLATES):
        fare_per_person = round(base_fare * class_multiplier * price_mult, -2)
        airline = airlines[i % len(airlines)]
        duration_hours = 3 if dest["country"] == "India" else (7 if stops == 0 else 10)
        flights.append({
            "tag": label,
            "airline": airline,
            "flightNumber": f"{airline[:2].upper()}-{100 + i}",
            "cabinClass": flight_class.replace("_", " ").title(),
            "stops": stops,
            "durationHours": duration_hours,
            "farePerPersonInr": int(fare_per_person),
            "fareTotalInr": int(fare_per_person * max(travelers, 1)),
            "refundable": label == "Premium",
            "status": "Estimated flight information — verify live fares and timings with an airline or OTA before booking.",
        })
    return flights


# ---- Hotels (estimated — labelled clearly) --------------------------

_HOTEL_CATEGORY_TIERS = [
    ("Budget", "budget", 0.55),
    ("Best Value", "best_value", 1.0),
    ("Premium", "premium", 1.8),
    ("Luxury", "luxury", 3.2),
]


def _build_hotels(dest: dict, hotel_category: str, nights: int) -> list[dict]:
    base_rate = 2500 if dest["country"] == "India" else 7500
    hotels = []
    for i, (label, key, mult) in enumerate(_HOTEL_CATEGORY_TIERS):
        rate = int(base_rate * mult)
        hotels.append({
            "category": label,
            "name": f"{label} Stay near {dest['name']} City Center",
            "location": f"{dest['name']}, {dest['country']}",
            "starRating": min(5, 2 + i),
            "userRating": round(3.9 + i * 0.25, 1),
            "reviewCount": 400 + i * 700,
            "roomType": "Deluxe Room" if i < 2 else "Suite",
            "amenities": ["Free Wi-Fi", "Breakfast available", "Swimming pool" if i >= 1 else None,
                          "Gym" if i >= 2 else None],
            "pricePerNightInr": rate,
            "totalEstimateInr": rate * max(nights, 1),
            "cancellation": "Free cancellation up to 24-48 hrs before check-in (verify at booking)",
            "isRecommendedTier": key == hotel_category,
            "status": "Estimated hotel information — check live availability and reviews on a booking platform.",
        })
    return hotels


def _build_attractions(dest: dict) -> list[dict]:
    return [
        {
            "name": name, "description": desc, "entryFee": fee, "recommendedDuration": duration,
            "image": _unsplash_image(f"{dest['name']} {name}"),
            "bestTime": "Morning" if i % 2 == 0 else "Afternoon/Evening",
        }
        for i, (name, desc, fee, duration) in enumerate(dest["attractions"])
    ]


def _build_restaurants(dest: dict) -> list[dict]:
    """Try a live web search for real restaurant options; fall back to a
    clearly-labelled generic cuisine suggestion rather than inventing a
    specific restaurant name/rating that could be wrong."""
    try:
        raw = tavily_search(f"Best restaurants to eat in {dest['name']}")
        results = _normalize_search_results(raw)[:4]
    except Exception:
        results = []

    if results:
        return [
            {
                "name": r["title"], "url": r["url"], "cuisine": "See listing for details",
                "priceRange": "$$", "note": r["content"][:140] if r["content"] else "",
                "status": "Web search result — verify hours, menu and rating before visiting.",
            }
            for r in results
        ]

    # Generic, honestly-labelled fallback (no invented names/ratings).
    return [{
        "name": f"Local {dest['name']} cuisine spot",
        "url": "", "cuisine": f"Popular local {dest['country']} dishes",
        "priceRange": "$$", "note": "Ask your hotel concierge or check a review app for a current top pick.",
        "status": "Suggested category only — no specific restaurant verified.",
    }]


_TRANSPORT_TEMPLATES = [
    ("Airport Taxi/Cab", "Best for: airport transfers and late-night arrivals", 1.0),
    ("Metro / Public Transit", "Best for: cheap city-center sightseeing", 0.05),
    ("Ride-hailing App (Uber/local equivalent)", "Best for: door-to-door convenience", 0.4),
    ("Rental Car", "Best for: day trips outside the city, flexible schedule", 1.5),
    ("Local Bus", "Best for: budget travel between neighborhoods", 0.03),
    ("Walking", "Best for: compact old towns and short hops", 0.0),
]


def _build_transportation(dest: dict) -> list[dict]:
    base = 1500 if dest["country"] == "India" else 4000
    return [
        {"mode": mode, "bestFor": best_for.replace("Best for: ", ""),
         "estimatedCostInr": int(base * mult) if mult > 0 else 0}
        for mode, best_for, mult in _TRANSPORT_TEMPLATES
    ]


def _build_best_time(dest: dict) -> dict:
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    calendar = []
    for m in months:
        if m in dest["best_months"]:
            rating = "Excellent"
        elif m in dest["peak_months"]:
            rating = "Peak (Warm/Busy)"
        elif m in dest["low_months"]:
            rating = "Low season"
        else:
            rating = "Good"
        calendar.append({"month": m, "rating": rating})
    return {
        "calendar": calendar,
        "bestMonths": dest["best_months"],
        "peakSeason": dest["peak_months"],
        "lowSeason": dest["low_months"],
    }


_ITINERARY_BLOCKS = [
    ("Morning", "Visit {attraction}"),
    ("Afternoon", "Explore the local area, lunch, and light shopping"),
    ("Evening", "Dinner and a relaxed walk near your hotel"),
]


def _build_itinerary(dest: dict, days: int, travelers: int, hotel_category: str) -> list[dict]:
    attractions = [a[0] for a in dest["attractions"]] or [f"central {dest['name']}"]
    per_person_day_cost = {"budget": 2500, "best_value": 4500, "premium": 8000, "luxury": 15000}.get(
        hotel_category, 4500
    ) if dest["country"] == "India" else {"budget": 6000, "best_value": 10000, "premium": 18000, "luxury": 32000}.get(
        hotel_category, 10000
    )

    itinerary = []
    for day_num in range(1, days + 1):
        if day_num == 1:
            title = f"Arrival in {dest['name']}"
            blocks = [
                {"time": "Morning", "activity": f"Arrival, airport/station transfer, hotel check-in"},
                {"time": "Afternoon", "activity": "Rest, orientation walk near the hotel"},
                {"time": "Evening", "activity": f"Welcome dinner introducing {dest['name']} cuisine"},
            ]
        elif day_num == days and days >= 3:
            title = "Departure"
            blocks = [
                {"time": "Morning", "activity": "Last-minute shopping / leisure"},
                {"time": "Afternoon", "activity": "Check out, buffer time before departure"},
                {"time": "Evening", "activity": "Airport/station transfer for departure"},
            ]
        else:
            attraction = attractions[(day_num - 2) % len(attractions)]
            title = f"{attraction}"
            blocks = [
                {"time": "Morning", "activity": f"Visit {attraction}"},
                {"time": "Afternoon", "activity": "Lunch, continue sightseeing or local market"},
                {"time": "Evening", "activity": "Dinner and free time"},
            ]

        itinerary.append({
            "day": day_num, "title": title, "blocks": blocks,
            "estimatedCostInr": int(per_person_day_cost * max(travelers, 1)),
        })
    return itinerary


def _build_budget(dest: dict, travelers: int, nights: int, hotel_category: str, flight_class: str) -> dict:
    travelers = max(travelers, 1)
    nights = max(nights, 1)

    flights = _build_flights(dest, flight_class, travelers)
    recommended_flight = next(f for f in flights if f["tag"] == "Recommended")
    hotels = _build_hotels(dest, hotel_category, nights)
    chosen_hotel = next((h for h in hotels if h["isRecommendedTier"]), hotels[1])

    food_per_day = 1200 if dest["country"] == "India" else 3500
    transport_per_day = 600 if dest["country"] == "India" else 1800
    activities_per_day = 800 if dest["country"] == "India" else 2500

    flight_cost = recommended_flight["fareTotalInr"]
    hotel_cost = chosen_hotel["totalEstimateInr"]
    food_cost = food_per_day * nights * travelers
    transport_cost = transport_per_day * nights * travelers
    activities_cost = activities_per_day * nights * travelers
    shopping_cost = int((flight_cost + hotel_cost) * 0.05)
    misc_cost = int((flight_cost + hotel_cost) * 0.04)

    total = flight_cost + hotel_cost + food_cost + transport_cost + activities_cost + shopping_cost + misc_cost

    return {
        "breakdown": {
            "flights": flight_cost, "hotels": hotel_cost, "food": food_cost,
            "localTransport": transport_cost, "activities": activities_cost,
            "shopping": shopping_cost, "miscellaneous": misc_cost,
        },
        "estimatedTotalInr": int(total),
        "tiers": {
            "budget": f"₹{int(total * 0.65):,} – ₹{int(total * 0.85):,}",
            "comfort": f"₹{int(total * 0.85):,} – ₹{int(total * 1.15):,}",
            "luxury": f"₹{int(total * 1.6):,}+",
        },
        "note": "This is a planning estimate, not a live price quote — flight and hotel fares change with dates and demand.",
    }


def _build_travel_info(dest: dict) -> dict:
    return {
        "currency": dest["currency"], "language": dest["language"], "timezone": dest["timezone"],
        "visaNote": (
            "Visa/passport requirements vary by nationality and change over time — this is general guidance, "
            "not official advice. Confirm on your country's official government travel-advisory site or the "
            f"{dest['country']} embassy/consulate website before booking."
        ),
        "safetyTip": "Keep digital + physical copies of your passport/ID, share your itinerary with someone, and buy travel insurance.",
        "paymentTip": "Carry some local cash for small vendors; cards are widely accepted in cities.",
        "tippingNote": "Tipping norms vary by country — check a local guide; it's rarely mandatory but often appreciated for good service.",
    }


def build_trip_plan(
    destination_key: str,
    travelers: int = 2,
    nights: int | None = None,
    hotel_category: str = "best_value",
    flight_class: str = "economy",
    travel_style: str = "balanced",
) -> dict:
    """Build the full structured trip-plan payload (spec section 17) for
    one destination + customization choices. This is what the
    'Explore Destinations' UI calls — separate from the free-text chat
    flow (run_travel_agent) above, which stays untouched."""
    key = destination_key.lower().strip().replace(" ", "_")
    dest = DESTINATIONS.get(key)
    if not dest:
        raise ValueError(f"Unknown destination '{destination_key}'. See list_destinations() for valid keys.")

    nights = nights if nights and nights > 0 else dest["default_nights"]
    days = nights + 1
    hotel_category = hotel_category if hotel_category in {"budget", "best_value", "premium", "luxury"} else "best_value"
    flight_class = flight_class if flight_class in {"economy", "premium_economy", "business"} else "economy"

    return {
        "destination": dest["name"],
        "destinationKey": key,
        "country": dest["country"],
        "capital": dest["capital"],
        "heroImage": _unsplash_image(dest["hero_keywords"]),
        "description": dest["description"],
        "durationLabel": f"{days} Days / {nights} Nights",
        "travelers": travelers,
        "travelStyle": travel_style,
        "currency": dest["currency"],
        "language": dest["language"],
        "timezone": dest["timezone"],
        "bestTime": _build_best_time(dest),
        "weather": {"note": "Live weather is available for many destinations via the chat planner; sample seasonal guidance is shown here."},
        "flights": _build_flights(dest, flight_class, travelers),
        "hotels": _build_hotels(dest, hotel_category, nights),
        "attractions": _build_attractions(dest),
        "restaurants": _build_restaurants(dest),
        "transportation": _build_transportation(dest),
        "itinerary": _build_itinerary(dest, days, travelers, hotel_category),
        "budget": _build_budget(dest, travelers, nights, hotel_category, flight_class),
        "travelInfo": _build_travel_info(dest),
    }


# ---------------------------------------------------------------------
# Example FastAPI wiring (add this to your main app file, not here —
# backend.py stays a plain module of functions with no app instance):
#
#   from backend import list_destinations, build_trip_plan
#
#   @app.get("/api/destinations")
#   def api_destinations():
#       return {"success": True, "destinations": list_destinations()}
#
#   @app.get("/api/destination/{key}/plan")
#   def api_destination_plan(key: str, travelers: int = 2, nights: int | None = None,
#                             hotel_category: str = "best_value", flight_class: str = "economy",
#                             travel_style: str = "balanced"):
#       try:
#           return {"success": True, "plan": build_trip_plan(key, travelers, nights,
#                    hotel_category, flight_class, travel_style)}
#       except ValueError as exc:
#           return {"success": False, "error": str(exc)}
# ---------------------------------------------------------------------
