"""Build the data snapshot (data/weather.db) from the public APIs.

    python -m scripts.ingest            # all hubs
    python -m scripts.ingest dallas     # selected hubs

Run locally, then commit data/weather.db; the deployed app reads it read-only.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone

from app.config import scoring_config
from app.data_sources import fema_nri, open_meteo, openfema
from app.hubs import find_hub, load_hubs
from app.storage.db import session, set_meta


def history_range() -> tuple[date, date]:
    years = scoring_config()["history"]["years"]
    today = date.today()
    start = date(today.year - years, 1, 1)
    end = today - timedelta(days=7)  # ERA5 archive lags ~5 days
    return start, end


def main(hub_ids: list[str]) -> None:
    hubs = [find_hub(h) for h in hub_ids] if hub_ids else list(load_hubs())
    start, end = history_range()
    nri_version = None
    with session(read_only=False) as conn:
        for hub in hubs:
            print(f"[{hub.id}] weather {start}..{end}", end=" ", flush=True)
            rows = open_meteo.fetch_daily(hub, start, end)
            conn.execute("DELETE FROM daily_weather WHERE hub_id = ?", (hub.id,))
            conn.executemany(
                "INSERT INTO daily_weather VALUES "
                "(:hub_id, :date, :snowfall_cm, :precip_mm, :gust_kmh, :tmin_c, :tmax_c)", rows)
            print(f"{len(rows)} days", end=" | ", flush=True)

            nri_rows, nri_version = fema_nri.fetch_county(hub.county_fips)
            conn.execute("DELETE FROM nri_hazard WHERE hub_id = ?", (hub.id,))
            conn.executemany(
                "INSERT INTO nri_hazard VALUES (:hub_id, :hazard, :loss_rate_pctl, :annual_freq, "
                ":risk_score, :risk_rating, :eal_usd)",
                [{**r, "hub_id": hub.id} for r in nri_rows])
            print("NRI ok", end=" | ", flush=True)

            decls = openfema.fetch_declarations(hub.county_fips)
            conn.execute("DELETE FROM disaster_declaration WHERE hub_id = ?", (hub.id,))
            conn.executemany(
                "INSERT INTO disaster_declaration VALUES (:hub_id, :disaster_number, "
                ":incident_type, :title, :declaration_date)",
                [{**d, "hub_id": hub.id} for d in decls])
            print(f"{len(decls)} declarations")
            conn.commit()

        set_meta(conn, "weather_start", start.isoformat())
        set_meta(conn, "weather_end", end.isoformat())
        set_meta(conn, "nri_version", nri_version or "unknown")
        set_meta(conn, "ingested_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print("done")


if __name__ == "__main__":
    main(sys.argv[1:])
