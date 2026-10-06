"""Pure change detection between two score snapshots."""

from __future__ import annotations

from pydantic import BaseModel


class HubSnapshot(BaseModel):
    hub_id: str
    name: str
    score: float
    tier: str
    live_alerts: list[str] = []      # NWS event names active at check time


class RiskChangeAlert(BaseModel):
    hub_id: str
    name: str
    previous_score: float
    current_score: float
    delta: float
    previous_tier: str
    current_tier: str
    reasons: list[str]
    live_alerts: list[str]
    detected_at: str


def detect_changes(previous: dict[str, HubSnapshot], current: dict[str, HubSnapshot],
                   threshold: float, detected_at: str) -> list[RiskChangeAlert]:
    """Alert when a hub's score moves by >= threshold points or its tier changes.

    Hubs missing from `previous` (newly added) are baselined silently.
    """
    alerts = []
    for hub_id, cur in current.items():
        prev = previous.get(hub_id)
        if prev is None:
            continue
        delta = round(cur.score - prev.score, 1)
        reasons = []
        if abs(delta) >= threshold:
            reasons.append(f"score {'rose' if delta > 0 else 'fell'} {abs(delta)} points "
                           f"(threshold {threshold})")
        if cur.tier != prev.tier:
            reasons.append(f"tier changed {prev.tier} -> {cur.tier}")
        if reasons:
            alerts.append(RiskChangeAlert(
                hub_id=hub_id, name=cur.name, previous_score=prev.score, current_score=cur.score,
                delta=delta, previous_tier=prev.tier, current_tier=cur.tier, reasons=reasons,
                live_alerts=cur.live_alerts, detected_at=detected_at))
    return sorted(alerts, key=lambda a: -abs(a.delta))
