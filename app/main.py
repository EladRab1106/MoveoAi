"""FastAPI app: the agent API used by the chat UI, plus raw scoring endpoints.

Local:  uvicorn app.main:app --reload   ->  http://localhost:8000
Vercel: zero-config FastAPI detects `app` in app/main.py; public/ is served by Vercel's CDN.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.agent.agent import AgentError, run_agent
from app.agent.schemas import ChatRequest, ChatResponse
from app.config import (ALERT_WEBHOOK_URL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, CRON_SECRET,
                        ROOT_DIR, scoring_config)
from app.hubs import HubNotFound, load_hubs
from app.scoring import engine
from app.scoring.models import HubRisk

app = FastAPI(
    title="Weather Risk Intelligence Agent",
    description="Ranks logistics hubs by weather-disruption exposure and explains why.",
    version="0.1.0",
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": ANTHROPIC_MODEL, "snapshot": engine.snapshot_meta(),
            "hubs": len(load_hubs()),
            # presence only, never the value
            "config": {"anthropic_api_key": bool(ANTHROPIC_API_KEY), "cron_secret": bool(CRON_SECRET),
                       "alert_webhook": bool(ALERT_WEBHOOK_URL)}}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        return run_agent(req.messages)
    except AgentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # never return a bare 500; log the traceback for debugging
        logging.exception("chat failed")
        raise HTTPException(status_code=500,
                            detail=f"Unexpected server error: {exc.__class__.__name__}") from exc


@app.get("/api/hubs")
def hubs() -> list[dict]:
    return [h.model_dump() for h in load_hubs()]


@app.get("/api/scores", response_model=list[HubRisk])
def scores(region: str | None = Query(None, description="Midwest | Northeast | South | West"),
           hazard: str | None = Query(None, description="Rank by one hazard sub-score")) -> list[HubRisk]:
    try:
        return engine.get_scores(region=region, hazard=hazard)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/hubs/{hub}/risk", response_model=HubRisk)
def hub_risk(hub: str) -> HubRisk:
    try:
        return engine.get_hub_risk(hub)
    except HubNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/methodology")
def methodology() -> dict:
    cfg = scoring_config()
    return {k: cfg[k] for k in ("disruption_thresholds", "hazards", "composite_weights", "tiers")}


# ------------------------------------------------------------------ alerts (bonus, isolated)
# The alerts module is imported lazily so a missing/broken Redis or webhook config can only
# affect these two endpoints, never chat or scoring.

@app.api_route("/api/alerts/check", methods=["GET", "POST"])
def alerts_check(authorization: str | None = Header(None)) -> dict:
    """Recompute scores (with live NWS alerts), diff against the last snapshot, notify.
    GET is what Vercel Cron calls; POST is for manual triggers."""
    _require_cron_secret(authorization)
    try:
        from app.alerts.service import run_check
        return run_check()
    except Exception as exc:
        logging.exception("alert check failed")
        raise HTTPException(status_code=503, detail=f"Alert check unavailable: {exc}") from exc


def _require_cron_secret(authorization: str | None) -> None:
    if CRON_SECRET and not hmac.compare_digest(authorization or "", f"Bearer {CRON_SECRET}"):
        raise HTTPException(status_code=401, detail="Missing or invalid CRON_SECRET bearer token")


@app.post("/api/alerts/test")
def alerts_send_test(authorization: str | None = Header(None)) -> dict:
    """Send a marked TEST alert to ALERT_WEBHOOK_URL (no effect on snapshot or alert log)."""
    _require_cron_secret(authorization)
    try:
        from app.alerts.service import send_test_alert
        return send_test_alert()
    except Exception as exc:
        logging.exception("test alert failed")
        raise HTTPException(status_code=503, detail=f"Test alert unavailable: {exc}") from exc


@app.post("/api/alerts/webhook-test-sink")
async def alerts_webhook_sink(request: Request) -> dict:
    """Built-in test webhook receiver. Accepts only payloads signed with CRON_SECRET."""
    from app.alerts.service import SIGNATURE_HEADER, record_webhook_delivery, verify_signature
    if not CRON_SECRET:
        raise HTTPException(status_code=503, detail="Test sink disabled: CRON_SECRET not set")
    body = await request.body()
    if len(body) > 64_000 or not verify_signature(body, request.headers.get(SIGNATURE_HEADER),
                                                  CRON_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook signature")
    try:
        import json as _json
        return {"received": record_webhook_delivery(_json.loads(body))}
    except Exception as exc:
        logging.exception("webhook sink failed")
        raise HTTPException(status_code=503, detail=f"Sink unavailable: {exc}") from exc


@app.get("/api/alerts")
def alerts_recent(limit: int = Query(20, ge=1, le=200)) -> dict:
    try:
        from app.alerts.service import recent_alerts
        return recent_alerts(limit)
    except Exception as exc:
        logging.exception("reading alerts failed")
        raise HTTPException(status_code=503, detail=f"Alerts unavailable: {exc}") from exc


# Local dev only: on Vercel, public/ is served statically before reaching Python.
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    # no-cache: always revalidate so a stale page is never served after an update
    return FileResponse(ROOT_DIR / "public" / "index.html", headers={"Cache-Control": "no-cache"})
