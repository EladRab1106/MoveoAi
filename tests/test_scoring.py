from datetime import date, timedelta

import pytest

from app.config import scoring_config
from app.hubs import Hub
from app.scoring.engine import (ActiveAlert, aggregate_nri, alert_bump, apply_alerts,
                                compute_weather_stat, disruption_days_per_year,
                                disruption_rules, full_years, min_max, rank, score_portfolio,
                                tier_for)

CFG = scoring_config()


def make_year(year: int, overrides_by_day: dict | None = None) -> list[dict]:
    """A calm year of daily rows; overrides_by_day maps day-of-year index -> field values."""
    d0 = date(year, 1, 1)
    n = 366 if year % 4 == 0 else 365
    rows = []
    for i in range(n):
        row = {"date": (d0 + timedelta(days=i)).isoformat(), "snowfall_cm": 0.0, "precip_mm": 0.0,
               "gust_kmh": 20.0, "tmin_c": 10.0, "tmax_c": 20.0}
        row.update((overrides_by_day or {}).get(i, {}))
        rows.append(row)
    return rows


def hub(id_, region="Midwest"):
    return Hub(id=id_, name=id_.title(), state="XX", region=region, lat=0, lon=0,
               county="X", county_fips="00000")


def test_winter_rule_counts_snow_or_extreme_cold():
    rule = disruption_rules(CFG)["winter"][1]
    days = make_year(2023, {0: {"snowfall_cm": 3.0}, 1: {"tmin_c": -20.0},
                              2: {"snowfall_cm": 1.0}})   # 1 cm is below threshold
    assert disruption_days_per_year(days, rule, [2023]) == 2


def test_missing_values_never_trigger():
    rule = disruption_rules(CFG)["heat"][1]
    assert not rule({"tmax_c": None})


def test_full_years_excludes_partial():
    days = make_year(2022) + make_year(2023)[:100]
    assert full_years(days) == [2022]


def test_min_max_and_degenerate_case():
    assert min_max({"a": 0, "b": 5, "c": 10}) == {"a": 0.0, "b": 50.0, "c": 100.0}
    assert min_max({"a": 3, "b": 3}) == {"a": 0.0, "b": 0.0}


def test_aggregate_nri_treats_not_applicable_as_zero():
    assert aggregate_nri({"IFLD": 40.0, "CFLD": None}, "max") == 40.0
    assert aggregate_nri({"WNTW": 60.0, "ISTM": None}, "mean") == 30.0


def test_tiers():
    assert tier_for(55, CFG) == "Critical"
    assert tier_for(45, CFG) == "High"
    assert tier_for(30, CFG) == "Moderate"
    assert tier_for(5, CFG) == "Low"


def test_portfolio_scoring_is_explainable_and_ranked():
    snowy = make_year(2023, {i: {"snowfall_cm": 5.0} for i in range(20)})
    calm = make_year(2023)
    weather = {"snowy": snowy, "calm": calm}
    nri = {"snowy": {"WNTW": 80.0, "ISTM": 60.0}, "calm": {"WNTW": 10.0, "ISTM": 0.0}}
    scores = score_portfolio([hub("snowy"), hub("calm")], weather, nri, CFG)

    w = next(h for h in scores["snowy"].hazards if h.hazard == "winter")
    assert w.frequency.days_per_year == 20
    assert w.frequency.normalized == 100
    assert w.long_term.score == 70  # mean(80, 60)
    assert w.score == pytest.approx(0.6 * 100 + 0.4 * 70)
    # composite is exactly the sum of contributions
    s = scores["snowy"]
    assert s.composite_score == pytest.approx(sum(h.composite_weight * h.score for h in s.hazards), abs=0.1)
    assert s.top_drivers[0] == "winter"

    ranked = rank(list(scores.values()), hazard="winter")
    assert [r.hub_id for r in ranked] == ["snowy", "calm"]
    assert ranked[0].rank == 1


def test_alert_bump_capped_and_changes_tier():
    alerts = [ActiveAlert(event="Hurricane Warning", severity="Extreme", headline=None)] * 3
    assert alert_bump(alerts, CFG) == CFG["active_alert_bump"]["max_bump"]
    scores = score_portfolio([hub("a"), hub("b")], {"a": make_year(2023), "b": make_year(2023)},
                             {}, CFG)
    bumped = apply_alerts(scores["a"], alerts, CFG)
    assert bumped.composite_score == scores["a"].base_score + 15
    assert bumped.base_score == scores["a"].base_score


def test_weather_stat_percent():
    days = make_year(2025, {i: {"snowfall_cm": 1.0} for i in range(73)})
    rule, hits, observed = compute_weather_stat(days, "snowfall", date(2025, 1, 1),
                                                date(2025, 12, 31), CFG)
    assert (hits, observed) == (73, 365)
    with pytest.raises(ValueError):
        compute_weather_stat(days, "fog", date(2025, 1, 1), date(2025, 12, 31), CFG)


def test_ties_share_rank_and_are_listed():
    a = make_year(2023, {i: {"precip_mm": 60.0} for i in range(4)})
    b = make_year(2023, {i: {"precip_mm": 60.0} for i in range(4)})
    c = make_year(2023, {i: {"precip_mm": 60.0} for i in range(1)})
    scores = score_portfolio([hub("a"), hub("b"), hub("c")], {"a": a, "b": b, "c": c}, {}, CFG)
    fa = next(h for h in scores["a"].hazards if h.hazard == "flood").frequency
    fc = next(h for h in scores["c"].hazards if h.hazard == "flood").frequency
    assert (fa.portfolio_rank, fa.tied_with) == (1, ["b"])
    assert (fc.portfolio_rank, fc.tied_with) == (3, [])
    ranked = rank(list(scores.values()), hazard="flood")
    assert [(r.hub_id, r.rank) for r in ranked] == [("a", 1), ("b", 1), ("c", 3)]
    assert ranked[0].tied_with == ["b"]
