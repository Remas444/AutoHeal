"""Deterministic health checks (invariants). Zero LLM cost.

The LLM is only invoked when at least one invariant is violated. These checks
describe SYMPTOMS, never root causes - diagnosis is the agent's job.
"""
from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from .db import scalar

STALE_HEARTBEAT_TICKS = 3
QUEUE_AGE_TICKS = 5  # oldest pending message at least this old ...
QUEUE_GROWTH_TICKS = 5  # ... AND the queue depth grew on each of the last this-many ticks


class Finding(BaseModel):
    check: str
    severity: str  # warning | critical
    value: float
    detail: str


# Order lifecycle reconciliation: payment, queue, dead letter and shipment must agree.
# A paid order is legitimately "in flight" while it has a pending queue row, or already reported
# (by dead_letter_backlog) while it has an unreplayed dead-letter row; neither counts as lost.
LIFECYCLE_SQL = {
    "lost": "SELECT COUNT(*) FROM orders o WHERE o.status='paid' "
            "AND NOT EXISTS (SELECT 1 FROM queue q WHERE q.order_id=o.id AND q.status='pending') "
            "AND NOT EXISTS (SELECT 1 FROM dead_letter d WHERE d.order_id=o.id AND d.replayed=0)",
    "progress_without_payment": "SELECT COUNT(*) FROM orders o WHERE o.status IN ('paid', 'fulfilled') "
                                "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id=o.id "
                                "AND p.status='captured')",
    "payment_without_progress": "SELECT COUNT(*) FROM payments p JOIN orders o ON o.id=p.order_id "
                                "WHERE p.status='captured' AND o.status IN ('created', 'payment_failed')",
    "fulfilled_without_shipment": "SELECT COUNT(*) FROM orders o WHERE o.status='fulfilled' "
                                  "AND NOT EXISTS (SELECT 1 FROM shipments s WHERE s.order_id=o.id)",
}


def lifecycle_violations(conn: sqlite3.Connection) -> dict[str, int]:
    return {k: int(scalar(conn, sql) or 0) for k, sql in LIFECYCLE_SQL.items()}


