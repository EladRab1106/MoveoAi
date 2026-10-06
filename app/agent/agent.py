"""Agent loop: Claude + deterministic tools + schema-enforced final answer."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import anthropic
from pydantic import ValidationError

from app.agent.prompts import system_prompt
from app.agent.schemas import (ChatMessage, ChatResponse, HubSummary, LLMAnswer, ToolCallTrace,
                               llm_answer_json_schema)
from app.agent.tools import TOOL_DEFS, run_tool
from app.config import ANTHROPIC_EFFORT, ANTHROPIC_MODEL, ANTHROPIC_WORKSPACE_ID
from app.scoring import engine

MAX_TURNS = 8          # model round-trips per user message
MAX_TOKENS = 16000


class AgentError(RuntimeError):
    pass


_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        headers = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else None
        _client = anthropic.Anthropic(timeout=120.0, max_retries=2, default_headers=headers)
    return _client


def _execute(block) -> tuple[dict, ToolCallTrace]:
    t0 = time.perf_counter()
    try:
        content, ok, err = run_tool(block.name, dict(block.input)), True, None
    except Exception as exc:  # report to the model so it can correct itself
        content, ok, err = f"Error: {exc}", False, str(exc)
    trace = ToolCallTrace(name=block.name, input=dict(block.input), ok=ok, error=err, output=content,
                          duration_ms=int((time.perf_counter() - t0) * 1000))
    result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
    if not ok:
        result["is_error"] = True
    return result, trace


def _enrich(answer: LLMAnswer) -> list[HubSummary]:
    """Attach deterministic scores to the hubs the model referenced."""
    by_id = {r.hub_id: r for r in engine.get_scores()}
    out, seen = [], set()
    for ref in answer.hub_refs:
        r = by_id.get(ref.hub_id)
        if r is None or ref.hub_id in seen:
            continue
        seen.add(ref.hub_id)
        out.append(HubSummary(hub_id=r.hub_id, name=f"{r.name}, {r.state}", region=r.region,
                              role=ref.role, composite_score=r.composite_score, tier=r.tier,
                              portfolio_rank=r.rank or 0, top_drivers=r.top_drivers,
                              hazard_scores={h.hazard: h.score for h in r.hazards}))
    return out


def run_agent(history: list[ChatMessage], model: str | None = None) -> ChatResponse:
    if history[-1].role != "user":
        raise AgentError("The last message must be from the user")
    t_start = time.perf_counter()
    messages: list[dict] = [{"role": m.role, "content": m.content} for m in history]
    traces: list[ToolCallTrace] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    schema_retry_used = False
    request = dict(
        model=model or ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt(),
        tools=TOOL_DEFS,
        thinking={"type": "adaptive"},
        output_config={"effort": ANTHROPIC_EFFORT,
                       "format": {"type": "json_schema", "schema": llm_answer_json_schema()}},
        # On a safety-classifier decline, re-run server-side on Anthropic's recommended model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    with ThreadPoolExecutor(max_workers=6) as pool:
        for _ in range(MAX_TURNS):
            try:
                response = client().beta.messages.create(messages=messages, **request)
            except anthropic.AuthenticationError as exc:
                raise AgentError("Anthropic API key is missing or invalid") from exc
            except anthropic.RateLimitError as exc:
                raise AgentError("Rate limited by the Anthropic API, try again shortly") from exc
            except anthropic.APIStatusError as exc:
                raise AgentError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise AgentError("Could not reach the Anthropic API") from exc

            usage["input_tokens"] += response.usage.input_tokens
            usage["output_tokens"] += response.usage.output_tokens

            if response.stop_reason == "refusal":
                raise AgentError("The model declined to answer this request")
            if response.stop_reason == "max_tokens":
                raise AgentError("The model ran out of output tokens")

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                calls = [b for b in response.content if b.type == "tool_use"]
                results = list(pool.map(_execute, calls))
                traces.extend(t for _, t in results)
                messages.append({"role": "user", "content": [r for r, _ in results]})
                continue

            # end_turn: the text block is constrained to our schema; validate it anyway.
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                answer = LLMAnswer.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                if schema_retry_used:
                    raise AgentError(f"Model output failed schema validation: {exc}") from exc
                schema_retry_used = True
                messages.append({"role": "user", "content":
                                 f"Your last message did not match the JSON schema ({exc}). "
                                 "Reply again with only the corrected JSON."})
                continue

            return ChatResponse(
                answer=answer, assistant_message=answer.model_dump_json(), hubs=_enrich(answer), tool_calls=traces, model=response.model,
                latency_ms=int((time.perf_counter() - t_start) * 1000), usage=usage)

    raise AgentError(f"No final answer after {MAX_TURNS} model turns")
