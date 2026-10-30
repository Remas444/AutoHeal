"""A small but realistic e-commerce backend, simulated in discrete ticks.

Pipeline per tick:
  orders -> payment gateway -> fulfillment queue -> worker -> shipments
  + CRM->customers sync job + billing workflow scheduler (single lock)

Every failure mode is caused by something with evidence the agent can find:
a bad config deploy (visible in config_history), a hung workflow (visible in
workflows), or an external outage (visible only as HTTP 503s in logs).
"""
from __future__ import annotations

import random
import sqlite3

from .db import init_system, rows, scalar

DEFAULT_CONFIG = {
    "payment.gateway_timeout_ms": "3000",
    "queue.max_depth": "200",
    "queue.worker_batch": "25",
    "sync.mapping_version": "v1",
    "sync.batch_size": "8",
    "workflow.lock_name": "billing",
}
N_CUSTOMERS = 60
NORMAL_TRAFFIC = 12


class Shop:
    def __init__(self, conn: sqlite3.Connection, seed: int = 7):
        self.conn = conn
        self.seed = seed

    # ------------------------------------------------------------------ setup
    def bootstrap(self) -> None:
        """Create a fresh, healthy system state."""
        init_system(self.conn)
        c = self.conn
        c.execute("INSERT OR REPLACE INTO sim_clock (id, tick) VALUES (1, 0)")
        for k, v in DEFAULT_CONFIG.items():
            c.execute("INSERT OR REPLACE INTO config (key, value, updated_tick) VALUES (?, ?, 0)", (k, v))
            c.execute("INSERT INTO config_history (key, old_value, new_value, tick, actor, note) "
                      "VALUES (?, NULL, ?, 0, 'bootstrap', 'initial value')", (k, v))
        rng = random.Random(self.seed)
        countries = ["SA", "AE", "EG", "JO", "KW"]
        for cid in range(1, N_CUSTOMERS + 1):
            email = f"customer{cid}@example.com"
            phone = "+9665" + "".join(str(rng.randint(0, 9)) for _ in range(8))
            country = rng.choice(countries)
            c.execute("INSERT INTO crm_source VALUES (?, ?, ?, ?)", (cid, email, phone, country))
            c.execute("INSERT INTO customers VALUES (?, ?, ?, ?, 0)", (cid, email, phone, country))
        c.commit()

    # ---------------------------------------------------------------- helpers
    @property
    def tick_now(self) -> int:
        return int(scalar(self.conn, "SELECT tick FROM sim_clock WHERE id = 1") or 0)

    def cfg(self, key: str) -> str:
        return scalar(self.conn, "SELECT value FROM config WHERE key = ?", (key,))

    def fault(self, name: str) -> bool:
        return bool(scalar(self.conn, "SELECT active FROM sim_faults WHERE name = ?", (name,)) or 0)

    def log(self, service: str, level: str, message: str) -> None:
        self.conn.execute("INSERT INTO logs (tick, service, level, message) VALUES (?, ?, ?, ?)",
                          (self.tick_now, service, level, message))

    def metric(self, name: str, value: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO metrics (tick, name, value) VALUES (?, ?, ?)",
                          (self.tick_now, name, float(value)))

    def set_config(self, key: str, value: str, actor: str, note: str = "") -> None:
        old = self.cfg(key)
        self.conn.execute("UPDATE config SET value = ?, updated_tick = ? WHERE key = ?",
                          (value, self.tick_now, key))
        self.conn.execute("INSERT INTO config_history (key, old_value, new_value, tick, actor, note) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (key, old, value, self.tick_now, actor, note))
        self.log("config", "INFO", f"config '{key}' changed {old!r} -> {value!r} by {actor}. {note}".strip())
        self.conn.commit()

    # ------------------------------------------------------------------- tick
    def tick(self, n_orders: int | None = None, probe: bool = False) -> dict:
        c = self.conn
        c.execute("UPDATE sim_clock SET tick = tick + 1 WHERE id = 1")
        t = self.tick_now
        rng = random.Random(self.seed * 100_003 + t)
        n = NORMAL_TRAFFIC if n_orders is None else n_orders

        # 1) new orders
        new_ids = []
        for _ in range(n):
            cur = c.execute("INSERT INTO orders (customer_id, amount, status, created_tick, updated_tick, is_probe) "
                            "VALUES (?, ?, 'created', ?, ?, ?)",
                            (rng.randint(1, N_CUSTOMERS), round(rng.uniform(20, 900), 2), t, t, int(probe)))
            new_ids.append(cur.lastrowid)

        # 2) payments
        stats = self._process_payments(new_ids, rng)
        # 3) worker drains queue, 4) sync job, 5) workflows
        shipped = self._run_worker()
        upserts = self._run_sync()
        wf = self._run_workflows()

        self.metric("orders_created", n)
        self.metric("payments_ok", stats["ok"])
        self.metric("payment_timeouts", stats["timeouts"])
        self.metric("payment_errors", stats["errors"])
        self.metric("queue_dropped", stats["dropped"])
        self.metric("queue_depth", scalar(c, "SELECT COUNT(*) FROM queue WHERE status='pending'"))
        self.metric("dead_letter_unreplayed", scalar(c, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0"))
        self.metric("shipments", shipped)
        self.metric("sync_upserts", upserts)
        self.metric("workflows_waiting", wf["waiting"])
        self.metric("workflows_completed", wf["completed"])
        c.commit()
        return {"tick": t, "orders": n, **stats, "shipped": shipped, **wf}

    def run(self, ticks: int, n_orders: int | None = None) -> None:
        for _ in range(ticks):
            self.tick(n_orders)

    # ---------------------------------------------------------------- stages
    def charge(self, order_id: int, amount: float, rng: random.Random) -> str:
        """Call the (simulated) payment gateway. Returns ok | timeout | error."""
        if self.fault("gateway_outage"):
            return "error"
        latency = rng.randint(600, 1400)
        timeout = int(self.cfg("payment.gateway_timeout_ms"))
        return "timeout" if latency > timeout else "ok"

    def _process_payments(self, order_ids: list[int], rng: random.Random) -> dict:
        c, t = self.conn, self.tick_now
        ok = timeouts = errors = dropped = 0
        for oid in order_ids:
            amount = scalar(c, "SELECT amount FROM orders WHERE id = ?", (oid,))
            result = self.charge(oid, amount, rng)
            if result == "ok":
                c.execute("INSERT INTO payments (order_id, amount, status, tick) VALUES (?, ?, 'captured', ?)",
                          (oid, amount, t))
                c.execute("UPDATE orders SET status='paid', updated_tick=? WHERE id=?", (t, oid))
                ok += 1
                dropped += 0 if self.enqueue(oid) else 1
            elif result == "timeout":
                # BUG (intentional): the timeout exception is swallowed. The order stays in
                # 'created' forever, no payment row, no error log -> "silently dropped order".
                timeouts += 1
            else:
                c.execute("UPDATE orders SET status='payment_failed', updated_tick=? WHERE id=?", (t, oid))
                errors += 1
        if timeouts:
            self.log("payment-service", "WARN",
                     f"{timeouts} gateway call(s) exceeded timeout "
                     f"({self.cfg('payment.gateway_timeout_ms')}ms; typical gateway latency 600-1400ms). "
                     f"Exception handled; continuing.")
        if errors:
            self.log("payment-service", "ERROR",
                     f"{errors} charge(s) failed: gateway responded HTTP 503 Service Unavailable "
                     f"(provider=PayFast, upstream)")
        if dropped:
            self.log("queue", "WARN", f"enqueue rejected {dropped} message(s): queue at capacity "
                                      f"(max_depth={self.cfg('queue.max_depth')}); moved to dead_letter")
        return {"ok": ok, "timeouts": timeouts, "errors": errors, "dropped": dropped}

    def enqueue(self, order_id: int) -> bool:
        c, t = self.conn, self.tick_now
        depth = scalar(c, "SELECT COUNT(*) FROM queue WHERE status='pending'")
        if depth >= int(self.cfg("queue.max_depth")):
            c.execute("INSERT INTO dead_letter (order_id, reason, tick) VALUES (?, 'queue_full', ?)", (order_id, t))
            return False
        c.execute("INSERT INTO queue (order_id, status, enqueued_tick) VALUES (?, 'pending', ?)", (order_id, t))
        return True

    def _run_worker(self) -> int:
        c, t = self.conn, self.tick_now
        batch = int(self.cfg("queue.worker_batch"))
        msgs = rows(c, "SELECT id, order_id FROM queue WHERE status='pending' ORDER BY id LIMIT ?", (batch,))
        for m in msgs:
            c.execute("INSERT OR IGNORE INTO shipments (order_id, tick) VALUES (?, ?)", (m["order_id"], t))
            c.execute("UPDATE orders SET status='fulfilled', updated_tick=? WHERE id=?", (t, m["order_id"]))
            c.execute("UPDATE queue SET status='done' WHERE id=?", (m["id"],))
        return len(msgs)

    def map_record(self, src: dict, version: str) -> tuple[str, str, str]:
        if version == "v2":
            # BUG (intentional): new mapping truncates phone numbers to 8 chars.
            return src["email"], src["phone"][:8], src["country"]
        return src["email"], src["phone"], src["country"]

    def _run_sync(self, full: bool = False) -> int:
        c, t = self.conn, self.tick_now
        version = self.cfg("sync.mapping_version")
        if full:
            batch = rows(c, "SELECT * FROM crm_source ORDER BY customer_id")
        else:
            size = int(self.cfg("sync.batch_size"))
            start = (t * size) % N_CUSTOMERS
            ids = [((start + i) % N_CUSTOMERS) + 1 for i in range(size)]
            batch = rows(c, f"SELECT * FROM crm_source WHERE customer_id IN ({','.join('?' * len(ids))})", tuple(ids))
        for src in batch:
            email, phone, country = self.map_record(src, version)
            c.execute("INSERT OR REPLACE INTO customers VALUES (?, ?, ?, ?, ?)",
                      (src["customer_id"], email, phone, country, t))
        self.log("sync-job", "INFO", f"upserted {len(batch)} customer record(s) (mapping={version})")
        return len(batch)

    def _run_workflows(self) -> dict:
        c, t = self.conn, self.tick_now
        c.execute("INSERT INTO workflows (name, status, created_tick) VALUES ('invoice_batch', 'waiting', ?)", (t,))
        # healthy running workflows heartbeat; hung ones do not
        c.execute("UPDATE workflows SET heartbeat_tick=? WHERE status='running' AND hung=0", (t,))
        holder = c.execute("SELECT id, name, heartbeat_tick FROM workflows WHERE holds_lock=1").fetchone()
        completed = 0
        if holder is None:
            # short jobs: each acquires the lock, runs, completes and releases (up to 10 per tick)
            for nxt in rows(c, "SELECT id FROM workflows WHERE status='waiting' ORDER BY id LIMIT 10"):
                c.execute("UPDATE workflows SET status='done', started_tick=?, heartbeat_tick=?, holds_lock=0 "
                          "WHERE id=?", (t, t, nxt["id"]))
                completed += 1
        waiting = scalar(c, "SELECT COUNT(*) FROM workflows WHERE status='waiting'")
        if holder is not None and waiting:
            self.log("workflow-engine", "WARN",
                     f"{waiting} workflow(s) waiting for lock '{self.cfg('workflow.lock_name')}' held by "
                     f"workflow #{holder['id']} ({holder['name']}), last heartbeat at tick {holder['heartbeat_tick']}")
        return {"waiting": waiting, "completed": completed}
