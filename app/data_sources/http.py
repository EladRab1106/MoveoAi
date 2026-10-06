"""Shared HTTP client with retries for the public data APIs."""

from __future__ import annotations

import time

import httpx

USER_AGENT = "weather-risk-intelligence-agent/0.1 (github.com/EladRab1106/MoveoAi)"


def get_json(url: str, params: dict | None = None, *, retries: int = 3, timeout: float = 30.0,
             headers: dict | None = None) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = httpx.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": USER_AGENT, **(headers or {})})
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request,
                                            response=resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            time.sleep(1.5 * 2**attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_exc}") from last_exc
