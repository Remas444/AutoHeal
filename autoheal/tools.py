"""Diagnostic and recovery tools exposed to the agent.

Every tool has: a Pydantic argument model (validated before execution), a risk
level (none = read-only, low, medium, high) and a plain-Python implementation
that acts on the real SQLite state.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from .db import HIDDEN_TABLES, rows, scalar
from .detector import STALE_HEARTBEAT_TICKS, run_checks
from .scenarios import CATEGORIES
from .simulator import Shop

RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Which config keys the agent may change, and their safe ranges.
CONFIG_SPEC: dict[str, dict] = {
    "payment.gateway_timeout_ms": {"type": "int", "min": 1000, "max": 10000},
    "queue.max_depth": {"type": "int", "min": 50, "max": 1000},
    "queue.worker_batch": {"type": "int", "min": 5, "max": 100},
    "sync.mapping_version": {"type": "enum", "values": ["v1", "v2"]},
    "sync.batch_size": {"type": "int", "min": 1, "max": 60},
}


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    shop: Shop
    incident_id: int = 0


class ToolError(Exception):
    pass


# ============================================================ argument models
class NoArgs(BaseModel):
    pass


class SearchLogsArgs(BaseModel):
    service: Optional[str] = Field(None, description="payment-service | queue | sync-job | workflow-engine | config")
    level: Optional[Literal["INFO", "WARN", "ERROR"]] = None
    contains: Optional[str] = Field(None, description="case-insensitive substring")
    last_ticks: int = Field(15, ge=1, le=200)
    limit: int = Field(25, ge=1, le=60)


class MetricsArgs(BaseModel):
    names: Optional[list[str]] = Field(None, description="metric names; omit for all")
    last_ticks: int = Field(8, ge=1, le=50)


class ConfigHistoryArgs(BaseModel):
    key: Optional[str] = None
    limit: int = Field(15, ge=1, le=50)


class SqlArgs(BaseModel):
    query: str = Field(..., description="A single read-only SELECT statement.")


class ListWorkflowsArgs(BaseModel):
    status: Optional[Literal["waiting", "running", "done", "killed"]] = None
    limit: int = Field(15, ge=1, le=50)


class WaitArgs(BaseModel):
    ticks: int = Field(3, ge=1, le=5, description="ticks of normal traffic to let pass")


class RollbackArgs(BaseModel):
    key: str = Field(..., description="config key to revert to its value before the most recent change")


class SetConfigArgs(BaseModel):
    key: str
    value: str


class RetryPaymentsArgs(BaseModel):
    max_orders: int = Field(500, ge=1, le=2000)


class ReplayArgs(BaseModel):
    max_messages: int = Field(500, ge=1, le=2000)


class TerminateArgs(BaseModel):
    workflow_id: int
    reason: str


class EscalateArgs(BaseModel):
    reason: str = Field(..., description="why the agent cannot safely resolve this itself")
    summary: str
    suspected_categories: list[Literal[tuple(CATEGORIES)]] = Field(default_factory=list)  # type: ignore[valid-type]
    recommended_actions: list[str] = Field(default_factory=list)


class RootCause(BaseModel):
    category: Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]
    cause: str = Field(..., description="the specific root cause")
    evidence: list[str] = Field(..., description="concrete evidence: log lines, config changes, query results")
    fix_applied: str


class FinishArgs(BaseModel):
    root_causes: list[RootCause]
    summary: str
    confidence: float = Field(..., ge=0, le=1)


# ============================================================ implementations
def _now(ctx: ToolContext) -> int:
    return ctx.shop.tick_now


def get_health_report(ctx: ToolContext, a: NoArgs) -> dict:
    findings = [f.model_dump() for f in run_checks(ctx.conn)]
    last = {r["name"]: r["value"] for r in rows(ctx.conn, "SELECT name, value FROM metrics WHERE tick = ?",
                                                 (_now(ctx),))}
    return {"tick": _now(ctx), "healthy": not findings, "findings": findings, "latest_metrics": last}


def search_logs(ctx: ToolContext, a: SearchLogsArgs) -> dict:
    sql = "SELECT tick, service, level, message FROM logs WHERE tick > ?"
    params: list[Any] = [_now(ctx) - a.last_ticks]
    if a.service:
        sql += " AND service = ?"; params.append(a.service)
    if a.level:
        sql += " AND level = ?"; params.append(a.level)
    if a.contains:
        sql += " AND lower(message) LIKE ?"; params.append(f"%{a.contains.lower()}%")
    sql += " ORDER BY id DESC LIMIT ?"; params.append(a.limit)
    found = rows(ctx.conn, sql, tuple(params))
    return {"count": len(found), "logs": found}


def get_metrics(ctx: ToolContext, a: MetricsArgs) -> dict:
    sql = "SELECT tick, name, value FROM metrics WHERE tick > ?"
    params: list[Any] = [_now(ctx) - a.last_ticks]
    if a.names:
        sql += f" AND name IN ({','.join('?' * len(a.names))})"; params += a.names
    series: dict[str, list] = {}
    for r in rows(ctx.conn, sql + " ORDER BY tick", tuple(params)):
        series.setdefault(r["name"], []).append([r["tick"], r["value"]])
    return {"series": series}


def get_config(ctx: ToolContext, a: NoArgs) -> dict:
    return {"config": rows(ctx.conn, "SELECT key, value, updated_tick FROM config ORDER BY key"),
            "agent_settable": CONFIG_SPEC}


def get_config_history(ctx: ToolContext, a: ConfigHistoryArgs) -> dict:
    sql = "SELECT id, key, old_value, new_value, tick, actor, note FROM config_history"
    params: tuple = ()
    if a.key:
        sql += " WHERE key = ?"; params = (a.key,)
    return {"changes": rows(ctx.conn, sql + " ORDER BY id DESC LIMIT ?", params + (a.limit,))}


def run_readonly_sql(ctx: ToolContext, a: SqlArgs) -> dict:
    q = a.query.strip().rstrip(";")
    if not q.lower().startswith(("select", "with")) or ";" in q:
        raise ToolError("Only a single SELECT/WITH statement is allowed.")
    denied: list[str] = []

    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ:
            if arg1 in HIDDEN_TABLES:
                denied.append(arg1)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", -1)):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    ctx.conn.set_authorizer(authorizer)
    try:
        cur = ctx.conn.execute(q)
        result = [dict(r) for r in cur.fetchmany(50)]
    except sqlite3.DatabaseError as e:
        raise ToolError(f"SQL rejected: {e}" + (f" (restricted table: {denied[0]})" if denied else "")) from e
    finally:
        # Python 3.10 cannot pass None here (3.11+ can), so install an allow-all authorizer instead.
        ctx.conn.set_authorizer(None if sys.version_info >= (3, 11) else (lambda *a: sqlite3.SQLITE_OK))
    return {"row_count": len(result), "rows": result, "note": "max 50 rows returned"}


def list_workflows(ctx: ToolContext, a: ListWorkflowsArgs) -> dict:
    sql = "SELECT id, name, status, started_tick, heartbeat_tick, created_tick, holds_lock FROM workflows"
    params: tuple = ()
    if a.status:
        sql += " WHERE status = ?"; params = (a.status,)
    found = rows(ctx.conn, sql + " ORDER BY holds_lock DESC, id DESC LIMIT ?", params + (a.limit,))
    return {"now_tick": _now(ctx), "stale_after_ticks": STALE_HEARTBEAT_TICKS, "workflows": found}


# ---------------------------------------------------------------- mutating
def _validate_config_value(key: str, value: str) -> str:
    spec = CONFIG_SPEC.get(key)
    if spec is None:
        raise ToolError(f"Config key '{key}' is not agent-settable. Allowed: {list(CONFIG_SPEC)}")
    if spec["type"] == "int":
        try:
            v = int(value)
        except ValueError as e:
            raise ToolError(f"{key} must be an integer") from e
        if not spec["min"] <= v <= spec["max"]:
            raise ToolError(f"{key}={v} outside safe range [{spec['min']}, {spec['max']}]")
        return str(v)
    if value not in spec["values"]:
        raise ToolError(f"{key} must be one of {spec['values']}")
    return value


def rollback_config(ctx: ToolContext, a: RollbackArgs) -> dict:
    last = ctx.conn.execute("SELECT old_value, new_value, tick, actor FROM config_history WHERE key = ? "
                            "ORDER BY id DESC LIMIT 1", (a.key,)).fetchone()
    if last is None or last["old_value"] is None:
        raise ToolError(f"No previous value recorded for '{a.key}'.")
    target = _validate_config_value(a.key, last["old_value"])
    ctx.shop.set_config(a.key, target, actor="autoheal", note=f"rollback of change by {last['actor']} at tick {last['tick']}")
    return {"key": a.key, "from": last["new_value"], "to": target}


def set_config(ctx: ToolContext, a: SetConfigArgs) -> dict:
    value = _validate_config_value(a.key, a.value)
    old = ctx.shop.cfg(a.key)
    ctx.shop.set_config(a.key, value, actor="autoheal", note="set by recovery agent")
    return {"key": a.key, "from": old, "to": value}


def retry_stuck_payments(ctx: ToolContext, a: RetryPaymentsArgs) -> dict:
    shop, c = ctx.shop, ctx.conn
    # Payment safety (enforced here, not by prompt): an order that already has ANY payment record is never sent to
    # the gateway again. Eligibility is decided before LIMIT, so already-paid orders cannot starve eligible ones.
    stuck = rows(c, "SELECT o.id, o.amount FROM orders o WHERE o.status='created' AND NOT EXISTS "
                    "(SELECT 1 FROM payments p WHERE p.order_id = o.id) ORDER BY o.id LIMIT ?", (a.max_orders,))
    already = rows(c, "SELECT o.id AS order_id, p.id AS payment_id, p.status FROM orders o "
                      "JOIN payments p ON p.order_id = o.id WHERE o.status='created' ORDER BY o.id")
    if already and not stuck:
        raise ToolError(f"Refused: all {len(already)} order(s) stuck in 'created' already have a payment record "
                        f"(order_ids {[r['order_id'] for r in already][:20]}, payment_ids "
                        f"{[r['payment_id'] for r in already][:20]}, statuses {sorted({r['status'] for r in already})}). "
                        f"This tool never re-charges an order that has a payment record.")
    rng = random.Random(shop.seed * 7919 + shop.tick_now)
    res = {"attempted": len(stuck), "paid": 0, "still_timeout": 0, "failed": 0, "dead_lettered": 0}
    for o in stuck:
        outcome = shop.charge(o["id"], o["amount"], rng)
        if outcome == "ok":
            c.execute("INSERT OR IGNORE INTO payments (order_id, amount, status, tick) VALUES (?, ?, 'captured', ?)",
                      (o["id"], o["amount"], shop.tick_now))
            c.execute("UPDATE orders SET status='paid', updated_tick=? WHERE id=?", (shop.tick_now, o["id"]))
            res["paid"] += 1
            if not shop.enqueue(o["id"]):
                res["dead_lettered"] += 1
        elif outcome == "timeout":
            res["still_timeout"] += 1
        else:
            res["failed"] += 1
    shop.log("payment-service", "INFO", f"autoheal retry of stuck payments: {res}")
    c.commit()
    if already:
        res["skipped_existing_payment"] = {"count": len(already), "order_ids": [r["order_id"] for r in already][:20],
                                           "payment_ids": [r["payment_id"] for r in already][:20]}
        res["note"] = "Skipped orders already have a payment record; this tool never re-charges them."
    return res


def replay_dead_letter(ctx: ToolContext, a: ReplayArgs) -> dict:
    shop, c = ctx.shop, ctx.conn
    msgs = rows(c, "SELECT id, order_id FROM dead_letter WHERE replayed=0 ORDER BY id LIMIT ?", (a.max_messages,))
    replayed = rejected = 0
    for m in msgs:
        depth = scalar(c, "SELECT COUNT(*) FROM queue WHERE status='pending'")
        if depth >= int(shop.cfg("queue.max_depth")):
            rejected += 1
            continue
        c.execute("INSERT INTO queue (order_id, status, enqueued_tick) VALUES (?, 'pending', ?)",
                  (m["order_id"], shop.tick_now))
        c.execute("UPDATE dead_letter SET replayed=1 WHERE id=?", (m["id"],))
        replayed += 1
    shop.log("queue", "INFO", f"autoheal replayed {replayed} dead-letter message(s); {rejected} rejected (queue full)")
    c.commit()
    out = {"replayed": replayed, "rejected_queue_full": rejected,
           "remaining": scalar(c, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0")}
    if rejected:
        out["hint"] = ("Queue is full. Let the worker drain it (wait_and_observe; a higher queue.worker_batch "
                       "drains faster), then replay again.")
    return out


def wait_and_observe(ctx: ToolContext, a: WaitArgs) -> dict:
    """Let time pass so backlogs drain on their own, then report what changed."""
    c = ctx.conn
    before = {"queue_pending": scalar(c, "SELECT COUNT(*) FROM queue WHERE status='pending'"),
              "dead_letter_unreplayed": scalar(c, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0")}
    ctx.shop.run(a.ticks)
    after = {"queue_pending": scalar(c, "SELECT COUNT(*) FROM queue WHERE status='pending'"),
             "dead_letter_unreplayed": scalar(c, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0")}
    return {"ticks_waited": a.ticks, "now_tick": _now(ctx), "before": before, "after": after,
            "queue_max_depth": int(ctx.shop.cfg("queue.max_depth")),
            "findings": [f.model_dump() for f in run_checks(c)]}


def resync_customers(ctx: ToolContext, a: NoArgs) -> dict:
    n = ctx.shop._run_sync(full=True)
    ctx.conn.commit()
    mismatched = scalar(ctx.conn, "SELECT COUNT(*) FROM customers c JOIN crm_source s USING(customer_id) "
                                  "WHERE c.phone != s.phone OR c.email != s.email OR c.country != s.country")
    return {"records_resynced": n, "mapping_used": ctx.shop.cfg("sync.mapping_version"),
            "mismatched_after": mismatched}


def terminate_workflow(ctx: ToolContext, a: TerminateArgs) -> dict:
    wf = ctx.conn.execute("SELECT * FROM workflows WHERE id = ?", (a.workflow_id,)).fetchone()
    if wf is None:
        raise ToolError(f"workflow {a.workflow_id} not found")
    now = ctx.shop.tick_now
    if wf["status"] != "running" or (wf["heartbeat_tick"] or now) >= now - STALE_HEARTBEAT_TICKS:
        raise ToolError("Refused: only running workflows with a stale heartbeat "
                        f"(> {STALE_HEARTBEAT_TICKS} ticks) may be terminated.")
    ctx.conn.execute("UPDATE workflows SET status='killed', holds_lock=0, hung=0 WHERE id=?", (a.workflow_id,))
    ctx.shop.log("workflow-engine", "WARN", f"workflow #{a.workflow_id} terminated by autoheal: {a.reason}; lock released")
    ctx.conn.commit()
    return {"terminated": a.workflow_id, "lock_released": True}


def purge_dead_letter(ctx: ToolContext, a: NoArgs) -> dict:
    n = ctx.conn.execute("DELETE FROM dead_letter WHERE replayed=0").rowcount
    ctx.conn.commit()
    return {"deleted": n, "warning": "messages permanently lost"}


def escalate_to_human(ctx: ToolContext, a: EscalateArgs) -> dict:
    return {"escalated": True}


def finish_incident(ctx: ToolContext, a: FinishArgs) -> dict:
    return {"finished": True}


# ================================================================ registry
@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[[ToolContext, Any], dict]
    risk: str = "none"
    terminal: bool = False
    advances_time: bool = False  # changes state by letting simulated time pass (not a config/data mutation)

    @property
    def mutating(self) -> bool:
        return self.risk != "none" and not self.terminal


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("get_health_report", "Current invariant violations (symptoms) and latest metrics.", NoArgs, get_health_report),
    ToolSpec("search_logs", "Search recent logs (newest first). Filter by service, level, substring.", SearchLogsArgs, search_logs),
    ToolSpec("get_metrics", "Per-tick metric time series (orders_created, payments_ok, payment_timeouts, "
             "payment_errors, queue_depth, queue_dropped, dead_letter_unreplayed, shipments, sync_upserts, "
             "workflows_waiting, workflows_completed).", MetricsArgs, get_metrics),
    ToolSpec("get_config", "Current runtime configuration and which keys the agent may change.", NoArgs, get_config),
    ToolSpec("get_config_history", "Audit trail of config changes (who/when/old/new). Recent deploys are prime suspects.",
             ConfigHistoryArgs, get_config_history),
    ToolSpec("run_readonly_sql", "Run one read-only SELECT. Tables: orders, payments, queue, dead_letter, shipments, "
             "customers, crm_source, workflows, logs, metrics, config, config_history.", SqlArgs, run_readonly_sql),
    ToolSpec("list_workflows", "List workflows; lock holder first.", ListWorkflowsArgs, list_workflows),
    ToolSpec("wait_and_observe", "Let 1-5 ticks of normal traffic pass (the worker drains the queue), then report "
             "queue/dead-letter changes and current symptoms.", WaitArgs, wait_and_observe, advances_time=True),
    ToolSpec("rollback_config", "Revert a config key to its value before its most recent change.", RollbackArgs,
             rollback_config, risk="low"),
    ToolSpec("set_config", "Set an agent-settable config key to a validated value.", SetConfigArgs, set_config, risk="medium"),
    ToolSpec("retry_stuck_payments", "Re-attempt payment for orders stuck in 'created' that have no payment record "
             "(orders that already have one are never re-charged). Fix the cause first.",
             RetryPaymentsArgs, retry_stuck_payments, risk="medium"),
    ToolSpec("replay_dead_letter", "Re-enqueue dead-lettered messages (respects queue capacity).", ReplayArgs,
             replay_dead_letter, risk="low"),
    ToolSpec("resync_customers", "Full re-sync of customers from CRM using the CURRENT mapping version.", NoArgs,
             resync_customers, risk="low"),
    ToolSpec("terminate_workflow", "Kill a running workflow with a stale heartbeat and release its lock.",
             TerminateArgs, terminate_workflow, risk="medium"),
    ToolSpec("purge_dead_letter", "DESTRUCTIVE: delete all dead-letter messages permanently.", NoArgs,
             purge_dead_letter, risk="high"),
    ToolSpec("escalate_to_human", "Stop and hand off to a human with a clear summary. Use when the cause is outside "
             "your control, evidence is insufficient, or fixes failed.", EscalateArgs, escalate_to_human,
             risk="none", terminal=True),
    ToolSpec("finish_incident", "Declare root causes and fixes. The system will then be independently verified.",
             FinishArgs, finish_incident, risk="none", terminal=True),
]}


# ======================================================== schema for the LLM
def _inline_refs(schema: dict) -> dict:
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def openai_tool_schemas() -> list[dict]:
    out = []
    for t in TOOLS.values():
        params = _inline_refs(t.args.model_json_schema())
        params.setdefault("properties", {})
        params["type"] = "object"
        risk = "" if t.risk == "none" else f" [risk: {t.risk}]"
        out.append({"type": "function", "function": {"name": t.name, "description": t.description + risk,
                                                      "parameters": params}})
    return out


def execute(ctx: ToolContext, name: str, raw_args: dict) -> dict:
    """Validate args and run a tool. Raises ToolError/ValidationError on bad input."""
    spec = TOOLS[name]
    args = spec.args.model_validate(raw_args or {})
    return spec.fn(ctx, args)


def to_json(obj: Any, limit: int | None = None) -> str:
    s = json.dumps(obj, default=str, ensure_ascii=False)
    if limit and len(s) > limit:
        s = s[:limit] + f'... [truncated {len(s) - limit} chars]'
    return s
