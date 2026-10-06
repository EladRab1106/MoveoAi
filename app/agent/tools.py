"""Tools exposed to Claude. Each one is a thin wrapper over deterministic code.

Definitions use `strict: true`, so Claude's tool inputs always match the schema.
Results are compact JSON so the model reads exactly the numbers we computed.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable

from app.config import scoring_config
from app.data_sources import nws
from app.hubs import REGIONS, find_hub, hubs_in_region, load_hubs
from app.scoring import engine
from app.scoring.models import ActiveAlert, HubRisk

HAZARDS = list(scoring_config()["hazards"])
METRICS = list(engine.stat_metrics(scoring_config()))


def _region(region: str) -> str | None:
    return None if region == "all" else region


def _brief(r: HubRisk, hazard: str | None = None) -> dict:
    out = {
        "rank": r.rank, "hub_id": r.hub_id, "hub": f"{r.name}, {r.state}", "region": r.region,
        "composite_score": r.composite_score, "tier": r.tier, "top_drivers": r.top_drivers,
        **({"tied_with": r.tied_with} if r.tied_with else {}),
        "hazard_scores": {h.hazard: h.score for h in r.hazards},
    }
    if hazard:
        h = next(h for h in r.hazards if h.hazard == hazard)
        out["hazard_detail"] = {
            "score": h.score,
            "observed_disruption_days_per_year": h.frequency.days_per_year,
            "observed_days_portfolio_rank": h.frequency.portfolio_rank,
            **({"observed_days_tied_with": h.frequency.tied_with} if h.frequency.tied_with else {}),
            "observed_rule": h.frequency.threshold,
            "fema_nri_long_term_score": h.long_term.score,
            "nri_components": h.long_term.components,
        }
    return out


# ------------------------------------------------------------------ implementations

def list_hubs(region: str) -> dict:
    return {"hubs": [{"hub_id": h.id, "hub": h.label, "region": h.region, "county": h.county}
                     for h in hubs_in_region(_region(region))]}


def rank_hubs(hazard: str, region: str, top_n: int) -> dict:
    hz = None if hazard == "composite" else hazard
    ranked = engine.get_scores(region=_region(region), hazard=hz)
    return {
        "ranked_by": "composite Weather Disruption Risk Score" if hz is None
                     else f"{hz} hazard sub-score",
        "region": region,
        "hubs_in_scope": len(ranked),
        "results": [_brief(r, hz) for r in ranked[:max(1, top_n)]],
        "note": "Scores are 0-100 and relative to this hub portfolio (min-max normalised "
                "observed frequency blended with FEMA NRI percentiles). Tied values share a "
                "rank and are listed in tied_with.",
    }


def get_hub_risk(hub: str) -> dict:
    r = engine.get_hub_risk(hub)
    data = r.model_dump(exclude={"active_alerts", "alert_bump", "base_score"})
    data["portfolio_size"] = len(load_hubs())
    data["tier_thresholds"] = scoring_config()["tiers"]
    return data


def compare_hubs(hubs: list[str]) -> dict:
    ids = [find_hub(h).id for h in hubs]
    all_scores = {r.hub_id: r for r in engine.get_scores()}
    rows = [all_scores[i] for i in ids]
    return {
        "hubs": [{**_brief(r), "hazards": {h.hazard: {
            "score": h.score,
            "observed_disruption_days_per_year": h.frequency.days_per_year,
            "observed_rule": h.frequency.threshold,
            "observed_days_portfolio_rank": h.frequency.portfolio_rank,
            **({"observed_days_tied_with": h.frequency.tied_with} if h.frequency.tied_with else {}),
            "fema_nri_long_term_score": h.long_term.score,
            "nri_components": h.long_term.components,
            "nri_modelled_events_per_year": h.long_term.annual_events,
        } for h in r.hazards},
            "major_disaster_declarations_since_2000": r.major_disaster_declarations_since_2000}
            for r in rows],
        "note": "rank = position in the full portfolio by composite score. Ranks use "
                "competition ranking: tied values share a rank and list tied_with.",
    }


def weather_stat(hub: str, metric: str, year: int | None = None, start_date: str | None = None,
                 end_date: str | None = None) -> dict:
    s = date.fromisoformat(start_date) if start_date else None
    e = date.fromisoformat(end_date) if end_date else None
    stat = engine.weather_stat(hub, metric, year=year, start=s, end=e)
    return {**stat.model_dump(), "source": "Open-Meteo ERA5 reanalysis, hub point location"}


def get_active_alerts(hub: str) -> dict:
    hubs = list(load_hubs()) if hub == "all" else [find_hub(hub)]
    out = []
    for h in hubs:
        try:
            alerts = [ActiveAlert(**a) for a in nws.active_alerts(h)]
        except Exception as exc:  # live API: degrade gracefully
            out.append({"hub_id": h.id, "error": f"NWS unavailable: {exc}"})
            continue
        base = engine.get_hub_risk(h.id)
        live = engine.apply_alerts(base, alerts, scoring_config())
        if hub == "all" and not alerts:
            continue
        out.append({"hub_id": h.id, "hub": f"{h.name}, {h.state}",
                    "alerts": [a.model_dump() for a in alerts],
                    "base_score": base.base_score, "alert_bump": live.alert_bump,
                    "live_score": live.composite_score, "live_tier": live.tier})
    return {"as_of": "now (live NWS)", "hubs_with_alerts" if hub == "all" else "hubs": out}


def get_methodology() -> dict:
    cfg = scoring_config()
    return {
        "snapshot": engine.snapshot_meta(),
        "disruption_thresholds": cfg["disruption_thresholds"],
        "hazards": cfg["hazards"],
        "composite_weights": cfg["composite_weights"],
        "tiers": cfg["tiers"],
        "active_alert_bump": cfg["active_alert_bump"],
        "nri_codes": {"WNTW": "winter weather", "ISTM": "ice storm", "HRCN": "hurricane",
                      "IFLD": "inland flooding", "CFLD": "coastal flooding", "HWAV": "heat wave",
                      "TRND": "tornado", "HAIL": "hail", "SWND": "strong wind"},
        "conventions": [
            "Tied values share a rank (competition ranking: 1, 1, 3) and are listed in tied_with.",
            "Disaster declaration counts include only FEMA major-disaster (DR) declarations; "
            "emergency (EM) declarations, e.g. for sheltering evacuees, are excluded.",
        ],
        "known_limitations": [
            "Each hub is one point; ERA5 reanalysis (~25 km grid) smooths local extremes.",
            "Observed frequency uses only the last ~5 full years; rare events are under-sampled, "
            "which is why FEMA NRI long-term percentiles are blended in.",
            "Scores are relative to this 22-hub portfolio, not absolute probabilities.",
            "NRI loss-rate percentiles describe the whole county, not the facility.",
            "Not modelled: earthquakes, wildfire, drought, facility-specific mitigation.",
        ],
    }


# ------------------------------------------------------------------ definitions

def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": list(props) if required is None else required,
            "additionalProperties": False}


HUB_ARG = {"type": "string", "description": "Hub name or id, e.g. 'Dallas' or 'st_louis'"}
REGION_ARG = {"type": "string", "enum": ["all", *REGIONS],
              "description": "US Census region filter, or 'all'"}

TOOL_DEFS: list[dict] = [
    {"name": "list_hubs", "strict": True,
     "description": "List the distribution hubs (id, city, region, county). Use to resolve which "
                    "hubs exist or belong to a region.",
     "input_schema": _obj({"region": REGION_ARG})},
    {"name": "rank_hubs", "strict": True,
     "description": "Rank hubs by the deterministic composite Weather Disruption Risk Score, or by "
                    "one hazard sub-score. Use for 'which hubs are most exposed to X' questions.",
     "input_schema": _obj({
         "hazard": {"type": "string", "enum": ["composite", *HAZARDS]},
         "region": REGION_ARG,
         "top_n": {"type": "integer", "description": "How many hubs to return (e.g. 5; use 25 "
                                                     "for all)"}})},
    {"name": "get_hub_risk", "strict": True,
     "description": "Full score breakdown for one hub: every hazard's observed disruption "
                    "days/yr, FEMA NRI components, weights and contribution to the composite, "
                    "plus FEMA disaster declaration counts. Use to explain WHY a hub scores as "
                    "it does.",
     "input_schema": _obj({"hub": HUB_ARG})},
    {"name": "compare_hubs", "strict": True,
     "description": "Side-by-side hazard breakdown for 2+ hubs.",
     "input_schema": _obj({"hubs": {"type": "array", "items": HUB_ARG}})},
    {"name": "weather_stat", "strict": True,
     "description": "Count days matching a weather condition at a hub over a calendar year or a "
                    "date range, from the daily history. Use for questions like 'what % of days "
                    "in Denver last year had snowfall'. Give either year, or start_date and "
                    "end_date (YYYY-MM-DD). 'Last year' = the last full calendar year (see "
                    "system prompt), passed as year.",
     "input_schema": _obj({
         "hub": HUB_ARG,
         "metric": {"type": "string", "enum": METRICS},
         "year": {"type": "integer"},
         "start_date": {"type": "string", "format": "date"},
         "end_date": {"type": "string", "format": "date"}}, required=["hub", "metric"])},
    {"name": "get_active_alerts", "strict": True,
     "description": "Live National Weather Service alerts for a hub (or 'all' hubs) and how they "
                    "bump the risk score right now. Use for current/this-week questions.",
     "input_schema": _obj({"hub": {"type": "string",
                                   "description": "Hub name/id, or 'all'"}})},
    {"name": "get_methodology", "strict": True,
     "description": "Scoring methodology: thresholds, weights, tiers, data snapshot dates and "
                    "known limitations. Use when asked how scores work or what data is used.",
     "input_schema": _obj({})},
]

TOOL_FUNCS: dict[str, Callable[..., dict]] = {
    "list_hubs": list_hubs, "rank_hubs": rank_hubs, "get_hub_risk": get_hub_risk,
    "compare_hubs": compare_hubs, "weather_stat": weather_stat,
    "get_active_alerts": get_active_alerts, "get_methodology": get_methodology,
}


def run_tool(name: str, args: dict[str, Any]) -> str:
    """Execute a tool; raises on bad input (the caller reports it back as is_error)."""
    if name not in TOOL_FUNCS:
        raise ValueError(f"Unknown tool {name}")
    return json.dumps(TOOL_FUNCS[name](**args), separators=(",", ":"), default=str)
