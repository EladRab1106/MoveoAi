"""Run the agent evaluation set.

    python -m evals.run --dev                 # fast dev subset (use while iterating on prompts)
    python -m evals.run --tags assignment     # only the 4 assignment questions
    python -m evals.run --case dallas_why_high --case denver_snow_last_year
    python -m evals.run                       # full set (run once the prompt is stable)
    python -m evals.run --judge               # + LLM-judge fact/quality grading (Claude Sonnet 5)
    python -m evals.run --verbose             # print every answer

Deterministic checks are resolved against the scoring engine at run time. Results are
written to evals/results/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import yaml

from app.agent.agent import AgentError, client, run_agent
from app.agent.schemas import ChatMessage, ChatResponse
from app.config import ANTHROPIC_MODEL
from app.hubs import find_hub
from app.scoring import engine

EVAL_DIR = Path(__file__).parent
JUDGE_MODEL = "claude-sonnet-5"
PRICES = {  # USD per 1M tokens (input, output)
    "claude-opus-5": (5.0, 25.0), "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
}

# ------------------------------------------------------------------ engine-backed expectations


def _scores(hazard: str | None, region: str | None):
    return engine.get_scores(region=None if region in (None, "all") else region,
                             hazard=None if hazard in (None, "composite") else hazard)


def resolve_hub(spec) -> str:
    if isinstance(spec, dict) and "top_of" in spec:
        t = spec["top_of"]
        return _scores(t.get("hazard"), t.get("region"))[0].hub_id
    return find_hub(spec).id


def _hazard(hub_id: str, hazard: str):
    return next(h for h in engine.get_hub_risk(hub_id).hazards if h.hazard == hazard)


def render_fact(template: str) -> str:
    def sub(m: re.Match) -> str:
        kind, *args = m.group(1).split(":")
        if kind == "leader":
            hazard, hubs = args[0], args[1].split(",")
            best = max(hubs, key=lambda h: _hazard(h, hazard).score)
            return engine.get_hub_risk(best).name
        if kind == "score":
            return str(_hazard(args[0], args[1]).score)
        if kind == "freqtie":
            f = _hazard(args[0], args[1]).frequency
            ties = ", ".join(engine.get_hub_risk(t).name for t in f.tied_with)
            return (f"rank {f.portfolio_rank} ({f.days_per_year} days/yr)"
                    + (f", tied with {ties}" if ties else ""))
        risk = engine.get_hub_risk(args[0])
        return {"composite": str(risk.composite_score), "tier": risk.tier,
                "rank": str(risk.rank),
                "drivers": " and ".join(d.replace("_", " ") for d in risk.top_drivers)}[kind]
    return re.sub(r"\{([^}]+)\}", sub, template)


# ------------------------------------------------------------------ number grounding

NUM_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
ABS_TOL, REL_TOL = 0.6, 0.01


def extract_numbers(text: str) -> list[float]:
    text = DATE_RE.sub(" ", text)
    out = []
    for tok in NUM_RE.findall(text):
        try:
            out.append(abs(float(tok.replace(",", ""))))
        except ValueError:
            pass
    return out


def _ignorable(x: float, user_numbers: set[float]) -> bool:
    if x == int(x) and (x <= 10 or 1990 <= x <= 2035):   # counts ("top 3") and years
        return True
    return any(abs(x - u) < 1e-9 for u in user_numbers)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOL, REL_TOL * abs(b))


UNIT_SUFFIX_RE = re.compile(r"\s*(°\s*F|F\b|-?inch|in\b|\"|mph)", re.IGNORECASE)


def grounding(answer_text: str, tool_outputs: list[str], user_text: str) -> list[float]:
    """Numbers in the answer that can't be traced to tool outputs (within tolerance).

    Accepted derivations: rounding, x100 / /100 (fraction <-> percent), the difference or
    ratio of two tool numbers, and unit conversions (C->F, cm/mm->in, km/h->mph) but only
    when the answer number is followed by that converted unit.
    """
    tool_nums = sorted({abs(n) for o in tool_outputs for n in extract_numbers(o)})
    base = sorted({v for t in tool_nums for v in (t, t * 100, t / 100)})
    converted = sorted({v for t in tool_nums
                        for v in (t * 9 / 5 + 32, t / 2.54, t / 25.4, t * 0.6214)})
    user_numbers = set(extract_numbers(user_text))
    text = DATE_RE.sub(" ", answer_text)
    ungrounded = []
    for m in NUM_RE.finditer(text):
        try:
            x = abs(float(m.group().replace(",", "")))
        except ValueError:
            continue
        if _ignorable(x, user_numbers) or any(_close(x, v) for v in base):
            continue
        if UNIT_SUFFIX_RE.match(text, m.end()) and any(_close(x, v) for v in converted):
            continue
        derived = any(_close(x, abs(a - b)) or (b and _close(x, a / b))
                      for a in tool_nums for b in tool_nums if a != b)
        if not derived:
            ungrounded.append(x)
    return ungrounded


SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


STAT_SUFFIX_RE = re.compile(r"\s*(%|percent\b|days?\b|of\b)", re.IGNORECASE)


def numbers_attributed_to(text: str, period: str) -> list[str]:
    """Statistic-like numbers (followed by %, 'percent', 'days' or 'of') stated in the same
    sentence as `period` (e.g. '2012'). Durations such as '13 years earlier', dates and
    labeled figures for other periods (in other sentences) are allowed."""
    found = []
    for sent in SENTENCE_RE.split(text):
        if period not in sent:
            continue
        sent = DATE_RE.sub(" ", sent)
        for m in NUM_RE.finditer(sent):
            if STAT_SUFFIX_RE.match(sent, m.end()) and m.group() != period:
                found.append(m.group())
    return found


# ------------------------------------------------------------------ checks


def _norm_arg(key: str, value):
    if key == "hub" and isinstance(value, str):
        try:
            return find_hub(value).id
        except LookupError:
            return value
    if isinstance(value, str):
        return value.lower()
    return value


def _call_matches(call, names: list[str], args: dict) -> bool:
    if call.name not in names:
        return False
    inp = call.input
    for k, v in args.items():
        if k == "hub" and "hubs" in inp:  # compare_hubs takes a list
            if _norm_arg("hub", v) not in {_norm_arg("hub", h) for h in inp["hubs"]}:
                return False
        elif _norm_arg(k, inp.get(k)) != _norm_arg(k, v):
            return False
    return True


def run_checks(checks: dict, resp: ChatResponse, all_tool_outputs: list[str],
               user_text: str) -> dict[str, tuple[bool, str]]:
    a = resp.answer
    text = a.answer
    low = text.lower()
    refs = [r.hub_id for r in a.hub_refs]
    res: dict[str, tuple[bool, str]] = {"schema": (True, "validated")}

    bad = grounding(text + "\n" + "\n".join(a.reasoning), all_tool_outputs, user_text)
    res["grounded"] = (not bad, "all numbers traceable" if not bad else f"untraced: {bad}")

    for name, spec in checks.items():
        if name == "tools":
            missing = []
            for names, args in spec:
                if not any(_call_matches(c, names.split("|"), args or {}) for c in resp.tool_calls):
                    missing.append(f"{names}{args or ''}")
            called = [f"{c.name}({c.input})" for c in resp.tool_calls]
            res[name] = (not missing, f"missing {missing}; called {called}" if missing else "ok")
        elif name == "tools_any":
            ok = any(c.name in spec for c in resp.tool_calls)
            res[name] = (ok, "ok" if ok else f"none of {spec} called")
        elif name == "top_k":
            exp = [r.hub_id for r in _scores(spec["hazard"], spec["region"])[:spec["k"]]]
            got = refs[:spec["k"]]
            ok = got == exp if spec.get("ordered", True) else set(got) == set(exp)
            res[name] = (ok, f"expected {exp}, got {got}")
        elif name == "hub_refs_first":
            exp = resolve_hub(spec)
            res[name] = (bool(refs) and refs[0] == exp, f"expected {exp} first, got {refs[:3]}")
        elif name == "hub_refs_include":
            missing = [h for h in spec if h not in refs]
            res[name] = (not missing, f"missing {missing}" if missing else "ok")
        elif name == "stat_number":
            stat = engine.weather_stat(
                spec["hub"], spec["metric"], year=spec.get("year"),
                start=date.fromisoformat(str(spec["start"])) if "start" in spec else None,
                end=date.fromisoformat(str(spec["end"])) if "end" in spec else None)
            ok = any(abs(x - stat.percent) <= 0.15 for x in extract_numbers(text))
            res[name] = (ok, f"expected {stat.percent}% ({stat.days_matching}/{stat.days_observed})")
        elif name == "hazard_days_number":
            hz = _hazard(resolve_hub(spec["hub"]), spec["hazard"])
            exp = hz.frequency.days_per_year
            ok = any(_close(x, exp) for x in extract_numbers(text))
            res[name] = (ok, f"expected ~{exp} days/yr")
        elif name == "tier_mentioned":
            tier = engine.get_hub_risk(spec).tier
            res[name] = (tier.lower() in low, f"expected tier '{tier}'")
        elif name == "mentions":
            missing = [g for g in spec if not any(str(k).lower() in low for k in g)]
            res[name] = (not missing, f"missing any of {missing}" if missing else "ok")
        elif name == "no_number_for_period":
            bad = numbers_attributed_to(text, str(spec))
            res[name] = (not bad, f"numbers attributed to {spec}: {bad}" if bad else "ok")
        elif name == "no_percent":
            found = re.findall(r"\d+(?:\.\d+)?\s*%", text)
            res[name] = (not found, f"found {found}" if found else "ok")
        elif name == "in_scope":
            res[name] = (a.in_scope == spec, f"expected {spec}, got {a.in_scope}")
        elif name == "facts":
            pass  # judged separately
        else:
            res[name] = (False, f"unknown check {name}")
    return res


# ------------------------------------------------------------------ LLM judge

JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["facts", "explanation_quality", "uncertainty_communication", "comment"],
    "properties": {
        "facts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["fact", "supported"],
            "properties": {"fact": {"type": "string"}, "supported": {"type": "boolean"}}}},
        "explanation_quality": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "uncertainty_communication": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "comment": {"type": "string"},
    },
}


def judge(question: str, resp: ChatResponse, facts: list[str]) -> tuple[bool, str, dict]:
    prompt = (
        "You are grading an AI analyst's answer about logistics-hub weather risk.\n\n"
        f"Question: {question}\n\nAnswer JSON:\n{resp.answer.model_dump_json(indent=1)}\n\n"
        "Ground-truth facts from the deterministic scoring engine:\n"
        + "\n".join(f"- {f}" for f in facts) +
        "\n\nFor each fact, is it consistent with (supported by, or at least not contradicted "
        "by and substantively reflected in) the answer? Then rate 1-5: explanation_quality "
        "(clear, uses the actual drivers/numbers, concise), uncertainty_communication "
        "(states relevant assumptions/limits without burying the answer).")
    # Sonnet 5 thinks adaptively by default, so leave room for thinking + the JSON.
    r = client().messages.create(
        model=JUDGE_MODEL, max_tokens=16000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}])
    usage = {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}
    if r.stop_reason != "end_turn":
        return False, f"judge error: stop_reason={r.stop_reason}", usage
    text = next((b.text for b in r.content if b.type == "text"), "")
    try:
        g = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"judge error: invalid JSON ({exc})", usage
    ok = all(f["supported"] for f in g["facts"]) and min(
        g["explanation_quality"], g["uncertainty_communication"]) >= 3
    return ok, f"facts {[f['supported'] for f in g['facts']]}, quality " \
               f"{g['explanation_quality']}/{g['uncertainty_communication']}: {g['comment']}", usage


# ------------------------------------------------------------------ runner


def run_case(case: dict, model: str, use_judge: bool, verbose: bool) -> dict:
    history: list[ChatMessage] = []
    tool_outputs: list[str] = []
    turns_out, usage, judge_usage = [], {"input_tokens": 0, "output_tokens": 0}, \
        {"input_tokens": 0, "output_tokens": 0}
    passed = True
    for i, turn in enumerate(case["turns"]):
        history.append(ChatMessage(role="user", content=turn["user"]))
        t0 = time.perf_counter()
        try:
            resp = run_agent(history, model=model)
        except AgentError as exc:
            turns_out.append({"user": turn["user"], "error": str(exc),
                              "checks": {"agent": [False, str(exc)]}})
            passed = False
            break
        latency = time.perf_counter() - t0
        for k in usage:
            usage[k] += resp.usage.get(k, 0)
        tool_outputs.extend(c.output or "" for c in resp.tool_calls)
        user_text = " ".join(m.content for m in history if m.role == "user")
        checks = run_checks(turn.get("checks", {}), resp, tool_outputs, user_text)
        facts = turn.get("checks", {}).get("facts")
        if facts and use_judge:
            ok, detail, ju = judge(turn["user"], resp, [render_fact(f) for f in facts])
            checks["judge"] = (ok, detail)
            for k in judge_usage:
                judge_usage[k] += ju[k]
        passed &= all(ok for ok, _ in checks.values())
        turns_out.append({
            "user": turn["user"], "answer": resp.answer.model_dump(),
            "tool_calls": [c.model_dump() for c in resp.tool_calls],
            "latency_s": round(latency, 1),
            "checks": {k: [ok, d] for k, (ok, d) in checks.items()},
        })
        if verbose:
            print(f"\n--- {case['id']} turn {i + 1}: {turn['user']}\n{resp.answer.answer}\n"
                  f"tools: {[(c.name, c.input) for c in resp.tool_calls]}", flush=True)
        history.append(ChatMessage(role="assistant", content=resp.assistant_message))
    return {"id": case["id"], "tags": case.get("tags", []), "passed": passed, "turns": turns_out,
            "usage": usage, "judge_usage": judge_usage}


def safe_run_case(case: dict, model: str, use_judge: bool, verbose: bool) -> dict:
    """One broken case (or judge call) must never abort the whole run."""
    try:
        return run_case(case, model, use_judge, verbose)
    except Exception as exc:
        return {"id": case["id"], "tags": case.get("tags", []), "passed": False,
                "turns": [{"user": case["turns"][0]["user"],
                           "checks": {"runner": [False, f"{exc.__class__.__name__}: {exc}"]}}],
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "judge_usage": {"input_tokens": 0, "output_tokens": 0}}


def cost(usage: dict, model: str) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return (usage["input_tokens"] * pin + usage["output_tokens"] * pout) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dev", action="store_true", help="only cases marked dev: true")
    ap.add_argument("--tags", nargs="*", help="only cases with any of these tags")
    ap.add_argument("--case", action="append", help="run specific case id(s)")
    ap.add_argument("--judge", action="store_true", help="LLM-judge fact/quality checks")
    ap.add_argument("--model", default=ANTHROPIC_MODEL)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    cases = yaml.safe_load((EVAL_DIR / "cases.yaml").read_text())["cases"]
    if args.dev:
        cases = [c for c in cases if c.get("dev")]
    if args.tags:
        cases = [c for c in cases if set(c.get("tags", [])) & set(args.tags)]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if not cases:
        print("No cases selected")
        return 1

    print(f"Running {len(cases)} case(s) on {args.model}"
          f"{' + judge ' + JUDGE_MODEL if args.judge else ''} ...", flush=True)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda c: safe_run_case(c, args.model, args.judge, args.verbose),
                                cases))
    wall = time.perf_counter() - t0

    # ---- report
    print(f"\n{'case':34} {'result':6}  failed checks")
    check_stats: dict[str, list[bool]] = {}
    latencies = []
    for r in results:
        failed = []
        for ti, t in enumerate(r["turns"]):
            if "latency_s" in t:
                latencies.append(t["latency_s"])
            for name, (ok, detail) in t["checks"].items():
                check_stats.setdefault(name, []).append(ok)
                if not ok:
                    failed.append(f"t{ti + 1}.{name}: {detail}")
        print(f"{r['id']:34} {'PASS' if r['passed'] else 'FAIL':6}  "
              + ("" if not failed else "\n" + "\n".join(f"{'':42}{f}" for f in failed)))
    n_pass = sum(r["passed"] for r in results)
    print(f"\nCases passed: {n_pass}/{len(results)}")
    print("Checks: " + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in sorted(check_stats.items())))
    usage = {k: sum(r["usage"][k] for r in results) for k in ("input_tokens", "output_tokens")}
    jusage = {k: sum(r["judge_usage"][k] for r in results) for k in ("input_tokens", "output_tokens")}
    total_cost = cost(usage, args.model) + cost(jusage, JUDGE_MODEL)
    if latencies:
        p95 = sorted(latencies)[max(0, int(round(0.95 * len(latencies))) - 1)]
        print(f"Latency per turn: p50 {statistics.median(latencies):.1f}s, p95 {p95:.1f}s; "
              f"wall {wall:.0f}s")
    print(f"Tokens: {usage['input_tokens']:,} in / {usage['output_tokens']:,} out; "
          f"est. cost ${total_cost:.2f}")

    out_dir = EVAL_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({
        "model": args.model, "judge": args.judge, "run_at": datetime.now().isoformat(),
        "snapshot": engine.snapshot_meta(), "passed": n_pass, "total": len(results),
        "usage": usage, "judge_usage": jusage, "est_cost_usd": round(total_cost, 3),
        "results": results}, indent=1, default=str))
    print(f"Results: {out.relative_to(EVAL_DIR.parent)}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
