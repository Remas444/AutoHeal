"""Safety & Audit: approvals, guardrails, the action record, session usage and technical details."""
import json
import re

import pandas as pd
import streamlit as st

from autoheal.db import rows
from autoheal.tools import RISK_ORDER, TOOLS, ToolContext, ToolError, execute
from autoheal_ui import session, theme
from autoheal_ui.content import ACTION_TITLES, ENGINE_LABELS, RISK_LABELS

c = session.current()
s = c.settings
ss = st.session_state

theme.hero("Safety & audit", "What AutoHeal is allowed to do, what it did, and what it is waiting for a human to decide.")


def _watch_run() -> None:
    """While a recovery runs, wait for it to finish, then redraw so its approvals appear and the controls unlock."""
    if c.run_active:
        ss.safety_watching = True
    elif ss.get("safety_watching"):
        ss.safety_watching = False
        st.rerun()


st.fragment(_watch_run, run_every="1s" if c.run_active else None)()

pending = rows(c.conn, "SELECT COUNT(*) AS n FROM approvals WHERE status='pending'")[0]["n"]
approvals_tab, guardrails_tab, record_tab, usage_tab, advanced_tab = st.tabs(
    [f"Human approvals ({pending})" if pending else "Human approvals", "Guardrails", "Action record", "Session Usage",
     "Advanced settings"])

# -------------------------------------------------------------------- approvals
with approvals_tab:
    requests = rows(c.conn, "SELECT * FROM approvals ORDER BY id DESC")
    if not requests:
        with st.container(border=True):
            theme.empty_state("No approval requests",
                              "Actions above the allowed risk level wait here for a human decision. To see one, set "
                              "Automatic actions allowed to Low only in Advanced settings, then recover from Orders stuck "
                              "before payment.")
    for ap in requests:
        with st.container(border=True):
            info, status = st.columns([5, 1])
            info.markdown(f"**{ACTION_TITLES.get(ap['tool'], ap['tool'])}**")
            info.caption(f"Request #{ap['id']} · incident #{ap['incident_id']} · {ap['risk'].capitalize()} risk · "
                         f"Tool `{ap['tool']}` · Arguments `{ap['args_json']}`")
            with status:
                st.badge(ap["status"].capitalize(), color={"pending": "orange", "approved": "green", "rejected": "gray",
                                                           "failed": "red"}.get(ap["status"], "gray"))
            if ap.get("result_json"):
                st.code(ap["result_json"], wrap_lines=True)
            if ap["status"] == "pending":
                yes, no, _ = st.columns([2, 1, 5])
                if yes.button("Approve and execute", key=f"approve-{ap['id']}", type="primary", width="stretch",
                              disabled=c.run_active):
                    try:
                        out = execute(ToolContext(c.conn, c.shop, ap["incident_id"]), ap["tool"], json.loads(ap["args_json"]))
                        c.conn.execute("UPDATE approvals SET status='approved', result_json=? WHERE id=?",
                                       (json.dumps(out, default=str), ap["id"]))
                    except ToolError as e:
                        c.conn.execute("UPDATE approvals SET status='failed', result_json=? WHERE id=?", (str(e), ap["id"]))
                    c.conn.commit()
                    st.rerun()
                if no.button("Reject", key=f"reject-{ap['id']}", width="stretch", disabled=c.run_active):
                    c.conn.execute("UPDATE approvals SET status='rejected' WHERE id=?", (ap["id"],))
                    c.conn.commit()
                    st.rerun()

# ------------------------------------------------------------------- guardrails
def _policy(tool) -> str:
    if tool.terminal:
        return "Closes the incident" if tool.name == "escalate_to_human" else "Closes only if verification passes"
    if not tool.mutating:
        return "Always allowed" if not tool.advances_time else "Allowed, within a time budget"
    return "Automatic" if RISK_ORDER[tool.risk] <= RISK_ORDER[s.max_auto_risk] else "Needs human approval"


