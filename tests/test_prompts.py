from datetime import date

from app.agent.prompts import system_prompt


def test_last_year_is_last_full_calendar_year():
    p = system_prompt(date(2026, 10, 6))
    assert '"Last year" always means the last full calendar year, 2025' in p
    assert "never the trailing 12 months" in p
    assert "January 1 to December 31, 2025" in p
