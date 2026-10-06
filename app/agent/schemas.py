"""The structured contract between the LLM and our code.

`LLMAnswer` is sent to Claude as a JSON schema via `output_config.format`, so the
API constrains the final message to it; we then validate it again with Pydantic.

Numbers never come from the LLM: it only names *which* hubs it is talking about
(`hub_refs`), and the API enriches those with the deterministic scores
(`HubSummary`) before returning to the client.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.hubs import load_hubs

DataSource = Literal["open_meteo_history", "fema_nri", "openfema_declarations",
                     "nws_active_alerts", "scoring_engine"]


class HubRef(BaseModel):
    hub_id: str = Field(description="Hub id exactly as returned by the tools, e.g. 'dallas'")
    role: Literal["ranked", "compared", "explained", "mentioned"]


class LLMAnswer(BaseModel):
    answer: str = Field(description="Direct answer to the user in concise prose. Cite the numbers "
                                    "returned by tools; never invent numbers.")
    hub_refs: list[HubRef] = Field(description="Hubs discussed, in the order presented "
                                               "(most exposed first when ranking)")
    reasoning: list[str] = Field(description="Short steps explaining how the answer follows from "
                                             "the tool data (which scores/drivers matter and why)")
    assumptions: list[str] = Field(description="Assumptions and scoping choices behind the answer")
    data_sources: list[DataSource]
    confidence: Literal["low", "medium", "high"]
    confidence_reason: str = Field(description="One sentence on what limits certainty")
    follow_up_suggestions: list[str] = Field(description="Up to 3 natural follow-up questions")
    in_scope: bool = Field(description="False if the question is outside what this agent models "
                                       "(e.g. earthquakes, non-hub locations, non-weather risk)")


def llm_answer_json_schema() -> dict:
    """JSON schema for output_config.format; hub ids constrained to the known set."""
    schema = LLMAnswer.model_json_schema()
    hub_ref = schema["$defs"]["HubRef"]
    hub_ref["properties"]["hub_id"]["enum"] = [h.id for h in load_hubs()]
    _close_objects(schema)
    return schema


def _close_objects(node) -> None:
    """Structured outputs require additionalProperties: false on every object."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for v in node.values():
            _close_objects(v)
    elif isinstance(node, list):
        for v in node:
            _close_objects(v)


# ------------------------------------------------------------------ API-facing models

class HubSummary(BaseModel):
    hub_id: str
    name: str
    region: str
    role: str
    composite_score: float
    tier: str
    portfolio_rank: int
    top_drivers: list[str]
    hazard_scores: dict[str, float]


class ToolCallTrace(BaseModel):
    name: str
    input: dict
    ok: bool
    duration_ms: int
    error: str | None = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


class ChatResponse(BaseModel):
    answer: LLMAnswer
    hubs: list[HubSummary]
    tool_calls: list[ToolCallTrace]
    model: str
    latency_ms: int
    usage: dict[str, int]
