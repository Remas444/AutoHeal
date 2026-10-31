"""Runs one incident recovery on a background thread so the console can show it as it happens.

The agent is constructed exactly as `autoheal.runtime.run_scenario` constructs it. The only addition is an `on_event`
observer, a parameter the agent already exposes. There is no Streamlit in here, and a browser refresh or page change
cannot interrupt a run mid-way, which would otherwise leave an incident half-recovered.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from autoheal.agent import AutoHealAgent, IncidentResult
from autoheal.config import Settings
from autoheal.runtime import build_llm, new_run_tag, open_ledger, open_system

from .activity import ActivityLog


@dataclass
class RecoveryRun:
    engine: str
    log: ActivityLog
    thread: Optional[threading.Thread] = None
    result: Optional[IncidentResult] = None
    error: Optional[str] = None
    finished: threading.Event = field(default_factory=threading.Event)

    @property
    def active(self) -> bool:
        return not self.finished.is_set()


# One simulated system per database, so at most one recovery may touch it at a time (this also covers a second tab).
_runs: dict[str, RecoveryRun] = {}
_lock = threading.Lock()


def current(db_path: str) -> Optional[RecoveryRun]:
    return _runs.get(str(db_path))


def is_active(db_path: str) -> bool:
    run = current(db_path)
    return bool(run and run.active)


def start(settings: Settings, engine: str, tag_prefix: str) -> RecoveryRun:
    """Start a recovery. Raises RuntimeError if one is already running against this database."""
    key = str(settings.db_path)
    with _lock:
        if is_active(key):
            raise RuntimeError("A recovery is already running.")
        run = RecoveryRun(engine=engine, log=ActivityLog(llm_used=engine != "playbook"))
        run.thread = threading.Thread(target=_work, args=(run, settings, tag_prefix), daemon=True, name="autoheal-recovery")
        _runs[key] = run
    run.thread.start()
    return run


def _work(run: RecoveryRun, settings: Settings, tag_prefix: str) -> None:
    try:
        # A connection of its own: SQLite connections are not shared between this thread and the UI's reruns.
        conn, shop = open_system(settings)
        # A tag per run keeps the per-incident cap separate, because incident ids restart on every scenario.
        ledger = open_ledger(settings, run_tag=new_run_tag(tag_prefix))
        llm = build_llm(run.engine, conn, settings, ledger)
        run.result = AutoHealAgent(conn, shop, llm, settings, ledger, on_event=run.log.on_event).run()
    except Exception as e:  # shown to the operator; the incident row stays 'open' and reads as interrupted
        run.error = str(e)[:500]
    finally:
        run.finished.set()
