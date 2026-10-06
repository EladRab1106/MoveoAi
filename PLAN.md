# Weather Risk Intelligence Agent: Plan

> This is the plan written at the start of the session and kept for reference. The build
> differs in a few places: `app/main.py` became the zero-config Vercel entrypoint (no
> `api/index.py`), the model is `claude-opus-5`, and there's a 17-case eval set. See
> [README.md](README.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for what was built.

## 1. Goal
Build an agent that helps logistics analysts decide which US distribution hubs are most exposed to weather disruption. It must:
- gather real data from public APIs,
- rank hubs with **deterministic** scoring,
- explain its reasoning,
- answer follow-up questions in a chat UI (served over an API), using JSON-schema-enforced output,
- come with an eval set and an optional risk-change alert.

**Scope principle:** narrow and well-reasoned beats broad and shallow.

---

## 2. Scoping and assumptions (state these in the README)
- **Hubs:** a fixed list of about 15 hubs in major US metros, in `data/hubs.yaml` with name, lat/lon, region and county FIPS. Hubs are assumed to sit in the city center.
- **Hazards in scope:** winter (snow/cold), hurricane/high wind, flooding/heavy rain, extreme heat. Out of scope: earthquakes and wildfire.
- **History window:** the last 5 full years of daily data, enough to estimate frequency. "Last year" means the last full calendar year.
- **Disruption day:** a day that crosses an operational threshold. Snowfall ≥ 2.5 cm, wind gust ≥ 70 km/h, precipitation ≥ 50 mm, Tmin ≤ −18 °C, or Tmax ≥ 40 °C. The thresholds are configurable and documented.
- **Uncertainty:** point-location reanalysis data is not the same as conditions at the facility. The 5-year sample is small for rare events like hurricanes, so long-term FEMA NRI data is blended in. Each answer reports a confidence level and lists its caveats.

---

## 3. Data sources (all free; only one possibly needs a key)
| Source | Use | Notes |
|---|---|---|
| **Open-Meteo Historical API** | Daily snowfall, precipitation, gusts, Tmin/Tmax per hub | No key. Main basis for the "disruption days" KPI and for questions like Denver snowfall % |
| **FEMA National Risk Index (NRI)** | County-level long-term hazard scores (hurricane, riverine/coastal flood, winter weather, heat, tornado) | Covers rare events that 5 years of history misses |
| **OpenFEMA Disaster Declarations API** | Count of weather-related disaster declarations per county | Supporting evidence for explanations |
| **NWS api.weather.gov alerts** | Active alerts per hub location | Feeds the "current risk" signal and the alert bonus |

All responses are cached in SQLite, so the demo still runs if an API is slow or down.

---

## 4. Deterministic scoring (the core KPI)
For each hub and each hazard *h*:
1. **Historical frequency score.** Average disruption days per year for *h* from Open-Meteo data, min-max normalized across hubs to 0–100.
2. **Long-term hazard score.** The FEMA NRI hazard score for the hub's county, 0–100.
3. **Hazard sub-score** = `0.6 * frequency + 0.4 * NRI`. For hurricanes the weights flip, since 5 years of data is too sparse.

**Composite Weather Disruption Risk Score** = a weighted sum of the hazard sub-scores (weights live in `config/scoring.yaml`), plus a small additive bump when NWS alerts are active. Each hub gets a tier: Low, Moderate, High or Critical.

Every score returns its full **breakdown** (inputs, weights, contributions). That breakdown is what the LLM uses to answer "why is Dallas high?", so the explanation rests on computed numbers instead of hallucination. Pure functions plus unit tests.

---

## 5. Agent design
- **LLM:** Claude (`claude-sonnet-5`, set by `ANTHROPIC_MODEL`) with tool use, via the Anthropic Python SDK.
- **Tools.** Typed with Pydantic; they call deterministic code only:
  - `list_hubs(region?)`
  - `get_hub_risk(hub)`: score plus breakdown
  - `rank_hubs(hazard?, region?, top_n?)`
  - `compare_hubs(hubs[], hazards[])`
  - `weather_stat(hub, metric, period)`: e.g. % of days with snowfall > 0 in 2025
  - `active_alerts(hub?)`
- **Structured output.** The final answer must validate against a JSON schema, enforced through a forced "final_answer" tool or structured output, then validated with Pydantic and retried once on failure:
  ```json
  { "answer": "...", "hubs": [{"name","score","rank","key_drivers"}],
    "reasoning": ["step", ...], "data_sources": [...],
    "assumptions": [...], "confidence": "low|medium|high", "follow_up_suggestions": [...] }
  ```
- **Conversation memory.** The client sends the prior turns with each request, so follow-ups like "and what about Chicago?" resolve correctly. The server stays stateless.
- **Latency budget.** Tool calls read precomputed data, so a full agent turn should finish well inside Vercel's function timeout.
- **System prompt.** Never invent numbers, always call tools for data, cite the score breakdown, and state uncertainty.

---

## 6. API (FastAPI)
| Endpoint | Purpose |
|---|---|
| `POST /chat` `{messages[]}` | Runs the agent and returns the structured answer |
| `GET /hubs`, `GET /scores?hazard=&region=` | Raw deterministic data for the UI and for debugging |
| `GET /hubs/{id}/risk` | Score breakdown for one hub |
| `POST /alerts/check`, `GET /alerts` | Recomputes scores and lists risk changes |
| `GET /health` | Health check |

## 7. Chat UI
A single static HTML/JS page (`public/index.html`, served by Vercel's CDN; FastAPI serves it in local dev). It has:
- a chat window,
- answers rendered with a hub ranking table, reasoning, assumptions and a confidence badge,
- a "show raw JSON" toggle.

**Voice bonus:** the browser Web Speech API for speech-to-text plus speechSynthesis to read answers aloud. No extra backend needed.
*(Alternative: Streamlit. Faster to build, but less control and harder to show the API boundary.)*

## 8. Alerts (bonus)
- A **Vercel Cron Job** (in `vercel.json`) calls `/api/alerts/check` daily. It can also be triggered by hand.
- It recomputes scores, refreshes NWS alerts and diffs the result against the last snapshot in SQLite.
- If the score moves by at least the threshold or the tier changes, it stores an alert and POSTs to a configurable `ALERT_WEBHOOK_URL` (Slack or webhook.site).

## 9. Data storage choice (Vercel-aware)
Vercel runs serverless functions: the filesystem is read-only apart from an ephemeral `/tmp`, and nothing runs in the background. The design follows from that:

| Data | Where | Why |
|---|---|---|
| Historical weather and FEMA data (slow-changing) | A **read-only SQLite snapshot** built by `scripts/ingest.py` and committed to the repo | Fast cold starts, no API calls on the hot path, reproducible results, and the demo survives an API outage |
| Live NWS alerts | Fetched at request time with a short in-memory cache | Needs to be current |
| Conversation history | **Stateless**: the browser sends the message history with each `/chat` call | Serverless instances share no memory. This also keeps the API simple and horizontally scalable |
| Score snapshots and alert log | **Upstash Redis** (Vercel Marketplace, free tier), falling back to local SQLite in dev | Needs durable writes across invocations |

**Rationale:** the structured analytical data lives in SQLite; the small amount of mutable state goes to a managed key-value store. No vector DB is needed, because the data is structured. The path to Postgres at larger scale is noted in the docs.

## 10. Evaluation
`evals/cases.yaml` holds about 15 cases. Run with `python -m evals.run`, which prints a report table.
- **Deterministic correctness.** The agent's numbers match direct tool calls, e.g. Denver snowfall % within ±1 pt, and the Midwest winter ranking's top 3 matches `rank_hubs`.
- **Tool selection.** The expected tools were called.
- **Schema validity.** 100% of outputs validate.
- **Behavioral checks.** An out-of-scope question (e.g. an earthquake) is declined or caveated; an unknown hub is handled gracefully; a follow-up resolves context.
- **Optional:** an LLM-as-judge rubric for explanation quality.

Unit tests (pytest) cover the scoring math and the threshold logic.

## 11. Repository structure
```
MoveoAi/
├── README.md              # run instructions, assumptions, limitations
├── PLAN.md
├── docs/ARCHITECTURE.md   # components diagram, repo structure, storage choice
├── config/scoring.yaml    # thresholds & weights
├── data/hubs.yaml
├── app/
│   ├── main.py            # FastAPI app + routes + static UI
│   ├── agent/             # llm client, tools, prompts, schemas (Pydantic)
│   ├── data_sources/      # open_meteo.py, fema_nri.py, openfema.py, nws.py
│   ├── scoring/           # deterministic risk engine
│   ├── storage/           # sqlite repo / cache
│   └── alerts/            # scheduler + webhook
├── public/index.html      # chat UI (+ voice)
├── evals/                 # cases.yaml, run.py
├── tests/
├── scripts/ingest.py      # pre-fetch & cache data
├── api/index.py           # Vercel entrypoint (exports the FastAPI app)
├── vercel.json            # routes, cron job
├── requirements.txt, .env.example
└── session/               # exported AI-agent session transcript (mandatory deliverable)
```

## 12. Execution timeline (~1 working day)
| # | Phase | Est. |
|---|---|---|
| 1 | Skeleton: repo, config, hubs list, SQLite layer | 0.5h |
| 2 | Data clients and ingestion script (Open-Meteo, NRI, OpenFEMA, NWS) with caching | 1.5h |
| 3 | Scoring engine with breakdowns, plus unit tests | 1.5h |
| 4 | Agent: tools, schema, LLM loop, session memory | 2h |
| 5 | FastAPI endpoints | 0.5h |
| 6 | Chat UI, then voice | 1h |
| 7 | Eval set and runner | 1h |
| 8 | Alerts scheduler and webhook (bonus) | 0.5h |
| 9 | Docs (README, ARCHITECTURE), deploy to Vercel (env vars, cron, Upstash) | 1h |
| 10 | Export the session transcript, final check | 0.5h |

**Order of priority if time runs short:** 1→5 plus a basic UI plus evals are must-haves. Voice, alerts and deployment are bonuses.
