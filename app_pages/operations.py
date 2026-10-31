"""Operations: the incident lifecycle and agent workspace."""
import streamlit as st

from autoheal.db import rows, wipe_system
from autoheal.scenarios import SCENARIOS, setup_scenario
from autoheal_ui import panels, recovery, session, theme
from autoheal_ui.activity import ActivityLog, classify_outcome, derive_lifecycle, parse_json
from autoheal_ui.content import ENGINE_LABELS, RISK_LABELS, SCENARIO_COPY

c = session.current()
ss = st.session_state

theme.hero("AutoHeal Operations Console", "Observe, investigate, recover, verify.")

# Widget values are cleared when a page is left, so the choices live in plain session state between visits.
ss.setdefault("scenario", list(SCENARIOS)[0])
ss.setdefault("engine", "openrouter" if c.llm_configured else "playbook")
ss.setdefault("scenario_widget", ss.scenario)
ss.setdefault("engine_widget", ss.engine)
ss.setdefault("injected", None)


def _snapshot() -> dict:
    """What the page shows right now: a live run if one is in progress, otherwise the latest stored incident."""
    run = recovery.current(c.settings.db_path)
    running = bool(run and run.active)
    found = session.findings(c.conn)
    incident = session.latest_incident(c.conn)
    if running:
        events = list(run.log.events)
        status, report = None, {}
    elif incident:
        incident, events = session.stored_events(c.conn, incident["id"])
        status, report = incident["status"], parse_json(incident.get("report_json")) or {}
    else:
        log = ActivityLog()
        if found:
            log.detect([f.model_dump() for f in found])
        events, status, report = log.events, None, {}
    return {"run": run, "running": running, "findings": found, "incident": incident, "events": events,
            "status": status, "report": report}


def _live_every() -> str | None:
    return "1s" if c.run_active else None


def _symptoms(n: int) -> str:
    return f"{n} active symptom{'s' * (n != 1)}"


def _status_tiles() -> None:
    snap = _snapshot()
    found, running, status = snap["findings"], snap["running"], snap["status"]

    if running:
        condition = theme.Tile("System condition", _symptoms(len(found)) if found else "Recovering", "warning",
                               "Recovery in progress")
    elif found:
        critical = sum(f.severity == "critical" for f in found)
        condition = theme.Tile("System condition", _symptoms(len(found)), "danger" if critical else "warning",
                               f"{critical} critical" if critical else "Warning level")
    else:
        condition = theme.Tile("System condition", "Healthy", "success", "All health checks pass")

    scenario_name = SCENARIO_COPY[ss.injected].title if ss.injected else ""
    if running:
        active = theme.Tile("Active incident", "Recovery in progress", "warning", scenario_name)
    elif status == "open":
        active = theme.Tile("Active incident", "Interrupted", "danger", "The last run did not finish")
    elif status == "escalated":
        active = theme.Tile("Active incident", f"#{snap['incident']['id']} escalated", "warning",
                            scenario_name or "Handed to a human")
    elif status in ("resolved", "healthy"):
        active = theme.Tile("Active incident", "None", "", f"#{snap['incident']['id']} closed")
    elif found:
        active = theme.Tile("Active incident", "Awaiting recovery", "danger", scenario_name or "Fault from an earlier session")
    else:
        active = theme.Tile("Active incident", "None", "", scenario_name or "No fault injected")

    engine = snap["run"].engine if running else ss.engine
    theme.tiles([condition, active, theme.Tile("Recovery engine", ENGINE_LABELS[engine], "info",
                                               "Calls an LLM (metered)" if engine == "openrouter" else "Offline, no cost")])
    pending = rows(c.conn, "SELECT COUNT(*) AS n FROM approvals WHERE status='pending'")[0]["n"]
    if pending:
        st.warning(f"**Human attention needed:** {pending} approval request{'s' * (pending != 1)} waiting for a decision.",
                   icon=":material/front_hand:")
        st.page_link("app_pages/safety_audit.py", label="Review approvals in Safety & Audit", icon=":material/verified_user:")


def _lifecycle() -> None:
    snap = _snapshot()
    running, status = snap["running"], snap["status"]
    theme.stepper(derive_lifecycle(snap["events"], running=running, symptoms_present=bool(snap["findings"]), status=status))
    if status:
        theme.banner(classify_outcome(snap["report"] or {"status": status}, snap["events"]))
    run = snap["run"]
    if run and run.error and not running and (status or snap["findings"]):  # Reset clears it along with the incident
        st.error(f"The recovery run stopped: {run.error}", icon=":material/error:")

    # A run finished since the last look: redraw everything, including the controls it had locked.
    if ss.get("watching_run") and not running:
        ss.watching_run = False
        st.rerun()


