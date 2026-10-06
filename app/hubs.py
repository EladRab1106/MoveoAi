"""Hub registry: loads data/hubs.yaml and resolves free-text hub names."""

from __future__ import annotations

from functools import lru_cache

import yaml
from pydantic import BaseModel, Field

from app.config import HUBS_FILE

REGIONS = ("Midwest", "Northeast", "South", "West")


class Hub(BaseModel):
    id: str
    name: str
    state: str
    region: str
    lat: float
    lon: float
    county: str
    county_fips: str = Field(pattern=r"^\d{5}$")
    aliases: list[str] = []

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


class HubNotFound(LookupError):
    pass


@lru_cache
def load_hubs() -> tuple[Hub, ...]:
    with HUBS_FILE.open() as f:
        raw = yaml.safe_load(f)["hubs"]
    hubs = tuple(Hub(**h) for h in raw)
    ids = [h.id for h in hubs]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate hub ids in hubs.yaml")
    bad = [h.id for h in hubs if h.region not in REGIONS]
    if bad:
        raise ValueError(f"Unknown region for hubs: {bad}")
    return hubs


def _norm(s: str) -> str:
    return " ".join(s.lower().replace(".", "").replace("_", " ").replace("-", " ").split())


def find_hub(query: str) -> Hub:
    """Resolve 'Dallas', 'dallas', 'Dallas, TX', 'st louis', 'NYC' to a Hub."""
    q = _norm(query.split(",")[0])
    for hub in load_hubs():
        candidates = {_norm(hub.id), _norm(hub.name), *(_norm(a) for a in hub.aliases)}
        if q in candidates:
            return hub
    raise HubNotFound(
        f"No hub named '{query}'. Known hubs: {', '.join(h.name for h in load_hubs())}"
    )


def hubs_in_region(region: str | None) -> list[Hub]:
    if region is None:
        return list(load_hubs())
    match = next((r for r in REGIONS if r.lower() == region.strip().lower()), None)
    if match is None:
        raise ValueError(f"Unknown region '{region}'. Use one of: {', '.join(REGIONS)}")
    return [h for h in load_hubs() if h.region == match]
