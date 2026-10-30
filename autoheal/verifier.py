"""Independent verification: never trust the agent's claim that it fixed things.

1. Push synthetic probe orders through the real pipeline (payment -> queue -> shipment).
2. Re-run every invariant check.
Recovery is confirmed only if all probes ship AND no invariant is violated.
"""
from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from .db import scalar
from .detector import Finding, run_checks
from .simulator import Shop


class Verification(BaseModel):
    passed: bool
    probe_orders: int
    probe_fulfilled: int
    findings: list[Finding]
    ticks_run: int

    def summary(self) -> str:
        if self.passed:
            return f"PASSED: {self.probe_fulfilled}/{self.probe_orders} probe orders fulfilled; all invariants OK."
        issues = "; ".join(f"{f.check}: {f.detail}" for f in self.findings) or "none"
        return (f"FAILED: {self.probe_fulfilled}/{self.probe_orders} probe orders fulfilled. "
                f"Remaining violations: {issues}")


def verify(conn: sqlite3.Connection, shop: Shop, ticks: int = 2, probes_per_tick: int = 8,
           max_drain_ticks: int = 10) -> Verification:
    first_probe_id = int(scalar(conn, "SELECT COALESCE(MAX(id), 0) FROM orders") or 0)
    for _ in range(ticks):
        shop.tick(n_orders=probes_per_tick, probe=True)
    # Let a legitimately recovering backlog drain (no new traffic) before judging.
    for _ in range(max_drain_ticks):
        if not scalar(conn, "SELECT COUNT(*) FROM queue WHERE status='pending'"):
            break
        shop.tick(n_orders=0)
        ticks += 1
    probes = scalar(conn, "SELECT COUNT(*) FROM orders WHERE is_probe=1 AND id > ?", (first_probe_id,))
    fulfilled = scalar(conn, "SELECT COUNT(*) FROM orders WHERE is_probe=1 AND id > ? AND status='fulfilled'",
                       (first_probe_id,))
    findings = run_checks(conn)
    return Verification(passed=(not findings and probes == fulfilled), probe_orders=probes,
                        probe_fulfilled=fulfilled, findings=findings, ticks_run=ticks)
