"""Turns observed agent events into the console's activity feed, lifecycle and outcome.

Pure logic (no Streamlit). Every event here comes from something the runtime actually did: the `on_event` callback
during a live run, or the `agent_steps` trace afterwards. Nothing is invented: if the runtime did not report it, it
does not appear. Agent text is shown only where the runtime recorded it as message content; private reasoning is
never recorded and never displayed.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .content import ACTION_TITLES, READ_TOOL_TITLES, check_label

STAGES = ("Detect", "Investigate", "Decide", "Execute", "Verify", "Outcome")

# Lifecycle states. "skipped" means the run ended without this stage ever being observed.
PENDING, ACTIVE, COMPLETED, FAILED, BLOCKED, SKIPPED, ESCALATED = (
    "pending", "active", "completed", "failed", "blocked", "skipped", "escalated")
STATE_LABELS = {PENDING: "Pending", ACTIVE: "In progress", COMPLETED: "Completed", FAILED: "Failed",
                BLOCKED: "Blocked", SKIPPED: "Not performed", ESCALATED: "Escalated"}

# Effects of a state-changing request.
EXECUTED, REJECTED, REFUSED, DRY_RUN, TOOL_FAILED = "executed", "blocked", "refused", "dry_run", "failed"

_APPROVAL = re.compile(r"approval #(\d+)")


@dataclass(frozen=True)
class ActivityEvent:
    stage: str
    tone: str  # info | success | warning | danger
    title: str
    summary: str = ""
    kind: str = ""
    tool: Optional[str] = None
    step: Optional[int] = None
    args: Any = None
    result: Any = None
    model: str = ""
    elapsed_s: Optional[float] = None
    effect: Optional[str] = None


def parse_json(value: Any) -> Any:
    """Trace columns hold JSON, sometimes truncated. Truncated values stay as text rather than being guessed at."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _read_summary(name: str, r: Any) -> str:
    if not isinstance(r, dict):
        return ""
    if name == "get_health_report":
        n = len(r.get("findings", []))
        return f"{_plural(n, 'active symptom')} reported" if n else "No active symptoms reported"
    if name == "search_logs":
        return f"{_plural(r.get('count', 0), 'log entry').replace('entrys', 'entries')} returned"
    if name == "get_metrics":
        return f"{_plural(len(r.get('series', {})), 'metric series').replace('seriess', 'series')} returned"
    if name == "get_config":
        return f"{_plural(len(r.get('config', [])), 'setting')} returned"
    if name == "get_config_history":
        return f"{_plural(len(r.get('changes', [])), 'configuration change')} returned"
    if name == "run_readonly_sql":
        return f"{_plural(r.get('row_count', 0), 'row')} returned"
    if name == "list_workflows":
        return f"{_plural(len(r.get('workflows', [])), 'workflow')} returned"
    if name == "wait_and_observe":
        after = r.get("after", {})
        return (f"{_plural(r.get('ticks_waited', 0), 'tick')} passed; {after.get('queue_pending', 0)} queued, "
                f"{after.get('dead_letter_unreplayed', 0)} in dead letter, "
                f"{_plural(len(r.get('findings', [])), 'symptom')} open")
    return ""


def _action_summary(name: str, r: Any) -> str:
    if not isinstance(r, dict):
        return ""
    if name in ("rollback_config", "set_config"):
        return f"{r.get('key')}: {r.get('from')} to {r.get('to')}"
    if name == "retry_stuck_payments":
        return (f"Attempted {r.get('attempted', 0)}: {r.get('paid', 0)} paid, {r.get('still_timeout', 0)} still timing "
                f"out, {r.get('failed', 0)} failed, {r.get('dead_lettered', 0)} dead-lettered")
    if name == "replay_dead_letter":
        return (f"{r.get('replayed', 0)} replayed, {r.get('rejected_queue_full', 0)} rejected (queue full), "
                f"{r.get('remaining', 0)} remaining")
    if name == "resync_customers":
        return f"{_plural(r.get('records_resynced', 0), 'record')} resynchronised, {r.get('mismatched_after', 0)} still mismatched"
    if name == "terminate_workflow":
        return f"Workflow #{r.get('terminated')} terminated; lock released"
    if name == "purge_dead_letter":
        return f"{_plural(r.get('deleted', 0), 'message')} permanently deleted"
    return ""


