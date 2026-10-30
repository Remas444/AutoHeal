"""AutoHeal dashboard.  Run:  streamlit run app.py"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from autoheal.agent import AutoHealAgent
from autoheal.config import get_settings
from autoheal.db import rows, wipe_system
from autoheal.detector import run_checks
from autoheal.evaluate import EVAL_DIR, run_eval
from autoheal.llm import LLMError
from autoheal.runtime import build_llm, new_run_tag, open_ledger, open_system
from autoheal.scenarios import SCENARIOS, setup_scenario
from autoheal.tools import ToolContext, ToolError, execute

st.set_page_config(page_title="AutoHeal", page_icon="🩺", layout="wide")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("🩺 AutoHeal")
    st.caption("Evidence-driven autonomous incident recovery")
    agent_kind = st.selectbox("Agent brain", ["openrouter", "playbook"],
                              format_func=lambda k: {"openrouter": "LLM agent (OpenRouter)",
                                                     "playbook": "Rule-based baseline (offline, $0)"}[k])
    risk = st.selectbox("Max risk without human approval", ["medium", "low", "high"])
    dry = st.toggle("Dry-run (agent proposes, nothing executes)", value=False)
    settings = get_settings(max_auto_risk=risk, dry_run=dry)
    st.caption(f"Cheap model: `{settings.cheap_model}`  \nStrong model: `{settings.strong_model}`")

    st.divider()
    st.subheader("💥 Chaos")
    scenario = st.selectbox("Scenario", list(SCENARIOS), format_func=lambda n: f"{SCENARIOS[n].title}")
    st.caption(SCENARIOS[scenario].description)

conn, shop = open_system(settings)
ledger = open_ledger(settings, run_tag=new_run_tag("dashboard"))

with st.sidebar:
    c1, c2 = st.columns(2)
    if c1.button("Inject", type="primary", width="stretch"):
        setup_scenario(conn, scenario, seed=settings.seed)
        st.rerun()
    if c2.button("Reset", width="stretch"):
        wipe_system(conn)
        shop.bootstrap()
        shop.run(3)
        st.rerun()
    if st.button("▶ Run 3 ticks of traffic", width="stretch"):
        shop.run(3, n_orders=SCENARIOS[scenario].spike)
        st.rerun()
    st.divider()
    spent = ledger.total()
    st.subheader("💰 LLM budget")
    st.progress(min(spent / settings.max_total_usd, 1.0) if settings.max_total_usd else 0.0,
                text=f"${spent:.4f} / ${settings.max_total_usd:.2f}")
    st.caption(f"Per-incident cap: ${settings.max_incident_usd:.2f}")

# --------------------------------------------------------------- top status
status_slot = st.empty()


def render_status() -> list:
    """KPI row + alerts. Redrawn after an agent run so it reflects the recovered state."""
    found = run_checks(conn)
    with status_slot.container():
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("System", "HEALTHY" if not found else f"{len(found)} ALERT(S)")
        k2.metric("Tick", shop.tick_now)
        k3.metric("Orders stuck (no payment)", rows(conn, "SELECT COUNT(*) n FROM orders WHERE status='created'")[0]["n"])
        k4.metric("Dead letters", rows(conn, "SELECT COUNT(*) n FROM dead_letter WHERE replayed=0")[0]["n"])
        k5.metric("Workflows waiting", rows(conn, "SELECT COUNT(*) n FROM workflows WHERE status='waiting'")[0]["n"])
        for f in found:
            (st.error if f.severity == "critical" else st.warning)(f"**{f.check}** — {f.detail}")
        if not found:
            st.success("All invariants pass.")
    return found


findings = render_status()

tab_heal, tab_sys, tab_appr, tab_eval, tab_spend = st.tabs(
    ["🤖 AutoHeal", "📈 System", "✋ Approvals", "🧪 Evaluation", "💰 Spend"])

# ----------------------------------------------------------------- autoheal
with tab_heal:
    if st.button("🩺 Run AutoHeal", type="primary", disabled=not findings):
        with st.status("AutoHeal is working…", expanded=True) as status:
            icons = {"observe": "🔍", "action": "🔧", "blocked": "⛔", "error": "⚠️", "verify": "🧪",
                     "thought": "💭", "escalate": "🧑‍💻", "route": "🧭"}

            def on_event(kind: str, d: dict) -> None:
                if kind == "route":
                    status.write(f"🧭 Model → `{d['model']}` ({d['reason']})")
                elif kind == "thought":
                    status.write(f"💭 {str(d['result'])[:300]}")
                elif kind == "verify":
                    ok = d["result"]["passed"]
                    status.write(f"🧪 Verification **{'PASSED' if ok else 'FAILED'}** — "
                                 f"{d['result']['probe_fulfilled']}/{d['result']['probe_orders']} probe orders shipped")
                elif kind == "escalate":
                    status.write(f"🧑‍💻 Escalated: {d['reason']}")
                elif kind in icons:
                    status.write(f"{icons[kind]} `{d['name']}` {json.dumps(d['args'])[:120]}")
                    if kind != "observe":
                        status.caption(f"→ {json.dumps(d['result'], default=str)[:220]}")

            try:
                agent = AutoHealAgent(conn, shop, build_llm(agent_kind, conn, settings, ledger), settings, ledger,
                                      on_event=on_event)
                res = agent.run()
                status.update(label=f"Incident #{res.incident_id}: {res.status.upper()} · ${res.cost_usd:.4f}",
                              state="complete" if res.status in ("resolved", "healthy") else "error")
                st.session_state["last_incident"] = res.incident_id
            except LLMError as e:
                status.update(label="LLM configuration problem", state="error")
                st.error(str(e))
        render_status()

    incidents = rows(conn, "SELECT id, status, opened_tick, closed_tick FROM incidents ORDER BY id DESC")
    if incidents:
        ids = [i["id"] for i in incidents]
        default = ids.index(st.session_state.get("last_incident", ids[0])) if st.session_state.get(
            "last_incident") in ids else 0
        pick = st.selectbox("Incident", ids, index=default,
                            format_func=lambda i: f"#{i} — {next(x['status'] for x in incidents if x['id'] == i)}")
        inc = rows(conn, "SELECT report_md FROM incidents WHERE id=?", (pick,))[0]
        left, right = st.columns([3, 2])
        with left:
            st.markdown(inc["report_md"] or "_running…_")
        with right:
            st.subheader("Agent timeline")
            steps = rows(conn, "SELECT step, kind, name, args_json, model FROM agent_steps WHERE incident_id=? "
                               "ORDER BY id", (pick,))
            if steps:
                st.dataframe(pd.DataFrame(steps), width="stretch", hide_index=True)
    else:
        st.info("Inject a scenario from the sidebar, then run AutoHeal.")

# ------------------------------------------------------------------- system
with tab_sys:
    m = rows(conn, "SELECT tick, name, value FROM metrics WHERE tick > ?", (shop.tick_now - 40,))
    if m:
        df = pd.DataFrame(m).pivot(index="tick", columns="name", values="value").fillna(0)
        a, b = st.columns(2)
        a.caption("Payments")
        a.line_chart(df[[c for c in ["payments_ok", "payment_timeouts", "payment_errors"] if c in df]])
        b.caption("Queue")
        b.line_chart(df[[c for c in ["queue_depth", "queue_dropped", "dead_letter_unreplayed"] if c in df]])
        a.caption("Throughput")
        a.line_chart(df[[c for c in ["orders_created", "shipments"] if c in df]])
        b.caption("Workflows")
        b.line_chart(df[[c for c in ["workflows_waiting", "workflows_completed"] if c in df]])
    st.subheader("Recent logs")
    lvl = st.multiselect("Level", ["ERROR", "WARN", "INFO"], default=["ERROR", "WARN"])
    logs = rows(conn, f"SELECT tick, service, level, message FROM logs WHERE level IN "
                      f"({','.join('?' * len(lvl)) or 'NULL'}) ORDER BY id DESC LIMIT 200", tuple(lvl))
    st.dataframe(pd.DataFrame(logs), width="stretch", hide_index=True)
    st.subheader("Config change history")
    st.dataframe(pd.DataFrame(rows(conn, "SELECT tick, key, old_value, new_value, actor, note FROM config_history "
                                         "WHERE actor != 'bootstrap' ORDER BY id DESC")),
                 width="stretch", hide_index=True)

# ---------------------------------------------------------------- approvals
with tab_appr:
    st.caption("Actions above the risk threshold are never executed by the agent. They wait here for a human.")
    pending = rows(conn, "SELECT * FROM approvals ORDER BY id DESC")
    if not pending:
        st.info("No approval requests.")
    for ap in pending:
        with st.container(border=True):
            st.write(f"**#{ap['id']}** · incident #{ap['incident_id']} · `{ap['tool']}` "
                     f"{ap['args_json']} · risk **{ap['risk']}** · status **{ap['status']}**")
            if ap["status"] == "pending":
                y, n = st.columns(2)
                if y.button("Approve & execute", key=f"y{ap['id']}"):
                    try:
                        out = execute(ToolContext(conn, shop, ap["incident_id"]), ap["tool"], json.loads(ap["args_json"]))
                        conn.execute("UPDATE approvals SET status='approved', result_json=? WHERE id=?",
                                     (json.dumps(out, default=str), ap["id"]))
                    except ToolError as e:
                        conn.execute("UPDATE approvals SET status='failed', result_json=? WHERE id=?", (str(e), ap["id"]))
                    conn.commit()
                    st.rerun()
                if n.button("Reject", key=f"n{ap['id']}"):
                    conn.execute("UPDATE approvals SET status='rejected' WHERE id=?", (ap["id"],))
                    conn.commit()
                    st.rerun()

# --------------------------------------------------------------- evaluation
# Committed final results (tag eval-final). Read-only: never rerun, never written, no LLM calls.
FINAL_SUITES = [
    ("Suite A: U1-U5 regression", "v321-suiteA-regression.json",
     "Development/regression evidence: AutoHeal v3.2 was improved after these cases were first observed.",
     "successes"),
    ("Suite B: P01-P10 prospective robustness", "v321-suiteB-prospective.json",
     "Prospective evidence: designed after the v3.2.1 freeze, by the same team (designer bias disclosed).",
     "correct outcomes"),
]
SYSTEM_NAMES = {"openrouter": "AutoHeal agent", "playbook": "Rule-based playbook"}


def for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Render mixed-type object columns as text so Streamlit/Arrow can show them without type warnings."""
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(lambda v: "" if v is None else str(v))
    return df


