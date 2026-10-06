"""Risk-change alert check: score now (with live NWS alerts) vs the last snapshot.

Run daily by Vercel Cron (GET /api/alerts/check) or manually. Day to day the change
comes from live NWS alerts; a re-ingested snapshot or edited scoring config also
shows up here. Failures in NWS, storage or the webhook are reported in the result
rather than raised wherever possible, and never affect the chat/scoring endpoints.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

import httpx

from app.alerts.detector import HubSnapshot, RiskChangeAlert, detect_changes
from app.alerts.store import AlertStore, dumps, get_store
from app.config import ALERT_WEBHOOK_URL, CRON_SECRET, scoring_config
from app.data_sources import nws
from app.hubs import load_hubs
from app.scoring import engine
from app.scoring.models import ActiveAlert

log = logging.getLogger(__name__)

SNAPSHOT_KEY = "alerts:snapshot"
LOG_KEY = "alerts:log"
LAST_CHECK_KEY = "alerts:last_check"
INBOX_KEY = "alerts:webhook_inbox"     # deliveries received by the built-in test sink
LOG_CAP = 200
INBOX_CAP = 20
SIGNATURE_HEADER = "X-Weather-Alert-Signature"


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 of the exact request body, so receivers can verify the sender."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    return bool(header) and hmac.compare_digest(sign(body, secret), header)


def fetch_live_alerts() -> tuple[dict[str, list[ActiveAlert]], list[str]]:
    """NWS alerts for every hub, in parallel. Per-hub failures are collected, not raised."""
    hubs = list(load_hubs())
    errors: list[str] = []

    def one(hub):
        try:
            return hub.id, [ActiveAlert(**a) for a in nws.active_alerts(hub)]
        except Exception as exc:
            errors.append(f"{hub.id}: {exc}")
            return hub.id, []

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, hubs)), errors


def current_snapshot(live: dict[str, list[ActiveAlert]]) -> dict[str, HubSnapshot]:
    return {r.hub_id: HubSnapshot(hub_id=r.hub_id, name=f"{r.name}, {r.state}",
                                  score=r.composite_score, tier=r.tier,
                                  live_alerts=[a.event or "" for a in r.active_alerts])
            for r in engine.get_scores(alerts=live)}


def send_webhook(url: str, alerts: list[RiskChangeAlert], test: bool = False) -> str:
    """POST alerts as JSON. `text` makes it render in Slack; `alerts` carries the data.
    When CRON_SECRET is set the body is signed (X-Weather-Alert-Signature: sha256=...)."""
    lines = [f"• {a.name}: {a.previous_score} → {a.current_score} ({a.previous_tier} → "
             f"{a.current_tier}); {'; '.join(a.reasons)}"
             + (f". Active NWS: {', '.join(a.live_alerts)}" if a.live_alerts else "")
             for a in alerts]
    title = "TEST weather risk alert" if test else "Weather risk change alert"
    payload = {"text": f"{title} ({len(alerts)} hub(s))\n" + "\n".join(lines), "test": test,
               "alerts": [a.model_dump() for a in alerts]}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if CRON_SECRET:
        headers[SIGNATURE_HEADER] = sign(body, CRON_SECRET)
    try:
        resp = httpx.post(url, content=body, headers=headers, timeout=10)
        return f"sent (HTTP {resp.status_code})" if resp.is_success else f"failed (HTTP {resp.status_code})"
    except httpx.HTTPError as exc:
        return f"failed ({exc.__class__.__name__})"


def run_check(store: AlertStore | None = None,
              live_fetcher: Callable[[], tuple[dict, list[str]]] = fetch_live_alerts,
              webhook_url: str | None = None,
              webhook_sender: Callable[[str, list[RiskChangeAlert]], str] = send_webhook) -> dict:
    store = store or get_store()
    webhook_url = ALERT_WEBHOOK_URL if webhook_url is None else webhook_url
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    threshold = float(scoring_config()["alerts"]["score_change_threshold"])

    live, nws_errors = live_fetcher()
    current = current_snapshot(live)

    raw_prev = store.get(SNAPSHOT_KEY)
    previous = ({k: HubSnapshot(**v) for k, v in json.loads(raw_prev)["hubs"].items()}
                if raw_prev else None)

    alerts: list[RiskChangeAlert] = []
    webhook_status = "not configured"
    if previous is None:
        status = "baseline saved (first run, nothing to compare)"
    else:
        alerts = detect_changes(previous, current, threshold, now)
        status = f"{len(alerts)} change(s) detected"
        if alerts:
            store.push(LOG_KEY, [dumps(a.model_dump()) for a in alerts], LOG_CAP)
            if webhook_url:
                webhook_status = webhook_sender(webhook_url, alerts)
        elif webhook_url:
            webhook_status = "configured (nothing to send)"

    store.set(SNAPSHOT_KEY, dumps({"taken_at": now,
                                   "hubs": {k: v.model_dump() for k, v in current.items()}}))
    summary = {"checked_at": now, "status": status, "threshold_points": threshold,
               "hubs_checked": len(current),
               "hubs_with_live_nws_alerts": sorted(k for k, v in current.items() if v.live_alerts),
               "alerts": [a.model_dump() for a in alerts], "webhook": webhook_status,
               "nws_errors": nws_errors, "store": {"backend": store.backend,
                                                   "persistent": store.persistent}}
    store.set(LAST_CHECK_KEY, dumps({k: summary[k] for k in
                                     ("checked_at", "status", "hubs_checked", "webhook")}))
    log.info("alert check: %s", status)
    return summary


def recent_alerts(limit: int = 20, store: AlertStore | None = None) -> dict:
    store = store or get_store()
    last = store.get(LAST_CHECK_KEY)
    return {"store": {"backend": store.backend, "persistent": store.persistent},
            "webhook_configured": bool(ALERT_WEBHOOK_URL),
            "last_check": json.loads(last) if last else None,
            "alerts": [json.loads(v) for v in store.recent(LOG_KEY, limit)],
            "webhook_test_inbox": [json.loads(v) for v in store.recent(INBOX_KEY, 5)]}


def send_test_alert(store: AlertStore | None = None, webhook_url: str | None = None,
                    webhook_sender: Callable[..., str] = send_webhook) -> dict:
    """Send a clearly marked synthetic alert to the webhook. Doesn't touch the snapshot or
    the real alert log, so it can't hide or fake a real risk change."""
    webhook_url = ALERT_WEBHOOK_URL if webhook_url is None else webhook_url
    if not webhook_url:
        return {"sent": False, "webhook": "not configured (set ALERT_WEBHOOK_URL)"}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    hub = next(iter(engine.get_scores()))
    alert = RiskChangeAlert(
        hub_id=hub.hub_id, name=f"{hub.name}, {hub.state}", previous_score=hub.composite_score,
        current_score=hub.composite_score, delta=0.0, previous_tier=hub.tier, current_tier=hub.tier,
        reasons=["TEST notification: no real risk change"], live_alerts=[], detected_at=now)
    status = webhook_sender(webhook_url, [alert], test=True)
    return {"sent": status.startswith("sent"), "webhook": status, "sent_at": now}


def record_webhook_delivery(payload: dict, store: AlertStore | None = None) -> dict:
    """Built-in test receiver: keep the last deliveries so a demo can show them arriving."""
    store = store or get_store()
    received = {"received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "test": bool(payload.get("test")),
                "headline": str(payload.get("text", "")).split("\n")[0][:200],
                "hubs": [a.get("hub_id") for a in payload.get("alerts", [])][:25]}
    store.push(INBOX_KEY, [dumps(received)], INBOX_CAP)
    return received