def run_checks(conn: sqlite3.Connection, include_probes: bool = True) -> list[Finding]:
    now = int(scalar(conn, "SELECT tick FROM sim_clock WHERE id=1") or 0)
    out: list[Finding] = []

    stuck = scalar(conn, "SELECT COUNT(*) FROM orders WHERE status='created' AND created_tick <= ?", (now,))
    if stuck:
        with_payment = scalar(conn, "SELECT COUNT(*) FROM orders o WHERE o.status='created' AND o.created_tick <= ? "
                                    "AND EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)", (now,))
        detail = (f"{stuck} order(s) still in status 'created' with no payment record (payment step never completed)."
                  if not with_payment else
                  f"{stuck} order(s) still in status 'created': {stuck - with_payment} with no payment record, "
                  f"{with_payment} that already have a payment record.")
        out.append(Finding(check="orders_without_payment", severity="critical", value=stuck, detail=detail))

    failed = scalar(conn, "SELECT COUNT(*) FROM orders WHERE status='payment_failed' AND updated_tick > ?",
                    (now - 5,))
    total_recent = scalar(conn, "SELECT COUNT(*) FROM orders WHERE created_tick > ?", (now - 5,)) or 0
    if failed and total_recent and failed / total_recent > 0.05:
        out.append(Finding(check="payment_failure_rate", severity="critical", value=round(failed / total_recent, 3),
                           detail=f"{failed}/{total_recent} recent orders ended in 'payment_failed'."))

    dlq = scalar(conn, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0")
    if dlq:
        out.append(Finding(check="dead_letter_backlog", severity="critical", value=dlq,
                           detail=f"{dlq} paid order message(s) in dead_letter; these orders will never ship."))

    # Silent queue degradation: old backlog AND sustained growth. Healthy traffic keeps age 0; legitimate bursts
    # (measured up to 45 orders/tick for 8 ticks) peak at age 3; a draining queue is never "growing".
    oldest = scalar(conn, "SELECT MIN(enqueued_tick) FROM queue WHERE status='pending'")
    age = 0 if oldest is None else now - int(oldest)
    depth = [r[0] for r in conn.execute("SELECT value FROM metrics WHERE name='queue_depth' ORDER BY tick DESC "
                                        "LIMIT ?", (QUEUE_GROWTH_TICKS,))][::-1]
    if age >= QUEUE_AGE_TICKS and len(depth) == QUEUE_GROWTH_TICKS and all(b > a for a, b in zip(depth, depth[1:])):
        out.append(Finding(check="queue_backlog_age", severity="warning", value=age,
                           detail=f"Oldest queued message has waited {age} ticks and the queue grew for "
                                  f"{QUEUE_GROWTH_TICKS} consecutive ticks ({', '.join(str(int(d)) for d in depth)}): "
                                  f"fulfilment is falling behind."))

    lc = lifecycle_violations(conn)
    if lc["lost"]:
        out.append(Finding(check="orders_lost", severity="critical", value=lc["lost"],
                           detail=f"{lc['lost']} paid order(s) have no pending queue message and no dead-letter "
                                  f"entry; they will never ship and nothing will retry them."))
    if lc["progress_without_payment"]:
        out.append(Finding(check="progress_without_payment", severity="critical", value=lc["progress_without_payment"],
                           detail=f"{lc['progress_without_payment']} paid/fulfilled order(s) have no captured "
                                  f"payment (goods without money)."))
    if lc["payment_without_progress"]:
        out.append(Finding(check="payment_without_progress", severity="critical", value=lc["payment_without_progress"],
                           detail=f"{lc['payment_without_progress']} captured payment(s) belong to orders still "
                                  f"'created' or 'payment_failed' (money without order progress)."))
    if lc["fulfilled_without_shipment"]:
        out.append(Finding(check="fulfilled_without_shipment", severity="critical",
                           value=lc["fulfilled_without_shipment"],
                           detail=f"{lc['fulfilled_without_shipment']} order(s) marked 'fulfilled' have no "
                                  f"shipment record."))

    drift = scalar(conn, "SELECT COUNT(*) FROM customers c JOIN crm_source s USING(customer_id) "
                         "WHERE c.phone != s.phone OR c.email != s.email OR c.country != s.country")
    if drift:
        out.append(Finding(check="data_reconciliation", severity="warning", value=drift,
                           detail=f"{drift} customer record(s) differ from the CRM source of truth."))

    stale = scalar(conn, "SELECT COUNT(*) FROM workflows WHERE status='running' AND heartbeat_tick < ?",
                   (now - STALE_HEARTBEAT_TICKS,))
    waiting = scalar(conn, "SELECT COUNT(*) FROM workflows WHERE status='waiting'")
    if stale or waiting > 3:
        out.append(Finding(check="workflow_stall", severity="critical", value=waiting,
                           detail=f"{stale} running workflow(s) with stale heartbeat (> {STALE_HEARTBEAT_TICKS} ticks); "
                                  f"{waiting} workflow(s) waiting."))
    return out


def outstanding_work(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts of unresolved work behind the invariants (no LLM). `queue_pending` is in-flight, not a violation."""
    now = int(scalar(conn, "SELECT tick FROM sim_clock WHERE id=1") or 0)
    return {
        "unpaid_orders": int(scalar(conn, "SELECT COUNT(*) FROM orders WHERE status='created'") or 0),
        "dead_letters": int(scalar(conn, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0") or 0),
        "data_drift": int(scalar(conn, "SELECT COUNT(*) FROM customers c JOIN crm_source s USING(customer_id) "
                                       "WHERE c.phone != s.phone OR c.email != s.email OR c.country != s.country") or 0),
        "stalled_workflows": int(scalar(conn, "SELECT COUNT(*) FROM workflows WHERE status='waiting' OR "
                                              "(status='running' AND heartbeat_tick < ?)",
                                        (now - STALE_HEARTBEAT_TICKS,)) or 0),
        "lifecycle_violations": sum(lifecycle_violations(conn).values()),
        "queue_pending": int(scalar(conn, "SELECT COUNT(*) FROM queue WHERE status='pending'") or 0),
    }


def made_progress(before: dict[str, int], after: dict[str, int]) -> bool:
    """Net reduction of unresolved work. Moving work from one backlog to another (e.g. unpaid orders into dead
    letters) is not progress. Draining in-flight queue messages counts only while violations remain and none grew."""
    kinds = [k for k in before if k != "queue_pending"]
    total_before, total_after = sum(before[k] for k in kinds), sum(after[k] for k in kinds)
    if total_after < total_before:
        return True
    return (total_before > 0 and all(after[k] <= before[k] for k in kinds)
            and after["queue_pending"] < before["queue_pending"])


def is_healthy(conn: sqlite3.Connection) -> bool:
    return not run_checks(conn)
