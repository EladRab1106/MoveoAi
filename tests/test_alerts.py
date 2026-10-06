import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.alerts import service, store as store_mod
from app.alerts.detector import HubSnapshot, detect_changes
from app.alerts.store import RedisRestStore, SqliteStore
from app.scoring.models import ActiveAlert

NOW = "2026-10-06T12:00:00+00:00"


def snap(hub_id, score, tier):
    return HubSnapshot(hub_id=hub_id, name=hub_id.title(), score=score, tier=tier)


# ------------------------------------------------------------ detector (pure)

def test_detects_score_jump_and_tier_change():
    prev = {"a": snap("a", 38.0, "Moderate"), "b": snap("b", 20.0, "Low"), "c": snap("c", 30.0, "Moderate")}
    cur = {"a": snap("a", 43.0, "High"), "b": snap("b", 22.0, "Low"), "c": snap("c", 29.9, "Low")}
    alerts = {a.hub_id: a for a in detect_changes(prev, cur, 5.0, NOW)}
    assert set(alerts) == {"a", "c"}                     # b moved only 2 points, same tier
    assert alerts["a"].delta == 5.0 and len(alerts["a"].reasons) == 2
    assert alerts["c"].reasons == ["tier changed Moderate -> Low"]


def test_new_hub_is_baselined_silently():
    assert detect_changes({}, {"a": snap("a", 90, "Critical")}, 5.0, NOW) == []


# ------------------------------------------------------------ service (fake deps)

@pytest.fixture
def tmp_store(tmp_path):
    return SqliteStore(tmp_path / "alerts.db", persistent=True)


def no_live():
    return {}, []


def test_first_run_baselines_then_live_alert_triggers_webhook(tmp_store):
    first = service.run_check(store=tmp_store, live_fetcher=no_live, webhook_url="")
    assert first["status"].startswith("baseline") and first["alerts"] == []
    assert first["webhook"] == "not configured"

    sent = []
    hurricane = {"miami": [ActiveAlert(event="Hurricane Warning", severity="Extreme", headline=None)]}
    second = service.run_check(
        store=tmp_store, live_fetcher=lambda: (hurricane, []), webhook_url="https://hook.test",
        webhook_sender=lambda url, alerts: sent.append((url, alerts)) or "sent (HTTP 200)")
    assert [a["hub_id"] for a in second["alerts"]] == ["miami"]
    assert second["alerts"][0]["delta"] == 10.0          # Extreme alert bump
    assert second["alerts"][0]["live_alerts"] == ["Hurricane Warning"]
    assert second["webhook"] == "sent (HTTP 200)" and sent[0][0] == "https://hook.test"

    recent = service.recent_alerts(store=tmp_store)
    assert recent["alerts"][0]["hub_id"] == "miami"
    assert recent["last_check"]["status"] == "1 change(s) detected"


def test_unchanged_scores_produce_no_alerts_and_no_webhook(tmp_store):
    service.run_check(store=tmp_store, live_fetcher=no_live, webhook_url="")
    calls = []
    res = service.run_check(store=tmp_store, live_fetcher=no_live, webhook_url="https://hook.test",
                            webhook_sender=lambda *a: calls.append(a) or "sent")
    assert res["alerts"] == [] and calls == []


def test_nws_errors_are_reported_not_raised(tmp_store):
    res = service.run_check(store=tmp_store, live_fetcher=lambda: ({}, ["dallas: timeout"]),
                            webhook_url="")
    assert res["nws_errors"] == ["dallas: timeout"]


def test_webhook_failure_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(service.httpx, "post", boom)
    assert service.send_webhook("https://hook.test", []).startswith("failed")


# ------------------------------------------------------------ redis REST store (mocked HTTP)

def test_redis_rest_store_speaks_upstash_protocol(monkeypatch):
    data: dict = {}

    def fake_post(url, json, headers, timeout):
        assert headers["Authorization"] == "Bearer tok"
        cmd, key, *rest = json
        if cmd == "SET":
            data[key] = rest[0]; result = "OK"
        elif cmd == "GET":
            result = data.get(key)
        elif cmd == "LPUSH":
            data[key] = list(reversed(rest)) + data.get(key, []); result = len(data[key])
        elif cmd == "LTRIM":
            data[key] = data[key][int(rest[0]):int(rest[1]) + 1]; result = "OK"
        elif cmd == "LRANGE":
            result = data.get(key, [])[int(rest[0]):int(rest[1]) + 1]
        return httpx.Response(200, json={"result": result}, request=httpx.Request("POST", url))

    monkeypatch.setattr(store_mod.httpx, "post", fake_post)
    s = RedisRestStore("https://redis.test/", "tok")
    s.set("k", "v")
    assert s.get("k") == "v"
    s.push("log", ["1", "2", "3"], cap=2)
    assert s.recent("log", 10) == ["3", "2"]


