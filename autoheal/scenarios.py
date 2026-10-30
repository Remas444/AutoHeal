"""Fault-injection scenarios (the "chaos button") with hidden ground truth for evaluation."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from .db import scalar, wipe_system
from .detector import lifecycle_violations
from .simulator import DEFAULT_CONFIG, Shop

CATEGORIES = ["payment", "queue", "data_sync", "workflow", "external_dependency", "unknown"]


@dataclass
class Scenario:
    name: str
    title: str
    challenge_item: str
    inject: Callable[[Shop], None]
    expected_categories: set[str]
    fixable: bool = True
    spike: int | None = None  # traffic during manifestation
    description: str = ""
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------- injectors
def _payment_timeout(shop: Shop) -> None:
    shop.set_config("payment.gateway_timeout_ms", "500", actor="deploy-bot",
                    note="release r142: 'tighten upstream timeouts for faster checkout'")


def _queue_capacity(shop: Shop) -> None:
    shop.set_config("queue.max_depth", "10", actor="deploy-bot",
                    note="release r143: 'reduce memory footprint of fulfillment queue'")


def _sync_corruption(shop: Shop) -> None:
    shop.set_config("sync.mapping_version", "v2", actor="data-team",
                    note="migrate CRM field mapping to v2 schema")


def _workflow_hang(shop: Shop) -> None:
    t = shop.tick_now
    shop.conn.execute("INSERT INTO workflows (name, status, started_tick, heartbeat_tick, created_tick, holds_lock, hung) "
                      "VALUES ('nightly_reconcile', 'running', ?, ?, ?, 1, 1)", (t, t, t))
    shop.log("workflow-engine", "INFO", "workflow nightly_reconcile started; acquired lock 'billing'")
    shop.conn.commit()


def _gateway_outage(shop: Shop) -> None:
    shop.conn.execute("INSERT OR REPLACE INTO sim_faults (name, active) VALUES ('gateway_outage', 1)")
    shop.conn.commit()


def _combo(shop: Shop) -> None:
    _queue_capacity(shop)
    shop.run(2, n_orders=40)  # spike overflows the shrunken queue
    _payment_timeout(shop)    # ...then a second, unrelated deploy breaks payments


SCENARIOS: dict[str, Scenario] = {
    s.name: s for s in [
        Scenario("payment_timeout", "Payments silently drop orders", "C1 #1", _payment_timeout, {"payment"},
                 description="A deploy cut the gateway timeout below real latency; the timeout is swallowed, "
                             "orders stay 'created' with no payment and no error."),
        Scenario("queue_capacity", "Queue loses messages under load", "C1 #2", _queue_capacity, {"queue"},
                 spike=40, description="A deploy shrank the queue; under a traffic spike messages overflow."),
        Scenario("sync_corruption", "Sync job slowly corrupts data", "C1 #3", _sync_corruption, {"data_sync"},
                 description="A new field mapping truncates phone numbers, a few records per tick."),
        Scenario("workflow_hang", "Workflow hangs forever", "C1 #4", _workflow_hang, {"workflow"},
                 description="A workflow holds the billing lock and stopped heart-beating; everything queues behind it."),
        Scenario("gateway_outage", "Unfixable: upstream provider outage", "C1 #5", _gateway_outage,
                 {"external_dependency"}, fixable=False,
                 description="The payment provider returns 503. No safe internal fix exists; the agent must escalate."),
        Scenario("combo_payment_queue", "Two failures at once", "C1 multi", _combo, {"payment", "queue"},
                 spike=40, description="Payment timeout and queue shrink overlap during a spike."),
        Scenario("healthy", "Control: nothing is wrong", "control", lambda shop: None, set(),
                 description="No fault. The agent should spend $0 and take no action."),
    ]
}


def setup_scenario(conn: sqlite3.Connection, name: str, seed: int = 7,
                   warmup: int = 4, manifest: int = 5) -> Shop:
    """Fresh DB -> healthy warmup -> inject fault -> let symptoms manifest."""
    sc = SCENARIOS[name]
    wipe_system(conn)
    shop = Shop(conn, seed=seed)
    shop.bootstrap()
    shop.run(warmup)
    sc.inject(shop)
    shop.run(manifest, n_orders=sc.spike)
    return shop


# ------------------------------------------------- ground truth (eval only)
def ground_truth_healthy(conn: sqlite3.Connection) -> dict:
    """Independent check used ONLY by the evaluator - looks at hidden state the agent cannot see."""
    cfg = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM config")}
    checks = {
        "config_restored": all(cfg.get(k) == v for k, v in DEFAULT_CONFIG.items()
                               if k in ("payment.gateway_timeout_ms", "sync.mapping_version"))
                           and int(cfg.get("queue.max_depth", 0)) >= 100,
        "no_outage": not bool(scalar(conn, "SELECT active FROM sim_faults WHERE name='gateway_outage'") or 0),
        "no_stuck_orders": scalar(conn, "SELECT COUNT(*) FROM orders WHERE status='created'") == 0,
        "no_dead_letters": scalar(conn, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0") == 0,
        "data_consistent": scalar(conn, "SELECT COUNT(*) FROM customers c JOIN crm_source s USING(customer_id) "
                                        "WHERE c.phone != s.phone OR c.email != s.email") == 0,
        "no_hung_workflow": scalar(conn, "SELECT COUNT(*) FROM workflows WHERE hung=1 AND status='running'") == 0,
        "order_lifecycle_consistent": not any(lifecycle_violations(conn).values()),
    }
    checks["healthy"] = all(checks.values())
    return checks