def _verification(result: Any) -> tuple[Optional[bool], Optional[int], Optional[int]]:
    """(passed, fulfilled, probes). Recorded traces may truncate the JSON, so fall back to reading its leading fields."""
    if isinstance(result, dict):
        return result.get("passed"), result.get("probe_fulfilled"), result.get("probe_orders")
    text = str(result)
    passed = {"true": True, "false": False}.get((re.search(r'"passed":\s*(true|false)', text) or [None, ""])[1])
    probes = re.search(r'"probe_orders":\s*(\d+)', text)
    fulfilled = re.search(r'"probe_fulfilled":\s*(\d+)', text)
    return passed, int(fulfilled[1]) if fulfilled else None, int(probes[1]) if probes else None


class ActivityLog:
    """Reduces runtime events into an ordered feed. Live runs and stored traces both go through this class."""

    def __init__(self, llm_used: bool = True):
        # The Playbook reports routing bookkeeping naming a model it never calls, so routing is hidden unless an LLM ran.
        self.llm_used = llm_used
        self.events: list[ActivityEvent] = []
        self._acted = False
        self._t0 = time.time()

    def _add(self, **kw) -> None:
        self.events.append(ActivityEvent(**kw))

    def detect(self, findings: Iterable[Any], elapsed_s: float | None = None) -> None:
        items = [f if isinstance(f, dict) else {"check": f} for f in findings]
        if not items:
            self._add(stage="Detect", tone="success", kind="detect", title="All health checks pass",
                      summary="No anomaly detected. No LLM call is made for a healthy system.", elapsed_s=elapsed_s)
            return
        names = ", ".join(check_label(i["check"]) for i in items)
        self._add(stage="Detect", tone="danger", kind="detect", title=f"Anomaly detected: {_plural(len(items), 'symptom')}",
                  summary=names, result=items, elapsed_s=elapsed_s)

    def route(self, model: str, reason: str, elapsed_s: float | None = None) -> None:
        if self.llm_used:
            self._add(stage="Investigate", tone="info", kind="route", title="Model selected for this incident",
                      summary=reason.replace("->", "to"), model=model, elapsed_s=elapsed_s)

    def step(self, kind: str, name: str | None, args: Any, result: Any, model: str = "", step: int | None = None,
             elapsed_s: float | None = None) -> None:
        args, result = parse_json(args), parse_json(result)
        base = dict(kind=kind, tool=name, step=step, args=args, result=result, model=model if self.llm_used else "",
                    elapsed_s=elapsed_s)
        action_title = ACTION_TITLES.get(name or "", name or "")

        if kind == "thought":
            stage = "Execute" if self._acted else "Investigate"
            self._add(stage=stage, tone="info", title="Agent note", summary=str(result), **base)
        elif kind == "observe":
            advancing = name == "wait_and_observe" and self._acted
            self._add(stage="Execute" if advancing else "Investigate", tone="info",
                      title=READ_TOOL_TITLES.get(name or "", name or "Reading system state"),
                      summary=_read_summary(name or "", result), **base)
        elif kind == "action":
            self._request(action_title, base)
            dry = isinstance(result, dict) and result.get("dry_run")
            if not dry:
                self._acted = True
            self._add(stage="Execute", tone="warning" if dry else "success",
                      title=f"Proposed only, not executed: {action_title}" if dry else f"Recovery action executed: {action_title}",
                      summary="Dry-run mode is on, so nothing changed." if dry else _action_summary(name or "", result),
                      effect=DRY_RUN if dry else EXECUTED, **base)
        elif kind == "blocked":
            reason = result.get("blocked", "") if isinstance(result, dict) else str(result)
            approval = _APPROVAL.search(reason)
            self._request(action_title, base)
            self._add(stage="Execute", tone="warning", summary=reason, effect=REJECTED,
                      title=f"Human approval requested: {action_title}" if approval
                      else f"Blocked by safety controls: {action_title}", **base)
        elif kind == "error":
            self._error(name, result, action_title, base)
        elif kind == "verify":
            self._verify(args, result, base)
        elif kind == "nudge":
            self._add(stage="Execute", tone="success", title="All health checks now pass",
                      summary="Reminder issued to finish; independent verification still has to confirm recovery.", **base)
        elif kind == "focus":
            self._add(stage="Decide", tone="warning", title="Reminder issued: no measurable progress",
                      summary=str(result), **base)
        elif kind == "decision":
            self._add(stage="Decide", tone="info", title="Decision requested after a long investigation",
                      summary=str(result), **base)

    def _request(self, title: str, base: dict) -> None:
        self._add(stage="Decide", tone="info", title=f"Guarded action requested: {title}",
                  summary="Checked by the safety guard against the risk policy and budgets.",
                  **{**base, "result": None})

    def _error(self, name: str | None, result: Any, title: str, base: dict) -> None:
        message = result.get("error", "") if isinstance(result, dict) else str(result)
        if name in ACTION_TITLES:
            refused = message.startswith("Refused")
            self._request(title, base)
            self._add(stage="Execute", tone="warning" if refused else "danger", summary=message,
                      effect=REFUSED if refused else TOOL_FAILED,
                      title=f"Refused by safety controls: {title}" if refused else f"Recovery action failed: {title}", **base)
        elif name in ("finish_incident", "escalate_to_human"):
            self._add(stage="Decide", tone="warning", title="Closing report rejected: invalid arguments", summary=message, **base)
        else:
            self._add(stage="Investigate", tone="warning", title=f"Tool call rejected: {READ_TOOL_TITLES.get(name or '', name)}",
                      summary=message, **base)

    def _verify(self, args: Any, result: Any, base: dict) -> None:
        causes = args.get("root_causes", []) if isinstance(args, dict) else []
        if causes:
            cats = ", ".join(sorted({str(c.get("category", "unknown")).replace("_", " ") for c in causes}))
            self._add(stage="Decide", tone="info", title="Root cause reported", summary=f"Category: {cats}",
                      **{**base, "kind": "root_cause", "result": causes})
        passed, fulfilled, probes = _verification(result)
        probe_text = f"{fulfilled}/{probes} probe orders fulfilled" if probes is not None else "Probe results recorded"
        word = {True: "passed", False: "failed"}.get(passed, "recorded")
        self._add(stage="Verify", tone={True: "success", False: "danger"}.get(passed, "info"),
                  title=f"Independent verification {word}", summary=probe_text, **base)

    def escalation(self, e: dict, elapsed_s: float | None = None) -> None:
        cause = e.get("cause")
        if cause == "agent":
            self._add(stage="Decide", tone="warning", kind="escalate_decision", tool="escalate_to_human",
                      title="Recovery engine chose to hand off to a human", summary=e.get("reason", ""), args=e,
                      elapsed_s=elapsed_s)
        title = {"guardrail": "Stopped by safety controls; escalated to a human",
                 "infrastructure": "Escalated: the LLM service or budget was unavailable"}.get(
            cause, "Escalation issued to a human operator")
        self._add(stage="Outcome", tone="warning", kind="escalate", title=title, summary=e.get("reason", ""), result=e,
                  elapsed_s=elapsed_s)

    def done(self, status: str, elapsed_s: float | None = None) -> None:
        if status == "resolved":
            self._add(stage="Outcome", tone="success", kind="done", title="Recovery verified",
                      summary="Incident closed after independent verification passed.", elapsed_s=elapsed_s)
        elif status == "healthy":
            self._add(stage="Outcome", tone="success", kind="done", title="No action required",
                      summary="Incident closed with no recovery action.", elapsed_s=elapsed_s)

    # ----------------------------------------------------------------- live
    def on_event(self, kind: str, d: dict) -> None:
        """Callback for AutoHealAgent(on_event=...). Observes only: it never returns data to the agent."""
        elapsed = round(time.time() - self._t0, 1)
        if kind == "route":
            if "findings" in d:
                self.detect(d["findings"], elapsed)
            self.route(d["model"], d["reason"], elapsed)
        elif kind == "escalate":
            self.escalation(d, elapsed)
        elif kind == "done":
            if d["status"] == "healthy":
                self.detect([], elapsed)
            self.done(d["status"], elapsed)
        else:
            self.step(kind, d.get("name"), d.get("args"), d.get("result"), d.get("model", ""), d.get("step"), elapsed)


