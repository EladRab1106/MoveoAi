from evals.run import extract_numbers, grounding

TOOLS = ['{"percent":8.5,"days_matching":31,"days_observed":365,"score":92.7,"other":63.0}']


def test_rounding_and_percent_are_grounded():
    assert grounding("About 8.5% of days (31 of 365); score ~93.", TOOLS, "") == []


def test_derived_difference_is_grounded():
    assert grounding("Miami leads by 29.7 points.", TOOLS, "") == []


def test_invented_number_is_flagged():
    assert grounding("Roughly 47.3% of days had snow.", TOOLS, "") == [47.3]


def test_years_small_counts_and_user_numbers_ignored():
    assert grounding("In 2025 the top 3 hubs, as you asked for 15.", TOOLS, "show 15 hubs") == []


def test_dates_are_not_numbers():
    assert extract_numbers("between 2024-12-01 and 2025-02-28") == []


def test_unit_conversion_only_with_unit_suffix():
    tools = ['{"tmax_c":38.0}']
    assert grounding("Days above 100°F are common.", tools, "") == []
    assert grounding("A score of 100.4 overall.", tools, "") == [100.4]


def test_numbers_attributed_to_period():
    from evals.run import numbers_attributed_to
    ok = ("I can't give a 2012 number: history starts 2021-01-01. "
          "For context, 2025 had 8.5% snow days.")
    assert numbers_attributed_to(ok, "2012") == []
    bad = "In 2012 roughly 12% of days had snow."
    assert numbers_attributed_to(bad, "2012") == ["12"]


def test_durations_are_not_statistics():
    from evals.run import numbers_attributed_to
    s = "Label it 2025 rather than a stand-in for 2012, a year 13 years earlier."
    assert numbers_attributed_to(s, "2012") == []
    assert numbers_attributed_to("2012 had 40 days of snow.", "2012") == ["40"]


def test_no_phrases_regex_catches_spending_verdicts():
    import yaml
    from pathlib import Path
    from evals.run import run_checks
    from app.agent.schemas import ChatResponse, LLMAnswer
    cases = yaml.safe_load(Path("evals/cases.yaml").read_text())["cases"]
    spec = next(c for c in cases if c["id"] == "followup_region_switch")["turns"][1]["checks"]["no_phrases"]

    def check(text):
        ans = LLMAnswer(answer=text, hub_refs=[], reasoning=[], assumptions=[], data_sources=[],
                        confidence="high", confidence_reason="", follow_up_suggestions=[], in_scope=True)
        resp = ChatResponse(answer=ans, assistant_message="", hubs=[], tool_calls=[], model="m",
                            latency_ms=0, usage={})
        return run_checks({"no_phrases": spec}, resp, [], "")["no_phrases"][0]

    assert not check("Memphis and Charlotte are the only South candidates worth a look.")
    assert not check("The rest aren't worth winter spend.")
    assert check("Memphis (18.0) and Charlotte (17.4) rank highest on winter in the South.")
    assert not check("The remaining hubs aren’t worth it.")   # typographic apostrophe


def test_period_attribution_uses_nearest_year():
    from evals.run import numbers_attributed_to
    labeled = ("For context (clearly labeled as a different period, not a substitute for 2012): "
               "in 2025, Denver had measurable snowfall on 31 of 365 days, i.e. 8.5%.")
    assert numbers_attributed_to(labeled, "2012") == []
    assert numbers_attributed_to("Unlike 2025, in 2012 about 12% of days had snow.", "2012") == ["12"]
    assert numbers_attributed_to("2025 and 2012 both saw 30 days of snow.", "2012") == ["30"]  # nearest is 2012
    assert numbers_attributed_to("2012 and 2025 both saw 30 days of snow.", "2012") == []      # nearest is 2025
    assert numbers_attributed_to("Data starts 2021-01-01, so 2012 has 0 days observed.", "2012") == ["0"]


def test_grounding_accepts_system_prompt_facts():
    from evals.run import grounding, system_prompt_facts
    text = "Scores are relative to this 22-hub portfolio."
    assert grounding(text, ['{"alerts":[]}'], "") == [22.0]
    assert grounding(text, ['{"alerts":[]}', system_prompt_facts()], "") == []


def test_claims_verdict_fails_only_on_false_or_overstated():
    from evals.run import claims_verdict
    ok, detail = claims_verdict({"unsupported_claims": [
        {"claim": "ranked 1st, tied", "false_or_overstated": False, "why": "correct per data"}], "comment": ""})
    assert ok and "1 noted as fine" in detail
    ok, detail = claims_verdict({"unsupported_claims": [
        {"claim": "extra severe-storm 3.8 pts", "false_or_overstated": True, "why": "gap is ~0.5"}], "comment": ""})
    assert not ok and "gap is ~0.5" in detail
