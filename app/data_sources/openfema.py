"""OpenFEMA Disaster Declarations Summaries (v2). Free, no key.

https://www.fema.gov/openfema-data-page/disaster-declarations-summaries-v2
"""

from __future__ import annotations

from app.data_sources.http import get_json

URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"

WEATHER_INCIDENT_TYPES = {
    "Hurricane", "Flood", "Severe Storm", "Severe Storm(s)", "Tornado", "Snowstorm",
    "Severe Ice Storm", "Winter Storm", "Coastal Storm", "Tropical Storm", "Typhoon",
    "Freezing", "Mud/Landslide",
}


# declarationType: DR = major disaster (direct damage), EM = emergency (often support
# actions, e.g. sheltering hurricane evacuees far from landfall), FM = fire management.
MAJOR_DISASTER = "DR"


def fetch_declarations(county_fips: str, since_year: int = 2000) -> list[dict]:
    """Weather-related federal declarations covering the county (all declaration types)."""
    state, county = county_fips[:2], county_fips[2:]
    data = get_json(URL, {
        "$filter": (f"fipsStateCode eq '{state}' and fipsCountyCode eq '{county}' "
                    f"and declarationDate ge '{since_year}-01-01T00:00:00.000Z'"),
        "$select": "disasterNumber,declarationType,incidentType,declarationTitle,declarationDate",
        "$orderby": "declarationDate desc",
        "$top": 1000,
    })
    seen, rows = set(), []
    for d in data.get("DisasterDeclarationsSummaries", []):
        if d["incidentType"] not in WEATHER_INCIDENT_TYPES or d["disasterNumber"] in seen:
            continue
        seen.add(d["disasterNumber"])
        rows.append({
            "disaster_number": d["disasterNumber"],
            "declaration_type": d["declarationType"],
            "incident_type": d["incidentType"],
            "title": d["declarationTitle"],
            "declaration_date": d["declarationDate"][:10],
        })
    return rows