def test_store_selection(monkeypatch):
    monkeypatch.setenv("KV_REST_API_URL", "https://redis.test")
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
    assert store_mod.get_store().backend == "upstash-redis"
    monkeypatch.delenv("KV_REST_API_URL"); monkeypatch.delenv("KV_REST_API_TOKEN")
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    assert store_mod.get_store().backend == "sqlite"


# ------------------------------------------------------------ endpoints: auth + isolation

def test_check_endpoint_requires_cron_secret(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "CRON_SECRET", "s3cret")
    client = TestClient(main.app)
    assert client.get("/api/alerts/check").status_code == 401
    assert client.get("/api/alerts/check", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_broken_alert_backend_does_not_affect_core_endpoints(monkeypatch):
    from app import main
    def broken(*a, **k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(service, "recent_alerts", broken)
    client = TestClient(main.app)
    assert client.get("/api/alerts").status_code == 503
    assert client.get("/api/scores").status_code == 200
    assert client.get("/api/health").status_code == 200


# ------------------------------------------------------------ signed webhooks + test sink

def test_signature_roundtrip_and_tamper_detection():
    body = b'{"text":"x"}'
    sig = service.sign(body, "s3cret")
    assert service.verify_signature(body, sig, "s3cret")
    assert not service.verify_signature(body + b" ", sig, "s3cret")
    assert not service.verify_signature(body, None, "s3cret")


def test_sender_signs_payload(monkeypatch):
    captured = {}

    def fake_post(url, content, headers, timeout):
        captured.update(body=content, headers=headers)
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr(service, "CRON_SECRET", "s3cret")
    monkeypatch.setattr(service.httpx, "post", fake_post)
    assert service.send_webhook("https://hook.test", [], test=True).startswith("sent")
    assert service.verify_signature(captured["body"], captured["headers"][service.SIGNATURE_HEADER], "s3cret")
    assert json.loads(captured["body"])["test"] is True


def test_send_test_alert_is_marked_and_skips_without_url(tmp_store):
    assert service.send_test_alert(webhook_url="")["sent"] is False
    sent = []
    res = service.send_test_alert(webhook_url="https://hook.test",
                                  webhook_sender=lambda url, alerts, test: sent.append((alerts, test)) or "sent (HTTP 200)")
    assert res["sent"] and sent[0][1] is True
    assert "TEST" in sent[0][0][0].reasons[0]


def test_sink_accepts_only_signed_payloads(monkeypatch, tmp_store):
    from app import main
    monkeypatch.setattr(main, "CRON_SECRET", "s3cret")
    monkeypatch.setattr(service, "get_store", lambda: tmp_store)
    client = TestClient(main.app)
    body = json.dumps({"text": "TEST weather risk alert (1 hub(s))\n• x", "test": True,
                       "alerts": [{"hub_id": "dallas"}]}).encode()
    url = "/api/alerts/webhook-test-sink"
    assert client.post(url, content=body).status_code == 401
    assert client.post(url, content=body, headers={service.SIGNATURE_HEADER: "sha256=bad"}).status_code == 401
    ok = client.post(url, content=body, headers={service.SIGNATURE_HEADER: service.sign(body, "s3cret")})
    assert ok.status_code == 200 and ok.json()["received"]["hubs"] == ["dallas"]
    inbox = service.recent_alerts(store=tmp_store)["webhook_test_inbox"]
    assert inbox[0]["test"] is True and inbox[0]["headline"].startswith("TEST")


def test_test_alert_endpoint_requires_secret(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "CRON_SECRET", "s3cret")
    assert TestClient(main.app).post("/api/alerts/test").status_code == 401


def test_baseline_run_reports_configured_webhook(tmp_store):
    res = service.run_check(store=tmp_store, live_fetcher=no_live, webhook_url="https://hook.test")
    assert res["webhook"] == "configured (baseline run, nothing to send)"
