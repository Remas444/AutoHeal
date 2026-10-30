"""Wiring helpers shared by the CLI, the evaluator and the dashboard."""
from __future__ import annotations

import sqlite3
import time
import uuid

from .agent import AutoHealAgent, IncidentResult
from .config import Settings
from .db import connect, init_system, usage_db_path
from .llm import OpenRouterClient, UsageLedger
from .playbook import PlaybookLLM
from .scenarios import setup_scenario
from .simulator import Shop


def build_llm(kind: str, conn: sqlite3.Connection, settings: Settings, ledger: UsageLedger):
    if kind == "playbook":
        return PlaybookLLM(conn)
    if kind == "openrouter":
        return OpenRouterClient(settings, ledger)
    raise ValueError(f"unknown llm kind: {kind}")


def open_system(settings: Settings) -> tuple[sqlite3.Connection, Shop]:
    conn = connect(settings.db_path)
    init_system(conn)
    shop = Shop(conn, seed=settings.seed)
    if not conn.execute("SELECT 1 FROM sim_clock").fetchone():
        shop.bootstrap()
    return conn, shop


def new_run_tag(prefix: str) -> str:
    """Unique ledger tag per process/session. The system DB resets incident ids on every scenario,
    so a fixed tag would make incident_total() (and the per-incident cap) sum across old runs."""
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def open_ledger(settings: Settings, run_tag: str = "") -> UsageLedger:
    return UsageLedger(usage_db_path(settings.db_path), run_tag=run_tag)


def run_scenario(name: str, llm_kind: str, settings: Settings, conn: sqlite3.Connection | None = None,
                 ledger: UsageLedger | None = None, on_event=None) -> tuple[IncidentResult, sqlite3.Connection]:
    conn = conn or connect(settings.db_path)
    ledger = ledger or open_ledger(settings)
    shop = setup_scenario(conn, name, seed=settings.seed)
    agent = AutoHealAgent(conn, shop, build_llm(llm_kind, conn, settings, ledger), settings, ledger, on_event)
    return agent.run(), conn
