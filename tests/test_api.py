from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["hubs"] >= 15


def test_scores_filter_and_rank():
    r = client.get("/api/scores", params={"region": "Midwest", "hazard": "winter"})
    assert r.status_code == 200
    rows = r.json()
    assert all(x["region"] == "Midwest" for x in rows)
    assert [x["rank"] for x in rows] == list(range(1, len(rows) + 1))


def test_bad_hazard_is_400():
    assert client.get("/api/scores", params={"hazard": "volcano"}).status_code == 400


def test_hub_risk_and_404():
    assert client.get("/api/hubs/dallas/risk").json()["hub_id"] == "dallas"
    assert client.get("/api/hubs/gotham/risk").status_code == 404


def test_chat_validates_input():
    assert client.post("/api/chat", json={"messages": []}).status_code == 422


def test_chat_without_credentials_returns_clear_502(monkeypatch):
    from app.agent import agent

    class NoCreds:
        class beta:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise TypeError("Could not resolve authentication method. Expected one of api_key")

    monkeypatch.setattr(agent, "client", lambda: NoCreds)
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_unexpected_chat_error_is_json_500(monkeypatch):
    from app import main

    def boom(_):
        raise RuntimeError("bug")
    monkeypatch.setattr(main, "run_agent", boom)
    r = TestClient(main.app, raise_server_exceptions=False).post(
        "/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 500 and r.json()["detail"] == "Unexpected server error: RuntimeError"


def test_methodology_separates_snapshot_range_from_frequency_window():
    from app.agent.tools import get_methodology
    w = get_methodology()["data_windows"]
    assert w["frequency_window"]["full_calendar_years"] == [2021, 2022, 2023, 2024, 2025]
    assert "full calendar years 2021-2025 only" in w["frequency_window"]["rule"]
    assert w["snapshot_range"]["start"] == "2021-01-01"
    assert w["snapshot_range"]["end"] > "2025-12-31"   # snapshot extends past the frequency window


def test_hub_risk_tool_ranks_contributions_without_changing_scores():
    from app.agent.tools import get_hub_risk
    from app.scoring import engine
    d = get_hub_risk("memphis")
    c = d["contributions_ranked"]
    pts = [x["contribution_points"] for x in c["order"]]
    assert pts == sorted(pts, reverse=True)
    by_hz = {h.hazard: h.contribution for h in engine.get_hub_risk("memphis").hazards}
    assert {x["hazard"]: x["contribution_points"] for x in c["order"]} == by_hz   # same numbers
    assert c["largest"] == [c["order"][0]["hazard"]] and c["smallest"][-1] == c["order"][-1]["hazard"]
    assert d["composite_score"] == engine.get_hub_risk("memphis").composite_score
