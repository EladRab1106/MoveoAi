"""Open-Meteo Historical Weather API (default model selection). Free, no key.

https://open-meteo.com/en/docs/historical-weather-api
"""

from __future__ import annotations

from datetime import date

from app.data_sources.http import get_json
from app.hubs import Hub

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Open-Meteo variable -> our column name
DAILY_VARS = {
    "snowfall_sum": "snowfall_cm",
    "precipitation_sum": "precip_mm",
    "wind_gusts_10m_max": "gust_kmh",
    "temperature_2m_min": "tmin_c",
    "temperature_2m_max": "tmax_c",
}


def fetch_daily(hub: Hub, start: date, end: date) -> list[dict]:
    """Daily weather rows for one hub, in the hub's local timezone."""
    data = get_json(ARCHIVE_URL, {
        "latitude": hub.lat,
        "longitude": hub.lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(DAILY_VARS),
        "timezone": "auto",
    }, timeout=60)
    daily = data["daily"]
    rows = []
    for i, day in enumerate(daily["time"]):
        row = {"hub_id": hub.id, "date": day}
        for api_name, col in DAILY_VARS.items():
            row[col] = daily[api_name][i]
        rows.append(row)
    return rows
