"""FEMA National Risk Index, county level, via FEMA's public ArcGIS feature service.

https://hazards.fema.gov/nri/
"""

from __future__ import annotations

from app.data_sources.http import get_json

NRI_COUNTIES_URL = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "National_Risk_Index_Counties/FeatureServer/0/query"
)

# NRI hazard codes we ingest -> human label
HAZARD_LABELS = {
    "WNTW": "Winter weather",
    "ISTM": "Ice storm",
    "CWAV": "Cold wave",
    "HRCN": "Hurricane",
    "IFLD": "Inland flooding",
    "CFLD": "Coastal flooding",
    "HWAV": "Heat wave",
    "TRND": "Tornado",
    "HAIL": "Hail",
    "SWND": "Strong wind",
}


def fetch_county(county_fips: str) -> tuple[list[dict], str | None]:
    """Return ([{hazard, loss_rate_pctl, annual_freq, risk_score, risk_rating, eal_usd}], nri_version)."""
    data = get_json(NRI_COUNTIES_URL, {
        "where": f"STCOFIPS='{county_fips}'",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    })
    features = data.get("features") or []
    if not features:
        raise LookupError(f"NRI has no county with FIPS {county_fips}")
    a = features[0]["attributes"]
    rows = [{
        "hazard": code,
        "loss_rate_pctl": a.get(f"{code}_ALR_NPCTL"),
        "annual_freq": a.get(f"{code}_AFREQ"),
        "risk_score": a.get(f"{code}_RISKS"),
        "risk_rating": a.get(f"{code}_RISKR"),
        "eal_usd": a.get(f"{code}_EALT"),
    } for code in HAZARD_LABELS]
    return rows, a.get("NRI_VER")
