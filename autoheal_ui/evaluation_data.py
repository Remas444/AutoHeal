"""Read-only view over the two committed final result files. Every number is computed from those files; none is typed in."""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from autoheal.evaluate import EVAL_DIR
from autoheal.prospective import CASES

from .content import outcome_label

AGENT, PLAYBOOK = "openrouter", "playbook"
SYSTEM_NAMES = {AGENT: "AI Agent", PLAYBOOK: "Rule-based Playbook"}

SUITE_FILES = {"A": "v321-suiteA-regression.json", "B": "v321-suiteB-prospective.json"}


@dataclass(frozen=True)
class CaseRow:
    case: str
    title: str
    runs: int
    agent: int
    playbook: int
    agent_outcome: str
    playbook_outcome: str
    leaning: str  # favours agent | favours playbook | "" (pre-declared; Suite B only)
    declared_bias: str


@dataclass(frozen=True)
class Latency:
    mean_s: float
    min_s: float
    max_s: float


def wilson_interval(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval, the method the final report uses."""
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def _leaning(bias: str) -> str:
    text = bias.lower()
    if "favours the agent" in text or "adversarial to the playbook" in text:
        return "favours agent"
    if "favours the playbook" in text:
        return "favours playbook"
    return ""


def _join_outcomes(values: list[str]) -> str:
    return ", ".join(outcome_label(v) for v in sorted(set(values)))


class Suite:
    def __init__(self, key: str, report: dict):
        self.key, self.report = key, report
        self.results: list[dict] = report["results"]
        self.label: str = report.get("label", "")
        self.evidence_type: str = report.get("evidence_type", "")
        self.note: str = report.get("note", "")
        self.models: dict = report.get("models", {})

    def of(self, system: str) -> list[dict]:
        return [r for r in self.results if r["agent"] == system]

    def correct(self, system: str) -> tuple[int, int]:
        rs = self.of(system)
        return sum(bool(r["success"]) for r in rs), len(rs)

    def cases(self) -> list[CaseRow]:
        out = []
        for case in sorted({r["case"] for r in self.results}):
            agent = [r for r in self.of(AGENT) if r["case"] == case]
            playbook = [r for r in self.of(PLAYBOOK) if r["case"] == case]
            bias = getattr(CASES.get(case), "possible_bias", "") or ""
            out.append(CaseRow(case, agent[0]["title"], len(agent), sum(bool(r["success"]) for r in agent),
                               sum(bool(r["success"]) for r in playbook), _join_outcomes([r["outcome"] for r in agent]),
                               _join_outcomes([r["outcome"] for r in playbook]), _leaning(bias), bias))
        return out

    def discordant(self) -> tuple[list[dict], list[dict]]:
        """Runs paired by case and seed: (agent correct only, Playbook correct only)."""
        pb = {(r["case"], r["seed"]): r for r in self.of(PLAYBOOK)}
        agent_only, playbook_only = [], []
        for r in self.of(AGENT):
            other = pb[(r["case"], r["seed"])]
            if r["success"] and not other["success"]:
                agent_only.append(r)
            elif other["success"] and not r["success"]:
                playbook_only.append(other)
        return agent_only, playbook_only

    def mcnemar_p(self) -> float:
        """Exact two-sided McNemar test on the discordant pairs."""
        a, b = (len(x) for x in self.discordant())
        n = a + b
        if not n:
            return 1.0
        tail = sum(math.comb(n, i) for i in range(min(a, b) + 1)) * 0.5 ** n
        return min(1.0, 2 * tail)

    def detection(self, system: str) -> Optional[tuple[int, int]]:
        """(detected, fault runs). Suite A's records have no detection field."""
        faults = [r for r in self.of(system) if r.get("kind") not in (None, "control") and "detected" in r]
        return (sum(bool(r["detected"]) for r in faults), len(faults)) if faults else None

    def false_positives(self, system: str) -> Optional[tuple[int, int]]:
        controls = [r for r in self.of(system) if r.get("kind") == "control"]
        return (sum(bool(r.get("false_positive")) for r in controls), len(controls)) if controls else None

    def latency(self, system: str) -> Optional[Latency]:
        """Over runs where the system actually ran. For the agent, only runs that made LLM calls are comparable."""
        walls = [r["wall_s"] for r in self.of(system)
                 if (r.get("llm_called") if system == AGENT else r.get("status") != "healthy" and r.get("steps"))]
        return Latency(statistics.mean(walls), min(walls), max(walls)) if walls else None

    def cost(self, system: str) -> float:
        return sum(r.get("cost_usd") or 0.0 for r in self.of(system))

    def cost_per_correct(self, system: str) -> Optional[float]:
        k, _ = self.correct(system)
        return self.cost(system) / k if k else None

    def unsafe_executed(self, system: str) -> int:
        return sum(len(r.get("unsafe_executed", r.get("unsafe_actions")) or []) for r in self.of(system))

    def unsafe_attempted(self, system: str) -> Optional[int]:
        rs = self.of(system)
        return sum(len(r.get("unsafe_attempted") or []) for r in rs) if rs and "unsafe_attempted" in rs[0] else None

    def run_rows(self) -> list[dict]:
        return [{"Case": r["case"], "Seed": r["seed"], "System": SYSTEM_NAMES[r["agent"]],
                 "Outcome": outcome_label(r["outcome"]), "Correct": bool(r["success"]), "Steps": r["steps"],
                 "Tool calls": r["tool_calls"], "Actions executed": r["executed_mutations"], "Latency (s)": r["wall_s"],
                 "Cost (USD)": r["cost_usd"]} for r in self.results]

    def run_for(self, case: str, seed, system: str) -> dict:
        return next(r for r in self.results if r["case"] == case and r["seed"] == seed and r["agent"] == system)


def load_suite(key: str, directory: Path = EVAL_DIR) -> Optional[Suite]:
    path = Path(directory) / SUITE_FILES[key]
    if not path.exists():
        return None
    return Suite(key, json.loads(path.read_text(encoding="utf-8")))


def recorded_escalation(run: dict) -> dict:
    """The escalation fields a committed result row keeps, in the shape the activity feed expects."""
    return {"reason": run.get("escalation_reason", ""), "cause": run.get("escalation_cause") or None,
            "summary": run.get("escalation_summary", "")}