def replay(findings: Iterable[Any], rows: Iterable[dict], routing: Iterable[dict] = (), llm_used: bool = True,
           status: str = "open", escalation: dict | None = None) -> list[ActivityEvent]:
    """Rebuild the feed from a stored trace. `rows` use the `agent_steps` shape; `args`/`result` aliases are accepted
    because the committed evaluation traces use those names."""
    log = ActivityLog(llm_used=llm_used)
    rows = list(rows)
    t0 = rows[0].get("ts") if rows else None
    routes = list(routing)
    if status == "healthy":
        log.detect([])
    else:
        log.detect(findings)
    for r in routes:
        if r.get("step", 0) == 0:
            log.route(r["model"], r["reason"])
    later = [r for r in routes if r.get("step", 0) != 0]

    def flush_routes(step: int) -> None:
        for r in [x for x in later if x["step"] == step]:
            log.route(r["model"], r["reason"])
            later.remove(r)

    for i, row in enumerate(rows):
        step = row.get("step")
        following = rows[i + 1].get("step") if i + 1 < len(rows) else None
        if row.get("kind") in ("focus", "decision"):  # the runtime upgrades the model just before these reminders
            flush_routes(step)
        if row.get("kind") != "escalate":  # the escalation record below carries the full reason
            ts = row.get("ts")
            log.step(row.get("kind", ""), row.get("name"), row.get("args_json", row.get("args")),
                     row.get("result_json", row.get("result")), row.get("model") or "", step,
                     round(ts - t0, 1) if ts and t0 else None)
        if following != step:
            flush_routes(step)
    if status == "escalated" and escalation:
        log.escalation(escalation)
    log.done(status)
    return log.events


