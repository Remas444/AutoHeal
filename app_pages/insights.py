"""System Insights: health, metrics, queue status, logs and configuration history."""
import pandas as pd
import streamlit as st

from autoheal.db import rows, scalar
from autoheal_ui import session, theme
from autoheal_ui.content import check_label

c = session.current()
ss = st.session_state

theme.hero("System insights", "What the simulated shop is doing right now: the same signals the recovery engine can read.")
st.caption("One tick is one step of simulated time. While a recovery runs, this page updates by itself.")


def _live() -> None:
    if c.run_active:
        ss.insights_watching = True
    elif ss.get("insights_watching"):  # the run just ended: one full redraw also stops the polling
        ss.insights_watching = False
        st.rerun()

    found = session.findings(c.conn)
    counts = session.system_counts(c.conn)
    oldest = scalar(c.conn, "SELECT MIN(enqueued_tick) FROM queue WHERE status='pending'")
    age = 0 if oldest is None else c.shop.tick_now - int(oldest)

    theme.tiles([
        theme.Tile("System condition", "Healthy" if not found else f"{len(found)} active symptom{'s' * (len(found) != 1)}",
                   "success" if not found else "danger"),
        theme.Tile("Queue depth", f"{counts['queued']} of {c.shop.cfg('queue.max_depth')}",
                   caption=f"{c.shop.cfg('queue.worker_batch')} shipped per tick"),
        theme.Tile("Oldest message", f"{age} tick{'s' * (age != 1)}"),
        theme.Tile("Dead letters", counts["dead_letters"], "danger" if counts["dead_letters"] else ""),
        theme.Tile("Workflows waiting", counts["waiting"]),
        theme.Tile("Simulation tick", c.shop.tick_now),
    ], "secondary")

    st.subheader("Health checks", anchor=False)
    if found:
        for f in found:
            (st.error if f.severity == "critical" else st.warning)(
                f"**{check_label(f.check)}**: {f.detail}",
                icon=":material/error:" if f.severity == "critical" else ":material/warning:")
    else:
        st.success("All health checks pass. These checks are plain code and cost nothing to run.",
                   icon=":material/check_circle:")

    st.subheader("Metrics", anchor=False)
    metrics = rows(c.conn, "SELECT tick, name, value FROM metrics WHERE tick > ?", (c.shop.tick_now - 40,))
    if metrics:
        df = pd.DataFrame(metrics).pivot(index="tick", columns="name", values="value").fillna(0)
        groups = [("Payments", {"payments_ok": "Succeeded", "payment_timeouts": "Timed out", "payment_errors": "Errored"},
                   "Per tick"),
                  ("Queue", {"queue_depth": "Queue depth", "queue_dropped": "Dropped", "dead_letter_unreplayed": "Dead letters"},
                   "Messages"),
                  ("Throughput", {"orders_created": "Orders created", "shipments": "Shipments"}, "Per tick"),
                  ("Workflows", {"workflows_waiting": "Waiting", "workflows_completed": "Completed"}, "Workflows")]
        grid = st.columns(2, gap="small")
        for i, (title, series, unit) in enumerate(groups):
            with grid[i % 2].container(border=True):
                st.markdown(f"**{title}**")
                view = df[[k for k in series if k in df]].rename(columns=series)
                st.line_chart(view, height=250, x_label="Simulation tick", y_label=unit, alt=f"{title} over simulation ticks")
        with st.expander("View chart data", icon=":material/table_chart:"):
            st.dataframe(df, width="stretch", alt="Metric values per simulation tick")
    else:
        with st.container(border=True):
            theme.empty_state("No metrics yet", "The simulation has not run any ticks. Inject a fault or generate test traffic "
                                                "on the Operations page.")

    st.subheader("Logs", anchor=False)
    levels = st.pills("Log level", ["ERROR", "WARN", "INFO"], selection_mode="multi", default=["ERROR", "WARN"], key="log_levels",
                      format_func=str.capitalize)
    if levels:
        logs = rows(c.conn, f"SELECT tick, service, level, message FROM logs WHERE level IN ({','.join('?' * len(levels))}) "
                            "ORDER BY id DESC LIMIT 200", tuple(levels))
    else:
        logs = []
    if logs:
        with st.container(height=340 if len(logs) > 6 else "content", border=False):
            st.table(pd.DataFrame(logs).rename(columns=str.capitalize), hide_index=True, alt="Recent system logs")
    else:
        st.caption("No log entries at the selected levels." if levels else "Select a log level to see entries.")

    st.subheader("Configuration history", anchor=False)
    changes = rows(c.conn, "SELECT tick, key, old_value, new_value, actor, note FROM config_history "
                           "WHERE actor != 'bootstrap' ORDER BY id DESC")
    if changes:
        st.table(pd.DataFrame(changes).rename(columns={"tick": "Tick", "key": "Setting", "old_value": "From",
                                                       "new_value": "To", "actor": "Changed by", "note": "Note"}),
                 hide_index=True, alt="Configuration change history")
    else:
        st.caption("No configuration changes since the system started.")
    with st.expander("Current configuration", icon=":material/settings:"):
        config = rows(c.conn, "SELECT key, value, updated_tick FROM config ORDER BY key")
        if config:
            st.table(pd.DataFrame(config).rename(columns={"key": "Setting", "value": "Value", "updated_tick": "Updated at tick"}),
                     hide_index=True, alt="Current configuration")


st.fragment(_live, run_every="2s" if c.run_active else None)()