with guardrails_tab:
    st.markdown(f"""
- **Riskier actions wait for a human.** Only actions up to {s.max_auto_risk} risk run automatically.
- **Paid orders are not re-charged.** Payment retries skip any order with a payment record.
- **The SQL investigation tool is read-only.** It cannot change data.
- **Repeated repairs must show progress.** An identical repair may repeat only while it helps.
- **Resolved means verified.** An incident closes as resolved only if independent verification passes.
""")
    st.markdown("**Limits for each incident.** Reaching any limit ends in an escalation to a human, never a silent stop.")
    theme.tiles([
        theme.Tile("Highest automatic risk", s.max_auto_risk.capitalize(), "info", "Change in Advanced settings"),
        theme.Tile("Dry run", "On" if s.dry_run else "Off", "warning" if s.dry_run else "", "Proposes without executing"),
        theme.Tile("Actions", s.max_mutations),
        theme.Tile("Steps", s.max_steps),
        theme.Tile("Verification attempts", s.max_verify_attempts),
        theme.Tile("Blocks before stopping", s.max_guard_blocks),
    ], "secondary")
    with st.expander("How each guardrail works", icon=":material/shield:"):
        st.markdown("""
- **Payment safety.** Retrying stuck payments never re-charges an order that already has a payment record. If every stuck order is already paid, the tool refuses and changes nothing.
- **Read-only investigation.** The SQL tool is restricted by SQLite's authorizer and cannot read internal tables.
- **Progress-aware repeat guard.** An identical repair may repeat only while it makes measurable progress.
- **Independent verification.** Finishing an incident always triggers probe orders and a full re-check. Every limit ends in an escalation, never a silent stop.
""")
    with st.expander("Tool policy", icon=":material/policy:"):
        st.table(pd.DataFrame([{"Tool": t.name.replace("_", " ").capitalize(), "Risk": "Read-only" if t.risk == "none" else t.risk.capitalize(),
                                "Policy now": _policy(t)} for t in TOOLS.values()]),
                 hide_index=True, alt="Every tool with its risk level and current policy")

# ---------------------------------------------------------------- action record
with record_tab:
    records = rows(c.conn, "SELECT incident_id, step, kind, name, args_json, result_json FROM agent_steps "
                           "WHERE kind IN ('action', 'blocked', 'verify') OR (kind='error' AND name IN ({})) "
                           "ORDER BY id".format(",".join("?" * len(ACTION_TITLES))), tuple(ACTION_TITLES))
    if records:
        words = {"action": "Executed", "blocked": "Blocked", "verify": "Verified", "error": "Failed or refused"}


        def _decision(r: dict) -> str:
            if r["kind"] == "action" and '"dry_run": true' in (r["result_json"] or ""):
                return "Dry run, not executed"
            if r["kind"] == "verify":
                return "Verification passed" if '"passed": true' in (r["result_json"] or "") else "Verification failed"
            return words[r["kind"]]


        st.dataframe(pd.DataFrame([{"Incident": r["incident_id"], "Step": r["step"], "Record": _decision(r),
                                    "Action": "Independent verification" if r["kind"] == "verify"
                                    else ACTION_TITLES.get(r["name"], r["name"]),
                                    "Arguments": "" if r["kind"] == "verify" else r["args_json"],
                                    "Result": r["result_json"]} for r in records]),
                     hide_index=True, width="stretch", height=420,
                     column_config={"Result": st.column_config.TextColumn(width="large")}, alt="Audit record of actions")
    else:
        st.caption("No actions recorded.")
    st.caption("Records live with the simulated system, so Reset and Inject Fault clear them.")

