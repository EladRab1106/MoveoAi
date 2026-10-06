"""Central settings: paths, environment variables and the scoring config."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
HUBS_FILE = DATA_DIR / "hubs.yaml"
SCORING_FILE = CONFIG_DIR / "scoring.yaml"
DB_PATH = Path(os.getenv("WEATHER_DB_PATH", DATA_DIR / "weather.db"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
# low | medium | high | xhigh | max. medium keeps chat latency reasonable for a tool-driven Q&A agent.
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "medium")
# Only needed for API keys that are not scoped to a workspace.
ANTHROPIC_WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")

# Vercel sets VERCEL=1. Its filesystem is read-only outside /tmp.
ON_VERCEL = os.getenv("VERCEL") == "1"


@lru_cache
def scoring_config() -> dict:
    with SCORING_FILE.open() as f:
        return yaml.safe_load(f)