def _activity_panel() -> None:
    snap = _snapshot()
    st.subheader("Agent activity", anchor=False)
    if snap["running"]:
        st.caption("Live. Open an event for its tool and result.")
    elif snap["status"] is None:
        st.caption("Detected by the health checks. Recovery has not started.")
    else:
        st.caption(f"Record of incident #{snap['incident']['id']}. Open an event for its tool and result.")
    panels.activity_feed(snap["events"], key="ops", height=480, follow=snap["running"])


def _detail_panel() -> None:
    snap = _snapshot()
    st.subheader("Incident details", anchor=False)
    incident = snap["incident"]
    approvals = rows(c.conn, "SELECT * FROM approvals WHERE incident_id=? ORDER BY id", (incident["id"],)) if incident else []
    panels.incident_detail(snap["report"], snap["events"], approvals, findings=[f.model_dump() for f in snap["findings"]],
                           status=snap["status"])


# ---------------------------------------------------------------------- layout
if c.run_active:
    ss.watching_run = True  # also covers a run started before this page was opened

st.fragment(_status_tiles, run_every=_live_every())()

locked = c.run_active
with st.container(border=True):
    pick, engine_col = st.columns(2, gap="medium")
    with pick:
        scenario = st.selectbox("Scenario", list(SCENARIOS), key="scenario_widget", disabled=locked,
                                format_func=lambda n: SCENARIO_COPY[n].title)
        ss.scenario = scenario
        st.caption(SCENARIO_COPY[scenario].symptom)
    with engine_col:
        engine = st.selectbox("Recovery engine", list(ENGINE_LABELS), key="engine_widget", disabled=locked,
                              format_func=lambda k: ENGINE_LABELS[k] + (
                                  " (not configured)" if k == "openrouter" and not c.llm_configured else ""))
        ss.engine = engine
        needs_key = engine == "openrouter" and not c.llm_configured
        if needs_key:
            st.warning("LLM recovery is not configured: `OPENROUTER_API_KEY` is not set in `.env`. The Rule-based Playbook "
                       "runs offline.", icon=":material/key_off:")
        if c.settings.dry_run:
            st.caption("Dry run is on: actions are proposed, not executed.")
        if c.settings.max_auto_risk != "medium":
            st.caption(f"Automatic actions: {RISK_LABELS[c.settings.max_auto_risk].lower()}. Change in Safety & Audit.")

    found_now = session.findings(c.conn)
    latest = session.latest_incident(c.conn)
    latest_status = latest["status"] if latest else None
    ready = bool(found_now) or (ss.injected == "healthy" and not latest)
    start_is_next = ready and latest_status in (None, "open")

    # The teal button is always the next useful action.
    inject_col, start_col, reset_col = st.columns([5, 5, 2])
    if inject_col.button("Inject Fault", key="inject", type="secondary" if start_is_next else "primary", width="stretch",
                         icon=":material/bolt:", disabled=locked,
                         help="Starts a fresh simulated shop, injects this fault and runs traffic so symptoms appear."):
        setup_scenario(c.conn, scenario, seed=c.settings.seed)
        ss.injected = scenario
        st.rerun()
    if start_col.button("Start Recovery", key="start", type="primary" if start_is_next else "secondary",
                        width="stretch", icon=":material/play_arrow:", disabled=locked or needs_key or not ready,
                        help="Runs the selected recovery engine against the current symptoms."):
        recovery.start(c.settings, engine, c.session_tag)
        ss.watching_run = True
        st.rerun()
    if reset_col.button("Reset", key="reset", type="tertiary", width="stretch", icon=":material/restart_alt:", disabled=locked,
                        help="Returns the simulated shop to a clean, healthy state and clears its incidents."):
        wipe_system(c.conn)
        c.shop.bootstrap()
        c.shop.run(3)
        ss.injected = None
        st.rerun()

    with st.expander("Additional controls", icon=":material/tune:", key="additional-controls"):
        if st.button("Generate Test Traffic", key="traffic", disabled=locked,
                     help=f"Runs 3 ticks of orders using this scenario's traffic profile "
                          f"({SCENARIOS[scenario].spike or 12} orders per tick). Only needed if symptoms have not "
                          "appeared, or to let an active fault grow."):
            c.shop.run(3, n_orders=SCENARIOS[scenario].spike)
            st.rerun()

st.fragment(_lifecycle, run_every=_live_every())()

if locked or _snapshot()["events"]:
    activity_col, detail_col = st.columns([7, 6], gap="medium")
    with activity_col:
        st.fragment(_activity_panel, run_every=_live_every())()
    with detail_col:
        st.fragment(_detail_panel, run_every=_live_every())()
else:
    st.subheader("Agent activity", anchor=False)
    with st.container(border=True):
        theme.empty_state("No active incident", "Inject a fault to watch AutoHeal investigate, recover and verify.")
