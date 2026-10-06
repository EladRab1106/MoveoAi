"""Persistence for alert snapshots and the alert log.

Backends (picked automatically by `get_store()`):
  * Upstash Redis over its REST API, when UPSTASH_REDIS_REST_URL/TOKEN (or the Vercel KV
    names KV_REST_API_URL/TOKEN) are set. Persistent across serverless invocations.
  * SQLite fallback otherwise: data/alerts.db locally, /tmp/alerts.db on Vercel (ephemeral
    there, which the API reports as persistent=false).

Only this module knows about storage; the rest of the app never imports it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Protocol

import httpx

from app.config import DATA_DIR, ON_VERCEL


class AlertStore(Protocol):
    backend: str
    persistent: bool

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def push(self, key: str, values: list[str], cap: int) -> None: ...
    def recent(self, key: str, limit: int) -> list[str]: ...


class RedisRestStore:
    """Minimal Upstash Redis REST client (POST a JSON command array)."""

    backend = "upstash-redis"
    persistent = True

    def __init__(self, url: str, token: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout

    def _cmd(self, *args) -> object:
        resp = httpx.post(self.url, json=[str(a) for a in args], headers=self.headers,
                          timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"Redis error: {body['error']}")
        return body.get("result")

    def get(self, key: str) -> str | None:
        return self._cmd("GET", key)  # type: ignore[return-value]

    def set(self, key: str, value: str) -> None:
        self._cmd("SET", key, value)

    def push(self, key: str, values: list[str], cap: int) -> None:
        if values:
            self._cmd("LPUSH", key, *values)
            self._cmd("LTRIM", key, 0, cap - 1)

    def recent(self, key: str, limit: int) -> list[str]:
        return list(self._cmd("LRANGE", key, 0, limit - 1) or [])  # type: ignore[arg-type]


class SqliteStore:
    backend = "sqlite"

    def __init__(self, path: Path, persistent: bool):
        self.path = path
        self.persistent = persistent
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                key TEXT, value TEXT);
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    def push(self, key: str, values: list[str], cap: int) -> None:
        with self._conn() as c:
            c.executemany("INSERT INTO log (key, value) VALUES (?, ?)", [(key, v) for v in values])
            c.execute("DELETE FROM log WHERE key = ? AND id NOT IN "
                      "(SELECT id FROM log WHERE key = ? ORDER BY id DESC LIMIT ?)", (key, key, cap))

    def recent(self, key: str, limit: int) -> list[str]:
        with self._conn() as c:
            rows = c.execute("SELECT value FROM log WHERE key = ? ORDER BY id DESC LIMIT ?",
                             (key, limit)).fetchall()
        return [r[0] for r in rows]


def get_store() -> AlertStore:
    url = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN")
    if url and token:
        return RedisRestStore(url, token)
    if ON_VERCEL:
        return SqliteStore(Path("/tmp/alerts.db"), persistent=False)
    return SqliteStore(Path(os.getenv("ALERTS_DB_PATH", DATA_DIR / "alerts.db")), persistent=True)


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)
