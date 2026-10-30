"""Command line interface.

  python -m autoheal.cli demo payment_timeout --llm playbook
  python -m autoheal.cli demo combo_payment_queue --llm openrouter
  python -m autoheal.cli eval --agents playbook openrouter --runs 1
  python -m autoheal.cli models            # verify model IDs + cache prices
  python -m autoheal.cli budget            # spend so far
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import get_settings
from .db import connect, rows
from .detector import run_checks
from .runtime import build_llm, new_run_tag, open_ledger, open_system
from .scenarios import SCENARIOS, setup_scenario


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
        except (AttributeError, ValueError):
            pass


def _printer(kind: str, d: dict) -> None:
    if kind == "route":
        print(f"  ⇢ model: {d['model']}  ({d['reason']})")
    elif kind in ("observe", "action", "blocked", "error"):
        mark = {"observe": "🔍", "action": "🔧", "blocked": "⛔", "error": "⚠️"}[kind]
        print(f"  {mark} step {d['step']} {d['name']} {json.dumps(d['args'])[:90]}")
        if kind != "observe":
            print(f"      → {json.dumps(d['result'], default=str)[:150]}")
    elif kind == "verify":
        print(f"  🧪 verification: {'PASSED' if d['result']['passed'] else 'FAILED'}")
    elif kind == "escalate":
        print(f"  🧑‍💻 escalated: {d['reason'][:140]}")


def main(argv=None) -> None:
    _utf8()
    p = argparse.ArgumentParser(prog="autoheal")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scenarios")
    d = sub.add_parser("demo", help="reset, inject a scenario, run the agent, print the report")
    d.add_argument("scenario", choices=list(SCENARIOS))
    d.add_argument("--llm", default="openrouter", choices=["openrouter", "playbook"])
    h = sub.add_parser("heal", help="run the agent on the current system state")
    h.add_argument("--llm", default="openrouter", choices=["openrouter", "playbook"])
    i = sub.add_parser("inject")
    i.add_argument("scenario", choices=list(SCENARIOS))
    sub.add_parser("status")
    e = sub.add_parser("eval")
    e.add_argument("--agents", nargs="+", default=["playbook"], choices=["openrouter", "playbook"])
    e.add_argument("--runs", type=int, default=1)
    e.add_argument("--scenarios", nargs="*", choices=list(SCENARIOS))
    m = sub.add_parser("models")
    m.add_argument("--suggest", action="store_true", help="list cheap tool-capable models")
    sub.add_parser("budget")
    a = p.parse_args(argv)
    s = get_settings()

    if a.cmd == "scenarios":
        for sc in SCENARIOS.values():
            print(f"{sc.name:22} [{sc.challenge_item:8}] {sc.title} — {sc.description}")
    elif a.cmd in ("demo", "inject"):
        conn = connect(s.db_path)
        setup_scenario(conn, a.scenario, seed=s.seed)
        print(f"Injected '{a.scenario}'. Symptoms: {[f.check for f in run_checks(conn)] or 'none'}")
        if a.cmd == "demo":
            _heal(s, a.llm)
    elif a.cmd == "heal":
        _heal(s, a.llm)
    elif a.cmd == "status":
        conn, _ = open_system(s)
        for f in run_checks(conn):
            print(f"- {f.check} ({f.severity}): {f.detail}")
        print("healthy" if not run_checks(conn) else "")
    elif a.cmd == "eval":
        from .evaluate import run_eval
        rep = run_eval(s, a.agents, a.runs, a.scenarios)
        print(json.dumps(rep["summary"], indent=2))
        print(f"\nSaved to data/eval/{rep['tag']}.json (+ .csv, latest.json)")
    elif a.cmd == "models":
        from .llm import fetch_model_catalog, suggest_tool_models
        if a.suggest:
            for mdl in suggest_tool_models(s):
                print(f"{mdl['id']:55} in ${mdl['in']}/M  out ${mdl['out']}/M")
        else:
            print(json.dumps(fetch_model_catalog(s, [s.cheap_model, s.strong_model]), indent=2))
            print("Prices cached to data/model_prices.json")
    elif a.cmd == "budget":
        led = open_ledger(s)
        print(f"Total spent: ${led.total():.4f} of ${s.max_total_usd:.2f} cap")
        for r in rows(led.conn, "SELECT model, COUNT(*) calls, SUM(prompt_tokens) tin, SUM(completion_tokens) tout, "
                                "ROUND(SUM(cost_usd), 4) usd FROM llm_usage GROUP BY model"):
            print(f"  {r['model']:45} calls={r['calls']:4} in={r['tin']} out={r['tout']} ${r['usd']}")


def _heal(s, llm_kind: str) -> None:
    from .agent import AutoHealAgent
    conn, shop = open_system(s)
    ledger = open_ledger(s, run_tag=new_run_tag("cli"))
    agent = AutoHealAgent(conn, shop, build_llm(llm_kind, conn, s, ledger), s, ledger, on_event=_printer)
    res = agent.run()
    print("\n" + res.report_md)


if __name__ == "__main__":
    from .llm import LLMError
    try:
        main()
    except LLMError as err:
        print(f"\n❌ {err}")
        sys.exit(1)
    except BrokenPipeError:
        pass
