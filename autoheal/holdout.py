"""Held-Out Stress & Fault-Combination Tests.

Kept separate from the 21-run benchmark: these scenarios are NOT in `scenarios.SCENARIOS`, and results go to
`data/eval/holdout-*.json`. They recombine known fault types under new conditions; they are not evidence of
generalisation to entirely unseen fault types.

Pre-registered before any agent was run on them (seeds, fault parameters and success criteria are fixed here):

* H1 - payment timeout under sustained heavy traffic (60 orders/tick). One symptom (routes to the cheap model);
  retrying the stuck payments overflows the restored queue, so a downstream cascade appears DURING recovery.
* H3 - data corruption and a hung workflow at the same time (a combination not in the benchmark).

Pre-registered for v3.1 (before any agent was run on them):

* H4 - Queue Backlog Recovery After Heavy-Traffic Fault Injection. The queue-shrink deploy (r143, max_depth 10)
  manifests for 5 ticks at a fixed load, then recovery runs at NORMAL traffic (wait_and_observe advances time with
  12 orders/tick). It measures recovery from a large dead-letter backlog, not sustained load during recovery.
  Exactly one fixed seed per load:  H4-45 = 45 orders/tick, seed 404 | H4-60 = 60, seed 505 | H4-75 = 75, seed 606.
  Expected category: queue. Same criteria C1-C5. A stress variant of a known fault type, not an unseen category.

A run succeeds only if ALL criteria hold:
  C1 resolved via finish_incident with independent verification passing (existing 2-attempt limit)
  C2 hidden ground truth fully healthy (incl. order lifecycle consistency)
  C3 diagnosis names every expected category
  C4 no high-risk approval request filed
  C5 config back at pre-incident values: gateway timeout 3000, mapping v1, queue.max_depth <= 200
     (queue.worker_batch may differ; it is reported)

Run:  python -m autoheal.holdout --agents playbook openrouter --budget 0.70      (H1 + H3, the original set)
      python -m autoheal.holdout --agents playbook openrouter --holdouts H4-45 H4-60 H4-75 --budget 0.60
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from . import agent as agent_module
from .agent import AutoHealAgent
from .config import PROJECT_ROOT, get_settings
from .db import connect, rows, scalar, wipe_system
from .detector import run_checks
from .runtime import build_llm, open_ledger
from .scenarios import _payment_timeout, _queue_capacity, _sync_corruption, _workflow_hang, ground_truth_healthy
from .simulator import Shop
from .tools import ToolContext, execute

HOLDOUT_SEEDS = (101, 202, 303)
WARMUP_TICKS, MANIFEST_TICKS = 4, 5
OUT_DIR = PROJECT_ROOT / "data" / "eval"
# Conservative per-run cost estimates for the pre-run budget gate (USD); updated with observed maxima.
PRIOR_COST = {"H1": 0.10, "H3": 0.18, "H4-45": 0.15, "H4-60": 0.15, "H4-75": 0.15}
MARGIN = 0.04


@dataclass(frozen=True)
class Holdout:
    name: str
    title: str
    inject: Callable[[Shop], None]
    manifest_orders: int | None  # traffic per tick while the fault manifests (None = normal)
    expected_categories: frozenset[str]
    expected_symptoms: frozenset[str]
    seeds: tuple[int, ...] = HOLDOUT_SEEDS  # pre-registered seeds; one run per seed


def _sync_and_hang(shop: Shop) -> None:
    _sync_corruption(shop)
    _workflow_hang(shop)


HOLDOUTS = {h.name: h for h in [
    Holdout("H1", "Payment timeout under sustained heavy traffic", _payment_timeout, 60,
            frozenset({"payment"}), frozenset({"orders_without_payment"})),
    Holdout("H3", "Simultaneous data corruption and hung workflow", _sync_and_hang, None,
            frozenset({"data_sync", "workflow"}), frozenset({"data_reconciliation", "workflow_stall"})),
] + [
    Holdout(f"H4-{load}", f"Queue backlog recovery after heavy-traffic fault injection ({load} orders/tick x 5 ticks)",
            _queue_capacity, load, frozenset({"queue"}), frozenset({"dead_letter_backlog"}), seeds=(seed,))
    for load, seed in ((45, 404), (60, 505), (75, 606))
]}
DEFAULT_HOLDOUTS = ("H1", "H3")  # the original pre-registered set; H4 cases must be selected explicitly


def build(name: str, seed: int, conn: sqlite3.Connection) -> Shop:
    """Fresh DB -> healthy warmup -> inject -> let symptoms manifest (same recipe as the benchmark)."""
    h = HOLDOUTS[name]
    wipe_system(conn)
    shop = Shop(conn, seed=seed)
    shop.bootstrap()
    shop.run(WARMUP_TICKS)
    h.inject(shop)
    shop.run(MANIFEST_TICKS, n_orders=h.manifest_orders)
    return shop


def known_good_fix(name: str, ctx: ToolContext) -> list[dict]:
    """A recovery sequence using only existing tools; proves each seed is solvable (feasibility check only)."""
    if name.startswith("H4"):
        plan = [("rollback_config", {"key": "queue.max_depth"}), ("set_config", {"key": "queue.worker_batch", "value": "100"}),
                ("replay_dead_letter", {}), ("wait_and_observe", {"ticks": 2}), ("replay_dead_letter", {})]
    elif name == "H1":
        plan = [("rollback_config", {"key": "payment.gateway_timeout_ms"}), ("retry_stuck_payments", {}),
                ("set_config", {"key": "queue.worker_batch", "value": "100"}), ("wait_and_observe", {"ticks": 2}),
                ("replay_dead_letter", {})]
    else:
        wf = scalar(ctx.conn, "SELECT id FROM workflows WHERE hung=1 AND status='running'")
        plan = [("rollback_config", {"key": "sync.mapping_version"}), ("resync_customers", {}),
                ("terminate_workflow", {"workflow_id": wf, "reason": "stale heartbeat"}),
                ("wait_and_observe", {"ticks": 1})]
    return [execute(ctx, n, a) for n, a in plan]


def criteria(name: str, res, conn: sqlite3.Connection) -> dict:
    h = HOLDOUTS[name]
    predicted = {rc.get("category") for rc in res.root_causes} if res.root_causes else set(
        (res.escalation or {}).get("suspected_categories") or [])
    cfg = {r["key"]: r["value"] for r in rows(conn, "SELECT key, value FROM config")}
    c = {
        "C1_resolved_and_verified": res.status == "resolved" and bool(res.verifications) and res.verifications[-1].passed,
        "C2_ground_truth_healthy": ground_truth_healthy(conn)["healthy"],
        "C3_diagnosis_correct": h.expected_categories <= predicted,
        "C4_no_high_risk_request": scalar(conn, "SELECT COUNT(*) FROM approvals WHERE incident_id=?",
                                          (res.incident_id,)) == 0,
        "C5_config_restored": cfg.get("payment.gateway_timeout_ms") == "3000" and cfg.get("sync.mapping_version") == "v1"
                              and int(cfg.get("queue.max_depth", 0)) <= 200,
    }
    return {"success": all(c.values()), "criteria": c, "predicted": sorted(p for p in predicted if p),
            "final_config": {k: cfg.get(k) for k in ("payment.gateway_timeout_ms", "queue.max_depth",
                                                     "queue.worker_batch", "sync.mapping_version")}}


def _trace(conn: sqlite3.Connection, incident_id: int) -> list[dict]:
    return [{"step": r["step"], "kind": r["kind"], "name": r["name"], "args": r["args_json"],
             "result": (r["result_json"] or "")[:1000]}
            for r in rows(conn, "SELECT step, kind, name, args_json, result_json FROM agent_steps "
                                "WHERE incident_id=? ORDER BY id", (incident_id,))]


def _field(result: str, key: str) -> int:
    try:
        return int(json.loads(result).get(key, 0))
    except (ValueError, AttributeError):
        return 0


Q_PENDING = "SELECT COUNT(*) FROM queue WHERE status='pending'"
DL_OPEN = "SELECT COUNT(*) FROM dead_letter WHERE replayed=0"
UNPAID = "SELECT COUNT(*) FROM orders WHERE status='created'"


def _backlog(conn: sqlite3.Connection, shop: Shop) -> dict:
    return {"tick": shop.tick_now, "queue_pending": scalar(conn, Q_PENDING), "dead_letters": scalar(conn, DL_OPEN),
            "unpaid_orders": scalar(conn, UNPAID)}


@contextmanager
def _observe_verification(records: list[dict]):
    """Reporting only: wrap the agent's verifier to snapshot the backlog before/after each finish_incident.
    The real verifier still runs unchanged; this shows what (if anything) it drained by itself."""
    real = agent_module.verify

    def observed(conn, shop, *args, **kwargs):
        before = _backlog(conn, shop)
        v = real(conn, shop, *args, **kwargs)
        after = _backlog(conn, shop)
        records.append({"before_finish": before, "after_verification": after, "verifier_ticks": v.ticks_run,
                        "probe_fulfilled": v.probe_fulfilled, "probe_orders": v.probe_orders, "passed": v.passed,
                        "verifier_drained_queue": before["queue_pending"] > 0,
                        "dead_letters_left_for_verifier": before["dead_letters"]})
        return v

    agent_module.verify = observed
    try:
        yield
    finally:
        agent_module.verify = real


def _queue_metrics(conn: sqlite3.Connection, start: dict, trace: list[dict]) -> dict:
    """H4 metrics from the step trace and the database (reporting only)."""
    acts = [t for t in trace if t["kind"] == "action"]
    replays = [t for t in acts if t["name"] == "replay_dead_letter"]
    waits = [t for t in trace if t["kind"] == "observe" and t["name"] == "wait_and_observe"]
    return {"initial_dead_letters": start["dead_letters"],
            "dead_letters_created_during_recovery": scalar(conn, "SELECT COUNT(*) FROM dead_letter WHERE id > ?",
                                                           (start["dl_max_id"],)),
            "replayed": sum(_field(t["result"], "replayed") for t in replays),
            "replay_rejections": sum(_field(t["result"], "rejected_queue_full") for t in replays),
            "outstanding_dead_letters_at_end": scalar(conn, DL_OPEN),
            "agent_wait_ticks": sum(int(json.loads(t["args"] or "{}").get("ticks", 3)) for t in waits),
            "blocked_actions": sum(t["kind"] == "blocked" for t in trace)}


def _observations(name: str, trace: list[dict]) -> dict:
    acts = [t for t in trace if t["kind"] == "action"]
    if name.startswith("H4"):
        return {}
    if name == "H1":
        dl = [_field(t["result"], "dead_lettered") for t in acts if t["name"] == "retry_stuck_payments"]
        rejected = [_field(t["result"], "rejected_queue_full") for t in acts if t["name"] == "replay_dead_letter"]
        return {"cascade_dead_lettered_by_retry": sum(dl), "replay_rejections": sum(rejected)}
    names = {t["name"] for t in acts}
    return {"sync_fix_applied": bool(names & {"rollback_config", "set_config", "resync_customers"}),
            "workflow_fix_applied": "terminate_workflow" in names}


HOLDOUT_LABEL = "Held-Out Stress & Fault-Combination Tests"
HOLDOUT_NOTE = ("Known fault types recombined under new conditions; not evidence of generalisation to "
                "entirely unseen fault types. Separate from the 21-run benchmark.")


def _requests(ledger) -> list[dict]:
    """Every LLM request of the current run as the provider answered it (failed 429/5xx attempts included)."""
    return [dict(r) for r in ledger.conn.execute(
        "SELECT model, ok, prompt_tokens, completion_tokens, cost_usd, cost_source, latency_ms FROM llm_usage "
        "WHERE run_tag = ? ORDER BY id", (ledger.run_tag,))]


EVIDENCE_DEV = "development/regression (v3.2; designed or tuned after these cases were observed)"


def run(agents: list[str], budget_usd: float, progress=print, holdouts: list[str] | None = None,
        seeds: list[int] | None = None, prefix: str = "holdout", label: str = HOLDOUT_LABEL,
        note: str = HOLDOUT_NOTE) -> dict:
    base = get_settings()
    ledger = open_ledger(base)
    start_total = ledger.total()
    # Hard ceiling: the existing global cap, tightened for this process only. The cap is checked before each
    # call, so one call can overshoot it; MARGIN covers a worst-case strong-model call (~$0.035).
    settings = get_settings(max_total_usd=min(base.max_total_usd, start_total + budget_usd - MARGIN))
    tag = time.strftime(f"{prefix}-%Y%m%d-%H%M%S")
    results, stopped, observed_max = [], "", dict(PRIOR_COST)
    selected = list(holdouts or DEFAULT_HOLDOUTS)
    for agent in agents:
        for name in selected:
            for seed in list(seeds or HOLDOUTS[name].seeds):
                spent = ledger.total() - start_total
                if agent != "playbook":
                    if stopped:
                        continue
                    if spent + observed_max[name] > budget_usd - MARGIN:
                        stopped = f"budget gate: spent ${spent:.4f} + next-run estimate ${observed_max[name]:.2f} " \
                                  f"would pass ${budget_usd:.2f}"
                        progress(f"STOP before {agent} {name} seed {seed}: {stopped}")
                        continue
                ledger.run_tag = f"{tag}:{agent}:{name}:{seed}"
                conn = connect(":memory:")
                shop = build(name, seed, conn)
                symptoms = sorted(f.check for f in run_checks(conn))
                start = {"dead_letters": scalar(conn, DL_OPEN),
                         "dl_max_id": scalar(conn, "SELECT COALESCE(MAX(id), 0) FROM dead_letter")}
                verification_detail: list[dict] = []
                with _observe_verification(verification_detail):
                    res = AutoHealAgent(conn, shop, build_llm(agent, conn, settings, ledger), settings, ledger).run()
                trace = _trace(conn, res.incident_id)
                row = {"agent": agent, "holdout": name, "seed": seed, "symptoms_at_start": symptoms,
                       "status": res.status, **criteria(name, res, conn),
                       "verifications": [{"passed": v.passed, "probe_fulfilled": v.probe_fulfilled,
                                          "probe_orders": v.probe_orders} for v in res.verifications],
                       "escalation_cause": res.escalation_cause or "",
                       "escalation_reason": (res.escalation or {}).get("reason", ""),
                       "steps": res.steps, "tool_calls": res.tool_calls, "mutations": res.mutations,
                       "nudges": sum(t["kind"] == "nudge" for t in trace),
                       "cost_usd": round(ledger.run_total(), 6), "wall_s": res.wall_s,
                       "models": [r["model"] for r in res.routing], "routing": res.routing,
                       "focus_reminders": sum(t["kind"] == "focus" for t in trace),
                       "decision_points": sum(t["kind"] == "decision" for t in trace),
                       "executed_mutations": res.executed_mutations,
                       "unexecuted_mutation_attempts": res.unexecuted_mutation_attempts,
                       # playbook routing/upgrades are bookkeeping only: the playbook never calls an LLM
                       "llm_called": agent != "playbook" and bool(_requests(ledger)),
                       "model_upgrades": 0 if agent == "playbook" else len(res.routing) - 1,
                       "load_orders_per_tick": HOLDOUTS[name].manifest_orders,
                       "verification_detail": verification_detail, "requests": _requests(ledger),
                       **(_queue_metrics(conn, start, trace) if name.startswith("H4") else {}),
                       **_observations(name, trace), "trace": trace}
                results.append(row)
                if agent != "playbook":
                    observed_max[name] = max(observed_max[name], row["cost_usd"] * 1.5)
                progress(f"[{agent:10}] {name} seed {seed}: {res.status:9} {'PASS' if row['success'] else 'FAIL'} "
                         f"${row['cost_usd']:.4f} steps={res.steps} cumulative=${ledger.total() - start_total:.4f}")
                if row["escalation_cause"] == "infrastructure" and not stopped:
                    stopped = f"infrastructure failure ({row['escalation_reason'][:120]}); no further paid runs"
                    progress(f"STOP: {stopped}")
    report = {"tag": tag, "label": label, "note": note, "evidence_type": EVIDENCE_DEV, "holdouts": selected,
              "seeds": {n: list(seeds or HOLDOUTS[n].seeds) for n in selected}, "budget_usd": budget_usd,
              "additional_spend_usd": round(ledger.total() - start_total, 6), "stopped": stopped,
              "models": {"cheap": settings.cheap_model, "strong": settings.strong_model}, "results": results}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{tag}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> None:
    p = argparse.ArgumentParser(prog="autoheal.holdout")
    p.add_argument("--agents", nargs="+", default=["playbook"], choices=["playbook", "openrouter"])
    p.add_argument("--budget", type=float, default=0.70, help="max additional USD for this evaluation")
    p.add_argument("--holdouts", nargs="+", choices=list(HOLDOUTS), help="subset of scenarios (default: all)")
    p.add_argument("--seeds", nargs="+", type=int, help="subset of seeds (default: the pre-registered seeds)")
    p.add_argument("--prefix", default="holdout", help="result file prefix, e.g. phase1 for development runs")
    p.add_argument("--label", default=HOLDOUT_LABEL)
    p.add_argument("--note", default=HOLDOUT_NOTE)
    a = p.parse_args()
    rep = run(a.agents, a.budget, holdouts=a.holdouts, seeds=a.seeds, prefix=a.prefix, label=a.label, note=a.note)
    print(f"\nAdditional spend: ${rep['additional_spend_usd']:.4f} of ${a.budget:.2f}"
          + (f"  (stopped: {rep['stopped']})" if rep["stopped"] else ""))
    print(f"Saved to data/eval/{rep['tag']}.json")


if __name__ == "__main__":
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()
