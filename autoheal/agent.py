"""AutoHeal agent loop: detect -> route model -> investigate -> act (guarded) -> verify -> resolve | escalate."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Callable, Optional

from pydantic import BaseModel, ValidationError

from .config import Settings
from .db import scalar
from .detector import Finding, made_progress, outstanding_work, run_checks
from .llm import BudgetExceeded, ChatModel, LLMError, UsageLedger
from .safety import SafetyGuard
from .simulator import Shop
from .tools import TOOLS, ToolContext, ToolError, execute, openai_tool_schemas, to_json
from .verifier import Verification, verify

SYSTEM_PROMPT = """You are AutoHeal, an SRE incident-recovery agent for an e-commerce backend.
Architecture: orders -> payment gateway -> fulfillment queue -> worker -> shipments; a CRM->customers sync job;
billing workflows that share one lock. Time advances in ticks.

Method:
1. Investigate before acting. You are given SYMPTOMS; find the ROOT CAUSES with concrete evidence.
   Recent config changes (get_config_history) and WARN/ERROR logs are prime evidence. There may be more
   than one independent cause at once.
2. Fix causes before symptoms: revert/repair the cause first, then repair the damage (retry stuck payments,
   replay dead letters, resync data, release a stuck lock). To undo a bad deploy, use rollback_config.
   If a backlog blocks a repair (e.g. the queue is full), add throughput (queue.worker_batch) and let it
   drain with wait_and_observe before retrying; prefer that over raising limits above pre-incident values.
3. Read every tool result. If a repair caused new damage (e.g. messages dead-lettered again), repair that too.
4. Prefer the lowest-risk action that works. Never use destructive actions to make metrics look healthy.
5. When done, call finish_incident with categories, evidence and fixes. The system is then verified
   independently with live probe traffic. If verification fails, keep working on what remains.
6. Call escalate_to_human when the cause is outside your control (e.g. an upstream provider outage),
   evidence is insufficient, or your fixes did not work. A clear escalation is a success; looping is a failure.
