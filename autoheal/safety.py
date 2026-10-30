"""Safety gate: every tool call passes through here before execution."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from pydantic import ValidationError

from .config import Settings
from .tools import RISK_ORDER, TOOLS


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    dry_run: bool = False
    approval_id: int | None = None


@dataclass
class SafetyGuard:
    settings: Settings
    conn: sqlite3.Connection
    incident_id: int
    mutations: int = 0  # allowed mutation attempts: what the mutation budget counts (unchanged)
    executed_mutations: int = 0  # mutations that actually ran and changed state
    unexecuted_mutation_attempts: int = 0  # allowed but rejected by the tool, failed, or dry-run
    blocks: int = 0
    attempts: dict = field(default_factory=dict)  # (tool, args) -> attempts that reached execution
    no_progress: dict = field(default_factory=dict)  # (tool, args) -> consecutive attempts without progress
    waited_ticks: int = 0

    def check(self, name: str, args: dict) -> Decision:
        spec = TOOLS.get(name)
        if spec is None:
            return self._block(f"Unknown tool '{name}'. Use only the provided tools.")

        # Progress-aware loop guard: an identical state-changing call may repeat only while its previous
        # attempts made measurable progress (e.g. successive payment batches); two non-progressing attempts
        # in a row (failed, dry-run, or no net reduction of outstanding work) block further repeats.
        key = self._key(name, args)
        repeated_without_progress = self.attempts.get(key, 0) >= 2 and self.no_progress.get(key, 0) >= 1
        if (spec.mutating or spec.advances_time) and repeated_without_progress:
            return self._block(f"Repeated identical action '{name}' without measurable progress. "
                               f"Change approach or escalate_to_human.")
        if spec.advances_time:
            ticks = self._ticks(spec, args)
            if self.waited_ticks + ticks > self.settings.max_wait_ticks:
                return self._block(f"Time budget exhausted: already waited {self.waited_ticks} of "
                                   f"{self.settings.max_wait_ticks} ticks this incident. Act or escalate_to_human.")
        if not spec.mutating:
            return Decision(True)

        if self.mutations >= self.settings.max_mutations:
            return self._block(f"Mutation budget exhausted ({self.settings.max_mutations}). Escalate to a human.")

        if RISK_ORDER[spec.risk] > RISK_ORDER[self.settings.max_auto_risk]:
            cur = self.conn.execute("INSERT INTO approvals (incident_id, tool, args_json, risk, status) "
                                    "VALUES (?, ?, ?, ?, 'pending')",
                                    (self.incident_id, name, json.dumps(args or {}), spec.risk))
            self.conn.commit()
            return self._block(f"'{name}' is {spec.risk}-risk and requires human approval "
                               f"(approval #{cur.lastrowid} filed). Do not wait; choose a safer action or escalate.",
                               approval_id=cur.lastrowid)

        self.mutations += 1
        return Decision(True, dry_run=self.settings.dry_run)

    def record(self, name: str, args: dict, executed: bool, progressed: bool) -> None:
        """Outcome of an allowed state-changing call. Failed and dry-run calls are recorded as not executed,
        so they can never count as progress."""
        spec = TOOLS[name]
        key = self._key(name, args)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if spec.mutating:
            if executed:
                self.executed_mutations += 1
            else:
                self.unexecuted_mutation_attempts += 1
        self.no_progress[key] = 0 if executed and progressed else self.no_progress.get(key, 0) + 1
        if executed and spec.advances_time:
            self.waited_ticks += self._ticks(spec, args)

    @staticmethod
    def _key(name: str, args: dict) -> tuple[str, str]:
        return name, json.dumps(args or {}, sort_keys=True)

    @staticmethod
    def _ticks(spec, args: dict) -> int:
        try:
            return int(getattr(spec.args.model_validate(args or {}), "ticks", 0))
        except ValidationError:
            return 0  # invalid arguments fail at execution and are recorded as not executed

    def _block(self, reason: str, approval_id: int | None = None) -> Decision:
        self.blocks += 1
        return Decision(False, reason=reason, approval_id=approval_id)

    @property
    def should_force_escalation(self) -> bool:
        return self.blocks >= self.settings.max_guard_blocks