def final_suite_table(path) -> tuple[pd.DataFrame, dict]:
    rep = json.loads(path.read_text(encoding="utf-8"))
    cases: dict[str, dict] = {}
    for r in rep["results"]:
        c = cases.setdefault(r["case"], {"runs": 0, "openrouter": 0, "playbook": 0,
                                         "openrouter_outcomes": [], "playbook_outcomes": []})
        if r["agent"] == "openrouter":
            c["runs"] += 1
        c[r["agent"]] += bool(r["success"])
        c[f"{r['agent']}_outcomes"].append(r["outcome"])
    table = pd.DataFrame([{"Case": k, "Runs": v["runs"],
                           "AutoHeal agent": f"{v['openrouter']}/{v['runs']}",
                           "Rule-based playbook": f"{v['playbook']}/{v['runs']}",
                           "Agent outcomes": ", ".join(sorted(set(v["openrouter_outcomes"]))),
                           "Playbook outcomes": ", ".join(sorted(set(v["playbook_outcomes"])))}
                          for k, v in sorted(cases.items())])
    totals = {a: (sum(c[a] for c in cases.values()), sum(c["runs"] for c in cases.values()))
              for a in ("openrouter", "playbook")}
    return table, totals


with tab_eval:
    st.subheader("Final evaluation: AutoHeal v3.2.1 vs rule-based playbook")
    st.caption("Read-only view of the committed results (tag `eval-final`). Nothing here reruns an evaluation or "
               "calls an LLM. The two suites are separate kinds of evidence and are never combined.")
    for title, filename, evidence, unit in FINAL_SUITES:
        path = EVAL_DIR / filename
        st.markdown(f"#### {title}")
        st.caption(evidence)
        if not path.exists():
            st.warning(f"`data/eval/{filename}` not found.")
            continue
        table, totals = final_suite_table(path)
        m1, m2 = st.columns(2)
        for col, agent in ((m1, "openrouter"), (m2, "playbook")):
            k, n = totals[agent]
            col.metric(f"{SYSTEM_NAMES[agent]}: {unit}", f"{k}/{n}", f"{100 * k / n:.0f}%", delta_color="off")
        st.dataframe(table, width="stretch", hide_index=True)
    st.info("Suite B counts correct outcomes (recovery, correct escalation, or correctly doing nothing), not full "
            "recovery. The agent's whole Suite B lead comes from P04, P05 and P07, which were declared in advance as "
            "favouring it; the two systems tied on the neutral and playbook-favouring cases, and both missed P02. "
            "This does not establish general superiority. Full report: `docs/FINAL_EVALUATION_v321.md` "
            "(see the erratum in `docs/EVALUATION_NOTES.md`: the historical v3.1 total is 5/12, not 7/12).")

    st.divider()
    st.subheader("Local evaluation runs (this machine)")
    st.caption("Runs of the 7-scenario benchmark started here or from the CLI. These are local outputs, not the "
               "final evaluation. Each run uses a fresh in-memory system, so it never touches the live demo state.")
    e1, e2, e3 = st.columns(3)
    runs = e1.number_input("Runs per scenario", 1, 5, 1)
    agents = e2.multiselect("Agents", ["playbook", "openrouter"], default=["playbook"])
    if e3.button("Run evaluation", type="primary"):
        log = st.empty()
        lines: list[str] = []

        def prog(line: str) -> None:
            lines.append(line)
            log.code("\n".join(lines[-14:]))

        try:
            run_eval(settings, agents, int(runs), progress=prog)
        except LLMError as e:
            st.error(str(e))
    latest = EVAL_DIR / "latest.json"
    if latest.exists():
        rep = json.loads(latest.read_text(encoding="utf-8"))
        st.markdown(f"**Most recent local run:** `{rep['tag']}` (not the final evaluation)")
        summ = pd.DataFrame(rep["summary"]).T
        st.dataframe(for_display(summ.drop(columns=["per_scenario"], errors="ignore")), width="stretch")
        per = pd.DataFrame({k: v["per_scenario"] for k, v in rep["summary"].items()})
        st.dataframe(for_display(per), width="stretch")
        with st.expander("All runs"):
            st.dataframe(for_display(pd.DataFrame(rep["results"])), width="stretch", hide_index=True)
    else:
        st.caption("No local run yet.")

# -------------------------------------------------------------------- spend
with tab_spend:
    usage = rows(ledger.conn, "SELECT datetime(ts, 'unixepoch', 'localtime') AS time, run_tag, incident_id, model, "
                              "purpose, prompt_tokens, completion_tokens, ROUND(cost_usd, 6) AS cost_usd, "
                              "cost_source, latency_ms, ok FROM llm_usage ORDER BY id DESC LIMIT 500")
    if usage:
        udf = pd.DataFrame(usage)
        st.dataframe(udf.groupby("model")[["prompt_tokens", "completion_tokens", "cost_usd"]].sum(),
                     width="stretch")
        st.dataframe(udf, width="stretch", hide_index=True)
    else:
        st.info("No LLM calls yet.")