Be efficient: request several independent read-only tools in one turn; keep text brief.
Never re-query data a tool already returned (e.g. SQL over logs/config); once evidence explains the symptoms, act or escalate."""

# Sent once per continuous healthy state after a successful repair/wait. It never closes the incident:
# the agent must still call finish_incident, and the independent verifier decides.
COMPLETION_NUDGE = ("All invariants now pass. Call finish_incident (independent verification will confirm) "
                    "or explain what remains.")
# Once recovery has started (a state-changing action executed), this many consecutive turns without measurable
# progress earn a factual focus reminder; at most MAX_FOCUS_REMINDERS per incident. A stalled recovery (health checks
# still failing) also moves to the strong model; a healthy-but-unfinished incident is reminded on the current model
# first and moves to the strong model only if a second completion reminder is needed.
STALL_TURNS = 3
MAX_FOCUS_REMINDERS = 2
# Before any state change: this many investigation turns without a decision earn ONE factual decision prompt and a
# move to the strong model. It never acts for the agent and explicitly allows escalation instead of a repair.
INVESTIGATION_TURNS = 6


class IncidentResult(BaseModel):
    incident_id: int
    status: str  # healthy | resolved | escalated
    findings: list[Finding]
    root_causes: list[dict] = []
    summary: str = ""
    confidence: Optional[float] = None
    escalation: Optional[dict] = None
    escalation_cause: Optional[str] = None  # agent | guardrail | infrastructure
    verifications: list[Verification] = []
    routing: list[dict] = []
    steps: int = 0
    tool_calls: int = 0
    mutations: int = 0  # allowed mutation attempts (the guard's budget accounting)
    executed_mutations: int = 0  # state changes that actually ran
    unexecuted_mutation_attempts: int = 0  # allowed but rejected by the tool, failed, or dry-run
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    report_md: str = ""


class AutoHealAgent:
    def __init__(self, conn: sqlite3.Connection, shop: Shop, llm: ChatModel, settings: Settings,
                 ledger: UsageLedger, on_event: Callable[[str, dict], None] | None = None):
        self.conn, self.shop, self.llm, self.s, self.ledger = conn, shop, llm, settings, ledger
        self.on_event = on_event or (lambda kind, data: None)
        self.tools_schema = openai_tool_schemas()

    # ------------------------------------------------------------- helpers
    def _route(self, findings: list[Finding]) -> tuple[str, str]:
        checks = {f.check for f in findings}
        if len(checks) >= 2:
            return self.s.strong_model, f"{len(checks)} distinct symptoms -> possible multi-fault -> strong model"
        return self.s.cheap_model, "single symptom -> cheap model first"

    def _log_step(self, incident_id: int, step: int, kind: str, name: str | None, args, result, model: str = ""):
        self.conn.execute("INSERT INTO agent_steps (incident_id, step, kind, name, args_json, result_json, model, ts) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (incident_id, step, kind, name, to_json(args), to_json(result, 4000), model, time.time()))
        self.conn.commit()
        if kind != "escalate":  # _escalate() emits the full escalation event (with reason) right after
            self.on_event(kind, {"step": step, "name": name, "args": args, "result": result, "model": model})

    # ----------------------------------------------------------------- run
    def run(self) -> IncidentResult:
        t0 = time.time()
        findings = run_checks(self.conn)
        cur = self.conn.execute("INSERT INTO incidents (opened_tick, status, findings_json) VALUES (?, 'open', ?)",
                                (self.shop.tick_now, to_json([f.model_dump() for f in findings])))
        self.conn.commit()
        res = IncidentResult(incident_id=cur.lastrowid, status="open", findings=findings)
        if not findings:
            res.status, res.summary = "healthy", "All invariants pass. No LLM call made ($0)."
            return self._close(res, t0)

        model, why = self._route(findings)
        res.routing.append({"step": 0, "model": model, "reason": why})
        self.on_event("route", {"model": model, "reason": why, "findings": [f.model_dump() for f in findings]})

        guard = SafetyGuard(self.s, self.conn, res.incident_id)
        ctx = ToolContext(self.conn, self.shop, res.incident_id)
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"INCIDENT #{res.incident_id} opened at tick {self.shop.tick_now}.\n"
                                        f"Detected symptoms:\n{to_json([f.model_dump() for f in findings])}\n"
                                        f"Investigate, recover, and finish or escalate."},
        ]
        nudges = 0
        verify_attempts = 0
        completion_nudged = False
        recovery_started = False
        stalled_turns = focus_reminders = completion_reminders = 0
        decision_prompted = False
        work = outstanding_work(self.conn)
        self._last_progress_action = ""

        for step in range(1, self.s.max_steps + 1):
            res.steps = step
            if guard.should_force_escalation:
                return self._escalate(res, t0, f"Safety guard blocked {guard.blocks} actions; stopping to avoid a loop.",
                                      cause="guardrail", guard=guard)
            try:
                msg = self.llm.chat(messages, self.tools_schema, model=model, purpose="investigate",
                                    incident_id=res.incident_id)
            except BudgetExceeded as e:
                return self._escalate(res, t0, f"Budget cap hit: {e}", cause="infrastructure", guard=guard)
            except LLMError as e:
                return self._escalate(res, t0, f"LLM unavailable: {e}", cause="infrastructure", guard=guard)

            tool_calls = msg.get("tool_calls") or []
            assistant = {"role": "assistant", "content": msg.get("content") or ""}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            if msg.get("reasoning_details"):  # must be echoed back unchanged (e.g. Gemini thought signatures)
                assistant["reasoning_details"] = msg["reasoning_details"]
            messages.append(assistant)
            if msg.get("content"):
                self._log_step(res.incident_id, step, "thought", None, {}, msg["content"], model)

            if not tool_calls:
                nudges += 1
                if nudges >= 2:
                    return self._escalate(res, t0, "Agent stopped calling tools without finishing.", cause="guardrail",
                                          guard=guard)
                messages.append({"role": "user", "content": "Use the tools. Finish with finish_incident or "
                                                            "escalate_to_human."})
                continue

            finished = None
            executed = mutated = terminal_called = False
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args, result = {}, {"error": "arguments were not valid JSON"}
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": to_json(result)})
                    continue
                res.tool_calls += 1
                result = self._handle_call(res, ctx, guard, step, name, args, model)
                executed |= self._last_call_executed
                mutated |= self._last_call_executed and TOOLS[name].mutating
                terminal_called |= bool(TOOLS.get(name) and TOOLS[name].terminal)

                if name == "escalate_to_human" and result.get("escalated"):
                    finished = ("escalate", args)
                elif name == "finish_incident" and result.get("verification"):
                    v: Verification = result.pop("_v")
                    res.verifications.append(v)
                    if v.passed:
                        finished = ("resolved", args)
                    else:
                        verify_attempts += 1
                        if v.findings:
                            completion_nudged = False
                        model = self._upgrade(res, messages, step, model,
                                              "verification failed -> upgrade to strong model")
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": to_json(result, self.s.tool_result_chars)})

            if finished and finished[0] == "resolved":
                a = finished[1]
                res.status, res.summary = "resolved", a.get("summary", "")
                res.root_causes, res.confidence = a.get("root_causes", []), a.get("confidence")
                res.mutations = guard.mutations
                res.executed_mutations = guard.executed_mutations
                res.unexecuted_mutation_attempts = guard.unexecuted_mutation_attempts
                return self._close(res, t0)
            if finished and finished[0] == "escalate":
                a = finished[1]
                return self._escalate(res, t0, a.get("reason", ""), summary=a.get("summary", ""),
                                      categories=a.get("suspected_categories", []),
                                      actions=a.get("recommended_actions", []), guard=guard)
            if verify_attempts >= self.s.max_verify_attempts:
                return self._escalate(res, t0, f"Verification failed {verify_attempts} times; "
                                               f"handing off instead of retrying.", cause="guardrail", guard=guard)
            # Evaluated once per turn, after all its tool calls: a successful repair or wait that leaves every
            # invariant passing earns ONE reminder to finish; new violations re-arm it.
            nudged_now = False
            if executed and not terminal_called:
                if run_checks(self.conn):
                    completion_nudged = False
                elif not completion_nudged:
                    completion_nudged = nudged_now = True
                    messages.append({"role": "user", "content": COMPLETION_NUDGE})
                    self._log_step(res.incident_id, step, "nudge", None, {}, COMPLETION_NUDGE, model)
            # Focus: once recovery has started, turns that leave outstanding work unchanged (e.g. investigation
            # unrelated to the active repair) are counted; a stall earns one factual reminder per episode.
            before, work = work, outstanding_work(self.conn)
            recovery_started |= mutated
            if terminal_called or nudged_now or made_progress(before, work):
                stalled_turns = 0
            elif recovery_started:
                stalled_turns += 1
            if stalled_turns >= STALL_TURNS and focus_reminders < MAX_FOCUS_REMINDERS and step < self.s.max_steps:
                stalled_turns, focus_reminders = 0, focus_reminders + 1
                failing = sorted({f.check for f in run_checks(self.conn)})
                if failing:  # recovery stalled: work or violations remain without measurable progress
                    model = self._upgrade(res, messages, step, model,
                                          "no measurable progress -> upgrade to strong model")
                else:  # recovery complete, verification pending: remind on the current model first
                    completion_reminders += 1
                    if completion_reminders > 1:  # bounded fallback when completion keeps being ignored
                        model = self._upgrade(res, messages, step, model,
                                              "finish_incident repeatedly not called -> upgrade to strong model")
                note = self._focus_message(work, failing, self.s.max_steps - step)
                messages.append({"role": "user", "content": note})
                self._log_step(res.incident_id, step, "focus", None, {}, note, model)
            # Decision point: no executed state change yet (rejected or blocked attempts do not count) after a long
            # read-only investigation -> one decision prompt and the strong model. Never forces a repair.
            if (not recovery_started and not terminal_called and not decision_prompted
                    and INVESTIGATION_TURNS <= step < self.s.max_steps):
                decision_prompted = True
                model = self._upgrade(res, messages, step, model,
                                      f"no decision after {step} investigation turns -> upgrade to strong model")
                note = self._decision_message(res.incident_id, step)
                messages.append({"role": "user", "content": note})
                self._log_step(res.incident_id, step, "decision", None, {}, note, model)

        return self._escalate(res, t0, f"Step limit ({self.s.max_steps}) reached without verified recovery.",
                              cause="guardrail", guard=guard)

    def _upgrade(self, res: IncidentResult, messages: list[dict], step: int, model: str, reason: str) -> str:
        if model == self.s.strong_model:
            return model
        for m in messages:  # reasoning_details are model-specific (e.g. Gemini thought signatures)
            m.pop("reasoning_details", None)
        res.routing.append({"step": step, "model": self.s.strong_model, "reason": reason})
        self.on_event("route", {"model": self.s.strong_model, "reason": reason})
        return self.s.strong_model

    def _decision_message(self, incident_id: int, step: int) -> str:
        failing = sorted({f.check for f in run_checks(self.conn)})
        reads = scalar(self.conn, "SELECT COUNT(*) FROM agent_steps WHERE incident_id=? AND kind='observe'",
                       (incident_id,))
        state = f"Current symptoms: {to_json(failing)}." if failing else "All checks currently pass."
        finish = "" if failing else " If you believe it is resolved, call finish_incident (verification will confirm)."
        return (f"{step} turns of investigation and no state change or decision yet ({reads} read-only calls; "
                f"{self.s.max_steps - step} of {self.s.max_steps} steps remain). {state} Decide now using the evidence "
                f"you already have: (a) act on the root cause your evidence supports, (b) escalate_to_human with that "
                f"evidence if no safe fix exists, or (c) name the one specific piece of evidence still missing and "
                f"get it.{finish} Do not take an action the evidence does not support; escalation is an acceptable "
                f"outcome.")

    def _focus_message(self, work: dict[str, int], failing: list[str], steps_left: int) -> str:
        open_work = {k: v for k, v in work.items() if v and k != "queue_pending"}
        if failing:
            outstanding = to_json(open_work) if open_work else f"failing checks {to_json(failing)}"
            head = f"No measurable recovery progress for {STALL_TURNS} turns. Outstanding work: {outstanding}."
            tail = "Act on the outstanding work, or call finish_incident / escalate_to_human."
        else:
            head = ("All invariants pass but finish_incident has not been called. Recovery looks complete and "
                    "is pending independent verification.")
            tail = "Call finish_incident now (verification will confirm or report what remains) or escalate_to_human."
        last = f" Last action with measurable progress: {self._last_progress_action}." if self._last_progress_action else ""
        return f"{head}{last} {steps_left} of {self.s.max_steps} steps remain. {tail}"

    def _handle_call(self, res: IncidentResult, ctx: ToolContext, guard: SafetyGuard, step: int,
                     name: str, args: dict, model: str) -> dict:
        self._last_call_executed = False  # an allowed mutation or time-advancing call actually ran
        decision = guard.check(name, args)
        if not decision.allowed:
            result = {"blocked": decision.reason}
            self._log_step(res.incident_id, step, "blocked", name, args, result, model)
            return result
        spec = TOOLS[name]
        if spec.terminal:
            try:
                spec.args.model_validate(args)
            except ValidationError as e:
                result = {"error": f"invalid arguments: {e.errors()[:3]}"}
                self._log_step(res.incident_id, step, "error", name, args, result, model)
                return result
            if name == "finish_incident":
                v = verify(self.conn, self.shop)
                result = {"verification": v.summary(), "_v": v}
                self._log_step(res.incident_id, step, "verify", name, args, v.model_dump(), model)
                return result
            self._log_step(res.incident_id, step, "escalate", name, args, {}, model)
            return {"escalated": True}
        tracked = spec.mutating or spec.advances_time
        if decision.dry_run:
            result = {"dry_run": True, "note": f"{name} NOT executed (dry-run mode)"}
            guard.record(name, args, executed=False, progressed=False)
            self._log_step(res.incident_id, step, "action", name, args, result, model)
            return result
        before = outstanding_work(self.conn) if tracked else None
        try:
            result = execute(ctx, name, args)
            kind = "action" if spec.mutating else "observe"
            if tracked:
                progressed = made_progress(before, outstanding_work(self.conn))
                guard.record(name, args, executed=True, progressed=progressed)
                self._last_call_executed = True
                if progressed:
                    self._last_progress_action = f"{name} {to_json(args)} at step {step}"
        except (ToolError, ValidationError) as e:
            result, kind = {"error": str(e)[:500]}, "error"
            if tracked:
                guard.record(name, args, executed=False, progressed=False)
        self._log_step(res.incident_id, step, kind, name, args, result, model)
        return result

    # ------------------------------------------------------------ closing
    def _escalate(self, res: IncidentResult, t0: float, reason: str, summary: str = "", categories=None,
                  actions=None, cause: str = "agent", guard: SafetyGuard | None = None) -> IncidentResult:
        """cause: agent (escalate_to_human), guardrail (step/mutation/loop/verify limits),
        infrastructure (LLM unavailable or budget cap)."""
        res.status, res.escalation_cause = "escalated", cause
        res.mutations = guard.mutations if guard else 0
        res.executed_mutations = guard.executed_mutations if guard else 0
        res.unexecuted_mutation_attempts = guard.unexecuted_mutation_attempts if guard else 0
        remaining = [f.model_dump() for f in run_checks(self.conn)]
        res.escalation = {"reason": reason, "cause": cause, "summary": summary,
                          "suspected_categories": categories or [], "recommended_actions": actions or [],
                          "open_symptoms": remaining}
        res.summary = summary or reason
        self.on_event("escalate", res.escalation)
        return self._close(res, t0)

    def _close(self, res: IncidentResult, t0: float) -> IncidentResult:
        usage = self.ledger.incident_rows(res.incident_id)
        res.cost_usd = round(sum(u["cost_usd"] or 0 for u in usage), 6)
        res.prompt_tokens = sum(u["prompt_tokens"] or 0 for u in usage)
        res.completion_tokens = sum(u["completion_tokens"] or 0 for u in usage)
        res.wall_s = round(time.time() - t0, 2)
        res.report_md = render_report(res, self.conn)
        self.conn.execute("UPDATE incidents SET status=?, closed_tick=?, report_json=?, report_md=? WHERE id=?",
                          (res.status, self.shop.tick_now, res.model_dump_json(), res.report_md, res.incident_id))
        self.conn.commit()
        self.on_event("done", {"status": res.status, "cost_usd": res.cost_usd})
        return res


def render_report(res: IncidentResult, conn: sqlite3.Connection) -> str:
    icon = {"resolved": "✅", "escalated": "🧑‍💻", "healthy": "💚"}.get(res.status, "•")
    L = [f"# {icon} Incident #{res.incident_id} — {res.status.upper()}", "", f"**Summary:** {res.summary}", ""]
    if res.findings:
        L += ["## Detected symptoms"] + [f"- `{f.check}` ({f.severity}): {f.detail}" for f in res.findings] + [""]
    if res.root_causes:
        L.append("## Root causes")
        for rc in res.root_causes:
            L.append(f"- **[{rc.get('category')}]** {rc.get('cause')}")
            for ev in rc.get("evidence", [])[:4]:
                L.append(f"  - evidence: {ev}")
            L.append(f"  - fix: {rc.get('fix_applied')}")
        if res.confidence is not None:
            L.append(f"\nConfidence: {res.confidence:.0%}")
        L.append("")
    if res.escalation:
        e = res.escalation
        L += ["## Escalation to human", f"- **Reason:** {e['reason']}",
              f"- Escalation cause: **{e['cause']}**"]
        if e["suspected_categories"]:
            L.append(f"- Suspected: {', '.join(e['suspected_categories'])}")
        L += [f"- Recommended: {a}" for a in e["recommended_actions"]]
        L += [f"- Still open: `{s['check']}` — {s['detail']}" for s in e["open_symptoms"]] + [""]
    acts = conn.execute("SELECT step, kind, name, args_json, result_json FROM agent_steps WHERE incident_id=? "
                        "AND kind IN ('action','blocked','verify') ORDER BY id", (res.incident_id,)).fetchall()
    if acts:
        L.append("## Actions & verification")
        for a in acts:
            L.append(f"- step {a['step']} · **{a['kind']}** `{a['name']}` {a['args_json'] if a['kind'] != 'verify' else ''}"
                     f" → {a['result_json'][:160]}")
        L.append("")
    L += ["## Cost & routing",
          f"- LLM cost: **${res.cost_usd:.4f}** · tokens in/out: {res.prompt_tokens}/{res.completion_tokens}",
          f"- Steps: {res.steps} · tool calls: {res.tool_calls} · mutations: {res.mutations} · wall: {res.wall_s}s"]
    L += [f"- Routing @step {r['step']}: `{r['model']}` — {r['reason']}" for r in res.routing]
    return "\n".join(L)