# ---------------------------------------------------------------- session usage
with usage_tab:
    use = session.session_usage(c)
    total = session.ledger_total(c)
    theme.tiles([
        theme.Tile("This session", f"${use['cost']:.4f}", "info", f"{use['calls']} LLM call{'s' * (use['calls'] != 1)}"),
        theme.Tile("Tokens this session", f"{use['prompt_tokens'] + use['completion_tokens']:,}",
                   caption=f"{use['prompt_tokens']:,} in, {use['completion_tokens']:,} out"),
        theme.Tile("Per-incident cap", f"${s.max_incident_usd:.2f}", caption="Reaching it ends in an escalation"),
        theme.Tile("Local usage, all sessions", f"${total:.4f}", caption=f"Of the ${s.max_total_usd:.2f} cap on this machine"),
    ], "secondary")
    st.progress(min(total / s.max_total_usd, 1.0) if s.max_total_usd else 0.0,
                text=f"Local budget used: \\${total:.4f} of \\${s.max_total_usd:.2f}")
    st.caption("Live figures from this machine's ledger. The Playbook makes no LLM calls, so it always adds $0.")
    usage = [dict(r) for r in c.usage.execute(
        "SELECT datetime(ts, 'unixepoch', 'localtime') AS time, run_tag, incident_id, model, purpose, prompt_tokens, "
        "completion_tokens, ROUND(cost_usd, 6) AS cost_usd, cost_source, latency_ms, ok FROM llm_usage "
        "ORDER BY id DESC LIMIT 500").fetchall()]
    if usage:
        with st.expander("LLM calls by model", icon=":material/table_chart:"):
            st.dataframe(pd.DataFrame(usage).groupby("model")[["prompt_tokens", "completion_tokens", "cost_usd"]].sum(),
                         width="stretch", alt="LLM usage totals per model")
        with st.expander("All recorded LLM calls", icon=":material/list_alt:"):
            st.dataframe(pd.DataFrame(usage), width="stretch", hide_index=True, alt="Recorded LLM calls")
    else:
        st.caption("No LLM calls recorded on this machine.")

# -------------------------------------------------------------- advanced settings
with advanced_tab:
    # Widget values are cleared when a page is left, so the choices live in plain session state between visits.
    ss.setdefault("risk", "medium")
    ss.setdefault("dry_run", False)
    ss.setdefault("risk_widget", ss.risk)
    ss.setdefault("dry_run_widget", ss.dry_run)
    with st.container(border=True):
        st.selectbox("Automatic actions allowed", list(RISK_LABELS), key="risk_widget", disabled=c.run_active,
                     format_func=RISK_LABELS.get, on_change=lambda: ss.update(risk=ss.risk_widget),
                     help="Actions above this risk level are never executed automatically. They are filed under Human "
                          "approvals.")
        st.toggle("Dry run", key="dry_run_widget", disabled=c.run_active, on_change=lambda: ss.update(dry_run=ss.dry_run_widget),
                  help="The recovery engine proposes actions, but nothing is executed.")
    with st.expander("Recovery engine configuration", icon=":material/settings:"):
        st.dataframe(pd.DataFrame([
            {"Setting": "LLM recovery", "Value": "Configured" if c.llm_configured else "Not configured (OPENROUTER_API_KEY is not set)"},
            {"Setting": "Engines", "Value": ", ".join(ENGINE_LABELS.values())},
            {"Setting": "Cheap model (single symptom)", "Value": s.cheap_model},
            {"Setting": "Strong model (several symptoms, or after a stall)", "Value": s.strong_model},
            {"Setting": "Request timeout", "Value": f"{s.request_timeout_s:g} s"},
            {"Setting": "Local budget cap", "Value": f"${s.max_total_usd:.2f}"},
            {"Setting": "Per-incident budget cap", "Value": f"${s.max_incident_usd:.2f}"},
            {"Setting": "Wait budget per incident", "Value": f"{s.max_wait_ticks} ticks"},
            {"Setting": "Simulation seed", "Value": str(s.seed)},
        ]), hide_index=True, width="stretch", alt="Recovery engine configuration")

    ids = session.incident_ids(c.conn)
    if ids:
        pick = st.selectbox("Incident", ids, format_func=lambda i: f"Incident #{i}", key="tech-incident")
        incident, steps = session.load_incident(c.conn, pick)
        with st.expander("Raw agent trace", icon=":material/data_object:"):
            if steps:
                st.caption("As stored by the runtime. Playbook rows name a model for routing bookkeeping only; the Playbook "
                           "makes no LLM calls.")
                st.dataframe(pd.DataFrame(steps).drop(columns=["ts", "incident_id", "id"]), hide_index=True, width="stretch",
                             alt="Raw agent step trace")
            else:
                st.caption("No steps recorded.")
        with st.expander("Stored incident report", icon=":material/description:"):
            # The stored report is produced by the evaluated runtime and is shown as recorded, minus its decorative heading icon.
            report_md = re.sub(r"^# \W+ ", "# ", incident.get("report_md") or "", count=1)
            st.code(report_md or "This incident has no report yet.", language="markdown", wrap_lines=True)
    else:
        st.caption("Raw traces and reports appear here once an incident has run.")
