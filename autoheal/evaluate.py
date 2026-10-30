"""Evaluation harness: run every scenario N times per agent and score against hidden ground truth."""
from __future__ import annotations

import csv
import json
import statistics
import time

from .config import PROJECT_ROOT, Settings
from .db import connect
from .runtime import open_ledger, run_scenario
from .scenarios import SCENARIOS, ground_truth_healthy

EVAL_DIR = PROJECT_ROOT / "data" / "eval"


def nudge_observations(res, conn) -> dict:
    """Reporting only: completion nudges (and whether one came before the first finish_incident) and focus
    reminders."""
    kinds = [r[0] for r in conn.execute("SELECT kind FROM agent_steps WHERE incident_id=? ORDER BY id",
                                        (res.incident_id,))]
    out = {"nudge_count": kinds.count("nudge"), "nudge_before_finish": "", "focus_count": kinds.count("focus"),
           "decision_count": kinds.count("decision")}
    if "nudge" in kinds:
        out["nudge_before_finish"] = "verify" in kinds and kinds.index("nudge") < kinds.index("verify")
    return out


def run_trace(res, conn, ledger) -> dict:
    """Reporting only: the run's step trace and every LLM request as the provider answered it."""
    steps = [{"step": r["step"], "kind": r["kind"], "name": r["name"], "args": r["args_json"],
              "result": (r["result_json"] or "")[:1000], "model": r["model"]}
             for r in conn.execute("SELECT step, kind, name, args_json, result_json, model FROM agent_steps "
                                   "WHERE incident_id=? ORDER BY id", (res.incident_id,))]
    requests = [dict(r) for r in ledger.conn.execute(
        "SELECT model, ok, prompt_tokens, completion_tokens, cost_usd, cost_source, latency_ms FROM llm_usage "
        "WHERE run_tag = ? ORDER BY id", (ledger.run_tag,))]
    return {"steps": steps, "requests": requests}


def score(name: str, res, conn) -> dict:
    sc = SCENARIOS[name]
    truth = ground_truth_healthy(conn)
    if res.root_causes:
        predicted = {rc.get("category") for rc in res.root_causes}
    elif res.escalation:
        predicted = set(res.escalation.get("suspected_categories") or [])
    else:
        predicted = set()
    if name == "healthy":
        success = res.status == "healthy" and res.cost_usd == 0
        expected = "healthy"
    elif sc.fixable:
        success = res.status == "resolved" and truth["healthy"]
        expected = "resolved"
    else:
        # Unfixable: only a deliberate agent hand-off that names the external dependency counts.
        # Guardrail stops are safe but not successes; infrastructure failures never are.
        success = (res.status == "escalated" and res.escalation_cause == "agent"
                   and "external_dependency" in predicted)
        expected = "escalated"
    return {
        "scenario": name, "status": res.status, "expected": expected, "success": success,
        "diagnosis_correct": sc.expected_categories <= predicted if sc.expected_categories else success,
        "predicted": sorted(c for c in predicted if c), "truth_healthy": truth["healthy"],
        "escalation_cause": res.escalation_cause or "",
        "escalation_reason": (res.escalation or {}).get("reason", ""),
        "steps": res.steps, "tool_calls": res.tool_calls, "mutations": res.mutations,
        "executed_mutations": res.executed_mutations,
        "verify_attempts": len(res.verifications), "cost_usd": res.cost_usd,
        "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens, "wall_s": res.wall_s,
        "models": [r["model"] for r in res.routing], "routing": res.routing,
        **nudge_observations(res, conn),
    }


def summarize(results: list[dict]) -> dict:
    out = {}
    for agent in sorted({r["agent"] for r in results}):
        rs = [r for r in results if r["agent"] == agent]
        wins = [r for r in rs if r["success"]]
        valid = [r for r in rs if r.get("escalation_cause") != "infrastructure"]
        cost = sum(r["cost_usd"] for r in rs)  # actual spend of ALL attempts, failed/infra runs included
        out[agent] = {
            "runs": len(rs),
            "valid_runs": len(valid),
            "valid_run_success_rate": round(sum(r["success"] for r in valid) / len(valid), 3) if valid else None,
            "end_to_end_success_rate": round(len(wins) / len(rs), 3),
            "infra_failure_count": len(rs) - len(valid),
            "guardrail_stop_count": sum(r.get("escalation_cause") == "guardrail" for r in rs),
            "diagnosis_accuracy": round(sum(r["diagnosis_correct"] for r in rs) / len(rs), 3),
            "total_cost_usd": round(cost, 4),
            # Total spend across all runs / successes: failed runs still cost money.
            "cost_per_success_usd": round(cost / len(wins), 4) if wins else None,
            "mean_steps": round(statistics.mean(r["steps"] for r in rs), 2),
            "mean_wall_s": round(statistics.mean(r["wall_s"] for r in rs), 2),
            "per_scenario": {s: f"{sum(r['success'] for r in rs if r['scenario'] == s)}/"
                                f"{sum(1 for r in rs if r['scenario'] == s)}" for s in SCENARIOS
                             if any(r["scenario"] == s for r in rs)},
        }
    return out


def run_eval(settings: Settings, agents: list[str], runs: int = 1, scenarios: list[str] | None = None,
             progress=print) -> dict:
    scenarios = scenarios or list(SCENARIOS)
    tag = time.strftime("eval-%Y%m%d-%H%M%S")
    ledger = open_ledger(settings)
    results, traces = [], {}
    for agent in agents:
        for name in scenarios:
            for i in range(runs):
                ledger.run_tag = f"{tag}:{agent}:{name}:{i}"
                conn = connect(":memory:")
                try:
                    res, conn = run_scenario(name, agent, settings, conn=conn, ledger=ledger)
                    row = {"agent": agent, "run": i, **score(name, res, conn)}
                    traces[ledger.run_tag] = run_trace(res, conn, ledger)
                except Exception as e:  # keep the harness alive; record the crash as a failure
                    row = {"agent": agent, "run": i, "scenario": name, "status": "crashed", "success": False,
                           "diagnosis_correct": False, "error": str(e)[:300], "steps": 0, "tool_calls": 0,
                           "mutations": 0, "cost_usd": round(ledger.run_total(), 6), "wall_s": 0.0,
                           "escalation_cause": "", "escalation_reason": "", "nudge_count": 0,
                           "nudge_before_finish": "", "focus_count": 0, "routing": []}
                results.append(row)
                progress(f"[{agent:10}] {name:22} run {i + 1}/{runs}: {row['status']:9} "
                         f"{'✔' if row['success'] else '✘'}  ${row['cost_usd']:.4f}  steps={row['steps']}")
    report = {"tag": tag, "models": {"cheap": settings.cheap_model, "strong": settings.strong_model},
              "summary": summarize(results), "results": results, "traces": traces}  # traces: JSON only
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / f"{tag}.json").write_text(json.dumps(report, indent=2, default=str))
    (EVAL_DIR / "latest.json").write_text(json.dumps(report, indent=2, default=str))
    with open(EVAL_DIR / f"{tag}.csv", "w", newline="", encoding="utf-8") as f:
        keys = sorted({k for r in results for k in r})
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(results)
    return report
