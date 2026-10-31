"""Activity feed and incident detail, shared by the live console and the recorded evaluation traces."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from .activity import DRY_RUN, EXECUTED, REFUSED, REJECTED, STAGES, TOOL_FAILED, ActivityEvent, Outcome
from .content import ACTION_TITLES, check_label

TONE_ICONS = {"info": ":material/radio_button_checked:", "success": ":material/check_circle:",
              "warning": ":material/warning:", "danger": ":material/error:"}
_DISPLAY_LIMIT = 6000
_STAGE_NAMES = {"Outcome": "Resolve / Escalate"}
_EFFECT_WORDS = {EXECUTED: "Executed", REJECTED: "Blocked", REFUSED: "Refused", DRY_RUN: "Dry run, not executed",
                 TOOL_FAILED: "Failed"}


def _show_payload(label: str, value: Any) -> None:
    if value in (None, "", {}, []):
        return
    st.caption(label)
    text = json.dumps(value, indent=2, default=str, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    if len(text) > _DISPLAY_LIMIT:
        text = text[:_DISPLAY_LIMIT] + f"\n... [display limited; {len(text) - _DISPLAY_LIMIT} more characters]"
    st.code(text, language="json" if isinstance(value, (dict, list)) else None, wrap_lines=True)


def activity_feed(events: list[ActivityEvent], key: str, height: int | None = 560, follow: bool = False) -> None:
    """Interactive feed: filter by lifecycle stage, expand any event for its tool, arguments and actual result."""
    counts = {s: sum(e.stage == s for e in events) for s in STAGES}
    options = ["All"] + [s for s in STAGES if counts[s]]
    choice = st.pills("Filter by stage", options, default="All", key=f"{key}-filter", label_visibility="collapsed",
                      format_func=lambda s: f"All {len(events)}" if s == "All" else f"{_STAGE_NAMES.get(s, s)} {counts.get(s, 0)}")
    shown = [(i, e) for i, e in enumerate(events) if choice in (None, "All") or e.stage == choice]
    box = st.container(height=height, border=False, autoscroll=True) if follow else (
        st.container(height=height, border=False) if height else st.container())
    with box:
        for i, e in shown:
            with st.expander(e.title, icon=TONE_ICONS.get(e.tone), type="step", key=f"{key}-tone-{e.tone}-event-{i}"):
                if e.summary:
                    st.markdown(_plain(e.summary))
                facts = [e.stage]
                if e.step:
                    facts.append(f"Step {e.step}")
                if e.tool:
                    facts.append(f"Tool: `{e.tool}`")
                if e.model:
                    facts.append(f"Model: `{e.model}`")
                if e.elapsed_s is not None:
                    facts.append(f"+{e.elapsed_s:g} s")
                st.caption(" · ".join(facts))
                _show_payload("Arguments", e.args)
                _show_payload("Result", e.result if e.kind != "thought" else None)


def _plain(text: str) -> str:
    """Runtime text is data, not Markdown: keep characters such as * _ $ ` from being interpreted."""
    out = str(text)
    for ch in "\\`*_$[]#~:":
        out = out.replace(ch, "\\" + ch)
    return out


# ------------------------------------------------------------------ incident detail
def _symptom_table(items: list[dict]) -> None:
    st.table(pd.DataFrame([{"Symptom": check_label(f["check"]),
                            "Severity": "Critical" if f.get("severity") == "critical" else "Warning",
                            "Detail": f.get("detail", "")} for f in items]),
             hide_index=True, alt="Detected symptoms")


def _verification_result(report: dict, status: str | None) -> None:
    checks = report.get("verifications") or []
    if not checks:
        st.caption({None: "Not run yet.", "healthy": "Not run: no action was taken."}.get(status, "Did not run for this incident."))
        return
    for n, v in enumerate(checks, 1):
        verdict = "Passed" if v.get("passed") else "Failed"
        attempt = f"Attempt {n}: {verdict}" if len(checks) > 1 else verdict
        st.markdown(f"**{attempt}.** {v.get('probe_fulfilled')}/{v.get('probe_orders')} probe orders fulfilled over "
                    f"{v.get('ticks_run')} ticks.")
        if v.get("findings"):
            _symptom_table(v["findings"])
    st.caption("Plain code sends probe orders through the real pipeline and re-runs every health check. It does not rely "
               "on the recovery engine's own claim.")


