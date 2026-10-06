"""Deterministic Weather Disruption Risk scoring.

All ranking numbers come from here, never from the LLM. The core functions are
pure (they take data in, return scores out) so they are easy to unit test; the
`*_from_db` wrappers load the committed snapshot.

Score recipe (see config/scoring.yaml for every constant):
  1. Disruption days/yr per hub = days crossing a threshold, averaged over full years.
  2. frequency_norm = min-max of (1) across all hubs -> 0-100 (relative to our portfolio).
  3. long_term = FEMA NRI loss-rate percentile(s) for the county, aggregated (mean/max).
  4. hazard score = w_f * frequency_norm + w_l * long_term
  5. composite = sum(composite_weight[h] * hazard score[h]) (+ live-alert bump, capped)
  6. tier from composite thresholds.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from functools import lru_cache
from statistics import mean
from typing import Callable

from app.config import scoring_config
from app.hubs import Hub, find_hub, hubs_in_region, load_hubs
from app.scoring.models import (ActiveAlert, FrequencyComponent, HazardScore, HubRisk,
                                LongTermComponent, WeatherStat)
from app.storage.db import get_meta, session

DayRow = dict  # keys: date, snowfall_cm, precip_mm, gust_kmh, tmin_c, tmax_c


# ---------------------------------------------------------------- thresholds

def _ge(v, t) -> bool:
    return v is not None and v >= t


def _le(v, t) -> bool:
    return v is not None and v <= t


def disruption_rules(cfg: dict) -> dict[str, tuple[str, Callable[[DayRow], bool]]]:
    t = cfg["disruption_thresholds"]
    w, wi, r, h = t["winter"], t["wind"], t["heavy_rain"], t["heat"]
    return {
        "winter": (f"snowfall >= {w['snowfall_cm']} cm OR min temp <= {w['tmin_c']} C",
                   lambda d: _ge(d["snowfall_cm"], w["snowfall_cm"]) or _le(d["tmin_c"], w["tmin_c"])),
        "wind": (f"max wind gust >= {wi['gust_kmh']} km/h",
                 lambda d: _ge(d["gust_kmh"], wi["gust_kmh"])),
        "heavy_rain": (f"precipitation >= {r['precip_mm']} mm",
                       lambda d: _ge(d["precip_mm"], r["precip_mm"])),
        "heat": (f"max temp >= {h['tmax_c']} C", lambda d: _ge(d["tmax_c"], h["tmax_c"])),
    }


def full_years(days: list[DayRow]) -> list[int]:
    """Calendar years with (near-)complete coverage; partial years would bias rates."""
    counts = Counter(d["date"][:4] for d in days)
    return sorted(int(y) for y, n in counts.items() if n >= 365)


def disruption_days_per_year(days: list[DayRow], rule: Callable[[DayRow], bool],
                             years: list[int]) -> float:
    if not years:
        return 0.0
    yrs = {str(y) for y in years}
    hits = sum(1 for d in days if d["date"][:4] in yrs and rule(d))
    return hits / len(years)


def min_max(values: dict[str, float]) -> dict[str, float]:
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-9:
        return {k: 0.0 for k in values}
    return {k: 100.0 * (v - lo) / (hi - lo) for k, v in values.items()}


def aggregate_nri(components: dict[str, float | None], how: str) -> float:
    vals = [v if v is not None else 0.0 for v in components.values()]  # N/A hazard -> 0 risk
    if not vals:
        return 0.0
    return max(vals) if how == "max" else mean(vals)


def competition_ranks(values: dict[str, float]) -> dict[str, tuple[int, list[str]]]:
    """Rank keys by value descending; equal values (at 1 decimal) share a rank and list
    each other as ties. Returns key -> (rank, tied_with)."""
    rounded = {k: round(v, 1) for k, v in values.items()}
    out = {}
    for k, v in rounded.items():
        rank = 1 + sum(1 for o in rounded.values() if o > v)
        out[k] = (rank, sorted(o for o, ov in rounded.items() if ov == v and o != k))
    return out


def tier_for(score: float, cfg: dict) -> str:
    for name, lower in sorted(cfg["tiers"].items(), key=lambda kv: -kv[1]):
        if score >= lower:
            return name.capitalize()
    return "Low"


def alert_bump(alerts: list[ActiveAlert], cfg: dict) -> float:
    c = cfg["active_alert_bump"]
    bump = sum(c["per_extreme_alert"] if a.severity == "Extreme"
               else c["per_severe_alert"] if a.severity == "Severe" else 0 for a in alerts)
    return float(min(bump, c["max_bump"]))


# ---------------------------------------------------------------- core (pure)

def score_portfolio(hubs: list[Hub], weather: dict[str, list[DayRow]],
                    nri: dict[str, dict[str, float | None]], cfg: dict,
                    nri_freq: dict[str, dict[str, float | None]] | None = None,
                    ) -> dict[str, HubRisk]:
    """Score every hub. Normalisation is across the whole portfolio passed in.

    nri: hub -> NRI code -> loss-rate percentile. nri_freq: hub -> NRI code -> modelled
    events/yr, only used for hazards with nri_afreq_weight > 0.
    """
    nri_freq = nri_freq or {}
    rules = disruption_rules(cfg)
    years_by_hub = {h.id: full_years(weather.get(h.id, [])) for h in hubs}

    raw_freq: dict[str, dict[str, float]] = defaultdict(dict)   # rule -> hub -> days/yr
    for key, (_, rule) in rules.items():
        for h in hubs:
            raw_freq[key][h.id] = disruption_days_per_year(weather.get(h.id, []), rule,
                                                           years_by_hub[h.id])
    norm_freq = {key: min_max(vals) for key, vals in raw_freq.items()}
    freq_rank = {key: competition_ranks(vals) for key, vals in raw_freq.items()}

    # NRI modelled annual frequency per hazard (summed over its codes), normalised across hubs
    afreq_raw: dict[str, dict[str, float]] = {}
    for hz, hcfg in cfg["hazards"].items():
        if hcfg.get("nri_afreq_weight", 0):
            afreq_raw[hz] = {h.id: sum(nri_freq.get(h.id, {}).get(c) or 0.0
                                       for c in hcfg["nri_hazards"]) for h in hubs}
    afreq_norm = {hz: min_max(v) for hz, v in afreq_raw.items()}

    result: dict[str, HubRisk] = {}
    for h in hubs:
        hazard_scores: list[HazardScore] = []
        for hz, hcfg in cfg["hazards"].items():
            fkey = hcfg["frequency_from"]
            comps = {code: nri.get(h.id, {}).get(code) for code in hcfg["nri_hazards"]}
            lt = aggregate_nri(comps, hcfg["nri_agg"])
            af_w = hcfg.get("nri_afreq_weight", 0)
            if af_w:
                lt = (1 - af_w) * lt + af_w * afreq_norm[hz][h.id]
            wf, wl = hcfg["weights"]["frequency"], hcfg["weights"]["long_term"]
            score = wf * norm_freq[fkey][h.id] + wl * lt
            cw = cfg["composite_weights"][hz]
            hazard_scores.append(HazardScore(
                hazard=hz, label=hcfg["label"], score=round(score, 1),
                frequency=FrequencyComponent(
                    metric=f"{fkey.replace('_', ' ')} disruption days", threshold=rules[fkey][0],
                    days_per_year=round(raw_freq[fkey][h.id], 1),
                    normalized=round(norm_freq[fkey][h.id], 1), years=years_by_hub[h.id],
                    portfolio_rank=freq_rank[fkey][h.id][0],
                    tied_with=freq_rank[fkey][h.id][1]),
                long_term=LongTermComponent(
                    score=round(lt, 1), aggregation=hcfg["nri_agg"],
                    components={k: (round(v, 1) if v is not None else None) for k, v in comps.items()},
                    annual_events=round(afreq_raw[hz][h.id], 3) if af_w else None,
                    annual_events_weight=af_w or None),
                weights={"frequency": wf, "long_term": wl}, composite_weight=cw,
                contribution=round(cw * score, 1)))
        composite = sum(hs.composite_weight * hs.score for hs in hazard_scores)
        drivers = sorted(hazard_scores, key=lambda s: -s.contribution)[:2]
        result[h.id] = HubRisk(
            hub_id=h.id, name=h.name, state=h.state, region=h.region,
            composite_score=round(composite, 1), base_score=round(composite, 1),
            tier=tier_for(composite, cfg), hazards=hazard_scores,
            top_drivers=[d.hazard for d in drivers])
    return result


def apply_alerts(risk: HubRisk, alerts: list[ActiveAlert], cfg: dict) -> HubRisk:
    bump = alert_bump(alerts, cfg)
    score = min(100.0, risk.base_score + bump)
    return risk.model_copy(update={"active_alerts": alerts, "alert_bump": bump,
                                   "composite_score": round(score, 1),
                                   "tier": tier_for(score, cfg)})


def rank(risks: list[HubRisk], hazard: str | None = None) -> list[HubRisk]:
    """Order by composite (or one hazard) score; equal scores share a rank and list ties."""
    def key(r: HubRisk) -> float:
        if hazard is None:
            return r.composite_score
        return next(h.score for h in r.hazards if h.hazard == hazard)
    ranks = competition_ranks({r.hub_id: key(r) for r in risks})
    ordered = sorted(risks, key=lambda r: (-key(r), r.hub_id))
    return [r.model_copy(update={"rank": ranks[r.hub_id][0], "tied_with": ranks[r.hub_id][1]})
            for r in ordered]


# ---------------------------------------------------------------- snapshot access

@lru_cache
def _snapshot() -> tuple[dict, dict, dict, dict[str, Counter], dict[str, str]]:
    weather: dict[str, list[DayRow]] = defaultdict(list)
    nri: dict[str, dict[str, float | None]] = defaultdict(dict)
    nri_freq: dict[str, dict[str, float | None]] = defaultdict(dict)
    decl: dict[str, Counter] = defaultdict(Counter)
    with session() as conn:
        for r in conn.execute("SELECT * FROM daily_weather ORDER BY hub_id, date"):
            weather[r["hub_id"]].append(dict(r))
        for r in conn.execute("SELECT hub_id, hazard, loss_rate_pctl, annual_freq FROM nri_hazard"):
            nri[r["hub_id"]][r["hazard"]] = r["loss_rate_pctl"]
            nri_freq[r["hub_id"]][r["hazard"]] = r["annual_freq"]
        for r in conn.execute("SELECT hub_id, incident_type FROM disaster_declaration "
                              "WHERE declaration_type = 'DR'"):
            decl[r["hub_id"]][r["incident_type"]] += 1
        meta = {k: get_meta(conn, k) or "" for k in
                ("weather_start", "weather_end", "nri_version", "ingested_at")}
    return dict(weather), dict(nri), dict(nri_freq), dict(decl), meta


def snapshot_meta() -> dict[str, str]:
    return _snapshot()[4]


def frequency_years() -> list[int]:
    """Full calendar years used for the observed-frequency KPI (common to all hubs)."""
    per_hub = [set(full_years(days)) for days in _snapshot()[0].values()]
    return sorted(set.intersection(*per_hub)) if per_hub else []


@lru_cache
def _base_scores() -> dict[str, HubRisk]:
    weather, nri, nri_freq, decl, _ = _snapshot()
    scores = score_portfolio(list(load_hubs()), weather, nri, scoring_config(), nri_freq)
    return {hid: r.model_copy(update={"major_disaster_declarations_since_2000":
                                      dict(decl.get(hid, {}))})
            for hid, r in scores.items()}


def get_scores(region: str | None = None, hazard: str | None = None,
               alerts: dict[str, list[ActiveAlert]] | None = None) -> list[HubRisk]:
    """Ranked hub risks, optionally filtered by region and ranked by one hazard."""
    if hazard is not None and hazard not in scoring_config()["hazards"]:
        raise ValueError(f"Unknown hazard '{hazard}'. Use one of: "
                         f"{', '.join(scoring_config()['hazards'])}")
    base = _base_scores()
    risks = []
    for h in hubs_in_region(region):
        r = base[h.id]
        if alerts and h.id in alerts:
            r = apply_alerts(r, alerts[h.id], scoring_config())
        risks.append(r)
    return rank(risks, hazard)


def get_hub_risk(name: str, alerts: list[ActiveAlert] | None = None) -> HubRisk:
    hub = find_hub(name)
    risk = next(r for r in get_scores() if r.hub_id == hub.id)  # rank within full portfolio
    return apply_alerts(risk, alerts, scoring_config()) if alerts else risk


# ---------------------------------------------------------------- ad-hoc weather statistics

def stat_metrics(cfg: dict) -> dict[str, tuple[str, Callable[[DayRow], bool]]]:
    rules = disruption_rules(cfg)
    return {
        "snowfall": ("snowfall >= 0.25 cm (~0.1 in, 'measurable' snow)",
                     lambda d: _ge(d["snowfall_cm"], 0.25)),
        "precipitation": ("precipitation >= 1 mm (a 'wet day')", lambda d: _ge(d["precip_mm"], 1.0)),
        "freezing": ("min temp <= 0 C", lambda d: _le(d["tmin_c"], 0.0)),
        "winter_disruption": rules["winter"],
        "high_wind": rules["wind"],
        "heavy_rain": rules["heavy_rain"],
        "extreme_heat": rules["heat"],
    }


def compute_weather_stat(days: list[DayRow], metric: str, start: date, end: date,
                         cfg: dict) -> tuple[str, int, int]:
    metrics = stat_metrics(cfg)
    if metric not in metrics:
        raise ValueError(f"Unknown metric '{metric}'. Use one of: {', '.join(metrics)}")
    rule_desc, rule = metrics[metric]
    s, e = start.isoformat(), end.isoformat()
    window = [d for d in days if s <= d["date"] <= e]
    return rule_desc, sum(1 for d in window if rule(d)), len(window)


def weather_stat(name: str, metric: str, year: int | None = None,
                 start: date | None = None, end: date | None = None) -> WeatherStat:
    hub = find_hub(name)
    weather, _, _, _, meta = _snapshot()
    days = weather.get(hub.id, [])
    if year is not None:
        start, end = date(year, 1, 1), date(year, 12, 31)
    if start is None or end is None:
        raise ValueError("Provide either year or both start and end dates")
    rule, hits, observed = compute_weather_stat(days, metric, start, end, scoring_config())
    expected = (end - start).days + 1
    note = None
    if observed == 0:
        note = (f"No data in this period. Snapshot covers {meta['weather_start']} to "
                f"{meta['weather_end']}.")
    elif observed < expected:
        note = f"Partial coverage: {observed} of {expected} days available in the snapshot."
    return WeatherStat(hub_id=hub.id, hub_name=hub.label, metric=metric, rule=rule,
                       period_start=start.isoformat(), period_end=end.isoformat(),
                       days_matching=hits, days_observed=observed,
                       percent=round(100.0 * hits / observed, 1) if observed else 0.0, note=note)
