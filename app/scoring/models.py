"""Typed outputs of the scoring engine. These are what tools return to the LLM."""

from __future__ import annotations

from pydantic import BaseModel


class FrequencyComponent(BaseModel):
    metric: str                   # e.g. "winter disruption days"
    threshold: str                # human-readable rule
    days_per_year: float          # observed average over full years in the window
    normalized: float             # 0-100, min-max across all hubs in the portfolio
    years: list[int]
    portfolio_rank: int = 0       # 1 = most days; equal values share a rank (competition ranking)
    tied_with: list[str] = []     # other hub ids with the same days_per_year


class LongTermComponent(BaseModel):
    source: str = "FEMA National Risk Index (annualized loss rate, national percentile)"
    score: float                  # 0-100 after aggregation
    aggregation: str              # mean | max
    components: dict[str, float | None]   # NRI code -> percentile (None = not applicable)
    annual_events: float | None = None        # NRI modelled events/yr, when blended in
    annual_events_weight: float | None = None


class HazardScore(BaseModel):
    hazard: str
    label: str
    score: float                  # 0-100 hazard sub-score
    frequency: FrequencyComponent
    long_term: LongTermComponent
    weights: dict[str, float]     # {frequency, long_term}
    composite_weight: float
    contribution: float           # composite_weight * score, points added to the composite


class ActiveAlert(BaseModel):
    event: str | None
    severity: str | None
    headline: str | None
    ends: str | None = None


class HubRisk(BaseModel):
    hub_id: str
    name: str
    state: str
    region: str
    composite_score: float        # 0-100
    base_score: float             # composite before the live-alert bump
    alert_bump: float = 0.0
    tier: str
    rank: int | None = None       # rank in the list it was returned in (1 = most exposed);
                                  # equal scores share a rank (1, 1, 3, ...)
    tied_with: list[str] = []     # hub ids with the same ranking score in that list
    hazards: list[HazardScore]
    top_drivers: list[str]        # hazards ordered by contribution, top 2
    active_alerts: list[ActiveAlert] = []
    # Federal *major disaster* (DR) declarations for the county since 2000, by incident type.
    # Emergency (EM) declarations are excluded: they often reflect support actions, e.g.
    # sheltering evacuees far from a hurricane's landfall, not direct impact.
    major_disaster_declarations_since_2000: dict[str, int] = {}


class WeatherStat(BaseModel):
    hub_id: str
    hub_name: str
    metric: str
    rule: str
    period_start: str
    period_end: str
    days_matching: int
    days_observed: int
    percent: float
    note: str | None = None
