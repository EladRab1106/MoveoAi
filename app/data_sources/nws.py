"""National Weather Service active alerts (api.weather.gov). Free, no key.

Fetched live at request time (not part of the snapshot) with a short in-memory cache.
"""

from __future__ import annotations

import time

from app.data_sources.http import get_json
from app.hubs import Hub

ALERTS_URL = "https://api.weather.gov/alerts/active"
CACHE_TTL_S = 600
_cache: dict[str, tuple[float, list[dict]]] = {}


def active_alerts(hub: Hub) -> list[dict]:
    cached = _cache.get(hub.id)
    if cached and time.time() - cached[0] < CACHE_TTL_S:
        return cached[1]
    data = get_json(ALERTS_URL, {"point": f"{hub.lat:.4f},{hub.lon:.4f}"},
                    headers={"Accept": "application/geo+json"}, retries=2, timeout=10)
    alerts = [{
        "event": f["properties"].get("event"),
        "severity": f["properties"].get("severity"),      # Extreme/Severe/Moderate/Minor/Unknown
        "headline": f["properties"].get("headline"),
        "onset": f["properties"].get("onset"),
        "ends": f["properties"].get("ends") or f["properties"].get("expires"),
    } for f in data.get("features", [])]
    _cache[hub.id] = (time.time(), alerts)
    return alerts
