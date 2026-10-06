"""Build the data snapshot (data/weather.db) from the public APIs.

    python -m scripts.ingest            # all hubs
    python -m scripts.ingest dallas     # selected hubs
    python -m scripts.ingest --only declarations   # refresh one source for all hubs

Run locally, then commit data/weather.db; the deployed app reads it read-only.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

from app.config import scoring_config
from app.data_sources import fema_nri, open_meteo, openfema
from app.hubs import find_hub, load_hubs
from app.storage.db import SCHEMA, session, set_meta


def history_range() -> tuple[date, date]:
    years = scoring_config()["history"]["years"]
    today = date.today()
    start = date(today.year - years, 1, 1)
    end = today - timedelta(days=7)  # ERA5 archive lags ~5 days
    return start, end


SOURCES = ("weather", "nri", "declarations")


def main(hub_ids: list[str], only: set[str]) -> None:
    hubs = [find_hub(h) for h in hub_ids] if hub_ids else list(load_hubs())
    start, end = history_range()
    nri_version = None
    with session(read_only=False) as conn:
        if "declarations" in only:  # recreate: schema may have gained columns
            conn.execute("DROP TABLE IF EXISTS disaster_declaration")
            conn.executescript(SCHEMA)
        for hub in hubs:
            print(f"[{hub.id}]", end=" ", flush=True)
            if "weather" in only:
                ingest_weather(conn, hub, start, end)
            if "nri" in only:
                nri_version = ingest_nri(conn, hub)
            if "declarations" in only:
                ingest_declarations(conn, hub)
            print()
            conn.commit()
        if "weather" in only:
            set_meta(conn, "weather_start", start.isoformat())
            set_meta(conn, "weather_end", end.isoformat())
        if nri_version:
            set_meta(conn, "nri_version", nri_version)
        set_meta(conn, "ingested_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print("done")


def ingest_weather(conn, hub, start, end) -> None:
    rows = open_meteo.fetch_daily(hub, start, end)
    conn.execute("DELETE FROM daily_weather WHERE hub_id = ?", (hub.id,))
    conn.executemany(
        "INSERT INTO daily_weather VALUES "
        "(:hub_id, :date, :snowfall_cm, :precip_mm, :gust_kmh, :tmin_c, :tmax_c)", rows)
    print(f"weather {len(rows)} days", end=" | ", flush=True)


def ingest_nri(conn, hub) -> str | None:
    nri_rows, version = fema_nri.fetch_county(hub.county_fips)
    conn.execute("DELETE FROM nri_hazard WHERE hub_id = ?", (hub.id,))
    conn.executemany(
        "INSERT INTO nri_hazard VALUES (:hub_id, :hazard, :loss_rate_pctl, :annual_freq, "
        ":risk_score, :risk_rating, :eal_usd)",
        [{**r, "hub_id": hub.id} for r in nri_rows])
    print("NRI ok", end=" | ", flush=True)
    return version


def ingest_declarations(conn, hub) -> None:
    decls = openfema.fetch_declarations(hub.county_fips)
    conn.execute("DELETE FROM disaster_declaration WHERE hub_id = ?", (hub.id,))
    conn.executemany(
        "INSERT INTO disaster_declaration VALUES (:hub_id, :disaster_number, "
        ":declaration_type, :incident_type, :title, :declaration_date)",
        [{**d, "hub_id": hub.id} for d in decls])
    print(f"{len(decls)} declarations", end="", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("hubs", nargs="*", help="hub names/ids (default: all)")
    ap.add_argument("--only", nargs="+", choices=SOURCES, default=list(SOURCES))
    a = ap.parse_args()
    main(a.hubs, set(a.only))
