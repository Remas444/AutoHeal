"""Per-browser-session state shared by the pages."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

import streamlit as st

from autoheal.config import Settings, get_settings
from autoheal.db import connect, init_usage, rows, scalar, usage_db_path
from autoheal.detector import Finding, run_checks
from autoheal.runtime import open_system
from autoheal.simulator import Shop

from . import recovery
from .activity import ActivityEvent, incident_events


@dataclass
class Console:
    settings: Settings
    conn: sqlite3.Connection
    shop: Shop
    usage: sqlite3.Connection
    session_tag: str

    @property
    def llm_configured(self) -> bool:
        return bool(self.settings.openrouter_api_key)

    @property
    def run_active(self) -> bool:
        return recovery.is_active(self.settings.db_path)


def console(risk: str = "medium", dry_run: bool = False) -> Console:
    """Connections last for the session; settings follow the current controls on every full run."""
    ss = st.session_state
    settings = get_settings(max_auto_risk=risk, dry_run=dry_run)
    if "console" not in ss:
        conn, shop = open_system(settings)
        usage = connect(usage_db_path(settings.db_path))
        init_usage(usage)
        ss.console = Console(settings, conn, shop, usage, f"dashboard-{uuid.uuid4().hex[:8]}")
    ss.console.settings = settings
    return ss.console


def current() -> Console:
    return st.session_state.console


def latest_incident(conn: sqlite3.Connection) -> dict | None:
    found = rows(conn, "SELECT * FROM incidents ORDER BY id DESC LIMIT 1")
    return found[0] if found else None


def incident_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in rows(conn, "SELECT id FROM incidents ORDER BY id DESC")]


def load_incident(conn: sqlite3.Connection, incident_id: int) -> tuple[dict, list[dict]]:
    incident = rows(conn, "SELECT * FROM incidents WHERE id=?", (incident_id,))[0]
    steps = rows(conn, "SELECT * FROM agent_steps WHERE incident_id=? ORDER BY id", (incident_id,))
    return incident, steps


def stored_events(conn: sqlite3.Connection, incident_id: int) -> tuple[dict, list[ActivityEvent]]:
    incident, steps = load_incident(conn, incident_id)
    return incident, incident_events(incident, steps)


def findings(conn: sqlite3.Connection) -> list[Finding]:
    return run_checks(conn)


def system_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {"stuck": scalar(conn, "SELECT COUNT(*) FROM orders WHERE status='created'") or 0,
            "dead_letters": scalar(conn, "SELECT COUNT(*) FROM dead_letter WHERE replayed=0") or 0,
            "waiting": scalar(conn, "SELECT COUNT(*) FROM workflows WHERE status='waiting'") or 0,
            "queued": scalar(conn, "SELECT COUNT(*) FROM queue WHERE status='pending'") or 0}


def session_usage(c: Console) -> dict[str, float]:
    """This browser session's LLM calls, separate from the machine-wide ledger total."""
    r = c.usage.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), COALESCE(SUM(prompt_tokens), 0), "
                        "COALESCE(SUM(completion_tokens), 0) FROM llm_usage WHERE run_tag LIKE ?",
                        (f"{c.session_tag}-%",)).fetchone()
    return {"calls": r[0], "cost": float(r[1]), "prompt_tokens": r[2], "completion_tokens": r[3]}


def ledger_total(c: Console) -> float:
    return float(scalar(c.usage, "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage") or 0)