def incident_events(incident: dict, steps: list[dict]) -> list[ActivityEvent]:
    """Feed for a stored incident row and its `agent_steps` rows."""
    report = parse_json(incident.get("report_json")) or {}
    findings = parse_json(incident.get("findings_json")) or []
    used = bool(report.get("prompt_tokens") or report.get("cost_usd"))
    return replay(findings, steps, report.get("routing", []), llm_used=used, status=incident.get("status", "open"),
                  escalation=report.get("escalation"))


# ------------------------------------------------------------------ lifecycle
@dataclass(frozen=True)
class Stage:
    name: str
    state: str
    detail: str = ""


def _execute_state(events: list[ActivityEvent]) -> tuple[Optional[str], str]:
    effects = [e.effect for e in events if e.effect]
    if not effects:
        return None, ""
    ran = effects.count(EXECUTED)
    stopped = effects.count(REJECTED) + effects.count(REFUSED)
    dry = effects.count(DRY_RUN)
    failed = effects.count(TOOL_FAILED)
    parts = [f"{ran} executed" if ran else "", f"{stopped} blocked" if stopped else "", f"{dry} dry run" if dry else "",
             f"{failed} failed" if failed else ""]
    detail = ", ".join(p for p in parts if p)
    if ran:
        return COMPLETED, detail
    if stopped or dry:
        return BLOCKED, detail
    return FAILED, detail


