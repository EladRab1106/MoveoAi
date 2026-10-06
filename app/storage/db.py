"""SQLite access for the ingested data snapshot.

The snapshot (data/weather.db) is built offline by scripts/ingest.py and committed,
so the deployed app only reads it. On Vercel it's opened read-only.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import DB_PATH, ON_VERCEL

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_weather (
    hub_id       TEXT NOT NULL,
    date         TEXT NOT NULL,          -- ISO yyyy-mm-dd
    snowfall_cm  REAL,
    precip_mm    REAL,
    gust_kmh     REAL,
    tmin_c       REAL,
    tmax_c       REAL,
    PRIMARY KEY (hub_id, date)
);

-- FEMA National Risk Index, one row per (hub, hazard code e.g. HRCN, IFLD)
CREATE TABLE IF NOT EXISTS nri_hazard (
    hub_id       TEXT NOT NULL,
    hazard       TEXT NOT NULL,
    loss_rate_pctl REAL,                 -- <CODE>_ALR_NPCTL: exposure-normalised, used for scoring
    annual_freq  REAL,                   -- <CODE>_AFREQ: modelled events per year
    risk_score   REAL,                   -- <CODE>_RISKS: headline (exposure-driven) score, context only
    risk_rating  TEXT,                   -- <CODE>_RISKR, e.g. 'Very High'
    eal_usd      REAL,                   -- <CODE>_EALT: expected annual loss, USD
    PRIMARY KEY (hub_id, hazard)
);

-- OpenFEMA weather-related disaster declarations for the hub's county
CREATE TABLE IF NOT EXISTS disaster_declaration (
    hub_id          TEXT NOT NULL,
    disaster_number INTEGER NOT NULL,
    declaration_type TEXT,               -- DR major disaster | EM emergency | FM fire mgmt
    incident_type   TEXT,
    title           TEXT,
    declaration_date TEXT,
    PRIMARY KEY (hub_id, disaster_number)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path = DB_PATH, read_only: bool | None = None) -> sqlite3.Connection:
    if read_only is None:
        read_only = ON_VERCEL
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session(path: Path = DB_PATH, read_only: bool | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path, read_only)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
