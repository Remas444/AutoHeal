"""SQLite storage.

Two databases:
  * system DB  (data/autoheal.db) - the simulated shop + incidents. Reset per scenario.
  * usage DB   (data/usage.db)    - LLM spend ledger. NEVER reset by scenarios, so the
                                    global budget cap survives resets and eval runs.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SYSTEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_clock (id INTEGER PRIMARY KEY CHECK (id = 1), tick INTEGER NOT NULL);

-- Hidden ground truth for the simulator. The agent's SQL tool is NOT allowed to read it.
CREATE TABLE IF NOT EXISTS sim_faults (name TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS config_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, old_value TEXT, new_value TEXT NOT NULL,
    tick INTEGER NOT NULL, actor TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, amount REAL NOT NULL,
    status TEXT NOT NULL, created_tick INTEGER NOT NULL, updated_tick INTEGER NOT NULL,
    is_probe INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL UNIQUE, amount REAL NOT NULL,
    status TEXT NOT NULL, tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, status TEXT NOT NULL,
    enqueued_tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dead_letter (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, reason TEXT NOT NULL,
    tick INTEGER NOT NULL, replayed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL UNIQUE, tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_source (
    customer_id INTEGER PRIMARY KEY, email TEXT NOT NULL, phone TEXT NOT NULL, country TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY, email TEXT NOT NULL, phone TEXT NOT NULL, country TEXT NOT NULL,
    synced_tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, status TEXT NOT NULL,
    started_tick INTEGER, heartbeat_tick INTEGER, created_tick INTEGER NOT NULL,
    holds_lock INTEGER NOT NULL DEFAULT 0, hung INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tick INTEGER NOT NULL, service TEXT NOT NULL,
    level TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    tick INTEGER NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY (tick, name)
);

-- Agent bookkeeping (also hidden from the agent's SQL tool)
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, opened_tick INTEGER NOT NULL, closed_tick INTEGER,
    status TEXT NOT NULL, findings_json TEXT, report_json TEXT, report_md TEXT
);
CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL, step INTEGER NOT NULL,
    kind TEXT NOT NULL, name TEXT, args_json TEXT, result_json TEXT, model TEXT, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL, tool TEXT NOT NULL,
    args_json TEXT NOT NULL, risk TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT
);
"""

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, run_tag TEXT, incident_id INTEGER,
    model TEXT NOT NULL, purpose TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
    cost_usd REAL NOT NULL, cost_source TEXT, latency_ms INTEGER, ok INTEGER NOT NULL
);
"""

# Tables the agent must never read through its SQL tool (ground truth / its own bookkeeping).
HIDDEN_TABLES = {"sim_faults", "incidents", "agent_steps", "approvals"}


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_system(conn: sqlite3.Connection) -> None:
    conn.executescript(SYSTEM_SCHEMA)
    conn.commit()


def init_usage(conn: sqlite3.Connection) -> None:
    conn.executescript(USAGE_SCHEMA)
    conn.commit()


def wipe_system(conn: sqlite3.Connection) -> None:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    init_system(conn)


def usage_db_path(system_db_path: str) -> str:
    if system_db_path == ":memory:":
        return ":memory:"
    return str(Path(system_db_path).with_name("usage.db"))


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    r = conn.execute(sql, params).fetchone()
    return None if r is None else r[0]
