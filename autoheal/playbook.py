"""Deterministic rule-based runbook that speaks the same tool-calling protocol as an LLM.

Uses: (1) offline tests at $0, (2) demo fallback without an API key, (3) the evaluation
BASELINE the LLM agent is compared against. It knows a fixed list of symptom->action
rules and cannot reason about anything outside them.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from .db import rows, scalar
from .detector import run_checks

SYMPTOM_TO_REPAIR = {
    "orders_without_payment": ("retry_stuck_payments", {}, "payment"),
    "dead_letter_backlog": ("replay_dead_letter", {}, "queue"),
    "data_reconciliation": ("resync_customers", {}, "data_sync"),
}
KEY_TO_CATEGORY = {"payment": "payment", "queue": "queue", "sync": "data_sync"}


def _call(name: str, args: dict) -> dict:
    return {"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class PlaybookLLM:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.done: dict[str, int] = {}
        self.categories: set[str] = set()
        self.fixes: list[str] = []

    def chat(self, messages, tools, model, purpose="", incident_id=0) -> dict:
        calls = []
        # Rule 1: revert any non-bootstrap, non-autoheal config change that is still in effect.
        for ch in rows(self.conn, "SELECT key, actor, new_value FROM config_history h WHERE id = "
                                  "(SELECT MAX(id) FROM config_history WHERE key = h.key)"):
            if ch["actor"] not in ("bootstrap", "autoheal") and self.done.get(f"rb:{ch['key']}", 0) < 1:
                self.done[f"rb:{ch['key']}"] = 1
                self.categories.add(KEY_TO_CATEGORY.get(ch["key"].split(".")[0], "unknown"))
                self.fixes.append(f"rollback {ch['key']}")
                calls.append(_call("rollback_config", {"key": ch["key"]}))
        if calls:
            return {"role": "assistant", "content": None, "tool_calls": calls}

        # Rule 2: repair known symptoms (each at most twice).
        findings = run_checks(self.conn)
        for f in findings:
            if f.check in SYMPTOM_TO_REPAIR:
                name, args, cat = SYMPTOM_TO_REPAIR[f.check]
            elif f.check == "workflow_stall":
                now = scalar(self.conn, "SELECT tick FROM sim_clock")
                wf = self.conn.execute("SELECT id FROM workflows WHERE status='running' AND heartbeat_tick < ?",
                                       (now - 3,)).fetchone()
                if not wf:
                    continue
                name, args, cat = "terminate_workflow", {"workflow_id": wf[0], "reason": "stale heartbeat"}, "workflow"
            else:
                continue
            k = f"{name}:{json.dumps(args, sort_keys=True)}"
            if self.done.get(k, 0) < 3:
                self.done[k] = self.done.get(k, 0) + 1
                self.categories.add(cat)
                self.fixes.append(name)
                calls.append(_call(name, args))
        if calls:
            return {"role": "assistant", "content": None, "tool_calls": calls}

        # Rule 3: no new rule applies. If we acted, let independent verification judge
        # (it advances time so backlogs can drain); if we never could act, escalate.
        if self.fixes:
            rc = [{"category": c, "cause": "matched runbook rule", "evidence": ["rule-based match"],
                   "fix_applied": ", ".join(self.fixes)} for c in sorted(self.categories)]
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("finish_incident", {"root_causes": rc, "summary": "Runbook rules applied.", "confidence": 0.6})]}
        return {"role": "assistant", "content": None, "tool_calls": [_call("escalate_to_human", {
            "reason": "No runbook rule matches the remaining symptoms.",
            "summary": "; ".join(f"{f.check}: {f.detail}" for f in findings) or "unknown state",
            "suspected_categories": ["unknown"], "recommended_actions": ["Manual investigation required"]})]}