def derive_lifecycle(events: list[ActivityEvent], running: bool = False, symptoms_present: bool = False,
                     status: str | None = None) -> list[Stage]:
    """Stage states from observed events only. Stages can repeat (agents re-investigate after acting), so the stage of
    the most recent event is the active one while a run is in progress."""
    by_stage = {s: [e for e in events if e.stage == s] for s in STAGES}
    ended = status in ("resolved", "escalated", "healthy")
    seen = [STAGES.index(e.stage) for e in events]
    furthest = max(seen) if seen else -1
    out: dict[str, Stage] = {}

    for idx, name in enumerate(STAGES[:-1]):
        items = by_stage[name]
        if items:
            state = COMPLETED
        elif ended or idx < furthest:
            state = SKIPPED
        else:
            state = PENDING
        detail = ""
        if name == "Detect":
            if not items and symptoms_present:
                state = COMPLETED
            if items:
                detail = items[0].summary if items[0].tone != "success" else "No anomaly"
                if len(detail) > 60:
                    detail = items[0].title.replace("Anomaly detected: ", "")
        elif name == "Investigate" and items:
            calls = sum(1 for e in items if e.kind in ("observe", "error"))
            detail = _plural(calls, "tool call") if calls else ""
        elif name == "Execute":
            effect_state, detail = _execute_state(items)
            state = effect_state or (COMPLETED if items else state)
        elif name == "Verify" and items:
            state = {"success": COMPLETED, "danger": FAILED}.get(items[-1].tone, COMPLETED)
            detail = items[-1].summary
        out[name] = Stage(name, state, detail)

    if running and events:
        last = events[-1].stage
        if last in out and out[last].state == COMPLETED:
            out[last] = Stage(last, ACTIVE, out[last].detail)

    final = Stage("Resolve / Escalate", PENDING)
    if status == "resolved":
        final = Stage("Resolved", COMPLETED, "Verified recovery")
    elif status == "healthy":
        final = Stage("No action", COMPLETED, "No action required")
    elif status == "escalated":
        final = Stage("Escalated", ESCALATED, "Handed to a human")
    elif status == "open" and not running:
        final = Stage("Interrupted", FAILED, "Run did not finish")
    return [out[s] for s in STAGES[:-1]] + [final]


# -------------------------------------------------------------------- outcome
@dataclass(frozen=True)
class Outcome:
    key: str
    label: str
    tone: str  # success | info | warning | danger
    explanation: str


def _symptoms_reduced(initial: list[dict], remaining: list[dict]) -> bool:
    before = {f["check"]: f.get("value", 0) for f in initial}
    after = {f["check"]: f.get("value", 0) for f in remaining}
    if set(after) - set(before):
        return False
    return bool(set(before) - set(after)) or any(after[c] < before[c] for c in after)


def classify_outcome(report: dict, events: Iterable[ActivityEvent] = ()) -> Outcome:
    """Distinguishes the ways an incident can end. An escalation is not automatically a failure, and an attempted
    repair is not automatically a success: only passed verification counts as recovery."""
    status = report.get("status", "open")
    if status == "healthy":
        return Outcome("no_action", "No action required", "info",
                       "All health checks passed. No recovery action was taken and no LLM call was made.")
    if status == "resolved":
        return Outcome("recovered", "Fully recovered", "success",
                       "The recovery engine finished and independent verification passed: probe orders shipped and "
                       "every health check passes.")
    if status != "escalated":
        return Outcome("interrupted", "Interrupted", "danger",
                       "This run did not finish, so it has no verified result. Reset to start again.")

    e = report.get("escalation") or {}
    reason = e.get("reason", "")
    cause = e.get("cause")
    executed = report.get("executed_mutations", 0)
    verifications = report.get("verifications") or []
    last_failed = bool(verifications) and not verifications[-1].get("passed")
    reduced = executed > 0 and _symptoms_reduced(report.get("findings") or [], e.get("open_symptoms") or [])
    stopped = any(ev.effect in (REJECTED, REFUSED) for ev in events)

    if last_failed and cause == "guardrail":
        cleared = " Some symptoms were cleared, but not all." if reduced else ""
        return Outcome("verification_failed", "Verification failed", "danger",
                       f"Recovery actions ran, but independent verification did not pass.{cleared} {reason}")
    if reduced:
        return Outcome("partial", "Partially recovered and escalated", "warning",
                       f"Some damage was repaired and the rest was handed to a human. {reason}")
    if last_failed:
        return Outcome("verification_failed", "Verification failed", "danger",
                       f"Independent verification did not pass, and the incident was handed to a human. {reason}")
    if executed == 0 and (stopped or cause == "guardrail"):
        return Outcome("blocked", "Blocked for safety", "warning",
                       f"Safety controls prevented the requested actions, and nothing was changed. {reason}")
    if stopped:
        return Outcome("blocked", "Blocked for safety", "warning",
                       f"Some actions ran, but safety controls blocked the rest, so a human takes over. {reason}")
    if executed:
        return Outcome("attempted", "Escalated after an unsuccessful attempt", "warning",
                       f"Recovery actions ran without clearing the symptoms, so a human takes over. {reason}")
    return Outcome("escalated", "Escalated without automatic recovery", "warning",
                   f"No state-changing action was taken. {reason}")
