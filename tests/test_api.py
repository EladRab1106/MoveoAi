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
