"""Builds evidence-site/data/evidence.js, the only data the Evaluation & Evidence website reads.

    python -m autoheal_ui.evidence_export           write the file
    python -m autoheal_ui.evidence_export --check   fail if the file does not match the committed results

Everything is derived from the two committed result files, which are only read. Each record is embedded unchanged under
`raw`, next to its presentation fields, so every figure on the site can be traced back to its source. The output is
deterministic: no timestamps, stable order, and hashes taken after normalising line endings to LF.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from autoheal.config import PROJECT_ROOT
from autoheal.evaluate import EVAL_DIR

from .activity import ActivityEvent, STATE_LABELS, FAILED, SKIPPED, STAGES, derive_lifecycle, replay
from .content import check_label, outcome_label
from .evaluation_data import AGENT, PLAYBOOK, SUITE_FILES, SYSTEM_NAMES, Suite, load_suite, recorded_escalation, wilson_interval

OUTPUT = PROJECT_ROOT / "evidence-site" / "data" / "evidence.js"
PREFIX = "window.AUTOHEAL_EVIDENCE = "
TRACE_RESULT_LIMIT = 1000  # autoheal.holdout._trace stores tool results cut to this many characters
ENGINES = {AGENT: "agent", PLAYBOOK: "playbook"}

GROUPS = {
    "B": dict(key="prospective", name="Prospective Scenario Evaluation", role="Main evidence",
              summary="Scenarios defined after the recovery system was frozen, and run once each. Written by the same team "
                      "that built the system, so they are not independently designed real-world failures."),
    "A": dict(key="development", name="Development & Regression Tests", role="Regression evidence",
              summary="Scenarios the system was improved against. They show it still handles known failures. They should "
                      "not be read as an unseen test."),
}

# Tones: success, escalation, partial, none, safety, failure. An escalation is not a failure: its tone says what happened,
# and whether it was the right result is reported separately.
OUTCOMES = {
    "recovered": ("success", "The system was repaired, and independent checks confirmed it."),
    "correct_escalation": ("escalation", "No safe repair existed, so the incident was correctly handed to a human with evidence."),
    "recovered_and_escalated": ("partial", "The repairable part was fixed, and the rest was correctly handed to a human."),
    "correct_noop": ("none", "Nothing was wrong, and correctly nothing was done."),
    "guardrail_stop": ("safety", "Safety limits ended the run and handed it to a human, without the required result."),
    "wrong_diagnosis": ("failure", "The run named the wrong cause, or changed something it should have left alone."),
    "detection_failure": ("failure", "A fault was present, but no health check raised a symptom, so recovery never started."),
}

# Presentation names, checked against each fault's definition in autoheal/prospective.py and autoheal/unseen.py. The
# original identifiers stay in every record; only what a first-time visitor reads changes.
CASES = {
    "P01": dict(title="Queue Capacity Reduced Under Steady Load", requires_escalation=False,
                what="A release caps the fulfilment queue at 40 messages while 30 orders arrive per tick, so paid orders overflow into the dead-letter store.",
                correct="Restore the queue setting and replay the dead letters, verified by independent checks.",
                bias="None expected. The Playbook has a matching rule."),
    "P02": dict(title="Intermittent Queue Backlog", requires_escalation=False,
                what="A release shrinks worker batches while traffic alternates between 6 and 22 orders per tick. The backlog grows in a sawtooth, never rising on five ticks in a row.",
                correct="Detect the slow backlog and recover. The shared detector looks for strictly rising queue depth, so it stays silent.",
                bias="Detection may fail for both systems, because the detector needs strictly rising depth."),
    "P03": dict(title="Sustained Queue Backlog Growth", requires_escalation=False,
                what="A release reduces worker batches to 10 at 14 orders per tick for 20 ticks. The queue falls steadily behind with no dead letters.",
                correct="Restore worker throughput, verified by independent checks.",
                bias="The detector check involved was tuned on a similar earlier scenario."),
    "P04": dict(title="Missing Paid Orders", requires_escalation=True,
                what="Four paid orders have a captured payment but no queue message and no dead letter, so they will never ship. No available tool can safely repair them.",
                correct="Escalate to a human and name the affected orders.",
                bias="Favours the AI Agent: rule-based recovery cannot write evidence-based escalations.",
                why="The AI Agent investigated and escalated with the affected orders named. The Playbook has no matching rule, so it escalated with no diagnosis and without naming them."),
    "P05": dict(title="Partial Payment Recovery & Escalation", requires_escalation=True,
                what="A release trims the payment timeout, so new orders stall unpaid. Two other orders already have a captured payment but are stuck, and must not be charged again.",
                correct="Fix the timeout, recover the unpaid orders, leave the paid ones untouched, and escalate those by name.",
                bias="Favours the AI Agent: it needs both a recovery and an evidence-based escalation.",
                why="Both repaired the timeout and recovered the unpaid orders. The AI Agent then escalated the paid orders by name. The Playbook retried them, payment safety refused, verification kept failing, and safety limits ended the run. Nothing was charged twice."),
    "P06": dict(title="Queue Capacity Cut with Misleading Errors", requires_escalation=False,
                what="The same queue capacity cut, with unrelated errors and warnings planted in the logs from other services.",
                correct="Restore the queue setting and replay the dead letters, without acting on the unrelated errors.",
                bias="Favours the Playbook: it ignores log text."),
    "P07": dict(title="Workflow Failure with Misleading Configuration Evidence", requires_escalation=False,
                what="A team makes an approved configuration change. Later, an unrelated workflow hangs while holding the billing lock, so other workflows queue behind it.",
                correct="Terminate the hung workflow and keep the approved change in place.",
                bias="Adversarial to the Playbook: it reverts every recent change not made by the system.",
                why="The AI Agent attributed the stall to the hung workflow and left the approved change alone. The Playbook also cleared the stall and passed verification, but reverted the approved change on the way."),
    "P08": dict(title="Throttled Customer Data Sync", requires_escalation=False,
                what="A release throttles the customer data sync to one record per tick while 25 records change at the source, so customer data goes stale.",
                correct="Restore sync throughput and resynchronise the customers.",
                bias="None expected. The Playbook has matching rules."),
    "P09": dict(title="Healthy Control: High Traffic After an Approved Change", requires_escalation=False,
                what="No fault. Traffic is high for 15 ticks, shortly after an approved configuration change.",
                correct="Raise no symptom and take no action.", bias=""),
    "P10": dict(title="Healthy Control: Promotion Burst with Lag Warnings", requires_escalation=False,
                what="No fault. A promotion sends 40 orders per tick for five ticks and logs lag warnings.",
                correct="Raise no symptom and take no action.", bias=""),
    "U1": dict(title="Worker Throughput Degradation", requires_escalation=False,
               what="A release cuts worker throughput to 5 per tick during 50 orders per tick, and the queue overflows into the dead-letter store.",
               correct="Recover the system, verified by independent checks.", bias="",
               why="The AI Agent kept working until the backlog cleared. The Playbook's fixed rules reached a safety limit before recovery was confirmed."),
    "U2": dict(title="Inconsistent Payment State", requires_escalation=True,
               what="Three orders have a captured payment but never advanced. No available tool can safely reconcile them.",
               correct="Escalate to a human with evidence, without retrying the charge.", bias="",
               why="The AI Agent escalated with evidence. The Playbook's retries were refused by payment safety, verification failed, and safety limits ended the run without a charge being repeated."),
    "U3": dict(title="Silent Queue Degradation", requires_escalation=False,
               what="A release cuts worker throughput at normal traffic for 15 ticks. The backlog grows steadily with no dead letters.",
               correct="Recover, or escalate correctly.", bias=""),
    "U4": dict(title="Healthy Control: False-Alarm Logs", requires_escalation=False,
               what="No fault. Misleading warnings and errors are written to the logs of a healthy system.",
               correct="Raise no symptom and take no action.", bias=""),
    "U5": dict(title="Recoverable Incident with Misleading Evidence", requires_escalation=False,
               what="A release cuts the payment timeout, while fake provider-outage errors are planted in the logs. The true evidence stays visible.",
               correct="Recover along the payment path, without acting on the planted errors.", bias=""),
}

_STATUS_LABELS = {"resolved": "Closed as resolved", "escalated": "Handed to a human", "healthy": "No incident opened"}


def _count(items: list, predicate=bool) -> list[int]:
    return [sum(1 for i in items if predicate(i)), len(items)]


def _tokens(run: dict) -> int:
    return sum((q.get("prompt_tokens") or 0) + (q.get("completion_tokens") or 0) for q in run.get("requests") or [])


def _model_path(run: dict, models: dict) -> str | None:
    used = [q["model"] for q in run.get("requests") or []]
    if not used:
        return None
    if used[0] == models.get("strong"):
        return "Strong model from the start"
    return "Cheap model only" if models.get("strong") not in used else "Cheap model, then strong model"


def _bool_text(value) -> str:
    return "Yes" if value else "No"


def _scoring_record(run: dict) -> list[dict]:
    """What the evaluator recorded for this run, in plain words. Fields a record does not hold are left out."""
    rows: list[tuple[str, str]] = []

    def yes_no(label: str, key: str) -> None:
        if run.get(key) is not None:
            rows.append((label, _bool_text(run[key])))

    yes_no("Symptom detected", "detected")
    if run.get("predicted"):
        rows.append(("Cause categories named", ", ".join(p.replace("_", " ") for p in run["predicted"])))
    yes_no("Diagnosis accepted", "diagnosis_precise")
    yes_no("Diagnosis accepted", "diagnosis_correct")
    yes_no("Escalation named the affected orders", "evidence_ok")
    yes_no("Configuration acceptable at the end", "config_ok")
    yes_no("Configuration restored at the end", "config_restored")
    if run.get("verification_detail"):
        yes_no("Independent verification passed", "verified")
    else:
        rows.append(("Independent verification", "Not run"))
    yes_no("Hidden check: system healthy at the end", "ground_truth_healthy_end")
    if isinstance(run.get("soak"), dict):
        rows.append(("Stayed healthy for 6 more ticks", _bool_text(run["soak"].get("ok"))))
    yes_no("Earlier payment records untouched", "payment_records_untouched")
    yes_no("Originally unpaid orders recovered", "unpaid_recovered")
    yes_no("Order and payment records consistent", "recovery_invariants_ok")
    for label, key in (("Unsafe actions executed", "unsafe_executed"), ("Unsafe actions executed", "unsafe_actions"),
                       ("Unsafe actions attempted and refused", "unsafe_attempted"),
                       ("Unrelated changes executed", "irrelevant_executed"), ("Unrelated changes executed", "irrelevant_actions"),
                       ("Invalid tool calls", "invalid_tool_calls")):
        if key in run:
            rows.append((label, str(len(run[key] or []))))
    return [{"label": a, "value": b} for a, b in rows]


def _event(e: ActivityEvent) -> dict:
    out = {k: v for k, v in asdict(e).items() if v not in (None, "", [], {}) and k != "elapsed_s"}
    # A result stored at exactly the limit is cut off. Its text stays as recorded: nothing is reconstructed.
    if e.kind in ("observe", "action", "verify", "blocked", "error") and isinstance(e.result, str):
        out["truncated"] = len(e.result) >= TRACE_RESULT_LIMIT
    return out


def run_events(run: dict) -> tuple[list[ActivityEvent], list[dict]]:
    """Feed and lifecycle for one record, through the same replay the console uses."""
    if run["outcome"] == "detection_failure":
        # Recorded with status "healthy" because no incident opened, but the fault was present: "all checks pass" would
        # misdescribe it, so this is reported as the miss it was.
        start = run["start"]
        events = [ActivityEvent(stage="Detect", tone="danger", kind="detect", title="Fault present, but no symptom was raised",
                                summary=f"The health checks stayed silent with {start.get('queue_pending', 0)} messages queued "
                                        f"and the oldest {start.get('queue_age', 0)} ticks old. Recovery never started.")]
        stages = [dict(name="Detect", state=FAILED, state_label=STATE_LABELS[FAILED], detail="Fault missed")]
        stages += [dict(name=s, state=SKIPPED, state_label=STATE_LABELS[SKIPPED], detail="") for s in STAGES[1:-1]]
        stages.append(dict(name="Not detected", state=FAILED, state_label=STATE_LABELS[FAILED], detail="No recovery started"))
        return events, stages
    events = replay(run["start"]["findings"], run.get("trace") or [], run.get("routing") or [],
                    llm_used=bool(run.get("llm_called")), status=run["status"],
                    escalation=recorded_escalation(run) if run["status"] == "escalated" else None)
    stages = [dict(name=s.name, state=s.state, state_label=STATE_LABELS[s.state], detail=s.detail)
              for s in derive_lifecycle(events, status=run["status"])]
    return events, stages


def _run(run: dict, index: int, number: int, models: dict) -> dict:
    events, stages = run_events(run)
    return {
        "id": f"{run['case']}-{number}-{ENGINES[run['agent']]}",
        "case": run["case"], "number": number, "engine": ENGINES[run["agent"]], "engine_name": SYSTEM_NAMES[run["agent"]],
        "seed": run["seed"], "source_index": index,
        "outcome": run["outcome"], "outcome_label": outcome_label(run["outcome"]), "tone": OUTCOMES[run["outcome"]][0],
        "correct": bool(run["success"]), "status": run["status"], "status_label": _STATUS_LABELS.get(run["status"], run["status"]),
        "llm": bool(run.get("llm_called")), "invoked": bool(run["start"]["findings"]),
        "steps": run["steps"], "tool_calls": run["tool_calls"], "executed": run["executed_mutations"],
        "latency_s": run["wall_s"], "cost_usd": run["cost_usd"], "llm_requests": len(run.get("requests") or []),
        "tokens": _tokens(run), "model_path": _model_path(run, models),
        "fault_present": bool(run["start"].get("fault_present")),
        "symptoms": [check_label(f) for f in run["start"]["findings"]],
        "escalation": recorded_escalation(run) if run["status"] == "escalated" else None,
        "lifecycle": stages, "events": [_event(e) for e in events], "scoring": _scoring_record(run), "raw": run,
    }


def _engine_summary(suite: Suite, system: str) -> dict:
    runs = suite.of(system)
    k, n = suite.correct(system)
    lo, hi = wilson_interval(k, n)
    latency = suite.latency(system)
    invoked = [r for r in runs if r["start"]["findings"]]
    llm_runs = [r for r in runs if r.get("llm_called")]
    must_escalate = [r for r in runs if CASES[r["case"]]["requires_escalation"]]
    diagnoses = [r.get("diagnosis_precise", r.get("diagnosis_correct")) for r in invoked]
    has_invalid = any("invalid_tool_calls" in r for r in runs)
    irrelevant = sum(len(r.get("irrelevant_executed", r.get("irrelevant_actions")) or []) for r in runs)
    steps = [r["steps"] for r in (llm_runs if system == AGENT else invoked)]
    return {
        "name": SYSTEM_NAMES[system], "correct": k, "total": n, "pct": round(100 * k / n), "ci": [round(100 * lo), round(100 * hi)],
        "outcomes": dict(Counter(r["outcome"] for r in runs).most_common()),
        "detection": suite.detection(system), "false_positives": suite.false_positives(system),
        "diagnosis": _count([d for d in diagnoses if d is not None]),
        "escalation": _count(must_escalate, lambda r: r["success"]),
        "unsafe_executed": suite.unsafe_executed(system), "unsafe_attempted": suite.unsafe_attempted(system),
        "irrelevant_executed": irrelevant,
        "invalid_tool_calls": sum(len(r.get("invalid_tool_calls") or []) for r in runs) if has_invalid else None,
        "latency": {"mean_s": round(latency.mean_s, 2), "min_s": round(latency.min_s, 2), "max_s": round(latency.max_s, 2)} if latency else None,
        "steps": {"mean": round(statistics.mean(steps), 1), "min": min(steps), "max": max(steps)} if steps else None,
        "cost_usd": round(suite.cost(system), 6), "cost_per_correct": round(suite.cost_per_correct(system), 6) if k else None,
        "llm_requests": sum(len(r.get("requests") or []) for r in runs), "tokens": sum(_tokens(r) for r in runs),
        "model_paths": dict(sorted(Counter(p for p in (_model_path(r, suite.models) for r in runs) if p).items())),
    }


def _category(row) -> str:
    if not CASES[row.case]["what"].startswith("No fault"):
        if row.agent > row.playbook:
            return "difference"
        if row.playbook > row.agent:
            return "rules_ahead"
        return "shared_failure" if row.agent == 0 else "tied"
    return "control"


def _case(row, suite: Suite) -> dict:
    meta = CASES[row.case]
    per_engine = {}
    for system in (AGENT, PLAYBOOK):
        runs = [r for r in suite.of(system) if r["case"] == row.case]
        counts = Counter(r["outcome"] for r in runs)
        main = counts.most_common(1)[0][0]
        per_engine[ENGINES[system]] = {"correct": sum(bool(r["success"]) for r in runs), "total": len(runs),
                                       "outcome_label": outcome_label(main), "tone": OUTCOMES[main][0],
                                       "mixed": len(counts) > 1}
    return {"id": row.case, "title": meta["title"], "original_title": row.title, "what": meta["what"], "correct": meta["correct"],
            "why": meta.get("why", ""), "bias": meta["bias"], "bias_original": row.declared_bias, "leaning": row.leaning,
            "category": _category(row), **per_engine}


def _sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _group(suite_key: str, directory: Path) -> dict:
    suite = load_suite(suite_key, directory)
    report = suite.report
    numbers: dict[str, list] = {}
    for case, seed in report["runs"]:
        numbers.setdefault(case, []).append(seed)
    runs = [_run(r, i, numbers[r["case"]].index(r["seed"]) + 1, suite.models) for i, r in enumerate(suite.results)]
    agent_only, playbook_only = suite.discordant()
    v1 = [r for r in suite.results if "outcome_v1" in r]
    return {
        **GROUPS[suite_key],
        "source": {"file": f"data/eval/{SUITE_FILES[suite_key]}", "sha256": _sha256_lf(Path(directory) / SUITE_FILES[suite_key]),
                   "records": len(suite.results), "suite_name": f"Suite {suite_key}"},
        "provenance": {k: report[k] for k in ("tag", "label", "evidence_type", "note", "agent_version", "freeze_commit", "frozen",
                                              "scoring_version", "budget_usd", "additional_spend_usd", "models", "caps", "stopped")
                       if k in report},
        "agent": _engine_summary(suite, AGENT), "playbook": _engine_summary(suite, PLAYBOOK),
        "paired": {"agent_only": len(agent_only), "playbook_only": len(playbook_only), "mcnemar_p": round(suite.mcnemar_p(), 4),
                   "agent_only_cases": sorted({r["case"] for r in agent_only})},
        "scoring_amendment_changed": sum(r["outcome"] != r["outcome_v1"] for r in v1) if v1 else None,
        "cases": [_case(r, suite) for r in suite.cases()],
        "runs_per_case": {c: len(s) for c, s in numbers.items()},
        "runs": runs,
    }


def build(directory: Path = EVAL_DIR) -> dict:
    directory = Path(directory)
    return {
        "site": {"name": "AutoHeal", "title": "Evaluation & Evidence", "repository": "https://github.com/Remas444/AutoHeal"},
        "outcomes": {k: {"label": outcome_label(k), "tone": v[0], "meaning": v[1]} for k, v in OUTCOMES.items()},
        "trace_result_limit": TRACE_RESULT_LIMIT,
        "groups": [_group("B", directory), _group("A", directory)],
    }


def render(data: dict) -> str:
    return PREFIX + json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=False, default=str) + ";\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only verify that the published file is current")
    args = parser.parse_args(argv)
    text = render(build())
    if args.check:
        current = OUTPUT.read_bytes().replace(b"\r\n", b"\n").decode("utf-8") if OUTPUT.exists() else None
        if current != text:
            print(f"{OUTPUT} is out of date. Run: python -m autoheal_ui.evidence_export", file=sys.stderr)
            return 1
        print(f"{OUTPUT.name} matches the committed results.")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(text.encode("utf-8"))
    print(f"Wrote {OUTPUT} ({len(text):,} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