def _incident_summary(report: dict, events: list[ActivityEvent], status: str | None) -> None:
    with st.container(border=True):
        st.markdown("**:material/troubleshoot: Root cause**")
        causes = report.get("root_causes") or []
        suspected = (report.get("escalation") or {}).get("suspected_categories")
        for rc in causes:
            st.markdown(f"**{_plain(str(rc.get('category', 'unknown')).replace('_', ' ').capitalize())}**: "
                        f"{_plain(rc.get('cause', ''))}")
        if not causes and suspected:
            cats = ", ".join(c.replace("_", " ") for c in suspected)
            st.markdown(f"Not established. Suspected categories named in the escalation: **{_plain(cats)}**.")
        elif not causes:
            st.caption({None: "Not established yet.", "healthy": "No fault to diagnose."}.get(status, "No root cause has been established."))

    with st.container(border=True):
        st.markdown("**:material/build: Recovery action**")
        taken = [e for e in events if e.effect]
        for e in taken:
            detail = f" {_plain(e.summary)}" if e.summary else ""
            st.markdown(f"- **{ACTION_TITLES.get(e.tool or '', e.tool)}**: {_EFFECT_WORDS[e.effect]}.{detail}")
        if not taken:
            st.caption("No state-changing action was requested.")

    with st.container(border=True):
        st.markdown("**:material/verified: Verification result**")
        _verification_result(report, status)

    if report.get("escalation"):
        st.caption("Handed to a human: see the Escalation tab.")
    elif report.get("summary"):
        st.markdown(_plain(report["summary"]))


def incident_detail(report: dict, events: list[ActivityEvent], approvals: list[dict],
                    findings: list[dict] | None = None, status: str | None = None) -> None:
    """Root cause, recovery action and verification first; symptoms, evidence, actions and escalation one tab away."""
    summary, symptoms, evidence, actions, escalation = st.tabs(
        ["Summary", "Symptoms", "Evidence", "Action history", "Escalation"])

    with summary:
        _incident_summary(report, events, status)

    with symptoms:
        items = report.get("findings") or findings or []
        if items:
            _symptom_table(items)
        else:
            st.caption("No symptoms detected.")

    with evidence:
        causes = report.get("root_causes") or []
        for rc in causes:
            with st.container(border=True):
                st.markdown(f"**{_plain(str(rc.get('category', 'unknown')).replace('_', ' ').capitalize())}**: "
                            f"{_plain(rc.get('cause', ''))}")
                for ev in rc.get("evidence", []):
                    st.markdown(f"- {_plain(ev)}")
                st.caption(f"Fix applied: {_plain(rc.get('fix_applied', ''))}")
        if causes and report.get("confidence") is not None:
            st.caption(f"Confidence reported by the recovery engine: {report['confidence']:.0%}. Recovery is judged "
                       "by independent verification, not by this figure.")
        reads = [e for e in events if e.kind == "observe"]
        if not reads:
            st.caption("No investigation was recorded for this incident. The rule-based Playbook applies fixed rules "
                       "without collecting evidence; the AI Agent's reads appear here.")
        else:
            st.table(pd.DataFrame([{"Step": e.step, "What was read": e.title, "Tool": e.tool, "Result": e.summary}
                                   for e in reads]),
                     hide_index=True, alt="Evidence collected by read-only tools")
            st.caption("Open an event in Agent Activity to see its full result.")

    with actions:
        taken = [e for e in events if e.effect]
        if taken:
            st.table(pd.DataFrame([{"Step": e.step, "Action": ACTION_TITLES.get(e.tool or "", e.tool),
                                    "Guardrail decision": _EFFECT_WORDS[e.effect], "Detail": e.summary} for e in taken]),
                     hide_index=True, alt="Recovery actions and guardrail decisions")
        else:
            st.caption("No state-changing action was requested.")
        if approvals:
            st.markdown("**Human approvals**")
            st.table(pd.DataFrame([{"Request": a["id"], "Action": ACTION_TITLES.get(a["tool"], a["tool"]),
                                    "Risk": a["risk"].capitalize(), "Status": a["status"].capitalize()}
                                   for a in approvals]),
                     hide_index=True, alt="Human approval requests")
            st.page_link("app_pages/safety_audit.py", label="Review approvals in Safety & Audit",
                         icon=":material/verified_user:")
        else:
            st.caption("No human approval was requested for this incident.")

    with escalation:
        e = report.get("escalation")
        if e:
            if e.get("summary"):
                st.markdown(_plain(e["summary"]))
            for action in e.get("recommended_actions", []):
                st.markdown(f"- {_plain(action)}")
            if e.get("open_symptoms"):
                st.caption("Still open when the incident was handed over")
                _symptom_table(e["open_symptoms"])
            elif e.get("open_symptoms") == []:
                st.caption("No symptoms were open at handover.")
        else:
            st.caption("This incident has not been escalated to a human.")
